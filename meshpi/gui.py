"""Tkinter touchscreen GUI for meshpi.

The GUI lives in the main thread. Meshtastic events arrive on background
threads via InterfaceManager and are pushed onto a thread-safe queue. The
Tk main loop drains the queue on a timer using after(), which is the
standard pattern for Tk + threads.

Sends are routed through the shared send_text callable so the GUI never
touches the serial interface directly.

The toolkit choice (Tkinter) is intentionally kept light for the 1 GB Pi 3.
The MessagingGui class is a thin facade so a Kivy implementation could
replace it later without touching app.py.
"""

from __future__ import annotations

import logging
import math
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import datetime, timezone
from tkinter import ttk
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


# Earth radius in km, used for the "farthest contact today" stat.
_EARTH_KM = 6371.0088

# Statute miles per degree of latitude (constant), used by the map's
# fixed-radius bbox helper and the scale bar.
_MI_PER_DEG_LAT = 69.0


def _haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in km."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(a))


# Meshtastic broadcast destination markers.
_BROADCAST_IDS = {"^all", "!ffffffff"}


def _classify_destination(
    to_id: str | None,
    channel: int | None,
    ch_names: dict[int, str],
    my_id: str | None,
) -> str:
    """Produce a short tag for the message list, e.g. '[ch:0 default]' or '[DM→us]'.

    Broadcasts get the channel tag; targeted packets to our node get DM→us;
    targeted packets we relayed/heard get DM→<short hex>.
    """
    if to_id is None or to_id.lower() in _BROADCAST_IDS:
        idx = channel if channel is not None else 0
        name = ch_names.get(int(idx), f"ch{idx}")
        return f"[ch:{idx} {name}]"
    if my_id and to_id.lower() == my_id.lower():
        return "[DM→us]"
    short = to_id[-4:] if to_id.startswith("!") else to_id
    return f"[DM→{short}]"


def _bbox_around(
    center_lat: float, center_lon: float, radius_mi: float,
) -> tuple[float, float, float, float]:
    """Square bbox around a center point with the given radius in miles.

    Longitude span is widened by 1/cos(lat) so that the projected map looks
    roughly square at the visible latitude rather than stretched east-west.
    """
    lat_delta = radius_mi / _MI_PER_DEG_LAT
    cos_lat = max(math.cos(math.radians(center_lat)), 0.01)
    lon_delta = radius_mi / (_MI_PER_DEG_LAT * cos_lat)
    return (
        center_lat - lat_delta,
        center_lat + lat_delta,
        center_lon - lon_delta,
        center_lon + lon_delta,
    )


def _bbox_with_padding(
    points: list[tuple[float, float]],
) -> tuple[float, float, float, float]:
    """Return (lat_min, lat_max, lon_min, lon_max) with 10% padding.

    Falls back to a small fixed box around the single point when all points
    coincide, so the projection does not divide by zero.
    """
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    lat_pad = max((lat_max - lat_min) * 0.1, 0.005)
    lon_pad = max((lon_max - lon_min) * 0.1, 0.005)
    return (lat_min - lat_pad, lat_max + lat_pad,
            lon_min - lon_pad, lon_max + lon_pad)


def _color_for_snr(snr: float | None) -> str:
    """Three-bucket color ramp: strong green, fair yellow, weak orange, unknown gray."""
    if snr is None:
        return "#888888"
    try:
        s = float(snr)
    except (TypeError, ValueError):
        return "#888888"
    if s >= 5:
        return "#7ed957"
    if s >= 0:
        return "#f5d04a"
    return "#f08a4b"


