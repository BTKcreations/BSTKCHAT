"""Decentralized, tamper-evident encrypted hash-chain storage for BSTK Chat rooms."""

import hashlib
import json
import os
import threading
from typing import Dict, List, Optional, Set, Tuple

from bstkchat.crypto import sanitize_room_id

GENESIS_HASH = "GENESIS"


def compute_block_hash(block: dict) -> str:
    """Computes the SHA-256 integrity hash for a chain block."""
    payload = "|".join([
        str(block.get("prev_hash", GENESIS_HASH)),
        str(block.get("id", "")),
        f"{float(block.get('ts', 0.0)):.6f}",
        str(block.get("time", "")),
        str(block.get("sender", "")),
        str(block.get("type", "message")),
        str(block.get("ciphertext", "")),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RoomChain:
    """Thread-safe, append-only encrypted message chain stored per room."""

    def __init__(self, room_id: str, store_dir: str = "", enabled: bool = True):
        self.room_id = sanitize_room_id(room_id)
        self.enabled = enabled
        self._lock = threading.Lock()
        self.store_dir = store_dir or os.path.expanduser("~/.bstkchat/chains")
        self.filepath = os.path.join(self.store_dir, f"{self.room_id}.json")
        self.blocks: List[dict] = []
        self._seen_ids: Set[str] = set()
        if self.enabled:
            self._load()

    def _load(self) -> None:
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                valid_blocks = [
                    b for b in data
                    if isinstance(b, dict) and b.get("id") and b.get("hash") == compute_block_hash(b)
                ]
                valid_blocks.sort(key=lambda b: (float(b.get("ts", 0.0)), str(b.get("id", ""))))
                self.blocks = valid_blocks
                self._seen_ids = {str(b["id"]) for b in valid_blocks}
        except (OSError, json.JSONDecodeError, ValueError):
            self.blocks = []
            self._seen_ids = set()

    def _save(self) -> None:
        if not self.enabled:
            return
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.blocks, f, indent=2)
        except OSError:
            pass

    def append_message(
        self,
        msg_id: str,
        ts: float,
        time_str: str,
        sender: str,
        msg_type: str,
        ciphertext: str,
    ) -> Optional[dict]:
        """Creates, hashes, and persists a new encrypted block if not already present."""
        if not self.enabled or not msg_id:
            return None

        with self._lock:
            if msg_id in self._seen_ids:
                return None

            prev_hash = self.blocks[-1]["hash"] if self.blocks else GENESIS_HASH
            block = {
                "id": msg_id,
                "ts": float(ts),
                "time": time_str,
                "sender": sender,
                "type": msg_type,
                "ciphertext": ciphertext,
                "prev_hash": prev_hash,
            }
            block["hash"] = compute_block_hash(block)

            self.blocks.append(block)
            self.blocks.sort(key=lambda b: (float(b.get("ts", 0.0)), str(b.get("id", ""))))
            self._seen_ids.add(msg_id)
            self._save()
            return block

    def merge_blocks(self, incoming_blocks: List[dict]) -> List[dict]:
        """Verifies hashes, deduplicates, merges, and persists blocks from peers."""
        if not self.enabled or not incoming_blocks:
            return []

        added: List[dict] = []
        with self._lock:
            for block in incoming_blocks:
                if not isinstance(block, dict):
                    continue
                msg_id = str(block.get("id", ""))
                if not msg_id or msg_id in self._seen_ids:
                    continue
                if block.get("hash") != compute_block_hash(block):
                    continue
                self.blocks.append(block)
                self._seen_ids.add(msg_id)
                added.append(block)

            if added:
                self.blocks.sort(key=lambda b: (float(b.get("ts", 0.0)), str(b.get("id", ""))))
                self._save()

        added.sort(key=lambda b: (float(b.get("ts", 0.0)), str(b.get("id", ""))))
        return added

    def get_known_ids(self, limit: int = 500) -> List[str]:
        with self._lock:
            return [str(b["id"]) for b in self.blocks[-limit:]]

    def get_missing_blocks(self, peer_known_ids: Set[str], limit: int = 100) -> List[dict]:
        with self._lock:
            missing = [b for b in self.blocks if str(b.get("id")) not in peer_known_ids]
            return missing[-limit:]

    def get_recent_blocks(self, limit: int = 20) -> List[dict]:
        if limit <= 0:
            return []
        with self._lock:
            return list(self.blocks[-limit:])

    def get_block(self, msg_id: str) -> Optional[dict]:
        with self._lock:
            for block in self.blocks:
                if str(block.get("id")) == msg_id:
                    return dict(block)
            return None

    def verify_integrity(self) -> Tuple[bool, int]:
        """Verifies every block's SHA-256 hash in the local chain."""
        with self._lock:
            for block in self.blocks:
                if block.get("hash") != compute_block_hash(block):
                    return False, len(self.blocks)
            return True, len(self.blocks)
