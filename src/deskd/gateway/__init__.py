"""Experimental, credential-free gateway building blocks.

These modules are internal primitives, not a network service or an installed
security boundary. They require explicit database paths and do not register
tools, start processes, read credentials, or connect to a broker. Existing
deskd configuration, CLI commands, and coordination databases are unchanged.

``registry`` owns binding and channel authorization state. ``events`` owns
transactional command receipts, an outbox, and independent consumer receipts.
``commands`` composes authorization with fixed handlers in one local transaction.
Transport authentication and controller isolation must be implemented and
validated before these primitives can guard real capabilities.
"""