class MessagingGui:
    """Touchscreen-friendly Tkinter GUI.

    Public API used by app.py:
      .push_event(event_type, payload)  -- thread-safe, from any thread
      .show_notice(source, message)     -- thread-safe, surface a banner
      .run()                            -- blocks; call from the main thread
      .stop()                           -- thread-safe shutdown
    """

    def __init__(
        self,
        send_text: Callable[..., None],
        canned_messages: list[str],
        recent_messages_provider: Callable[[int], list[dict[str, Any]]],
        nodes_provider: Callable[[], list[dict[str, Any]]],
        my_position_provider: Callable[[], tuple[float | None, float | None]],
        channels_provider: Callable[[], list[tuple[int, str]]] | None = None,
        my_node_id_provider: Callable[[], str | None] | None = None,
        fullscreen: bool = True,
        display_timezone: str = "UTC",
    ):
        self._send_text = send_text
        self._canned = list(canned_messages)
        self._recent_messages = recent_messages_provider
        self._nodes = nodes_provider
        self._my_position = my_position_provider
        self._channels_provider = channels_provider or (lambda: [(0, "default")])
        self._my_node_id_provider = my_node_id_provider or (lambda: None)
        self._fullscreen = fullscreen
        self._tz = ZoneInfo(display_timezone) if display_timezone else timezone.utc

        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._notice_text = ""
        self._stop_event = threading.Event()

        # Tk widgets created in run() to keep all Tk on one thread.
        self.root: tk.Tk | None = None
        self._notebook: ttk.Notebook | None = None
        self._glance_vars: dict[str, tk.StringVar] = {}
        self._messages_text: tk.Text | None = None
        self._compose_var: tk.StringVar | None = None
        self._notice_label: ttk.Label | None = None

        # Channel selector state.
        self._channel_var: tk.StringVar | None = None
        self._channel_combo: ttk.Combobox | None = None
        # [(index, "name (ch:N)"), ...] kept in sync with the provider.
        self._channel_options: list[tuple[int, str]] = []

        # Map tab state.
        self._map_canvas: tk.Canvas | None = None
        self._map_status_var: tk.StringVar | None = None
        # Map viewing radius in statute miles. None means auto-fit to all
        # positioned nodes (the previous default). Touching the buttons in
        # the map control bar updates this and triggers a redraw.
        self._map_scale_miles: float | None = None
        self._map_scale_buttons: dict[str, ttk.Button] = {}

        # Virtual keyboard subprocess (matchbox-keyboard / wvkbd / onboard).
        self._kb_process: subprocess.Popen | None = None
        self._kb_button: ttk.Button | None = None

    # ----- thread-safe inputs -----

    def push_event(self, event_type: str, payload: Any) -> None:
        self._events.put((event_type, payload))

    def show_notice(self, source: str, message: str) -> None:
        self._events.put(("notice", f"[{source}] {message}"))

    def stop(self) -> None:
        self._stop_event.set()
        # Tear down any spawned virtual keyboard before destroying the window.
        self._kill_keyboard()
        # Schedule the destroy on the Tk thread.
        root = self.root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:  # noqa: BLE001
                pass

    # ----- virtual keyboard -----

    # Order matters: prefer Wayland-native, then X11 options. The first one
    # that is on PATH is used.
    _KEYBOARD_COMMANDS: tuple[tuple[str, list[str]], ...] = (
        ("wvkbd-mobintl", ["wvkbd-mobintl", "-L", "240"]),
        ("matchbox-keyboard", ["matchbox-keyboard"]),
        ("onboard", ["onboard"]),
        ("florence", ["florence"]),
    )

    def _toggle_keyboard(self) -> None:
        """Show or hide a virtual on-screen keyboard."""
        if self._kb_process is not None and self._kb_process.poll() is None:
            self._kill_keyboard()
            self._set_notice("keyboard hidden")
            return

        for name, argv in self._KEYBOARD_COMMANDS:
            if shutil.which(argv[0]) is None:
                continue
            try:
                self._kb_process = subprocess.Popen(  # noqa: S603
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._set_notice(f"keyboard: {name}")
                log.info("launched virtual keyboard: %s", name)
                return
            except Exception:  # noqa: BLE001
                log.exception("failed to launch %s", name)

        self._set_notice(
            "no on-screen keyboard installed; "
            "try: sudo apt install matchbox-keyboard"
        )

    def _kill_keyboard(self) -> None:
        proc = self._kb_process
        self._kb_process = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001
            log.debug("error terminating keyboard process", exc_info=True)

    # ----- Tk main loop -----

    def run(self) -> None:
        self.root = tk.Tk()
        self.root.title("meshpi")
        if self._fullscreen:
            self.root.attributes("-fullscreen", True)
        else:
            self.root.geometry("800x480")  # typical Pi 7" touch

        self._build_styles()
        self._build_layout()
        self._refresh_glance()
        self._refresh_messages()
        self.root.after(250, self._drain_events)
        self.root.after(5000, self._periodic_refresh)
        self.root.protocol("WM_DELETE_WINDOW", self.stop)
        self.root.mainloop()

    # ----- layout -----

    def _build_styles(self) -> None:
        assert self.root is not None
        style = ttk.Style(self.root)
        # Pick a theme that exists on Pi/Tk; "clam" is reliable.
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("DejaVu Sans", 18), padding=12)
        style.configure("Glance.TLabel", font=("DejaVu Sans", 14))
        style.configure("Heading.TLabel", font=("DejaVu Sans", 20, "bold"))
        style.configure("Notice.TLabel", font=("DejaVu Sans", 12), foreground="#aa3300")

    def _build_layout(self) -> None:
        assert self.root is not None
        self._notice_label = ttk.Label(
            self.root, text="", style="Notice.TLabel", anchor="w"
        )
        self._notice_label.pack(side="top", fill="x", padx=8, pady=(6, 0))

        self._notebook = ttk.Notebook(self.root)
        self._notebook.pack(fill="both", expand=True, padx=4, pady=4)

        self._notebook.add(self._build_glance_tab(), text="Glance")
        self._notebook.add(self._build_messages_tab(), text="Messages")
        self._notebook.add(self._build_map_tab(), text="Map")

    def _build_glance_tab(self) -> ttk.Frame:
        assert self.root is not None
        frame = ttk.Frame(self.root, padding=12)
        ttk.Label(frame, text="meshpi status", style="Heading.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        for key, label in [
            ("nodes_heard", "Nodes heard:"),
            ("last_msg", "Last message:"),
            ("farthest_km", "Farthest today:"),
            ("local_time", "Local time:"),
        ]:
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=label, style="Glance.TLabel", width=18).pack(side="left")
            var = tk.StringVar(value="--")
            self._glance_vars[key] = var
            ttk.Label(row, textvariable=var, style="Glance.TLabel").pack(side="left")

        # Compact node table.
        ttk.Label(frame, text="Nodes", style="Heading.TLabel").pack(
            anchor="w", pady=(16, 4)
        )
        cols = ("name", "last_heard", "battery", "snr")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=8)
        for col, label, w in [
            ("name", "Name", 220),
            ("last_heard", "Last heard", 180),
            ("battery", "Batt %", 80),
            ("snr", "SNR", 80),
        ]:
            tree.heading(col, text=label)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True)
        self._nodes_tree = tree
        return frame

    def _build_messages_tab(self) -> ttk.Frame:
        assert self.root is not None
        frame = ttk.Frame(self.root, padding=8)

        # IMPORTANT: pack the bottom controls FIRST so they reserve their
        # natural height. Then pack the messages-text frame last with
        # expand=True so it claims whatever vertical space is left over.
        # If we packed messages-text first, it would grab all the height
        # and push the compose / canned rows off the bottom of the screen
        # on the 800x480 Pi touchscreen.

        # Canned message grid (bottom-most). Chunk into rows of 3 for touch.
        canned = ttk.Frame(frame)
        canned.pack(side="bottom", fill="x", pady=(4, 0))
        per_row = 3
        for idx, text in enumerate(self._canned):
            r, c = divmod(idx, per_row)
            btn = ttk.Button(
                canned,
                text=text,
                style="Big.TButton",
                command=lambda t=text: self._send_canned(t),
            )
            btn.grid(row=r, column=c, sticky="nsew", padx=4, pady=4)
        for c in range(per_row):
            canned.grid_columnconfigure(c, weight=1)

        # Compose row (above the canned grid).
        compose = ttk.Frame(frame)
        compose.pack(side="bottom", fill="x", pady=(8, 6))
        self._compose_var = tk.StringVar()
        entry = ttk.Entry(compose, textvariable=self._compose_var, font=("DejaVu Sans", 16))
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        # Virtual keyboard toggle. Off by default; useful when typing
        # freeform messages on the touchscreen. Press again to dismiss.
        self._kb_button = ttk.Button(
            compose, text="Kbd", style="Big.TButton",
            command=self._toggle_keyboard,
        )
        self._kb_button.pack(side="right", padx=(0, 6))
        ttk.Button(
            compose, text="Send", style="Big.TButton", command=self._on_send_pressed
        ).pack(side="right")

        # Channel selector row (above the compose row).
        chrow = ttk.Frame(frame)
        chrow.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Label(chrow, text="Channel:", style="Glance.TLabel").pack(side="left")
        self._channel_var = tk.StringVar()
        self._channel_combo = ttk.Combobox(
            chrow,
            textvariable=self._channel_var,
            state="readonly",
            width=24,
            font=("DejaVu Sans", 14),
        )
        self._channel_combo.pack(side="left", padx=(8, 0))
        self._refresh_channels()

        # Recent messages text box (top, takes remaining height).
        top = ttk.Frame(frame)
        top.pack(side="top", fill="both", expand=True)
        self._messages_text = tk.Text(
            top, wrap="word", height=8, font=("DejaVu Sans Mono", 11)
        )
        self._messages_text.configure(state="disabled")
        scroll = ttk.Scrollbar(top, command=self._messages_text.yview)
        self._messages_text.configure(yscrollcommand=scroll.set)
        self._messages_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        return frame

    def _build_map_tab(self) -> ttk.Frame:
        """Simple offline scatter map. No tiles, no external deps.

        Uses an equirectangular projection. By default auto-fits to the
        bounding box of all positioned nodes. The scale buttons in the
        control bar switch to a fixed radius around our own position,
        which is what you want when a single distant node would otherwise
        squash the local neighborhood into a few pixels.
        """
        assert self.root is not None
        frame = ttk.Frame(self.root, padding=4)

        # Control bar: scale buttons on the left, status in the middle,
        # redraw on the right.
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 4))

        ttk.Label(bar, text="Scale:", style="Glance.TLabel").pack(side="left", padx=(4, 4))
        # (label, miles-or-None)
        scale_options: list[tuple[str, float | None]] = [
            ("1 mi", 1.0),
            ("10 mi", 10.0),
            ("25 mi", 25.0),
            ("Full", None),
        ]
        for label, miles in scale_options:
            btn = ttk.Button(
                bar, text=label,
                command=lambda m=miles: self._set_map_scale(m),
            )
            btn.pack(side="left", padx=2)
            self._map_scale_buttons[label] = btn

        self._map_status_var = tk.StringVar(value="(no positions yet)")
        ttk.Label(bar, textvariable=self._map_status_var, style="Glance.TLabel").pack(
            side="left", padx=12
        )
        ttk.Button(bar, text="Redraw", command=self._redraw_map).pack(side="right")

        self._map_canvas = tk.Canvas(
            frame, bg="#0b1020", highlightthickness=0
        )
        self._map_canvas.pack(fill="both", expand=True)
        # Redraw when the canvas resizes (window resize, tab switch).
        self._map_canvas.bind("<Configure>", lambda _e: self._redraw_map())
        self._update_scale_button_styles()
        return frame

    def _set_map_scale(self, miles: float | None) -> None:
        """Change the map's viewing radius and redraw."""
        self._map_scale_miles = miles
        self._update_scale_button_styles()
        self._redraw_map()

    def _update_scale_button_styles(self) -> None:
        """Mark the active scale button so the user can see what's selected."""
        if not self._map_scale_buttons:
            return
        active_label = self._scale_label(self._map_scale_miles)
        for label, btn in self._map_scale_buttons.items():
            if label == active_label:
                btn.state(["pressed"])
            else:
                btn.state(["!pressed"])

    @staticmethod
    def _scale_label(miles: float | None) -> str:
        if miles is None:
            return "Full"
        if miles == int(miles):
            return f"{int(miles)} mi"
        return f"{miles:g} mi"

    # ----- send actions -----

    def _on_send_pressed(self) -> None:
        if self._compose_var is None:
            return
        text = self._compose_var.get().strip()
        if not text:
            return
        self._send_safe(text)
        self._compose_var.set("")

    def _send_canned(self, text: str) -> None:
        self._send_safe(text)

    def _send_safe(self, text: str) -> None:
        ch_idx = self._selected_channel_index()
        ch_label = self._selected_channel_label()
        try:
            self._send_text(text, channel=ch_idx)
            self._set_notice(f"sent on {ch_label}: {text}")
        except Exception as exc:  # noqa: BLE001
            log.exception("send failed")
            self._set_notice(f"send failed: {exc}")

    def _selected_channel_index(self) -> int:
        """Resolve the dropdown's current selection back to a channel int."""
        if not self._channel_var or not self._channel_options:
            return 0
        label = self._channel_var.get()
        for idx, formatted in self._channel_options:
            if formatted == label:
                return idx
        return 0

    def _selected_channel_label(self) -> str:
        if self._channel_var:
            return self._channel_var.get() or "ch0"
        return "ch0"

    def _refresh_channels(self) -> None:
        """Pull the current channel list and refresh the dropdown."""
        if self._channel_combo is None or self._channel_var is None:
            return
        try:
            channels = self._channels_provider() or [(0, "default")]
        except Exception:  # noqa: BLE001
            log.exception("channels_provider failed")
            channels = [(0, "default")]
        # Format as "default (ch:0)" so the user sees both name and index.
        formatted = [(idx, f"{name} (ch:{idx})") for idx, name in channels]
        if formatted == self._channel_options:
            return
        self._channel_options = formatted
        labels = [f for _idx, f in formatted]
        self._channel_combo["values"] = labels
        # Keep current selection if it still exists, otherwise default to the
        # first channel (typically the public default).
        current = self._channel_var.get()
        if current not in labels and labels:
            self._channel_var.set(labels[0])

    # ----- event drain and refresh -----

    def _drain_events(self) -> None:
        if self._stop_event.is_set() or self.root is None:
            return
        try:
            while True:
                event_type, payload = self._events.get_nowait()
                if event_type == "notice":
                    self._set_notice(str(payload))
                elif event_type in ("packet", "node", "connected", "disconnected"):
                    # We rely on the SQL store for content; just trigger refresh.
                    self._refresh_messages()
                    self._refresh_glance()
                    self._refresh_map()
                    if event_type == "connected":
                        # Channels are populated after connect; refresh dropdown.
                        self._refresh_channels()
                        self._set_notice("interface connected")
                    elif event_type == "disconnected":
                        self._set_notice("interface disconnected")
        except queue.Empty:
            pass
        self.root.after(250, self._drain_events)

    def _periodic_refresh(self) -> None:
        if self._stop_event.is_set() or self.root is None:
            return
        self._refresh_glance()
        self._refresh_channels()
        self._refresh_map()
        self.root.after(15000, self._periodic_refresh)

    def _refresh_messages(self) -> None:
        if self._messages_text is None:
            return
        try:
            rows = self._recent_messages(40)
        except Exception:  # noqa: BLE001
            log.exception("recent_messages provider failed")
            rows = []
        # Resolve channel index -> name once per refresh.
        ch_names = {idx: name for idx, name in (self._channels_provider() or [])}
        my_id = self._my_node_id_provider()

        self._messages_text.configure(state="normal")
        self._messages_text.delete("1.0", "end")
        for row in reversed(rows):
            ts = self._fmt_time(row.get("rx_time_utc"))
            from_id = row.get("from_id") or "?"
            to_id = row.get("to_id")
            ch_idx = row.get("channel")
            text = row.get("text") or ""

            tag = _classify_destination(to_id, ch_idx, ch_names, my_id)
            line = f"[{ts}] {tag} {from_id}: {text}\n"
            self._messages_text.insert("end", line)
        self._messages_text.configure(state="disabled")
        self._messages_text.see("end")

    def _refresh_glance(self) -> None:
        try:
            nodes = self._nodes()
        except Exception:  # noqa: BLE001
            log.exception("nodes provider failed")
            nodes = []
        try:
            messages = self._recent_messages(1)
        except Exception:  # noqa: BLE001
            messages = []
        my_lat, my_lon = (None, None)
        try:
            my_lat, my_lon = self._my_position()
        except Exception:  # noqa: BLE001
            log.debug("my_position failed", exc_info=True)

        self._glance_vars["nodes_heard"].set(str(len(nodes)))
        if messages:
            last = messages[0]
            self._glance_vars["last_msg"].set(
                f"{self._fmt_time(last.get('rx_time_utc'))} {last.get('from_id') or '?'}: "
                f"{(last.get('text') or '')[:40]}"
            )
        else:
            self._glance_vars["last_msg"].set("(none yet)")

        self._glance_vars["farthest_km"].set(
            self._farthest_today_label(nodes, my_lat, my_lon)
        )
        self._glance_vars["local_time"].set(
            datetime.now(self._tz).strftime("%Y-%m-%d %H:%M %Z")
        )

        # Refresh nodes treeview.
        tree = getattr(self, "_nodes_tree", None)
        if tree is not None:
            tree.delete(*tree.get_children())
            for n in nodes[:50]:
                name = n.get("long_name") or n.get("short_name") or n.get("node_id") or "?"
                tree.insert(
                    "",
                    "end",
                    values=(
                        name,
                        self._fmt_time(n.get("last_heard_utc")),
                        n.get("battery_level") if n.get("battery_level") is not None else "",
                        f"{n['snr']:.1f}" if n.get("snr") is not None else "",
                    ),
                )

    def _farthest_today_label(
        self,
        nodes: list[dict[str, Any]],
        my_lat: float | None,
        my_lon: float | None,
    ) -> str:
        if my_lat is None or my_lon is None:
            return "(need our position)"
        today_utc = datetime.now(timezone.utc).date().isoformat()
        best_km = 0.0
        best_name = None
        for n in nodes:
            lat, lon = n.get("latitude"), n.get("longitude")
            heard = (n.get("last_heard_utc") or "")[:10]
            if lat is None or lon is None or heard != today_utc:
                continue
            km = _haversine_km(my_lat, my_lon, float(lat), float(lon))
            if km > best_km:
                best_km = km
                best_name = n.get("long_name") or n.get("short_name") or n.get("node_id")
        if best_name is None:
            return "(no positions today)"
        return f"{best_km:.1f} km ({best_name})"

    # ----- helpers -----

    def _set_notice(self, text: str) -> None:
        if self._notice_label is None:
            return
        self._notice_text = text
        self._notice_label.configure(text=text)

    def _fmt_time(self, iso_utc: str | None) -> str:
        if not iso_utc:
            return "--"
        try:
            dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(self._tz).strftime("%m-%d %H:%M")
        except ValueError:
            return iso_utc

    # ----- map drawing -----

    def _redraw_map(self) -> None:
        """Clear and repaint the map canvas from the latest SQLite + my-pos."""
        canvas = self._map_canvas
        if canvas is None:
            return
        canvas.delete("all")

        w = max(canvas.winfo_width(), 2)
        h = max(canvas.winfo_height(), 2)
        margin = 30

        try:
            nodes = self._nodes() or []
        except Exception:  # noqa: BLE001
            log.exception("nodes provider failed in map")
            nodes = []
        positioned = [
            n for n in nodes
            if n.get("latitude") is not None and n.get("longitude") is not None
        ]
        try:
            my_lat, my_lon = self._my_position()
        except Exception:  # noqa: BLE001
            my_lat = my_lon = None

        status = self._map_status_var

        # Decide the viewing bbox. Fixed-radius mode needs our position;
        # if we don't have one yet, fall back to auto-fit and tell the user.
        scale_miles = self._map_scale_miles
        scale_note = ""
        if scale_miles is not None and (my_lat is None or my_lon is None):
            scale_miles = None
            scale_note = " (need our position for fixed scale)"

        if scale_miles is not None:
            lat_min, lat_max, lon_min, lon_max = _bbox_around(
                float(my_lat), float(my_lon), scale_miles
            )
        else:
            points: list[tuple[float, float]] = [
                (float(n["latitude"]), float(n["longitude"])) for n in positioned
            ]
            if my_lat is not None and my_lon is not None:
                points.append((float(my_lat), float(my_lon)))
            if not points:
                if status is not None:
                    status.set("(no positions yet)")
                canvas.create_text(
                    w // 2, h // 2,
                    text="No node positions yet. Once nodes broadcast positions,\n"
                         "they will appear here.",
                    fill="#8aa", justify="center", font=("DejaVu Sans", 12),
                )
                return
            lat_min, lat_max, lon_min, lon_max = _bbox_with_padding(points)

        # Filter to nodes inside the current viewing bbox.
        in_view = [
            n for n in positioned
            if lat_min <= float(n["latitude"]) <= lat_max
               and lon_min <= float(n["longitude"]) <= lon_max
        ]

        # Equirectangular projection scaled to canvas; longitude scaled by
        # cos(mean lat) so a 5 mi east-west span looks the same as 5 mi
        # north-south at the visible latitude.
        mean_lat_rad = math.radians((lat_min + lat_max) / 2)
        lon_scale = math.cos(mean_lat_rad) or 1.0
        lat_range = max(lat_max - lat_min, 1e-6)
        lon_range = max((lon_max - lon_min) * lon_scale, 1e-6)
        avail_w = w - 2 * margin
        avail_h = h - 2 * margin
        scale = min(avail_w / lon_range, avail_h / lat_range)
        proj_w = lon_range * scale
        proj_h = lat_range * scale
        x_off = (w - proj_w) / 2
        y_off = (h - proj_h) / 2

        def project(lat: float, lon: float) -> tuple[float, float]:
            x = x_off + (lon - lon_min) * lon_scale * scale
            y = y_off + (lat_max - lat) * scale  # invert: canvas y grows down
            return x, y

        # Spokes from our node first, so dots draw on top.
        if my_lat is not None and my_lon is not None:
            mx, my = project(float(my_lat), float(my_lon))
            for n in in_view:
                nx, ny = project(float(n["latitude"]), float(n["longitude"]))
                canvas.create_line(mx, my, nx, ny, fill="#22344a", width=1)

        # Neighbor dots.
        for n in in_view:
            lat = float(n["latitude"])
            lon = float(n["longitude"])
            x, y = project(lat, lon)
            color = _color_for_snr(n.get("snr"))
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=color, outline="")
            label = (
                n.get("short_name") or
                (n.get("long_name") or "")[:8] or
                (n.get("node_id") or "")[-4:]
            )
            canvas.create_text(
                x, y + 10, text=label, fill="#cdd6f4",
                font=("DejaVu Sans", 9), anchor="n",
            )

        # Our position last, larger, distinct color.
        if my_lat is not None and my_lon is not None:
            mx, my = project(float(my_lat), float(my_lon))
            canvas.create_oval(mx - 8, my - 8, mx + 8, my + 8,
                               fill="#ff5c5c", outline="#ffffff", width=2)
            canvas.create_text(mx, my - 14, text="us", fill="#ffffff",
                               font=("DejaVu Sans", 10, "bold"), anchor="s")

        # Small scale bar in the lower-left so the user can eyeball distance.
        self._draw_scale_bar(canvas, w, h, scale, lon_scale)

        if status is not None:
            mode = "Full extent" if scale_miles is None else f"{self._scale_label(scale_miles)} view"
            status.set(
                f"{mode}{scale_note}: {len(in_view)} of {len(positioned)} positioned plotted"
            )

    def _draw_scale_bar(
        self,
        canvas: tk.Canvas,
        w: int,
        h: int,
        scale: float,
        lon_scale: float,
    ) -> None:
        """Draw a small scale bar in the lower-left corner."""
        # Pick a bar length in miles that fits roughly in 120 pixels.
        # 1 mile in projected x-pixels = (1 / 69.0) * lon_scale * scale
        px_per_mile = (1.0 / _MI_PER_DEG_LAT) * lon_scale * scale
        if px_per_mile <= 0:
            return
        target_px = 120
        # Choose a "nice" mile length: 1, 2, 5, 10, 25, 50, 100, ...
        nice = [0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000]
        miles = nice[0]
        for m in nice:
            if m * px_per_mile <= target_px:
                miles = m
        bar_px = miles * px_per_mile
        x0 = 14
        y0 = h - 18
        canvas.create_line(x0, y0, x0 + bar_px, y0, fill="#cdd6f4", width=3)
        canvas.create_line(x0, y0 - 4, x0, y0 + 4, fill="#cdd6f4", width=2)
        canvas.create_line(x0 + bar_px, y0 - 4, x0 + bar_px, y0 + 4,
                           fill="#cdd6f4", width=2)
        label = f"{miles:g} mi" if miles >= 1 else f"{miles:g} mi"
        canvas.create_text(x0 + bar_px / 2, y0 - 10, text=label,
                           fill="#cdd6f4", font=("DejaVu Sans", 9))

    def _refresh_map(self) -> None:
        """Schedule a redraw, used when events arrive."""
        if self._map_canvas is not None:
            self._redraw_map()
