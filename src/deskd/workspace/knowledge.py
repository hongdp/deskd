"""Role-owned durable notes with explicit, audited sharing and provenance.

This is searchable text memory, not a source of policy, capabilities or verified
truth. Gateway-authenticated actors own their notes. Sharing is deliberate, and
any revision becomes private until explicitly published again. Forget removes
the note from this live memory index and deletes its memory revisions. The
gateway audit journal retains original memory-write arguments; source snapshots,
prior deliveries, SQLite storage remnants and backups are separate records.
This operation is not secure erasure and does not rewrite those records.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from deskd.workspace.sources import WorkspaceSources, verify_source_references
from deskd.workspace.store import WorkspaceError, WorkspaceStore, _integer, _json, _text


_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_memories(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES seats(principal),
 version INTEGER NOT NULL DEFAULT 1, shared INTEGER NOT NULL DEFAULT 0,
 forgotten INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_revisions(
 memory_id TEXT NOT NULL REFERENCES workspace_memories(id), version INTEGER NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, sources TEXT NOT NULL, digest TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(memory_id,version));
CREATE INDEX IF NOT EXISTS workspace_memory_owner ON workspace_memories(owner,forgotten,id);
"""

_CURRENT = """SELECT m.*,r.title,r.body,r.sources,r.digest
 FROM workspace_memories m JOIN memory_revisions r ON r.memory_id=m.id AND r.version=m.version """
MAX_MEMORY_BODY_BYTES = 16_384
MAX_MEMORY_PAGE_BYTES = 65_536


