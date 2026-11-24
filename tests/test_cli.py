import json
import os
import sys
import types
from unittest.mock import MagicMock


def _ensure_cli_dependencies():
    """Provides minimal module stubs only when optional runtime deps are absent."""
    if "paho.mqtt.client" not in sys.modules:
        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:
            paho_mod = types.ModuleType("paho")
            mqtt_mod = types.ModuleType("paho.mqtt")
            client_mod = types.ModuleType("paho.mqtt.client")
            client_mod.Client = MagicMock
            sys.modules["paho"] = paho_mod
            sys.modules["paho.mqtt"] = mqtt_mod
            sys.modules["paho.mqtt.client"] = client_mod

    if "rich" not in sys.modules:
        try:
            import rich  # noqa: F401
        except ImportError:
            rich_mod = types.ModuleType("rich")
            box_mod = types.ModuleType("rich.box")
            box_mod.ROUNDED = "ROUNDED"
            console_mod = types.ModuleType("rich.console")

            class _DummyCapture:
                def __init__(self, buf):
                    self._buf = buf

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def get(self):
                    return "".join(self._buf)

            class _DummyConsole:
                def __init__(self, *args, **kwargs):
                    self.width = 100
                    self._buf = []

                def capture(self):
                    self._buf = []
                    return _DummyCapture(self._buf)

                def print(self, *args, **kwargs):
                    self._buf.append(" ".join(str(a) for a in args) + "\n")

            console_mod.Console = _DummyConsole
            md_mod = types.ModuleType("rich.markdown")
            md_mod.Markdown = lambda text: text
            markup_mod = types.ModuleType("rich.markup")
            markup_mod.escape = lambda s: s.replace("[", "\\[")
            panel_mod = types.ModuleType("rich.panel")
            panel_mod.Panel = lambda *a, **kw: a[0] if a else ""
            table_mod = types.ModuleType("rich.table")
            table_mod.Table = MagicMock
            text_mod = types.ModuleType("rich.text")
            text_mod.Text = MagicMock

            sys.modules["rich"] = rich_mod
            sys.modules["rich.box"] = box_mod
            sys.modules["rich.console"] = console_mod
            sys.modules["rich.markdown"] = md_mod
            sys.modules["rich.markup"] = markup_mod
            sys.modules["rich.panel"] = panel_mod
            sys.modules["rich.table"] = table_mod
            sys.modules["rich.text"] = text_mod

    if "prompt_toolkit" not in sys.modules:
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            pt_mod = types.ModuleType("prompt_toolkit")
            pt_mod.PromptSession = MagicMock
            pt_mod.print_formatted_text = lambda *a, **kw: None

            comp_mod = types.ModuleType("prompt_toolkit.completion")
            comp_mod.Completer = object
            comp_mod.Completion = lambda text, start_position=0, display_meta="": (
                text,
                start_position,
                display_meta,
            )

            fmt_mod = types.ModuleType("prompt_toolkit.formatted_text")
            fmt_mod.ANSI = lambda s: s
            fmt_mod.HTML = lambda s: s

            hist_mod = types.ModuleType("prompt_toolkit.history")
            hist_mod.InMemoryHistory = MagicMock

            kb_mod = types.ModuleType("prompt_toolkit.key_binding")

            class _DummyKB:
                def add(self, *args, **kwargs):
                    return lambda fn: fn

            kb_mod.KeyBindings = _DummyKB

            patch_mod = types.ModuleType("prompt_toolkit.patch_stdout")
            patch_mod.patch_stdout = MagicMock

            short_mod = types.ModuleType("prompt_toolkit.shortcuts")
            short_mod.clear = lambda: None

            styles_mod = types.ModuleType("prompt_toolkit.styles")
            styles_mod.Style = MagicMock
            styles_mod.DynamicStyle = MagicMock

            sys.modules["prompt_toolkit"] = pt_mod
            sys.modules["prompt_toolkit.completion"] = comp_mod
            sys.modules["prompt_toolkit.formatted_text"] = fmt_mod
            sys.modules["prompt_toolkit.history"] = hist_mod
            sys.modules["prompt_toolkit.key_binding"] = kb_mod
            sys.modules["prompt_toolkit.patch_stdout"] = patch_mod
            sys.modules["prompt_toolkit.shortcuts"] = short_mod
            sys.modules["prompt_toolkit.styles"] = styles_mod


_ensure_cli_dependencies()

from bstkchat.cli import (  # noqa: E402
    ChatClient,
    ChatCompleter,
    build_arg_parser,
    is_user_mentioned,
    parse_command_args,
)
from bstkchat.config import (  # noqa: E402
    load_config,
    sanitize_filename,
    save_config,
    save_received_file,
    search_log,
    tail_log,
)
from bstkchat.crypto import encrypt_payload  # noqa: E402


def test_parse_command_args_supports_quotes():
    assert parse_command_args('/room my-room "my secret pass"') == [
        "/room",
        "my-room",
        "my secret pass",
    ]
    assert parse_command_args('/room my-room "unclosed') == [
        "/room",
        "my-room",
        '"unclosed',
    ]


def test_is_user_mentioned_avoids_substring_false_positives():
    assert is_user_mentioned("hey @Al check this", "al") is True
    assert is_user_mentioned("Al, can you look?", "al") is True
    assert is_user_mentioned("always check your work", "al") is False


