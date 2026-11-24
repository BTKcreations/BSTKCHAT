import argparse
import base64
import hashlib
import json
import os
import queue
import random
import re
import shlex
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

# Styling and layout packages
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as escape_markup
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Input and output handlers to prevent visual corruption
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear as pt_clear
from prompt_toolkit.styles import DynamicStyle, Style as PromptStyle

from bstkchat.chain import RoomChain
from bstkchat.config import (
    DEFAULT_BROKER,
    DEFAULT_PLAIN_PORT,
    DEFAULT_TLS_PORT,
    MAX_FILE_BYTES,
    VALID_THEMES,
    load_config,
    save_config,
    save_received_file,
    search_log,
    tail_log,
)
from bstkchat.crypto import (
    decrypt_message,
    derive_room_key,
    encrypt_payload,
    sanitize_room_id,
)

APP_VERSION = "2.4.3"
AUTO_AWAY_SECONDS = 300
TYPING_TTL_SECONDS = 5.0
TYPING_THROTTLE_SECONDS = 3.0

USER_COLORS = [
    "cyan",
    "magenta",
    "yellow",
    "green",
    "blue",
    "bright_red",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "bright_magenta",
    "bright_cyan",
]

THEME_STYLES = {
    "dark": {
        "prompt": "bold fg:ansicyan",
        "bottom-toolbar": "bg:#1e293b fg:#e2e8f0",
    },
    "light": {
        "prompt": "bold fg:ansiblue",
        "bottom-toolbar": "bg:#e2e8f0 fg:#0f172a",
    },
    "ansi": {
        "prompt": "bold fg:ansicyan",
        "bottom-toolbar": "reverse",
    },
}

COMMANDS = [
    ("/help", "Show available commands"),
    ("/room", "Switch room: /room <id> [password|--lock]"),
    ("/nick", "Change nickname: /nick <name>"),
    ("/msg", "Send private DM: /msg <user> <message>"),
    ("/send", "Send encrypted file: /send <filepath>"),
    ("/accept", "Accept pending file: /accept [id]"),
    ("/status", "Set status: /status <text>"),
    ("/who", "List online users in the room"),
    ("/users", "Alias for /who"),
    ("/invite", "Show shareable room join command"),
    ("/history", "Show recent local log lines: /history [N]"),
    ("/search", "Search local room log: /search <query>"),
    ("/chain", "Show decentralized room chain status & integrity"),
    ("/sync", "Sync missing room history from peers: /sync [N]"),
    ("/log", "Logging mode: /log [on|off|ephemeral]"),
    ("/mute", "Toggle audio bell alerts"),
    ("/compact", "Toggle compact single-line messages"),
    ("/multiline", "Toggle multi-line Enter behavior"),
    ("/theme", "Switch toolbar theme: /theme <dark|light|ansi>"),
    ("/web", "Open the local browser Web UI for this session"),
    ("/clear", "Clear the terminal screen"),
    ("/exit", "Disconnect and exit"),
    ("/quit", "Alias for /exit"),
]


