"""Harness smoke tests.

These tests don't run the full corpus (slow); they exercise the
deterministic building blocks:

- path_class classifies activegraph package paths as 'runtime',
  selfgraph repo paths as 'selfgraph', and None / seed as 'selfgraph'.
- generate_goal_set is deterministic: same graph state → same goal
  sequence (sorted by (node-type, name, template)).
- classify_change matches the prose taxonomy of the citation reader
  for the four canonical change shapes.
- run_goal emits a row with the expected keys and a
  live_graph_unchanged==True invariant around a sandbox call.
"""

from __future__ import annotations

import os

import activegraph
from activegraph import Graph, IDGen, Runtime

from harness.run_corpus import (
    generate_goal_set,
    path_class,
    run_goal,
)
from selfgraph.extract import extract_capabilities
from selfgraph.ingest import ingest_paths
from selfgraph.query import classify_change


def test_path_class_runtime_vs_selfgraph():
    pkg = os.path.dirname(activegraph.__file__)
    assert path_class(os.path.join(pkg, "core/graph.py")) == "runtime"
    assert path_class(os.path.join(pkg, "packs/diligence/behaviors.py")) == "runtime"
    assert path_class("module://activegraph.core") == "runtime"
    assert path_class("selfgraph/extract.py") == "selfgraph"
    assert path_class("README.md") == "selfgraph"
    assert path_class(None) == "selfgraph"  # seed nodes


def _small_graph():
    g = Graph(ids=IDGen(), run_id="test")
    ingest_paths(g, ["selfgraph/__init__.py", "selfgraph/ingest.py"])
    extract_capabilities(g, use_llm=False)
    return g


def test_generate_goal_set_is_deterministic_and_sorted():
    g1 = _small_graph()
    g2 = _small_graph()
    goals1 = generate_goal_set(g1)
    goals2 = generate_goal_set(g2)
    # Determinism: same shape across runs.
    assert [g["goal"] for g in goals1] == [g["goal"] for g in goals2]
    # Sorted by (node-type, name, template).
    keys = [(g["derived_from_node_type"], g["derived_from_node_name"])
            for g in goals1]
    assert keys == sorted(keys)


def test_generate_goal_set_records_provenance():
    g = _small_graph()
    goals = generate_goal_set(g)
    assert goals, "expected at least one goal from the small corpus"
    for row in goals:
        assert row["derived_from_node_id"]
        assert row["derived_from_node_type"] in ("Capability", "ObjectType")
        assert row["derived_from_path_class"] in ("runtime", "selfgraph")


def test_classify_change_matches_citation_taxonomy():
    g = _small_graph()
    # Find a real extracted ObjectType (selfgraph-source) to use as a
    # GROUNDED_IN target so the relation-classification path fires.
    extracted = g.objects(type="ObjectType")
    assert extracted, "test setup needs at least one extracted ObjectType"
    target_name = extracted[0].data["name"]

    cases = {
        "grounded-in-extracted": {
            "kind": "add_relation",
            "rel_type": "GROUNDED_IN",
            "from_type": "ObjectType", "from_name": "Atom",
            "to_type": "ObjectType", "to_name": target_name,
        },
        "built-in-scaffold": {
            "kind": "add_object",
            "type": "ObjectType",
            "data": {"name": "Atom",
                     "source": "selfgraph-fallback-scaffold"},
        },
        "self-authored": {
            "kind": "add_evaluation",
            "type": "Evaluation",
            "data": {"criterion": "x"},
        },
        "domain-new": {
            "kind": "add_object",
            "type": "ObjectType",
            "data": {"name": "BrandNew"},
        },
    }
    for expected, change in cases.items():
        assert classify_change(g, change) == expected, (
            f"classify_change({change}) != {expected}"
        )


