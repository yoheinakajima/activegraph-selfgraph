"""Aggregate the corpus JSONL into a flat table.

Reads ``harness/results/corpus.jsonl`` (produced by run_corpus.py)
and prints a tab-aligned report. No prose conclusions.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_JSONL_PATH = Path("harness/results/corpus.jsonl")
_META_PATH = Path("harness/results/run.meta.json")


def _load() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not _JSONL_PATH.exists():
        print(f"[report] missing {_JSONL_PATH}; run "
              f"`python -m harness.run_corpus` first",
              file=sys.stderr)
        sys.exit(2)
    rows = [json.loads(l) for l in _JSONL_PATH.read_text().splitlines() if l]
    meta = (json.loads(_META_PATH.read_text())
            if _META_PATH.exists() else {})
    return rows, meta


def _pct(num: int, denom: int) -> str:
    if denom == 0:
        return "n/a (0 denom)"
    return f"{num}/{denom} ({100.0 * num / denom:.1f}%)"


def _section(title: str) -> None:
    print()
    print("-" * 72)
    print(title)
    print("-" * 72)


def main() -> int:
    rows, meta = _load()
    n = len(rows)

    _section("corpus shape")
    print(f"  n_goals                       {n}")
    by_class = Counter(r["derived_from_path_class"] for r in rows)
    print(f"  runtime-derived               {by_class.get('runtime', 0)}")
    print(f"  selfgraph-derived             {by_class.get('selfgraph', 0)}")
    if meta:
        print(f"  templates                     {meta.get('templates')}")
        print(f"  jsonl sha256[:16]             {meta.get('jsonl_sha256_16')}")

    _section("grounding rate (any GROUNDED_IN / PATCH_MODIFIES wired)")
    grounded_overall = sum(1 for r in rows if r["n_patch_modifies"] > 0)
    print(f"  overall                       {_pct(grounded_overall, n)}")
    for cls in ("runtime", "selfgraph"):
        subset = [r for r in rows if r["derived_from_path_class"] == cls]
        grounded = sum(1 for r in subset if r["n_patch_modifies"] > 0)
        print(f"  derived_from_path={cls:9}  {_pct(grounded, len(subset))}")

    _section("fallback-scaffold rate (used_fallback_scaffold == True)")
    fb_overall = sum(1 for r in rows if r["used_fallback_scaffold"])
    print(f"  overall                       {_pct(fb_overall, n)}")
    for cls in ("runtime", "selfgraph"):
        subset = [r for r in rows if r["derived_from_path_class"] == cls]
        fb = sum(1 for r in subset if r["used_fallback_scaffold"])
        print(f"  derived_from_path={cls:9}  {_pct(fb, len(subset))}")

    _section("origin mix across ALL changes in ALL proposals")
    origin_total: Counter = Counter()
    for r in rows:
        for k, v in r["origin_counts"].items():
            origin_total[k] += v
    total_changes = sum(origin_total.values())
    for k in ("grounded-in-extracted", "built-in-scaffold",
             "self-authored", "domain-new"):
        v = origin_total.get(k, 0)
        print(f"  {k:24}      {_pct(v, total_changes)}")
    print(f"  TOTAL changes                 {total_changes}")

    _section("guardrail outcomes")
    g_ok = sum(1 for r in rows if r["guardrail"]["ok"])
    g_rej = n - g_ok
    print(f"  validated                     {_pct(g_ok, n)}")
    print(f"  rejected                      {_pct(g_rej, n)}")
    vk_counter: Counter = Counter()
    for r in rows:
        for vk in r["guardrail"]["violation_kinds"]:
            vk_counter[vk] += 1
    if vk_counter:
        print("  rejection kinds (rows mentioning each kind ≥1×):")
        for kind, c in vk_counter.most_common():
            print(f"    {kind:24}    {c}")
    else:
        print("  rejection kinds                (none)")

    _section("sandbox isolation + fork-path")
    n_forks = sum(1 for r in rows if r.get("sandbox"))
    n_sqlite = sum(1 for r in rows
                   if r["sandbox"]["fork_path"] == "sqlite")
    n_mem = sum(1 for r in rows
                if r["sandbox"]["fork_path"] == "in-memory")
    n_isolated = sum(1 for r in rows
                     if r["sandbox"]["live_graph_unchanged"])
    print(f"  n_forks                       {n_forks}")
    print(f"  fork_path == sqlite           {_pct(n_sqlite, n_forks)}    "
          f"(target: 100%)")
    print(f"  fork_path == in-memory        {_pct(n_mem, n_forks)}")
    print(f"  live_graph_unchanged          {_pct(n_isolated, n_forks)}    "
          f"(target: 100%)")
    print(f"  total objects added in forks  "
          f"{sum(r['sandbox']['n_added_objects'] for r in rows)}")
    print(f"  total relations added in forks "
          f"{sum(r['sandbox']['n_added_relations'] for r in rows)}")

    if meta.get("fork_violations") or meta.get("isolation_violations"):
        _section("RUN HAD ASSERTION FAILURES — see run.meta.json")
        for v in meta.get("fork_violations", []):
            print(f"  FORK    {v}")
        for v in meta.get("isolation_violations", []):
            print(f"  ISOLATE {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
