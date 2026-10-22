"""Canonical signed records and validation at the storage boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import ERASURE_THRESHOLD, SERVER_COUNT

OWNER_PREFIX = b"BFR-OWNER-v1\0"
BLOCK_PREFIX = b"BFR-BLOCK-v1\0"
FILE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
HEX_32 = re.compile(r"^[0-9a-f]{64}$")


class InvalidRecord(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str, limit: int = 2 * 1024 * 1024) -> bytes:
    if not isinstance(text, str) or len(text) > limit * 2:
        raise InvalidRecord("base64 field exceeds limit")
    try:
        result = base64.b64decode(text, validate=True)
    except Exception as error:
        raise InvalidRecord("invalid base64 field") from error
    if len(result) > limit:
        raise InvalidRecord("decoded field exceeds limit")
    return result


def owner_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def validate_file_id(file_id: str) -> str:
    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
        raise InvalidRecord("file identifier is invalid")
    return file_id


def key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def public_bytes(private_key: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def load_public(raw: bytes) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(raw)


def sign_update(body: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    return {"body": body, "signature": b64(private_key.sign(OWNER_PREFIX + canonical(body)))}


def update_hash(signed: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(signed)).hexdigest()


def block_message(owner: str, file_id: str, version: int, stripe: int, share_id: int, digest: str) -> bytes:
    return BLOCK_PREFIX + canonical({
        "owner": owner,
        "file": file_id,
        "version": version,
        "stripe": stripe,
        "share_id": share_id,
        "sha256": digest,
    })


def validate_update(signed: dict[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(signed, dict) or not isinstance(signed.get("body"), dict):
            raise InvalidRecord("signed update must be an object")
        body = signed["body"]
        public_key = bytes.fromhex(body["public_key"])
        owner = body["owner"]
        file_id = body["file"]
        version = body["version"]
        parent = body["parent"]
        action = body["action"]
        size = body["size"]
        stripes = body["stripes"]
        signature = unb64(signed["signature"], 128)
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidRecord("malformed signed update") from error
    if len(public_key) != 32 or body["public_key"] != public_key.hex() or owner != owner_id(public_key):
        raise InvalidRecord("owner namespace or file identifier is invalid")
    validate_file_id(file_id)
    if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= 256:
        raise InvalidRecord("version is outside the bounded range")
    if (version == 1 and parent is not None) or (version > 1 and (not isinstance(parent, str) or not HEX_32.fullmatch(parent))):
        raise InvalidRecord("parent head is invalid")
    if action not in ("put", "delete") or not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= 1024 * 1024:
        raise InvalidRecord("operation or file size is invalid")
    if not isinstance(stripes, list):
        raise InvalidRecord("stripe manifest must be an array")
    if action == "delete" and (size != 0 or stripes):
        raise InvalidRecord("tombstone contains file data")
    if action == "put" and (len(stripes) > 64 or (size == 0 and stripes) or (size > 0 and not stripes)):
        raise InvalidRecord("stripe count does not match file size")
    try:
        load_public(public_key).verify(signature, OWNER_PREFIX + canonical(body))
    except Exception as error:
        raise InvalidRecord("owner signature is invalid") from error
    for stripe_index, stripe in enumerate(stripes):
        if not isinstance(stripe, dict) or not isinstance(stripe.get("length"), int) or isinstance(stripe.get("length"), bool) or not 1 <= stripe["length"] <= 128 * 1024:
            raise InvalidRecord("stripe length is invalid")
        blocks = stripe.get("blocks")
        if not isinstance(blocks, list) or len(blocks) != SERVER_COUNT:
            raise InvalidRecord("stripe manifest has wrong number of shares")
        ids = set()
        for descriptor in blocks:
            if not isinstance(descriptor, dict):
                raise InvalidRecord("block descriptor is malformed")
            share_id, digest = descriptor.get("share_id"), descriptor.get("sha256")
            if not isinstance(share_id, int) or isinstance(share_id, bool) or share_id not in range(SERVER_COUNT) or share_id in ids or not isinstance(digest, str) or not HEX_32.fullmatch(digest):
                raise InvalidRecord("block identity or digest is invalid")
            ids.add(share_id)
            try:
                load_public(public_key).verify(
                    unb64(descriptor["signature"], 128),
                    block_message(owner, file_id, version, stripe_index, share_id, digest),
                )
            except Exception as error:
                raise InvalidRecord("block signature is invalid") from error
    if action == "put" and sum(stripe["length"] for stripe in stripes) != size:
        raise InvalidRecord("stripe lengths do not equal file size")
    return body


def validate_history(history: list[dict[str, Any]], expected_owner: str | None = None, expected_file: str | None = None) -> str | None:
    if not isinstance(history, list) or len(history) > 256:
        raise InvalidRecord("history exceeds the bounded version limit")
    previous = None
    for number, signed in enumerate(history, start=1):
        body = validate_update(signed)
        if body["version"] != number or body["parent"] != previous:
            raise InvalidRecord("history is not a continuous signed parent chain")
        if expected_owner is not None and body["owner"] != expected_owner:
            raise InvalidRecord("history belongs to a different owner")
        if expected_file is not None and body["file"] != expected_file:
            raise InvalidRecord("history belongs to a different file")
        previous = update_hash(signed)
    return previous


def checkpoint_relation(left: dict[str, Any], right: dict[str, Any]) -> str:
    validate_checkpoint(left)
    validate_checkpoint(right)
    if left["owner"] != right["owner"] or left["file"] != right["file"]:
        raise InvalidRecord("checkpoints refer to different files")
    left_head, right_head = left["head"], right["head"]
    if left_head == right_head:
        return "same"
    if left_head in right["chain"]:
        return "right-extends-left"
    if right_head in left["chain"]:
        return "left-extends-right"
    return "divergent"


def validate_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        raise InvalidRecord("checkpoint must be an object")
    owner, file_id = checkpoint.get("owner"), checkpoint.get("file")
    version, head, chain = checkpoint.get("version"), checkpoint.get("head"), checkpoint.get("chain")
    if not isinstance(owner, str) or not HEX_32.fullmatch(owner):
        raise InvalidRecord("checkpoint owner is invalid")
    validate_file_id(file_id)
    if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= 256:
        raise InvalidRecord("checkpoint version is invalid")
    if not isinstance(chain, list) or len(chain) != version or any(not isinstance(item, str) or not HEX_32.fullmatch(item) for item in chain) or chain[-1] != head:
        raise InvalidRecord("checkpoint chain is invalid")
    history = checkpoint.get("history")
    if history is not None:
        if len(history) != version:
            raise InvalidRecord("checkpoint signed history length is invalid")
        validate_history(history, owner, file_id)
        if [update_hash(update) for update in history] != chain:
            raise InvalidRecord("checkpoint signed history does not match its digest chain")
    return checkpoint
