"""Configuration, safe file transfer, and local log helpers for BSTK Chat."""

import json
import os
import re
from typing import Any, Dict, List

DEFAULT_BROKER = "broker.hivemq.com"
DEFAULT_TLS_PORT = 8883
DEFAULT_PLAIN_PORT = 1883
MAX_FILE_BYTES = 512 * 1024  # 512 KB limit for MQTT payload safety

VALID_THEMES = ("dark", "light", "ansi")


def get_config_path() -> str:
    return os.path.expanduser("~/.bstkchat/config.json")


def load_config() -> Dict[str, Any]:
    """Loads persisted user preferences from ~/.bstkchat/config.json."""
    path = get_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: Dict[str, Any]) -> None:
    """Persists user preferences (never passwords) to ~/.bstkchat/config.json."""
    path = get_config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass


def record_recent_room(room_id: str, nickname: str, has_password: bool) -> List[Dict[str, Any]]:
    """Updates the recent rooms list in config.json (never stores passwords)."""
    cfg = load_config()
    recent = cfg.get("recent_rooms", [])
    if not isinstance(recent, list):
        recent = []
    recent = [r for r in recent if isinstance(r, dict) and r.get("room_id") != room_id]
    recent.insert(0, {
        "room_id": room_id,
        "nickname": nickname,
        "has_password": bool(has_password),
    })
    cfg["recent_rooms"] = recent[:10]
    save_config(cfg)
    return cfg["recent_rooms"]


def sanitize_filename(filename: str) -> str:
    """Strips directory components and unsafe characters from an incoming filename."""
    base = os.path.basename(filename.strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    return cleaned or "received_file.bin"


def save_received_file(filename: str, data: bytes, target_dir: str = "") -> str:
    """Saves received bytes safely into ~/Downloads/bstkchat without overwriting."""
    dest_dir = target_dir or os.path.expanduser("~/Downloads/bstkchat")
    os.makedirs(dest_dir, exist_ok=True)

    safe_name = sanitize_filename(filename)
    stem, ext = os.path.splitext(safe_name)
    candidate = os.path.join(dest_dir, safe_name)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dest_dir, f"{stem}_{counter}{ext}")
        counter += 1

    with open(candidate, "wb") as f:
        f.write(data)
    return candidate


def tail_log(log_path: str, limit: int = 15) -> List[str]:
    """Returns the last `limit` lines from a local log file."""
    if limit <= 0 or not os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [line.rstrip("\r\n") for line in f]
        return lines[-limit:]
    except OSError:
        return []


def search_log(log_path: str, query: str, limit: int = 25) -> List[str]:
    """Searches a local log file case-insensitively and returns matching lines."""
    if not query.strip() or not os.path.exists(log_path):
        return []
    needle = query.strip().lower()
    matches: List[str] = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f, start=1):
                if needle in line.lower():
                    matches.append(f"L{idx}: {line.rstrip()}")
        return matches[-limit:]
    except OSError:
        return []
