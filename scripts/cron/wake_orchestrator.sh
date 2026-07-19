#!/usr/bin/env bash
# Wake-orchestrator DRIVER: executes the plan produced by `deskd wake tick`.
#
# Division of labour: the orchestrator DECIDES (collects demand, records
# wake_attempts, escalates up the ladder, returns a plan) in pure Python; this
# script is the ONLY place that actually RESUMES or SPAWNS an agent session.
# Keeping the decision side effect-free and the effect side dumb is what makes
# the engine testable and the ladder auditable.
#
# SAFE BY DEFAULT — dry-run unless DESKD_WAKE_EXECUTE=1. Rollout:
#   1. schedule every 60s:  * * * * * /path/to/deskd/scripts/cron/wake_orchestrator.sh
#   2. watch logs/wake.log to confirm the decisions look right
#   3. only then set DESKD_WAKE_EXECUTE=1 to let it resume/spawn for real
#
# Guards: per-role flock (skip if a session for that role is already running —
# an active session's PostToolUse hook already delivers, so the planner picks the
# L0 "hook" channel and emits NO action here; the flock is defence against stale
# presence). At most one resume/spawn per role per tick. This layer only wakes
# agents; it never acts as one.
#
# HARNESS: the resume/spawn commands below are Claude Code's. A host running a
# different agent harness swaps those two invocations; everything else (locks,
# plan parsing, escalation, rollover) is harness-agnostic.
#
# Tunables (all DESKD_*): WAKE_EXECUTE, WAKE_MODEL, WAKE_TIMEOUT,
# WAKE_ALLOWED_TOOLS, AGENT_CMD, PYTHON, BIN, LOG_DIR.
set -u

# Resolve the project from THIS script's location (scripts/cron/ -> project),
# never from a hardcoded path: the repo must be clonable anywhere.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)

# cron hands us a near-empty PATH; keep the caller's PATH as a suffix.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

LOGDIR=${DESKD_LOG_DIR:-$PROJECT/logs}
mkdir -p "$LOGDIR" 2>/dev/null || true
LOG=$LOGDIR/wake.log

# deskd's default DB path is resolved relative to the process cwd (see
# config.BASE_DIR), so anchor cwd at the project or a bare DESKD_DB-less
# install would open a different database than the agents do.
cd -- "$PROJECT" || exit 1

# --- interpreter + CLI resolution -------------------------------------------
PY=${DESKD_PYTHON:-}
if [ -z "$PY" ]; then
  if [ -x "$PROJECT/.venv/bin/python" ]; then PY="$PROJECT/.venv/bin/python"
  else PY=$(command -v python3 2>/dev/null || true); fi
fi
[ -n "$PY" ] || { echo "[$(date)] wake: no python interpreter" >>"$LOG"; exit 1; }

CLI_BIN=${DESKD_BIN:-}
if [ -z "$CLI_BIN" ]; then
  if [ -x "$PROJECT/.venv/bin/deskd" ]; then CLI_BIN="$PROJECT/.venv/bin/deskd"
  else CLI_BIN=$(command -v deskd 2>/dev/null || true); fi
fi
# Wrapper so every call site is identical whether deskd is installed as a
# console script or only importable. Shell functions are inherited by the
# subshells below, so this works inside the backgrounded session launchers too.
deskd() {
  if [ -n "$CLI_BIN" ]; then "$CLI_BIN" "$@"; else "$PY" -m deskd "$@"; fi
}

