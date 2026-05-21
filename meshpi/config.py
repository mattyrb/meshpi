"""Configuration loader for meshpi.

Reads a TOML file with tomllib (stdlib in Python 3.11+; deployment target
is Python 3.13 on the Pi) and returns typed dataclasses for each section.
Designed so the same code runs on Windows during development (COM port
serial) and on the Pi (by-id serial path).
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SerialConfig:
    device: str
    baud: int = 115200


@dataclass
class DatabaseConfig:
    path: str
    batch_size: int = 50
    batch_seconds: float = 5.0


@dataclass
class WatchdogConfig:
    silence_seconds: int = 900
    reconnect_grace_seconds: int = 60
    heartbeat_seconds: int = 60


@dataclass
class GuiConfig:
    fullscreen: bool = True
    display_timezone: str = "UTC"
    canned_messages: list[str] = field(default_factory=list)


@dataclass
class BacklightConfig:
    path: str = ""
    max_brightness_path: str = ""
    day_brightness: int = 200
    night_brightness: int = 0
    day_start: str = "06:30"
    night_start: str = "22:00"


@dataclass
class AutomationsConfig:
    enabled: list[str] = field(default_factory=list)
    # Per-automation parameter blocks, keyed by module name.
    params: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class PostgisConfig:
    enabled: bool = False
    dsn: str = ""
    table: str = "meshpi.packets"
    batch_size: int = 5000


@dataclass
class Config:
    serial: SerialConfig
    database: DatabaseConfig
    watchdog: WatchdogConfig
    gui: GuiConfig
    backlight: BacklightConfig
    automations: AutomationsConfig
    postgis: PostgisConfig
    source_path: Path


def _default_config_path() -> Path:
    """Pick a config path. Env var wins, then ./config.toml next to the package."""
    env = os.environ.get("MESHPI_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve().parent.parent
    return here / "config.toml"


def is_windows_com_port(device: str) -> bool:
    """Return True for strings like 'COM4'. Used to skip POSIX-only checks on dev."""
    return sys.platform.startswith("win") and device.upper().startswith("COM")


def load(path: Path | str | None = None) -> Config:
    """Load and validate the TOML config. Raises FileNotFoundError if missing."""
    cfg_path = Path(path).expanduser().resolve() if path else _default_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Config not found at {cfg_path}. "
            "Copy config.example.toml to config.toml and edit it."
        )

    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)

    serial_raw = raw.get("serial", {})
    db_raw = raw.get("database", {})
    wd_raw = raw.get("watchdog", {})
    gui_raw = raw.get("gui", {})
    bl_raw = raw.get("backlight", {})
    auto_raw = raw.get("automations", {})
    pg_raw = raw.get("postgis", {})

    # Pull per-automation tables out of [automations.<name>] subtables.
    auto_params: dict[str, dict[str, Any]] = {}
    for key, value in auto_raw.items():
        if key == "enabled":
            continue
        if isinstance(value, dict):
            auto_params[key] = value

    cfg = Config(
        serial=SerialConfig(
            device=serial_raw["device"],
            baud=int(serial_raw.get("baud", 115200)),
        ),
        database=DatabaseConfig(
            path=db_raw["path"],
            batch_size=int(db_raw.get("batch_size", 50)),
            batch_seconds=float(db_raw.get("batch_seconds", 5.0)),
        ),
        watchdog=WatchdogConfig(
            silence_seconds=int(wd_raw.get("silence_seconds", 900)),
            reconnect_grace_seconds=int(wd_raw.get("reconnect_grace_seconds", 60)),
            heartbeat_seconds=int(wd_raw.get("heartbeat_seconds", 60)),
        ),
        gui=GuiConfig(
            fullscreen=bool(gui_raw.get("fullscreen", True)),
            display_timezone=str(gui_raw.get("display_timezone", "UTC")),
            canned_messages=list(gui_raw.get("canned_messages", [])),
        ),
        backlight=BacklightConfig(
            path=str(bl_raw.get("path", "")),
            max_brightness_path=str(bl_raw.get("max_brightness_path", "")),
            day_brightness=int(bl_raw.get("day_brightness", 200)),
            night_brightness=int(bl_raw.get("night_brightness", 0)),
            day_start=str(bl_raw.get("day_start", "06:30")),
            night_start=str(bl_raw.get("night_start", "22:00")),
        ),
        automations=AutomationsConfig(
            enabled=list(auto_raw.get("enabled", [])),
            params=auto_params,
        ),
        postgis=PostgisConfig(
            enabled=bool(pg_raw.get("enabled", False)),
            dsn=str(pg_raw.get("dsn", "")),
            table=str(pg_raw.get("table", "meshpi.packets")),
            batch_size=int(pg_raw.get("batch_size", 5000)),
        ),
        source_path=cfg_path,
    )

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    """Sanity checks that catch typical config mistakes early."""
    if not cfg.serial.device:
        raise ValueError("serial.device is required")
    if not cfg.database.path:
        raise ValueError("database.path is required")
    if cfg.database.batch_size < 1:
        raise ValueError("database.batch_size must be >= 1")
    if cfg.database.batch_seconds <= 0:
        raise ValueError("database.batch_seconds must be > 0")
    if cfg.watchdog.silence_seconds < 10:
        raise ValueError("watchdog.silence_seconds is unreasonably low")
