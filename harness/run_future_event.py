"""Future-event test: is a promoted bind_behavior binding causally live?

For each of three target bind_behavior proposals from the relaxed corpus
(reference IDs #578 monitor company → company_planner, #587 monitor
evidence → evidence_linker, #596 monitor question → question_generator)
this harness asks one mechanism-level question:

    Does the bound runtime behavior fire on a matching event after
    the proposal is promoted, AND does it NOT fire when promotion is
    withheld?

This is a TEST OF MECHANISM, not of usefulness or correctness. We do
not score whether the behavior's output is good — only whether it
runs at all.

The pipeline + binding executor
-------------------------------
1. Build a SQLite-backed selfgraph runtime mirroring run_corpus.py's
   setup (same ingest + extract + propose). This materializes the
   target proposal in the live graph.
2. validate_proposal(pid).
3. TREATMENT:
     a. sandbox_apply(promote=True) — the proposal is applied to the
        live graph, which materializes one or more BehaviorBinding
        objects naming the bound runtime behavior.
     b. Build a fresh SQLite-backed *test* runtime. The harness
        inspects BehaviorBinding objects in the pipeline graph; for
        every binding whose `behavior` name matches a diligence pack
        behavior the harness calls `Runtime.load_pack(diligence_pack,
        settings=...)` on the test runtime. This step — reading
        binding objects and registering the named behaviors with a
        runtime — is the "binding executor." selfgraph's
        sandbox_apply only writes the BehaviorBinding object; it
        does not itself register behaviors with the activegraph
        Runtime. The harness performs that last step here so the
        binding can be carried out, transparently and from outside
        the agent. This separation is documented on every trial row
        and in the meta block.
     c. Emit a fresh event matching the binding's on_event_type into
        the test runtime. Run to idle. Count behavior.started events
        whose payload behavior matches the bound name (short or
        diligence.<short>).
4. CONTROL:
     a. Skip the sandbox_apply promotion. No BehaviorBinding is
        materialized.
     b. Build the same kind of test runtime. The binding executor
        finds no relevant bindings, so it does NOT load any pack.
        The test runtime has zero behaviors registered for
        diligence.
     c. Emit the same event. Count behavior.started events.

Target result (per the paper's claim): fired_in_treatment=True and
fired_in_control=False for every trial. A diverging result is a real
finding worth reporting — it would mean the binding mechanism does
not gate firing as claimed.

LLM-free invariant
------------------
question_generator is @llm_behavior. The diligence pack ships a
RecordedDiligenceProvider that serves canned responses for the three
fixture companies — no live model call. The harness invariant
(harness/invariants.py) still refuses to run when ANTHROPIC_API_KEY
is set; this script attaches the recorded provider regardless.

Output
------
harness/results/future_event.jsonl — one row per trial.
harness/results/future_event.meta.json — companion meta + sha.

Run
---
PYTHONPATH=. python -m harness.run_future_event
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
from activegraph import Event, Graph, IDGen, Runtime
from activegraph.packs.diligence import pack as DILIGENCE_PACK
from activegraph.packs.diligence import DiligenceSettings
from activegraph.packs.diligence.behaviors import BEHAVIORS as DILIGENCE_BEHAVIORS
from activegraph.packs.diligence.fixtures import (
    RecordedDiligenceProvider,
    THREE_COMPANIES,
)

from selfgraph.extract import extract_capabilities
from selfgraph.guardrails import validate_proposal
from selfgraph.ingest import ingest_module_docs, ingest_paths
from selfgraph.propose import propose_patch_for
from selfgraph.sandbox import sandbox_apply


_RESULTS_DIR = Path("harness/results")
_JSONL_PATH = _RESULTS_DIR / "future_event.jsonl"
_META_PATH = _RESULTS_DIR / "future_event.meta.json"
_DB_PARENT = Path(".selfgraph-future-event")

_PKG_ROOT = os.path.dirname(activegraph.__file__)
_DILIGENCE_BEHAVIOR_SHORT_NAMES = {b.name for b in DILIGENCE_BEHAVIORS}

_FIXTURE_COMPANY = "Northwind Robotics"  # has the richest fixture coverage


# A trial spec: which proposal to materialize and which event to emit.
# `goal` is the propose-input that yields a bind_behavior change for
# `expected_behavior_short` (see selfgraph.propose._pick_behavior_bindings).
# `reference_proposal_id` is the ID this proposal got under the original
# relaxed corpus run (see harness/results/corpus.relaxed.jsonl) — kept
# for cross-reference with the paper; the live ID in THIS run will
# differ because we don't replay every preceding goal.
_TRIALS = [
    {
        "reference_proposal_id": "PatchProposal#578",
        "goal": "monitor company",
        "expected_behavior_short": "company_planner",
        "expected_on_event_type": "goal.created",
        "emit": {
            "kind": "run_goal",
            "goal": f"Diligence: {_FIXTURE_COMPANY}",
        },
    },
    {
        "reference_proposal_id": "PatchProposal#587",
        "goal": "monitor evidence",
        "expected_behavior_short": "evidence_linker",
        "expected_on_event_type": "object.created",
        "emit": {
            "kind": "add_object",
            "type": "evidence",
            # claim_id is a dangling reference; evidence_linker's body
            # checks graph.get_object(claim_id) is None and returns
            # early — but behavior.started/.completed still fires,
            # which is what the test observes. No LLM call.
            "data": {
                "text": "test evidence quote",
                "document_id": "doc-unused",
                "claim_id": "claim-unused",
                "location": "",
            },
        },
    },
    {
        "reference_proposal_id": "PatchProposal#596",
        "goal": "monitor question",
        "expected_behavior_short": "question_generator",
        "expected_on_event_type": "object.created",
        "emit": {
            "kind": "add_object",
            "type": "company",
            # Schema-valid Company payload. question_generator is
            # @llm_behavior; the runtime dispatches through the
            # RecordedDiligenceProvider keyed off the company name.
            "data": {
                "name": _FIXTURE_COMPANY,
                "description": "Future-event test company",
            },
        },
    },
]


# ---------- pipeline (mirror run_corpus.build_graph closely) ----------


def _fresh_db_dir(trial_idx: int, condition: str) -> Path:
    db_dir = _DB_PARENT / f"trial{trial_idx}_{condition}"
    if db_dir.exists():
        shutil.rmtree(db_dir)
    db_dir.mkdir(parents=True)
    return db_dir


def _build_pipeline(db_dir: Path) -> tuple[Graph, Runtime]:
    """SQLite-backed graph + runtime with the same ingest+extract as
    run_corpus.py. The pipeline runtime has no behaviors registered —
    propose_patch_for and the selfgraph pipeline don't need them; the
    runtime is here so sandbox_apply can take the real Runtime.fork
    path (the cleanliness invariant the corpus harness already enforces)."""
    g = Graph(ids=IDGen(), run_id="future-event-pipeline")
    rt = Runtime(g, persist_to=str(db_dir / "graph.db"))
    ingest_paths(g, ["selfgraph", "README.md", "demo.py"])
    ingest_module_docs(g, "activegraph", max_submodules=40)
    ingest_paths(g, [os.path.join(_PKG_ROOT, "packs")], max_bytes=400_000)
    extract_capabilities(g, use_llm=False)
    return g, rt


# ---------- binding executor (harness-side) ----------


def _diligence_bindings_in_graph(graph: Graph) -> list[dict[str, Any]]:
    """Return a list of BehaviorBinding rows whose `behavior` value
    refers to a diligence pack behavior. Empty when no bindings exist
    (control arm)."""
    out: list[dict[str, Any]] = []
    for b in graph.objects(type="BehaviorBinding"):
        name = b.data.get("behavior") or ""
        short = name.split(".")[-1]
        if short in _DILIGENCE_BEHAVIOR_SHORT_NAMES:
            out.append({
                "binding_object_id": b.id,
                "behavior": name,
                "on_event_type": b.data.get("on_event_type"),
                "scope_object_type": b.data.get("scope_object_type"),
            })
    return out


def _build_test_runtime(
    db_dir: Path,
    *,
    load_diligence: bool,
) -> Runtime:
    """Fresh SQLite-backed runtime for the firing test. Attaches the
    RecordedDiligenceProvider so any @llm_behavior dispatches stay
    offline. Loads the diligence pack iff `load_diligence` (the
    binding executor decides this from the pipeline graph)."""
    db_dir.mkdir(parents=True, exist_ok=True)
    g = Graph(ids=IDGen(), run_id="future-event-test")
    provider = RecordedDiligenceProvider(THREE_COMPANIES)
    rt = Runtime(
        g,
        persist_to=str(db_dir / "graph.db"),
        llm_provider=provider,
    )
    if load_diligence:
        rt.load_pack(
            DILIGENCE_PACK,
            settings=DiligenceSettings(
                auto_approve_risks=True,
                auto_approve_memos=True,
            ),
        )
    return rt


def _emit(rt: Runtime, emit_spec: dict[str, Any]) -> None:
    """Emit the trial's matching event into the test runtime."""
    kind = emit_spec["kind"]
    if kind == "run_goal":
        rt.run_goal(emit_spec["goal"])
    elif kind == "add_object":
        rt.graph.add_object(emit_spec["type"], dict(emit_spec["data"]),
                            actor="future-event-test")
        rt.run_until_idle()
    else:
        raise ValueError(f"unknown emit kind {kind!r}")


