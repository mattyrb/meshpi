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


def _haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in km."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(a))


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
        fullscreen: bool = True,
        display_timezone: str = "UTC",
    ):
        self._send_text = send_text
        self._canned = list(canned_messages)
        self._recent_messages = recent_messages_provider
        self._nodes = nodes_provider
        self._my_position = my_position_provider
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

    # ----- thread-safe inputs -----

    def push_event(self, event_type: str, payload: Any) -> None:
        self._events.put((event_type, payload))

    def show_notice(self, source: str, message: str) -> None:
        self._events.put(("notice", f"[{source}] {message}"))

    def stop(self) -> None:
        self._stop_event.set()
        # Schedule the destroy on the Tk thread.
        root = self.root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:  # noqa: BLE001
                pass

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

        # Recent messages text box.
        top = ttk.Frame(frame)
        top.pack(fill="both", expand=True)
        self._messages_text = tk.Text(
            top, wrap="word", height=14, font=("DejaVu Sans Mono", 11)
        )
        self._messages_text.configure(state="disabled")
        scroll = ttk.Scrollbar(top, command=self._messages_text.yview)
        self._messages_text.configure(yscrollcommand=scroll.set)
        self._messages_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # Compose row.
        compose = ttk.Frame(frame)
        compose.pack(fill="x", pady=(8, 6))
        self._compose_var = tk.StringVar()
        entry = ttk.Entry(compose, textvariable=self._compose_var, font=("DejaVu Sans", 16))
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(
            compose, text="Send", style="Big.TButton", command=self._on_send_pressed
        ).pack(side="right")

        # Canned message grid; chunk into rows of 3 for touch targets.
        canned = ttk.Frame(frame)
        canned.pack(fill="x", pady=(4, 0))
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

        return frame

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
        try:
            self._send_text(text)
            self._set_notice(f"sent: {text}")
        except Exception as exc:  # noqa: BLE001
            log.exception("send failed")
            self._set_notice(f"send failed: {exc}")

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
                    if event_type == "disconnected":
                        self._set_notice("interface disconnected")
                    elif event_type == "connected":
                        self._set_notice("interface connected")
        except queue.Empty:
            pass
        self.root.after(250, self._drain_events)

    def _periodic_refresh(self) -> None:
        if self._stop_event.is_set() or self.root is None:
            return
        self._refresh_glance()
        self.root.after(15000, self._periodic_refresh)

    def _refresh_messages(self) -> None:
        if self._messages_text is None:
            return
        try:
            rows = self._recent_messages(40)
        except Exception:  # noqa: BLE001
            log.exception("recent_messages provider failed")
            rows = []
        self._messages_text.configure(state="normal")
        self._messages_text.delete("1.0", "end")
        for row in reversed(rows):
            ts = self._fmt_time(row.get("rx_time_utc"))
            from_id = row.get("from_id") or "?"
            text = row.get("text") or ""
            line = f"[{ts}] {from_id}: {text}\n"
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