class WorkspaceKnowledge:
    def __init__(self, store: WorkspaceStore):
        self.store = store
        WorkspaceSources(store)
        with store._connect(write=True) as conn:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)

    @staticmethod
    def _content(title: str, body: str, sources: object) -> tuple[str, str, list, str]:
        _text(title, "memory_title", 256)
        _text(body, "memory_body", MAX_MEMORY_BODY_BYTES)
        if type(sources) is not list or len(sources) > 8:
            raise WorkspaceError("invalid_memory_sources")
        # Validate the narrow reference shape before canonicalizing arbitrary input.
        for ref in sources:
            if type(ref) is not dict or set(ref) != {"job_id", "digest"}:
                raise WorkspaceError("invalid_memory_sources")
            _text(ref["job_id"], "source_job_id")
            _text(ref["digest"], "source_digest", 64)
        digest = hashlib.sha256(_json([title, body, sources]).encode("utf-8")).hexdigest()
        return title, body, sources, digest

    def _read(self, actor: str, row) -> dict:
        value = dict(row)
        sources = json.loads(value["sources"])
        expected = hashlib.sha256(_json([value["title"], value["body"], sources]).encode("utf-8")).hexdigest()
        if expected != value["digest"]:
            raise WorkspaceError("memory_digest_mismatch")
        value["sources"] = sources
        value["evidence"] = verify_source_references(self.store, actor, sources, require_shared=bool(value["shared"]))
        value["trust"] = "untrusted_content"
        return value

    @staticmethod
    def _summary(row) -> dict:
        return {key: row[key] for key in ("id", "owner", "version", "shared", "forgotten")}

    def get(self, actor: str, memory_id: str) -> dict:
        _text(memory_id, "memory_id")
        with self.store._connect() as conn:
            self.store._seat(conn, actor)
            row = conn.execute(_CURRENT + "WHERE m.id=? AND m.forgotten=0 AND (m.owner=? OR (m.shared=1 AND substr(m.owner,1,instr(m.owner,'/')-1)=?))",
                               (memory_id, actor, actor.split("/", 1)[0])).fetchone()
            if row is None:
                raise WorkspaceError("memory_not_found")
            return self._read(actor, row)

    def search(self, actor: str, query: str = "", *, include_shared: bool = True, limit: int = 20) -> dict:
        if type(query) is not str or len(query.encode("utf-8")) > 512 or "\x00" in query:
            raise WorkspaceError("invalid_memory_query")
        if type(include_shared) is not bool:
            raise WorkspaceError("invalid_include_shared")
        _integer(limit, "limit", 1, 50)
        with self.store._connect() as conn:
            self.store._seat(conn, actor)
            rows = conn.execute(
                _CURRENT + "WHERE m.forgotten=0 AND (m.owner=? OR (?=1 AND m.shared=1 AND substr(m.owner,1,instr(m.owner,'/')-1)=?)) "
                "AND (instr(lower(r.title),lower(?))>0 OR instr(lower(r.body),lower(?))>0) "
                "ORDER BY m.updated_at DESC,m.id LIMIT ?", (actor, int(include_shared), actor.split("/", 1)[0], query, query, limit + 1),
            ).fetchall()
            memories = []
            for row in rows[:limit]:
                value = self._read(actor, row)
                if memories and len(_json([*memories, value]).encode("utf-8")) > MAX_MEMORY_PAGE_BYTES:
                    break
                memories.append(value)
            return {"memories": memories, "has_more": len(rows) > len(memories), "trust": "untrusted_content"}

    def remember(self, actor: str, title: str, body: str, *, sources: list | None = None, request_id: str) -> dict:
        sources = [] if sources is None else sources
        title, body, sources, digest = self._content(title, body, sources)
        with self.store._connect(write=True) as conn:
            self.store._seat(conn, actor)
            fingerprint, old = self.store._receipt(conn, actor, request_id, ["memory.remember", title, body, sources])
            if old is not None:
                return old
            verify_source_references(self.store, actor, sources)
            if conn.execute("SELECT count(*) FROM workspace_memories WHERE owner=? AND forgotten=0", (actor,)).fetchone()[0] >= 1000:
                raise WorkspaceError("memory_quota_exceeded")
            memory_id, now = uuid.uuid4().hex, self.store._now()
            conn.execute("INSERT INTO workspace_memories(id,owner,created_at,updated_at) VALUES(?,?,?,?)", (memory_id, actor, now, now))
            conn.execute("INSERT INTO memory_revisions VALUES(?,1,?,?,?,?,?)", (memory_id, title, body, _json(sources), digest, now))
            self.store._event(conn, "memory.remembered", actor, memory_id)
            row = conn.execute("SELECT * FROM workspace_memories WHERE id=?", (memory_id,)).fetchone()
            return self.store._save_receipt(conn, actor, request_id, fingerprint, self._summary(row))

    def _owned(self, conn, actor: str, memory_id: str, expected_version: int):
        row = conn.execute("SELECT * FROM workspace_memories WHERE id=? AND owner=? AND forgotten=0", (memory_id, actor)).fetchone()
        if row is None:
            raise WorkspaceError("memory_not_owned")
        if row["version"] != expected_version:
            raise WorkspaceError("version_conflict")
        return row

    def revise(self, actor: str, memory_id: str, *, expected_version: int,
               title: str, body: str, sources: list | None = None, request_id: str) -> dict:
        _text(memory_id, "memory_id")
        _integer(expected_version, "expected_version")
        sources = [] if sources is None else sources
        title, body, sources, digest = self._content(title, body, sources)
        with self.store._connect(write=True) as conn:
            self.store._seat(conn, actor)
            fingerprint, old = self.store._receipt(conn, actor, request_id, ["memory.revise", memory_id, expected_version, title, body, sources])
            if old is not None:
                return old
            self._owned(conn, actor, memory_id, expected_version)
            verify_source_references(self.store, actor, sources)
            version, now = expected_version + 1, self.store._now()
            conn.execute("UPDATE workspace_memories SET version=?,shared=0,updated_at=? WHERE id=?", (version, now, memory_id))
            conn.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)", (memory_id, version, title, body, _json(sources), digest, now))
            self.store._event(conn, "memory.revised", actor, memory_id)
            row = conn.execute("SELECT * FROM workspace_memories WHERE id=?", (memory_id,)).fetchone()
            return self.store._save_receipt(conn, actor, request_id, fingerprint, self._summary(row))

    def publish(self, actor: str, memory_id: str, *, expected_version: int, request_id: str) -> dict:
        _text(memory_id, "memory_id")
        _integer(expected_version, "expected_version")
        with self.store._connect(write=True) as conn:
            self.store._seat(conn, actor)
            fingerprint, old = self.store._receipt(conn, actor, request_id, ["memory.publish", memory_id, expected_version])
            if old is not None:
                return old
            self._owned(conn, actor, memory_id, expected_version)
            revision = conn.execute("SELECT * FROM memory_revisions WHERE memory_id=? AND version=?", (memory_id, expected_version)).fetchone()
            verify_source_references(self.store, actor, json.loads(revision["sources"]), require_shared=True)
            version, now = expected_version + 1, self.store._now()
            conn.execute("UPDATE workspace_memories SET shared=1,version=?,updated_at=? WHERE id=?", (version, now, memory_id))
            conn.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                         (memory_id, version, revision["title"], revision["body"], revision["sources"], revision["digest"], now))
            self.store._event(conn, "memory.published", actor, memory_id)
            row = conn.execute("SELECT * FROM workspace_memories WHERE id=?", (memory_id,)).fetchone()
            return self.store._save_receipt(conn, actor, request_id, fingerprint, self._summary(row))

    def forget(self, actor: str, memory_id: str, *, expected_version: int, request_id: str) -> dict:
        _text(memory_id, "memory_id")
        _integer(expected_version, "expected_version")
        with self.store._connect(write=True) as conn:
            self.store._seat(conn, actor)
            fingerprint, old = self.store._receipt(conn, actor, request_id, ["memory.forget", memory_id, expected_version])
            if old is not None:
                return old
            self._owned(conn, actor, memory_id, expected_version)
            conn.execute("UPDATE workspace_memories SET forgotten=1,shared=0,version=version+1,updated_at=? WHERE id=?", (self.store._now(), memory_id))
            conn.execute("DELETE FROM memory_revisions WHERE memory_id=?", (memory_id,))
            self.store._event(conn, "memory.forgotten", actor, memory_id)
            row = conn.execute("SELECT * FROM workspace_memories WHERE id=?", (memory_id,)).fetchone()
            return self.store._save_receipt(conn, actor, request_id, fingerprint, self._summary(row))
