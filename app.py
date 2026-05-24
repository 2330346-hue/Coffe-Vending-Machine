# =============================================================================
# CaféBot — Coffee Vending Machine Backend
# Flask + SQLite + OpenCV + Face Recognition + Arduino Serial
# Production-ready for Raspberry Pi 4 + Arduino Uno R3
# =============================================================================

import os
import json
import sqlite3
import base64
import logging
import threading
import time
import io
import hashlib
import glob
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, jsonify, Response, session
from flask_socketio import SocketIO, emit
import cv2
import numpy as np

# ── Optional imports with graceful fallback ───────────────────────────────────

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

try:
    from picamera2 import Picamera2
    PICAMERA2_AVAILABLE = True
except ImportError:
    PICAMERA2_AVAILABLE = False

try:
    import face_recognition
    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False

try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False

# =============================================================================
# PATHS & DIRECTORIES
# =============================================================================

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(BASE_DIR, "database", "cafebot.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH    = os.path.join(BASE_DIR, "cafebot.log")

for _d in ["database", "face_data",
           os.path.join("static", "css"),
           os.path.join("static", "js"),
           "templates", "arduino"]:
    os.makedirs(os.path.join(BASE_DIR, _d), exist_ok=True)

# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_CONFIG = {
    "machine_name": "CaféBot",
    "currency_symbol": "₹",
    "ms_per_level": 600,
    "arduino_port": "auto",
    "arduino_baud": 9600,
    "prices": {
        "black": 15, "espresso": 20, "cappuccino": 25, "latte": 25,
        "custom_base": 10, "per_coffee_level": 2,
        "per_milk_level": 1.5, "per_sugar_level": 0.5, "per_50ml_water": 1
    },
    "presets": {
        "black":      {"coffee": 3, "milk": 0, "sugar": 0, "water": 120},
        "espresso":   {"coffee": 5, "milk": 0, "sugar": 0, "water": 80},
        "cappuccino": {"coffee": 3, "milk": 3, "sugar": 2, "water": 100},
        "latte":      {"coffee": 2, "milk": 4, "sugar": 1, "water": 150}
    },
    "water": {"min_ml": 80, "max_ml": 250, "step": 10},
    "face_recognition_threshold": 0.55,
    "reset_delay_sec": 5,
    "idle_timeout_sec": 60,
    "upi_id": "coffebot@paytm",
    "upi_name": "CaféBot",
    "admin_password_hash": hashlib.sha256("admin1234".encode()).hexdigest(),
    "heater_preheat": True,
    "simulation_mode": False
}

if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH) as _f:
        config = json.load(_f)
    for _k, _v in DEFAULT_CONFIG.items():
        if _k not in config:
            config[_k] = _v
else:
    config = DEFAULT_CONFIG.copy()
    with open(CONFIG_PATH, "w") as _f:
        json.dump(config, _f, indent=2)

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# =============================================================================
# FLASK + SOCKETIO SETUP
# =============================================================================

_secret_file = os.path.join(BASE_DIR, ".secret_key")
if os.path.exists(_secret_file):
    with open(_secret_file) as _f:
        _secret_key = _f.read().strip()
else:
    _secret_key = os.urandom(32).hex()
    with open(_secret_file, "w") as _f:
        _f.write(_secret_key)

app = Flask(__name__)
app.config["SECRET_KEY"] = _secret_key
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    logger=False, engineio_logger=False)

# =============================================================================
# DATABASE
# =============================================================================

def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            face_encoding TEXT,
            photo_base64  TEXT,
            image_hash    TEXT,
            visit_count   INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS preferences (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL UNIQUE,
            coffee_type     TEXT    DEFAULT 'custom',
            coffee_strength INTEGER DEFAULT 3,
            milk_level      INTEGER DEFAULT 2,
            sugar_level     INTEGER DEFAULT 1,
            water_ml        INTEGER DEFAULT 150,
            updated_at      TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER,
            user_name       TEXT    NOT NULL,
            coffee_type     TEXT    NOT NULL,
            coffee_strength INTEGER NOT NULL,
            milk_level      INTEGER NOT NULL,
            sugar_level     INTEGER NOT NULL,
            water_ml        INTEGER NOT NULL,
            amount          REAL    NOT NULL,
            payment_method  TEXT    NOT NULL,
            payment_status  TEXT    DEFAULT 'pending',
            status          TEXT    DEFAULT 'pending',
            created_at      TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS machine_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL, message TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_face_samples (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            face_encoding TEXT,
            image_hash    TEXT,
            angle_label   TEXT DEFAULT 'front',
            quality_score REAL DEFAULT 0,
            source        TEXT DEFAULT 'capture',
            created_at    TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)
    # Lightweight migration for existing databases.
    cols = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
    if "photo_base64" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN photo_base64 TEXT")
    if "image_hash" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN image_hash TEXT")
    sample_cols = {r["name"] for r in db.execute("PRAGMA table_info(user_face_samples)").fetchall()}
    if "angle_label" not in sample_cols:
        db.execute("ALTER TABLE user_face_samples ADD COLUMN angle_label TEXT DEFAULT 'front'")
    if "quality_score" not in sample_cols:
        db.execute("ALTER TABLE user_face_samples ADD COLUMN quality_score REAL DEFAULT 0")
    db.commit()
    db.close()
    logger.info("Database initialized")

# =============================================================================
# CAMERA MANAGER  (picamera2 for Bookworm Pi Camera, OpenCV fallback for USB)
# =============================================================================

class CameraManager:
    def __init__(self):
        self.cap    = None
        self.picam  = None
        self._frame   = None
        self._lock    = threading.Lock()
        self._running = False
        self.available = False

    def frame_usable(self, frame):
        """Reject empty/black frames that some webcam drivers return."""
        if frame is None or not isinstance(frame, np.ndarray):
            return False
        if frame.size == 0:
            return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_val = float(np.mean(gray))
        std_val = float(np.std(gray))
        return mean_val > 8.0 and std_val > 4.0

    def start(self):
        if PICAMERA2_AVAILABLE:
            try:
                self.picam = Picamera2()
                cfg = self.picam.create_preview_configuration(
                    main={"size": (640, 480), "format": "BGR888"})
                self.picam.configure(cfg)
                self.picam.start()
                time.sleep(0.5)
                self._running = True
                threading.Thread(target=self._loop_picam, daemon=True).start()
                self.available = True
                logger.info("Camera started via picamera2 (640x480 BGR)")
                return True
            except Exception as e:
                logger.warning(f"picamera2 failed ({e}), trying OpenCV...")
                self.picam = None
        try:
            self.cap = None
            preferred_backends = [None]
            if os.name == "nt":
                preferred_backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, None]

            for backend in preferred_backends:
                try:
                    cap = cv2.VideoCapture(0) if backend is None else cv2.VideoCapture(0, backend)
                except Exception:
                    cap = None
                if not cap or not cap.isOpened():
                    continue

                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

                # Warm up sensor and reject startup black frames.
                first_good = None
                for _ in range(20):
                    ok, frm = cap.read()
                    if ok and self.frame_usable(frm):
                        first_good = frm
                        break
                    time.sleep(0.05)

                if first_good is None:
                    cap.release()
                    continue

                self.cap = cap
                with self._lock:
                    self._frame = first_good
                self._running = True
                threading.Thread(target=self._loop_cv, daemon=True).start()
                self.available = True
                backend_name = "default" if backend is None else str(backend)
                logger.info(f"Camera started via OpenCV VideoCapture backend={backend_name} (640x480)")
                return True

            logger.warning("Camera not found or returning invalid frames")
            return False
        except Exception as e:
            logger.error(f"Camera start failed: {e}")
            return False

    def _loop_picam(self):
        while self._running:
            try:
                frame = self.picam.capture_array()
                with self._lock:
                    self._frame = frame
            except Exception:
                pass
            time.sleep(0.04)

    def _loop_cv(self):
        while self._running:
            if self.cap and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret and self.frame_usable(frame):
                    with self._lock:
                        self._frame = frame
            time.sleep(0.04)

    def get_frame(self):
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def get_jpeg(self, quality=80):
        frame = self.get_frame()
        if frame is None:
            return _placeholder_frame()
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes()

    def stop(self):
        self._running = False
        if self.picam:
            self.picam.stop()
            self.picam = None
        if self.cap:
            self.cap.release()
            self.cap = None
        self.available = False

    def restart(self):
        self.stop()
        with self._lock:
            self._frame = None
        time.sleep(0.25)
        return self.start()