def configure_stdio() -> None:
    """Ensures UTF-8 output on Windows consoles to prevent emoji/box-drawing errors."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def get_user_color(username: str) -> str:
    """Returns a consistent color based on the username."""
    idx = sum(ord(c) for c in username) % len(USER_COLORS)
    return USER_COLORS[idx]


def generate_room_id() -> str:
    """Generates a memorable phonetic room ID."""
    adjectives = [
        "neon", "cyber", "quantum", "cosmic", "shadow", "retro", "hyper", "solar",
        "alpha", "omega", "silent", "golden", "amber", "arctic", "stellar", "spectral",
    ]
    nouns = [
        "tiger", "falcon", "phoenix", "matrix", "glitch", "nebula", "vortex", "hacker",
        "ranger", "comet", "beacon", "nomad", "pulse", "orbit", "phantom", "echo",
    ]
    number = random.randint(10, 99)
    return f"{random.choice(adjectives)}-{random.choice(nouns)}-{number}"


def parse_command_args(raw_input: str) -> List[str]:
    """Parses slash command arguments, supporting quoted strings with fallback."""
    try:
        return shlex.split(raw_input)
    except ValueError:
        return raw_input.split()


def is_user_mentioned(text: str, nickname: str) -> bool:
    """Returns True if nickname is explicitly mentioned as @nick or a whole word."""
    if not nickname:
        return False
    pattern = rf"(?:^|[\s@]){re.escape(nickname)}\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


class ChatCompleter(Completer):
    """Context-aware autocomplete for slash commands, options, and @mentions."""

    def __init__(self, chat_client: "ChatClient"):
        self.chat = chat_client

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        if text.startswith("/"):
            parts = text.split(" ")
            if len(parts) == 1:
                prefix = parts[0].lower()
                for cmd, desc in COMMANDS:
                    if cmd.startswith(prefix):
                        yield Completion(
                            cmd,
                            start_position=-len(parts[0]),
                            display_meta=desc,
                        )
                return

            cmd = parts[0].lower()
            if cmd == "/msg" and len(parts) == 2:
                prefix = parts[1].lower()
                for peer in self.chat.get_peer_names():
                    if peer.lower().startswith(prefix):
                        yield Completion(
                            peer,
                            start_position=-len(parts[1]),
                            display_meta="online peer",
                        )
                return

            if cmd == "/log" and len(parts) == 2:
                prefix = parts[1].lower()
                for opt in ("on", "off", "ephemeral"):
                    if opt.startswith(prefix):
                        yield Completion(opt, start_position=-len(parts[1]))
                return

            if cmd == "/theme" and len(parts) == 2:
                prefix = parts[1].lower()
                for opt in VALID_THEMES:
                    if opt.startswith(prefix):
                        yield Completion(opt, start_position=-len(parts[1]))
                return

            if cmd == "/accept" and len(parts) == 2:
                prefix = parts[1].lower()
                for fid in self.chat.get_pending_file_ids():
                    if fid.lower().startswith(prefix):
                        yield Completion(fid, start_position=-len(parts[1]))
                return

        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("@"):
            prefix = word[1:].lower()
            for peer in self.chat.get_peer_names():
                if peer.lower().startswith(prefix):
                    yield Completion(
                        f"@{peer}",
                        start_position=-len(word),
                        display_meta="mention",
                    )


class ChatClient:
    def __init__(
        self,
        nickname: str,
        room_id: str,
        password: str = "",
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_TLS_PORT,
        use_tls: bool = True,
        theme: str = "dark",
        compact: bool = False,
        logging_enabled: bool = True,
        store_chain: bool = True,
    ):
        self.nickname = nickname
        self.room_id = sanitize_room_id(room_id)
        self.password = password
        self.status = "Active"
        self.broker = broker
        self.port = port
        self.use_tls = use_tls
        self.theme = theme if theme in VALID_THEMES else "dark"
        self.compact_mode = compact
        self.multiline_mode = False
        self.muted = False
        self.ephemeral_log = False

        # Derive authenticated encryption key for the room
        self.secret_key = derive_room_key(self.room_id, self.password)

        # peer -> {"last_seen": timestamp, "status": status_text}
        self.peers: Dict[str, Dict[str, object]] = {}
        self.typing_peers: Dict[str, float] = {}
        self.pending_files: Dict[str, Dict[str, object]] = {}
        self.shared_files: Dict[str, Dict[str, object]] = {}
        self._file_seq = 0

        self.running = True
        self.connected = False
        self.logging_enabled = logging_enabled
        self._log_error_warned = False
        self._last_activity = time.time()
        self._last_typing_sent = 0.0
        self._auto_away_active = False

        self._lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._web_listeners: List[queue.Queue] = []
        self._recent_events: deque = deque(maxlen=150)

        self.console = Console(force_terminal=True)
        self.client: Optional[mqtt.Client] = None
        self.chain = RoomChain(self.room_id, enabled=store_chain)
        self._update_topics(self.room_id)

    def _update_topics(self, room_id: str) -> None:
        self.base_topic = f"bstkchat/rooms/{room_id}"
        self.msg_topic = f"{self.base_topic}/messages"
        self.presence_topic = f"{self.base_topic}/presence"
        self.sync_topic = f"{self.base_topic}/sync"

    def register_web_listener(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._web_listeners.append(q)
        return q

    def unregister_web_listener(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._web_listeners:
                self._web_listeners.remove(q)

    def _emit_event(self, event_type: str, data: Dict[str, Any], record_history: bool = False) -> None:
        event = {"event": event_type, "data": data}
        with self._lock:
            if record_history:
                self._recent_events.append(event)
            listeners = list(self._web_listeners)
        for q in listeners:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def get_web_state(self) -> Dict[str, Any]:
        now = time.time()
        valid, count = self.chain.verify_integrity()
        with self._lock:
            peers_list = [
                {"name": name, "status": str(info["status"]), "color": get_user_color(name)}
                for name, info in sorted(self.peers.items(), key=lambda x: x[0].lower())
            ]
            typers = [
                name
                for name, ts in self.typing_peers.items()
                if now - ts <= TYPING_TTL_SECONDS
            ]
            pending = [
                {
                    "id": fid,
                    "sender": str(info["sender"]),
                    "filename": str(info["filename"]),
                    "size": int(info["size"]),
                }
                for fid, info in self.pending_files.items()
            ]
            recent = list(self._recent_events)
            return {
                "version": APP_VERSION,
                "nickname": self.nickname,
                "status": self.status,
                "room_id": self.room_id,
                "e2ee": bool(self.password),
                "use_tls": self.use_tls,
                "connected": self.connected,
                "peers": peers_list,
                "typing": typers,
                "chain": {
                    "enabled": self.chain.enabled,
                    "valid": valid,
                    "count": count,
                    "filepath": self.chain.filepath,
                },
                "pending_files": pending,
                "recent_events": recent,
            }

    def record_activity(self) -> None:
        """Records local user activity and restores Active status if Auto-Away."""
        self._last_activity = time.time()
        if self._auto_away_active:
            self._auto_away_active = False
            self.status = "Active"
            self.send_presence("heartbeat")

    def notify_typing(self) -> None:
        """Throttled typing indicator broadcast on local buffer changes."""
        now = time.time()
        self.record_activity()
        if now - self._last_typing_sent >= TYPING_THROTTLE_SECONDS:
            self._last_typing_sent = now
            self.send_presence("typing")

    def get_peer_names(self) -> List[str]:
        with self._lock:
            return sorted(self.peers.keys(), key=str.lower)

    def get_pending_file_ids(self) -> List[str]:
        with self._lock:
            return list(self.pending_files.keys())

    def get_log_filepath(self) -> str:
        log_dir = os.path.expanduser("~/.bstkchat/logs")
        safe_room = sanitize_room_id(self.room_id)
        return os.path.join(log_dir, f"{safe_room}.log")

    def _ring_bell(self) -> None:
        if not self.muted:
            sys.stdout.write("\a")
            sys.stdout.flush()

    def safe_print(self, *renderables, **kwargs) -> None:
        """
        Atomically captures Rich output and prints via prompt_toolkit's ANSI engine
        under a lock so concurrent threads never interleave lines or corrupt prompts.
        """
        with self._print_lock:
            with self.console.capture() as capture:
                for item in renderables:
                    self.console.print(item, **kwargs)
            output = capture.get()
            if output:
                print_formatted_text(ANSI(output), end="")

    def clear_screen(self) -> None:
        """Clears the terminal cleanly without spawning a shell subprocess."""
        with self._print_lock:
            try:
                pt_clear()
            except Exception:
                sys.stdout.write("\x1b[2J\x1b[H")
                sys.stdout.flush()

    def write_to_log(self, text_to_log: str) -> None:
        """Saves chat logs locally to ~/.bstkchat/logs/ with sanitized filenames."""
        if not self.logging_enabled or self.ephemeral_log:
            return
        try:
            log_filepath = self.get_log_filepath()
            os.makedirs(os.path.dirname(log_filepath), exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_filepath, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {text_to_log}\n")
        except OSError as exc:
            if not self._log_error_warned:
                self._log_error_warned = True
                self.logging_enabled = False
                self.safe_print(
                    f"[bold yellow]⚠ Logging disabled (I/O error: {escape_markup(str(exc))})[/]"
                )

    def print_system(self, text: str, log_text: Optional[str] = None) -> None:
        """Prints a styled system/alert message."""
        self.safe_print(f"[bold dim yellow]⚡ System:[/] [dim white]{text}[/]")
        self.write_to_log(f"System: {log_text if log_text is not None else text}")

    def setup_mqtt(self) -> None:
        """Initializes and connects the MQTT client with TLS (optional) and Last Will."""
        try:
            from paho.mqtt.enums import CallbackAPIVersion

            self.client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        except (ImportError, AttributeError):
            self.client = mqtt.Client()

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect

        if self.use_tls:
            try:
                self.client.tls_set()
            except Exception as exc:
                self.safe_print(
                    f"[bold yellow]⚠ TLS setup warning: {escape_markup(str(exc))}[/]"
                )

        will_payload = json.dumps({
            "sender": self.nickname,
            "type": "leave",
            "status": self.status,
        })
        self.client.will_set(self.presence_topic, will_payload, qos=1)

        try:
            self.client.connect(self.broker, self.port, keepalive=60)
            self.client.loop_start()
        except Exception as exc:
            self.safe_print(
                f"[bold red]CRITICAL: Could not connect to broker at {escape_markup(self.broker)}:{self.port}[/]",
                f"[yellow]Error detail: {escape_markup(str(exc))}[/]",
            )
            sys.exit(1)

    def on_connect(self, client, userdata, flags, rc, *args, **kwargs) -> None:
        """Triggered when the client connects to the broker."""
        with self._lock:
            self.connected = True
            msg_topic = self.msg_topic
            presence_topic = self.presence_topic
            sync_topic = self.sync_topic
        self.client.subscribe(msg_topic)
        self.client.subscribe(presence_topic)
        self.client.subscribe(sync_topic)
        self.send_presence("join")
        t = threading.Timer(0.6, self.request_sync)
        t.daemon = True
        t.start()
        self._emit_event("state_update", self.get_web_state())

    def on_disconnect(self, client, userdata, rc, *args, **kwargs) -> None:
        """Handles disconnection events gracefully."""
        with self._lock:
            self.connected = False
        self._emit_event("state_update", self.get_web_state())
        if self.running:
            self.print_system("Connection lost. Reconnecting...")

    def on_message(self, client, userdata, msg) -> None:
        """Invoked when a message is received from MQTT."""
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(payload, dict):
            return

        sender = str(payload.get("sender", "Anonymous"))
        msg_type = str(payload.get("type", "message"))

        if sender == self.nickname:
            return

        with self._lock:
            presence_topic = self.presence_topic
            msg_topic = self.msg_topic
            sync_topic = self.sync_topic

        if msg.topic == presence_topic:
            self.handle_presence(sender, msg_type, payload)
        elif msg.topic == msg_topic:
            self.handle_chat_message(sender, msg_type, payload, msg.topic)
        elif msg.topic == sync_topic:
            self.handle_sync(sender, msg_type, payload, msg.topic)

    def handle_presence(self, sender: str, msg_type: str, payload: dict) -> None:
        """Processes peer presence events (joins, leaves, heartbeats, typing, renames)."""
        now = time.time()
        peer_status = str(payload.get("status", "Active"))
        safe_sender = escape_markup(sender)
        color = get_user_color(sender)

        if msg_type == "join":
            is_new = False
            with self._lock:
                if sender not in self.peers:
                    is_new = True
                self.peers[sender] = {"last_seen": now, "status": peer_status}
            if is_new:
                self.print_system(
                    f"🎨 [bold {color}]{safe_sender}[/] joined the room!",
                    log_text=f"{sender} joined the room.",
                )
                self.send_presence("heartbeat")
                self._emit_event("system", {"text": f"{sender} joined the room.", "time": datetime.now().strftime("%H:%M:%S")}, record_history=True)
                t = threading.Timer(0.4, self.request_sync)
                t.daemon = True
                t.start()
            self._emit_event("state_update", self.get_web_state())

        elif msg_type == "leave":
            removed = False
            with self._lock:
                self.typing_peers.pop(sender, None)
                if sender in self.peers:
                    del self.peers[sender]
                    removed = True
            if removed:
                self.print_system(
                    f"❌ [bold {color}]{safe_sender}[/] left the room.",
                    log_text=f"{sender} left the room.",
                )
                self._emit_event("system", {"text": f"{sender} left the room.", "time": datetime.now().strftime("%H:%M:%S")}, record_history=True)
            self._emit_event("state_update", self.get_web_state())

        elif msg_type == "heartbeat":
            with self._lock:
                self.peers[sender] = {"last_seen": now, "status": peer_status}
            self._emit_event("state_update", self.get_web_state())

        elif msg_type == "typing":
            with self._lock:
                self.peers[sender] = {"last_seen": now, "status": peer_status}
                self.typing_peers[sender] = now
            self._emit_event("state_update", self.get_web_state())

        elif msg_type == "rename":
            old_name = str(payload.get("old", sender))
            new_name = str(payload.get("new", sender))
            with self._lock:
                self.peers.pop(old_name, None)
                self.typing_peers.pop(old_name, None)
                self.peers[new_name] = {"last_seen": now, "status": peer_status}
            self.print_system(
                f"🔄 [bold {get_user_color(old_name)}]{escape_markup(old_name)}[/] "
                f"changed nickname to [bold {get_user_color(new_name)}]{escape_markup(new_name)}[/]",
                log_text=f"{old_name} changed nickname to {new_name}",
            )
            self._emit_event("system", {"text": f"{old_name} changed nickname to {new_name}", "time": datetime.now().strftime("%H:%M:%S")}, record_history=True)
            self._emit_event("state_update", self.get_web_state())

    def _render_incoming_text(
        self, timestamp: str, sender: str, text: str, is_mention: bool = False, is_dm: bool = False
    ) -> None:
        safe_sender = escape_markup(sender)
        color = get_user_color(sender)

        if is_dm:
            if self.compact_mode:
                self.safe_print(
                    f"[dim gray][{timestamp}][/] [bold magenta]🔒 DM from {safe_sender}:[/] {escape_markup(text)}"
                )
            else:
                self.safe_print(
                    f"[dim gray][{timestamp}][/] [bold magenta]🔒 DM from {safe_sender}[/]:",
                    Markdown(text),
                )
            return

        if is_mention:
            if self.compact_mode:
                self.safe_print(
                    f"🔔 [bold yellow][{timestamp}][/] [bold {color}]<{safe_sender}>[/] [bold]{escape_markup(text)}[/]"
                )
            else:
                self.safe_print(
                    f"🔔 [bold yellow]MENTION[/] [dim gray][{timestamp}][/] [bold {color}]<{safe_sender}>[/]:",
                    Markdown(f"**{text}**"),
                )
            return

        if self.compact_mode:
            self.safe_print(
                f"[dim gray][{timestamp}][/] [bold {color}]<{safe_sender}>[/] {escape_markup(text)}"
            )
        else:
            self.safe_print(
                f"[dim gray][{timestamp}][/] [bold {color}]<{safe_sender}>[/]:",
                Markdown(text),
            )

    def handle_chat_message(
        self, sender: str, msg_type: str, payload: dict, topic: str
    ) -> None:
        """Decrypts and renders incoming chat messages, DMs, and file offers."""
        timestamp = escape_markup(str(payload.get("time", datetime.now().strftime("%H:%M:%S"))))
        encrypted_text = str(payload.get("text", ""))
        safe_sender = escape_markup(sender)
        color = get_user_color(sender)

        with self._lock:
            self.typing_peers.pop(sender, None)
            key = self.secret_key

        ok, decrypted_text = decrypt_message(
            encrypted_text, key, aad=topic.encode("utf-8")
        )

        if not ok:
            self.safe_print(
                f"[dim gray][{timestamp}][/] [bold {color}]<{safe_sender}>[/] "
                f"[bold red]⚠ {escape_markup(decrypted_text)}[/]"
            )
            self.write_to_log(f"<{sender}>: {decrypted_text}")
            return

        if msg_type == "file":
            self._handle_incoming_file(sender, decrypted_text)
            return

        if msg_type == "dm":
            target = str(payload.get("target", ""))
            if target == self.nickname:
                self._render_incoming_text(timestamp, sender, decrypted_text, is_dm=True)
                self.write_to_log(f"DM from {sender}: {decrypted_text}")
                self._emit_event(
                    "message",
                    {
                        "sender": sender,
                        "color": color,
                        "text": decrypted_text,
                        "time": timestamp,
                        "dm": True,
                        "target": target,
                        "own": False,
                    },
                    record_history=True,
                )
                self._ring_bell()
            return

        mentioned = is_user_mentioned(decrypted_text, self.nickname)
        self._render_incoming_text(timestamp, sender, decrypted_text, is_mention=mentioned)
        if mentioned:
            self.write_to_log(f"Mentioned by {sender}: {decrypted_text}")
            self._ring_bell()
        else:
            self.write_to_log(f"<{sender}>: {decrypted_text}")

        self._emit_event(
            "message",
            {
                "sender": sender,
                "color": color,
                "text": decrypted_text,
                "time": timestamp,
                "mention": mentioned,
                "own": False,
            },
            record_history=True,
        )

        if msg_type == "message":
            msg_id = str(
                payload.get("id")
                or hashlib.sha256(f"{sender}:{timestamp}:{encrypted_text}".encode("utf-8")).hexdigest()
            )
            ts = float(payload.get("ts") or time.time())
            self.chain.append_message(msg_id, ts, timestamp, sender, "message", encrypted_text)
            self._emit_event("state_update", self.get_web_state())

    def request_sync(self, limit: int = 100) -> None:
        """Broadcasts a decentralized history sync request to online room peers."""
        if not self.client or not self.chain.enabled:
            return
        with self._lock:
            topic = self.sync_topic
        payload = {
            "sender": self.nickname,
            "type": "sync_request",
            "known_ids": self.chain.get_known_ids(),
            "limit": limit,
        }
        try:
            self.client.publish(topic, json.dumps(payload), qos=1)
        except Exception:
            pass

    def handle_sync(self, sender: str, msg_type: str, payload: dict, sync_topic: str) -> None:
        """Handles decentralized peer-to-peer room chain sync requests and responses."""
        if not self.chain.enabled:
            return

        if msg_type == "sync_request":
            raw_known = payload.get("known_ids", [])
            known_ids = {str(x) for x in raw_known} if isinstance(raw_known, list) else set()
            try:
                limit = max(1, min(500, int(payload.get("limit", 100))))
            except (TypeError, ValueError):
                limit = 100
            missing = self.chain.get_missing_blocks(known_ids, limit=limit)
            if missing and self.client:
                resp = {
                    "sender": self.nickname,
                    "type": "sync_response",
                    "target": sender,
                    "blocks": missing,
                }
                try:
                    self.client.publish(sync_topic, json.dumps(resp), qos=1)
                except Exception:
                    pass

        elif msg_type == "sync_response":
            if str(payload.get("target", "")) != self.nickname:
                return
            blocks = payload.get("blocks", [])
            if not isinstance(blocks, list):
                return
            added = self.chain.merge_blocks(blocks)
            if not added:
                return

            with self._lock:
                key = self.secret_key
                aad = self.msg_topic.encode("utf-8")

            self.print_system(
                f"🔗 Synced {len(added)} message(s) from [bold {get_user_color(sender)}]{escape_markup(sender)}[/].",
                log_text=f"Synced {len(added)} message(s) from {sender}",
            )
            for block in added:
                ok, text = decrypt_message(str(block.get("ciphertext", "")), key, aad=aad)
                if ok:
                    b_sender = str(block.get("sender", "Peer"))
                    b_time = str(block.get("time", "--:--:--"))
                    self._render_incoming_text(
                        escape_markup(b_time),
                        b_sender,
                        text,
                    )
                    self._emit_event(
                        "message",
                        {
                            "sender": b_sender,
                            "color": get_user_color(b_sender),
                            "text": text,
                            "time": b_time,
                            "synced": True,
                            "own": b_sender == self.nickname,
                        },
                        record_history=True,
                    )
            self._emit_event("state_update", self.get_web_state())

    def replay_recent_chain(self, limit: int = 25) -> None:
        """Displays recent decrypted blocks from the local room chain on join."""
        if not self.chain.enabled:
            return
        recent = self.chain.get_recent_blocks(limit=limit)
        if not recent:
            return
        with self._lock:
            key = self.secret_key
            aad = self.msg_topic.encode("utf-8")
        rendered = 0
        for block in recent:
            ok, text = decrypt_message(str(block.get("ciphertext", "")), key, aad=aad)
            if ok:
                if rendered == 0:
                    self.print_system(f"⛓️  Loaded {len(recent)} recent block(s) from local room chain:")
                b_sender = str(block.get("sender", "Peer"))
                b_time = str(block.get("time", "--:--:--"))
                self._render_incoming_text(
                    escape_markup(b_time),
                    b_sender,
                    text,
                )
                self._emit_event(
                    "message",
                    {
                        "sender": b_sender,
                        "color": get_user_color(b_sender),
                        "text": text,
                        "time": b_time,
                        "synced": True,
                        "own": b_sender == self.nickname,
                    },
                    record_history=True,
                )
                rendered += 1

    def _handle_incoming_file(self, sender: str, decrypted_json: str) -> None:
        try:
            file_obj = json.loads(decrypted_json)
            filename = str(file_obj["filename"])
            b64_data = str(file_obj["data"])
            expected_sha = str(file_obj.get("sha256", ""))
            raw_bytes = base64.b64decode(b64_data.encode("ascii"))
        except Exception:
            self.print_system(f"Received malformed file offer from {escape_markup(sender)}.")
            return

        if len(raw_bytes) > MAX_FILE_BYTES:
            self.print_system(
                f"Rejected oversized file from {escape_markup(sender)} ({len(raw_bytes)} bytes)."
            )
            return

        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        if expected_sha and actual_sha != expected_sha:
            self.print_system(
                f"Rejected file from {escape_markup(sender)}: checksum mismatch."
            )
            return

        with self._lock:
            self._file_seq += 1
            file_id = str(self._file_seq)
            file_entry = {
                "sender": sender,
                "filename": filename,
                "data": raw_bytes,
                "size": len(raw_bytes),
            }
            self.pending_files[file_id] = file_entry
            self.shared_files[file_id] = file_entry

        size_kb = len(raw_bytes) / 1024.0
        self.print_system(
            f"📎 [bold {get_user_color(sender)}]{escape_markup(sender)}[/] sent file "
            f"[bold white]{escape_markup(filename)}[/] ({size_kb:.1f} KB). "
            f"Type [bold cyan]/accept {file_id}[/] to save.",
            log_text=f"{sender} sent file {filename} ({len(raw_bytes)} bytes)",
        )
        self._emit_event(
            "file_offer",
            {
                "id": file_id,
                "sender": sender,
                "filename": filename,
                "size": len(raw_bytes),
                "own": False,
                "time": datetime.now().strftime("%H:%M:%S"),
            },
            record_history=True,
        )
        self._emit_event("state_update", self.get_web_state())
        self._ring_bell()

    def get_shared_file(self, file_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            return self.shared_files.get(file_id)

    def accept_file(self, file_id: Optional[str] = None, target_dir: str = "") -> Optional[str]:
        """Saves a pending encrypted file transfer to ~/Downloads/bstkchat."""
        with self._lock:
            if not self.pending_files:
                self.print_system("No pending files to accept.")
                return None
            if not file_id:
                file_id = list(self.pending_files.keys())[-1]
            entry = self.pending_files.pop(file_id, None)

        if not entry:
            self.print_system(f"File ID '{escape_markup(str(file_id))}' not found.")
            return None

        saved_path = save_received_file(
            str(entry["filename"]), bytes(entry["data"]), target_dir=target_dir
        )
        self.print_system(
            f"💾 Saved file to [bold green]{escape_markup(saved_path)}[/]",
            log_text=f"Saved file to {saved_path}",
        )
        return saved_path

    def send_file_bytes(self, filename: str, raw_bytes: bytes) -> bool:
        """Checksums, encrypts, and broadcasts raw file bytes up to MAX_FILE_BYTES."""
        if len(raw_bytes) > MAX_FILE_BYTES:
            self.print_system(
                f"File too large ({len(raw_bytes) // 1024} KB). Max allowed is {MAX_FILE_BYTES // 1024} KB."
            )
            return False

        clean_name = os.path.basename(filename.strip()) or "file.bin"
        payload_json = json.dumps({
            "filename": clean_name,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "data": base64.b64encode(raw_bytes).decode("ascii"),
        })
        if self._publish_encrypted("file", payload_json):
            with self._lock:
                self._file_seq += 1
                file_id = str(self._file_seq)
                self.shared_files[file_id] = {
                    "sender": self.nickname,
                    "filename": clean_name,
                    "data": raw_bytes,
                    "size": len(raw_bytes),
                }
            self.print_system(
                f"📤 Sent encrypted file [bold white]{escape_markup(clean_name)}[/] "
                f"({len(raw_bytes) / 1024:.1f} KB).",
                log_text=f"Sent file {clean_name}",
            )
            self._emit_event(
                "file_offer",
                {
                    "id": file_id,
                    "sender": self.nickname,
                    "filename": clean_name,
                    "size": len(raw_bytes),
                    "own": True,
                    "time": datetime.now().strftime("%H:%M:%S"),
                },
                record_history=True,
            )
            return True
        return False

    def send_file(self, filepath: str) -> bool:
        """Reads, checksums, encrypts, and broadcasts a file up to MAX_FILE_BYTES."""
        expanded = os.path.expanduser(filepath)
        if not os.path.isfile(expanded):
            self.print_system(f"File not found: {escape_markup(filepath)}")
            return False

        size = os.path.getsize(expanded)
        if size > MAX_FILE_BYTES:
            self.print_system(
                f"File too large ({size // 1024} KB). Max allowed is {MAX_FILE_BYTES // 1024} KB."
            )
            return False

        try:
            with open(expanded, "rb") as f:
                raw_bytes = f.read()
        except OSError as exc:
            self.print_system(f"Could not read file: {escape_markup(str(exc))}")
            return False

        return self.send_file_bytes(os.path.basename(expanded), raw_bytes)

    def send_presence(self, status_type: str, extra_data: Optional[dict] = None) -> None:
        """Broadcasts a presence payload."""
        if not self.client:
            return
        payload = {
            "sender": self.nickname,
            "type": status_type,
            "status": self.status,
        }
        if extra_data:
            payload.update(extra_data)

        with self._lock:
            topic = self.presence_topic

        try:
            self.client.publish(topic, json.dumps(payload), qos=1)
        except Exception:
            pass

    def _publish_encrypted(
        self, msg_type: str, text: str, extra: Optional[dict] = None
    ) -> bool:
        """Encrypts text with AES-256-GCM and publishes to the room's message topic."""
        if not self.client:
            return False

        with self._lock:
            key = self.secret_key
            topic = self.msg_topic

        now_ts = time.time()
        now_str = datetime.now().strftime("%H:%M:%S")
        msg_id = uuid.uuid4().hex
        encrypted_text = encrypt_payload(text, key, aad=topic.encode("utf-8"))
        payload = {
            "id": msg_id,
            "ts": now_ts,
            "sender": self.nickname,
            "type": msg_type,
            "text": encrypted_text,
            "time": now_str,
        }
        if extra:
            payload.update(extra)

        try:
            self.client.publish(topic, json.dumps(payload), qos=1)
            if msg_type == "message":
                self.chain.append_message(msg_id, now_ts, now_str, self.nickname, "message", encrypted_text)
            return True
        except Exception as exc:
            self.print_system(f"Error sending message: {escape_markup(str(exc))}")
            return False

    def send_chat_message(self, text: str) -> None:
        """Broadcasts an encrypted text chat message."""
        self.record_activity()
        if self._publish_encrypted("message", text):
            timestamp = datetime.now().strftime("%H:%M:%S")
            if self.compact_mode:
                self.safe_print(
                    f"[dim gray][{timestamp}][/] [bold green]<You>[/] {escape_markup(text)}"
                )
            else:
                self.safe_print(
                    f"[dim gray][{timestamp}][/] [bold green]<You>[/]:",
                    Markdown(text),
                )
            self.write_to_log(f"<You>: {text}")
            self._emit_event(
                "message",
                {
                    "sender": self.nickname,
                    "color": get_user_color(self.nickname),
                    "text": text,
                    "time": timestamp,
                    "own": True,
                },
                record_history=True,
            )
            self._emit_event("state_update", self.get_web_state())

    def send_private_message(self, target: str, text: str) -> None:
        """Sends an encrypted Direct Message to a specific user in the room."""
        self.record_activity()
        if self._publish_encrypted("dm", text, extra={"target": target}):
            timestamp = datetime.now().strftime("%H:%M:%S")
            if self.compact_mode:
                self.safe_print(
                    f"[dim gray][{timestamp}][/] [bold magenta]🔒 DM to {escape_markup(target)}:[/] {escape_markup(text)}"
                )
            else:
                self.safe_print(
                    f"[dim gray][{timestamp}][/] [bold magenta]🔒 DM to {escape_markup(target)}[/]:",
                    Markdown(text),
                )
            self.write_to_log(f"DM to {target}: {text}")
            self._emit_event(
                "message",
                {
                    "sender": self.nickname,
                    "color": get_user_color(self.nickname),
                    "text": text,
                    "time": timestamp,
                    "dm": True,
                    "target": target,
                    "own": True,
                },
                record_history=True,
            )

    def switch_room(self, new_room_id: str, new_password: str = "") -> None:
        """Gracefully disconnects from the current room and enters a new one."""
        sanitized_room = sanitize_room_id(new_room_id)
        if not new_room_id.strip():
            self.print_system("Invalid room ID.")
            return

        self.send_presence("leave")

        with self._lock:
            old_msg_topic = self.msg_topic
            old_presence_topic = self.presence_topic
            old_sync_topic = self.sync_topic

        if self.client:
            self.client.unsubscribe(old_msg_topic)
            self.client.unsubscribe(old_presence_topic)
            self.client.unsubscribe(old_sync_topic)

        new_key = derive_room_key(sanitized_room, new_password)
        new_chain = RoomChain(sanitized_room, enabled=self.chain.enabled)

        with self._lock:
            self.room_id = sanitized_room
            self.password = new_password
            self.secret_key = new_key
            self.chain = new_chain
            self._update_topics(sanitized_room)
            self.peers.clear()
            self.typing_peers.clear()
            self.pending_files.clear()
            self.shared_files.clear()
            new_msg_topic = self.msg_topic
            new_presence_topic = self.presence_topic
            new_sync_topic = self.sync_topic

        if self.client:
            self.client.subscribe(new_msg_topic)
            self.client.subscribe(new_presence_topic)
            self.client.subscribe(new_sync_topic)
        self.send_presence("join")

        self.clear_screen()
        self.print_room_banner()
        self.replay_recent_chain(25)
        t = threading.Timer(0.6, self.request_sync)
        t.daemon = True
        t.start()
        self._emit_event("state_update", self.get_web_state())

    def print_room_banner(self) -> None:
        """Displays a clean metadata panel for the current active room."""
        with self._lock:
            room_id = self.room_id
            has_password = bool(self.password)
            peer_count = len(self.peers)

        transport = "TLS" if self.use_tls else "TCP"
        banner_text = Text()
        banner_text.append("💬 Room: ", style="bold green")
        banner_text.append(f"{room_id}", style="bold white underline")
        banner_text.append(f"  ({peer_count + 1} online • {transport})\n", style="dim white")

        banner_text.append("👤 Nick: ", style="bold cyan")
        banner_text.append(f"{self.nickname} ", style="bold white")
        banner_text.append(f"({self.status})\n", style="italic dim yellow")

        if has_password:
            banner_text.append("🔐 Security: ", style="bold green")
            banner_text.append("AES-256-GCM E2EE (Password Protected)\n", style="bold bright_green")
        else:
            banner_text.append("🔓 Security: ", style="bold yellow")
            banner_text.append("Public Room (Use /room <id> --lock for E2EE)\n", style="dim white")

        banner_text.append("⌨️  Shortcuts: ", style="bold magenta")
        banner_text.append(
            "/ for commands • @ for mentions • Alt+Enter for newline • /help",
            style="italic dim white",
        )

        self.safe_print(
            Panel(
                banner_text,
                title=f"[bold cyan]BSTK Chat v{APP_VERSION}[/]",
                box=box.ROUNDED,
                border_style="blue",
                expand=False,
            ),
            "[bold dim yellow]⚡ System:[/] [dim white]Joined room. Markdown & syntax highlighting are active.[/]",
        )

    def render_bottom_toolbar(self) -> HTML:
        """Renders the live status bar at the bottom of the prompt."""
        now = time.time()
        with self._lock:
            room = self.room_id
            peers = len(self.peers)
            has_pw = bool(self.password)
            is_conn = self.connected
            active_typers = [
                name
                for name, ts in list(self.typing_peers.items())
                if now - ts <= TYPING_TTL_SECONDS
            ]

        conn_badge = (
            "<style fg='ansigreen'>● Connected</style>"
            if is_conn
            else "<style fg='ansiyellow'>● Reconnecting</style>"
        )
        e2ee_badge = (
            "<style fg='ansigreen'>🔒 E2EE</style>"
            if has_pw
            else "<style fg='ansiyellow'>🔓 Public</style>"
        )
        if self.ephemeral_log:
            log_badge = "📝 Ephemeral"
        elif self.logging_enabled:
            log_badge = "📝 Log: ON"
        else:
            log_badge = "📝 Log: OFF"

        extras = []
        if self.muted:
            extras.append("🔇 Muted")
        if self.multiline_mode:
            extras.append("↵ Multi-line")
        if active_typers:
            who = ", ".join(active_typers[:2])
            suffix = "…" if len(active_typers) > 2 else ""
            extras.append(f"✎ {who}{suffix} typing")

        extra_str = f" │ {' │ '.join(extras)}" if extras else ""
        return HTML(
            f" {conn_badge} │ <b>#{room}</b> │ {e2ee_badge} │ 👥 {peers + 1} online │ {log_badge}{extra_str} │ <i>/help</i> "
        )

    def start_background_tasks(self) -> None:
        """Runs the background heartbeat, peer pruning, and auto-away thread."""
        def run():
            while not self._stop_event.is_set():
                now = time.time()
                if (
                    not self._auto_away_active
                    and self.status == "Active"
                    and now - self._last_activity >= AUTO_AWAY_SECONDS
                ):
                    self._auto_away_active = True
                    self.status = "Auto-Away"

                self.send_presence("heartbeat")

                stale_peers = []
                with self._lock:
                    for name, info in list(self.peers.items()):
                        if now - float(info["last_seen"]) > 45:
                            stale_peers.append(name)
                            del self.peers[name]
                    for name, ts in list(self.typing_peers.items()):
                        if now - ts > TYPING_TTL_SECONDS:
                            del self.typing_peers[name]

                for peer in stale_peers:
                    self.print_system(
                        f"User [bold]{escape_markup(peer)}[/] timed out.",
                        log_text=f"User {peer} timed out.",
                    )

                self._stop_event.wait(15)

        t = threading.Thread(target=run, daemon=True)
        t.start()

    def show_users(self) -> None:
        """Renders a table of active users and status metadata in the room."""
        with self._lock:
            room_id = self.room_id
            peers_snapshot = list(self.peers.items())

        table = Table(
            title=f"Users in #{room_id}",
            title_style="bold magenta",
            box=box.ROUNDED,
            show_header=True,
        )
        table.add_column("Username", style="cyan")
        table.add_column("Status", style="yellow")
        table.add_column("Last Seen", style="green")

        table.add_row(f"{self.nickname} (You)", self.status, "Now")
        for peer, info in sorted(peers_snapshot, key=lambda x: x[0].lower()):
            last_seen_diff = int(time.time() - float(info["last_seen"]))
            seen_str = "Just now" if last_seen_diff < 5 else f"{last_seen_diff}s ago"
            table.add_row(peer, str(info["status"]), seen_str)

        self.safe_print(table)

    def close(self) -> None:
        """Gracefully tears down the client and connections."""
        self.running = False
        self._stop_event.set()
        self.send_presence("leave")
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
        if self.ephemeral_log:
            try:
                log_path = self.get_log_filepath()
                if os.path.exists(log_path):
                    os.remove(log_path)
            except OSError:
                pass


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bstkchat",
        description="Real-time E2EE terminal chat over MQTT.",
    )
    parser.add_argument("--nick", help="Nickname to use in the chat room")
    parser.add_argument("--room", help="Room ID to join")
    parser.add_argument("--password", help="Room password for AES-256-GCM E2EE")
    parser.add_argument("--broker", help=f"MQTT broker host (default: {DEFAULT_BROKER})")
    parser.add_argument("--port", type=int, help="MQTT broker port")
    parser.add_argument("--no-tls", action="store_true", help="Disable TLS (use plain TCP)")
    parser.add_argument("--theme", choices=VALID_THEMES, help="Toolbar theme")
    parser.add_argument("--compact", action="store_true", help="Use compact single-line messages")
    parser.add_argument("--no-log", action="store_true", help="Disable local session logging")
    parser.add_argument("--no-store", action="store_true", help="Disable decentralized room chain storage")
    parser.add_argument("--web", action="store_true", help="Launch the local browser Web UI")
    parser.add_argument("--web-port", type=int, default=0, help="Port for local Web UI (default: auto)")
    parser.add_argument("--version", action="version", version=f"bstkchat {APP_VERSION}")
    return parser


