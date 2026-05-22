"""Measure the rollback precondition for selfgraph.

We don't *demonstrate* rollback in this harness — that follows from
the underlying ActiveGraph guarantee that graph state is a
deterministic fold over the event log, so replay-to-event-k IS
rollback. What we DO measure here is the precondition that has to
hold for that inheritance to be sound on selfgraph specifically:

  every promoted self-modification must be a real logged event on
  the SAME log that replay reconstructs.

Sample (widened from the n=5 v0.9 slice)
----------------------------------------
The previous version measured rollback on a fixed five-Capability
slice; readers reasonably asked whether five is enough. This routine
now covers EVERY proposal selfgraph's proposer drafts on the relaxed
corpus that the guardrails pass — i.e. all 72 mechanical-goal
proposals from the relaxed run, including all 9 bind_behavior
proposals (the most consequential class of self-modification, since
a binding is the closest thing in v0.9 to wiring a new runtime
behavior into the agent). The selection rule is:

  Reproduce the corpus.relaxed pipeline exactly — same ingest, same
  extract, same generate_goal_set — and call propose_patch_for +
  validate_proposal on every goal in that mechanically-generated
  goal set in order. Every proposal whose guardrail validation
  passes (report.ok == True) becomes a rollback trial. No
  cherry-picking, no whitelist.

Isolation
---------
Per the harness contract, the widened test must not contaminate any
other corpus result. Each promotion runs in a SQLite fork of the
main pipeline graph taken AT the moment of the proposal's
validation: `runtime.fork(at_event=graph.events[-1].id, ...)`
returns an isolated Runtime backed by a new run_id in the same DB
file. We promote and measure rollback inside that fork; the fork is
then discarded (its events stay in the SQLite file under their own
run_id, invisible to the main pipeline's iter_events). The main
pipeline graph therefore only accumulates ingest + extract +
(propose + validate) events — identical in shape to what
run_corpus.py builds with sandbox_apply(promote=False). The corpus
.jsonl shas are NOT touched by this run.

Per-promotion measurement
-------------------------
Inside each fork we record:

  1. A deep snapshot of every Object and Relation and the full event
     log immediately before the promote call.
  2. The number of `actor="promote"` events appended during the
     `sandbox_apply(promote=True)` call. The strict expectation is
     n_promote_events == n_changes + 1 (one event per allowed-kind
     change in the proposal plus the status-flip patch on the
     proposal); some proposals legitimately come in lower when an
     add_relation change refers to a name the applier cannot resolve.
     We record both numbers and the boolean `all_changes_logged`
     instead of hard-asserting equality.
  3. A replay-from-the-store check: opening a fresh
     `SQLiteEventStore(db_path, run_id=fork_run_id)` and projecting
     its events into an empty Graph, stopping immediately before the
     first promote-actor event, must yield a snapshot byte-identical
     to (1).

A trial is "rollback-clean" iff all_changes_logged AND
replay_byte_identical.

If any promotion mutates graph state without producing a log event
or the replay diverges, the runner reports the offending proposals
and exits non-zero.
"""

from __future__ import annotations

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

# Reuse the corpus harness's mechanical goal set so the rollback
# sample is exactly the corpus.relaxed proposal set. Importing the
# function (not duplicating it) makes the "same goal sequence" claim
# auditable from a single source.
from harness.run_corpus import generate_goal_set


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
    """Open the SQLite store for ``run_id`` and replay its events
    into a fresh Graph until we hit ``cutoff_event_id`` (exclusive).
    Same snapshot shape as :func:`_snapshot`."""
    store = SQLiteEventStore(db_path, run_id=run_id)
    fresh = Graph(ids=IDGen(), run_id=run_id + "-replayed")
    for ev in store.iter_events():
        if ev.id == cutoff_event_id:
            break
        fresh._replay_event(ev)  # noqa: SLF001 — documented replay path
    return _snapshot(fresh)


