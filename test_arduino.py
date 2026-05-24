#!/usr/bin/env python3
"""
Simple Arduino serial connectivity test for CafeBot.

Usage:
  python3 test_arduino.py
  python3 test_arduino.py /dev/ttyUSB0
  python3 test_arduino.py COM5 --baud 9600
"""

import argparse
import json
import os
import sys

from arduino_handler import ArduinoManager


def load_config(base_dir):
    config_path = os.path.join(base_dir, "config.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_args():
    parser = argparse.ArgumentParser(description="Test Arduino serial communication")
    parser.add_argument(
        "port",
        nargs="?",
        default=None,
        help="Optional serial port override (example: /dev/ttyUSB0 or COM5)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help="Optional baud rate override (default from config.json or 9600)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg = load_config(base_dir)

    cfg["simulation_mode"] = False
    if args.port:
        cfg["arduino_port"] = args.port
    if args.baud:
        cfg["arduino_baud"] = args.baud

    manager = ArduinoManager(config=cfg)

    print("=== Arduino Serial Test ===")
    print(f"Port setting: {cfg.get('arduino_port', 'auto')}")
    print(f"Baud setting: {cfg.get('arduino_baud', 9600)}")

    try:
        if not manager.connect():
            print("ERROR: Could not connect to Arduino")
            return 1

        print(f"Connected: {manager.connected} on {manager.port}")

        ok, msg = manager.send_command("CMD:STATUS")
        if ok:
            print(f"STATUS OK: {msg}")
            return 0

        print(f"STATUS FAIL: {msg}")
        return 2

    except KeyboardInterrupt:
        print("Interrupted")
        return 130

    finally:
        manager.cleanup()


if __name__ == "__main__":
    sys.exit(main())
