"""Patch validator. Rejects unsafe or out-of-scope proposals.

Allowed v1 change kinds:
    add_object, add_relation, add_policy, add_state_bucket,
    add_task, add_evaluation, bind_behavior

Rejected:
    shell, exec/eval/subprocess/__import__ payloads, network calls,
    file writes, mutations of AuthorityRule, mutations of Capability
    objects already on the graph, anything else.
"""

from __future__ import annotations

import re
from typing import Iterable

from activegraph import Graph


ALLOWED_KINDS = {
    "add_object", "add_relation", "add_policy", "add_state_bucket",
    "add_task", "add_evaluation", "bind_behavior",
}

# Substring banlist for any string anywhere in the proposal payload.
_BANNED_TOKENS = (
    "subprocess", "os.system", "__import__", "exec(", "eval(",
    "shutil.rmtree", "open(", "urllib", "requests.", "socket.",
    "rm -rf", "curl ", "wget ", "/bin/sh", "/bin/bash", "popen",
    "compile(", "globals()", "setattr",
)

# Object types the validator considers part of the agent's "authority"
# substrate. Patches that try to mutate these without explicit approval
# are blocked.
_PROTECTED_TYPES = {"AuthorityRule", "Capability"}


class GuardrailViolation(Exception):
    """Raised when a PatchProposal fails validation. The message names
    the rule that fired and the change index that triggered it."""


def validate_proposal(
    graph: Graph,
    proposal_id: str,
    *,
    approved_by: str = None,
) -> dict:
    """Validate the PatchProposal with ``proposal_id``. Marks it
    'validated' (or 'rejected') by emitting a patch on the proposal
    object itself. Returns a report dict."""
    obj = graph.get_object(proposal_id)
    if obj is None or obj.type != "PatchProposal":
        raise GuardrailViolation(f"{proposal_id} is not a PatchProposal")
    changes = obj.data.get("changes", [])
    report = {"checked": len(changes), "violations": [], "ok": True}

    # Banned-token scan over the entire proposal payload.
    for hit in _scan_banned(obj.data):
        report["violations"].append(("banned-token", -1, hit))

    for i, change in enumerate(changes):
        if not isinstance(change, dict):
            report["violations"].append(("malformed-change", i, str(change)))
            continue
        kind = change.get("kind")
        if kind not in ALLOWED_KINDS:
            report["violations"].append(
                ("disallowed-kind", i, f"{kind!r} not in {sorted(ALLOWED_KINDS)}")
            )
            continue
        if kind == "add_object":
            t = change.get("type")
            if t in _PROTECTED_TYPES and not approved_by:
                report["violations"].append(
                    ("protected-type", i,
                     f"cannot add {t} without explicit approval")
                )
        if kind == "add_policy":
            policy = change.get("policy", {})
            # Refuse policies that escalate by claiming approval rights.
            if "can_approve" in policy:
                report["violations"].append(
                    ("permission-escalation", i,
                     "policies may not declare can_approve")
                )
        if kind == "bind_behavior":
            beh_name = change.get("behavior")
            known = {b.data.get("name") for b in graph.objects(type="Behavior")}
            if beh_name not in known:
                report["violations"].append(
                    ("unknown-behavior", i,
                     f"behavior {beh_name!r} not in capability graph; "
                     f"v1 only binds existing behaviors")
                )

    report["ok"] = not report["violations"]
    new_status = "validated" if report["ok"] else "rejected"
    graph.patch_object(
        proposal_id,
        {"status": new_status, "validation_report": report},
        actor="guardrails",
        rationale=(
            "All changes within allowed v1 surface."
            if report["ok"] else
            f"Rejected by {len(report['violations'])} rule(s)."
        ),
    )
    print(f"[guardrails] {proposal_id} → {new_status} "
          f"({len(report['violations'])} violation(s))")
    return report


def _scan_banned(payload, _path: str = "") -> Iterable[str]:
    if isinstance(payload, str):
        low = payload.lower()
        for tok in _BANNED_TOKENS:
            if tok in low:
                yield f"{_path}: {tok}"
    elif isinstance(payload, dict):
        for k, v in payload.items():
            yield from _scan_banned(v, f"{_path}.{k}")
    elif isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            yield from _scan_banned(v, f"{_path}[{i}]")