def _placeholder_frame():
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = (44, 24, 16)
    cv2.putText(img, "Camera not available", (100, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 1.1, (212, 165, 116), 2)
    _, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()

camera = CameraManager()

# =============================================================================
# ARDUINO SERIAL MANAGER
# =============================================================================

class ArduinoManager:
    """
    Communicates with Arduino Uno over USB Serial using JSON protocol.

    Replaces PiGPIOManager while keeping the exact same public interface:
      - send_dispense(coffee, milk, sugar, water) → generator of progress dicts
      - send_command(cmd_string) → (bool, str)
      - stop() / cleanup()

    The generator yields dicts identical to the old PiGPIOManager:
      {"type": "progress", "component": "coffee", "pct": 50}
      {"type": "status",   "message": "..."}
      {"type": "done"}
      {"type": "error",    "message": "..."}
    """

    # How long to wait for Arduino to reboot after opening serial (seconds).
    BOOT_WAIT = 2.5

    # Read timeout per readline call (seconds).
    READ_TIMEOUT = 2.0

    # Maximum time to wait for the entire dispense cycle to finish (seconds).
    # Safety net — if Arduino goes silent for this long, abort.
    DISPENSE_HARD_TIMEOUT = 120

    def __init__(self):
        self.ser        = None
        self.connected  = False
        self.port       = None
        self._lock      = threading.Lock()
        self._stop_flag = False

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self):
        """Attempt to open the serial port to the Arduino."""
        if config.get("simulation_mode", False):
            logger.info("ArduinoManager: simulation_mode=True — skipping connect")
            return False

        if not SERIAL_AVAILABLE:
            logger.warning("ArduinoManager: pyserial not installed — "
                           "pip install pyserial")
            return False

        port = self._resolve_port()
        if port is None:
            logger.warning("ArduinoManager: No Arduino found on any serial port")
            return False

        baud = int(config.get("arduino_baud", 9600))

        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baud,
                timeout=self.READ_TIMEOUT,
                write_timeout=5
            )
            self.port = port
            logger.info(f"ArduinoManager: Opened {port} @ {baud} baud — "
                        f"waiting {self.BOOT_WAIT}s for Arduino reboot")

            # Arduino resets when the serial port opens (DTR toggles).
            # Wait for it to boot and send {"status":"ready"}.
            time.sleep(self.BOOT_WAIT)

            # Drain the boot message(s).
            ready_received = False
            deadline = time.time() + 5
            while time.time() < deadline:
                line = self._read_line()
                if line is None:
                    break
                try:
                    msg = json.loads(line)
                    if msg.get("status") == "ready":
                        ready_received = True
                        break
                except (json.JSONDecodeError, ValueError):
                    pass

            self.connected = True
            if ready_received:
                logger.info("ArduinoManager: Arduino reported READY ✓")
            else:
                logger.warning("ArduinoManager: Connected but did not receive "
                               "'ready' — continuing anyway")
            return True

        except Exception as e:
            logger.error(f"ArduinoManager: Connection failed — {e}")
            self.ser = None
            self.connected = False
            return False

    def _resolve_port(self):
        """
        Determine which serial port the Arduino is on.
        If config says "auto", scan common paths.  Otherwise use the literal value.
        """
        configured = config.get("arduino_port", "auto")

        if configured != "auto":
            if os.path.exists(configured):
                return configured
            logger.warning(f"ArduinoManager: Configured port {configured} "
                           f"does not exist — falling back to auto-detect")

        # Auto-detect: try ACM first (genuine Uno), then USB (CH340 clones).
        candidates = sorted(glob.glob("/dev/ttyACM*")) + \
                     sorted(glob.glob("/dev/ttyUSB*"))
        if candidates:
            logger.info(f"ArduinoManager: Auto-detected ports: {candidates}")
            return candidates[0]

        return None

    def reconnect(self):
        """Close and re-open the serial connection."""
        self.cleanup()
        time.sleep(1)
        return self.connect()

    # ── Low-level Serial I/O ──────────────────────────────────────────────────

    def _write_json(self, obj):
        """Serialize a dict to JSON, append newline, send over serial."""
        if self.ser is None or not self.ser.is_open:
            raise IOError("Serial port not open")
        payload = json.dumps(obj, separators=(",", ":")) + "\n"
        self.ser.write(payload.encode("utf-8"))
        self.ser.flush()
        logger.debug(f"Arduino TX: {payload.strip()}")

    def _read_line(self):
        """
        Read one newline-terminated line from serial.
        Returns the stripped string, or None on timeout / error.
        """
        if self.ser is None or not self.ser.is_open:
            return None
        try:
            raw = self.ser.readline()
            if raw:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    logger.debug(f"Arduino RX: {line}")
                    return line
        except serial.SerialException as e:
            logger.error(f"ArduinoManager: Serial read error — {e}")
            self.connected = False
        except Exception as e:
            logger.error(f"ArduinoManager: Unexpected read error — {e}")
        return None

    def _flush_input(self):
        """Discard any unread data sitting in the serial buffer."""
        if self.ser and self.ser.is_open:
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass

    # ── Dispense (main generator) ─────────────────────────────────────────────

    def send_dispense(self, coffee, milk, sugar, water):
        """
        Send a dispense command to Arduino and yield progress dicts until done.

        This is a **generator** — the caller iterates over it and gets dicts:
            {"type": "status",   "message": "Starting dispense"}
            {"type": "progress", "component": "coffee", "pct": 50}
            {"type": "flow",     "ml": 75}
            {"type": "progress", "component": "water",  "pct": 50}
            {"type": "done"}
          or
            {"type": "error",    "message": "..."}

        If Arduino is not connected, falls back to software simulation.
        """
        if not self.connected or self.ser is None:
            yield from self._simulate(coffee, milk, sugar, water)
            return

        with self._lock:
            self._stop_flag = False
            try:
                yield {"type": "status", "message": "Starting dispense"}

                # Flush stale data before sending.
                self._flush_input()

                # Build and send the dispense command.
                cmd = {
                    "coffee": int(coffee),
                    "milk":   int(milk),
                    "sugar":  int(sugar),
                    "water":  int(water)
                }
                self._write_json(cmd)

                # Read responses until we get "done", "error", or timeout.
                deadline = time.time() + self.DISPENSE_HARD_TIMEOUT

                while time.time() < deadline:
                    if self._stop_flag:
                        # Emergency stop was requested from another thread.
                        yield {"type": "error", "message": "Emergency stop"}
                        return

                    line = self._read_line()
                    if line is None:
                        # Timeout on this read — not necessarily fatal.
                        # The Arduino might just be busy spinning a servo.
                        continue

                    try:
                        msg = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        logger.warning(f"ArduinoManager: Non-JSON line: {line}")
                        continue

                    status = msg.get("status", "")

                    # ── Map Arduino JSON → internal progress dicts ────────
                    if status == "dispensing":
                        component = msg.get("component", "unknown")
                        pct       = msg.get("pct", 0)
                        out = {
                            "type":      "progress",
                            "component": component,
                            "pct":       pct
                        }
                        # Attach extra fields if present.
                        if "ml" in msg:
                            out["ml"] = msg["ml"]
                        if "level" in msg:
                            out["level"] = msg["level"]
                        yield out

                        # Also yield a flow dict for water (matches old
                        # PiGPIOManager interface that the frontend expects).
                        if component == "water" and "ml" in msg:
                            ml_dispensed = int(msg["ml"] * pct / 100) \
                                          if pct > 0 else 0
                            yield {"type": "flow", "ml": ml_dispensed}

                    elif status == "dispensing_start":
                        yield {
                            "type":    "status",
                            "message": "Arduino dispensing started"
                        }

                    elif status == "done":
                        yield {"type": "done"}
                        return

                    elif status == "stopped":
                        yield {
                            "type":    "error",
                            "message": "Dispensing stopped by Arduino"
                        }
                        return

                    elif status == "error":
                        yield {
                            "type":    "error",
                            "message": msg.get("message", "Unknown Arduino error")
                        }
                        return

                    elif status == "ready":
                        # Arduino rebooted mid-cycle — unexpected.
                        yield {
                            "type":    "error",
                            "message": "Arduino rebooted unexpectedly"
                        }
                        return

                    # Any other status — log and continue.
                    else:
                        logger.info(f"ArduinoManager: Unhandled status: {msg}")

                # If we exit the while loop, we timed out.
                logger.error("ArduinoManager: Dispense hard timeout reached")
                self._send_stop()
                yield {
                    "type":    "error",
                    "message": "Dispense timeout — Arduino not responding"
                }

            except IOError as e:
                logger.error(f"ArduinoManager: IO error during dispense — {e}")
                self.connected = False
                yield {"type": "error", "message": f"Serial error: {e}"}

            except Exception as e:
                logger.error(f"ArduinoManager: Unexpected error — {e}")
                self._send_stop()
                yield {"type": "error", "message": str(e)}

    # ── Simulation fallback ───────────────────────────────────────────────────

    def _simulate(self, coffee, milk, sugar, water):
        """Software-only simulation when Arduino is not connected."""
        yield {
            "type":    "status",
            "message": "Simulation mode — no Arduino connected"
        }
        steps = []
        if coffee > 0:
            steps.append(("coffee", coffee * 0.5))
        if milk > 0:
            steps.append(("milk",   milk   * 0.5))
        if sugar > 0:
            steps.append(("sugar",  sugar  * 0.4))
        steps.append(("water", water / 120.0))

        for name, dur in steps:
            for pct in range(0, 101, 10):
                time.sleep(dur * 0.1)
                yield {
                    "type":      "progress",
                    "component": name,
                    "pct":       pct
                }
        yield {"type": "done"}

    # ── Admin / utility commands ──────────────────────────────────────────────

    def send_command(self, cmd):
        """
        Send a short admin command to Arduino.
        Returns (success: bool, message: str).

        Supported commands (matches old PiGPIOManager interface):
            "CMD:TEST"   → {"cmd":"TEST"}
            "CMD:STATUS" → returns connection info (no Arduino command needed)
            "CMD:BUZZ"   → {"cmd":"TEST"} (buzzer is tested as part of TEST)
            "CMD:STOP"   → {"cmd":"STOP"}
        """
        if not self.connected or self.ser is None:
            return False, "Arduino not connected"

        with self._lock:
            try:
                if cmd == "CMD:STATUS":
                    return True, (f"Arduino OK | port={self.port} | "
                                  f"connected={self.connected}")

                elif cmd == "CMD:TEST":
                    self._flush_input()
                    self._write_json({"cmd": "TEST"})
                    return self._wait_for_completion("test_complete", timeout=15)

                elif cmd == "CMD:BUZZ":
                    # The Arduino TEST command already includes a buzzer beep.
                    # Send TEST and report success.
                    self._flush_input()
                    self._write_json({"cmd": "TEST"})
                    return self._wait_for_completion("test_complete", timeout=15)

                elif cmd == "CMD:STOP":
                    self._send_stop()
                    return True, "STOP sent to Arduino"

                else:
                    return False, f"Unknown command: {cmd}"

            except IOError as e:
                self.connected = False
                return False, f"Serial error: {e}"
            except Exception as e:
                return False, str(e)

    def _wait_for_completion(self, expected_status, timeout=15):
        """
        Read serial lines until we see the expected status or timeout.
        Returns (success, message).
        """
        deadline = time.time() + timeout
        messages = []
        while time.time() < deadline:
            line = self._read_line()
            if line is None:
                continue
            try:
                msg = json.loads(line)
                status = msg.get("status", "")
                messages.append(status)
                if status == expected_status:
                    return True, f"{expected_status} — OK"
                if status == "error":
                    return False, msg.get("message", "Arduino error")
                if status == "stopped":
                    return False, "Stopped by Arduino"
            except (json.JSONDecodeError, ValueError):
                pass
        return False, f"Timeout waiting for '{expected_status}'"

    def _send_stop(self):
        """Send emergency stop command to Arduino (best-effort)."""
        try:
            if self.ser and self.ser.is_open:
                self._write_json({"cmd": "STOP"})
        except Exception as e:
            logger.error(f"ArduinoManager: Failed to send STOP — {e}")

    def stop(self):
        """Public method — send STOP to Arduino. Called by /api/vend/stop."""
        self._stop_flag = True
        self._send_stop()

    def cleanup(self):
        """Close the serial port and release resources."""
        self._stop_flag = True
        if self.ser:
            try:
                self._send_stop()
                time.sleep(0.1)
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.connected = False
        self.port = None
        logger.info("ArduinoManager: Cleaned up")


