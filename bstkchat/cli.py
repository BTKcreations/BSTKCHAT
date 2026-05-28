import sys
import json
import time
import random
import threading
import os
import base64
import hashlib
from datetime import datetime
import paho.mqtt.client as mqtt

# Styling and layout packages
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.markdown import Markdown

# Input and output handlers to prevent visual corruption
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style as PromptStyle

# Global color pool for deterministic username coloring
USER_COLORS = [
    "cyan", "magenta", "yellow", "green", "blue", 
    "bright_red", "bright_green", "bright_yellow", 
    "bright_blue", "bright_magenta", "bright_cyan"
]

def get_user_color(username: str) -> str:
    """Returns a consistent color based on the hash of the username."""
    idx = sum(ord(c) for c in username) % len(USER_COLORS)
    return USER_COLORS[idx]

def generate_room_id() -> str:
    """Generates a memorable and fun phonetic room ID."""
    adjectives = ["neon", "cyber", "quantum", "cosmic", "shadow", "retro", "hyper", "solar", "alpha", "omega", "silent", "golden", "amber", "arctic", "stellar", "spectral"]
    nouns = ["tiger", "falcon", "phoenix", "matrix", "glitch", "nebula", "vortex", "hacker", "ranger", "comet", "beacon", "nomad", "pulse", "orbit", "phantom", "echo"]
    number = random.randint(10, 99)
    return f"{random.choice(adjectives)}-{random.choice(nouns)}-{number}"

def encrypt_payload(data: str, key_string: str) -> str:
    """
    Symmetric stream cipher utilizing SHA-256 for key derivation and keystream generation.
    Completely pure-Python, avoiding external package compilation problems.
    """
    key_bytes = hashlib.sha256(key_string.encode('utf-8')).digest()
    data_bytes = data.encode('utf-8')
    out = bytearray()
    
    state = key_bytes
    for i, b in enumerate(data_bytes):
        if i % 32 == 0 and i > 0:
            state = hashlib.sha256(state).digest()
        keystream_byte = state[i % 32]
        out.append(b ^ keystream_byte)
        
    return base64.b64encode(out).decode('utf-8')

def decrypt_payload(encrypted_base64: str, key_string: str) -> str:
    """Decrypts symmetric payloads encrypted with encrypt_payload."""
    try:
        data_bytes = base64.b64decode(encrypted_base64.encode('utf-8'))
    except Exception:
        return "[Decryption Failed - Bad Payload]"
    
    key_bytes = hashlib.sha256(key_string.encode('utf-8')).digest()
    out = bytearray()
    state = key_bytes
    for i, b in enumerate(data_bytes):
        if i % 32 == 0 and i > 0:
            state = hashlib.sha256(state).digest()
        keystream_byte = state[i % 32]
        out.append(b ^ keystream_byte)
    try:
        return out.decode('utf-8')
    except UnicodeDecodeError:
        return "[Decryption Failed - Check Password]"

