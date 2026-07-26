"""The overnight helpdesk — a runnable, zero-LLM deskd demo.

Three scripted agents (triage / writer / auditor) play out one night on a tiny
customer-support desk while the real engine does everything this project
promises: presence, a deduplicating capability-routed inbox, a bounded meeting
with read receipts and a termination handshake, and a wake ladder that climbs
past the machine rungs to page a human when an agent goes dark.

Entry point: ``python -m examples.support_desk.run_demo`` (see README.md).
"""
