# meshpi

A single Python service that turns a Raspberry Pi 3 with a built-in touchscreen into a dedicated, always-on Meshtastic node and messaging terminal. One process owns the USB serial connection, logs every mesh packet to SQLite, drives a Tkinter touch GUI, and runs pluggable automations. A watchdog and systemd unit keep it alive across stalls, crashes, and reboots.

This README targets a Raspberry Pi 3 running Raspberry Pi OS Bookworm 64-bit with Python 3.13. Development happens on a MacBook but every deploy command below is for the Pi. The code is compatible with Python 3.11+, but 3.13 is the deployment target and the version the pinned dependency ranges are validated against.

## What it does

- Owns the serial port to a Meshtastic node (Silicon Labs CP210x, `VID:PID 10C4:EA60`) using a stable `/dev/serial/by-id/...` path so the device name does not shift across reboots.
- Subscribes to the Meshtastic pubsub events and routes them to a logger, the GUI, and the automation dispatcher inside a single process. No MQTT, no bridging.
- Logs every packet to SQLite in WAL mode with batched commits, stores the raw JSON alongside parsed columns, and dedups rebroadcasts on `(packet_id, from_id)`.
- Shows a touch-friendly Tkinter UI with a glance screen, a messages view, and large canned-message buttons.
- Runs automations (auto-reply on keywords, quiet-node alert) loaded from config.
- Includes an application-level watchdog that reconnects on silence and exits non-zero on hard failure so systemd restarts the service cleanly.
- Optionally syncs SQLite rows to PostGIS through a separate script you run from a timer.

## Repo layout

```
meshpi/
  meshpi/
    __init__.py
    config.py            # tomllib-based loader
    interface.py         # single connection owner + pubsub fan-out + send
    watchdog.py          # silence detection, reconnect, fail-exit
    logger.py            # SQLite WAL, batched, raw + parsed, dedup
    gui.py               # Tkinter touchscreen UI
    automations/
      __init__.py
      base.py
      autoreply.py
      quiet_alert.py
    app.py               # wires interface, logger, gui, automations
  scripts/
    postgis_sync.py
  systemd/
    meshpi.service
    meshpi-backlight-day.{service,timer}
    meshpi-backlight-night.{service,timer}
    meshpi-postgis-sync.{service,timer}
  config.example.toml
  requirements.txt             # core deps with loose ranges
  requirements-postgis.txt     # optional psycopg for the sync script
  requirements.lock.txt        # commit after a clean install for reproducibility
  .gitignore
  README.md
```

## Deploying to the Pi

### 1. Clone

```bash
git clone https://github.com/<you>/meshpi.git
cd meshpi
```

### 2. Virtual environment and dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

**Confirm a clean install.** Successful pip runs end with a `Successfully installed ...` line that lists every package. If dependency resolution fails, pip installs nothing, even packages it already "Collected," and the failure surfaces later as `ModuleNotFoundError` (for example `No module named 'pubsub'`). Always read for the success line before running the app.

If a lock file is committed in the repo, prefer it for reproducible installs on the same Python version:

```bash
pip install -r requirements.lock.txt
```

After a clean install on a fresh Pi, regenerate and commit the lock:

```bash
pip freeze > requirements.lock.txt
git add requirements.lock.txt
git commit -m "Refresh lock for Python 3.13"
```

`requirements.txt` keeps loose version ranges so pip can pick wheels that match the running Python; the lock file pins the exact set that resolved cleanly.

### 3. Find the node by-id path

```bash
ls -l /dev/serial/by-id/
```

You should see something like:

```
usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
```

The product portion can read `CP2102` or `CP2102N` depending on the adapter revision; copy whatever your `ls` shows. The `cp210x` kernel driver is in mainline so no driver setup is needed.

If you have not yet, add your user to the `dialout` group so the service can open the port without root:

```bash
sudo usermod -aG dialout $USER
```

Log out and back in for the group change to take effect.

### 4. Format and mount a USB data drive (ext4)

The SQLite database lives on a USB stick, not the SD card, to spare the card from write wear. The filesystem is ext4. ext4 cannot be created from Windows or macOS, so format the drive on the Pi:

```bash
lsblk -f                                   # identify the USB drive (e.g. /dev/sda1); the SD card is mmcblk0
sudo umount /dev/sda1                      # if auto-mounted
sudo mkfs.ext4 -L meshpi-data /dev/sda1    # erases the drive
sudo blkid /dev/sda1                       # note the UUID
sudo mkdir -p /mnt/meshpi-data
```

Add a UUID-based `/etc/fstab` entry so the drive auto-mounts at boot:

```
UUID=<your-uuid>  /mnt/meshpi-data  ext4  defaults,noatime,nofail  0  2
```

`noatime` cuts needless writes. `nofail` lets the Pi boot if the drive is absent.

Mount and hand ownership to the user that runs the service so the logger can write:

```bash
sudo mount -a
sudo chown -R pi:pi /mnt/meshpi-data       # match your service User=, default is pi
ls -ld /mnt/meshpi-data
```

### 5. Configure

```bash
cp config.example.toml config.toml
nano config.toml
```

At a minimum set:

