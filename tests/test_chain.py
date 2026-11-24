import json
from unittest.mock import MagicMock

from tests import test_cli  # noqa: F401 (ensures optional runtime stubs if needed)
from bstkchat.chain import RoomChain
from bstkchat.cli import ChatClient
from bstkchat.crypto import encrypt_payload


def test_room_chain_persistence_and_integrity(tmp_path):
    chain = RoomChain("dev-room", store_dir=str(tmp_path))
    b1 = chain.append_message("m1", 100.0, "01:00:00", "Alice", "message", "cipher1")
    b2 = chain.append_message("m2", 101.0, "01:00:01", "Bob", "message", "cipher2")

    assert b1 is not None and b2 is not None
    assert b2["prev_hash"] == b1["hash"]
    assert chain.verify_integrity() == (True, 2)

    # Reload from disk
    reloaded = RoomChain("dev-room", store_dir=str(tmp_path))
    assert reloaded.verify_integrity() == (True, 2)
    assert reloaded.get_known_ids() == ["m1", "m2"]


def test_room_chain_rejects_tampered_and_duplicate_blocks(tmp_path):
    chain_a = RoomChain("room-x", store_dir=str(tmp_path / "a"))
    b1 = chain_a.append_message("m1", 10.0, "00:00:10", "Alice", "message", "c1")
    b2 = chain_a.append_message("m2", 11.0, "00:00:11", "Bob", "message", "c2")

    tampered = dict(b2)
    tampered["ciphertext"] = "hacked"

    chain_b = RoomChain("room-x", store_dir=str(tmp_path / "b"))
    added = chain_b.merge_blocks([b1, b1, tampered])
    assert len(added) == 1
    assert added[0]["id"] == "m1"


def test_peer_sync_request_and_response_between_clients(tmp_path):
    alice = ChatClient("Alice", "sync-room", "pw", store_chain=False)
    alice.chain = RoomChain("sync-room", store_dir=str(tmp_path / "alice"))
    alice.client = MagicMock()
    alice.safe_print = MagicMock()
    alice.write_to_log = MagicMock()

    c1 = encrypt_payload("first msg", alice.secret_key, aad=alice.msg_topic.encode("utf-8"))
    c2 = encrypt_payload("second msg", alice.secret_key, aad=alice.msg_topic.encode("utf-8"))
    alice.chain.append_message("id1", 10.0, "02:00:00", "Alice", "message", c1)
    alice.chain.append_message("id2", 11.0, "02:00:01", "Alice", "message", c2)

    bob = ChatClient("Bob", "sync-room", "pw", store_chain=False)
    bob.chain = RoomChain("sync-room", store_dir=str(tmp_path / "bob"))
    bob.client = MagicMock()
    bob.safe_print = MagicMock()
    bob.write_to_log = MagicMock()

    # Bob requests sync -> Alice responds with missing blocks
    bob.request_sync()
    sync_req = json.loads(bob.client.publish.call_args[0][1])
    alice.handle_sync("Bob", "sync_request", sync_req, alice.sync_topic)

    sync_resp = json.loads(alice.client.publish.call_args[0][1])
    assert sync_resp["target"] == "Bob"
    assert len(sync_resp["blocks"]) == 2

    # Bob merges and decrypts Alice's synced blocks
    bob.handle_sync("Alice", "sync_response", sync_resp, bob.sync_topic)
    assert bob.chain.get_known_ids() == ["id1", "id2"]
    assert bob.chain.verify_integrity() == (True, 2)