def build_key_bindings(chat: ChatClient) -> KeyBindings:
    """Supports Alt+Enter newline in single-line mode and Enter newline in /multiline."""
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        if chat.multiline_mode:
            event.current_buffer.insert_text("\n")
        else:
            event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _(event):
        if chat.multiline_mode:
            event.current_buffer.validate_and_handle()
        else:
            event.current_buffer.insert_text("\n")

    return kb


def show_welcome(console: Console) -> None:
    """Prints the startup banner safely without overflowing narrow terminals."""
    try:
        pt_clear()
    except Exception:
        pass

    ascii_banner = (
        " ██████╗ ███████╗████████╗██╗  ██╗ ██████╗██╗  ██╗ █████╗ ████████╗\n"
        " ██╔══██╗██╔════╝╚══██╔══╝██║ ██╔╝██╔════╝██║  ██║██╔══██╗╚══██╔══╝\n"
        " ██████╔╝███████╗   ██║   █████╔╝ ██║     ███████║███████║   ██║   \n"
        " ██╔══██╗╚════██║   ██║   ██╔═██╗ ██║     ██╔══██║██╔══██║   ██║   \n"
        " ██████╔╝███████║   ██║   ██║  ██╗╚██████╗██║  ██║██║  ██║   ██║   \n"
        " ╚═════╝ ╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   "
    )

    with console.capture() as capture:
        if console.width >= 72:
            console.print(f"[bold cyan]{ascii_banner}[/]")
        else:
            console.print("[bold cyan]═══ BSTK CHAT ═══[/]")
        console.print(
            Panel(
                f"[bold white]BSTK Terminal Chat v{APP_VERSION}[/]\n"
                "• AES-256-GCM End-to-End Encryption & TLS transport\n"
                "• Rich Markdown, Autocomplete, Multi-line & Themes\n"
                "• Encrypted File Sharing, DMs, @Mentions & Log Search",
                box=box.ROUNDED,
                border_style="cyan",
                expand=False,
            )
        )
    print_formatted_text(ANSI(capture.get()), end="")


