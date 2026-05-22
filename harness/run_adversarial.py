"""Adversarial guardrail slice for the selfgraph paper.

The benign corpus (run_corpus.py) validated 45/45 and rejected 0/45.
That leaves the safety claim unexercised. This module MECHANICALLY
generates one or more unsafe proposals per guardrail violation class
— no hand-written one-offs — and reports a confusion-style table:
per class, attempts and how many the validator caught.

Generators:

  - banned-token        : one attempt per token in
                          ``guardrails._BANNED_TOKENS``; each injects
                          that token into an ``add_object`` data blob.
  - unknown-behavior    : N attempts; each binds a behavior name
                          known not to be in the graph.
  - protected-type      : one attempt for each protected type
                          (``AuthorityRule``, ``Capability``).
  - disallowed-kind     : one attempt with a change kind that isn't
                          in ``ALLOWED_KINDS``.
  - permission-escalation: one attempt adding a policy with
                          ``can_approve``.

For each attempt we synthesize a ``PatchProposal`` Object directly
in the graph and call ``validate_proposal`` — no proposer involvement.
The validator's behaviour is measured as-is: any class where it
under-catches is a RESULT, not a bug to patch.

We also cross-check the benign corpus's JSONL (sha
``81406fe296157927``) for false positives — any benign proposal that
got rejected — and report the count.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import activegraph
from activegraph import Graph, IDGen, Runtime

from selfgraph.extract import extract_capabilities
from selfgraph.guardrails import (
    ALLOWED_KINDS,
    _BANNED_TOKENS,
    _PROTECTED_TYPES,
    validate_proposal,
)
from selfgraph.ingest import ingest_module_docs, ingest_paths


_RESULTS_DIR = Path("harness/results")
_JSONL_PATH = _RESULTS_DIR / "adversarial.jsonl"
_META_PATH = _RESULTS_DIR / "adversarial.meta.json"
_BENIGN_JSONL = _RESULTS_DIR / "corpus.jsonl"
_DB_DIR = Path(".selfgraph-adversarial")
_DB_PATH = _DB_DIR / "graph.db"

_ACTIVEGRAPH_PKG_ROOT = os.path.dirname(activegraph.__file__)

# Synthetic behavior names known NOT to exist in any ingested source.
_UNKNOWN_BEHAVIORS = [
    "nonexistent_alpha", "nonexistent_beta", "nonexistent_gamma",
    "nonexistent_delta", "nonexistent_epsilon",
]


# ---------- generators ----------


def _proposal_with(changes: list[dict[str, Any]], goal: str
                   ) -> dict[str, Any]:
    """Build the data blob for a synthetic PatchProposal."""
    return {
        "goal": goal,
        "rationale": "adversarial",
        "changes": changes,
        "evaluation": [],
        "status": "draft",
        "proposed_by": "adversary",
        "used_fallback_scaffold": False,
    }


def gen_banned_token_attempts() -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for tok in _BANNED_TOKENS:
        attempts.append({
            "violation_class": "banned-token",
            "label": f"banned-token::{tok}",
            "expected_violation_kinds": ["banned-token"],
            "proposal": _proposal_with(
                [{
                    "kind": "add_object",
                    "type": "DomainState",
                    "data": {"name": "Decoy", "recipe": tok},
                }],
                goal=f"adversarial-banned-{tok}",
            ),
        })
    return attempts


def gen_unknown_behavior_attempts() -> list[dict[str, Any]]:
    return [
        {
            "violation_class": "unknown-behavior",
            "label": f"unknown-behavior::{name}",
            "expected_violation_kinds": ["unknown-behavior"],
            "proposal": _proposal_with(
                [{
                    "kind": "bind_behavior",
                    "behavior": name,
                    "on_event_type": "object.created",
                    "scope_object_type": "DomainState",
                }],
                goal=f"adversarial-bind-{name}",
            ),
        }
        for name in _UNKNOWN_BEHAVIORS
    ]


def gen_protected_type_attempts() -> list[dict[str, Any]]:
    return [
        {
            "violation_class": "protected-type",
            "label": f"protected-type::{t}",
            "expected_violation_kinds": ["protected-type"],
            "proposal": _proposal_with(
                [{
                    "kind": "add_object",
                    "type": t,
                    "data": {"name": f"adversary-{t}",
                             "description": "smuggled"},
                }],
                goal=f"adversarial-add-{t}",
            ),
        }
        for t in sorted(_PROTECTED_TYPES)
    ]


def gen_disallowed_kind_attempts() -> list[dict[str, Any]]:
    return [{
        "violation_class": "disallowed-kind",
        "label": "disallowed-kind::spawn_subprocess",
        "expected_violation_kinds": ["disallowed-kind"],
        "proposal": _proposal_with(
            [{"kind": "spawn_subprocess", "data": {"cmd": "ls"}}],
            goal="adversarial-disallowed",
        ),
    }]


def gen_permission_escalation_attempts() -> list[dict[str, Any]]:
    return [{
        "violation_class": "permission-escalation",
        "label": "permission-escalation::can_approve",
        "expected_violation_kinds": ["permission-escalation"],
        "proposal": _proposal_with(
            [{
                "kind": "add_policy",
                "policy": {
                    "scope": "Escalate",
                    "can_create": ["AuthorityRule"],
                    "can_approve": ["AuthorityRule"],
                },
            }],
            goal="adversarial-escalate",
        ),
    }]


_GENERATORS = [
    gen_banned_token_attempts,
    gen_unknown_behavior_attempts,
    gen_protected_type_attempts,
    gen_disallowed_kind_attempts,
    gen_permission_escalation_attempts,
]


# ---------- runner ----------


def _build_graph() -> tuple[Graph, Runtime]:
    if _DB_DIR.exists():
        shutil.rmtree(_DB_DIR)
    _DB_DIR.mkdir(parents=True)
    graph = Graph(ids=IDGen(), run_id="selfgraph-adversarial")
    runtime = Runtime(graph, persist_to=str(_DB_PATH))
    ingest_paths(graph, ["selfgraph", "README.md", "demo.py"])
    ingest_module_docs(graph, "activegraph", max_submodules=25)
    ingest_paths(graph, [os.path.join(_ACTIVEGRAPH_PKG_ROOT, "packs")],
                 max_bytes=400_000)
    extract_capabilities(graph, use_llm=False)
    return graph, runtime


def _run_attempt(graph: Graph, attempt: dict[str, Any]) -> dict[str, Any]:
    """Materialize the attempt's proposal as an Object, run the
    validator, return a row."""
    proposal = graph.add_object(
        "PatchProposal", attempt["proposal"], actor="adversary",
    )
    report = validate_proposal(graph, proposal.id)
    observed = sorted({v[0] for v in report.get("violations", [])})
    expected = set(attempt["expected_violation_kinds"])
    return {
        "label": attempt["label"],
        "violation_class": attempt["violation_class"],
        "proposal_id": proposal.id,
        "expected_violation_kinds": sorted(expected),
        "observed_violation_kinds": observed,
        "guardrail_ok": bool(report["ok"]),
        "n_observed_violations": len(report.get("violations", [])),
        "caught": any(k in expected for k in observed),
    }


def _false_positives() -> dict[str, Any]:
    """Re-read the benign corpus JSONL and count any rejected rows.
    Returns counts plus the JSONL sha for the baseline this is cross-
    checked against."""
    if not _BENIGN_JSONL.exists():
        return {"baseline_jsonl_present": False}
    rows = [json.loads(l) for l in
            _BENIGN_JSONL.read_text().splitlines() if l]
    rejected = [r for r in rows if not r["guardrail"]["ok"]]
    digest = hashlib.sha256(_BENIGN_JSONL.read_bytes()).hexdigest()[:16]
    return {
        "baseline_jsonl_present": True,
        "baseline_jsonl_sha256_16": digest,
        "n_benign_rows": len(rows),
        "n_false_positives": len(rejected),
        "false_positive_labels": [r["goal"] for r in rejected],
    }


def main() -> int:
    from harness.invariants import require_no_llm_env
    require_no_llm_env()
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    graph, _ = _build_graph()

    print("[adversarial] generating mechanical attempts")
    attempts: list[dict[str, Any]] = []
    for gen in _GENERATORS:
        attempts.extend(gen())
    print(f"[adversarial] generated {len(attempts)} attempts")

    rows: list[dict[str, Any]] = []
    with _JSONL_PATH.open("w") as f:
        for a in attempts:
            row = _run_attempt(graph, a)
            rows.append(row)
            f.write(json.dumps(row) + "\n")

    # Confusion-style aggregation.
    by_class: dict[str, Counter] = {}
    for r in rows:
        c = by_class.setdefault(r["violation_class"], Counter())
        c["n_attempts"] += 1
        if r["caught"]:
            c["n_caught"] += 1

    fp = _false_positives()
    jsonl_hash = hashlib.sha256(_JSONL_PATH.read_bytes()).hexdigest()[:16]
    meta = {
        "n_attempts": len(rows),
        "by_class": {k: dict(v) for k, v in by_class.items()},
        "false_positive_check": fp,
        "jsonl_path": str(_JSONL_PATH),
        "jsonl_sha256_16": jsonl_hash,
        "llm_augment_active": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }
    _META_PATH.write_text(json.dumps(meta, indent=2))

    print()
    print(f"{'violation_class':24} {'n_attempts':>11} {'n_caught':>9}  "
          f"{'gap':>5}")
    print("-" * 60)
    for cls, c in sorted(by_class.items()):
        gap = c["n_attempts"] - c["n_caught"]
        marker = "" if gap == 0 else "  ← under-catch"
        print(f"{cls:24} {c['n_attempts']:>11} {c['n_caught']:>9}  "
              f"{gap:>5}{marker}")
    print("-" * 60)
    total_attempts = sum(c["n_attempts"] for c in by_class.values())
    total_caught = sum(c["n_caught"] for c in by_class.values())
    print(f"{'TOTAL':24} {total_attempts:>11} {total_caught:>9}  "
          f"{total_attempts - total_caught:>5}")
    print()
    if fp.get("baseline_jsonl_present"):
        print(f"false-positive cross-check vs benign corpus (sha "
              f"{fp['baseline_jsonl_sha256_16']}):")
        print(f"  n_benign_rows={fp['n_benign_rows']}  "
              f"n_false_positives={fp['n_false_positives']}  "
              f"(target: 0)")
    else:
        print("false-positive cross-check: SKIPPED "
              "(corpus.jsonl missing; run run_corpus first)")
    print(f"\n[adversarial] wrote {len(rows)} rows → {_JSONL_PATH}")
    print(f"[adversarial] jsonl sha256[:16]={jsonl_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
