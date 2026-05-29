import os
import random
from typing import Dict, Any, List

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.markdown import Markdown

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI

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


class UIManager:
    def __init__(self):
        self.console = Console(force_terminal=True)

    def safe_print(self, *args, **kwargs):
        with self.console.capture() as capture:
            self.console.print(*args, **kwargs)
        print_formatted_text(ANSI(capture.get()), end="")

    def print_system(self, text: str):
        self.safe_print(f"[bold dim yellow]⚡ System:[/] [dim white]{text}[/]")

    def print_room_banner(self, room_id: str, nickname: str, status: str, has_password: bool):
        banner_text = Text()
        banner_text.append("💬 Active Room: ", style="bold green")
        banner_text.append(f"{room_id}\n", style="bold white underline")
        banner_text.append("👤 Nickname: ", style="bold cyan")
        banner_text.append(f"{nickname} ", style="bold white")
        banner_text.append(f"({status})\n", style="italic dim yellow")

        if has_password:
            banner_text.append("🔐 Encryption: ", style="bold green")
            banner_text.append("On (AES-GCM Protected)\n", style="bold bright_green")
        else:
            banner_text.append("🔓 Encryption: ", style="bold red")
            banner_text.append("Standard (No Password)\n", style="dim white")

        banner_text.append("ℹ️  Commands: ", style="bold magenta")
        banner_text.append("/room <id> [pass], /nick <name>, /msg <user> <text>, /status <text>, /who, /log, /broker <addr> [port], /clear, /exit", style="italic gray")

        self.safe_print(Panel(banner_text, border_style="blue", expand=False))
        self.print_system("Successfully joined room. Type markdown or slash commands!")

    def show_users(self, room_id: str, nickname: str, status: str, peers: Dict[str, Any], current_time: float):
        table = Table(title=f"Users in {room_id}", title_style="bold underline magenta", show_header=True)
        table.add_column("Username", style="cyan")
        table.add_column("Status Message", style="yellow")
        table.add_column("Last Seen", style="green")

        table.add_row(f"{nickname} (You)", status, "Now")
        for peer, info in peers.items():
            last_seen_diff = int(current_time - info["last_seen"])
            seen_str = "Just now" if last_seen_diff < 5 else f"{last_seen_diff}s ago"
            table.add_row(peer, info["status"], seen_str)

        self.safe_print(table)

    def print_help(self):
        table = Table(title="Available Commands", title_style="bold green", show_header=True)
        table.add_column("Command", style="cyan")
        table.add_column("Description", style="white")
        table.add_row("/room <id> [pass]", "Switch rooms instantly, optionally encrypting with password")
        table.add_row("/broker <host> [port]", "Switch to a custom MQTT broker")
        table.add_row("/nick <name>", "Change your active nickname")
        table.add_row("/msg <user> <msg>", "Send a private message (DM) to a user (Uses X25519 E2EE)")
        table.add_row("/status <text>", "Set a customizable status message (e.g. AFK, Writing)")
        table.add_row("/who", "List all users and their status messages")
        table.add_row("/log [on/off]", "Enable/Disable session logging or view log file path")
        table.add_row("/clear", "Clear terminal screen")
        table.add_row("/exit", "Disconnect and close the app")
        self.safe_print(table)

def show_welcome(console: Console):
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
            "• End-to-End Encryption (AES-GCM & X25519)\n"
            "• Built-in Markdown & Syntax Highlighting\n"
            "• Private Messaging, Mentions & Audio Alerts\n"
            "• Session Logging, Custom Statuses & Custom Brokers",
            border_style="cyan",
            expand=False
        ))
    print_formatted_text(ANSI(capture.get()), end="")
