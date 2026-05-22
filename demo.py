"""Scripted demo. Run:  python demo.py

Walks the user through:

  1. "Read this repo and build your capability graph."
  2. "What can you do?"
  3. "How would you implement forking?"
  4. Grounded proposal:  a goal whose tokens overlap extracted
                          ObjectType nodes — exercises the
                          GROUNDED_IN / PATCH_MODIFIES citation path.
  5. Fallback proposal: a goal that overlaps nothing — exercises the
                          built-in scaffold path for explicit contrast.

The two proposals are deliberately paired. Reading the citation
output side-by-side is the demo's strongest signal: every change
either traces to a node the agent really extracted (with a path
and id) or is labelled as scaffold / authored / seed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import activegraph
from activegraph import Graph, IDGen, Runtime

from selfgraph.extract import extract_capabilities
from selfgraph.guardrails import validate_proposal
from selfgraph.ingest import ingest_module_docs, ingest_paths
from selfgraph.propose import propose_patch_for
from selfgraph.query import answer_question, summarize_capabilities, trace_grounding
from selfgraph.sandbox import sandbox_apply


_DEMO_DB_DIR = ".selfgraph-demo"
_DEMO_DB_PATH = f"{_DEMO_DB_DIR}/graph.db"

# Goal for the grounded proposal. The tokens 'patch', 'proposal',
# 'policy', and 'bindings' deliberately overlap ObjectType nodes the
# extractor pulled from selfgraph's own source (PatchProposal, Policy,
# BehaviorBinding). The proposer's GROUNDED_IN branch wires the
# fallback atom/snapshot scaffold to those extracted ObjectTypes, so
# PATCH_MODIFIES is non-empty and the citation trace shows real
# source paths.
_GROUNDED_GOAL = "monitor patch proposal lifecycle and policy bindings"

# Goal for the fallback proposal. No tokens overlap any extracted
# Behavior or ObjectType, so the scaffold path runs without any
# GROUNDED_IN grounding — every cited node is scaffold, seed, or
# authored. This is the explicit contrast.
_FALLBACK_GOAL = "track project updates"


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def run() -> None:
    if Path(_DEMO_DB_DIR).exists():
        shutil.rmtree(_DEMO_DB_DIR)
    Path(_DEMO_DB_DIR).mkdir()

    graph = Graph(ids=IDGen(), run_id="selfgraph-demo")
    runtime = Runtime(graph, persist_to=_DEMO_DB_PATH)

    _banner("USER: Read this repo and build your capability graph.")
    ingest_paths(graph, ["selfgraph", "README.md", "demo.py"])
    # Modules give the agent a structured view of the runtime it sits on.
    ingest_module_docs(graph, "activegraph", max_submodules=25)
    # Also ingest the activegraph package source files so the
    # regex-based behavior extractor catches real @behavior /
    # @llm_behavior decorators in the runtime — the citation chain
    # for bind_behavior changes points back at the actual file inside
    # the installed activegraph package.
    pkg_root = os.path.dirname(activegraph.__file__)
    ingest_paths(graph, [os.path.join(pkg_root, "packs")], max_bytes=400_000)
    extract_capabilities(graph)
    print(f"\n[graph] {len(graph.all_objects())} objects, "
          f"{len(graph.all_relations())} relations, "
          f"{len(graph.events)} events")
    _print_extracted_summary(graph)

    _banner("USER: What can you do?")
    print(answer_question(graph, "what can you do?"))

    _banner("USER: How would you implement forking?")
    print(answer_question(graph, "how would you implement forking?"))

    # ---------- Beat 1: Grounded proposal ----------
    _banner(f"USER: {_GROUNDED_GOAL}  (grounded proposal)")
    print("This goal's tokens overlap ObjectType nodes the extractor "
          "pulled out of the ingested source. Expect non-empty "
          "PATCH_MODIFIES and at least one [extracted from: ...] "
          "citation in the grounding trace below.")
    grounded_pid = _run_one_proposal(graph, runtime, _GROUNDED_GOAL,
                                     auto_promote=False)

    # ---------- Beat 2: Fallback proposal (contrast) ----------
    _banner(f"USER: {_FALLBACK_GOAL}  (fallback proposal, for contrast)")
    print("This goal's tokens overlap NOTHING the extractor pulled. "
          "Expect every cited node to be scaffold / seed / authored — "
          "and when no discovered primitive matches, the rationale "
          "says so explicitly with a [FALLBACK] tag.")
    fallback_pid = _run_one_proposal(graph, runtime, _FALLBACK_GOAL,
                                     auto_promote=True)

    _banner("Done")
    print(f"  grounded proposal: {grounded_pid} "
          f"(used_fallback_scaffold={graph.get_object(grounded_pid).data['used_fallback_scaffold']})")
    print(f"  fallback proposal: {fallback_pid} "
          f"(used_fallback_scaffold={graph.get_object(fallback_pid).data['used_fallback_scaffold']})")
    print(f"\n[graph] now {len(graph.all_objects())} objects, "
          f"{len(graph.all_relations())} relations, "
          f"{len(graph.events)} events")
    print(f"Full event log persisted at sqlite:///{_DEMO_DB_PATH}")


# ---------- per-proposal flow ----------


def _run_one_proposal(graph, runtime, goal: str, *, auto_promote: bool) -> str:
    pid = propose_patch_for(graph, goal)
    proposal = graph.get_object(pid)
    print("\n[proposal rationale]")
    print(" ", proposal.data["rationale"])
    print("\n[proposal changes]")
    for c in proposal.data["changes"]:
        print(f"  - {c['kind']:18}  {_change_label(c)}")

    print("\n[grounding citations]")
    print(trace_grounding(graph, pid))

    print("\n[guardrails — primary: structural; secondary: token scan]")
    print("  primary:   v1 patches can ONLY use add_object / add_relation "
          "/ add_policy / add_state_bucket / add_task / add_evaluation "
          "/ bind_behavior(existing).")
    print("  secondary: substring banlist on payload (demo-grade; evadable).")
    report = validate_proposal(graph, pid)
    print(f"  → ok={report['ok']}  checked={report['checked']}  "
          f"violations={len(report['violations'])}")
    for v in report["violations"]:
        print(f"    ! {v}")
    if not report["ok"]:
        print("  Proposal rejected; skipping sandbox + promote.")
        return pid

    print("\n[sandbox: fork → apply → diff]")
    sandbox = sandbox_apply(graph, pid, runtime=runtime, promote=False)
    print(f"  fork={sandbox['fork_label']}")
    print(f"  +{len(sandbox['diff']['added_objects'])} objects, "
          f"+{len(sandbox['diff']['added_relations'])} relations in fork")
    for o in sandbox["diff"]["added_objects"][:8]:
        print(f"    + {o['type']:18}  {o['label']}")
    for r in sandbox["diff"]["added_relations"][:6]:
        print(f"    + {r['type']:20}  {r['source']} → {r['target']}")

    if auto_promote:
        print("\n[promote] auto-approving in demo (real session would gate "
              "here on user approval)")
        sandbox_apply(graph, pid, runtime=runtime, promote=True)
    else:
        print("\n[promote] NOT promoting this proposal in the demo "
              "(left at status='validated' so the contrast beat below "
              "sees an unchanged main graph)")
    return pid


# ---------- helpers ----------


def _print_extracted_summary(graph) -> None:
    """Print a quick top-of-graph summary so the reader can see WHICH
    real activegraph runtime artifacts were extracted — proves the
    runtime really was read before the proposal citations point at
    those same files."""
    behaviors = graph.objects(type="Behavior")
    object_types = graph.objects(type="ObjectType")
    apis = graph.objects(type="API")
    if behaviors:
        print("\n[extracted Behaviors — real @behavior / @llm_behavior decorators]")
        for b in behaviors[:8]:
            src = b.data.get("source_file_path") or "?"
            print(f"  - {b.data.get('name'):28}  on={b.data.get('on')}")
            print(f"      from {src}")
    if object_types:
        print("\n[extracted ObjectType names — what the extractor's regex caught]")
        for o in object_types[:8]:
            src = o.data.get("source_file_path") or "?"
            print(f"  - {o.data.get('name'):28}  from {src}")
    if apis:
        print(f"\n[extracted APIs: {len(apis)} signatures across "
              f"activegraph submodules]")


def _change_label(c: dict) -> str:
    if c["kind"] in ("add_object", "add_state_bucket", "add_task",
                     "add_evaluation"):
        d = c.get("data", {})
        label = (d.get("name", "") or d.get("goal", "")
                 or d.get("criterion", "")[:60])
        return f"{c.get('type','?'):14}  {label}"
    if c["kind"] == "add_relation":
        return (f"{c.get('rel_type'):20}  "
                f"{c.get('from_type')}:{c.get('from_name')} → "
                f"{c.get('to_type')}:{c.get('to_name')}")
    if c["kind"] == "add_policy":
        scope = c.get('policy', {}).get('scope', '?')
        return f"Policy           scope={scope}"
    if c["kind"] == "bind_behavior":
        return (f"{c.get('behavior'):20}  on={c.get('on_event_type')}  "
                f"scope={c.get('scope_object_type')}")
    return str(c)


if __name__ == "__main__":
    run()
