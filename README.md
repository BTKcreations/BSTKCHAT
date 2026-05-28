# **BSTK Chat (bstkchat)**

bstkchat is a real-time, terminal-based, zero-configuration collaborative chat client. Anyone who launches the client and joins using the same Room ID will instantly join a shared, encrypted-by-knowledge room to text and exchange thoughts.

## **Features**

* **Zero Configuration**: Connects instantly to a public MQTT infrastructure—no servers to set up or configure\!  
* **Non-blocking Input**: Messages arriving dynamically will *never* corrupt or break the active line you are typing into, thanks to prompt\_toolkit.  
* **Memorable Auto-Rooms**: If you don't supply a Room ID, the app will generate a fun phonetic key like quantum-beacon-55 for you.  
* **Deterministic Color Codes**: Users are automatically allocated static text colors based on their username hashes, making conversations extremely easy to follow.  
* **Command Architecture**: Supports built-in slash commands to change nicknames, jump rooms, see active user tables, and clear logs.

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
| /help | Displays a formatted table listing all commands |
| /room \<new-room-id\> | Switches context and connects you to a new room ID instantly |
| /nick \<new-name\> | Renames your user identity inside the room and alerts others |
| /who or /users | Lists all online participants active inside your room |
| /clear | Wipes the active console screen |
| /exit | Gracefully announces leave status and exits the CLI |

