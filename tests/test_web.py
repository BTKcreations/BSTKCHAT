import base64
import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

from tests import test_cli  # noqa: F401
from bstkchat.cli import ChatClient
from bstkchat.web import WebServer


def test_web_server_auth_and_api(tmp_path):
    chat = ChatClient("Alice", "web-room", "pw", store_chain=False)
    chat.client = MagicMock()
    chat.safe_print = MagicMock()
    chat.write_to_log = MagicMock()

    server = WebServer(chat, port=0)
    url = server.start()
    try:
        base_url, token_part = url.split("/?token=")

        # Unauthenticated request must return 403
        try:
            urllib.request.urlopen(f"{base_url}/api/state")
            assert False, "Expected 403 Forbidden"
        except urllib.error.HTTPError as err:
            assert err.code == 403

        # Authenticated HTML root returns 200 and UI markup
        with urllib.request.urlopen(url) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert "BSTK Chat" in html

        # Authenticated /api/state returns room state
        req = urllib.request.Request(
            f"{base_url}/api/state",
            headers={"X-BSTK-Token": token_part},
        )
        with urllib.request.urlopen(req) as resp:
            state = json.loads(resp.read().decode("utf-8"))
            assert state["room_id"] == "web-room"
            assert state["nickname"] == "Alice"
            assert state["e2ee"] is True

        # Authenticated POST /api/send publishes encrypted message
        send_req = urllib.request.Request(
            f"{base_url}/api/send",
            data=json.dumps({"text": "hello from web"}).encode("utf-8"),
            headers={"X-BSTK-Token": token_part, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(send_req) as resp:
            assert json.loads(resp.read().decode("utf-8"))["ok"] is True
        assert chat.client.publish.called

        # Authenticated POST /api/file/send publishes encrypted file
        file_req = urllib.request.Request(
            f"{base_url}/api/file/send",
            data=json.dumps({
                "filename": "hello.txt",
                "data_b64": base64.b64encode(b"web file bytes").decode("ascii"),
            }).encode("utf-8"),
            headers={"X-BSTK-Token": token_part, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(file_req) as resp:
            assert json.loads(resp.read().decode("utf-8"))["ok"] is True
    finally:
        server.stop()


def test_web_file_preview_inline_image_and_attachment():
    chat = ChatClient("Alice", "img-room", "pw", store_chain=False)
    chat.client = MagicMock()
    chat.safe_print = MagicMock()
    chat.write_to_log = MagicMock()

    assert chat.send_file_bytes("photo.png", b"\x89PNG\r\n\x1a\nfake") is True
    assert chat.send_file_bytes("script.html", b"<script>alert(1)</script>") is True

    server = WebServer(chat, port=0)
    url = server.start()
    try:
        base_url, token_part = url.split("/?token=")

        # Safe PNG image is served inline with image/png and nosniff
        with urllib.request.urlopen(f"{base_url}/api/file/preview?id=1&token={token_part}") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert "inline;" in resp.headers["Content-Disposition"]
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert resp.read().startswith(b"\x89PNG")

        # HTML/non-image file is forced as attachment (never inline XSS)
        with urllib.request.urlopen(f"{base_url}/api/file/preview?id=2&token={token_part}") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "application/octet-stream"
            assert "attachment;" in resp.headers["Content-Disposition"]
    finally:
        server.stop()


def test_web_whatsapp_features_react_edit_delete_ack_and_audio():
    chat = ChatClient("Alice", "wa-room", "pw", store_chain=False)
    chat.client = MagicMock()
    chat.safe_print = MagicMock()
    chat.write_to_log = MagicMock()

    msg_id = chat.send_chat_message("original", reply_to={"id": "0", "sender": "Bob", "preview": "hi"})
    assert msg_id is not None
    assert chat.send_file_bytes("note.webm", b"OggSfakeaudio") is True

    server = WebServer(chat, port=0)
    url = server.start()
    try:
        base_url, token_part = url.split("/?token=")
        headers = {"X-BSTK-Token": token_part, "Content-Type": "application/json"}

        for path, body in [
            ("/api/react", {"target_id": msg_id, "emoji": "👍"}),
            ("/api/edit", {"target_id": msg_id, "text": "edited text"}),
            ("/api/ack", {"target_id": msg_id, "status": "read"}),
            ("/api/delete", {"target_id": msg_id}),
        ]:
            req = urllib.request.Request(
                f"{base_url}{path}", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
            )
            with urllib.request.urlopen(req) as resp:
                assert json.loads(resp.read().decode("utf-8"))["ok"] is True

        assert chat.edited_msgs[msg_id] == "edited text"
        assert msg_id in chat.deleted_msgs
        assert "👍" in chat.reactions[msg_id]

        with urllib.request.urlopen(f"{base_url}/api/file/preview?id=1&token={token_part}") as resp:
            assert resp.headers["Content-Type"] == "audio/webm"
            assert "inline;" in resp.headers["Content-Disposition"]
    finally:
        server.stop()
