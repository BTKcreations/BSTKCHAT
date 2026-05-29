import sys
import time
import random
import threading
import os
from datetime import datetime

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style as PromptStyle
from rich.markdown import Markdown

try:
    from bstkchat.ui import UIManager, show_welcome, generate_room_id, get_user_color
    from bstkchat.network import MQTTClientWrapper
    from bstkchat.crypto import derive_room_key, encrypt_payload, decrypt_payload, DmCryptoManager
except ImportError:
    from ui import UIManager, show_welcome, generate_room_id, get_user_color
    from network import MQTTClientWrapper
    from crypto import derive_room_key, encrypt_payload, decrypt_payload, DmCryptoManager

class ChatClient:
    def __init__(self, nickname: str, room_id: str, password: str = ""):
        self.nickname = nickname
        self.room_id = room_id.strip().lower()
        self.password = password
        self.status = "Active"
        
        self.running = True
        self.logging_enabled = True
        
        self.ui = UIManager()
        self.dm_crypto = DmCryptoManager()
        
        self.broker = "broker.hivemq.com"
        self.port = 1883
        
        self.peers = {}

        self._setup_room_keys()
        self.mqtt = None

    def _setup_room_keys(self):
        if self.password:
            self.room_key = derive_room_key(self.room_id, self.password)
        else:
            self.room_key = derive_room_key(self.room_id, "")

        self.base_topic = f"bstkchat/rooms/{self.room_id}"
        self.msg_topic = f"{self.base_topic}/messages"
        self.presence_topic = f"{self.base_topic}/presence"

    def write_to_log(self, text_to_log: str):
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

    def setup_mqtt(self):
        self.mqtt = MQTTClientWrapper(self.broker, self.port)
        self.mqtt.on_connect_callback = self.on_connect
        self.mqtt.on_disconnect_callback = self.on_disconnect
        self.mqtt.on_message_callback = self.on_message

        try:
            self.mqtt.connect()
        except Exception as e:
            self.ui.safe_print(f"[bold red]CRITICAL: Could not connect to broker at {self.broker}:{self.port}[/]")
            self.ui.safe_print(f"[yellow]Error detail: {e}[/]")
            raise

    def on_connect(self):
        self.mqtt.subscribe(self.msg_topic)
        self.mqtt.subscribe(self.presence_topic)
        self.send_presence("join")

    def on_disconnect(self):
        if self.running:
            self.ui.print_system("Connection lost. Retrying to connect...")

    def on_message(self, topic: str, payload: dict):
        sender = payload.get("sender", "Anonymous")
        msg_type = payload.get("type", "message")

        if sender == self.nickname and msg_type not in ["rename"]:
            return

        if topic == self.presence_topic:
            self.handle_presence(sender, msg_type, payload)
        elif topic == self.msg_topic:
            self.handle_chat_message(sender, msg_type, payload)

    def handle_presence(self, sender: str, msg_type: str, payload: dict):
        now = time.time()
        peer_status = payload.get("status", "Active")
        pub_key = payload.get("pub_key", "")

        if msg_type == "join":
            if sender not in self.peers:
                self.peers[sender] = {"last_seen": now, "status": peer_status}
                self.dm_crypto.add_peer_public_key(sender, pub_key)
                self.ui.print_system(f"🎨 [bold {get_user_color(sender)}]{sender}[/] joined the room!")
                self.send_presence("heartbeat")
        elif msg_type == "leave":
            if sender in self.peers:
                del self.peers[sender]
                self.ui.print_system(f"❌ [bold {get_user_color(sender)}]{sender}[/] left the room.")
        elif msg_type == "heartbeat":
            self.peers[sender] = {"last_seen": now, "status": peer_status}
            self.dm_crypto.add_peer_public_key(sender, pub_key)
        elif msg_type == "rename":
            old_name = payload.get("old", sender)
            new_name = payload.get("new", sender)
            if old_name in self.peers:
                del self.peers[old_name]
            self.peers[new_name] = {"last_seen": now, "status": peer_status}
            self.dm_crypto.add_peer_public_key(new_name, pub_key)
            self.ui.print_system(f"🔄 [bold {get_user_color(old_name)}]{old_name}[/] changed nickname to [bold {get_user_color(new_name)}]{new_name}[/]")

    def handle_chat_message(self, sender: str, msg_type: str, payload: dict):
        timestamp = payload.get("time", datetime.now().strftime("%H:%M:%S"))
        encrypted_text = payload.get("text", "")
        
        if msg_type == "dm":
            target = payload.get("target", "")
            if target == self.nickname and sender != self.nickname:
                decrypted_text = self.dm_crypto.decrypt_dm(sender, encrypted_text)
                self.ui.safe_print(f"[bold magenta]🔒 [DM from {sender}][/]:")
                self.ui.safe_print(Markdown(decrypted_text))
                self.write_to_log(f"DM from {sender}: {decrypted_text}")
                sys.stdout.write("\a")
                sys.stdout.flush()
            return

        decrypted_text = decrypt_payload(encrypted_text, self.room_key)

        mention_tag = f"@{self.nickname}"
        is_mentioned = (mention_tag.lower() in decrypted_text.lower() or self.nickname.lower() in decrypted_text.lower()) and sender != self.nickname
        color = get_user_color(sender)
        
        if is_mentioned:
            self.ui.safe_print(f"🔔 [bold yellow]MENTIONED[/] [dim gray][{timestamp}][/] <[bold {color}]{sender}[/]>:")
            self.ui.safe_print(Markdown(f"**{decrypted_text}**"))
            self.write_to_log(f"Mentioned by {sender}: {decrypted_text}")
            sys.stdout.write("\a")
            sys.stdout.flush()
        else:
            self.ui.safe_print(f"[dim gray][{timestamp}][/] <[bold {color}]{sender}[/]>:")
            self.ui.safe_print(Markdown(decrypted_text))
            self.write_to_log(f"<{sender}>: {decrypted_text}")

    def send_presence(self, status_type: str, extra_data: dict = None):
        if not self.mqtt:
            return
        payload = {
            "sender": self.nickname, 
            "type": status_type,
            "status": self.status,
            "pub_key": self.dm_crypto.get_my_public_key_b64()
        }
        if extra_data:
            payload.update(extra_data)
        self.mqtt.publish(self.presence_topic, payload)

    def send_chat_message(self, text: str):
        if not self.mqtt:
            return
        encrypted_text = encrypt_payload(text, self.room_key)
        payload = {
            "sender": self.nickname,
            "type": "message",
            "text": encrypted_text,
            "time": datetime.now().strftime("%H:%M:%S")
        }
        self.mqtt.publish(self.msg_topic, payload)

    def send_private_message(self, target: str, text: str):
        if not self.mqtt:
            return
        try:
            encrypted_text = self.dm_crypto.encrypt_dm(target, text)
        except ValueError as e:
            self.ui.print_system(f"Cannot send DM: {e}")
            return

        payload = {
            "sender": self.nickname,
            "type": "dm",
            "target": target,
            "text": encrypted_text,
            "time": datetime.now().strftime("%H:%M:%S")
        }
        self.mqtt.publish(self.msg_topic, payload)

        # Echo to ourselves
        self.ui.safe_print(f"[bold magenta]🔒 [DM to {target}][/]:")
        self.ui.safe_print(Markdown(text))
        self.write_to_log(f"DM to {target}: {text}")

    def switch_room(self, new_room_id: str, new_password: str = ""):
        new_room_id = new_room_id.strip().lower()
        if not new_room_id:
            self.ui.print_system("Invalid room ID.")
            return

        self.send_presence("leave")
        self.mqtt.unsubscribe(self.msg_topic)
        self.mqtt.unsubscribe(self.presence_topic)

        self.room_id = new_room_id
        self.password = new_password
        self._setup_room_keys()
        
        # update topics
        self.base_topic = f"bstkchat/rooms/{self.room_id}"
        self.msg_topic = f"{self.base_topic}/messages"
        self.presence_topic = f"{self.base_topic}/presence"

        self.peers.clear()

        self.mqtt.subscribe(self.msg_topic)
        self.mqtt.subscribe(self.presence_topic)
        self.send_presence("join")

        os.system('cls' if os.name == 'nt' else 'clear')
        self.ui.print_room_banner(self.room_id, self.nickname, self.status, bool(self.password))

    def change_broker(self, new_broker: str, new_port: int):
        old_broker = self.broker
        old_port = self.port
        self.ui.print_system(f"Switching broker to {new_broker}:{new_port}...")
        self.send_presence("leave")
        self.mqtt.disconnect()
        self.broker = new_broker
        self.port = new_port
        try:
            self.setup_mqtt()
        except Exception:
            self.ui.print_system(f"Failed to connect to {new_broker}:{new_port}. Reverting...")
            self.broker = old_broker
            self.port = old_port
            self.setup_mqtt()

    def start_background_tasks(self):
        def run():
            while self.running:
                self.send_presence("heartbeat")
                now = time.time()
                stale_peers = [name for name, info in self.peers.items() if now - info["last_seen"] > 45]
                for p in stale_peers:
                    del self.peers[p]
                    self.ui.print_system(f"User [bold]{p}[/] has disconnected (timeout).")
                time.sleep(15)

        t = threading.Thread(target=run, daemon=True)
        t.start()

    def close(self):
        self.running = False
        self.send_presence("leave")
        if self.mqtt:
            self.mqtt.disconnect()

