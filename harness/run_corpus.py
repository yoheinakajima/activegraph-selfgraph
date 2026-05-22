"""Run the selfgraph measurement corpus.

Pipeline:

  1. Ingest a fixed corpus (selfgraph repo + activegraph package) into
     a SQLite-backed graph + runtime so every sandbox fork takes the
     real Runtime.fork path.
  2. Extract capabilities deterministically (no LLM augmentation, no
     network).
  3. Generate the goal set MECHANICALLY from extracted node names
     (every Capability, every ObjectType) × a fixed list of templates.
     The list is sorted by extracted-name then template — the same
     graph produces the same goal sequence on every machine.
  4. For each goal, run propose → validate → sandbox(promote=False)
     and emit one JSONL row capturing:
       - goal text and PROVENANCE (which extracted node it came from,
         and whether that node's source_file_path is a runtime path
         or a selfgraph-repo path)
       - used_fallback_scaffold (bool, read off the proposal)
       - per-change origin counts (the SAME taxonomy classify_change
         uses for the citation reader)
       - PATCH_MODIFIES details: target node id + source-path class
       - guardrail report: ok, n_violations, violation kinds
       - sandbox: fork_label, fork_path (sqlite|in-memory),
         n_added_objects, n_added_relations, live_graph_unchanged
  5. ASSERT every sandbox row has fork_path == "sqlite". If any row
     falls back to in-memory replay, the script exits non-zero with
     a list of offending goals.

Outputs are written to ``harness/results/corpus.jsonl`` and a
companion ``harness/results/run.meta.json`` with the corpus shape
and a content hash so a re-run is bit-comparable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import activegraph
from activegraph import Graph, IDGen, Runtime

from selfgraph.extract import extract_capabilities
from selfgraph.guardrails import validate_proposal
from selfgraph.ingest import ingest_module_docs, ingest_paths
from selfgraph.propose import propose_patch_for
from selfgraph.query import classify_change
from selfgraph.sandbox import sandbox_apply


_RESULTS_DIR = Path("harness/results")
_DEFAULTjsonl_path = _RESULTS_DIR / "corpus.jsonl"
_DEFAULTmeta_path = _RESULTS_DIR / "run.meta.json"
_DB_DIR = Path(".selfgraph-harness")
_DB_PATH = _DB_DIR / "graph.db"

# Goal templates — fixed, mechanical. Three per node; no per-node
# curation. If a template+name produces a nonsensical English
# sentence, that's the point — the harness is not curated.
_TEMPLATES = ("monitor {name}", "track {name}", "configure {name}")

# A node is "runtime"-derived if its source_file_path lives inside
# the installed activegraph package (or is a module:// pseudo-path
# pointing at activegraph). Everything else — selfgraph repo files,
# seed nodes with no source_file_path — counts as "selfgraph".
_ACTIVEGRAPH_PKG_ROOT = os.path.dirname(activegraph.__file__)


def path_class(source_file_path: str | None) -> str:
    if not source_file_path:
        return "selfgraph"  # seed / authored, no extraction citation
    if source_file_path.startswith(_ACTIVEGRAPH_PKG_ROOT):
        return "runtime"
    if source_file_path.startswith("module://activegraph"):
        return "runtime"
    return "selfgraph"


# ---------- corpus setup ----------


def build_graph() -> tuple[Graph, Runtime]:
    """Fresh SQLite-backed graph + runtime, ingest+extract once."""
    if _DB_DIR.exists():
        shutil.rmtree(_DB_DIR)
    _DB_DIR.mkdir(parents=True)
    graph = Graph(ids=IDGen(), run_id="selfgraph-harness")
    runtime = Runtime(graph, persist_to=str(_DB_PATH))

    print("[harness] ingesting selfgraph repo + activegraph runtime")
    ingest_paths(graph, ["selfgraph", "README.md", "demo.py"])
    ingest_module_docs(graph, "activegraph", max_submodules=40)
    ingest_paths(graph, [os.path.join(_ACTIVEGRAPH_PKG_ROOT, "packs")],
                 max_bytes=400_000)

    print("[harness] extracting capability graph (deterministic only)")
    extract_capabilities(graph, use_llm=False)
    print(f"[harness] {len(graph.all_objects())} objects, "
          f"{len(graph.all_relations())} relations after extract")
    return graph, runtime


# ---------- goal set ----------


def generate_goal_set(graph: Graph) -> list[dict[str, Any]]:
    """Mechanical goal set: every Capability and ObjectType × templates.

    Sorted by (node type, node name, template index) so the sequence
    is reproducible across runs on the same graph state.
    """
    rows: list[dict[str, Any]] = []
    candidates: list[tuple[str, Any]] = []  # (sort_key, obj)
    for ot in graph.objects(type="Capability"):
        name = ot.data.get("name", "")
        candidates.append((("Capability", name), ot))
    for ot in graph.objects(type="ObjectType"):
        name = ot.data.get("name", "")
        candidates.append((("ObjectType", name), ot))
    candidates.sort(key=lambda kv: kv[0])

    for (kind, name), node in candidates:
        src = node.data.get("source_file_path") or node.data.get("source_file")
        for tmpl in _TEMPLATES:
            rows.append({
                "goal": tmpl.format(name=name),
                "derived_from_node_id": node.id,
                "derived_from_node_type": kind,
                "derived_from_node_name": name,
                "derived_from_source": src,
                "derived_from_path_class": path_class(src),
            })
    return rows


# ---------- per-goal instrumentation ----------


def run_goal(graph: Graph, runtime: Runtime, goal_row: dict[str, Any]
             ) -> dict[str, Any]:
    """Run propose → validate → sandbox(promote=False) for one goal.

    Returns the JSONL row to emit. Does NOT promote — the live graph
    keeps growing only by the propose-emitted PatchProposal Object and
    the validate-emitted patch on it, never by sandbox application.
    """
    out: dict[str, Any] = dict(goal_row)

    # propose + validate happen on the LIVE graph (they emit events).
    # That's expected: a PatchProposal is itself an Object in the log.
    pid = propose_patch_for(graph, goal_row["goal"])
    proposal = graph.get_object(pid)
    out["proposal_id"] = pid
    out["used_fallback_scaffold"] = bool(
        proposal.data.get("used_fallback_scaffold")
    )

    changes = proposal.data.get("changes", [])
    out["n_changes"] = len(changes)

    # Origin mix — reuse the citation taxonomy verbatim.
    origin_counts = {
        "grounded-in-extracted": 0,
        "built-in-scaffold": 0,
        "self-authored": 0,
        "domain-new": 0,
    }
    per_change: list[dict[str, Any]] = []
    for i, c in enumerate(changes):
        cat = classify_change(graph, c)
        origin_counts[cat] = origin_counts.get(cat, 0) + 1
        entry: dict[str, Any] = {"i": i, "kind": c.get("kind"),
                                 "category": cat}
        if c.get("kind") == "bind_behavior":
            beh_name = c.get("behavior")
            entry["behavior"] = beh_name
            entry["on_event_type"] = c.get("on_event_type")
            entry["scope_object_type"] = c.get("scope_object_type")
            beh_src: str | None = None
            for b in graph.objects(type="Behavior"):
                if b.data.get("name") == beh_name:
                    beh_src = (b.data.get("source_file_path")
                               or b.data.get("source_file"))
                    break
            entry["behavior_source_file"] = beh_src
        per_change.append(entry)
    out["origin_counts"] = origin_counts
    out["per_change"] = per_change

    # PATCH_MODIFIES edge targets — what extracted ObjectTypes does the
    # proposal cite, and which path class do they belong to?
    modifies = [r for r in graph.relations(source=pid)
                if r.type == "PATCH_MODIFIES"]
    patch_modifies: list[dict[str, Any]] = []
    for r in modifies:
        tgt = graph.get_object(r.target)
        if tgt is None:
            continue
        src = tgt.data.get("source_file_path") or tgt.data.get("source_file")
        patch_modifies.append({
            "target_id": tgt.id,
            "target_name": tgt.data.get("name"),
            "target_source": src,
            "target_path_class": path_class(src),
        })
    out["patch_modifies"] = patch_modifies
    out["n_patch_modifies"] = len(patch_modifies)

    # Guardrail.
    g_report = validate_proposal(graph, pid)
    violation_kinds = sorted({v[0] for v in g_report.get("violations", [])})
    out["guardrail"] = {
        "ok": bool(g_report["ok"]),
        "n_violations": len(g_report.get("violations", [])),
        "violation_kinds": violation_kinds,
    }

    # Sandbox — bracket counts around the call only; propose+validate
    # already mutated the live graph by design (the proposal IS an
    # Object in the log), so the unchanged-by-sandbox guarantee is the
    # strict claim we measure.
    live_objs_before = len(graph.all_objects())
    live_rels_before = len(graph.all_relations())
    live_events_before = len(graph.events)
    sandbox = sandbox_apply(graph, pid, runtime=runtime, promote=False)
    live_objs_after = len(graph.all_objects())
    live_rels_after = len(graph.all_relations())
    live_events_after = len(graph.events)
    fork_label = sandbox.get("fork_label", "")
    fork_path = ("sqlite" if fork_label.startswith("sqlite-fork@")
                 else "in-memory")
    out["sandbox"] = {
        "fork_label": fork_label,
        "fork_path": fork_path,
        "n_added_objects": len(sandbox.get("diff", {}).get("added_objects", [])),
        "n_added_relations": len(sandbox.get("diff", {}).get("added_relations", [])),
        "live_objs_before": live_objs_before,
        "live_objs_after": live_objs_after,
        "live_rels_before": live_rels_before,
        "live_rels_after": live_rels_after,
        "live_events_before": live_events_before,
        "live_events_after": live_events_after,
        "live_graph_unchanged": (
            live_objs_before == live_objs_after
            and live_rels_before == live_rels_after
            and live_events_before == live_events_after
        ),
    }
    return out


# ---------- entry point ----------


def main(argv: list[str] | None = None) -> int:
    from harness.invariants import require_no_llm_env
    require_no_llm_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Optional positional arg: alternate jsonl output path (e.g. for
    # the extractor-relaxation A/B). The meta file follows the same
    # stem.
    if argv:
        jsonl_path = Path(argv[0])
        meta_path = jsonl_path.with_suffix(".meta.json")
    else:
        jsonl_path = _DEFAULTjsonl_path
        meta_path = _DEFAULTmeta_path
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    graph, runtime = build_graph()
    goal_set = generate_goal_set(graph)
    runtime_count = sum(1 for g in goal_set
                        if g["derived_from_path_class"] == "runtime")
    selfgraph_count = sum(1 for g in goal_set
                          if g["derived_from_path_class"] == "selfgraph")
    print(f"[harness] generated {len(goal_set)} goals "
          f"(runtime-derived={runtime_count}, "
          f"selfgraph-derived={selfgraph_count})")

    # Stream results to JSONL.
    rows: list[dict[str, Any]] = []
    fork_violations: list[dict[str, Any]] = []
    isolation_violations: list[dict[str, Any]] = []
    with jsonl_path.open("w") as f:
        for i, gr in enumerate(goal_set):
            print(f"[harness] {i + 1}/{len(goal_set)}  {gr['goal']!r}")
            row = run_goal(graph, runtime, gr)
            rows.append(row)
            f.write(json.dumps(row) + "\n")
            f.flush()
            if row["sandbox"]["fork_path"] != "sqlite":
                fork_violations.append({
                    "goal": gr["goal"],
                    "fork_label": row["sandbox"]["fork_label"],
                })
            if not row["sandbox"]["live_graph_unchanged"]:
                isolation_violations.append({
                    "goal": gr["goal"],
                    "before": (row["sandbox"]["live_objs_before"],
                               row["sandbox"]["live_rels_before"],
                               row["sandbox"]["live_events_before"]),
                    "after": (row["sandbox"]["live_objs_after"],
                              row["sandbox"]["live_rels_after"],
                              row["sandbox"]["live_events_after"]),
                })

    # Meta + content hash.
    jsonl_hash = hashlib.sha256(jsonl_path.read_bytes()).hexdigest()[:16]
    meta = {
        "n_goals": len(goal_set),
        "runtime_derived": runtime_count,
        "selfgraph_derived": selfgraph_count,
        "templates": list(_TEMPLATES),
        "db_path": str(_DB_PATH),
        "jsonl_path": str(jsonl_path),
        "jsonl_sha256_16": jsonl_hash,
        "fork_violations": fork_violations,
        "isolation_violations": isolation_violations,
        # Audit invariant for the paper: this run was on the
        # deterministic floor (no LLM augmentation). False here means
        # the canonical shas apply.
        "llm_augment_active": bool(os.environ.get("ANTHROPIC_API_KEY")),
        # Which ObjectType match mode was active for this run. Empty
        # string when the default is in force (== 'relaxed'); the env
        # var is the canonical source of truth.
        "objecttype_match_mode":
            os.environ.get("SELFGRAPH_OBJECTTYPE_MATCH", "relaxed"),
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    if fork_violations:
        print("\n[harness] FAIL — SQLite-fork-for-every-row requirement "
              "violated", file=sys.stderr)
        for v in fork_violations:
            print(f"  {v['goal']!r}  fell back to "
                  f"{v['fork_label']!r}", file=sys.stderr)
        return 2
    if isolation_violations:
        print("\n[harness] FAIL — sandbox isolation violated (live "
              "graph changed during a promote=False sandbox)",
              file=sys.stderr)
        for v in isolation_violations:
            print(f"  {v['goal']!r}  before={v['before']} "
                  f"after={v['after']}", file=sys.stderr)
        return 3

    print(f"\n[harness] wrote {len(rows)} rows → {jsonl_path}")
    print(f"[harness] meta → {meta_path}  (sha256[:16]={jsonl_hash})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