# --- config-derived constants ------------------------------------------------
# Lock paths come from deskd.config so this script and the engine can never
# disagree about which file guards a role. Fall back to the documented defaults
# if deskd is not importable (then the tick below fails and logs anyway).
CFG=$("$PY" -c 'from deskd.config import CONFIG, driver_lock_path, role_lock_path
print(driver_lock_path())
print(role_lock_path("__ROLE__"))
print(CONFIG.timezone)' 2>/dev/null) || CFG=""
DRIVER_LOCK=$(printf '%s\n' "$CFG" | sed -n 1p)
ROLE_LOCK_TMPL=$(printf '%s\n' "$CFG" | sed -n 2p)
TZ_NAME=$(printf '%s\n' "$CFG" | sed -n 3p)
[ -n "${DRIVER_LOCK:-}" ]    || DRIVER_LOCK=/tmp/deskd-wake-driver.lock
[ -n "${ROLE_LOCK_TMPL:-}" ] || ROLE_LOCK_TMPL=/tmp/deskd-role-__ROLE__.lock
[ -n "${TZ_NAME:-}" ]        || TZ_NAME=${DESKD_TZ:-UTC}

ts() { TZ="$TZ_NAME" date '+%F %H:%M:%S'; }
role_lock() { printf '%s' "${ROLE_LOCK_TMPL/__ROLE__/$1}"; }

EXECUTE=${DESKD_WAKE_EXECUTE:-0}
AGENT=${DESKD_AGENT_CMD:-claude}
MODEL=${DESKD_WAKE_MODEL:-claude-opus-4-8}
TIMEOUT=${DESKD_WAKE_TIMEOUT:-1800}
# Domain tools belong to the host: extend this via DESKD_WAKE_ALLOWED_TOOLS
# (e.g. append your MCP server's tools). The engine grants nothing by itself.
# This is only the DEFAULT: a role whose registry authority declares
# `allowed_tools` gets exactly that list — the plan carries the declaration and
# this driver is the enforcement point (deskd declares, the harness enforces).
# NB --allowedTools is only a boundary while `Bash` is excluded from the grant;
# a role with a shell can reach anything the process can.
ALLOWED=${DESKD_WAKE_ALLOWED_TOOLS:-Bash,Read,Write,Edit,Glob,Grep,Skill,ToolSearch,TodoWrite,WebSearch,WebFetch}

# Single-instance: don't let two driver ticks overlap.
exec 9>"$DRIVER_LOCK"
flock -n 9 || exit 0

# In dry-run, tick with --dry so the preview records NOTHING (no phantom
# attempts, no escalation-clock advance). Real ticks only when executing.
TICK_ARGS=""; [ "$EXECUTE" != "1" ] && TICK_ARGS="--dry"
PLAN=$(deskd wake tick $TICK_ARGS 2>>"$LOG") \
  || { echo "[$(ts)] wake tick failed" >>"$LOG"; exit 0; }

# One TSV row per action: role \t channel \t level \t session_id \t tools \t
# prompt. session_id/tools are emitted as "-" when absent so consecutive tabs
# never collapse under IFS=$'\t' (tab is IFS-whitespace) and shift the prompt
# field; the prompt stays LAST because it is freeform. `tools` is the role's
# declared authority.allowed_tools, comma-joined — the declaration this driver
# enforces below.
printf '%s' "$PLAN" | "$PY" -c '
import sys, json
p = json.load(sys.stdin)
for a in p.get("actions", []):
    at = (a.get("authority") or {}).get("allowed_tools")
    tools = ",".join(at) if isinstance(at, list) and at else "-"
    print("\t".join([a["role"], a["channel"], str(a["level"]),
                     a.get("session_id") or "-", tools,
                     a["prompt"].replace("\t", " ").replace("\n", " ")]))
' | while IFS=$'\t' read -r role channel level session tools prompt; do
  [ -z "${role:-}" ] && continue
  [ "$session" = "-" ] && session=""
  # Per-role enforcement of the declared grant; the global default only covers
  # roles that declare nothing.
  ROLE_ALLOWED=$ALLOWED
  [ "$tools" != "-" ] && ROLE_ALLOWED=$tools

  if [ "$EXECUTE" != "1" ]; then
    echo "[$(ts)] DRY-RUN $channel L$level role=$role :: $prompt" >>"$LOG"
    continue
  fi

  case "$channel" in
    resume|spawn)
      # Launch the session in a BACKGROUND subshell so the driver tick returns
      # promptly and can process other roles this tick / next ticks — otherwise
      # a 30-min session serializes every other wake behind it. The subshell:
      #   - drops fd 9 (exec 9>&-) so it does NOT hold the driver's
      #     single-instance lock for the session's lifetime (else the next tick
      #     would block behind a running session);
      #   - holds the PER-ROLE lock (fd 8) for the whole session lifetime, which
      #     is what actually prevents a double-spawn of the same role.
      # It outlives the driver (reparented to init) — fine under cron/nohup.
      (
        exec 9>&-
        # Canonical per-role lock — every path that can start a session for a
        # role flocks this same file, so at most one session per role ever runs.
        exec 8>"$(role_lock "$role")"
        flock -n 8 || { echo "[$(ts)] role=$role has an active session (lock held); skip $channel" >>"$LOG"; exit 0; }
        if [ "$channel" = "resume" ] && [ -n "$session" ]; then
          echo "[$(ts)] resume role=$role session=$session" >>"$LOG"
          DESKD_ROLE="$role" timeout "$TIMEOUT" "$AGENT" -p "$prompt" \
            --resume "$session" --allowedTools "$ROLE_ALLOWED" >>"$LOG" 2>&1 || true
        else
          SID=$(uuidgen 2>/dev/null || "$PY" -c 'import uuid; print(uuid.uuid4())')
          deskd status set --role "$role" --state booting \
            --session-id "$SID" --harness "wake-$role" >/dev/null 2>&1 || true
          echo "[$(ts)] spawn role=$role session=$SID" >>"$LOG"
          DESKD_ROLE="$role" DESKD_SESSION_ID="$SID" timeout "$TIMEOUT" "$AGENT" -p "$prompt" \
            --model "$MODEL" --session-id "$SID" --allowedTools "$ROLE_ALLOWED" >>"$LOG" 2>&1 || true
          deskd status end --role "$role" >/dev/null 2>&1 || true
        fi
      ) &
      ;;
    human)
      # Human rung: the ENGINE owns it now. Arrival at a leaves_machine rung
      # writes a durable wake_escalations row inside the planning tick and
      # dispatches it through the registered channels after commit — for EVERY
      # reason kind, not just meeting wakes (this branch used to escalate
      # meetings only, so any other demand reaching this rung pulled in
      # nobody). Nothing left for the driver to execute.
      echo "[$(ts)] L$level human rung role=$role (engine dispatched; see wake_escalations)" >>"$LOG"
      ;;
    *)
      # supervisor_badge (terminal): the arrival wrote its wake_escalations
      # row too; the board shows it red until someone acts. Nothing to execute.
      echo "[$(ts)] L$level $channel role=$role (persistent red on console)" >>"$LOG"
      ;;
  esac
