"""Break down the self-authored origin class by underlying change kind.

Confirmatory query over an existing corpus JSONL — does not re-run
the corpus. Hypothesis under test (recorded only; not concluded):
the self-authored majority is fixed by-construction bookkeeping
(the four evaluation criteria + the Task object every proposal
emits + the scoped Policy), not variable agent-generated content.

Usage:  python -m harness.query_self_authored [JSONL]   # default: corpus.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


_DEFAULT_PATH = Path("harness/results/corpus.jsonl")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = Path(argv[0]) if argv else _DEFAULT_PATH
    if not path.exists():
        print(f"[query] missing {path}", file=sys.stderr)
        return 2
    rows = [json.loads(l) for l in path.read_text().splitlines() if l]

    # Tally every "self-authored" change by its raw kind.
    by_kind: Counter = Counter()
    total_self_authored = 0
    total_changes = 0
    for r in rows:
        for c in r.get("per_change", []):
            total_changes += 1
            if c.get("category") == "self-authored":
                total_self_authored += 1
                by_kind[c.get("kind")] += 1

    print(f"source: {path}")
    print(f"  rows                          {len(rows)}")
    print(f"  total changes                 {total_changes}")
    print(f"  total self-authored changes   {total_self_authored}  "
          f"({100.0 * total_self_authored / max(total_changes, 1):.1f}% "
          f"of all changes)")
    print()
    print(f"  {'change kind':24} {'count':>8} {'pct of self-authored':>22}")
    print(f"  {'-' * 22:24} {'-' * 8:>8} {'-' * 22:>22}")
    for kind, n in by_kind.most_common():
        pct = 100.0 * n / max(total_self_authored, 1)
        print(f"  {kind:24} {n:>8} {pct:>21.1f}%")

    # Anything-else flag: any self-authored kind outside the known
    # bookkeeping set { add_task, add_evaluation, add_policy }.
    bookkeeping = {"add_task", "add_evaluation", "add_policy",
                   "add_state_bucket"}
    other = sum(n for k, n in by_kind.items() if k not in bookkeeping)
    print()
    print(f"  fixed-bookkeeping kinds       "
          f"{sum(n for k, n in by_kind.items() if k in bookkeeping)}  "
          f"(add_task, add_evaluation, add_policy, add_state_bucket)")
    print(f"  other self-authored kinds     {other}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
