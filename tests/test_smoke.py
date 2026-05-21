"""Smoke tests. Run:  python -m pytest tests/  (or `python tests/test_smoke.py`)

Verifies:
 - ingestion produces File + Chunk objects
 - extraction emits Capability + AuthorityRule anchors
 - propose_patch_for produces a validatable proposal
 - validate_proposal accepts safe proposals
 - validate_proposal rejects banned-token proposals
 - sandbox_apply produces a non-empty diff
"""

from __future__ import annotations

from activegraph import Graph, IDGen

from selfgraph.extract import extract_capabilities
from selfgraph.guardrails import validate_proposal
from selfgraph.ingest import ingest_paths
from selfgraph.propose import propose_patch_for
from selfgraph.sandbox import sandbox_apply


def _fresh() -> Graph:
    return Graph(ids=IDGen(), run_id="test-run")


def test_ingest_and_extract():
    g = _fresh()
    ingest_paths(g, ["selfgraph/__init__.py", "README.md"])
    assert any(o.type == "File" for o in g.all_objects())
    assert any(o.type == "Chunk" for o in g.all_objects())
    extract_capabilities(g, use_llm=False)
    caps = [o for o in g.all_objects() if o.type == "Capability"]
    rules = [o for o in g.all_objects() if o.type == "AuthorityRule"]
    assert len(caps) >= 6
    assert len(rules) >= 4


def test_proposal_accepted():
    g = _fresh()
    ingest_paths(g, ["selfgraph/__init__.py"])
    extract_capabilities(g, use_llm=False)
    pid = propose_patch_for(g, "track inbound emails")
    report = validate_proposal(g, pid)
    assert report["ok"], report
    sandbox = sandbox_apply(g, pid, promote=False)
    assert sandbox["diff"]["added_objects"]


def test_proposal_rejected_when_banned_token_injected():
    g = _fresh()
    ingest_paths(g, ["selfgraph/__init__.py"])
    extract_capabilities(g, use_llm=False)
    pid = propose_patch_for(g, "track changes")
    # Inject an unsafe change manually so we exercise the rejection path.
    proposal = g.get_object(pid)
    bad_changes = list(proposal.data["changes"]) + [{
        "kind": "add_object",
        "type": "BadActor",
        "data": {"recipe": "subprocess.Popen(['rm', '-rf', '/'])"},
    }]
    g.patch_object(pid, {"changes": bad_changes}, actor="test")
    report = validate_proposal(g, pid)
    assert not report["ok"]
    assert any("banned-token" in v[0] for v in report["violations"])


def test_proposal_rejected_for_unknown_behavior():
    g = _fresh()
    ingest_paths(g, ["selfgraph/__init__.py"])
    extract_capabilities(g, use_llm=False)
    pid = propose_patch_for(g, "do something")
    proposal = g.get_object(pid)
    bad_changes = list(proposal.data["changes"]) + [{
        "kind": "bind_behavior",
        "behavior": "behavior_that_does_not_exist",
        "on_event_type": "object.created",
        "scope_object_type": "Anything",
    }]
    g.patch_object(pid, {"changes": bad_changes}, actor="test")
    report = validate_proposal(g, pid)
    assert not report["ok"]
    assert any("unknown-behavior" in v[0] for v in report["violations"])


def test_proposal_rejected_for_protected_type_add():
    """Adding an AuthorityRule object without approval is blocked."""
    g = _fresh()
    ingest_paths(g, ["selfgraph/__init__.py"])
    extract_capabilities(g, use_llm=False)
    pid = propose_patch_for(g, "tighten policy")
    proposal = g.get_object(pid)
    bad_changes = list(proposal.data["changes"]) + [{
        "kind": "add_object",
        "type": "AuthorityRule",
        "data": {"name": "self-grant", "rule": "agent may auto-promote"},
    }, {
        "kind": "add_object",
        "type": "Capability",
        "data": {"name": "secret-power", "description": "anything"},
    }]
    g.patch_object(pid, {"changes": bad_changes}, actor="test")
    report = validate_proposal(g, pid)
    assert not report["ok"]
    assert sum(1 for v in report["violations"]
               if v[0] == "protected-type") >= 2


def test_proposal_rejected_for_unknown_change_kind():
    g = _fresh()
    ingest_paths(g, ["selfgraph/__init__.py"])
    extract_capabilities(g, use_llm=False)
    pid = propose_patch_for(g, "do thing")
    proposal = g.get_object(pid)
    bad_changes = list(proposal.data["changes"]) + [{
        "kind": "spawn_subprocess",
        "data": {"cmd": "ls"},
    }, {
        "kind": "add_policy",
        "policy": {"scope": "X", "can_approve": ["AuthorityRule"]},
    }]
    g.patch_object(pid, {"changes": bad_changes}, actor="test")
    report = validate_proposal(g, pid)
    assert not report["ok"]
    kinds = {v[0] for v in report["violations"]}
    assert "disallowed-kind" in kinds
    assert "permission-escalation" in kinds


def test_sandbox_promote_changes_main_graph():
    """promote=True must materialize new objects on the live graph and
    leave the proposal in status='applied'."""
    g = _fresh()
    ingest_paths(g, ["selfgraph/__init__.py"])
    extract_capabilities(g, use_llm=False)
    pid = propose_patch_for(g, "watch repo")
    validate_proposal(g, pid)
    before = len(g.all_objects())
    sandbox_apply(g, pid, promote=True)
    after = len(g.all_objects())
    assert after > before, "promote should add objects to the main graph"
    assert g.get_object(pid).data["status"] == "applied"


def test_validate_proposal_mutate_status_false():
    """mutate_status=False returns a report without touching the
    proposal's lifecycle status — used by cmd_promote to re-check a
    persisted proposal without overwriting an existing status."""
    g = _fresh()
    ingest_paths(g, ["selfgraph/__init__.py"])
    extract_capabilities(g, use_llm=False)
    pid = propose_patch_for(g, "non-mutating recheck")
    assert g.get_object(pid).data["status"] == "draft"
    report = validate_proposal(g, pid, mutate_status=False)
    assert report["ok"]
    assert g.get_object(pid).data["status"] == "draft"


def test_promote_lifecycle_requires_validated_status():
    """sandbox_apply must refuse to fork+apply a still-draft proposal."""
    g = _fresh()
    ingest_paths(g, ["selfgraph/__init__.py"])
    extract_capabilities(g, use_llm=False)
    pid = propose_patch_for(g, "no-validate-then-promote")
    # Intentionally skip validate_proposal — proposal stays in 'draft'.
    try:
        sandbox_apply(g, pid, promote=True)
    except ValueError as e:
        assert "validated" in str(e)
        return
    raise AssertionError("expected ValueError; sandbox_apply accepted a draft")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"running {name}...")
            fn()
            print(f"  ok")
    print("all tests passed")
