# **BSTK Chat (bstkchat)**

bstkchat is a real-time, terminal-based, zero-configuration collaborative chat client. Anyone who launches the client and joins using the same Room ID will instantly join a shared, encrypted-by-knowledge room to text and exchange thoughts.

## **Features**

* **Zero Configuration**: Connects instantly to a public MQTT infrastructure—no servers to set up or configure!
* **End-to-End Encryption (E2EE)**: Optional room-password encryption using scrypt key derivation and AES-256-GCM authenticated encryption with per-message random nonces.
* **Modern Terminal UI/UX**: Non-blocking input with `prompt_toolkit`, live bottom status bar, slash-command & `@mention` autocomplete, command history, and Rich Markdown rendering.
* **Memorable Auto-Rooms**: If you don't supply a Room ID, the app generates a fun phonetic key like `quantum-beacon-55`.
* **Deterministic Color Codes**: Users are automatically allocated consistent text colors based on their username.
* **Private DMs, Mentions & Logging**: Send encrypted DMs (`/msg`), receive audio/highlight alerts on `@mentions`, and save sanitized local logs to `~/.bstkchat/logs/`.

## **Installation**

### **Method 1: Local Pip installation (Recommended)**

1. Save the generated setup.py and bstkchat/cli.py files to your computer under a directory layout like this:  
   my-chat-app/  
   ├── setup.py  
   └── bstkchat/  
       ├── \_\_init\_\_.py  (this can be empty)  
       └── cli.py

2. Navigate into the parent directory my-chat-app and execute:  
   pip install .

3. Once completed, simply run the global cli application from anywhere:  
   bstkchat

### **Method 2: Running directly from Python**

If you do not want to install it as a system CLI tool, install dependencies and run:

pip install paho-mqtt rich prompt-toolkit  
python \-m bstkchat.cli

## **Slash Commands inside BSTK Chat**

| Command | Action |
| :---- | :---- |
| `/help` | Displays a formatted table listing all commands |
| `/room <id> [pass]` | Switches rooms and optionally enables AES-256-GCM E2EE with a password |
| `/nick <name>` | Renames your user identity inside the room and alerts others |
| `/msg <user> <msg>` | Sends an encrypted private direct message (DM) to a user |
| `/status <text>` | Sets a custom status message (e.g., `AFK`, `Coding`) |
| `/who` or `/users` | Lists all online participants active inside your room |
| `/log [on\|off]` | Toggles session logging or shows the current log file path |
| `/clear` | Wipes the active console screen |
| `/exit` or `/quit` | Gracefully announces leave status and exits the CLI |

