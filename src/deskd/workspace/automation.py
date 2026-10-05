"""Trusted composition of finite goals, evidence, memory and human attention.

Role writes arrive only through the authenticated gateway outbox. Management
methods are a separate, closed protocol. No model or arbitrary tool is invoked
by this deterministic coordinator.
"""

from __future__ import annotations

import json
import sqlite3

from deskd.gateway.commands import CommandHandler
from deskd.gateway.identity import IdentityError
from .extensions import ExtendedExchange
from .goals import GoalEngine, _ids
from .knowledge import WorkspaceKnowledge
from .notifications import NotificationStore, NotificationDispatcher, WebhookTarget
from .operations import diagnostics
from .sources import WorkspaceSources
from .store import WorkspaceError, _integer, _number


class WorkspaceAutomation:
    def __init__(self, store, gateway_db):
        self.store, self.gateway_db = store, gateway_db
        self.sources = WorkspaceSources(store)
        self.knowledge = WorkspaceKnowledge(store)
        self.notifications = NotificationStore(store)
        with store._connect(write=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS workspace_notification_target(singleton INTEGER PRIMARY KEY CHECK(singleton=1),url TEXT NOT NULL,enabled INTEGER NOT NULL)")
        self.goals = GoalEngine(store, artifact_reader=self.artifact, evidence_reader=self.sources.get)
        self.exchange = ExtendedExchange(store, self.goals, self.sources, self.knowledge)
        with sqlite3.connect(gateway_db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS workspace_cancelled_proposals("
                         "proposal_id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,cancelled_at REAL NOT NULL)")

    def artifact(self, kind, artifact_id):
        statements = {
            "proposal": "SELECT * FROM memo_proposals WHERE proposal_id=?",
            "approval": "SELECT * FROM memo_approvals WHERE approval_id=?",
            "memo": "SELECT * FROM memo_published WHERE memo_id=?",
            "memo_for_proposal": "SELECT * FROM memo_published WHERE proposal_id=?",
        }
        if kind not in statements:
            raise WorkspaceError("unknown_artifact_kind")
        with sqlite3.connect(self.gateway_db.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(statements[kind], (artifact_id,)).fetchone()
            return dict(row) if row is not None else None

    def memo_handlers(self, handlers):
        def protect(name, handler):
            def apply(conn, args, identity):
                proposal = args.get("proposal_id")
                if name == "action.execute":
                    approval = args.get("approval_id")
                    if type(approval) is str:
                        row = conn.execute("SELECT proposal_id FROM memo_approvals WHERE approval_id=?", (approval,)).fetchone()
                        proposal = row[0] if row else None
                if type(proposal) is str and conn.execute(
                    "SELECT 1 FROM workspace_cancelled_proposals WHERE proposal_id=?", (proposal,)
                ).fetchone():
                    raise IdentityError("goal_cancelled")
                return handler.apply(conn, args, identity)
            return CommandHandler(handler.event_type, apply)
        return {name: protect(name, handler) if name in {"approval.issue", "action.execute"} else handler
                for name, handler in handlers.items()}

    def update_goal(self, **params):
        # The gateway barrier commits first. A partial failure can suppress work,
        # never leave a cancelled goal's retained proposal newly executable.
        _integer(params["expected_version"], "expected_version")
        with self.store._connect() as conn:
            _, old = self.store._receipt(conn, "@supervisor", params["request_id"],
                                         ["goal.update", params["goal_id"], params["action"], params["expected_version"]])
        if old is not None:
            return old
        if params.get("action") == "cancel":
            goal = self.goals.read(goal_id=params["goal_id"])["goals"][0]
            if goal["version"] == params["expected_version"] and goal["state"] not in {"completed", "cancelled"}:
                proposal_id = goal["cycles"][0]["proposal_id"]
                if proposal_id:
                    with sqlite3.connect(self.gateway_db, timeout=5) as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute("INSERT OR IGNORE INTO workspace_cancelled_proposals VALUES(?,?,?)",
                                     (proposal_id, goal["id"], self.store._now()))
                        conn.execute("UPDATE memo_approvals SET status='revoked',revoked_by='@supervisor',revoked_at=? "
                                     "WHERE proposal_id=? AND status='active'", (self.store._now(), proposal_id))
        return self.goals.update(**params)

    def create_goal(self, **params):
        # Configuration and capabilities are prerequisites, not promises from
        # a role's text. No goal silently waits on a nonexistent source/reviewer.
        params["source_ids"] = _ids(params["source_ids"], "source_ids")
        params["followup_seconds"] = _number(params["followup_seconds"], "followup_seconds")
        if params["interval_seconds"] is not None:
            params["interval_seconds"] = _number(params["interval_seconds"], "interval_seconds")
        with self.store._connect() as conn:
            _, old = self.store._receipt(conn, "@supervisor", params["request_id"], [
                "goal.create", params["title"], params["objective"], params["researcher"],
                params["reviewer"], params["executor"], params["source_ids"],
                params["interval_seconds"], params["max_cycles"], params["followup_seconds"], params["max_followups"],
            ])
        if old is not None:
            return old
        sources = params.get("source_ids")
        if type(sources) is not list or not sources or any(type(x) is not str for x in sources):
            raise WorkspaceError("goal_sources_required")
        with self.store._connect() as conn:
            known = {r[0] for r in conn.execute("SELECT name FROM workspace_sources WHERE enabled=1")}
        if not set(sources) <= known:
            raise WorkspaceError("unknown_source")
        with sqlite3.connect(self.gateway_db.as_uri() + "?mode=ro", uri=True) as conn:
            common = {"goal.read", "goal.report", "goal.ask", "inbox.read", "inbox.ack", "tasks.read"}
            requirements = {
                "researcher": {"proposal.create", "source.list", "source.request", "source.read", "source.publish", "workspace.receipt"},
                "reviewer": {"approval.issue", "source.read", "workspace.receipt"},
                "executor": {"action.execute", "source.read", "workspace.receipt"},
            }
            for field, capabilities in requirements.items():
                principal = params.get(field)
                if type(principal) is not str:
                    raise WorkspaceError("invalid_principal")
                row = conn.execute("SELECT status,capabilities FROM bindings WHERE principal=?", (principal,)).fetchone()
                if row is None or row[0] != "bound" or not (common | capabilities) <= set(json.loads(row[1])):
                    raise WorkspaceError("goal_participant_not_authorized")
        return self.goals.create(**params)

    def shared_memory(self, *, query="", limit=20):
        if type(query) is not str or len(query.encode("utf-8")) > 512 or "\x00" in query:
            raise WorkspaceError("invalid_memory_query")
        _integer(limit, "limit", 1, 20)
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT m.*,r.title,r.body,r.sources,r.digest FROM workspace_memories m "
                "JOIN memory_revisions r ON r.memory_id=m.id AND r.version=m.version "
                "WHERE m.shared=1 AND m.forgotten=0 AND (instr(lower(r.title),lower(?))>0 OR instr(lower(r.body),lower(?))>0) "
                "ORDER BY m.updated_at DESC,m.id LIMIT ?", (query, query, limit + 1)).fetchall()
            values, size = [], 0
            for row in rows[:limit]:
                value = self.knowledge._read(row["owner"], row)
                size += len(json.dumps(value).encode("utf-8"))
                if values and size > 128 * 1024:
                    break
                values.append(value)
            return {"memories": values, "has_more": len(rows) > len(values), "trust": "untrusted_content"}

    def snapshot(self):
        with self.store._connect() as conn:
            sources = [dict(r) for r in conn.execute("SELECT * FROM workspace_sources ORDER BY name LIMIT 100")]
            source_counts = {r[0]: r[1] for r in conn.execute("SELECT state,count(*) FROM source_jobs GROUP BY state")}
            goal_counts = {r[0]: r[1] for r in conn.execute("SELECT state,count(*) FROM workspace_goals GROUP BY state")}
        result = self.goals.read()
        goals = result["goals"]
        # Whole records only, with an explicit omission flag for a bounded RPC.
        truncated = result.get("has_more", False)
        while len(json.dumps(goals).encode("utf-8")) > 384 * 1024:
            goals.pop()
            truncated = True
        snapshot = self.store.snapshot()
        snapshot.update(gateway_fenced=not self.active(), live_observation=True)
        return {"goals": goals, "goals_truncated": truncated,
                "notifications": self.notifications.list_notifications(),
                "sources": sources, "knowledge": self.shared_memory(limit=10)["memories"],
                "health": diagnostics(snapshot, notification_counts=self.notifications.counters(),
                                      source_counts=source_counts, goal_counts=goal_counts)}

    def active(self):
        if self.store.snapshot()["service"]["active"] != 1:
            return False
        with sqlite3.connect(self.gateway_db.as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT active FROM service WHERE singleton=1").fetchone()
            return bool(row and row[0])

    def tick(self):
        if self.active():
            self.goals.tick()
        # Import notices atomically with their projection marker. Acknowledged
        # notices never reappear simply because this function is called again.
        with self.store._connect(write=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS workspace_attention_projection(id TEXT PRIMARY KEY)")
            rows = conn.execute("SELECT n.* FROM workspace_goal_notices n LEFT JOIN workspace_attention_projection p ON p.id=n.id "
                                "WHERE p.id IS NULL ORDER BY n.created_at,n.id LIMIT 100").fetchall()
            for row in rows:
                self.notifications.emit(row["kind"], row["goal_id"], row["id"], "目标需要关注" if row["kind"] != "completion" else "成果已交付", row["body"])
                conn.execute("INSERT INTO workspace_attention_projection VALUES(?)", (row["id"],))
            for row in conn.execute("SELECT id,owner,error FROM source_jobs WHERE state='failed' ORDER BY completed_at DESC LIMIT 100"):
                self.notifications.emit("error", "source:" + row["id"], "failed", "信息源获取失败",
                                        "获取任务未完成，可检查信息源后重新发起。", principal=row["owner"])
            for row in conn.execute("SELECT principal,budget_turns FROM seats WHERE revoked=0 AND turns_used>=budget_turns"):
                self.notifications.emit("budget", "seat:" + row["principal"], str(row["budget_turns"]), "自动工作次数已用完",
                                        "该角色需要补充自动工作次数后才能继续。", principal=row["principal"])
            for row in conn.execute("SELECT id,sender FROM operator_messages WHERE is_read=0 ORDER BY created_at LIMIT 100"):
                self.notifications.emit("decision", "message:" + str(row["id"]), "unread", "角色发来了消息",
                                        "请在收件箱查看并回复。", principal=row["sender"])

    def configure_notifications(self, *, url, enabled):
        if type(enabled) is not bool:
            raise WorkspaceError("invalid_notification_setting")
        WebhookTarget(url, approved=True)  # Explicit protected management operation.
        with self.store._connect(write=True) as conn:
            conn.execute("INSERT INTO workspace_notification_target VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET url=excluded.url,enabled=excluded.enabled", (url, int(enabled)))
            self.store._event(conn, "notifications.configured", "@supervisor", "enabled" if enabled else "disabled")
        return {"enabled": enabled, "payload": "generic_metadata_only"}

    def dispatch_notifications(self):
        with self.store._connect() as conn:
            row = conn.execute("SELECT url FROM workspace_notification_target WHERE enabled=1").fetchone()
        if row:
            return NotificationDispatcher(self.notifications, WebhookTarget(row[0], approved=True)).dispatch(limit=1)
        return {"attempted": 0, "delivered": 0, "unconfirmed": 0}

    def handlers(self):
        specs = {
            "workspace.console.extended": (self.snapshot, set()),
            "workspace.goal.create": (self.create_goal, {"title", "objective", "researcher", "reviewer", "executor", "source_ids", "request_id", "interval_seconds", "max_cycles", "followup_seconds", "max_followups"}),
            "workspace.goal.update": (self.update_goal, {"goal_id", "action", "expected_version", "request_id"}),
            "workspace.goal.answer": (self.goals.answer, {"goal_id", "body", "expected_version", "request_id"}),
            "workspace.source.configure": (self.sources.configure, {"name", "url", "max_bytes", "timeout_seconds"}),
            "workspace.source.disable": (lambda **kw: self.sources.disable(**kw), {"name", "expected_version"}),
            "workspace.notification.ack": (self.notifications.acknowledge, {"notification_ids"}),
            "workspace.notification.configure": (self.configure_notifications, {"url", "enabled"}),
            "workspace.memory.search": (self.shared_memory, {"query", "limit"}),
        }
        def wrap(callback, fields):
            def invoke(params):
                if type(params) is not dict or set(params) != fields:
                    raise WorkspaceError("invalid_management_params")
                return callback(**params)
            return invoke
        return {name: wrap(callback, fields) for name, (callback, fields) in specs.items()}