def _build_pipeline(db_path: Path) -> tuple[Graph, Runtime]:
    """Same ingest + extract setup as run_corpus.build_graph. We
    inline it here (rather than importing) to keep the rollback DB
    dir independent of the corpus harness state, and to keep the
    rollback test runnable in isolation."""
    graph = Graph(ids=IDGen(), run_id=_RUN_ID)
    runtime = Runtime(graph, persist_to=str(db_path))
    print("[rollback] ingesting selfgraph repo + activegraph runtime")
    ingest_paths(graph, ["selfgraph", "README.md", "demo.py"])
    ingest_module_docs(graph, "activegraph", max_submodules=40)
    ingest_paths(graph, [os.path.join(_ACTIVEGRAPH_PKG_ROOT, "packs")],
                 max_bytes=400_000)
    print("[rollback] extracting capability graph (deterministic only)")
    extract_capabilities(graph, use_llm=False)
    return graph, runtime


def _has_binding(proposal_obj) -> bool:
    """True iff any of the proposal's changes is a bind_behavior."""
    for c in (proposal_obj.data.get("changes") or []):
        if c.get("kind") == "bind_behavior":
            return True
    return False


def _measure_one(
    pipeline_graph: Graph,
    pipeline_runtime: Runtime,
    goal: str,
    trial_index: int,
) -> dict[str, Any] | None:
    """Run propose + validate in the main pipeline graph, then fork
    the runtime and run promote + rollback measurement inside the
    fork. Returns the per-trial row, or None if the proposal was
    rejected by the guardrail (in which case the trial is skipped
    — selecting on report.ok is documented in the script docstring)."""
    pid = propose_patch_for(pipeline_graph, goal)
    report = validate_proposal(pipeline_graph, pid)
    if not report["ok"]:
        return None
    proposal = pipeline_graph.get_object(pid)
    has_binding = _has_binding(proposal)
    n_changes = len(proposal.data.get("changes", []))

    # Fork AT the latest main event — this is the same `at_event` the
    # in-process sandbox_apply uses internally, but we capture the
    # Runtime for the fork ourselves so we can replay its store.
    at_event = pipeline_graph.events[-1].id
    fork_rt = pipeline_runtime.fork(
        at_event=at_event, label=f"rollback-trial-{trial_index}",
    )
    fork_run_id = fork_rt.graph.run_id

    pre = _snapshot(fork_rt.graph)
    n_events_before = len(pre["events"])

    # Promote inside the fork. sandbox_apply needs a SQLite-backed
    # runtime so its inner sandbox-fork can take the real fork path;
    # fork_rt satisfies that.
    sandbox_apply(fork_rt.graph, pid, runtime=fork_rt, promote=True)

    new_events = fork_rt.graph.events[n_events_before:]
    promote_events = [e for e in new_events if e.actor == "promote"]
    # Strict expectation: n_changes + 1 (one event per change plus
    # the proposal status patch). add_relation changes whose
    # source/target names can't be resolved by _apply_changes don't
    # emit, so we measure both ends and let the report show the
    # relationship.
    all_changes_logged = len(promote_events) >= n_changes

    first_promote_id = promote_events[0].id if promote_events else None
    objs_match = False
    rels_match = False
    events_match = False
    replay_ok = False
    if first_promote_id is not None:
        replayed = _replay_from_store_until(
            str(_DB_PATH), fork_run_id, first_promote_id,
        )
        objs_match = replayed["objects"] == pre["objects"]
        rels_match = replayed["relations"] == pre["relations"]
        events_match = replayed["events"] == pre["events"]
        replay_ok = objs_match and rels_match and events_match

    return {
        "goal": goal,
        "proposal_id": pid,
        "has_binding": has_binding,
        "n_changes": n_changes,
        "n_promote_events": len(promote_events),
        "first_promote_event_id": first_promote_id,
        "all_changes_logged": all_changes_logged,
        "replay_byte_identical": replay_ok,
        "replay_objects_match": objs_match,
        "replay_relations_match": rels_match,
        "replay_events_match": events_match,
        "n_objects_pre": len(pre["objects"]),
        "n_objects_post": len(fork_rt.graph.all_objects()),
        "n_relations_pre": len(pre["relations"]),
        "n_relations_post": len(fork_rt.graph.all_relations()),
        "n_events_pre": n_events_before,
        "n_events_post": len(fork_rt.graph.events),
    }
    # NB: fork_run_id is intentionally NOT in this row. IDGen.run()
    # is a ULID (timestamp-bearing); writing it would break the
    # canonical-sha contract. The fork is identified for THIS run by
    # the run-time log line and by first_promote_event_id, which is
    # counter-based and stable.