def _count_fires(rt: Runtime, target_short: str) -> int:
    """Number of behavior.started events naming the target (either as
    the short name or as the canonical `diligence.<short>`)."""
    canonical = f"diligence.{target_short}"
    n = 0
    for ev in rt.graph.events:
        if ev.type != "behavior.started":
            continue
        name = ev.payload.get("behavior") or ""
        if name == target_short or name == canonical:
            n += 1
    return n


# ---------- per-trial driver ----------


def _run_trial(trial_idx: int, spec: dict[str, Any]) -> dict[str, Any]:
    """Run TREATMENT and CONTROL for one proposal. Returns the JSONL row."""
    print(f"\n[future-event] trial {trial_idx + 1}/{len(_TRIALS)}  "
          f"{spec['reference_proposal_id']}  goal={spec['goal']!r}  "
          f"expects {spec['expected_behavior_short']!r} on "
          f"{spec['expected_on_event_type']!r}")

    out: dict[str, Any] = {
        "reference_proposal_id": spec["reference_proposal_id"],
        "goal": spec["goal"],
        "behavior_short": spec["expected_behavior_short"],
        "behavior_canonical": f"diligence.{spec['expected_behavior_short']}",
        "on_event_type": spec["expected_on_event_type"],
    }

    # ---- TREATMENT
    db_t = _fresh_db_dir(trial_idx, "treatment")
    g_t, rt_t = _build_pipeline(db_t)
    pid_t = propose_patch_for(g_t, spec["goal"])
    report_t = validate_proposal(g_t, pid_t)
    if not report_t["ok"]:
        raise RuntimeError(f"treatment proposal {pid_t} did not validate: "
                           f"{report_t['violations']}")
    bound_changes_t = _summarize_bind_behavior(g_t, pid_t)
    sandbox_apply(g_t, pid_t, runtime=rt_t, promote=True)
    bindings_t = _diligence_bindings_in_graph(g_t)
    test_rt_t = _build_test_runtime(db_t / "test-rt",
                                    load_diligence=bool(bindings_t))
    n_events_t_before = len(test_rt_t.graph.events)
    _emit(test_rt_t, spec["emit"])
    n_events_t_after = len(test_rt_t.graph.events)
    fire_count_t = _count_fires(test_rt_t, spec["expected_behavior_short"])

    # ---- CONTROL
    db_c = _fresh_db_dir(trial_idx, "control")
    g_c, rt_c = _build_pipeline(db_c)
    pid_c = propose_patch_for(g_c, spec["goal"])
    report_c = validate_proposal(g_c, pid_c)
    if not report_c["ok"]:
        raise RuntimeError(f"control proposal {pid_c} did not validate: "
                           f"{report_c['violations']}")
    bound_changes_c = _summarize_bind_behavior(g_c, pid_c)
    # NO sandbox_apply with promote=True. (We don't call any sandbox at
    # all — control is "promotion withheld," i.e. the live graph never
    # sees the changes that would materialize a BehaviorBinding.)
    bindings_c = _diligence_bindings_in_graph(g_c)
    test_rt_c = _build_test_runtime(db_c / "test-rt",
                                    load_diligence=bool(bindings_c))
    n_events_c_before = len(test_rt_c.graph.events)
    _emit(test_rt_c, spec["emit"])
    n_events_c_after = len(test_rt_c.graph.events)
    fire_count_c = _count_fires(test_rt_c, spec["expected_behavior_short"])

    out.update({
        "treatment": {
            "live_proposal_id": pid_t,
            "bind_behavior_changes_in_proposal": bound_changes_t,
            "behavior_bindings_after_promote": bindings_t,
            "diligence_pack_loaded_into_test_runtime": bool(bindings_t),
            "test_runtime_events_total": n_events_t_after,
            "test_runtime_events_emitted_by_trial":
                n_events_t_after - n_events_t_before,
            "fire_count": fire_count_t,
            "fired": fire_count_t > 0,
        },
        "control": {
            "live_proposal_id": pid_c,
            "bind_behavior_changes_in_proposal": bound_changes_c,
            "behavior_bindings_after_promote": bindings_c,
            "diligence_pack_loaded_into_test_runtime": bool(bindings_c),
            "test_runtime_events_total": n_events_c_after,
            "test_runtime_events_emitted_by_trial":
                n_events_c_after - n_events_c_before,
            "fire_count": fire_count_c,
            "fired": fire_count_c > 0,
        },
        "fired_in_treatment": fire_count_t > 0,
        "fired_in_control": fire_count_c > 0,
    })
    print(f"  treatment fired={out['fired_in_treatment']} "
          f"(n={fire_count_t}, bindings={len(bindings_t)})  "
          f"control fired={out['fired_in_control']} "
          f"(n={fire_count_c}, bindings={len(bindings_c)})")
    return out


