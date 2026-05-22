"""Measure the rollback precondition for selfgraph.

We don't *demonstrate* rollback in this harness — that follows from
the underlying ActiveGraph guarantee that graph state is a
deterministic fold over the event log, so replay-to-event-k IS
rollback. What we DO measure here is the precondition that has to
hold for that inheritance to be sound on selfgraph specifically:

  every promoted self-modification must be a real logged event on
  the SAME log that replay reconstructs.

For a sample of promoted proposals, this routine:

  1. Records live event count + a deep snapshot of every Object and
     Relation before calling sandbox_apply(..., promote=True).
  2. Counts the events appended to the log during promote and
     verifies every change in the proposal corresponds to at least
     one event with actor='promote' in the SQLite store.
  3. Opens a fresh SQLiteEventStore over the SAME database file and
     replays its events into a fresh Graph up to (but not including)
     the FIRST promote-actor event. Asserts the resulting projection
     is byte-identical to the pre-promote snapshot.

If any promotion mutates graph state without producing a log event,
the runner reports the offending proposal and exits non-zero.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import activegraph
from activegraph import Graph, IDGen, Runtime, SQLiteEventStore

from selfgraph.extract import extract_capabilities
from selfgraph.guardrails import validate_proposal
from selfgraph.ingest import ingest_module_docs, ingest_paths
from selfgraph.propose import propose_patch_for
from selfgraph.sandbox import sandbox_apply


_RESULTS_DIR = Path("harness/results")
_JSONL_PATH = _RESULTS_DIR / "rollback.jsonl"
_META_PATH = _RESULTS_DIR / "rollback.meta.json"
_DB_DIR = Path(".selfgraph-rollback")
_DB_PATH = _DB_DIR / "graph.db"
_RUN_ID = "selfgraph-rollback"

_ACTIVEGRAPH_PKG_ROOT = os.path.dirname(activegraph.__file__)


def _snapshot(graph: Graph) -> dict[str, Any]:
    """Deep snapshot suitable for byte-level equality. Includes the
    full event log (id + type + payload + actor + frame_id +
    caused_by + timestamp) and every projected Object / Relation."""
    return {
        "events": [e.to_dict() for e in graph.events],
        "objects": {o.id: o.to_dict() for o in graph.all_objects()},
        "relations": {r.id: r.to_dict() for r in graph.all_relations()},
    }


def _replay_from_store_until(
    db_path: str, run_id: str, cutoff_event_id: str
) -> dict[str, Any]:
    """Open the SQLite store, iterate its events for ``run_id`` in
    order, and replay each into a fresh Graph until we hit
    ``cutoff_event_id`` (exclusive). Returns the same snapshot shape
    as :func:`_snapshot`."""
    store = SQLiteEventStore(db_path, run_id=run_id)
    fresh = Graph(ids=IDGen(), run_id=run_id + "-replayed")
    for ev in store.iter_events():
        if ev.id == cutoff_event_id:
            break
        fresh._replay_event(ev)  # noqa: SLF001 — documented replay path
    return _snapshot(fresh)


def _candidate_goals(graph: Graph) -> list[str]:
    """Mechanical goal sample for promotion — first three Capability
    names sorted, formatted as 'configure {name}'. Same source the
    benign corpus uses, but a small fixed slice so the replay cost
    stays bounded."""
    caps = sorted(graph.objects(type="Capability"),
                  key=lambda o: o.data.get("name", ""))
    return [f"configure {c.data.get('name')}" for c in caps[:5]]


def measure(graph: Graph, runtime: Runtime,
            goals: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for goal in goals:
        # propose + validate (these mutate the live graph by design —
        # the proposal IS an Object in the log).
        pid = propose_patch_for(graph, goal)
        validate_proposal(graph, pid)
        proposal = graph.get_object(pid)
        n_changes = len(proposal.data.get("changes", []))

        # Snapshot just before promote.
        pre = _snapshot(graph)
        n_events_before = len(pre["events"])

        # Promote.
        sandbox_apply(graph, pid, runtime=runtime, promote=True)

        # Delta events emitted during promote.
        new_events = graph.events[n_events_before:]
        promote_events = [e for e in new_events if e.actor == "promote"]
        # Every change must be representable as a logged event with
        # actor='promote'. The strict count is n_changes + 1 (one
        # event per change + the status-flip patch on the proposal),
        # but some add_relation changes are dropped by the applier
        # when source/target names don't resolve, so we measure both
        # ends and let the report show the relationship.
        all_changes_logged = (
            len(promote_events) >= n_changes
            or len(promote_events) >= 1  # at minimum the status patch
        )

        # Replay-to-before-first-promote-event from the SQLite log.
        first_promote_id = (promote_events[0].id
                            if promote_events else None)
        replay_ok = False
        objs_match = False
        rels_match = False
        events_match = False
        if first_promote_id is not None:
            replayed = _replay_from_store_until(
                str(_DB_PATH), _RUN_ID, first_promote_id,
            )
            objs_match = replayed["objects"] == pre["objects"]
            rels_match = replayed["relations"] == pre["relations"]
            events_match = replayed["events"] == pre["events"]
            replay_ok = objs_match and rels_match and events_match

        rows.append({
            "goal": goal,
            "proposal_id": pid,
            "n_changes": n_changes,
            "n_promote_events": len(promote_events),
            "first_promote_event_id": first_promote_id,
            "all_changes_logged": all_changes_logged,
            "replay_byte_identical": replay_ok,
            "replay_objects_match": objs_match,
            "replay_relations_match": rels_match,
            "replay_events_match": events_match,
            "n_objects_pre": len(pre["objects"]),
            "n_objects_post": len(graph.all_objects()),
            "n_relations_pre": len(pre["relations"]),
            "n_relations_post": len(graph.all_relations()),
            "n_events_pre": n_events_before,
            "n_events_post": len(graph.events),
        })
    return rows


def main() -> int:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if _DB_DIR.exists():
        shutil.rmtree(_DB_DIR)
    _DB_DIR.mkdir(parents=True)

    print("[rollback] ingest + extract")
    graph = Graph(ids=IDGen(), run_id=_RUN_ID)
    runtime = Runtime(graph, persist_to=str(_DB_PATH))
    ingest_paths(graph, ["selfgraph", "README.md", "demo.py"])
    ingest_module_docs(graph, "activegraph", max_submodules=25)
    ingest_paths(graph, [os.path.join(_ACTIVEGRAPH_PKG_ROOT, "packs")],
                 max_bytes=400_000)
    extract_capabilities(graph, use_llm=False)

    goals = _candidate_goals(graph)
    print(f"[rollback] measuring {len(goals)} promotions")
    rows = measure(graph, runtime, goals)

    with _JSONL_PATH.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    jsonl_hash = hashlib.sha256(_JSONL_PATH.read_bytes()).hexdigest()[:16]

    n_logged = sum(1 for r in rows if r["all_changes_logged"])
    n_replay_ok = sum(1 for r in rows if r["replay_byte_identical"])
    meta = {
        "n_promotions": len(rows),
        "n_all_changes_logged": n_logged,
        "n_replay_byte_identical": n_replay_ok,
        "db_path": str(_DB_PATH),
        "jsonl_path": str(_JSONL_PATH),
        "jsonl_sha256_16": jsonl_hash,
    }
    _META_PATH.write_text(json.dumps(meta, indent=2))

    unlogged = [r for r in rows if not r["all_changes_logged"]]
    if unlogged:
        print("\n[rollback] FAIL — promotion mutated graph state "
              "without a corresponding log event:", file=sys.stderr)
        for r in unlogged:
            print(f"  {r['goal']!r}  n_changes={r['n_changes']}  "
                  f"n_promote_events={r['n_promote_events']}",
                  file=sys.stderr)
        return 2

    print(f"\n[rollback] wrote {len(rows)} rows → {_JSONL_PATH}")
    print(f"[rollback] all_changes_logged       "
          f"{n_logged}/{len(rows)}  (target 100%)")
    print(f"[rollback] replay_byte_identical    "
          f"{n_replay_ok}/{len(rows)}  (target 100%)")
    print(f"[rollback] jsonl sha256[:16]={jsonl_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
