"""Quantify extractor discovery recall on the activegraph runtime.

A measurement, not a change. Walks the installed activegraph package
source with `ast` to count the *actual* @behavior / @llm_behavior /
@relation_behavior decorated functions (Behavior denominator) and the
*actual* ObjectType declarations from both conventions — `add_object("X",
...)` literal-string first-positional-arg and `ObjectType(name="X", ...)`
constructor calls (ObjectType denominator). Then runs the existing
deterministic extractor under each `SELFGRAPH_OBJECTTYPE_MATCH` mode
(literal / relaxed) and reports how many of the runtime ground-truth
names show up as runtime-derived nodes in the resulting graph.

The extractor is NOT modified. The ground-truth count uses Python's
`ast` rather than the extractor's regex pass; that way the recall
denominator is independent of the thing being measured.

Output:
  - harness/results/extractor_recall.json (per-mode counts, ground
    truth lists, runtime-derived sets, missing names, sha)

Run:
  PYTHONPATH=. python -m harness.extractor_recall
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import activegraph
from activegraph import Graph, IDGen

from selfgraph.extract import extract_capabilities
from selfgraph.ingest import ingest_module_docs, ingest_paths


_RESULTS_DIR = Path("harness/results")
_OUT_PATH = _RESULTS_DIR / "extractor_recall.json"
_PKG_ROOT = os.path.dirname(activegraph.__file__)

_BEHAVIOR_DECORATORS = {"behavior", "llm_behavior", "relation_behavior"}


# ---------- ground-truth walkers ----------


def _walk_py_files(root: str):
    """Sorted recursive walk of .py files under `root`. Stable order."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def ground_truth_behaviors(pkg_root: str) -> list[dict[str, Any]]:
    """Walk `pkg_root` with ast; return one entry per function that is
    decorated with @behavior / @llm_behavior / @relation_behavior.

    A decorator counts if its callable name (top-level or attribute
    tail, with or without a call) matches one of the three. This
    matches the extractor's regex intent without piggybacking on it.

    Entries: {decorator, function_name, source_file}. Sorted by
    (source_file, function_name) for stable output.
    """
    entries: list[dict[str, Any]] = []
    for path in _walk_py_files(pkg_root):
        try:
            tree = ast.parse(open(path).read(), path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in node.decorator_list:
                name = _decorator_name(d)
                if name in _BEHAVIOR_DECORATORS:
                    entries.append({
                        "decorator": name,
                        "function_name": node.name,
                        "source_file": path[len(pkg_root) + 1:],
                    })
    entries.sort(key=lambda e: (e["source_file"], e["function_name"]))
    return entries


def ground_truth_object_types(pkg_root: str) -> dict[str, list[dict[str, Any]]]:
    """Walk `pkg_root` with ast; return ObjectType-name citations
    split by the two conventions extractor regexes are gated on:

      add_object_literal: first positional arg of `add_object(...)` is a
        string literal. (Matches the extractor's
        ``add_object\\(\\s*[\"']<NAME>``.)
      objecttype_constructor: ObjectType(name="<NAME>", ...) keyword arg
        is a string literal. (Matches the extractor's
        ``ObjectType\\(\\s*name\\s*=\\s*[\"']<NAME>``.)

    Each entry: {name, source_file}. Sorted by (source_file, name).
    The two lists may overlap in names — the activegraph diligence pack
    declares each type once with ObjectType() in object_types.py and
    then calls add_object("X", ...) inside the pack behaviors.
    """
    add_object: list[dict[str, Any]] = []
    constructor: list[dict[str, Any]] = []
    for path in _walk_py_files(pkg_root):
        try:
            tree = ast.parse(open(path).read(), path)
        except SyntaxError:
            continue
        rel = path[len(pkg_root) + 1:]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = _callable_name(node.func)
            if fname == "add_object" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) \
                        and isinstance(first.value, str):
                    add_object.append({"name": first.value, "source_file": rel})
            if fname == "ObjectType":
                for kw in node.keywords:
                    if kw.arg == "name" \
                            and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        constructor.append({
                            "name": kw.value.value, "source_file": rel,
                        })
    add_object.sort(key=lambda e: (e["source_file"], e["name"]))
    constructor.sort(key=lambda e: (e["source_file"], e["name"]))
    return {"add_object_literal": add_object,
            "objecttype_constructor": constructor}


def _decorator_name(d: ast.AST) -> str | None:
    if isinstance(d, ast.Name):
        return d.id
    if isinstance(d, ast.Attribute):
        return d.attr
    if isinstance(d, ast.Call):
        return _callable_name(d.func)
    return None


def _callable_name(f: ast.AST) -> str | None:
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


# ---------- extractor under both modes ----------


def run_extractor(mode: str) -> dict[str, set[str]]:
    """Run the standard ingest+extract pipeline under
    SELFGRAPH_OBJECTTYPE_MATCH=<mode> and return the set of runtime-
    derived Behavior and ObjectType names.

    Mirrors the run_corpus.py setup exactly so the extracted graph
    matches the corpus runs. Runs in-memory (no persisted DB), so
    repeated calls do not clobber the harness's other artifacts.
    """
    os.environ["SELFGRAPH_OBJECTTYPE_MATCH"] = mode
    g = Graph(ids=IDGen(), run_id=f"recall-{mode}")
    ingest_paths(g, ["selfgraph", "README.md", "demo.py"])
    ingest_module_docs(g, "activegraph", max_submodules=40)
    ingest_paths(g, [os.path.join(_PKG_ROOT, "packs")], max_bytes=400_000)
    extract_capabilities(g, use_llm=False)

    def runtime_derived(node_type: str) -> set[str]:
        names: set[str] = set()
        for o in g.objects(type=node_type):
            src = o.data.get("source_file_path") \
                  or o.data.get("source_file") or ""
            if src.startswith(_PKG_ROOT) or src.startswith("module://activegraph"):
                name = o.data.get("name")
                if name:
                    names.add(name)
        return names

    return {
        "Behavior": runtime_derived("Behavior"),
        "ObjectType": runtime_derived("ObjectType"),
    }