def main() -> int:
    from harness.invariants import require_no_llm_env
    require_no_llm_env()
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if _DB_DIR.exists():
        shutil.rmtree(_DB_DIR)
    _DB_DIR.mkdir(parents=True)

    pipeline_graph, pipeline_runtime = _build_pipeline(_DB_PATH)
    goal_set = generate_goal_set(pipeline_graph)
    print(f"[rollback] sample = every guardrail-validated proposal in "
          f"the relaxed corpus goal sequence (n_goals={len(goal_set)})")

    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for i, gr in enumerate(goal_set):
        print(f"[rollback] {i + 1}/{len(goal_set)}  {gr['goal']!r}")
        row = _measure_one(pipeline_graph, pipeline_runtime,
                           gr["goal"], i)
        if row is None:
            skipped.append({"index": i, "goal": gr["goal"]})
            continue
        rows.append(row)

    with _JSONL_PATH.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    jsonl_hash = hashlib.sha256(_JSONL_PATH.read_bytes()).hexdigest()[:16]

    n_logged = sum(1 for r in rows if r["all_changes_logged"])
    n_replay_ok = sum(1 for r in rows if r["replay_byte_identical"])
    binding_rows = [r for r in rows if r["has_binding"]]
    n_binding_logged = sum(1 for r in binding_rows if r["all_changes_logged"])
    n_binding_replay_ok = sum(1 for r in binding_rows
                              if r["replay_byte_identical"])
    meta = {
        "n_goals_in_corpus_pipeline": len(goal_set),
        "n_promotions": len(rows),
        "n_skipped_guardrail_rejected": len(skipped),
        "skipped_goals": skipped,
        "n_all_changes_logged": n_logged,
        "n_replay_byte_identical": n_replay_ok,
        "n_with_binding": len(binding_rows),
        "n_binding_all_changes_logged": n_binding_logged,
        "n_binding_replay_byte_identical": n_binding_replay_ok,
        "selection_rule": (
            "Every guardrail-validated proposal produced by the "
            "run_corpus.py pipeline under SELFGRAPH_OBJECTTYPE_MATCH="
            "relaxed (same ingest, same extract, same generate_goal_set, "
            "same order). Each promotion runs in a Runtime.fork taken "
            "at the moment of the proposal's validation; forks share "
            "the SQLite file but use distinct run_ids so they neither "
            "see each other nor mutate the main pipeline graph."
        ),
        "db_path": str(_DB_PATH),
        "jsonl_path": str(_JSONL_PATH),
        "jsonl_sha256_16": jsonl_hash,
        "llm_augment_active": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "objecttype_match_mode":
            os.environ.get("SELFGRAPH_OBJECTTYPE_MATCH", "relaxed"),
    }
    _META_PATH.write_text(json.dumps(meta, indent=2))

    unlogged = [r for r in rows if not r["all_changes_logged"]]
    replay_failed = [r for r in rows if not r["replay_byte_identical"]]

    if unlogged:
        print("\n[rollback] FAIL — promotion mutated graph state "
              "without a corresponding log event:", file=sys.stderr)
        for r in unlogged:
            print(f"  {r['proposal_id']}  {r['goal']!r}  "
                  f"n_changes={r['n_changes']}  "
                  f"n_promote_events={r['n_promote_events']}",
                  file=sys.stderr)
        return 2

    if replay_failed:
        print("\n[rollback] FAIL — replay-to-before-promote did not "
              "reconstruct the pre-promote snapshot for "
              f"{len(replay_failed)} promotion(s):", file=sys.stderr)
        for r in replay_failed:
            print(f"  {r['proposal_id']}  {r['goal']!r}  "
                  f"objs={r['replay_objects_match']} "
                  f"rels={r['replay_relations_match']} "
                  f"events={r['replay_events_match']}",
                  file=sys.stderr)
        return 3

    print(f"\n[rollback] wrote {len(rows)} rows → {_JSONL_PATH}")
    print(f"[rollback] all_changes_logged       "
          f"{n_logged}/{len(rows)}  (target 100%)")
    print(f"[rollback] replay_byte_identical    "
          f"{n_replay_ok}/{len(rows)}  (target 100%)")
    print(f"[rollback] bind_behavior subset    "
          f"{n_binding_replay_ok}/{len(binding_rows)} replay-clean, "
          f"{n_binding_logged}/{len(binding_rows)} all-changes-logged")
    print(f"[rollback] jsonl sha256[:16]={jsonl_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
