import json
import os
import random
import re
import shlex
import sys
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

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
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear as pt_clear
from prompt_toolkit.styles import Style as PromptStyle

from bstkchat.crypto import (
    decrypt_message,
    derive_room_key,
    encrypt_payload,
    sanitize_room_id,
)

# Global color pool for deterministic username coloring
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

COMMANDS = [
    ("/help", "Show available commands"),
    ("/room", "Switch room: /room <id> [password]"),
    ("/nick", "Change nickname: /nick <name>"),
    ("/msg", "Send private DM: /msg <user> <message>"),
    ("/status", "Set status: /status <text>"),
    ("/who", "List online users in the room"),
    ("/users", "Alias for /who"),
    ("/log", "Toggle or inspect logging: /log [on|off]"),
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
    """Context-aware autocomplete for slash commands, log options, and @mentions."""

    def __init__(self, chat_client: "ChatClient"):
        self.chat = chat_client

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        # Slash command completion at start of line
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
                for opt in ("on", "off"):
                    if opt.startswith(prefix):
                        yield Completion(opt, start_position=-len(parts[1]))
                return

        # @mention completion anywhere in regular messages
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
    def __init__(self, nickname: str, room_id: str, password: str = ""):
        self.nickname = nickname
        self.room_id = sanitize_room_id(room_id)
        self.password = password
        self.status = "Active"

        # Derive authenticated encryption key for the room
        self.secret_key = derive_room_key(self.room_id, self.password)

        # peer -> {"last_seen": timestamp, "status": status_text}
        self.peers: Dict[str, Dict[str, object]] = {}
        self.running = True
        self.connected = False
        self.logging_enabled = True
        self._log_error_warned = False

        self._lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._stop_event = threading.Event()

        # Force terminal capability so Rich generates ANSI codes inside captures
        self.console = Console(force_terminal=True)
        self.client: Optional[mqtt.Client] = None

        # Public broker
        self.broker = "broker.hivemq.com"
        self.port = 1883

        # Isolated topic namespace
        self._update_topics(self.room_id)

    def _update_topics(self, room_id: str) -> None:
        self.base_topic = f"bstkchat/rooms/{room_id}"
        self.msg_topic = f"{self.base_topic}/messages"
        self.presence_topic = f"{self.base_topic}/presence"

    def get_peer_names(self) -> List[str]:
        with self._lock:
            return sorted(self.peers.keys(), key=str.lower)

    def get_peer_count(self) -> int:
        with self._lock:
            return len(self.peers)

    def get_log_filepath(self) -> str:
        log_dir = os.path.expanduser("~/.bstkchat/logs")
        safe_room = sanitize_room_id(self.room_id)
        return os.path.join(log_dir, f"{safe_room}.log")

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
        if not self.logging_enabled:
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
        """Initializes and connects the MQTT client with Last Will presence."""
        try:
            from paho.mqtt.enums import CallbackAPIVersion

            self.client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        except (ImportError, AttributeError):
            self.client = mqtt.Client()

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect

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
                f"[bold red]CRITICAL: Could not connect to broker at {self.broker}:{self.port}[/]",
                f"[yellow]Error detail: {escape_markup(str(exc))}[/]",
            )
            sys.exit(1)

    def on_connect(self, client, userdata, flags, rc, *args, **kwargs) -> None:
        """Triggered when the client connects to the broker."""
        with self._lock:
            self.connected = True
            msg_topic = self.msg_topic
            presence_topic = self.presence_topic
        self.client.subscribe(msg_topic)
        self.client.subscribe(presence_topic)
        self.send_presence("join")

    def on_disconnect(self, client, userdata, rc, *args, **kwargs) -> None:
        """Handles disconnection events gracefully."""
        with self._lock:
            self.connected = False
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

        # Ignore our own echoed traffic (sent messages, DMs, and renames render locally)
        if sender == self.nickname:
            return

        with self._lock:
            presence_topic = self.presence_topic
            msg_topic = self.msg_topic

        if msg.topic == presence_topic:
            self.handle_presence(sender, msg_type, payload)
        elif msg.topic == msg_topic:
            self.handle_chat_message(sender, msg_type, payload, msg.topic)

    def handle_presence(self, sender: str, msg_type: str, payload: dict) -> None:
        """Processes peer presence events (joins, leaves, heartbeats, renames)."""
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

        elif msg_type == "leave":
            removed = False
            with self._lock:
                if sender in self.peers:
                    del self.peers[sender]
                    removed = True
            if removed:
                self.print_system(
                    f"❌ [bold {color}]{safe_sender}[/] left the room.",
                    log_text=f"{sender} left the room.",
                )

        elif msg_type == "heartbeat":
            with self._lock:
                self.peers[sender] = {"last_seen": now, "status": peer_status}

        elif msg_type == "rename":
            old_name = str(payload.get("old", sender))
            new_name = str(payload.get("new", sender))
            with self._lock:
                self.peers.pop(old_name, None)
                self.peers[new_name] = {"last_seen": now, "status": peer_status}
            self.print_system(
                f"🔄 [bold {get_user_color(old_name)}]{escape_markup(old_name)}[/] "
                f"changed nickname to [bold {get_user_color(new_name)}]{escape_markup(new_name)}[/]",
                log_text=f"{old_name} changed nickname to {new_name}",
            )

    def handle_chat_message(
        self, sender: str, msg_type: str, payload: dict, topic: str
    ) -> None:
        """Decrypts and renders incoming chat messages and DMs."""
        timestamp = escape_markup(str(payload.get("time", datetime.now().strftime("%H:%M:%S"))))
        encrypted_text = str(payload.get("text", ""))
        safe_sender = escape_markup(sender)
        color = get_user_color(sender)

        with self._lock:
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

        if msg_type == "dm":
            target = str(payload.get("target", ""))
            if target == self.nickname:
                self.safe_print(
                    f"[dim gray][{timestamp}][/] [bold magenta]🔒 DM from {safe_sender}[/]:",
                    Markdown(decrypted_text),
                )
                self.write_to_log(f"DM from {sender}: {decrypted_text}")
                sys.stdout.write("\a")
                sys.stdout.flush()
            return

        if is_user_mentioned(decrypted_text, self.nickname):
            self.safe_print(
                f"🔔 [bold yellow]MENTION[/] [dim gray][{timestamp}][/] "
                f"[bold {color}]<{safe_sender}>[/]:",
                Markdown(f"**{decrypted_text}**"),
            )
            self.write_to_log(f"Mentioned by {sender}: {decrypted_text}")
            sys.stdout.write("\a")
            sys.stdout.flush()
        else:
            self.safe_print(
                f"[dim gray][{timestamp}][/] [bold {color}]<{safe_sender}>[/]:",
                Markdown(decrypted_text),
            )
            self.write_to_log(f"<{sender}>: {decrypted_text}")

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

        encrypted_text = encrypt_payload(text, key, aad=topic.encode("utf-8"))
        payload = {
            "sender": self.nickname,
            "type": msg_type,
            "text": encrypted_text,
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        if extra:
            payload.update(extra)

        try:
            self.client.publish(topic, json.dumps(payload), qos=1)
            return True
        except Exception as exc:
            self.print_system(f"Error sending message: {escape_markup(str(exc))}")
            return False

    def send_chat_message(self, text: str) -> None:
        """Broadcasts an encrypted text chat message."""
        if self._publish_encrypted("message", text):
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.safe_print(
                f"[dim gray][{timestamp}][/] [bold green]<You>[/]:",
                Markdown(text),
            )
            self.write_to_log(f"<You>: {text}")

    def send_private_message(self, target: str, text: str) -> None:
        """Sends an encrypted Direct Message to a specific user in the room."""
        if self._publish_encrypted("dm", text, extra={"target": target}):
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.safe_print(
                f"[dim gray][{timestamp}][/] [bold magenta]🔒 DM to {escape_markup(target)}[/]:",
                Markdown(text),
            )
            self.write_to_log(f"DM to {target}: {text}")

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

        if self.client:
            self.client.unsubscribe(old_msg_topic)
            self.client.unsubscribe(old_presence_topic)

        new_key = derive_room_key(sanitized_room, new_password)

        with self._lock:
            self.room_id = sanitized_room
            self.password = new_password
            self.secret_key = new_key
            self._update_topics(sanitized_room)
            self.peers.clear()
            new_msg_topic = self.msg_topic
            new_presence_topic = self.presence_topic

        if self.client:
            self.client.subscribe(new_msg_topic)
            self.client.subscribe(new_presence_topic)
        self.send_presence("join")

        self.clear_screen()
        self.print_room_banner()

    def print_room_banner(self) -> None:
        """Displays a clean metadata panel for the current active room."""
        with self._lock:
            room_id = self.room_id
            has_password = bool(self.password)
            peer_count = len(self.peers)

        banner_text = Text()
        banner_text.append("💬 Room: ", style="bold green")
        banner_text.append(f"{room_id}", style="bold white underline")
        banner_text.append(f"  ({peer_count + 1} online)\n", style="dim white")

        banner_text.append("👤 Nick: ", style="bold cyan")
        banner_text.append(f"{self.nickname} ", style="bold white")
        banner_text.append(f"({self.status})\n", style="italic dim yellow")

        if has_password:
            banner_text.append("🔐 Security: ", style="bold green")
            banner_text.append("AES-256-GCM E2EE (Password Protected)\n", style="bold bright_green")
        else:
            banner_text.append("🔓 Security: ", style="bold yellow")
            banner_text.append("Standard Room (No Password — use /room <id> <pass> for E2EE)\n", style="dim white")

        banner_text.append("⌨️  Tip: ", style="bold magenta")
        banner_text.append("Type / for command autocomplete, @ for mentions, or /help for all commands.", style="italic dim white")

        self.safe_print(
            Panel(banner_text, title="[bold cyan]BSTK Chat[/]", box=box.ROUNDED, border_style="blue", expand=False),
            "[bold dim yellow]⚡ System:[/] [dim white]Joined room. Markdown & syntax highlighting are active.[/]",
        )

    def render_bottom_toolbar(self) -> HTML:
        """Renders the live status bar at the bottom of the prompt."""
        with self._lock:
            room = self.room_id
            peers = len(self.peers)
            has_pw = bool(self.password)
            is_conn = self.connected

        conn_badge = "<style fg='ansigreen'>● Connected</style>" if is_conn else "<style fg='ansiyellow'>● Reconnecting</style>"
        e2ee_badge = "<style fg='ansigreen'>🔒 E2EE</style>" if has_pw else "<style fg='ansiyellow'>🔓 Public</style>"
        log_badge = "📝 Log: ON" if self.logging_enabled else "📝 Log: OFF"
        return HTML(
            f" {conn_badge} │ <b>#{room}</b> │ {e2ee_badge} │ 👥 {peers + 1} online │ {log_badge} │ <i>/help</i> "
        )

    def start_background_tasks(self) -> None:
        """Runs the background heartbeat and peer pruning thread."""
        def run():
            while not self._stop_event.is_set():
                self.send_presence("heartbeat")

                now = time.time()
                stale_peers = []
                with self._lock:
                    for name, info in list(self.peers.items()):
                        if now - float(info["last_seen"]) > 45:
                            stale_peers.append(name)
                            del self.peers[name]

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
                "[bold white]BSTK Terminal Chat v2.2.0[/]\n"
                "• AES-256-GCM End-to-End Encryption with room passwords\n"
                "• Rich Markdown, Syntax Highlighting & Autocomplete\n"
                "• Private DMs, @Mentions, Audio Alerts & Local Logs",
                box=box.ROUNDED,
                border_style="cyan",
                expand=False,
            )
        )
    print_formatted_text(ANSI(capture.get()), end="")


