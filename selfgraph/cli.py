"""selfgraph CLI.

Usage:
    python -m selfgraph build [repo_path]
    python -m selfgraph ask "what can you do?"
    python -m selfgraph propose "track project updates"
    python -m selfgraph chat            # interactive REPL
    python -m selfgraph demo            # scripted demo (same as demo.py)

State persists to ./.selfgraph/graph.db (SQLite event store) so
`build` and `ask` can be run as separate processes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from activegraph import Graph, IDGen, Runtime

from selfgraph.extract import extract_capabilities
from selfgraph.guardrails import validate_proposal
from selfgraph.ingest import ingest_module_docs, ingest_paths
from selfgraph.propose import propose_patch_for
from selfgraph.query import answer_question, repl, summarize_capabilities
from selfgraph.sandbox import sandbox_apply


_DB_DIR = ".selfgraph"
_DB_PATH = f"{_DB_DIR}/graph.db"
_RUN_ID = "selfgraph"


def _open(create: bool = False) -> tuple[Graph, Runtime]:
    Path(_DB_DIR).mkdir(exist_ok=True)
    if create and Path(_DB_PATH).exists():
        os.remove(_DB_PATH)
    if create or not Path(_DB_PATH).exists():
        graph = Graph(ids=IDGen(), run_id=_RUN_ID)
        rt = Runtime(graph, persist_to=_DB_PATH)
        return graph, rt
    # Reuse existing log: load() rebuilds the graph from the event store.
    rt = Runtime.load(_DB_PATH, run_id=_RUN_ID)
    return rt.graph, rt


def cmd_build(args: list[str]) -> int:
    repo = args[0] if args else "."
    print(f"[build] ingesting repo at {repo} and the activegraph module")
    graph, _rt = _open(create=True)
    ingest_paths(graph, [repo])
    ingest_module_docs(graph, "activegraph", max_submodules=40)
    extract_capabilities(graph)
    print()
    print(summarize_capabilities(graph))
    return 0


def cmd_ask(args: list[str]) -> int:
    question = " ".join(args) or "what can you do?"
    graph, _rt = _open()
    print(answer_question(graph, question))
    return 0


def cmd_propose(args: list[str]) -> int:
    goal = " ".join(args) or "track project updates using whatever pattern makes sense"
    graph, rt = _open()
    pid = propose_patch_for(graph, goal)
    report = validate_proposal(graph, pid)
    print(f"\n[propose] validation: ok={report['ok']} "
          f"violations={report['violations']}")
    if report["ok"]:
        sandbox = sandbox_apply(graph, pid, runtime=rt, promote=False)
        print(f"[propose] sandbox diff summary: "
              f"+{len(sandbox['diff']['added_objects'])} objects, "
              f"+{len(sandbox['diff']['added_relations'])} relations")
        print(f"[propose] to promote: python -m selfgraph promote {pid}")
    return 0 if report["ok"] else 1


def cmd_promote(args: list[str]) -> int:
    if not args:
        print("usage: python -m selfgraph promote <proposal_id>")
        return 2
    pid = args[0]
    graph, rt = _open()
    # Re-validate against the current persisted state — the graph may
    # have changed between propose and promote (new ingestions, other
    # patches), so a stale 'validated' marker is not enough.
    # mutate_status=False so a re-check doesn't overwrite the existing
    # lifecycle status on the proposal.
    report = validate_proposal(graph, pid, mutate_status=False)
    if not report["ok"]:
        print(f"[promote] revalidation failed: {report['violations']}")
        return 1
    sandbox_report = sandbox_apply(graph, pid, runtime=rt, promote=True)
    print(f"[promote] done. fork={sandbox_report['fork_label']} "
          f"changes={sandbox_report['applied_changes']}")
    return 0


def cmd_chat(args: list[str]) -> int:
    graph, _rt = _open()
    repl(graph)
    return 0


def cmd_demo(args: list[str]) -> int:
    # demo.py is gated on __name__ == "__main__"; the import just gets
    # us a handle to demo.run() so the scripted flow lives in one file.
    import demo
    demo.run()
    return 0


_COMMANDS = {
    "build":   cmd_build,
    "ask":     cmd_ask,
    "propose": cmd_propose,
    "promote": cmd_promote,
    "chat":    cmd_chat,
    "demo":    cmd_demo,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    fn = _COMMANDS.get(cmd)
    if not fn:
        print(f"unknown command: {cmd}")
        print(__doc__)
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