# ---------- entry point ----------


def main(argv: list[str] | None = None) -> int:
    from harness.invariants import require_no_llm_env
    require_no_llm_env()

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Ground truth
    beh_gt = ground_truth_behaviors(_PKG_ROOT)
    ot_gt = ground_truth_object_types(_PKG_ROOT)
    beh_gt_names = sorted({e["function_name"] for e in beh_gt})
    ot_gt_names = sorted({
        e["name"] for e in ot_gt["add_object_literal"] + ot_gt["objecttype_constructor"]
    })

    # Extractor under each mode
    discovered_literal = run_extractor("literal")
    discovered_relaxed = run_extractor("relaxed")

    def recall_block(node_type: str, gt: list[str], discovered: dict) -> dict:
        disc = sorted(discovered[node_type])
        gt_set = set(gt)
        disc_set = set(disc)
        true_positive = sorted(disc_set & gt_set)
        missed = sorted(gt_set - disc_set)
        false_positive = sorted(disc_set - gt_set)
        denom = len(gt_set)
        numer = len(true_positive)
        return {
            "denominator": denom,
            "numerator_true_positive": numer,
            "recall": (numer / denom) if denom else None,
            "discovered_runtime_derived": disc,
            "true_positive": true_positive,
            "missed": missed,
            "false_positive_runtime_derived": false_positive,
        }

    out: dict[str, Any] = {
        "package_root": _PKG_ROOT,
        "ground_truth_counting_method": {
            "behaviors": (
                "ast.walk over every .py in the installed activegraph "
                "package; a function counts if any of its decorators "
                "has callable-name 'behavior', 'llm_behavior', or "
                "'relation_behavior' (top-level name or attribute "
                "tail, with or without a call). Walk order is sorted "
                "directory/file."
            ),
            "object_types": (
                "ast.walk over every .py in the installed activegraph "
                "package. Two conventions counted separately and then "
                "unioned for the denominator: (1) add_object(\"<X>\", "
                "...) where the first positional arg is a string "
                "literal; (2) ObjectType(name=\"<X>\", ...) where the "
                "name kwarg is a string literal."
            ),
        },
        "ground_truth": {
            "Behavior": {
                "unique_names": beh_gt_names,
                "count": len(beh_gt_names),
                "decorations": beh_gt,
            },
            "ObjectType": {
                "unique_names": ot_gt_names,
                "count": len(ot_gt_names),
                "add_object_literal": ot_gt["add_object_literal"],
                "objecttype_constructor": ot_gt["objecttype_constructor"],
            },
        },
        "results": {
            "literal_mode": {
                "Behavior": recall_block("Behavior", beh_gt_names,
                                         discovered_literal),
                "ObjectType": recall_block("ObjectType", ot_gt_names,
                                           discovered_literal),
            },
            "relaxed_mode": {
                "Behavior": recall_block("Behavior", beh_gt_names,
                                         discovered_relaxed),
                "ObjectType": recall_block("ObjectType", ot_gt_names,
                                           discovered_relaxed),
            },
        },
        "notes": [
            "The SELFGRAPH_OBJECTTYPE_MATCH flag gates only the "
            "ObjectType regex set; Behavior recall is identical "
            "under literal and relaxed modes.",
            "Runtime-derived = the extracted node's source_file_path "
            "starts with the installed activegraph package directory "
            "OR with the 'module://activegraph' synthetic prefix used "
            "by ingest_module_docs. Same definition as the corpus "
            "harness uses for the path_class field.",
            "false_positive_runtime_derived lists extracted runtime-"
            "derived names that have no matching @behavior / "
            "ObjectType declaration in the AST walk — these are real "
            "extractor over-matches (typically a decorator string "
            "embedded in a code-template literal, e.g. scaffold.py).",
            "llm_augment_active=false: the harness invariant refuses "
            "to run with ANTHROPIC_API_KEY set. Same as every other "
            "harness result.",
        ],
        "llm_augment_active": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }

    payload = json.dumps(out, indent=2, sort_keys=True) + "\n"
    _OUT_PATH.write_text(payload)
    sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    print(f"[recall] wrote {_OUT_PATH}  sha256[:16]={sha}")

    print()
    print("Behavior recall (denominator counted by ast walk):")
    print(f"  literal: {out['results']['literal_mode']['Behavior']['numerator_true_positive']}"
          f"/{out['results']['literal_mode']['Behavior']['denominator']}  "
          f"missed={out['results']['literal_mode']['Behavior']['missed']}")
    print(f"  relaxed: {out['results']['relaxed_mode']['Behavior']['numerator_true_positive']}"
          f"/{out['results']['relaxed_mode']['Behavior']['denominator']}  "
          f"missed={out['results']['relaxed_mode']['Behavior']['missed']}")
    print()
    print("ObjectType recall:")
    print(f"  literal: {out['results']['literal_mode']['ObjectType']['numerator_true_positive']}"
          f"/{out['results']['literal_mode']['ObjectType']['denominator']}")
    print(f"  relaxed: {out['results']['relaxed_mode']['ObjectType']['numerator_true_positive']}"
          f"/{out['results']['relaxed_mode']['ObjectType']['denominator']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
