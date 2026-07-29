"""Workflow orchestration layer — multi-step, session-level operations.

`session.py` hosts the G1 composed ops (get_context_snapshot, pickup_work,
handoff_work): each runs in ONE transaction, built directly on the conn-level
privates of dct.core (K-10b — NOT on a dct.services facade, which stays
reserved for future shared ops). Consumed by dct.mcp and dct.cli; never
imports from dct.integrations."""
