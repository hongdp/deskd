"""deskd command line — the agent-facing surface of the orchestration engine.

Every subcommand here is a thin argparse -> engine-call -> ``json.dumps``
adapter. Deliberately thin: the CLI must never be the place a rule lives,
because the Web console and any host application call the very same engine
functions and would not inherit a rule enforced here. If you are tempted to
validate something in this file, validate it in the engine instead.

Two properties are worth stating explicitly, because they are easy to break:

Roles are NOT an argparse ``choices`` list.
    The ``agent_registry`` table is the only source of truth for which roles
    exist, and a host may add, rename, or disable one at runtime. So every
    ``--role``/``--for``/``--by`` flag takes a free string, and the engine
    resolves it against the registry and raises ``ValueError`` for anything
    unknown or disabled. Baking role names into the parser would silently fork
    the truth (and would reintroduce the bug the registry exists to prevent:
    a role that exists in code but not in the database, or vice versa).
    ``--help`` therefore cannot enumerate roles; ``deskd status show`` does.

``ValueError`` is the engine's "you asked for something illegal" channel.
    It is a rejection, not a crash: print ``REJECTED: <why>`` and exit 1, never
    a traceback. Callers (scripts, hooks, cron) branch on the exit code, so a
    rejection must never be confused with success. Every dispatch below runs
    inside that guard.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import NoReturn

from .config import CONFIG, PROJECT_NAME, __version__, env

# --- engine-level enumerations ----------------------------------------------
# These are domain-agnostic engine vocabulary, not roles, so they are safe to
# fix in the parser. Anything that names a *role* or a host-extensible set is
# derived from CONFIG instead (see below), never hardcoded.

SESSION_STATES = ("booting", "working", "idle_standby", "in_meeting",
                  "stopping", "dead")
TASK_STATUSES = ("pending", "in_progress", "blocked", "done", "cancelled")
PRIORITIES = ("urgent", "normal", "low")
MEETING_TYPES = ("live", "review", "ad-hoc")
MEETING_KINDS = ("evidence", "question", "answer", "proposal", "decision")
REVIEW_STAGES = ("report", "review", "finalize")

# NOTE: escalation channels are deliberately NOT a choices list. The engine
# ships no channels beyond `auto` and `outbox`; a host registers its own at
# startup (meetings.register_channel), so the valid set is only known at
# runtime — exactly like roles. The engine rejects an unknown channel and names
# the ones it knows.


def _task_sources() -> tuple[str, ...]:
    """Task provenance. ``supervisor_role`` is configurable, so this set is
    built at parse time rather than frozen as a literal."""
    return (CONFIG.supervisor_role, "meeting", "self", "system")


# --- output -----------------------------------------------------------------

def _emit(out) -> None:
    """One JSON document per invocation, on stdout. ``default=str`` keeps
    stray datetimes/Paths from turning an engine result into a crash."""
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


def _role_hint() -> str:
    """Registered roles, for rejection messages — since ``--help`` cannot list
    them. Best-effort: if the database is unreachable the hint is simply
    omitted, because a hint must never itself become an error path."""
    try:
        from . import orchestration as orch
        roles = ", ".join(sorted(a["role"] for a in orch.presence()))
    except Exception:
        return ""
    return f" (registered roles: {roles})" if roles else " (no roles registered)"


def _reject(message: str, *, with_roles: bool = False) -> NoReturn:
    hint = _role_hint() if with_roles else ""
    print(f"REJECTED: {message}{hint}")
    raise SystemExit(1)


# --- parser -----------------------------------------------------------------

def _add_role(parser: argparse.ArgumentParser, *names: str, dest: str = "role",
              required: bool = True, help: str = "agent role (see `%s status show`)"
                                                 % PROJECT_NAME) -> None:
    """Add a role flag. No ``choices=`` — see the module docstring."""
    parser.add_argument(*names, dest=dest, required=required, metavar="ROLE",
                        help=help)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROJECT_NAME,
        description="Domain-agnostic orchestration engine for multi-agent desks.",
    )
    parser.add_argument("--version", action="version",
                        version=f"{PROJECT_NAME} {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- status -------------------------------------------------------------
    p_st = sub.add_parser("status", help="agent presence / current activity")
    st = p_st.add_subparsers(dest="status_cmd", required=True)
    st_set = st.add_parser("set", help="declare/refresh this agent's live status")
    _add_role(st_set, "--role")
    st_set.add_argument("--state", choices=SESSION_STATES)
    st_set.add_argument("--activity", help="one line: what you are doing now")
    st_set.add_argument("--session-id")
    st_set.add_argument("--harness", help="e.g. cron, interactive")
    st.add_parser("show", help="presence for all registered agents")
    st_end = st.add_parser("end", help="mark this agent's session stopped")
    _add_role(st_end, "--role")

    # --- task ---------------------------------------------------------------
    p_tk = sub.add_parser("task", help="cross-session agent work items")
    tk = p_tk.add_subparsers(dest="task_cmd", required=True)
    tk_a = tk.add_parser("add")
    tk_a.add_argument("title")
    _add_role(tk_a, "--for", dest="assignee")
    tk_a.add_argument("--detail")
    tk_a.add_argument("--priority", choices=PRIORITIES, default="normal")
    tk_a.add_argument("--source", choices=_task_sources(), default="self")
    tk_a.add_argument("--ref", help="source reference: meeting/message/inbox id")
    tk_a.add_argument("--due", help="soft deadline ISO ts (visibility/ordering only)")
    tk_a.add_argument("--by", help="creator role/actor")
    tk_l = tk.add_parser("list")
    _add_role(tk_l, "--for", dest="assignee", required=False)
    tk_l.add_argument("--status", choices=TASK_STATUSES)
    tk_l.add_argument("--all", action="store_true", help="include done/cancelled")
    tk_u = tk.add_parser("update")
    tk_u.add_argument("id", type=int)
    tk_u.add_argument("--status", choices=TASK_STATUSES)
    tk_u.add_argument("--priority", choices=PRIORITIES)
    tk_u.add_argument("--due")
    tk_u.add_argument("--detail")
    tk_u.add_argument("--title")
    tk_u.add_argument("--note", help="result note")
    tk_u.add_argument("--blocked-on",
                      help="what this task waits ON; required by --status blocked")
    _add_role(tk_u, "--for", dest="assignee", required=False, help="reassign")
    tk_u.add_argument("--by", help="actor")
    for _tc in ("done", "cancel"):
        _tp = tk.add_parser(_tc)
        _tp.add_argument("id", type=int)
        _tp.add_argument("--note")
        _tp.add_argument("--by", help="actor")

    # --- inbox --------------------------------------------------------------
    p_ib = sub.add_parser("inbox", help="unified agent notification queue")
    ib = p_ib.add_subparsers(dest="inbox_cmd", required=True)
    ib_e = ib.add_parser("enqueue")
    _add_role(ib_e, "--for", dest="target")
    # Host-extensible (CONFIG.inbox_sources), so read at parse time rather than
    # frozen: a host that calls configure() before main() gets its own set.
    ib_e.add_argument("--source", required=True, choices=tuple(CONFIG.inbox_sources))
    ib_e.add_argument("--title", required=True)
    ib_e.add_argument("--body")
    ib_e.add_argument("--ref")
    ib_e.add_argument("--priority", choices=PRIORITIES, default="normal")
    ib_e.add_argument("--dedup-key")
    ib_l = ib.add_parser("list")
    _add_role(ib_l, "--for", dest="target", required=False)
    ib_l.add_argument("--undelivered", action="store_true",
                      help="only items not yet delivered")
    ib_a = ib.add_parser("ack")
    _add_role(ib_a, "--for", dest="target", required=False)
    ib_a.add_argument("--id", type=int, action="append", help="ack specific id(s)")

    # --- wake ---------------------------------------------------------------
    p_wk = sub.add_parser("wake",
                          help="wake orchestrator: run the loop / inspect attempts")
    wk = p_wk.add_subparsers(dest="wake_cmd", required=True)
    wk_t = wk.add_parser("tick",
                         help="run the wake loop (records attempts, prints driver plan)")
    wk_t.add_argument("--dry", action="store_true",
                      help="side-effect-free preview: decide but record nothing")
    wk_l = wk.add_parser("list", help="recent wake attempts")
    wk_l.add_argument("--limit", type=int, default=20)
    wk_s = wk.add_parser(
        "sources",
        help="what can currently wake THIS role (hooks/meetings/inbox/tasks) "
             "+ how to change it")
    _add_role(wk_s, "--role")

    # --- hook ---------------------------------------------------------------
    p_hk = sub.add_parser("hook",
                          help="self-service wake hooks: timers & custom watcher probes")
    hk = p_hk.add_subparsers(dest="hook_cmd", required=True)
    hk_a = hk.add_parser("add", help="register a wake hook (one of --at/--every/--cron/--probe)")
    _add_role(hk_a, "--for", dest="owner")
    hk_a.add_argument("--title", required=True)
    hk_a.add_argument("--body")
    hk_a.add_argument("--priority", choices=PRIORITIES, default="normal")
    hk_a.add_argument("--at", help="one-shot: fire at this ISO timestamp")
    hk_a.add_argument("--every", type=int,
                      help=f"recurring: fire every N seconds (>={CONFIG.min_hook_every})")
    hk_a.add_argument("--cron",
                      help="calendar schedule 'm h dom mon dow' (e.g. '15 6 * * 1-5')")
    hk_a.add_argument("--tz", help=f"timezone for --cron (default {CONFIG.timezone})")
    hk_a.add_argument(
        "--probe",
        help="custom watcher '<module>:<function>', evaluated every --every "
             f"seconds (default {CONFIG.default_probe_every}). The module must "
             "sit under an allowed prefix: "
             + (", ".join(CONFIG.probe_allowlist) if CONFIG.probe_allowlist
                else "NONE — probes are disabled by this host"))
    hk_a.add_argument("--until", help="stop recurring after this ISO timestamp")
    hk_l = hk.add_parser("list")
    _add_role(hk_l, "--for", dest="owner", required=False)
    hk_l.add_argument("--all", action="store_true", help="include done/cancelled/error")
    hk_c = hk.add_parser("cancel")
    hk_c.add_argument("id", type=int)
    hk_c.add_argument("--by", help="actor")

    # --- session ------------------------------------------------------------
    p_ss = sub.add_parser("session", help="session lifecycle (cross-day rollover)")
    ss = p_ss.add_subparsers(dest="session_cmd", required=True)
    ss_r = ss.add_parser("rollover",
                         help="plan wind-down of stale (prior-day) sessions")
    ss_r.add_argument("--dry", action="store_true",
                      help="preview stale sessions without marking them draining")

    # --- delivery -----------------------------------------------------------
    p_dl = sub.add_parser(
        "delivery",
        help="message delivery ledger (queued/notified/read/overdue/escalated)")
    p_dl.add_argument("--meeting", help="filter to one meeting thread id")

    # --- meeting ------------------------------------------------------------
    p_mt = sub.add_parser("meeting", help="bounded multi-agent meetings")
    mt = p_mt.add_subparsers(dest="meeting_cmd", required=True)
    mt_c = mt.add_parser("call")
    _add_role(mt_c, "--by", dest="caller")
    mt_c.add_argument("--agenda", required=True)
    mt_c.add_argument("--attendees",
                      help="comma-separated roles (default: all registered roles)")
    mt_c.add_argument("--type", dest="meeting_type", choices=MEETING_TYPES,
                      default="ad-hoc")
    mt_c.add_argument("--priority", choices=("normal", "urgent"), default="normal")
    mt_c.add_argument("--idle-minutes", type=int, default=60)
    mt_c.add_argument("--max-messages", type=int)
    mt_c.add_argument("--consensus-threshold", type=int, default=4)
    mt_c.add_argument("--wait-timeout-seconds", type=int, default=300)
    mt_d = mt.add_parser("discover")
    _add_role(mt_d, "--role")
    mt_d.add_argument("--all", action="store_true")
    mt_ci = mt.add_parser("check-in")
    mt_ci.add_argument("meeting")
    _add_role(mt_ci, "--role")
    mt_u = mt.add_parser("updates")
    mt_u.add_argument("meeting")
    _add_role(mt_u, "--role")
    mt_u.add_argument("--mark-read", action="store_true")
    mt_u.add_argument("--wait-seconds", type=int, default=0)
    mt_s = mt.add_parser("send")
    mt_s.add_argument("meeting")
    _add_role(mt_s, "--role")
    mt_s.add_argument("--kind", choices=MEETING_KINDS, default="evidence")
    mt_s.add_argument("--body", required=True)
    mt_s.add_argument("--reply-to", type=int)
    mt_s.add_argument("--resolves", type=int, nargs="+", metavar="MSG_ID",
                      help="response obligations this message settles (may be "
                           "several; independent of --reply-to)")
    mt_r = mt.add_parser("resolve",
                         help="settle obligations an earlier message of yours "
                              "already answered, without saying anything new")
    mt_r.add_argument("meeting")
    _add_role(mt_r, "--role")
    mt_r.add_argument("--covered-by", type=int, required=True, metavar="MSG_ID",
                      help="your own message that answered them")
    mt_r.add_argument("--resolves", type=int, nargs="+", required=True,
                      metavar="MSG_ID", help="obligations it covers")
    mt_p = mt.add_parser("position")
    mt_p.add_argument("meeting")
    _add_role(mt_p, "--role")
    mt_p.add_argument("--body", required=True)
    mt_p.add_argument("--reply-to", type=int)
    mt_l = mt.add_parser("leave")
    mt_l.add_argument("meeting")
    _add_role(mt_l, "--role")
    mt_l.add_argument("--reason", required=True)
    mt_pe = mt.add_parser("propose-end")
    mt_pe.add_argument("meeting")
    _add_role(mt_pe, "--role")
    mt_pe.add_argument("--resolution", required=True)
    mt_ce = mt.add_parser("confirm-end")
    mt_ce.add_argument("meeting")
    _add_role(mt_ce, "--role")
    mt_re = mt.add_parser("reject-end")
    mt_re.add_argument("meeting")
    _add_role(mt_re, "--role")
    mt_re.add_argument("--reason", required=True)
    mt_ps = mt.add_parser("pause")
    mt_ps.add_argument("meeting")
    _add_role(mt_ps, "--role")
    mt_ps.add_argument("--reason", required=True)
    mt_e = mt.add_parser("escalate")
    mt_e.add_argument("meeting")
    _add_role(mt_e, "--role")
    mt_e.add_argument("--reason", required=True)
    mt_e.add_argument("--channel", default="auto", metavar="CHANNEL",
                      help="'auto' (every available registered channel), "
                           "'outbox' (the ledger row itself — always available), "
                           "or a channel this host registered")
    mt_e.add_argument("--keep-open", action="store_true")
    mt_es = mt.add_parser("escalations")
    mt_es.add_argument("meeting", nargs="?")
    mt_w = mt.add_parser("wake-list")
    _add_role(mt_w, "--role")
    mt_wa = mt.add_parser("wake-ack")
    mt_wa.add_argument("meeting")
    _add_role(mt_wa, "--role")
    mt_stt = mt.add_parser("status")
    mt_stt.add_argument("meeting")
    # Supervisor actions never enter through an agent-facing path: the payload
    # must carry a valid Ed25519 signature from the root-owned public key, so
    # possession of this subcommand grants nothing on its own.
    mt_b = mt.add_parser(
        "supervisor-apply",
        help="apply a supervisor assertion signed by the external key "
             f"(requires {PROJECT_NAME.upper()}_SUPERVISOR_AUTH_MODE=signed|hybrid)")
    mt_b.add_argument("--assertion", required=True, metavar="PATH",
                      help="path to the raw JSON assertion")
    mt_b.add_argument("--signature", required=True, metavar="PATH",
                      help="path to its Ed25519 signature (raw or base64)")

    # --- review -------------------------------------------------------------
    p_rv = sub.add_parser("review", help="bounded report/cross-review workflow")
    rv = p_rv.add_subparsers(dest="review_cmd", required=True)
    rv_s = rv.add_parser("start")
    rv_s.add_argument("--subject", required=True)
    _add_role(rv_s, "--by", dest="caller")
    rv_s.add_argument("--attendees",
                      help="comma-separated roles (default: all registered roles)")
    rv_s.add_argument("--idle-minutes", type=int, default=1440)
    rv_s.add_argument("--max-messages", type=int, default=40)
    rv_s.add_argument("--max-discussion", type=int, default=6)
    for stage in REVIEW_STAGES:
        rv_a = rv.add_parser(stage)
        rv_a.add_argument("thread")
        _add_role(rv_a, "--role")
        rv_a.add_argument("--file", required=True)
    rv_d = rv.add_parser("discuss")
    rv_d.add_argument("thread")
    _add_role(rv_d, "--role")
    rv_d.add_argument("--body", required=True)
    rv_ag = rv.add_parser("agree")
    rv_ag.add_argument("thread")
    _add_role(rv_ag, "--role")
    rv_ag.add_argument(
        "--body", default="No unresolved objections; ready for final synthesis.")
    rv_c = rv.add_parser("conclude")
    rv_c.add_argument("thread")
    _add_role(rv_c, "--role")
    rv_c.add_argument("--reason", required=True)
    rv_st = rv.add_parser("status")
    rv_st.add_argument("thread")

    # --- serve --------------------------------------------------------------
    p_sv = sub.add_parser("serve", help="run the web console (board/agent/meetings)")
    # Loopback by default: the console exposes the supervisor adapter, and a
    # host that wants it on a network should say so explicitly.
    p_sv.add_argument("--host", default=env("HOST") or "127.0.0.1")
    p_sv.add_argument("--port", type=int, default=int(env("PORT") or 8000))
    p_sv.add_argument("--reload", action="store_true", help="development autoreload")

    return parser


# --- dispatch ---------------------------------------------------------------

def _cmd_status(args) -> None:
    from . import orchestration as orch
    if args.status_cmd == "set":
        out = orch.set_status(args.role, state=args.state, activity=args.activity,
                              session_id=args.session_id, harness=args.harness)
    elif args.status_cmd == "end":
        orch.end_session(args.role)
        out = {"ended": args.role}
    else:  # show
        out = orch.presence()
    _emit(out)


def _cmd_task(args) -> None:
    from . import orchestration as orch
    if args.task_cmd == "add":
        tid = orch.task_add(args.title, assignee_role=args.assignee,
                            detail=args.detail, priority=args.priority,
                            source_kind=args.source, source_ref=args.ref,
                            due_at=args.due, created_by=args.by)
        out = {"task": tid, "assignee": args.assignee, "priority": args.priority}
    elif args.task_cmd == "list":
        out = orch.tasks(assignee_role=args.assignee, status=args.status,
                         include_closed=args.all)
    elif args.task_cmd == "update":
        ok = orch.task_update(args.id, status=args.status, priority=args.priority,
                              due_at=args.due, detail=args.detail, title=args.title,
                              result_note=args.note, assignee_role=args.assignee,
                              blocked_on=args.blocked_on, actor=args.by)
        out = {"updated": ok, "id": args.id}
    else:  # done | cancel
        status = "done" if args.task_cmd == "done" else "cancelled"
        ok = orch.task_close(args.id, status=status, note=args.note, actor=args.by)
        out = {args.task_cmd: ok, "id": args.id}
    _emit(out)


def _cmd_inbox(args) -> None:
    from . import orchestration as orch
    if args.inbox_cmd == "enqueue":
        iid = orch.inbox_enqueue(args.target, args.source, args.title,
                                 body=args.body, ref=args.ref,
                                 priority=args.priority, dedup_key=args.dedup_key)
        out = {"enqueued": iid} if iid else {"deduped": True}
    elif args.inbox_cmd == "list":
        out = orch.inbox_pending(args.target, include_delivered=not args.undelivered)
    else:  # ack — only ever acks items already DELIVERED; see inbox_ack().
        out = {"acked": orch.inbox_ack(args.target, ids=args.id)}
    _emit(out)


def _cmd_wake(args) -> None:
    from . import orchestration as orch
    if args.wake_cmd == "tick":
        # --dry must not mutate: no attempt rows, no escalation, no delivery
        # marks. It is the only safe way to inspect the plan from a session.
        out = orch.plan_wakes(record=not args.dry)
    elif args.wake_cmd == "sources":
        out = orch.wake_sources(args.role)
    else:  # list
        out = orch.wake_attempts_recent(args.limit)
    _emit(out)


def _cmd_hook(args) -> None:
    from . import orchestration as orch
    if args.hook_cmd == "add":
        # Probe paths are validated fail-fast at registration against
        # CONFIG.probe_allowlist — never at fire time, when nobody is watching.
        out = orch.hook_add(args.owner, args.title, at=args.at, every=args.every,
                            callable_path=args.probe, cron=args.cron, tz=args.tz,
                            until=args.until, body=args.body, priority=args.priority)
    elif args.hook_cmd == "list":
        out = orch.hooks(args.owner, include_closed=args.all)
    else:  # cancel
        out = {"cancelled": orch.hook_cancel(args.id, actor=args.by)}
    _emit(out)


def _cmd_session(args) -> None:
    from . import orchestration as orch
    _emit(orch.rollover_plan(record=not args.dry))


def _cmd_delivery(args) -> None:
    from . import orchestration as orch
    _emit(orch.delivery_ledger(args.meeting))


def _roles_arg(value: str | None) -> list[str] | None:
    """Parse ``--attendees a,b``. ``None`` means "let the engine decide" — it
    defaults to the registered roles, which is why we never expand it here."""
    if not value:
        return None
    return [r.strip() for r in value.split(",") if r.strip()]


def _cmd_meeting(args) -> None:
    from . import meetings
    if args.meeting_cmd == "call":
        out = meetings.call_meeting(
            agenda=args.agenda, called_by=args.caller,
            attendees=_roles_arg(args.attendees),
            meeting_type=args.meeting_type, priority=args.priority,
            idle_minutes=args.idle_minutes, max_messages=args.max_messages,
            consensus_threshold=args.consensus_threshold,
            wait_timeout_seconds=args.wait_timeout_seconds,
        )
    elif args.meeting_cmd == "discover":
        out = meetings.discover(args.role, include_closed=args.all)
    elif args.meeting_cmd == "check-in":
        out = meetings.check_in(args.meeting, role=args.role)
    elif args.meeting_cmd == "updates":
        out = meetings.wait_for_updates(args.meeting, role=args.role,
                                        wait_seconds=args.wait_seconds,
                                        mark_read=args.mark_read)
    elif args.meeting_cmd == "send":
        out = meetings.send_update(args.meeting, role=args.role, body=args.body,
                                   kind=args.kind, reply_to=args.reply_to,
                                   resolves=args.resolves)
    elif args.meeting_cmd == "resolve":
        out = meetings.resolve_obligations(args.meeting, role=args.role,
                                           message_ids=args.resolves,
                                           covered_by=args.covered_by)
    elif args.meeting_cmd == "position":
        out = meetings.submit_position(args.meeting, role=args.role, body=args.body,
                                       reply_to=args.reply_to)
    elif args.meeting_cmd == "leave":
        out = meetings.leave_meeting(args.meeting, role=args.role, reason=args.reason)
    elif args.meeting_cmd == "propose-end":
        out = meetings.propose_end(args.meeting, role=args.role,
                                   resolution=args.resolution)
    elif args.meeting_cmd == "confirm-end":
        out = meetings.confirm_end(args.meeting, role=args.role)
    elif args.meeting_cmd == "reject-end":
        out = meetings.reject_end(args.meeting, role=args.role, reason=args.reason)
    elif args.meeting_cmd == "pause":
        out = meetings.pause_meeting(args.meeting, role=args.role, reason=args.reason)
    elif args.meeting_cmd == "escalate":
        # Append-only: an escalation supersedes and inserts, never edits.
        out = meetings.escalate_meeting(args.meeting, role=args.role,
                                        reason=args.reason, channel=args.channel,
                                        pause=not args.keep_open)
    elif args.meeting_cmd == "escalations":
        out = meetings.list_escalations(args.meeting)
    elif args.meeting_cmd == "wake-list":
        out = meetings.wake_requests(args.role)
    elif args.meeting_cmd == "wake-ack":
        out = meetings.acknowledge_wake(args.meeting, role=args.role)
    elif args.meeting_cmd == "status":
        out = meetings.meeting_status(args.meeting)
    else:  # supervisor-apply
        out = meetings.apply_supervisor_assertion(args.assertion, args.signature)
    _emit(out)


def _cmd_review(args) -> None:
    from . import mailbox
    if args.review_cmd == "start":
        from . import meetings
        out = meetings.call_meeting(
            agenda=args.subject, called_by=args.caller,
            attendees=_roles_arg(args.attendees), meeting_type="review",
            idle_minutes=args.idle_minutes, max_messages=args.max_messages,
            consensus_threshold=min(4, args.max_discussion),
        )
    elif args.review_cmd in set(REVIEW_STAGES):
        # CLI verb -> engine stage: "finalize" writes the "final" artifact.
        stage = "final" if args.review_cmd == "finalize" else args.review_cmd
        out = mailbox.submit_review_artifact(args.thread, role=args.role,
                                             stage=stage, path=args.file)
    elif args.review_cmd == "discuss":
        out = mailbox.review_discuss(args.thread, role=args.role, body=args.body)
    elif args.review_cmd == "agree":
        out = mailbox.review_discuss(args.thread, role=args.role, body=args.body,
                                     agree=True)
    elif args.review_cmd == "conclude":
        out = mailbox.conclude_review(args.thread, role=args.role, reason=args.reason)
    else:  # status
        out = {"thread": mailbox.get_thread(args.thread),
               "artifacts": mailbox.review_artifacts(args.thread)}
    _emit(out)


def _cmd_serve(args) -> None:
    """Run the web console. Imported lazily: fastapi/uvicorn are an optional
    extra, and every other subcommand must work without them installed."""
    try:
        import uvicorn
    except ImportError:
        _reject(f"the web console needs the optional extra: "
                f"pip install {PROJECT_NAME}[web]")
    # An import string (not the app object) so --reload can re-import cleanly,
    # and factory=True because the web module exposes create_app() rather than a
    # module-level app: a host may run several engines in one process, and
    # building the app at import time would bind the config too early.
    uvicorn.run(f"{PROJECT_NAME}.web.app:create_app", factory=True,
                host=args.host, port=args.port, reload=args.reload)


_DISPATCH = {
    "status": _cmd_status,
    "task": _cmd_task,
    "inbox": _cmd_inbox,
    "wake": _cmd_wake,
    "hook": _cmd_hook,
    "session": _cmd_session,
    "delivery": _cmd_delivery,
    "meeting": _cmd_meeting,
    "review": _cmd_review,
    "serve": _cmd_serve,
}


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        _DISPATCH[args.cmd](args)
    except ValueError as e:
        # The engine's rejection channel — an illegal request, not a bug.
        # Unknown roles land here, so offer the registry as a hint.
        _reject(str(e), with_roles=True)
    except BrokenPipeError:
        # `deskd ... | head` — not an error; exit quietly without a traceback.
        sys.stdout = None  # type: ignore[assignment]
        raise SystemExit(0)
    except OSError as e:
        # Missing --file artifact, unreadable key, etc. Still a rejection from
        # the caller's point of view: report it, don't traceback.
        _reject(str(e))


if __name__ == "__main__":
    main()