def main(argv: Optional[List[str]] = None) -> None:
    configure_stdio()
    args = build_arg_parser().parse_args(argv)
    cfg = load_config()

    console = Console(force_terminal=True)
    if not (args.nick and args.room):
        show_welcome(console)
    setup_session = PromptSession()

    # Phase 1: Establish Identity
    if args.nick:
        nickname = args.nick.strip()
    else:
        default_nick = str(cfg.get("nickname", "")).strip()
        prompt_label = (
            f"👤 Choose your Nickname (Enter for {default_nick}): "
            if default_nick
            else "👤 Choose your Nickname (Enter for random): "
        )
        try:
            nick_input = setup_session.prompt(prompt_label)
        except (KeyboardInterrupt, EOFError):
            print_formatted_text("\nExit requested.")
            return
        nickname = nick_input.strip() or default_nick or f"User-{random.randint(100, 999)}"

    # Phase 2: Enter Room
    if args.room:
        room_id = args.room.strip()
    else:
        suggested_room = generate_room_id()
        try:
            room_input = setup_session.prompt(
                f"🔑 Room ID to join (Enter for {suggested_room}): "
            )
        except (KeyboardInterrupt, EOFError):
            print_formatted_text("\nExit requested.")
            return
        room_id = room_input.strip() or suggested_room

    # Phase 3: Setup E2EE Password
    if args.password is not None:
        password = args.password
    elif args.room and args.nick:
        password = ""
    else:
        try:
            password = setup_session.prompt(
                "🔐 Room Password for E2EE (Enter for public room): ", is_password=True
            )
        except (KeyboardInterrupt, EOFError):
            print_formatted_text("\nExit requested.")
            return

    use_tls = not args.no_tls if args.no_tls else bool(cfg.get("tls", True))
    broker = args.broker or str(cfg.get("broker", DEFAULT_BROKER))
    default_port = DEFAULT_TLS_PORT if use_tls else DEFAULT_PLAIN_PORT
    port = args.port or int(cfg.get("port", default_port))
    theme = args.theme or str(cfg.get("theme", "dark"))
    compact = bool(args.compact or cfg.get("compact", False))
    logging_enabled = not args.no_log

    cfg.update({"nickname": nickname, "theme": theme, "compact": compact})
    save_config(cfg)

    chat = ChatClient(
        nickname=nickname,
        room_id=room_id,
        password=password,
        broker=broker,
        port=port,
        use_tls=use_tls,
        theme=theme,
        compact=compact,
        logging_enabled=logging_enabled,
        store_chain=not args.no_store,
    )
    chat.setup_mqtt()
    chat.start_background_tasks()

    web_server = None
    if args.web:
        from bstkchat.web import WebServer

        chat.replay_recent_chain(25)
        web_server = WebServer(chat, port=args.web_port)
        url = web_server.start()
        chat.safe_print(
            Panel(
                f"[bold green]🌐 Web UI running at:[/]\n[bold cyan]{escape_markup(url)}[/]\n\n"
                "[dim white]Press Ctrl+C to stop the server and exit.[/]",
                title=f"[bold cyan]BSTK Chat Web UI v{APP_VERSION}[/]",
                box=box.ROUNDED,
                border_style="cyan",
                expand=False,
            )
        )
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            while chat.running:
                chat._stop_event.wait(1)
        except KeyboardInterrupt:
            pass
        finally:
            web_server.stop()
            chat.close()
        return

    chat.clear_screen()
    chat.print_room_banner()
    chat.replay_recent_chain(25)

    dynamic_style = DynamicStyle(
        lambda: PromptStyle.from_dict(THEME_STYLES.get(chat.theme, THEME_STYLES["dark"]))
    )
    chat_session = PromptSession(
        history=InMemoryHistory(),
        completer=ChatCompleter(chat),
        complete_while_typing=True,
        bottom_toolbar=chat.render_bottom_toolbar,
        style=dynamic_style,
        key_bindings=build_key_bindings(chat),
    )

    def _on_text_changed(_):
        if chat_session.default_buffer.text:
            chat.notify_typing()

    chat_session.default_buffer.on_text_changed += _on_text_changed

    with patch_stdout():
        while chat.running:
            try:
                user_msg = chat_session.prompt(f"{chat.nickname} > ")
                user_msg = user_msg.strip()
                if not user_msg:
                    continue

                chat.record_activity()

                if user_msg.startswith("/"):
                    parts = parse_command_args(user_msg)
                    cmd = parts[0].lower()

                    if cmd in ("/exit", "/quit"):
                        chat.print_system("Goodbye!")
                        break

                    elif cmd == "/room":
                        if len(parts) < 2:
                            chat.print_system("Usage: /room <room_id> [password|--lock]")
                        else:
                            new_room = parts[1]
                            if len(parts) > 2 and parts[2] in ("--lock", "-p"):
                                new_pass = chat_session.prompt(
                                    "🔐 Enter Room Password: ", is_password=True
                                )
                            else:
                                new_pass = parts[2] if len(parts) > 2 else ""
                            chat.switch_room(new_room, new_pass)

                    elif cmd == "/nick":
                        if len(parts) < 2:
                            chat.print_system("Usage: /nick <new_name>")
                        else:
                            new_nick = " ".join(parts[1:]).strip()
                            if not new_nick:
                                chat.print_system("Nickname cannot be empty.")
                            else:
                                old_nick = chat.nickname
                                chat.send_presence("rename", {"old": old_nick, "new": new_nick})
                                chat.nickname = new_nick
                                cfg["nickname"] = new_nick
                                save_config(cfg)
                                chat.print_system(
                                    f"Nickname updated to [bold]{escape_markup(new_nick)}[/]",
                                    log_text=f"Nickname updated to {new_nick}",
                                )

                    elif cmd == "/msg":
                        if len(parts) < 3:
                            chat.print_system("Usage: /msg <recipient_name> <message>")
                        else:
                            chat.send_private_message(parts[1], " ".join(parts[2:]))

                    elif cmd == "/send":
                        if len(parts) < 2:
                            chat.print_system("Usage: /send <filepath>")
                        else:
                            chat.send_file(" ".join(parts[1:]))

                    elif cmd == "/accept":
                        fid = parts[1] if len(parts) > 1 else None
                        chat.accept_file(fid)

                    elif cmd == "/status":
                        chat.status = "Active" if len(parts) < 2 else " ".join(parts[1:])
                        chat._auto_away_active = False
                        chat.send_presence("heartbeat")
                        chat.print_system(
                            f"Status changed to: [italic yellow]{escape_markup(chat.status)}[/]",
                            log_text=f"Status changed to: {chat.status}",
                        )

                    elif cmd in ("/who", "/users"):
                        chat.show_users()

                    elif cmd == "/invite":
                        tls_flag = "" if chat.use_tls else " --no-tls"
                        broker_flag = f" --broker {chat.broker}" if chat.broker != DEFAULT_BROKER else ""
                        invite_cmd = f"bstkchat --room {chat.room_id}{broker_flag}{tls_flag}"
                        note = " (share password separately)" if chat.password else ""
                        chat.print_system(
                            f"Invite command{note}: [bold cyan]{escape_markup(invite_cmd)}[/]",
                            log_text=f"Invite: {invite_cmd}",
                        )

                    elif cmd == "/history":
                        limit = 15
                        if len(parts) > 1 and parts[1].isdigit():
                            limit = max(1, min(100, int(parts[1])))
                        lines = tail_log(chat.get_log_filepath(), limit=limit)
                        if not lines:
                            chat.print_system("No local log history for this room yet.")
                        else:
                            chat.safe_print(
                                Panel(
                                    "\n".join(escape_markup(line) for line in lines),
                                    title=f"[bold cyan]Last {len(lines)} log entries[/]",
                                    box=box.ROUNDED,
                                    border_style="dim",
                                    expand=False,
                                )
                            )

                    elif cmd == "/search":
                        if len(parts) < 2:
                            chat.print_system("Usage: /search <query>")
                        else:
                            query = " ".join(parts[1:])
                            matches = search_log(chat.get_log_filepath(), query)
                            if not matches:
                                chat.print_system(f"No matches for '{escape_markup(query)}'.")
                            else:
                                chat.safe_print(
                                    Panel(
                                        "\n".join(escape_markup(m) for m in matches),
                                        title=f"[bold green]Search: {escape_markup(query)} ({len(matches)})[/]",
                                        box=box.ROUNDED,
                                        border_style="green",
                                        expand=False,
                                    )
                                )

                    elif cmd == "/chain":
                        if not chat.chain.enabled:
                            chat.print_system("Decentralized chain storage is disabled (--no-store).")
                        else:
                            valid, count = chat.chain.verify_integrity()
                            badge = "[bold green]✓ Valid[/]" if valid else "[bold red]✗ Tampered[/]"
                            chat.print_system(
                                f"Room chain #{escape_markup(chat.room_id)}: [bold]{count}[/] block(s) • "
                                f"Integrity: {badge} • Store: [italic]{escape_markup(chat.chain.filepath)}[/]"
                            )

                    elif cmd == "/sync":
                        limit = 100
                        if len(parts) > 1 and parts[1].isdigit():
                            limit = max(1, min(500, int(parts[1])))
                        chat.request_sync(limit=limit)
                        chat.print_system(f"Requested up to {limit} missing message(s) from online peers.")

                    elif cmd == "/log":
                        if len(parts) > 1 and parts[1].lower() in ("off", "disable"):
                            chat.logging_enabled = False
                            chat.ephemeral_log = False
                            chat.print_system("Session logging disabled.")
                        elif len(parts) > 1 and parts[1].lower() in ("on", "enable"):
                            chat.logging_enabled = True
                            chat.ephemeral_log = False
                            chat.print_system("Session logging enabled.")
                        elif len(parts) > 1 and parts[1].lower() == "ephemeral":
                            chat.ephemeral_log = True
                            chat.logging_enabled = False
                            chat.print_system("Ephemeral mode enabled (room log will be cleared on exit).")
                        else:
                            status_str = (
                                "EPHEMERAL"
                                if chat.ephemeral_log
                                else ("ENABLED" if chat.logging_enabled else "DISABLED")
                            )
                            chat.print_system(
                                f"Session logging is [bold]{status_str}[/]. "
                                f"Log file: [italic white]{escape_markup(chat.get_log_filepath())}[/]"
                            )

                    elif cmd == "/mute":
                        chat.muted = not chat.muted
                        state = "MUTED" if chat.muted else "UNMUTED"
                        chat.print_system(f"Audio alerts are now [bold]{state}[/].")

                    elif cmd == "/compact":
                        chat.compact_mode = not chat.compact_mode
                        cfg["compact"] = chat.compact_mode
                        save_config(cfg)
                        state = "ON" if chat.compact_mode else "OFF"
                        chat.print_system(f"Compact message mode is now [bold]{state}[/].")

                    elif cmd == "/multiline":
                        chat.multiline_mode = not chat.multiline_mode
                        if chat.multiline_mode:
                            chat.print_system("Multi-line mode ON (Enter = newline, Alt+Enter = send).")
                        else:
                            chat.print_system("Multi-line mode OFF (Enter = send, Alt+Enter = newline).")

                    elif cmd == "/theme":
                        if len(parts) < 2 or parts[1].lower() not in VALID_THEMES:
                            chat.print_system(f"Usage: /theme <{'|'.join(VALID_THEMES)}>")
                        else:
                            chat.theme = parts[1].lower()
                            cfg["theme"] = chat.theme
                            save_config(cfg)
                            chat.print_system(f"Toolbar theme set to [bold]{chat.theme}[/].")

                    elif cmd == "/web":
                        if web_server is None:
                            from bstkchat.web import WebServer

                            web_server = WebServer(chat, port=args.web_port)
                            url = web_server.start()
                        else:
                            url = web_server.url
                        chat.print_system(f"🌐 Web UI running at [bold cyan]{escape_markup(url)}[/]")
                        try:
                            webbrowser.open(url)
                        except Exception:
                            pass

                    elif cmd == "/clear":
                        chat.clear_screen()
                        chat.print_room_banner()

                    elif cmd == "/help":
                        table = Table(
                            title="Available Commands",
                            title_style="bold green",
                            box=box.ROUNDED,
                            show_header=True,
                        )
                        table.add_column("Command", style="cyan")
                        table.add_column("Description", style="white")
                        for cmd_name, desc in COMMANDS:
                            if cmd_name not in ("/users", "/quit"):
                                table.add_row(cmd_name, desc)
                        chat.safe_print(table)

                    else:
                        chat.print_system(
                            f"Unknown command: {escape_markup(cmd)}. Type [bold]/help[/] for commands."
                        )
                else:
                    chat.send_chat_message(user_msg)

            except KeyboardInterrupt:
                chat.print_system("Exiting chat context. Goodbye!")
                break
            except EOFError:
                break

    if web_server is not None:
        web_server.stop()
    chat.close()


if __name__ == "__main__":
    main()
