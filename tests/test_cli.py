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

            patch_mod = types.ModuleType("prompt_toolkit.patch_stdout")
            patch_mod.patch_stdout = MagicMock

            short_mod = types.ModuleType("prompt_toolkit.shortcuts")
            short_mod.clear = lambda: None

            styles_mod = types.ModuleType("prompt_toolkit.styles")
            styles_mod.Style = MagicMock

            sys.modules["prompt_toolkit"] = pt_mod
            sys.modules["prompt_toolkit.completion"] = comp_mod
            sys.modules["prompt_toolkit.formatted_text"] = fmt_mod
            sys.modules["prompt_toolkit.history"] = hist_mod
            sys.modules["prompt_toolkit.patch_stdout"] = patch_mod
            sys.modules["prompt_toolkit.shortcuts"] = short_mod
            sys.modules["prompt_toolkit.styles"] = styles_mod


_ensure_cli_dependencies()

from bstkchat.cli import (  # noqa: E402
    ChatClient,
    ChatCompleter,
    is_user_mentioned,
    parse_command_args,
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
