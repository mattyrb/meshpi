#!/usr/bin/env bash
#
# meshpi Pi-side setup helper.
#
# Idempotent: re-run anytime to bring the Pi back into the desired state.
# Run from the meshpi repo root on the Pi:
#
#     bash scripts/pi_setup.sh
#
# What this does (each step prompts before acting):
#   1. Disable OS screen blanking via raspi-config (so the touchscreen stays on).
#   2. Install and enable the day/night backlight systemd timers.
#   3. Install matchbox-keyboard so the in-app "Kbd" button has something to launch.
#   4. Install emoji-capable fonts so messages render glyphs instead of tofu boxes.
#   5. Install a udev rule so the meshpi user can write to the backlight
#      brightness file (needed for idle dimming and new-message wake).
#
# Pass --yes to skip all prompts and accept defaults.

set -euo pipefail

YES=0
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then
    YES=1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SYSTEMD_SRC="${REPO_ROOT}/systemd"

confirm() {
    # Returns 0 if user accepts.
    if [[ "$YES" -eq 1 ]]; then
        return 0
    fi
    local prompt="$1"
    local reply
    read -r -p "$prompt [Y/n] " reply
    [[ -z "$reply" || "$reply" =~ ^[Yy]$ ]]
}

require_root_for() {
    # Re-exec with sudo when needed for the steps that touch system state.
    if [[ "$EUID" -ne 0 ]]; then
        echo "Re-running with sudo for: $1"
        exec sudo --preserve-env=YES bash "$0" "$@"
    fi
}

step_disable_screen_blanking() {
    echo
    echo "Step 1/3: disable OS screen blanking"
    echo "  Without this, the Wayland/X compositor blanks the screen after ~10 min idle."
    if confirm "Disable screen blanking now?"; then
        if command -v raspi-config >/dev/null 2>&1; then
            # 'nonint' lets us drive raspi-config without the menu UI.
            # do_blanking 1 = disable blanking.
            sudo raspi-config nonint do_blanking 1
            echo "  raspi-config: screen blanking disabled. Takes effect on next reboot."
        else
            echo "  raspi-config not found. Skipping. Edit ~/.config/wayfire.ini manually if needed."
        fi
    else
        echo "  skipped."
    fi
}

step_install_backlight_timers() {
    echo
    echo "Step 2/3: install backlight day/night timers"
    if [[ ! -f "${SYSTEMD_SRC}/meshpi-backlight-day.timer" ]]; then
        echo "  systemd units not found in ${SYSTEMD_SRC}; skipping."
        return
    fi
    if [[ ! -e /sys/class/backlight/rpi_backlight/brightness ]]; then
        echo "  WARNING: /sys/class/backlight/rpi_backlight/brightness does not exist."
        echo "  Your display may use a different sysfs path. Check: ls /sys/class/backlight/"
        echo "  Edit the ExecStart lines in systemd/meshpi-backlight-*.service before continuing."
        if ! confirm "Continue anyway?"; then
            echo "  skipped."
            return
        fi
    fi
    if confirm "Install and enable backlight day/night timers?"; then
        sudo cp "${SYSTEMD_SRC}/meshpi-backlight-day.service"   /etc/systemd/system/
        sudo cp "${SYSTEMD_SRC}/meshpi-backlight-night.service" /etc/systemd/system/
        sudo cp "${SYSTEMD_SRC}/meshpi-backlight-day.timer"     /etc/systemd/system/
        sudo cp "${SYSTEMD_SRC}/meshpi-backlight-night.timer"   /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl enable --now \
            meshpi-backlight-day.timer \
            meshpi-backlight-night.timer
        echo "  installed and enabled. Current schedule:"
        systemctl list-timers 'meshpi-backlight-*' --no-pager || true
    else
        echo "  skipped."
    fi
}

step_install_keyboard() {
    echo
    echo "Step 3/4: install matchbox-keyboard (for the in-app Kbd button)"
    if command -v matchbox-keyboard >/dev/null 2>&1; then
        echo "  already installed."
        return
    fi
    if confirm "Install matchbox-keyboard?"; then
        sudo apt-get update
        sudo apt-get install -y matchbox-keyboard
        echo "  installed."
    else
        echo "  skipped."
    fi
}

step_install_emoji_fonts() {
    echo
    echo "Step 4/5: install emoji-capable fonts (so messages render glyphs, not boxes)"
    # fonts-noto-color-emoji: full color emoji (rendered monochrome by Tk).
    # fonts-symbola: monochrome but very broad Unicode coverage; reliable fallback.
    # fonts-noto-core: Latin/symbol coverage the messages widget can fall back to.
    local pkgs=(fonts-noto-color-emoji fonts-symbola fonts-noto-core)
    local missing=()
    for p in "${pkgs[@]}"; do
        if ! dpkg -s "$p" >/dev/null 2>&1; then
            missing+=("$p")
        fi
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        echo "  already installed: ${pkgs[*]}"
        return
    fi
    echo "  will install: ${missing[*]}"
    if confirm "Install emoji fonts now?"; then
        sudo apt-get update
        sudo apt-get install -y "${missing[@]}"
        # Refresh fontconfig so newly installed fonts are picked up.
        if command -v fc-cache >/dev/null 2>&1; then
            sudo fc-cache -f
        fi
        echo "  installed. Restart meshpi to pick up the new fonts:"
        echo "    sudo systemctl restart meshpi"
    else
        echo "  skipped."
    fi
}

step_install_backlight_udev() {
    echo
    echo "Step 5/5: install udev rule so meshpi can dim/wake the backlight"
    local rule_src="${REPO_ROOT}/systemd/99-meshpi-backlight.rules"
    local rule_dst="/etc/udev/rules.d/99-meshpi-backlight.rules"
    if [[ ! -f "$rule_src" ]]; then
        echo "  rule file not found at $rule_src; skipping."
        return
    fi
    if confirm "Install backlight udev rule?"; then
        sudo cp "$rule_src" "$rule_dst"
        sudo udevadm control --reload
        sudo udevadm trigger --subsystem-match=backlight || true
        echo "  installed at $rule_dst. Reboot if /sys/class/backlight/*/brightness is not mode 0666."
    else
        echo "  skipped."
    fi
}

echo "meshpi Pi setup"
echo "Repo: ${REPO_ROOT}"

step_disable_screen_blanking
step_install_backlight_timers
step_install_keyboard
step_install_emoji_fonts
step_install_backlight_udev

echo
echo "Done. If you changed screen-blanking settings, reboot to apply: sudo reboot"