- `serial.device` to the by-id path you found in step 3.
- `database.path` to the USB drive, default `/mnt/meshpi-data/meshpi.db`.
- `gui.canned_messages` to taste.
- `backlight.path` to whatever `ls /sys/class/backlight/` shows on your display. Common candidates are `/sys/class/backlight/rpi_backlight/brightness` and `/sys/class/backlight/10-0045/brightness`. Some HDMI+USB touch panels expose no sysfs backlight at all, in which case skip the backlight timers.

### 6. Foreground test run

```bash
python -m meshpi.app
```

Watch the console for `Meshtastic connection established`, then send a test message from another node. Hit Ctrl-C to stop.

### 7. Install the systemd service

```bash
sudo cp systemd/meshpi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshpi
journalctl -u meshpi -f
```

If your user is not `pi` or the repo lives somewhere other than `/home/pi/meshpi`, edit `User`, `Group`, `WorkingDirectory`, `Environment=MESHPI_CONFIG`, and `ExecStart` in `meshpi.service` before installing it.

The unit uses `WantedBy=graphical.target` and a `DISPLAY=:0` environment so the Tk GUI can draw on the autologin desktop session. If you run a headless setup or a different display server, adjust accordingly.

### 8. Backlight schedule (optional)

```bash
sudo cp systemd/meshpi-backlight-*.service /etc/systemd/system/
sudo cp systemd/meshpi-backlight-*.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshpi-backlight-day.timer meshpi-backlight-night.timer
```

Edit the `ExecStart` paths and values in the two `.service` files to match your sysfs path and brightness levels.

### 9. PostGIS sync (optional)

PostGIS sync is off by default. The core appliance does not need `psycopg`, so it is not in `requirements.txt`. To enable the sync, install the optional dependency on top of the base set:

```bash
source .venv/bin/activate
pip install -r requirements-postgis.txt
```

Then set `[postgis] enabled = true` in `config.toml` and provide a libpq `dsn`. Create the destination table once with the DDL inlined at the top of `scripts/postgis_sync.py`. Run on demand:

```bash
.venv/bin/python scripts/postgis_sync.py
```

or install the timer:

```bash
sudo cp systemd/meshpi-postgis-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshpi-postgis-sync.timer
```

The sync writes lat/lon as `geometry(Point, 4326)`. Any reprojection to a working CRS (for example EPSG:5070) is left to downstream analysis.

## Iteration loop: ship a change

Edit on the Mac, commit, push. On the Pi:

```bash
cd ~/meshpi
git pull
sudo systemctl restart meshpi
journalctl -u meshpi -f
```

If you changed `requirements.txt` or `requirements.lock.txt`, also run (in the activated venv):

```bash
source .venv/bin/activate
pip install -r requirements.lock.txt   # or requirements.txt
```

If you bumped a dependency range and pip resolved a new set, regenerate the lock and commit it from the Pi:

```bash
pip freeze > requirements.lock.txt
git add requirements.lock.txt && git commit -m "Refresh lock" && git push
```

## Configuration reference

See `config.example.toml` for the full set of options with inline comments. Highlights:

- `serial.device` accepts a Linux by-id path or a Windows COM name like `COM4`, so the same code runs on a development laptop and on the Pi.
- `database.batch_size` and `database.batch_seconds` control flush cadence. Defaults of 50 packets or 5 seconds keep wear low without losing more than a few seconds of writes on a hard cut.
- `watchdog.silence_seconds` defaults to 900 (15 minutes). If your mesh is quiet, raise it. If it is busy, you can lower it.
- `automations.enabled` is a list of module names under `meshpi.automations`. Add your own by dropping a new module that subclasses `Automation` from `automations/base.py`.

## Operational notes

- **SD card protection.** Keep the SQLite database on a USB stick (`database.path`). Use a quality power supply to avoid brownout corruption; the Pi 3 is happy with a steady 5V/2.5A. For long unattended deployments, consider a read-only root filesystem with a separate writable data partition.
- **Remote access.** Keep SSH enabled. Tailscale is a low-friction option for headless management from anywhere.
- **Logs.** Everything goes through Python's `logging` to stdout, so `journalctl -u meshpi -f` is the live view. Set `MESHPI_LOG_LEVEL=DEBUG` in the unit's `Environment=` lines to crank verbosity.
- **Threading.** Meshtastic callbacks fire on background threads. The GUI drains a thread-safe queue via Tk's `after()`. All sends route through `InterfaceManager.send_text`, which is the only code path that touches the serial interface.

## Architecture (the short version)

One process. The interface owner is the only thing that touches the serial port. Pubsub events fan out to the SQLite logger, the GUI queue, and the automation dispatcher. The watchdog watches packet age and interface heartbeat; on silence past threshold it reconnects, and on failed reconnect it exits non-zero so systemd restarts the whole service. This is Option A from the design notes: simple, single-owner, no MQTT.

## Out of scope

- MQTT and any other network bridging.
- Map view in the GUI. A Kivy port could carry that; the GUI is kept behind a thin facade so the toolkit could change without rewriting `app.py`.
- Multi-node serial fan-in. This is a single-node appliance.
