# The playbook pattern

How to write a phase playbook that a wake hook can drive.

## The shape

deskd wakes an agent with a **notification whose body points at a phase**. The
woken session boots (loads its skill, declares its role), reads the phase
instruction, and executes that phase. So a playbook is a set of **phases**, each
independently executable by a session that just woke with no context.

```
skills/<your-desk>/PLAYBOOK_<ROLE>_SESSION.md
  ## Phase: open      ← driven by a cron hook, e.g. "0 9 * * *"   (start of day)
  ## Phase: cycle     ← driven by a recurring hook, e.g. "*/30 * * * *"
  ## Phase: close     ← driven by a cron hook, e.g. "0 18 * * *"  (end of day)
```

Register them once, idempotently, from a script in your repo:

```python
HOOKS = [("operator", "open", "15 6 * * 1-5", "urgent",
          "Execute PLAYBOOK_OPERATOR §open: ...concrete steps..."), ...]
for owner, title, cron, prio, body in HOOKS:
    if (owner, title) not in existing:
        hook_add(owner, title, cron=cron, priority=prio, body=body)
```

## Rules for a phase

1. **Assume zero context.** The session may be brand new and rebuilt from the DB.
   Name the exact commands and files; don't say "continue where you left off".
2. **The body is the instruction.** The hook's `body` is what the woken agent
   reads — make it point precisely at the phase and its steps.
3. **End the turn.** A phase finishes and stops. It never sleeps or loops waiting
   for the next phase — the next hook fires it.
4. **Idempotent.** A phase may run twice (a retried wake). Make it safe.
5. **Journal as you go**, not at the end. A session can be cut short; anything not
   written is lost, and other agents read your journal.
6. **Edit the playbook, not the hook prompt.** The hook points at the playbook;
   the playbook holds the logic. One place to change.

## Detection vs handling

Don't make a phase poll for events. Split them:

- **Detection** — a cheap scheduled process (or a probe, if fast and local)
  enqueues a notification when something is true.
- **Handling** — the orchestrator wakes the agent with that notification.

A phase that hits the network to decide whether anything happened is a polling
loop wearing a hat, and it will stall the tick if it runs as a probe.