done

# --- cross-day rollover: wind down sessions left from a prior day ------------
# The driver resumes each stale session with a wrap-up prompt, then ends it
# (bounding the drain to one pass). Guarded by the same per-role lock, so it
# only resumes when the session's process is idle (not mid-turn).
if [ "$EXECUTE" = "1" ]; then
  ROLLPLAN=$(deskd session rollover 2>>"$LOG") || ROLLPLAN=""
  printf '%s' "$ROLLPLAN" | "$PY" -c '
import sys, json
try: p = json.load(sys.stdin)
except Exception: sys.exit(0)
for r in p.get("rollovers", []):
    at = (r.get("authority") or {}).get("allowed_tools")
    tools = ",".join(at) if isinstance(at, list) and at else "-"
    print("\t".join([r["role"], r.get("session_id") or "-", tools,
                     r["prompt"].replace("\t", " ").replace("\n", " ")]))
' | while IFS=$'\t' read -r role session tools prompt; do
    [ -z "${role:-}" ] && continue
    [ "$session" = "-" ] && session=""
    ROLE_ALLOWED=$ALLOWED
    [ "$tools" != "-" ] && ROLE_ALLOWED=$tools
    if [ -z "$session" ]; then
      echo "[$(ts)] rollover role=$role has no resumable session; ending it" >>"$LOG"
      deskd status end --role "$role" >/dev/null 2>&1 || true
      continue
    fi
    (
      exec 9>&-
      exec 8>"$(role_lock "$role")"
      flock -n 8 || { echo "[$(ts)] rollover role=$role: session lock busy (mid-turn); retry next tick" >>"$LOG"; exit 0; }
      echo "[$(ts)] rollover-drain role=$role session=$session" >>"$LOG"
      DESKD_ROLE="$role" timeout "$TIMEOUT" "$AGENT" -p "$prompt" \
        --resume "$session" --allowedTools "$ROLE_ALLOWED" >>"$LOG" 2>&1 || true
      deskd status end --role "$role" >/dev/null 2>&1 || true
    ) &
  done
else
  # Dry preview: `rollover --dry` marks nothing draining and ends nothing.
  deskd session rollover --dry 2>>"$LOG" \
    | "$PY" -c '
import sys, json
try: p = json.load(sys.stdin)
except Exception: sys.exit(0)
for r in p.get("rollovers", []):
    print("DRY-RUN rollover role="+r["role"]+" from "+r["from_day"])' \
    | while read -r line; do echo "[$(ts)] $line" >>"$LOG"; done
fi
exit 0