class ChatClient:
    def __init__(self, nickname: str, room_id: str, password: str = ""):
        self.nickname = nickname
        self.room_id = room_id.strip().lower()
        self.password = password
        self.status = "Active"
        
        # Derive cryptographic key for E2EE
        self.secret_key = f"{self.room_id}:{self.password}"
        
        # peer -> {"last_seen": timestamp, "status": status_text}
        self.peers = {}  
        self.running = True
        self.logging_enabled = True
        
        # Force terminal capability so rich generates raw ANSI codes inside captures
        self.console = Console(force_terminal=True)
        self.client = None
        
        # Public broker
        self.broker = "broker.hivemq.com"
        self.port = 1883
        
        # Isolated topic namespace
        self.base_topic = f"bstkchat/rooms/{self.room_id}"
        self.msg_topic = f"{self.base_topic}/messages"
        self.presence_topic = f"{self.base_topic}/presence"

    def safe_print(self, *args, **kwargs):
        """
        Captures Rich output and processes it through prompt_toolkit's ANSI engine.
        This prevents raw ANSI escapes (like ?[1m) from leaking into terminal outputs.
        """
        with self.console.capture() as capture:
            self.console.print(*args, **kwargs)
        print_formatted_text(ANSI(capture.get()), end="")

    def write_to_log(self, text_to_log: str):
        """Saves chat logs locally to ~/.bstkchat/logs/ directory."""
        if not self.logging_enabled:
            return
        try:
            log_dir = os.path.expanduser("~/.bstkchat/logs")
            os.makedirs(log_dir, exist_ok=True)
            log_filepath = os.path.join(log_dir, f"{self.room_id}.log")
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_filepath, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {text_to_log}\n")
        except Exception:
            pass

    def print_system(self, text: str):
        """Prints a styled system/alert message."""
        self.safe_print(f"[bold dim yellow]⚡ System:[/] [dim white]{text}[/]")
        self.write_to_log(f"System: {text}")

    def setup_mqtt(self):
        """Initializes and connects the MQTT client."""
        try:
            from paho.mqtt.enums import CallbackAPIVersion
            self.client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        except (ImportError, AttributeError):
            self.client = mqtt.Client()

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect

        try:
            self.client.connect(self.broker, self.port, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            self.safe_print(f"[bold red]CRITICAL: Could not connect to public broker at {self.broker}:{self.port}[/]")
            self.safe_print(f"[yellow]Error detail: {e}[/]")
            sys.exit(1)

    def on_connect(self, client, userdata, flags, rc, *args, **kwargs):
        """Triggered when the client connects to the broker."""
        self.client.subscribe(self.msg_topic)
        self.client.subscribe(self.presence_topic)
        self.send_presence("join")

    def on_disconnect(self, client, userdata, rc, *args, **kwargs):
        """Handles disconnection events gracefully."""
        if self.running:
            self.print_system("Connection lost. Retrying to connect...")

    def on_message(self, client, userdata, msg):
        """Invoked when a message is received from MQTT."""
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        sender = payload.get("sender", "Anonymous")
        msg_type = payload.get("type", "message")

        # Skip messages sent by ourselves (except rename and DM echoing)
        if sender == self.nickname and msg_type not in ["rename", "dm"]:
            return

        if msg.topic == self.presence_topic:
            self.handle_presence(sender, msg_type, payload)
        elif msg.topic == self.msg_topic:
            self.handle_chat_message(sender, msg_type, payload)

    def handle_presence(self, sender: str, msg_type: str, payload: dict):
        """Processes peer presence events (joins, leaves, heartbeats)."""
        now = time.time()
        peer_status = payload.get("status", "Active")

        if msg_type == "join":
            if sender not in self.peers:
                self.peers[sender] = {"last_seen": now, "status": peer_status}
                self.print_system(f"🎨 [bold {get_user_color(sender)}]{sender}[/] joined the room!")
                self.send_presence("heartbeat")
        elif msg_type == "leave":
            if sender in self.peers:
                del self.peers[sender]
                self.print_system(f"❌ [bold {get_user_color(sender)}]{sender}[/] left the room.")
        elif msg_type == "heartbeat":
            self.peers[sender] = {"last_seen": now, "status": peer_status}
        elif msg_type == "rename":
            old_name = payload.get("old", sender)
            new_name = payload.get("new", sender)
            if old_name in self.peers:
                del self.peers[old_name]
            self.peers[new_name] = {"last_seen": now, "status": peer_status}
            self.print_system(f"🔄 [bold {get_user_color(old_name)}]{old_name}[/] changed nickname to [bold {get_user_color(new_name)}]{new_name}[/]")

    def handle_chat_message(self, sender: str, msg_type: str, payload: dict):
        """Renders incoming chat messages beautifully."""
        timestamp = payload.get("time", datetime.now().strftime("%H:%M:%S"))
        encrypted_text = payload.get("text", "")
        
        # Attempt Decryption
        decrypted_text = decrypt_payload(encrypted_text, self.secret_key)
        
        # Handle Private Message (DM) Filtering
        if msg_type == "dm":
            target = payload.get("target", "")
            if target == self.nickname and sender != self.nickname:
                # Private DM received
                self.safe_print(f"[bold magenta]🔒 [DM from {sender}][/]:")
                self.safe_print(Markdown(decrypted_text))
                self.write_to_log(f"DM from {sender}: {decrypted_text}")
                # Play terminal alert
                sys.stdout.write("\a")
                sys.stdout.flush()
            elif sender == self.nickname:
                # Echo of DM sent by us
                self.safe_print(f"[bold magenta]🔒 [DM to {target}][/]:")
                self.safe_print(Markdown(decrypted_text))
                self.write_to_log(f"DM to {target}: {decrypted_text}")
            return

        # Direct Mention System Notification
        mention_tag = f"@{self.nickname}"
        is_mentioned = (mention_tag.lower() in decrypted_text.lower() or self.nickname.lower() in decrypted_text.lower()) and sender != self.nickname

        color = get_user_color(sender)
        
        if is_mentioned:
            # Render highlighted mention
            self.safe_print(f"🔔 [bold yellow]MENTIONED[/] [dim gray][{timestamp}][/] <[bold {color}]{sender}[/]>:")
            self.safe_print(Markdown(f"**{decrypted_text}**"))
            self.write_to_log(f"Mentioned by {sender}: {decrypted_text}")
            # Sound chime
            sys.stdout.write("\a")
            sys.stdout.flush()
        else:
            # Render standard markdown chat
            self.safe_print(f"[dim gray][{timestamp}][/] <[bold {color}]{sender}[/]>:")
            self.safe_print(Markdown(decrypted_text))
            self.write_to_log(f"<{sender}>: {decrypted_text}")

    def send_presence(self, status_type: str, extra_data: dict = None):
        """Broadcasts a presence payload."""
        if not self.client:
            return
        payload = {
            "sender": self.nickname, 
            "type": status_type,
            "status": self.status
        }
        if extra_data:
            payload.update(extra_data)
        
        try:
            self.client.publish(self.presence_topic, json.dumps(payload), qos=1)
        except Exception:
            pass

    def send_chat_message(self, text: str):
        """Broadcasts a standard text chat message."""
        if not self.client:
            return
        
        # Encrypt contents before shipping to public broker
        encrypted_text = encrypt_payload(text, self.secret_key)
        
        payload = {
            "sender": self.nickname,
            "type": "message",
            "text": encrypted_text,
            "time": datetime.now().strftime("%H:%M:%S")
        }
        try:
            self.client.publish(self.msg_topic, json.dumps(payload), qos=1)
        except Exception as e:
            self.print_system(f"Error sending message: {e}")

    def send_private_message(self, target: str, text: str):
        """Sends an encrypted Direct Message to a specific user in the room."""
        if not self.client:
            return
        
        encrypted_text = encrypt_payload(text, self.secret_key)
        payload = {
            "sender": self.nickname,
            "type": "dm",
            "target": target,
            "text": encrypted_text,
            "time": datetime.now().strftime("%H:%M:%S")
        }
        try:
            self.client.publish(self.msg_topic, json.dumps(payload), qos=1)
        except Exception as e:
            self.print_system(f"Error sending DM: {e}")

    def switch_room(self, new_room_id: str, new_password: str = ""):
        """Gracefully disconnects from the current room and enters a new one."""
        new_room_id = new_room_id.strip().lower()
        if not new_room_id:
            self.print_system("Invalid room ID.")
            return

        self.send_presence("leave")
        self.client.unsubscribe(self.msg_topic)
        self.client.unsubscribe(self.presence_topic)

        self.room_id = new_room_id
        self.password = new_password
        self.secret_key = f"{self.room_id}:{self.password}"
        
        self.base_topic = f"bstkchat/rooms/{self.room_id}"
        self.msg_topic = f"{self.base_topic}/messages"
        self.presence_topic = f"{self.base_topic}/presence"
        self.peers.clear()

        self.client.subscribe(self.msg_topic)
        self.client.subscribe(self.presence_topic)
        self.send_presence("join")

        os.system('cls' if os.name == 'nt' else 'clear')
        self.print_room_banner()

    def print_room_banner(self):
        """Displays beautiful dynamic metadata panel for the current active room."""
        banner_text = Text()
        banner_text.append("💬 Active Room: ", style="bold green")
        banner_text.append(f"{self.room_id}\n", style="bold white underline")
        banner_text.append("👤 Nickname: ", style="bold cyan")
        banner_text.append(f"{self.nickname} ", style="bold white")
        banner_text.append(f"({self.status})\n", style="italic dim yellow")
        
        if self.password:
            banner_text.append("🔐 Encryption: ", style="bold green")
            banner_text.append("On (E2EE Protected)\n", style="bold bright_green")
        else:
            banner_text.append("🔓 Encryption: ", style="bold red")
            banner_text.append("Standard (No Password)\n", style="dim white")

        banner_text.append("ℹ️  Commands: ", style="bold magenta")
        banner_text.append("/room <id> [pass], /nick <name>, /msg <user> <text>, /status <text>, /who, /log, /clear, /exit", style="italic gray")
        
        self.safe_print(Panel(banner_text, border_style="blue", expand=False))
        self.print_system(f"Successfully joined room. Type markdown or slash commands!")

    def start_background_tasks(self):
        """Runs the background heartbeat and peer pruning thread."""
        def run():
            while self.running:
                self.send_presence("heartbeat")
                
                # Prune peers who missed heartbeats (older than 45 seconds)
                now = time.time()
                stale_peers = [name for name, info in self.peers.items() if now - info["last_seen"] > 45]
                for p in stale_peers:
                    del self.peers[p]
                    self.print_system(f"User [bold]{p}[/] has disconnected (timeout).")
                
                time.sleep(15)

        t = threading.Thread(target=run, daemon=True)
        t.start()

    def show_users(self):
        """Renders an interactive list of active users and status metadata in the room."""
        table = Table(title=f"Users in {self.room_id}", title_style="bold underline magenta", show_header=True)
        table.add_column("Username", style="cyan")
        table.add_column("Status Message", style="yellow")
        table.add_column("Last Seen", style="green")

        table.add_row(f"{self.nickname} (You)", self.status, "Now")
        for peer, info in self.peers.items():
            last_seen_diff = int(time.time() - info["last_seen"])
            seen_str = "Just now" if last_seen_diff < 5 else f"{last_seen_diff}s ago"
            table.add_row(peer, info["status"], seen_str)

        self.safe_print(table)

    def close(self):
        """Gracefully tears down the client and connections."""
        self.running = False
        self.send_presence("leave")
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

def show_welcome(console: Console):
    """Prints a magnificent terminal start banner safely."""
    os.system('cls' if os.name == 'nt' else 'clear')
    ascii_banner = r"""
 ██████╗  ███████╗████████╗██╗  ██╗ ██████╗██╗  ██╗ █████╗ ████████╗
 ██╔══██╗ ██╔════╝╚══██╔══╝██║ ██╔╝██╔════╝██║  ██║██╔══██╗╚══██╔══╝
 ██████╔╝ ███████╗   ██║   █████╔╝ ██║     ███████║███████║   ██║   
 ██╔══██╗ ╚════██║   ██║   ██╔═██╗ ██║     ██╔══██║██╔══██║   ██║   
 ██████╔╝ ███████║   ██║   ██║  ██╗╚██████╗██║  ██║██║  ██║   ██║   
 ╚══════╝  ╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   
    """
    
    with console.capture() as capture:
        console.print(f"[bold cyan]{ascii_banner}[/]")
        console.print(Panel(
            "[bold white]Welcome to BSTK Terminal Chat v1.1.0 (PRO Edition)![/]\n"
            "• End-to-End Encryption (E2EE) with room keys\n"
            "• Built-in Markdown & Syntax Highlighting\n"
            "• Private Messaging, Mentions & Audio Alerts\n"
            "• Session Logging & Customizable Custom Statuses",
            border_style="cyan",
            expand=False
        ))
    print_formatted_text(ANSI(capture.get()), end="")

def main():
    console = Console(force_terminal=True)
    show_welcome(console)
    session = PromptSession()

    # Phase 1: Establish Identity
    while True:
        try:
            nick_input = session.prompt("👤 Choose your Nickname (Default: User): ")
        except (KeyboardInterrupt, EOFError):
            print_formatted_text("\nExit requested.")
            return
        
        nickname = nick_input.strip() or f"User-{random.randint(100, 999)}"
        if nickname:
            break

    # Phase 2: Enter Room
    suggested_room = generate_room_id()
    try:
        room_input = session.prompt(f"🔑 Enter Room ID to Join (Press Enter for: {suggested_room}): ")
    except (KeyboardInterrupt, EOFError):
        print_formatted_text("\nExit requested.")
        return
        
    room_id = room_input.strip().lower() or suggested_room

    # Phase 3: Setup E2EE Password
    try:
        password = session.prompt("🔐 Optional Room Password (for E2EE, press Enter for None): ", is_password=True)
    except (KeyboardInterrupt, EOFError):
        print_formatted_text("\nExit requested.")
        return

    # Phase 4: Connect and Start
    chat = ChatClient(nickname, room_id, password)
    chat.setup_mqtt()
    chat.start_background_tasks()
    
    os.system('cls' if os.name == 'nt' else 'clear')
    chat.print_room_banner()

    # Style input prompts gracefully
    prompt_style = PromptStyle.from_dict({
        'prompt': 'bold fg:ansicyan',
    })

    # Thread-safe terminal input loop
    with patch_stdout():
        while chat.running:
            try:
                user_msg = session.prompt(f"{chat.nickname} > ", style=prompt_style)
                user_msg = user_msg.strip()
                if not user_msg:
                    continue

                # Command Parser
                if user_msg.startswith("/"):
                    parts = user_msg.split()
                    cmd = parts[0].lower()
                    
                    if cmd in ["/exit", "/quit"]:
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
                            new_nick = " ".join(parts[1:])
                            chat.send_presence("rename", {"old": chat.nickname, "new": new_nick})
                            chat.nickname = new_nick
                            chat.print_system(f"Nickname updated to [bold]{new_nick}[/]")
                    
                    elif cmd == "/msg":
                        if len(parts) < 3:
                            chat.print_system("Usage: /msg <recipient_name> <message>")
                        else:
                            recipient = parts[1]
                            dm_text = " ".join(parts[2:])
                            chat.send_private_message(recipient, dm_text)
                    
                    elif cmd == "/status":
                        if len(parts) < 2:
                            chat.status = "Active"
                        else:
                            chat.status = " ".join(parts[1:])
                        chat.send_presence("heartbeat")
                        chat.print_system(f"Status changed to: [italic yellow]{chat.status}[/]")
                    
                    elif cmd in ["/who", "/users"]:
                        chat.show_users()
                    
                    elif cmd == "/log":
                        if len(parts) > 1 and parts[1].lower() in ["off", "disable"]:
                            chat.logging_enabled = False
                            chat.print_system("Session logging disabled.")
                        elif len(parts) > 1 and parts[1].lower() in ["on", "enable"]:
                            chat.logging_enabled = True
                            chat.print_system("Session logging enabled.")
                        else:
                            log_path = os.path.expanduser(f"~/.bstkchat/logs/{chat.room_id}.log")
                            status_str = "ENABLED" if chat.logging_enabled else "DISABLED"
                            chat.print_system(f"Session logging is [bold]{status_str}[/]. Log file: [italic white]{log_path}[/]")
                    
                    elif cmd == "/clear":
                        os.system('cls' if os.name == 'nt' else 'clear')
                        chat.print_room_banner()
                    
                    elif cmd == "/help":
                        table = Table(title="Available Commands", title_style="bold green", show_header=True)
                        table.add_column("Command", style="cyan")
                        table.add_column("Description", style="white")
                        table.add_row("/room <id> [pass]", "Switch rooms instantly, optionally encrypting with password")
                        table.add_row("/nick <name>", "Change your active nickname")
                        table.add_row("/msg <user> <msg>", "Send an encrypted private message (DM) to a user")
                        table.add_row("/status <text>", "Set a customizable status message (e.g. AFK, Writing)")
                        table.add_row("/who", "List all users and their status messages")
                        table.add_row("/log [on/off]", "Enable/Disable session logging or view log file path")
                        table.add_row("/clear", "Clear terminal screen")
                        table.add_row("/exit", "Disconnect and close the app")
                        chat.safe_print(table)
                    
                    else:
                        chat.print_system(f"Unknown command: {cmd}. Type [bold]/help[/] for a list of commands.")
                else:
                    # Regular text message broadcast (encrypted)
                    chat.send_chat_message(user_msg)
                    # Render our own sent message instantly
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    chat.safe_print(f"[dim gray][{timestamp}][/] <[bold green]You[/]>:")
                    chat.safe_print(Markdown(user_msg))
                    chat.write_to_log(f"<You>: {user_msg}")

            except KeyboardInterrupt:
                chat.print_system("\nExiting chat context. Goodbye!")
                break
            except EOFError:
                break

    chat.close()

if __name__ == "__main__":
    main()