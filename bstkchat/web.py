"""Lightweight local HTTP + SSE server for BSTK Chat browser UI."""

import base64
import hmac
import importlib.resources
import json
import os
import queue
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from bstkchat.config import MAX_FILE_BYTES, sanitize_filename, search_log

MAX_BODY_BYTES = 1024 * 1024  # 1 MB max JSON request body
SAFE_INLINE_IMAGES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _load_web_html() -> bytes:
    """Loads the bundled web_ui.html asset cleanly across editable and wheel installs."""
    try:
        html_text = (
            importlib.resources.files("bstkchat")
            .joinpath("web_ui.html")
            .read_text(encoding="utf-8")
        )
        return html_text.encode("utf-8")
    except Exception:
        fallback_path = os.path.join(os.path.dirname(__file__), "web_ui.html")
        with open(fallback_path, "rb") as f:
            return f.read()


class WebServer:
    """Localhost-only HTTP + SSE server bound to 127.0.0.1 with per-session token auth."""

    def __init__(self, chat_client: Any, host: str = "127.0.0.1", port: int = 0):
        self.chat = chat_client
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._html_bytes = _load_web_html()

    @property
    def url(self) -> str:
        actual_port = self._httpd.server_address[1] if self._httpd else self.port
        return f"http://{self.host}:{actual_port}/?token={self.token}"

    def start(self) -> str:
        """Starts the HTTP server in a daemon thread and returns the authenticated URL."""
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                # Suppress noisy stdout access logs so terminal TUI stays clean
                return

            def _check_auth(self, parsed_url) -> bool:
                header_tok = self.headers.get("X-BSTK-Token", "")
                qs = parse_qs(parsed_url.query)
                query_tok = qs.get("token", [""])[0]
                candidate = header_tok or query_tok
                return bool(candidate) and hmac.compare_digest(candidate, server_ref.token)

            def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self) -> Optional[Dict[str, Any]]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return None
                if length <= 0 or length > MAX_BODY_BYTES:
                    return None
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8"))
                    return data if isinstance(data, dict) else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None

            def do_GET(self):
                parsed = urlparse(self.path)
                if not self._check_auth(parsed):
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid or missing session token"})
                    return

                if parsed.path == "/":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(server_ref._html_bytes)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(server_ref._html_bytes)
                    return

                if parsed.path == "/api/state":
                    self._send_json(HTTPStatus.OK, server_ref.chat.get_web_state())
                    return

                if parsed.path == "/api/search":
                    qs = parse_qs(parsed.query)
                    query = qs.get("q", [""])[0]
                    matches = search_log(server_ref.chat.get_log_filepath(), query, limit=40)
                    self._send_json(HTTPStatus.OK, {"matches": matches})
                    return

                if parsed.path == "/api/file/preview":
                    qs = parse_qs(parsed.query)
                    file_id = qs.get("id", [""])[0]
                    force_download = qs.get("download", ["0"])[0] == "1"
                    entry = server_ref.chat.get_shared_file(file_id)
                    if not entry:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "File not found"})
                        return
                    filename = sanitize_filename(str(entry["filename"]))
                    ext = os.path.splitext(filename)[1].lower()
                    raw_bytes = bytes(entry["data"])
                    mime = SAFE_INLINE_IMAGES.get(ext, "application/octet-stream")
                    is_inline = (ext in SAFE_INLINE_IMAGES) and not force_download
                    disp = f'inline; filename="{filename}"' if is_inline else f'attachment; filename="{filename}"'

                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(raw_bytes)))
                    self.send_header("Content-Disposition", disp)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(raw_bytes)
                    return

                if parsed.path == "/events":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()

                    q = server_ref.chat.register_web_listener()
                    try:
                        init_payload = json.dumps(server_ref.chat.get_web_state())
                        self.wfile.write(f"event: state_update\ndata: {init_payload}\n\n".encode("utf-8"))
                        self.wfile.flush()

                        while server_ref.chat.running:
                            try:
                                evt = q.get(timeout=15)
                                msg = f"event: {evt['event']}\ndata: {json.dumps(evt['data'])}\n\n"
                                self.wfile.write(msg.encode("utf-8"))
                                self.wfile.flush()
                            except queue.Empty:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        server_ref.chat.unregister_web_listener(q)
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_POST(self):
                parsed = urlparse(self.path)
                if not self._check_auth(parsed):
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid or missing session token"})
                    return

                data = self._read_json()
                if data is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
                    return

                chat = server_ref.chat

                if parsed.path == "/api/send":
                    text = str(data.get("text", "")).strip()
                    target = str(data.get("target", "")).strip()
                    if not text:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Empty message"})
                        return
                    if target:
                        chat.send_private_message(target, text)
                    else:
                        chat.send_chat_message(text)
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return

                if parsed.path == "/api/room":
                    new_room = str(data.get("room_id", "")).strip()
                    new_pw = str(data.get("password", ""))
                    if not new_room:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Room ID required"})
                        return
                    chat.switch_room(new_room, new_pw)
                    self._send_json(HTTPStatus.OK, {"ok": True, "state": chat.get_web_state()})
                    return

                if parsed.path == "/api/nick":
                    new_nick = str(data.get("nickname", "")).strip()
                    if not new_nick:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Nickname required"})
                        return
                    old_nick = chat.nickname
                    chat.send_presence("rename", {"old": old_nick, "new": new_nick})
                    chat.nickname = new_nick
                    chat._emit_event("state_update", chat.get_web_state())
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return

                if parsed.path == "/api/status":
                    new_status = str(data.get("status", "")).strip() or "Active"
                    chat.status = new_status
                    chat._auto_away_active = False
                    chat.send_presence("heartbeat")
                    chat._emit_event("state_update", chat.get_web_state())
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return

                if parsed.path == "/api/typing":
                    chat.notify_typing()
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return

                if parsed.path == "/api/sync":
                    limit = max(1, min(500, int(data.get("limit", 100))))
                    chat.request_sync(limit=limit)
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return

                if parsed.path == "/api/file/send":
                    filename = str(data.get("filename", "")).strip()
                    b64_data = str(data.get("data_b64", ""))
                    try:
                        raw_bytes = base64.b64decode(b64_data.encode("ascii"))
                    except Exception:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid base64 payload"})
                        return
                    if len(raw_bytes) > MAX_FILE_BYTES:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "File exceeds 512 KB limit"})
                        return
                    ok = chat.send_file_bytes(filename or "upload.bin", raw_bytes)
                    self._send_json(HTTPStatus.OK, {"ok": ok})
                    return

                if parsed.path == "/api/file/accept":
                    file_id = str(data.get("file_id", "")).strip() or None
                    saved = chat.accept_file(file_id)
                    if not saved:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "File not found"})
                        return
                    self._send_json(HTTPStatus.OK, {"ok": True, "path": saved, "state": chat.get_web_state()})
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
