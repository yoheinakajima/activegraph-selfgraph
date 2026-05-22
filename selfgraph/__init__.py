"""selfgraph: an ActiveGraph agent that ingests its own runtime/docs,
builds a capability graph, and proposes safe self-configuration patches.
"""

from selfgraph.ingest import ingest_paths, ingest_module_docs
from selfgraph.extract import extract_capabilities
from selfgraph.query import (
    answer_question,
    classify_change,
    summarize_capabilities,
    trace_grounding,
)
from selfgraph.propose import propose_patch_for
from selfgraph.guardrails import validate_proposal, GuardrailViolation
from selfgraph.sandbox import sandbox_apply

__all__ = [
    "ingest_paths",
    "ingest_module_docs",
    "extract_capabilities",
    "answer_question",
    "classify_change",
    "summarize_capabilities",
    "trace_grounding",
    "propose_patch_for",
    "validate_proposal",
    "GuardrailViolation",
    "sandbox_apply",
]