def main():
    console = UIManager().console
    show_welcome(console)
    session = PromptSession()

    while True:
        try:
            nick_input = session.prompt("👤 Choose your Nickname (Default: User): ")
        except (KeyboardInterrupt, EOFError):
            print_formatted_text("\nExit requested.")
            return
        nickname = nick_input.strip() or f"User-{random.randint(100, 999)}"
        if nickname: break

    suggested_room = generate_room_id()
    try:
        room_input = session.prompt(f"🔑 Enter Room ID to Join (Press Enter for: {suggested_room}): ")
    except (KeyboardInterrupt, EOFError):
        print_formatted_text("\nExit requested.")
        return
    room_id = room_input.strip().lower() or suggested_room

    try:
        password = session.prompt("🔐 Optional Room Password (for E2EE, press Enter for None): ", is_password=True)
    except (KeyboardInterrupt, EOFError):
        print_formatted_text("\nExit requested.")
        return

    chat = ChatClient(nickname, room_id, password)
    try:
        chat.setup_mqtt()
    except Exception:
        sys.exit(1)
    chat.start_background_tasks()
    
    os.system('cls' if os.name == 'nt' else 'clear')
    chat.ui.print_room_banner(chat.room_id, chat.nickname, chat.status, bool(chat.password))

    prompt_style = PromptStyle.from_dict({'prompt': 'bold fg:ansicyan'})

    with patch_stdout():
        while chat.running:
            try:
                user_msg = session.prompt(f"{chat.nickname} > ", style=prompt_style)
                user_msg = user_msg.strip()
                if not user_msg: continue

                if user_msg.startswith("/"):
                    parts = user_msg.split()
                    cmd = parts[0].lower()
                    
                    if cmd in ["/exit", "/quit"]:
                        chat.ui.print_system("Goodbye!")
                        break
                    
                    elif cmd == "/room":
                        if len(parts) < 2: chat.ui.print_system("Usage: /room <room_id> [optional_password]")
                        else: chat.switch_room(parts[1], parts[2] if len(parts) > 2 else "")

                    elif cmd == "/broker":
                        if len(parts) < 2: chat.ui.print_system("Usage: /broker <host> [port]")
                        else:
                            host = parts[1]
                            try:
                                port = int(parts[2]) if len(parts) > 2 else 1883
                                chat.change_broker(host, port)
                            except ValueError:
                                chat.ui.print_system("Port must be a valid integer.")
                    
                    elif cmd == "/nick":
                        if len(parts) < 2: chat.ui.print_system("Usage: /nick <new_name>")
                        else:
                            new_nick = " ".join(parts[1:])
                            chat.send_presence("rename", {"old": chat.nickname, "new": new_nick})
                            chat.nickname = new_nick
                            chat.ui.print_system(f"Nickname updated to [bold]{new_nick}[/]")
                    
                    elif cmd == "/msg":
                        if len(parts) < 3: chat.ui.print_system("Usage: /msg <recipient_name> <message>")
                        else: chat.send_private_message(parts[1], " ".join(parts[2:]))
                    
                    elif cmd == "/status":
                        chat.status = "Active" if len(parts) < 2 else " ".join(parts[1:])
                        chat.send_presence("heartbeat")
                        chat.ui.print_system(f"Status changed to: [italic yellow]{chat.status}[/]")
                    
                    elif cmd in ["/who", "/users"]:
                        chat.ui.show_users(chat.room_id, chat.nickname, chat.status, chat.peers, time.time())
                    
                    elif cmd == "/log":
                        if len(parts) > 1 and parts[1].lower() in ["off", "disable"]:
                            chat.logging_enabled = False
                            chat.ui.print_system("Session logging disabled.")
                        elif len(parts) > 1 and parts[1].lower() in ["on", "enable"]:
                            chat.logging_enabled = True
                            chat.ui.print_system("Session logging enabled.")
                        else:
                            chat.ui.print_system(f"Session logging is [bold]{'ENABLED' if chat.logging_enabled else 'DISABLED'}[/].")
                    
                    elif cmd == "/clear":
                        os.system('cls' if os.name == 'nt' else 'clear')
                        chat.ui.print_room_banner(chat.room_id, chat.nickname, chat.status, bool(chat.password))
                    
                    elif cmd == "/help":
                        chat.ui.print_help()
                    
                    else:
                        chat.ui.print_system(f"Unknown command: {cmd}. Type [bold]/help[/] for a list of commands.")
                else:
                    chat.send_chat_message(user_msg)
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    chat.ui.safe_print(f"[dim gray][{timestamp}][/] <[bold green]You[/]>:")
                    chat.ui.safe_print(Markdown(user_msg))
                    chat.write_to_log(f"<You>: {user_msg}")

            except KeyboardInterrupt:
                chat.ui.print_system("\nExiting chat context. Goodbye!")
                break
            except EOFError:
                break

    chat.close()

if __name__ == "__main__":
    main()
