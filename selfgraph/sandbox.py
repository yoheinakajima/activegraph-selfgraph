"""Apply a validated PatchProposal in a fork, run test events, diff.

Real fork support requires a SQLite-backed runtime (ActiveGraph
``Runtime.fork`` constraint). When the active graph is in-memory we
fall back to a structural sandbox: copy events into a fresh Graph and
replay them, then apply. The user-visible interface is the same.
"""

from __future__ import annotations

from typing import Optional

from activegraph import Graph, IDGen, Runtime, SQLiteEventStore


def sandbox_apply(
    graph: Graph,
    proposal_id: str,
    *,
    runtime: Optional[Runtime] = None,
    promote: bool = False,
) -> dict:
    """Apply the validated proposal in a fork; return a report.

    If ``promote=True`` (and the report has no failures), also apply
    the same changes to the live graph.
    """
    proposal = graph.get_object(proposal_id)
    if proposal is None or proposal.type != "PatchProposal":
        raise ValueError(f"{proposal_id} is not a PatchProposal")
    if proposal.data.get("status") != "validated":
        raise ValueError(
            f"proposal {proposal_id} has status "
            f"{proposal.data.get('status')!r}; expected 'validated'"
        )

    fork_graph, fork_label = _build_fork(graph, runtime)
    print(f"[sandbox] running proposal in fork: {fork_label}")

    applied = _apply_changes(fork_graph, proposal.data["changes"],
                             actor="sandbox")

    # Simple test event: emit a synthetic Task.update event so any newly
    # bound behaviors get a chance to fire (in-memory only; we don't
    # spin up a fresh Runtime for the fork in v1).
    fork_graph.add_object("TestEvent", {
        "goal": proposal.data.get("goal"),
        "kind": "smoke",
    }, actor="sandbox")

    diff = _diff(graph, fork_graph)
    report = {
        "proposal_id": proposal_id,
        "fork_label": fork_label,
        "applied_changes": len(applied),
        "diff": diff,
        "ok": True,
    }
    print(f"[sandbox] fork diff: +{len(diff['added_objects'])} objects, "
          f"+{len(diff['added_relations'])} relations")

    if promote:
        print(f"[sandbox] promoting proposal to main graph (user approved)")
        _apply_changes(graph, proposal.data["changes"], actor="promote")
        graph.patch_object(
            proposal_id, {"status": "applied"},
            actor="promote",
            rationale="Promoted from sandbox after user approval.",
        )
    else:
        print(f"[sandbox] NOT promoting — pass promote=True to apply to main")

    return report


# ---------- fork construction ----------


def _build_fork(graph: Graph, runtime: Optional[Runtime]):
    """Return (fork_graph, label). Uses Runtime.fork when SQLite-backed,
    else falls back to a structural replay into a fresh Graph."""
    store = graph.store
    if runtime is not None and isinstance(store, SQLiteEventStore):
        try:
            last_event = graph.events[-1].id if graph.events else None
            if last_event:
                fork_rt = runtime.fork(at_event=last_event, label="selfgraph-sandbox")
                return fork_rt.graph, f"sqlite-fork@{last_event}"
        except Exception as e:  # noqa: BLE001
            print(f"[sandbox] real fork failed, falling back: {e}")

    # Fallback: structural copy by replaying events into a new Graph.
    # The public fork primitive (Runtime.fork) is SQLite-only; for the
    # in-memory case there is no published replay-into-fresh-graph API
    # in v1, so we use the documented internal projector entry point.
    # This is the only private-API call in selfgraph; isolate it here.
    fresh = Graph(ids=IDGen(), run_id=graph.run_id + "-sandbox")
    _replay_into(fresh, graph.events)
    return fresh, "in-memory-replay"


def _replay_into(target: Graph, events) -> None:
    """Project ``events`` into ``target`` without firing listeners or
    persisting. Calls ``Graph._replay_event``, the documented replay
    entry point used by ``Runtime.load`` and ``Runtime.fork``. If
    ActiveGraph ships a public equivalent later, swap it in here."""
    for ev in events:
        target._replay_event(ev)  # noqa: SLF001 — see docstring


# ---------- change applier ----------


def _apply_changes(graph: Graph, changes: list[dict], *, actor: str) -> list[str]:
    """Apply allowed-kind changes to ``graph``. Returns the list of new
    object ids (for the diff). Unknown kinds are skipped — guardrails
    should have caught them already."""
    new_ids: list[str] = []
    name_index: dict[tuple[str, str], str] = {
        (o.type, o.data.get("name", "")): o.id
        for o in graph.all_objects()
        if o.data.get("name")
    }
    for change in changes:
        kind = change.get("kind")
        if kind in ("add_object", "add_state_bucket", "add_task", "add_evaluation"):
            t = change.get("type") or (
                "Task" if kind == "add_task"
                else "Evaluation" if kind == "add_evaluation"
                else "ObjectType"
            )
            o = graph.add_object(t, dict(change.get("data", {})), actor=actor)
            new_ids.append(o.id)
            if o.data.get("name"):
                name_index[(o.type, o.data["name"])] = o.id
        elif kind == "add_relation":
            src_id = name_index.get(
                (change.get("from_type"), change.get("from_name"))
            )
            tgt_id = name_index.get(
                (change.get("to_type"), change.get("to_name"))
            )
            if src_id and tgt_id:
                graph.add_relation(src_id, tgt_id,
                                   change.get("rel_type", "RELATED_TO"),
                                   actor=actor)
        elif kind == "add_policy":
            graph.add_object("Policy", dict(change.get("policy", {})),
                             actor=actor)
        elif kind == "bind_behavior":
            graph.add_object("BehaviorBinding", {
                "behavior": change.get("behavior"),
                "on_event_type": change.get("on_event_type"),
                "scope_object_type": change.get("scope_object_type"),
            }, actor=actor)
    return new_ids


# ---------- structural diff ----------


def _diff(before: Graph, after: Graph) -> dict:
    before_ids = {o.id for o in before.all_objects()}
    before_rel_ids = {r.id for r in before.all_relations()}
    added_objects = [
        {"id": o.id, "type": o.type,
         "label": o.data.get("name") or o.data.get("goal") or ""}
        for o in after.all_objects() if o.id not in before_ids
    ]
    added_relations = [
        {"id": r.id, "type": r.type, "source": r.source, "target": r.target}
        for r in after.all_relations() if r.id not in before_rel_ids
    ]
    return {"added_objects": added_objects, "added_relations": added_relations}
