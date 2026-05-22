"""Side-by-side report for the extractor-relaxation A/B.

Reads two corpus JSONL files (BEFORE and AFTER) plus their meta
files, and prints a flat table that compares: extractable ObjectType
count by path class, goal count by path class, grounding rate
overall + split, origin mix, sandbox isolation + fork-path
regression check. No prose conclusions.

Usage:  python -m harness.compare BEFORE.jsonl AFTER.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def _load(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [json.loads(l) for l in path.read_text().splitlines() if l]
    # Meta may be named ``<stem>.meta.json`` (the parameterized
    # convention) or ``run.meta.json`` (the older default used by
    # the BEFORE baseline). Try both.
    candidates = [
        path.with_suffix(".meta.json"),
        path.parent / "run.meta.json",
    ]
    meta: dict[str, Any] = {}
    for c in candidates:
        if c.exists():
            meta = json.loads(c.read_text())
            break
    return rows, meta


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    by_class = Counter(r["derived_from_path_class"] for r in rows)
    grounded_overall = sum(1 for r in rows if r["n_patch_modifies"] > 0)
    fb_overall = sum(1 for r in rows if r["used_fallback_scaffold"])
    origin_total: Counter = Counter()
    for r in rows:
        for k, v in r["origin_counts"].items():
            origin_total[k] += v
    total_changes = sum(origin_total.values())
    by_class_grounded = {}
    by_class_fb = {}
    for cls in ("runtime", "selfgraph"):
        subset = [r for r in rows if r["derived_from_path_class"] == cls]
        by_class_grounded[cls] = (
            sum(1 for r in subset if r["n_patch_modifies"] > 0),
            len(subset),
        )
        by_class_fb[cls] = (
            sum(1 for r in subset if r["used_fallback_scaffold"]),
            len(subset),
        )
    n_sqlite = sum(1 for r in rows
                   if r["sandbox"]["fork_path"] == "sqlite")
    n_isolated = sum(1 for r in rows
                     if r["sandbox"]["live_graph_unchanged"])
    return {
        "n_goals": n,
        "by_class": dict(by_class),
        "grounded_overall": (grounded_overall, n),
        "by_class_grounded": by_class_grounded,
        "fb_overall": (fb_overall, n),
        "by_class_fb": by_class_fb,
        "origin_total": dict(origin_total),
        "total_changes": total_changes,
        "sqlite_pct": (n_sqlite, n),
        "isolated_pct": (n_isolated, n),
    }


def _frac(t: tuple[int, int]) -> str:
    n, d = t
    if d == 0:
        return "n/a (0)"
    return f"{n}/{d} ({100.0 * n / d:.1f}%)"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print("usage: python -m harness.compare BEFORE.jsonl AFTER.jsonl",
              file=sys.stderr)
        return 2
    before_path = Path(argv[0])
    after_path = Path(argv[1])
    b_rows, b_meta = _load(before_path)
    a_rows, a_meta = _load(after_path)
    b = _summary(b_rows)
    a = _summary(a_rows)

    print()
    print(f"{'metric':46} {'BEFORE':>18} {'AFTER':>18}")
    print("-" * 84)
    print(f"{'jsonl path':46} {str(before_path):>18} {str(after_path):>18}")
    print(f"{'jsonl sha256[:16]':46} "
          f"{b_meta.get('jsonl_sha256_16', '?'):>18} "
          f"{a_meta.get('jsonl_sha256_16', '?'):>18}")

    print()
    print("--- goal set --------------------------------------------------"
          "------------------")
    print(f"{'n_goals':46} {b['n_goals']:>18} {a['n_goals']:>18}")
    for cls in ("runtime", "selfgraph"):
        print(f"{'  derived_from_path_class = ' + cls:46} "
              f"{b['by_class'].get(cls, 0):>18} "
              f"{a['by_class'].get(cls, 0):>18}")

    print()
    print("--- grounding rate --------------------------------------------"
          "------------------")
    print(f"{'overall':46} {_frac(b['grounded_overall']):>18} "
          f"{_frac(a['grounded_overall']):>18}")
    for cls in ("runtime", "selfgraph"):
        print(f"{'  derived_from_path_class = ' + cls:46} "
              f"{_frac(b['by_class_grounded'][cls]):>18} "
              f"{_frac(a['by_class_grounded'][cls]):>18}")

    print()
    print("--- fallback-scaffold rate ------------------------------------"
          "------------------")
    print(f"{'overall':46} {_frac(b['fb_overall']):>18} "
          f"{_frac(a['fb_overall']):>18}")
    for cls in ("runtime", "selfgraph"):
        print(f"{'  derived_from_path_class = ' + cls:46} "
              f"{_frac(b['by_class_fb'][cls]):>18} "
              f"{_frac(a['by_class_fb'][cls]):>18}")

    print()
    print("--- origin mix (all changes, all proposals) -------------------"
          "------------------")
    for k in ("grounded-in-extracted", "built-in-scaffold",
             "self-authored", "domain-new"):
        bv = b["origin_total"].get(k, 0)
        av = a["origin_total"].get(k, 0)
        b_pct = _frac((bv, b["total_changes"]))
        a_pct = _frac((av, a["total_changes"]))
        print(f"{'  ' + k:46} {b_pct:>18} {a_pct:>18}")
    print(f"{'  TOTAL changes':46} "
          f"{b['total_changes']:>18} {a['total_changes']:>18}")

    print()
    print("--- sandbox regression check ----------------------------------"
          "------------------")
    print(f"{'fork_path == sqlite':46} {_frac(b['sqlite_pct']):>18} "
          f"{_frac(a['sqlite_pct']):>18}")
    print(f"{'live_graph_unchanged':46} {_frac(b['isolated_pct']):>18} "
          f"{_frac(a['isolated_pct']):>18}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