def test_presence_join_rename_leave_and_self_echo():
    client = ChatClient("Alice", "../../evil-room", "pw")
    assert client.room_id == "evil-room"

    client.client = MagicMock()
    client.safe_print = MagicMock()
    client.write_to_log = MagicMock()

    # Own message must be ignored
    own_msg = MagicMock()
    own_msg.topic = client.presence_topic
    own_msg.payload = json.dumps({"sender": "Alice", "type": "rename", "old": "A", "new": "Alice"}).encode()
    client.on_message(None, None, own_msg)
    assert client.get_peer_names() == []

    # Peer join, rename, leave
    client.handle_presence("Bob", "join", {"status": "Coding"})
    assert client.get_peer_names() == ["Bob"]

    client.handle_presence("Bob", "rename", {"old": "Bob", "new": "Bobby", "status": "Active"})
    assert client.get_peer_names() == ["Bobby"]

    client.handle_presence("Bobby", "leave", {})
    assert client.get_peer_names() == []


def test_handle_chat_message_decryption_failure_and_dm():
    client = ChatClient("Alice", "room-x", "secret")
    client.safe_print = MagicMock()
    client.write_to_log = MagicMock()

    # Wrong key should render warning and not raise
    bad_token = encrypt_payload("hi", b"0" * 32, aad=client.msg_topic.encode("utf-8"))
    client.handle_chat_message("Bob", "message", {"text": bad_token}, client.msg_topic)
    assert "Decryption Failed" in str(client.safe_print.call_args[0][0])

    # Valid DM addressed to Alice
    client.safe_print.reset_mock()
    good_token = encrypt_payload("psst", client.secret_key, aad=client.msg_topic.encode("utf-8"))
    client.handle_chat_message("Bob", "dm", {"target": "Alice", "text": good_token}, client.msg_topic)
    assert "DM from Bob" in str(client.safe_print.call_args[0][0])


def test_write_to_log_stays_inside_log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    client = ChatClient("Alice", "../../escape", "")
    client.write_to_log("hello log")

    expected = tmp_path / ".bstkchat" / "logs" / "escape.log"
    assert expected.exists()
    assert "hello log" in expected.read_text(encoding="utf-8")


def test_chat_completer_suggests_commands_and_peers():
    client = ChatClient("Alice", "room-1", "")
    client.handle_presence("Bob", "join", {"status": "Active"})
    completer = ChatCompleter(client)

    class _Doc:
        def __init__(self, text):
            self.text_before_cursor = text

        def get_word_before_cursor(self, WORD=True):
            return self.text_before_cursor.split()[-1] if self.text_before_cursor.split() else ""

    cmd_completions = [c[0] for c in completer.get_completions(_Doc("/ro"), None)]
    assert "/room" in cmd_completions

    dm_completions = [c[0] for c in completer.get_completions(_Doc("/msg B"), None)]
    assert "Bob" in dm_completions

    mention_completions = [c[0] for c in completer.get_completions(_Doc("hello @B"), None)]
    assert "@Bob" in mention_completions


def test_encrypted_file_send_and_accept_roundtrip(tmp_path):
    src_file = tmp_path / "notes.txt"
    src_file.write_bytes(b"hello encrypted file")

    sender = ChatClient("Alice", "room-file", "pw")
    sender.client = MagicMock()
    sender.safe_print = MagicMock()
    sender.write_to_log = MagicMock()

    assert sender.send_file(str(src_file)) is True
    published_payload = json.loads(sender.client.publish.call_args[0][1])

    receiver = ChatClient("Bob", "room-file", "pw")
    receiver.safe_print = MagicMock()
    receiver.write_to_log = MagicMock()
    receiver.muted = True

    receiver.handle_chat_message("Alice", "file", published_payload, receiver.msg_topic)
    assert receiver.get_pending_file_ids() == ["1"]

    dest_dir = tmp_path / "downloads"
    saved = receiver.accept_file("1", target_dir=str(dest_dir))
    assert saved is not None
    assert os.path.basename(saved) == "notes.txt"
    with open(saved, "rb") as f:
        assert f.read() == b"hello encrypted file"


def test_sanitize_filename_and_collision_avoidance(tmp_path):
    assert sanitize_filename("../../evil.sh") == "evil.sh"
    p1 = save_received_file("report.txt", b"one", target_dir=str(tmp_path))
    p2 = save_received_file("report.txt", b"two", target_dir=str(tmp_path))
    assert p1 != p2
    assert os.path.basename(p2) == "report_1.txt"


def test_tail_and_search_log(tmp_path):
    log_file = tmp_path / "room.log"
    log_file.write_text("line one\nHello World\nline three\nhello again\n", encoding="utf-8")

    assert tail_log(str(log_file), limit=2) == ["line three", "hello again"]
    matches = search_log(str(log_file), "hello")
    assert len(matches) == 2
    assert "L2: Hello World" in matches[0]


def test_config_roundtrip_and_cli_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    save_config({"nickname": "Alice", "theme": "light", "compact": True})
    loaded = load_config()
    assert loaded["nickname"] == "Alice"
    assert loaded["theme"] == "light"

    args = build_arg_parser().parse_args(["--nick", "Alice", "--room", "dev", "--no-tls", "--compact"])
    assert args.nick == "Alice"
    assert args.room == "dev"
    assert args.no_tls is True
    assert args.compact is True


def test_typing_presence_and_theme_completer():
    client = ChatClient("Alice", "room-1", "")
    client.handle_presence("Bob", "typing", {"status": "Active"})
    assert "Bob" in client.typing_peers

    completer = ChatCompleter(client)

    class _Doc:
        def __init__(self, text):
            self.text_before_cursor = text

        def get_word_before_cursor(self, WORD=True):
            return ""

    themes = [c[0] for c in completer.get_completions(_Doc("/theme l"), None)]
    assert themes == ["light"]