# Instantiate the global Arduino manager (replaces pi_gpio).
arduino = ArduinoManager()

# =============================================================================
# HELPERS
# =============================================================================

def calculate_price(coffee_type, cs, ml, sl, wm):
    p = config["prices"]
    if coffee_type in ("black", "espresso", "cappuccino", "latte"):
        return float(p.get(coffee_type, 20))
    base  = float(p.get("custom_base", 10))
    base += cs * float(p.get("per_coffee_level", 2))
    base += ml * float(p.get("per_milk_level",   1.5))
    base += sl * float(p.get("per_sugar_level",  0.5))
    base += (wm / 50) * float(p.get("per_50ml_water", 1))
    return round(base, 2)

def encode_face(image_bgr):
    if not FACE_RECOGNITION_AVAILABLE:
        return None
    rgb  = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    locs = face_recognition.face_locations(rgb, model="hog")
    if not locs:
        return None
    encs = face_recognition.face_encodings(rgb, locs)
    return encs[0].tolist() if encs else None

def image_hash64(image_bgr):
    """Simple aHash (64-bit) used as fallback when face_recognition is unavailable."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
    avg = float(np.mean(small))
    bits = (small > avg).astype(np.uint8).flatten().tolist()
    return "".join(str(int(b)) for b in bits)

def hamming_distance(a, b):
    if not a or not b or len(a) != len(b):
        return 10**9
    return sum(ch1 != ch2 for ch1, ch2 in zip(a, b))

def frame_quality_score(image_bgr):
    if image_bgr is None:
        return 0.0
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    contrast = float(np.std(gray))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    exposure_balance = max(0.0, 127.5 - abs(brightness - 127.5))
    score = (contrast * 0.9) + (sharpness / 7.0) + (exposure_balance * 0.12)
    return round(min(100.0, max(0.0, score)), 2)

def _robust_user_score(values, top_k=3):
    if not values:
        return None
    ordered = sorted(values)
    chosen = ordered[:max(1, min(top_k, len(ordered)))]
    return float(sum(chosen)) / float(len(chosen))

def save_face_sample(user_id, image_bgr, source="capture", angle_label="front"):
    """Store a face sample to gradually improve recognition quality."""
    face_enc_json = None
    if FACE_RECOGNITION_AVAILABLE:
        try:
            enc = encode_face(image_bgr)
            if enc is not None:
                face_enc_json = json.dumps(enc)
        except Exception:
            face_enc_json = None

    try:
        img_hash = image_hash64(image_bgr)
    except Exception:
        img_hash = None

    quality = frame_quality_score(image_bgr)

    if face_enc_json is None and img_hash is None:
        return False

    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO user_face_samples
                (user_id, face_encoding, image_hash, angle_label, quality_score, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, face_enc_json, img_hash, angle_label, quality, source)
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()

def find_user_by_face(image_bgr):
    if not FACE_RECOGNITION_AVAILABLE:
        return find_user_by_hash(image_bgr)
    enc = encode_face(image_bgr)
    if enc is None:
        return None, None
    query = np.array(enc)
    thr   = float(config.get("face_recognition_threshold", 0.55))
    db    = get_db()
    samples = db.execute(
        """
        SELECT s.user_id AS id, u.name, s.face_encoding, s.quality_score
        FROM user_face_samples s
        JOIN users u ON u.id = s.user_id
        WHERE s.face_encoding IS NOT NULL
        """
    ).fetchall()
    if not samples:
        samples = db.execute(
            "SELECT id, name, face_encoding FROM users WHERE face_encoding IS NOT NULL"
        ).fetchall()
    db.close()
    per_user = {}
    for u in samples:
        stored = np.array(json.loads(u["face_encoding"]))
        dist   = face_recognition.face_distance([stored], query)[0]
        quality = float(u["quality_score"] or 0.0)
        adjusted = max(0.0, float(dist) - min(0.06, quality / 1800.0))
        per_user.setdefault((u["id"], u["name"]), []).append(adjusted)

    best_user, best_dist = None, None
    for (uid, uname), dists in per_user.items():
        score = _robust_user_score(dists, top_k=3)
        if score is None:
            continue
        if best_dist is None or score < best_dist:
            best_dist = score
            best_user = {"id": uid, "name": uname}

    if best_user is None or best_dist is None or best_dist >= thr:
        return None, None
    return best_user, best_dist

def find_user_by_hash(image_bgr):
    """Fallback match by image hash for environments without dlib/face_recognition."""
    query_hash = image_hash64(image_bgr)
    db = get_db()
    samples = db.execute(
        """
        SELECT s.user_id AS id, u.name, s.image_hash, s.quality_score
        FROM user_face_samples s
        JOIN users u ON u.id = s.user_id
        WHERE s.image_hash IS NOT NULL
        """
    ).fetchall()
    if not samples:
        samples = db.execute(
            "SELECT id, name, image_hash FROM users WHERE image_hash IS NOT NULL"
        ).fetchall()
    db.close()
    per_user = {}
    for u in samples:
        dist = hamming_distance(query_hash, u["image_hash"])
        quality = float(u["quality_score"] or 0.0)
        adjusted = max(0.0, float(dist) - min(2.0, quality / 35.0))
        per_user.setdefault((u["id"], u["name"]), []).append(adjusted)

    best_user = None
    best_dist = None
    for (uid, uname), dists in per_user.items():
        score = _robust_user_score(dists, top_k=3)
        if score is None:
            continue
        if best_dist is None or score < best_dist:
            best_dist = score
            best_user = {"id": uid, "name": uname}

    # Threshold tuned conservatively for 64-bit aHash multi-sample scoring.
    if best_user is None or best_dist is None or best_dist > 9:
        return None, None
    return best_user, best_dist

def get_user_sample_summary(user_id):
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT angle_label, COUNT(*) AS c
            FROM user_face_samples
            WHERE user_id = ?
            GROUP BY angle_label
            """,
            (user_id,)
        ).fetchall()
        total = sum(int(r["c"]) for r in rows)
        by_angle = {str(r["angle_label"] or "front"): int(r["c"]) for r in rows}
        required = ["front", "left", "right", "up", "down"]
        missing = [a for a in required if by_angle.get(a, 0) < 1]
        return {
            "total_samples": total,
            "by_angle": by_angle,
            "missing_angles": missing,
            "enrollment_complete": len(missing) == 0 and total >= 5
        }
    finally:
        db.close()