def main() -> None:
    configure_stdio()
    console = Console(force_terminal=True)
    show_welcome(console)
    setup_session = PromptSession()

    # Phase 1: Establish Identity
    try:
        nick_input = setup_session.prompt("👤 Choose your Nickname (Enter for random): ")
    except (KeyboardInterrupt, EOFError):
        print_formatted_text("\nExit requested.")
        return
    nickname = nick_input.strip() or f"User-{random.randint(100, 999)}"

    # Phase 2: Enter Room
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
    try:
        password = setup_session.prompt(
            "🔐 Room Password for E2EE (Enter for public room): ", is_password=True
        )
    except (KeyboardInterrupt, EOFError):
        print_formatted_text("\nExit requested.")
        return

    # Phase 4: Connect and Start
    chat = ChatClient(nickname, room_id, password)
    chat.setup_mqtt()
    chat.start_background_tasks()

    chat.clear_screen()
    chat.print_room_banner()

    prompt_style = PromptStyle.from_dict({
        "prompt": "bold fg:ansicyan",
        "bottom-toolbar": "bg:#1e293b fg:#e2e8f0",
    })
    chat_session = PromptSession(
        history=InMemoryHistory(),
        completer=ChatCompleter(chat),
        complete_while_typing=True,
        bottom_toolbar=chat.render_bottom_toolbar,
        style=prompt_style,
    )

    with patch_stdout():
        while chat.running:
            try:
                user_msg = chat_session.prompt(f"{chat.nickname} > ")
                user_msg = user_msg.strip()
                if not user_msg:
                    continue

                if user_msg.startswith("/"):
                    parts = parse_command_args(user_msg)
                    cmd = parts[0].lower()

                    if cmd in ("/exit", "/quit"):
                        chat.print_system("Goodbye!")
                        break

                    elif cmd == "/room":
                        if len(parts) < 2:
                            chat.print_system("Usage: /room <room_id> [optional_password]")
                        else:
                            new_room = parts[1]
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
                                chat.print_system(
                                    f"Nickname updated to [bold]{escape_markup(new_nick)}[/]",
                                    log_text=f"Nickname updated to {new_nick}",
                                )

                    elif cmd == "/msg":
                        if len(parts) < 3:
                            chat.print_system("Usage: /msg <recipient_name> <message>")
                        else:
                            recipient = parts[1]
                            dm_text = " ".join(parts[2:])
                            chat.send_private_message(recipient, dm_text)

                    elif cmd == "/status":
                        chat.status = "Active" if len(parts) < 2 else " ".join(parts[1:])
                        chat.send_presence("heartbeat")
                        chat.print_system(
                            f"Status changed to: [italic yellow]{escape_markup(chat.status)}[/]",
                            log_text=f"Status changed to: {chat.status}",
                        )

                    elif cmd in ("/who", "/users"):
                        chat.show_users()

                    elif cmd == "/log":
                        if len(parts) > 1 and parts[1].lower() in ("off", "disable"):
                            chat.logging_enabled = False
                            chat.print_system("Session logging disabled.")
                        elif len(parts) > 1 and parts[1].lower() in ("on", "enable"):
                            chat.logging_enabled = True
                            chat.print_system("Session logging enabled.")
                        else:
                            status_str = "ENABLED" if chat.logging_enabled else "DISABLED"
                            chat.print_system(
                                f"Session logging is [bold]{status_str}[/]. "
                                f"Log file: [italic white]{escape_markup(chat.get_log_filepath())}[/]"
                            )

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
                        table.add_row("/room <id> [pass]", "Switch rooms, optionally encrypting with password")
                        table.add_row("/nick <name>", "Change your active nickname")
                        table.add_row("/msg <user> <msg>", "Send an encrypted private DM to a user")
                        table.add_row("/status <text>", "Set a custom status message (e.g. AFK, Coding)")
                        table.add_row("/who", "List all online users and their statuses")
                        table.add_row("/log [on/off]", "Enable/disable session logging or view log path")
                        table.add_row("/clear", "Clear terminal screen")
                        table.add_row("/exit", "Disconnect and close the app")
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

    chat.close()


if __name__ == "__main__":
    main()
