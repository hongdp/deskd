#!/usr/bin/env python3
"""Conflict-safe transactions for agent-orchestration/KNOWLEDGE.md."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid


SKILL_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE = SKILL_DIR / "KNOWLEDGE.md"
LOCK = SKILL_DIR / ".KNOWLEDGE.md.lock"
STATE_DIR = Path.home() / ".cache" / "agent-orchestration-knowledge-txns"
TXN_RE = re.compile(r"^[0-9a-f]{32}$")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def txn_paths(txn_id: str) -> tuple[Path, Path, Path]:
    if not TXN_RE.fullmatch(txn_id):
        raise SystemExit("Invalid transaction id")
    root = STATE_DIR / txn_id
    return root, root / "meta.json", root / "KNOWLEDGE.md"


def locked_read() -> bytes:
    LOCK.touch(exist_ok=True)
    with LOCK.open("rb") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH)
        return KNOWLEDGE.read_bytes()


def begin(actor: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    txn_id = uuid.uuid4().hex
    root, meta_path, draft_path = txn_paths(txn_id)
    root.mkdir(mode=0o700)
    original = locked_read()
    draft_path.write_bytes(original)
    meta_path.write_text(
        json.dumps(
            {
                "transaction": txn_id,
                "actor": actor,
                "base_sha256": digest(original),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"TXN_ID={txn_id}")
    print(f"DRAFT={draft_path}")
    print(f"BASE_SHA256={digest(original)}")
    print("Edit DRAFT only, then run: knowledge_txn.py commit TXN_ID")


def validate(draft: bytes) -> None:
    if not draft.strip():
        raise SystemExit("Refusing to commit an empty knowledge file")
    text = draft.decode("utf-8")
    if not text.lstrip().startswith("#"):
        raise SystemExit("Refusing to commit: KNOWLEDGE.md must start with a heading")
    if len(text.splitlines()) > 150:
        raise SystemExit("Refusing to commit: KNOWLEDGE.md exceeds the 150-line limit")


def commit(txn_id: str) -> None:
    root, meta_path, draft_path = txn_paths(txn_id)
    if not meta_path.is_file() or not draft_path.is_file():
        raise SystemExit(f"Unknown or incomplete transaction: {txn_id}")

    meta = json.loads(meta_path.read_text())
    draft = draft_path.read_bytes()
    validate(draft)

    LOCK.touch(exist_ok=True)
    with LOCK.open("rb") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        current = KNOWLEDGE.read_bytes()
        current_hash = digest(current)
        if current_hash != meta["base_sha256"]:
            conflict_copy = root / "KNOWLEDGE.latest.md"
            conflict_copy.write_bytes(current)
            print("CONFLICT: KNOWLEDGE.md changed after this transaction began.")
            print("No content was overwritten.")
            print(f"YOUR_DRAFT={draft_path}")
            print(f"LATEST={conflict_copy}")
            print(
                "Start a new transaction from LATEST, reapply the generalizable "
                "lesson, and commit again."
            )
            raise SystemExit(3)

        mode = KNOWLEDGE.stat().st_mode & 0o777
        fd, tmp_name = tempfile.mkstemp(prefix=".KNOWLEDGE.md.", dir=SKILL_DIR)
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(draft)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, KNOWLEDGE)
            dir_fd = os.open(SKILL_DIR, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    (root / "committed").write_text(
        json.dumps(
            {
                "committed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "sha256": digest(draft),
            }
        )
        + "\n"
    )
    print(f"COMMITTED={txn_id}")
    print(f"SHA256={digest(draft)}")


def status(txn_id: str) -> None:
    root, meta_path, draft_path = txn_paths(txn_id)
    if not meta_path.is_file() or not draft_path.is_file():
        raise SystemExit(f"Unknown or incomplete transaction: {txn_id}")
    meta = json.loads(meta_path.read_text())
    current = locked_read()
    print(f"TXN_ID={txn_id}")
    print(f"STATE={'committed' if (root / 'committed').exists() else 'open'}")
    print(f"BASE_SHA256={meta['base_sha256']}")
    print(f"CURRENT_SHA256={digest(current)}")
    print(f"DRAFT_SHA256={digest(draft_path.read_bytes())}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    begin_parser = subparsers.add_parser("begin")
    begin_parser.add_argument(
        "--actor", default="agent", help="Short audit label, e.g. codex or claude"
    )

    for command in ("commit", "status"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("transaction")

    args = parser.parse_args()
    if args.command == "begin":
        begin(args.actor)
    elif args.command == "commit":
        commit(args.transaction)
    else:
        status(args.transaction)


if __name__ == "__main__":
    main()
