"""selfgraph measurement harness.

Run ``python -m harness.run_corpus`` to generate a JSONL dataset of
per-goal measurements over a mechanically-generated goal set, then
``python -m harness.report`` to aggregate.

The harness does not edit any agent file — it instruments
``propose_patch_for``, ``validate_proposal``, and ``sandbox_apply``
from outside and emits structured output. No prose conclusions.
"""
