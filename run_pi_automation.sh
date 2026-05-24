#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
REQUIREMENTS_FILE="$PROJECT_DIR/requirements.txt"

LOG_PREFIX="[cafebot-setup]"

log() {
    echo "$LOG_PREFIX $*"
}

run_or_sudo() {
    if [[ $EUID -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

ensure_system_packages() {
    local pkgs=(python3-venv python3-pip python3-picamera2)

    if apt-cache show rpicam-apps >/dev/null 2>&1; then
        pkgs+=(rpicam-apps)
    else
        pkgs+=(libcamera-apps)
    fi

    log "Installing system dependencies..."
    run_or_sudo apt-get update -y
    run_or_sudo apt-get install -y "${pkgs[@]}"
}

ensure_dialout_access() {
    if id -nG "$USER" | tr ' ' '\n' | grep -qx "dialout"; then
        log "User $USER already in dialout group."
        return
    fi

    log "Adding $USER to dialout group for Arduino USB serial access."
    run_or_sudo usermod -aG dialout "$USER"
    echo
    echo "$LOG_PREFIX Reboot required to apply dialout group membership."
    echo "$LOG_PREFIX Run: sudo reboot"
    exit 0
}

ensure_virtualenv_and_deps() {
    if [[ ! -d "$VENV_DIR" ]]; then
        log "Creating virtual environment with system-site-packages..."
        python3 -m venv "$VENV_DIR" --system-site-packages
    fi

    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"

    log "Installing Python dependencies..."
    python -m pip install --upgrade pip
    pip install -r "$REQUIREMENTS_FILE"
}

configure_project_for_pi() {
    log "Configuring camera/Arduino settings in config.json..."

    python - <<'PY'
import glob
import json
import os

cfg_path = os.path.join(os.getcwd(), "config.json")
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

cfg["simulation_mode"] = False
cfg["camera_backend"] = "picamera2"
cfg["camera_allow_fallback"] = False

ports = (
    sorted(glob.glob("/dev/serial/by-id/*"))
    + sorted(glob.glob("/dev/ttyACM*"))
    + sorted(glob.glob("/dev/ttyUSB*"))
)
cfg["arduino_port"] = ports[0] if ports else "auto"
cfg["arduino_baud"] = int(cfg.get("arduino_baud", 9600) or 9600)

with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)

print("camera_backend =", cfg["camera_backend"])
print("camera_allow_fallback =", cfg["camera_allow_fallback"])
print("simulation_mode =", cfg["simulation_mode"])
print("arduino_port =", cfg["arduino_port"])
print("arduino_baud =", cfg["arduino_baud"])
PY
}

quick_camera_check() {
    log "Running quick camera check..."

    if command -v rpicam-hello >/dev/null 2>&1; then
        rpicam-hello -n -t 800 >/dev/null 2>&1 || true
    elif command -v libcamera-hello >/dev/null 2>&1; then
        libcamera-hello -n -t 800 >/dev/null 2>&1 || true
    fi
}

start_app() {
    log "Starting CafeBot backend..."

    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
    exec python app.py
}

main() {
    cd "$PROJECT_DIR"

    ensure_system_packages
    ensure_dialout_access
    ensure_virtualenv_and_deps
    configure_project_for_pi
    quick_camera_check
    start_app
}

main "$@"