def test_objecttype_match_flag_literal_excludes_runtime_object_types(monkeypatch=None):
    """In SELFGRAPH_OBJECTTYPE_MATCH=literal mode the extractor must
    NOT pick up the lowercase ObjectType(name="...") constructor
    calls from activegraph runtime source — that's the whole BEFORE
    condition for the paper's A/B."""
    import os
    import activegraph as ag
    from selfgraph.ingest import ingest_paths
    pkg = os.path.dirname(ag.__file__)
    saved = os.environ.get("SELFGRAPH_OBJECTTYPE_MATCH")
    os.environ["SELFGRAPH_OBJECTTYPE_MATCH"] = "literal"
    try:
        g = Graph(ids=IDGen(), run_id="literal-mode")
        ingest_paths(g, [os.path.join(pkg, "packs")], max_bytes=400_000)
        extract_capabilities(g, use_llm=False)
        runtime_ots = [
            o for o in g.objects(type="ObjectType")
            if (o.data.get("source_file_path") or "").startswith(pkg)
        ]
        assert runtime_ots == [], (
            f"literal mode should not emit runtime ObjectTypes; "
            f"got {[o.data.get('name') for o in runtime_ots]}"
        )
    finally:
        if saved is None:
            os.environ.pop("SELFGRAPH_OBJECTTYPE_MATCH", None)
        else:
            os.environ["SELFGRAPH_OBJECTTYPE_MATCH"] = saved


def test_objecttype_match_flag_invalid_value_raises():
    """A typo'd flag value must fail loudly rather than silently
    falling back to a default that would shift the paper's shas."""
    import os
    from selfgraph.extract import _objecttype_regexes
    saved = os.environ.get("SELFGRAPH_OBJECTTYPE_MATCH")
    os.environ["SELFGRAPH_OBJECTTYPE_MATCH"] = "lenient"
    try:
        try:
            _objecttype_regexes()
        except ValueError as e:
            assert "lenient" in str(e)
            return
        raise AssertionError(
            "expected ValueError for unknown SELFGRAPH_OBJECTTYPE_MATCH "
            "value"
        )
    finally:
        if saved is None:
            os.environ.pop("SELFGRAPH_OBJECTTYPE_MATCH", None)
        else:
            os.environ["SELFGRAPH_OBJECTTYPE_MATCH"] = saved


def test_relaxed_extractor_catches_runtime_object_types():
    """After the ObjectType regex relaxation, extractor must emit at
    least one ObjectType node whose source_file_path lives inside the
    installed activegraph package (the activegraph runtime uses
    lowercase ``ObjectType(name="...")`` constructor calls that the
    pre-relaxation regex missed)."""
    import os
    import activegraph as ag
    from selfgraph.ingest import ingest_paths
    pkg = os.path.dirname(ag.__file__)
    g = Graph(ids=IDGen(), run_id="relaxed")
    ingest_paths(g, [os.path.join(pkg, "packs")], max_bytes=400_000)
    extract_capabilities(g, use_llm=False)
    runtime_ots = [
        o for o in g.objects(type="ObjectType")
        if (o.data.get("source_file_path") or "").startswith(pkg)
    ]
    assert runtime_ots, (
        "expected the relaxed extractor to emit at least one "
        "ObjectType from an activegraph package path; got none"
    )
    # The diligence pack contains lowercase ObjectType(name=...) calls
    # — at least 'company' should be there.
    names = {o.data.get("name") for o in runtime_ots}
    assert "company" in names, (
        f"expected 'company' among runtime-derived ObjectType names; "
        f"got {sorted(names)}"
    )


def test_run_goal_emits_expected_row_shape(tmp_path):
    """End-to-end on a single goal: row must include the spec'd keys
    and the sandbox isolation invariant must hold."""
    db = tmp_path / "t.db"
    graph = Graph(ids=IDGen(), run_id="t")
    runtime = Runtime(graph, persist_to=str(db))
    ingest_paths(graph, ["selfgraph/__init__.py"])
    extract_capabilities(graph, use_llm=False)
    goals = generate_goal_set(graph)
    assert goals
    row = run_goal(graph, runtime, goals[0])

    for key in ("goal", "derived_from_node_id", "derived_from_path_class",
                "used_fallback_scaffold", "n_changes", "origin_counts",
                "patch_modifies", "guardrail", "sandbox"):
        assert key in row, f"missing key {key}"
    assert row["sandbox"]["fork_path"] in ("sqlite", "in-memory")
    assert row["sandbox"]["live_graph_unchanged"] is True
    # Origin-count taxonomy uses the citation labels verbatim.
    assert set(row["origin_counts"]) == {
        "grounded-in-extracted", "built-in-scaffold",
        "self-authored", "domain-new",
    }


if __name__ == "__main__":
    import tempfile
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"running {name}...")
            if "tmp_path" in fn.__code__.co_varnames:
                with tempfile.TemporaryDirectory() as td:
                    from pathlib import Path
                    fn(Path(td))
            else:
                fn()
            print(f"  ok")
    print("all harness tests passed")
