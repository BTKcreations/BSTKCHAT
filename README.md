# BSTK Chat (`bstkchat`)

**BSTK Chat** is a real-time, zero-configuration terminal chat client with optional **AES-256-GCM end-to-end encryption (E2EE)**, Rich Markdown rendering, and a modern interactive TUI. Anyone who launches `bstkchat` and enters the same Room ID (and optional password) joins a shared room instantly—no server setup required.

---

## Features

- **Zero Configuration:** Connects automatically over MQTT (`broker.hivemq.com`) with no servers, accounts, or port forwarding.
- **End-to-End Encryption (E2EE):** Protect rooms with a password using `scrypt` key derivation, per-message random 96-bit nonces, **AES-256-GCM** authenticated encryption, and topic-bound AAD.
- **Modern Terminal UI/UX:**
  - Non-blocking input powered by `prompt_toolkit` so incoming messages never interrupt what you are typing.
  - Live bottom status bar showing connection state, active `#room`, E2EE badge, online user count, and logging state.
  - Context-aware autocomplete for `/` commands, `/msg <user>`, `/log [on|off]`, and `@mentions`, plus `↑`/`↓` command history.
- **Rich Markdown & Syntax Highlighting:** Send formatted text, inline code, and fenced code blocks rendered cleanly with `rich`.
- **Private DMs & `@Mention` Alerts:** Send encrypted direct messages with `/msg` and receive highlighted notifications + terminal bell alerts when mentioned.
- **Presence & Heartbeats:** Automatic join/leave announcements, Last Will disconnect detection, custom `/status` messages, and `/who` user tables.
- **Safe Local Logging:** Saves transcripts to `~/.bstkchat/logs/<room>.log` with sanitized filenames and `/log on|off` controls.

---

## Installation

### Option 1: Using `uv` (Recommended)
Run without installing:
```bash
uvx bstkchat
```

Or install globally:
```bash
uv tool install bstkchat
bstkchat
```

### Option 2: Using `pip`
```bash
pip install bstkchat
bstkchat
```

### Option 3: From Source
```bash
git clone https://github.com/BTKcreations/BSTKCHAT.git
cd BSTKCHAT
pip install .
bstkchat
```

---

## Quick Start & Examples

### 1. Start a Private Encrypted Room
1. Launch `bstkchat`:
   ```bash
   bstkchat
   ```
2. Enter your **Nickname** (or press `Enter` for a random `User-XXX` name).
3. Enter a **Room ID** (e.g., `dev-sync-42`) or press `Enter` to use the generated phonetic ID (e.g., `quantum-beacon-55`).
4. Enter a **Room Password** to enable AES-256-GCM E2EE.
5. Share the **Room ID** and **Password** with your teammates—anyone with both can join and decrypt messages.

### 2. Example In-Chat Workflow
```text
# Send Rich Markdown and code snippets
Alice > Hey @Bob, check this: `pip install bstkchat`

# Send an encrypted private Direct Message to Bob
Alice > /msg Bob Can you review PR #12?

# Set your status
Alice > /status In a meeting

# Switch to another encrypted room (use quotes for passwords with spaces)
Alice > /room release-ops "my secret passphrase"

# Check who is online
Alice > /who

# Pause local logging
Alice > /log off
```

---

## Use Cases

- **Quick Developer Syncs:** Spin up a temporary room in seconds from any terminal without opening a browser or creating an account.
- **Pair Programming & Code Sharing:** Share formatted Markdown and code snippets directly in the terminal while staying in your coding workflow.
- **Ad-Hoc Encrypted Discussions:** Use a shared passphrase for private conversations where payloads are encrypted before leaving your terminal.
- **Remote / SSH / Headless Environments:** Chat from minimal Linux/Windows/macOS terminals or over SSH sessions where GUI chat apps aren't available.

---

## Slash Commands

| Command | Description |
| :--- | :--- |
| `/help` | Displays a formatted table of all available commands |
| `/room <id> [password]` | Switches rooms and optionally enables AES-256-GCM E2EE |
| `/nick <name>` | Changes your nickname and notifies peers in the room |
| `/msg <user> <message>` | Sends an encrypted private direct message (DM) to a user |
| `/status <text>` | Sets your custom status message (e.g., `AFK`, `Coding`) |
| `/who` or `/users` | Lists all online participants, statuses, and last-seen times |
| `/log [on\|off]` | Toggles local session logging or displays the log file path |
| `/clear` | Clears the terminal screen and redraws the room banner |
| `/exit` or `/quit` | Announces departure and exits cleanly |

---

## Security Notes

- **Password-protected rooms** derive a 256-bit key with `scrypt` and encrypt each message using **AES-256-GCM** with a unique random 96-bit nonce and room-topic AAD.
- **Public rooms (no password)** still use a room-derived key for transport formatting, so anyone who knows the Room ID can read messages—always set a password for confidential chats.
- **Local logs** are stored in plain text under `~/.bstkchat/logs/` when logging is enabled; use `/log off` if you do not want transcripts saved to disk.