def generate_upi_qr(amount, order_id):
    upi_id   = config.get("upi_id",   "coffebot@paytm")
    upi_name = config.get("upi_name", "CafeBot")
    desc     = f"Coffee Order #{order_id}"
    upi_url  = (f"upi://pay?pa={upi_id}&pn={upi_name}"
                f"&am={amount:.2f}&cu=INR&tn={desc}")
    if QRCODE_AVAILABLE:
        qr = qrcode.QRCode(version=1, box_size=8, border=4)
        qr.add_data(upi_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#2C1810", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode(), upi_url
    return None, upi_url

def generate_text_qr_base64(payload):
    if not QRCODE_AVAILABLE:
        return None
    qr = qrcode.QRCode(version=1, box_size=8, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#2C1810", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# =============================================================================
# ROUTES — MAIN UI
# =============================================================================

@app.route("/")
def index():
    return render_template("index.html",
        machine_name     = config.get("machine_name", "CaféBot"),
        currency_symbol  = config.get("currency_symbol", "₹"),
        face_recognition = FACE_RECOGNITION_AVAILABLE,
        sim_mode         = config.get("simulation_mode", False) or not arduino.connected,
        presets          = config.get("presets", {}),
        prices           = config.get("prices",  {}),
        water_config     = config.get("water",   {}),
    )

@app.route("/admin")
def admin_page():
    return render_template("admin.html",
        machine_name = config.get("machine_name", "CaféBot"))

# =============================================================================
# ROUTES — STATUS & CAMERA
# =============================================================================

@app.route("/api/status")
def api_status():
    return jsonify({
        "machine_name":      config.get("machine_name"),
        "arduino_connected": arduino.connected,
        "arduino_port":      arduino.port,
        "camera_available":  camera.available,
        "face_recognition":  FACE_RECOGNITION_AVAILABLE,
        "simulation_mode":   config.get("simulation_mode", False) or not arduino.connected,
        "upi_id":            config.get("upi_id"),
        "timestamp":         datetime.now().isoformat()
    })

@app.route("/api/payment/qr", methods=["POST"])
def api_payment_qr():
    data = request.get_json(silent=True) or {}
    gateway = str(data.get("gateway", "UPI")).lower()
    amount = float(data.get("amount", 0) or 0)
    order_ref = str(data.get("order_ref", f"ord-{int(time.time())}"))

    if amount <= 0:
        return jsonify({"success": False, "error": "Amount must be greater than 0"}), 400

    if gateway in ("upi", "paytm", "phonepe", "gpay", "googlepay"):
        upi_id = config.get("upi_id", "coffebot@paytm")
        upi_name = config.get("upi_name", "CafeBot")
        payload = (
            f"upi://pay?pa={upi_id}&pn={upi_name}&am={amount:.2f}&cu=INR"
            f"&tn=Coffee%20Order%20{order_ref}"
        )
    else:
        payload = f"{gateway.upper()}|ORDER={order_ref}|AMOUNT={amount:.2f}|INR"

    qr_image = generate_text_qr_base64(payload)
    return jsonify({
        "success": True,
        "gateway": gateway,
        "amount": amount,
        "payload": payload,
        "qr_image": qr_image
    })

@app.route("/api/camera/stream")
def camera_stream():
    def gen():
        while True:
            jpeg = camera.get_jpeg()
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
            time.sleep(0.04)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/api/camera/capture", methods=["POST"])
def camera_capture():
    frame = camera.get_frame()
    if frame is None or not camera.frame_usable(frame):
        # Attempt one automatic recovery cycle for black/stale frames.
        camera.restart()
        time.sleep(0.2)
        frame = camera.get_frame()
    if frame is None or not camera.frame_usable(frame):
        return jsonify({
            "success": False,
            "error": "Camera frame is invalid/black. Please check lighting and close other camera apps."
        })
    draw = frame.copy()
    has_face = False
    if FACE_RECOGNITION_AVAILABLE:
        rgb  = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        locs = face_recognition.face_locations(rgb, model="hog")
        has_face = len(locs) > 0
        for top, right, bottom, left in locs:
            cv2.rectangle(draw, (left, top), (right, bottom), (0, 210, 100), 2)
    elif camera.available:
        # Fallback path when FR model is unavailable: accept only usable frames.
        has_face = camera.frame_usable(frame)
    _, buf = cv2.imencode(".jpg", draw, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return jsonify({
        "success":  True,
        "image":    base64.b64encode(buf.tobytes()).decode(),
        "has_face": has_face
    })

# =============================================================================
# ROUTES — USER MANAGEMENT
# =============================================================================

@app.route("/api/register", methods=["POST"])
def api_register():
    data    = request.get_json(silent=True) or {}
    name    = str(data.get("name", "")).strip()
    img_b64 = data.get("image", "")

    if len(name) < 2:
        return jsonify({"success": False, "error": "Name must be at least 2 characters"})
    if len(name) > 50:
        return jsonify({"success": False, "error": "Name too long (max 50 chars)"})

    face_enc_json = None
    image_hash = None
    if img_b64 and FACE_RECOGNITION_AVAILABLE:
        try:
            img_bgr  = cv2.imdecode(
                np.frombuffer(base64.b64decode(img_b64), dtype=np.uint8),
                cv2.IMREAD_COLOR
            )
            encoding = encode_face(img_bgr)
            if encoding is None:
                return jsonify({"success": False,
                                "error": "No face detected. Look directly at the camera."})
            face_enc_json = json.dumps(encoding)
            image_hash = image_hash64(img_bgr)
        except Exception as e:
            logger.error(f"Face encoding error: {e}")
            return jsonify({"success": False, "error": "Face processing failed"})
    elif img_b64:
        # Save a robust-enough visual fingerprint even when FR libs are missing.
        try:
            img_bgr = cv2.imdecode(
                np.frombuffer(base64.b64decode(img_b64), dtype=np.uint8),
                cv2.IMREAD_COLOR
            )
            if img_bgr is not None:
                image_hash = image_hash64(img_bgr)
        except Exception:
            image_hash = None

    db = get_db()
    try:
        cur     = db.execute(
            "INSERT INTO users (name, face_encoding, photo_base64, image_hash) VALUES (?, ?, ?, ?)",
            (name, face_enc_json, img_b64 or None, image_hash)
        )
        user_id = cur.lastrowid
        db.execute("INSERT INTO preferences (user_id) VALUES (?)", (user_id,))
        db.commit()
        if img_b64:
            try:
                img_bgr = cv2.imdecode(
                    np.frombuffer(base64.b64decode(img_b64), dtype=np.uint8),
                    cv2.IMREAD_COLOR
                )
                if img_bgr is not None:
                    save_face_sample(user_id, img_bgr, source="register", angle_label="front")
            except Exception:
                pass
        logger.info(f"Registered: {name} (id={user_id})")
        return jsonify({"success": True, "user_id": user_id, "name": name})
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": "Registration failed"})
    finally:
        db.close()

@app.route("/api/face-scan", methods=["POST"])
def api_face_scan():
    frame = camera.get_frame()
    if frame is None:
        return jsonify({"success": False, "error": "Camera not available"})
    user, dist = find_user_by_face(frame)
    if user is None:
        return jsonify({"success": False, "error": "Face not recognised", "face_found": False})
    db   = get_db()
    pref = db.execute("SELECT * FROM preferences WHERE user_id = ?", (user["id"],)).fetchone()
    db.execute("UPDATE users SET visit_count = visit_count + 1 WHERE id = ?", (user["id"],))
    db.commit()
    db.close()
    # Continuously learn from each successful scan to improve matching over time.
    try:
        save_face_sample(user["id"], frame, source="face_scan", angle_label="front")
    except Exception:
        pass
    if FACE_RECOGNITION_AVAILABLE:
        confidence = round((1 - float(dist)) * 100, 1)
        match_mode = "face_encoding"
    else:
        confidence = round(max(0.0, (1 - (float(dist) / 64.0)) * 100), 1)
        match_mode = "image_hash"
    return jsonify({
        "success":     True,
        "user_id":     user["id"],
        "name":        user["name"],
        "confidence":  confidence,
        "match_mode":  match_mode,
        "preferences": dict(pref) if pref else None,
        "sample_summary": get_user_sample_summary(user["id"])
    })

@app.route("/api/users/<int:user_id>/face-enrollment-status")
def api_face_enrollment_status(user_id):
    db = get_db()
    user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    db.close()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404
    return jsonify({"success": True, "summary": get_user_sample_summary(user_id)})

@app.route("/api/users/<int:user_id>/face-enroll-sample", methods=["POST"])
def api_face_enroll_sample(user_id):
    data = request.get_json(silent=True) or {}
    img_b64 = data.get("image", "")
    angle_label = str(data.get("angle_label", "front")).strip().lower()
    source = str(data.get("source", "guided_enroll")).strip()[:40]
    allowed_angles = {"front", "left", "right", "up", "down"}
    if angle_label not in allowed_angles:
        angle_label = "front"

    db = get_db()
    user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    db.close()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    if not img_b64:
        return jsonify({"success": False, "error": "Image is required"}), 400

    try:
        img_bgr = cv2.imdecode(
            np.frombuffer(base64.b64decode(img_b64), dtype=np.uint8),
            cv2.IMREAD_COLOR
        )
        if img_bgr is None or not camera.frame_usable(img_bgr):
            return jsonify({"success": False, "error": "Invalid or too-dark frame"}), 400
        if FACE_RECOGNITION_AVAILABLE and encode_face(img_bgr) is None:
            return jsonify({"success": False, "error": "No face detected in sample"}), 400
        ok = save_face_sample(user_id, img_bgr, source=source, angle_label=angle_label)
        if not ok:
            return jsonify({"success": False, "error": "Could not store sample"}), 500
        return jsonify({
            "success": True,
            "angle_label": angle_label,
            "summary": get_user_sample_summary(user_id)
        })
    except Exception as e:
        logger.error(f"Enroll sample error: {e}")
        return jsonify({"success": False, "error": "Sample processing failed"}), 500

@app.route("/api/users/<int:user_id>/preferences", methods=["POST"])
def save_preferences(user_id):
    data = request.get_json(silent=True) or {}
    db   = get_db()
    try:
        db.execute("""
            INSERT INTO preferences
                (user_id,coffee_type,coffee_strength,milk_level,sugar_level,water_ml,updated_at)
            VALUES (?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                coffee_type=excluded.coffee_type,
                coffee_strength=excluded.coffee_strength,
                milk_level=excluded.milk_level,
                sugar_level=excluded.sugar_level,
                water_ml=excluded.water_ml,
                updated_at=excluded.updated_at
        """, (user_id,
              data.get("coffee_type",     "custom"),
              max(0, min(5, int(data.get("coffee_strength", 3)))),
              max(0, min(5, int(data.get("milk_level",      2)))),
              max(0, min(5, int(data.get("sugar_level",     1)))),
              max(80, min(250, int(data.get("water_ml",   150))))))
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": str(e)})
    finally:
        db.close()

@app.route("/api/users/<int:user_id>/auto-dispense", methods=["POST"])
def auto_dispense_previous(user_id):
    """Dispense using the user's stored preference without manual customization."""
    db = get_db()
    try:
        user = db.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        pref = db.execute("SELECT * FROM preferences WHERE user_id = ?", (user_id,)).fetchone()
        if not pref:
            return jsonify({"success": False, "error": "No saved preference found"}), 404

        coffee_type = str(pref["coffee_type"] or "custom")
        cs = int(pref["coffee_strength"])
        ml = int(pref["milk_level"])
        sl = int(pref["sugar_level"])
        wm = int(pref["water_ml"])
        amount = calculate_price(coffee_type, cs, ml, sl, wm)

        cur = db.execute("""
            INSERT INTO orders
                (user_id,user_name,coffee_type,coffee_strength,
                 milk_level,sugar_level,water_ml,amount,payment_method,payment_status,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (user_id, user["name"], coffee_type, cs, ml, sl, wm,
              amount, "auto", "paid", "vending"))
        order_id = cur.lastrowid
        db.commit()
    finally:
        db.close()

    last_update = {"type": "error", "message": "Unknown error"}
    for update in arduino.send_dispense(coffee=cs, milk=ml, sugar=sl, water=wm):
        last_update = update
        if update.get("type") in ("done", "error"):
            break

    final_status = "completed" if last_update.get("type") == "done" else "failed"
    db = get_db()
    db.execute("UPDATE orders SET status=? WHERE id=?", (final_status, order_id))
    db.commit()
    db.close()

    return jsonify({
        "success": final_status == "completed",
        "order_id": order_id,
        "status": final_status,
        "last_update": last_update
    })

# =============================================================================
# ROUTES — ORDERS & PAYMENT
# =============================================================================

@app.route("/api/order", methods=["POST"])
def api_place_order():
    data = request.get_json(silent=True) or {}
    user_id    = data.get("user_id")
    user_name  = str(data.get("user_name", "Guest")).strip()[:50]
    coffee_type = str(data.get("coffee_type", "custom")).strip()
    if coffee_type not in ("black", "espresso", "cappuccino", "latte", "custom"):
        coffee_type = "custom"
    try:
        cs = max(0, min(5, int(data.get("coffee_strength", 3))))
        ml = max(0, min(5, int(data.get("milk_level",      2))))
        sl = max(0, min(5, int(data.get("sugar_level",     1))))
        wm = max(80, min(250, int(data.get("water_ml",    150))))
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid parameters"})
    pm = str(data.get("payment_method", "cash"))
    if pm not in ("cash", "card", "upi", "paypal", "paytm", "phonepe", "gpay", "auto"):
        return jsonify({"success": False, "error": "Invalid payment method"})
    amount = calculate_price(coffee_type, cs, ml, sl, wm)
    db = get_db()
    try:
        cur = db.execute("""
            INSERT INTO orders
                (user_id,user_name,coffee_type,coffee_strength,
                 milk_level,sugar_level,water_ml,amount,payment_method)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (user_id, user_name, coffee_type, cs, ml, sl, wm, amount, pm))
        order_id = cur.lastrowid
        db.commit()
        if user_id:
            db.execute("""
                INSERT INTO preferences
                    (user_id,coffee_type,coffee_strength,milk_level,sugar_level,water_ml)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    coffee_type=excluded.coffee_type,
                    coffee_strength=excluded.coffee_strength,
                    milk_level=excluded.milk_level,
                    sugar_level=excluded.sugar_level,
                    water_ml=excluded.water_ml,
                    updated_at=datetime('now')
            """, (user_id, coffee_type, cs, ml, sl, wm))
            db.commit()
        resp = {"success": True, "order_id": order_id, "amount": amount}
        if pm == "upi":
            qr_img, upi_url = generate_upi_qr(amount, order_id)
            resp.update(qr_image=qr_img, upi_url=upi_url, upi_id=config.get("upi_id"))
        return jsonify(resp)
    except Exception as e:
        db.rollback()
        logger.error(f"Order error: {e}")
        return jsonify({"success": False, "error": "Order creation failed"})
    finally:
        db.close()

@app.route("/api/payment/confirm", methods=["POST"])
def api_confirm_payment():
    data     = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"success": False, "error": "Order ID required"})
    db = get_db()
    try:
        order = db.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            return jsonify({"success": False, "error": "Order not found"})
        if order["status"] != "pending":
            return jsonify({"success": False, "error": f"Order is {order['status']}"})
        db.execute(
            "UPDATE orders SET payment_status='paid', status='processing' WHERE id = ?",
            (order_id,))
        db.commit()
        return jsonify({"success": True, "order_id": order_id})
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": str(e)})
    finally:
        db.close()

# =============================================================================
# SOCKETIO — VENDING
# =============================================================================

@socketio.on("start_vend")
def handle_start_vend(data):
    order_id = data.get("order_id")
    if not order_id:
        emit("vend_update", {"type": "error", "message": "No order ID"})
        return
    db    = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        db.close()
        emit("vend_update", {"type": "error", "message": "Order not found"})
        return
    if order["status"] not in ("processing", "pending"):
        db.close()
        emit("vend_update", {"type": "error",
                             "message": f"Order status: {order['status']}"})
        return
    db.execute("UPDATE orders SET status='vending' WHERE id = ?", (order_id,))
    db.commit()
    db.close()
    socketio.start_background_task(
        _vend_task, order_dict=dict(order), sid=request.sid)

def _vend_task(order_dict, sid):
    order_id    = order_dict["id"]
    last_update = {"type": "error"}
    try:
        for update in arduino.send_dispense(
            coffee = order_dict["coffee_strength"],
            milk   = order_dict["milk_level"],
            sugar  = order_dict["sugar_level"],
            water  = order_dict["water_ml"]
        ):
            socketio.emit("vend_update", update, room=sid)
            last_update = update
            if update.get("type") == "error":
                break
    except Exception as e:
        logger.error(f"Vend task error: {e}")
        socketio.emit("vend_update", {"type": "error", "message": str(e)}, room=sid)
    finally:
        db  = get_db()
        fin = "completed" if last_update.get("type") == "done" else "failed"
        db.execute("UPDATE orders SET status=? WHERE id=?", (fin, order_id))
        db.commit()
        db.close()
        logger.info(f"Order {order_id}: {fin}")

@app.route("/api/vend/stop", methods=["POST"])
def api_vend_stop():
    arduino.stop()
    return jsonify({"success": True})

# =============================================================================
# ROUTES — ADMIN
# =============================================================================

@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    data = request.get_json(silent=True) or {}
    pw   = data.get("password", "")
    if hashlib.sha256(pw.encode()).hexdigest() == config.get("admin_password_hash", ""):
        session["admin_logged_in"] = True
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Invalid password"})

@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout():
    session.pop("admin_logged_in", None)
    return jsonify({"success": True})

@app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    db = get_db()
    try:
        r = lambda q: db.execute(q).fetchone()[0]
        recent = [dict(o) for o in db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT 15").fetchall()]
        return jsonify({
            "success": True,
            "stats": {
                "total_orders":  r("SELECT COUNT(*) FROM orders WHERE status='completed'"),
                "today_orders":  r("SELECT COUNT(*) FROM orders WHERE status='completed' AND date(created_at)=date('now')"),
                "total_revenue": round(float(r("SELECT COALESCE(SUM(amount),0) FROM orders WHERE payment_status='paid'")), 2),
                "today_revenue": round(float(r("SELECT COALESCE(SUM(amount),0) FROM orders WHERE payment_status='paid' AND date(created_at)=date('now')")), 2),
                "total_users":   r("SELECT COUNT(*) FROM users"),
                "arduino":       arduino.connected,
                "arduino_port":  arduino.port,
                "camera":        camera.available,
                "face_rec":      FACE_RECOGNITION_AVAILABLE,
            },
            "recent_orders": recent
        })
    finally:
        db.close()

@app.route("/api/admin/orders")
@admin_required
def api_admin_orders():
    db     = get_db()
    orders = db.execute(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT 200").fetchall()
    db.close()
    return jsonify({"success": True, "orders": [dict(o) for o in orders]})

@app.route("/api/admin/users")
@admin_required
def api_admin_users():
    db    = get_db()
    users = db.execute(
        "SELECT id,name,visit_count,created_at FROM users ORDER BY visit_count DESC"
    ).fetchall()
    db.close()
    return jsonify({"success": True, "users": [dict(u) for u in users]})

@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_user(user_id):
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/admin/calibrate", methods=["POST"])
@admin_required
def api_admin_calibrate():
    data    = request.get_json(silent=True) or {}
    command = data.get("command", "CMD:TEST")
    allowed = {"CMD:TEST", "CMD:STATUS", "CMD:BUZZ", "CMD:STOP"}
    if command not in allowed:
        return jsonify({"success": False, "error": "Command not permitted"})
    ok, resp = arduino.send_command(command)
    return jsonify({"success": ok, "response": resp})

@app.route("/api/admin/config", methods=["GET", "POST"])
@admin_required
def api_admin_config():
    if request.method == "GET":
        safe = {k: v for k, v in config.items() if k != "admin_password_hash"}
        return jsonify({"success": True, "config": safe})
    data         = request.get_json(silent=True) or {}
    allowed_keys = {"machine_name","upi_id","upi_name","prices","presets",
                    "water","reset_delay_sec","idle_timeout_sec",
                    "face_recognition_threshold","arduino_port","arduino_baud"}
    for k, v in data.items():
        if k in allowed_keys:
            config[k] = v
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    return jsonify({"success": True})

@app.route("/api/admin/change-password", methods=["POST"])
@admin_required
def api_change_password():
    data = request.get_json(silent=True) or {}
    pw   = data.get("new_password", "")
    if len(pw) < 6:
        return jsonify({"success": False, "error": "Minimum 6 characters"})
    config["admin_password_hash"] = hashlib.sha256(pw.encode()).hexdigest()
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    return jsonify({"success": True})

# =============================================================================
# STARTUP
# =============================================================================

def startup():
    logger.info("=== CaféBot starting ===")
    init_db()
    camera.start()
    if not config.get("simulation_mode", False):
        if not arduino.connect():
            logger.warning("Arduino not available — running in simulation mode")
            logger.warning("  -> Check USB cable, run: ls /dev/ttyACM* /dev/ttyUSB*")
    else:
        logger.info("Simulation mode — no hardware required")

if __name__ == "__main__":
    startup()
    try:
        socketio.run(app, host="0.0.0.0", port=5000, debug=False,
                     allow_unsafe_werkzeug=True)
    finally:
        arduino.cleanup()
        camera.stop()