def _summarize_bind_behavior(graph: Graph, pid: str) -> list[dict[str, Any]]:
    """Return the bind_behavior changes inside a proposal, for the row."""
    obj = graph.get_object(pid)
    out: list[dict[str, Any]] = []
    for c in (obj.data.get("changes") or []):
        if c.get("kind") != "bind_behavior":
            continue
        out.append({
            "behavior": c.get("behavior"),
            "on_event_type": c.get("on_event_type"),
            "scope_object_type": c.get("scope_object_type"),
        })
    return out


# ---------- entry point ----------


def main(argv: list[str] | None = None) -> int:
    from harness.invariants import require_no_llm_env
    require_no_llm_env()

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if _DB_PARENT.exists():
        shutil.rmtree(_DB_PARENT)
    _DB_PARENT.mkdir(parents=True)

    rows: list[dict[str, Any]] = []
    for i, spec in enumerate(_TRIALS):
        rows.append(_run_trial(i, spec))

    with _JSONL_PATH.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    sha = hashlib.sha256(_JSONL_PATH.read_bytes()).hexdigest()[:16]
    meta = {
        "n_trials": len(rows),
        "fixture_company": _FIXTURE_COMPANY,
        "all_treatment_fired": all(r["fired_in_treatment"] for r in rows),
        "all_control_did_not_fire": all(not r["fired_in_control"] for r in rows),
        "jsonl_path": str(_JSONL_PATH),
        "jsonl_sha256_16": sha,
        "diligence_pack_name": DILIGENCE_PACK.name,
        "diligence_pack_version": DILIGENCE_PACK.version,
        # LLM-free invariant: the recorded fixture provider is used
        # for @llm_behavior dispatches in the test runtime; no live
        # API call. The harness still refuses to run when
        # ANTHROPIC_API_KEY is set (see harness/invariants.py).
        "llm_augment_active": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "objecttype_match_mode":
            os.environ.get("SELFGRAPH_OBJECTTYPE_MATCH", "relaxed"),
        "binding_executor_notes": (
            "The harness scans the post-promotion graph for "
            "BehaviorBinding objects whose `behavior` resolves to a "
            "diligence pack behavior, and loads the diligence pack "
            "into the test runtime iff at least one such binding is "
            "found. selfgraph's sandbox_apply only materializes the "
            "BehaviorBinding object; the registration with the "
            "runtime registry is performed by this harness. Without "
            "promotion, no BehaviorBinding exists, so the harness "
            "does not load the pack and the bound behavior has "
            "nothing to fire against."
        ),
    }
    _META_PATH.write_text(json.dumps(meta, indent=2, sort_keys=True))

    print()
    print(f"[future-event] wrote {len(rows)} rows → {_JSONL_PATH}")
    print(f"[future-event] sha256[:16]={sha}")
    print(f"[future-event] all treatment fired: {meta['all_treatment_fired']}")
    print(f"[future-event] all control did NOT fire: "
          f"{meta['all_control_did_not_fire']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
