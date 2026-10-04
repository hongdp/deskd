"""Experimental, credential-free gateway and local memo workflow.

Importing this package does not load host configuration, start services, read
credentials, or connect to a broker. Explicit ``python -m deskd.gateway``
commands offer a mock demo, metadata preflight, memo-only Unix service and
fixed stdio bridge. Existing deskd entry points and databases are unchanged.

``registry`` owns binding and channel authorization state. ``events`` owns
transactional command receipts, an outbox, and independent consumer receipts.
``commands`` composes authorization with fixed handlers in one local transaction.
``transport`` authenticates Linux peer credentials and separates administration
from business calls. Full official-harness sandbox and controller lifecycle
acceptance remain required before guarding credentials or external effects.
"""
