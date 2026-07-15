# Evolution protocol

The desk must keep learning, and the knowledge base must stay small enough to
stay useful.

## What is knowledge vs what is data

- **Knowledge** = generalizable method / process / risk lessons. "Verify a
  signal's independence before trusting it" is knowledge.
- **Data** = domain- or instance-specific facts and views. Those live in your
  domain store, never in the knowledge base.

Before writing, strip identifiers, dates, and amounts unless they *are* the
evidence, then ask: *would this help someone running a different desk in a
different domain?* If no, it's data.

## Never hand-edit KNOWLEDGE.md

Concurrent agents will clobber each other. Use the transaction:

```bash
python skills/agent-orchestration/scripts/knowledge_txn.py begin --actor <you>
# edit ONLY the DRAFT path it prints
python skills/agent-orchestration/scripts/knowledge_txn.py commit <TXN_ID>
```

The commit takes an exclusive lock, verifies a SHA-256 baseline, and atomically
replaces the file. On `CONFLICT` **nothing was overwritten**: start a new
transaction, re-read the latest knowledge, re-apply your still-valid lesson, and
commit again. Never bypass a conflict by copying your draft over the file.

## Rules

- Every entry carries a date and an evidence line.
- An entry contradicted by later evidence is **corrected or deleted**, not
  accumulated alongside.
- On every edit, prune: scan for stale, duplicated, or contradicted entries.
- Keep it under ~150 lines. The pressure to stay concise is what keeps it useful.
- After a formal review, also audit the skill and playbooks. Apply only
  evidence-backed, generalizable process changes; record "skill audit: no change"
  when none is warranted. Never encode a transient state into a skill.

## Fixing the tools is the work

If a tool errors or a needed signal is missing, fixing or extending it **is**
session work — do it now, note it, and let the lesson (not the incident) reach
the knowledge base.
