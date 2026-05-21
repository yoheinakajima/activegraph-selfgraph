"""Scripted demo. Run:  python demo.py

Walks the user through:

  1. "Read this repo and build your capability graph."
  2. "What can you do?"
  3. "Configure yourself to track project updates."

Step 3 is intentionally vague. The agent composes proposals from
discovered ActiveGraph primitives — the Behaviors, EventTypes,
ObjectTypes, and AuthorityRules the extractor put in the graph.
If a primitive isn't in the graph it doesn't appear in the proposal.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from activegraph import Graph, IDGen, Runtime

from selfgraph.extract import extract_capabilities
from selfgraph.guardrails import validate_proposal
from selfgraph.ingest import ingest_module_docs, ingest_paths
from selfgraph.propose import propose_patch_for
from selfgraph.query import answer_question, summarize_capabilities
from selfgraph.sandbox import sandbox_apply


_DEMO_DB_DIR = ".selfgraph-demo"
_DEMO_DB_PATH = f"{_DEMO_DB_DIR}/graph.db"


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
    extract_capabilities(graph)
    print(f"\n[graph] {len(graph.all_objects())} objects, "
          f"{len(graph.all_relations())} relations, "
          f"{len(graph.events)} events")

    _banner("USER: What can you do?")
    print(answer_question(graph, "what can you do?"))

    _banner("USER: How would you implement forking?")
    print(answer_question(graph, "how would you implement forking?"))

    _banner("USER: Configure yourself to track project updates.")
    goal = "track project updates"
    pid = propose_patch_for(graph, goal)
    proposal = graph.get_object(pid)
    print("\n[proposal rationale]")
    print(" ", proposal.data["rationale"])
    print("\n[proposal changes]")
    for c in proposal.data["changes"]:
        print(f"  - {c['kind']:18}  {_change_label(c)}")
    print("\n[proposal evaluation]")
    for e in proposal.data["evaluation"]:
        print(f"  - {e}")

    _banner("Guardrails")
    report = validate_proposal(graph, pid)
    print(f"  ok={report['ok']}  checked={report['checked']}  "
          f"violations={len(report['violations'])}")
    for v in report["violations"]:
        print(f"    ! {v}")

    if not report["ok"]:
        print("Proposal rejected; demo stops here.")
        return

    _banner("Sandbox apply (fork → test event → diff)")
    sandbox = sandbox_apply(graph, pid, runtime=runtime, promote=False)
    print(f"  fork={sandbox['fork_label']}")
    print(f"  added_objects   ({len(sandbox['diff']['added_objects'])})")
    for o in sandbox["diff"]["added_objects"][:10]:
        print(f"    + {o['type']:14}  {o['label']}")
    print(f"  added_relations ({len(sandbox['diff']['added_relations'])})")
    for r in sandbox["diff"]["added_relations"][:10]:
        print(f"    + {r['type']:20}  {r['source']} → {r['target']}")

    _banner("Promote? (auto-approving in demo)")
    print("In a real session selfgraph would block here for user approval.")
    sandbox_apply(graph, pid, runtime=runtime, promote=True)
    print(f"\n[graph] now {len(graph.all_objects())} objects, "
          f"{len(graph.all_relations())} relations, "
          f"{len(graph.events)} events")

    _banner("Done. Trace prefix (first 8 lines):")
    for line in runtime.trace.lines()[:8]:
        # Trace lines include event payload previews; trim hard so the
        # demo's tail stays readable.
        if len(line) > 140:
            line = line[:140] + " ..."
        print(" ", line)
    print(f"\nFull event log persisted at sqlite:///{_DEMO_DB_PATH}")


def _change_label(c: dict) -> str:
    if c["kind"] in ("add_object", "add_state_bucket", "add_task",
                     "add_evaluation"):
        return f"{c.get('type','?'):14}  {c.get('data', {}).get('name', '') or c.get('data', {}).get('goal', '') or c.get('data', {}).get('criterion', '')[:60]}"
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
