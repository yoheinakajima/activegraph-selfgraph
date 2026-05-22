"""Harness invariants. Imported at the top of every entry point.

The measured paper results are deterministic-floor-only — no LLM
augmentation, no model calls anywhere in propose / validate / sandbox
/ classify_change / harness. Set ``SELFGRAPH_HARNESS_ALLOW_LLM=1`` to
override (only meaningful if you want to deliberately measure an
LLM-augmented run; the paper's reported shas are the floor).
"""

from __future__ import annotations

import os
import sys


_OVERRIDE_VAR = "SELFGRAPH_HARNESS_ALLOW_LLM"


def require_no_llm_env() -> None:
    """Refuse to run if ``ANTHROPIC_API_KEY`` is set without an
    explicit override. Print a structured error so a paper reviewer
    can see at a glance which invariant tripped."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return
    if os.environ.get(_OVERRIDE_VAR) == "1":
        print("[harness] WARNING: ANTHROPIC_API_KEY is set AND "
              f"{_OVERRIDE_VAR}=1 — proceeding with LLM augmentation "
              "ENABLED. Results will NOT match the paper's canonical "
              "shas; this is an LLM-augmented variant, not the "
              "measured-floor baseline.", file=sys.stderr)
        return
    print(
        "[harness] REFUSING TO RUN — ANTHROPIC_API_KEY is set in the "
        "environment.\n"
        "\n"
        "The paper's canonical shas were generated on the deterministic"
        " floor (no\n"
        "LLM augmentation). If the key is set, the extractor's optional"
        " LLM pass\n"
        "would run and the resulting graph would be LLM-shaped, "
        "breaking sha\n"
        "reproducibility and the 'no API key required' claim in "
        "REPRODUCE.md.\n"
        "\n"
        "To run the canonical pipeline: unset ANTHROPIC_API_KEY and try"
        " again.\n"
        f"To deliberately run an LLM-augmented variant: set "
        f"{_OVERRIDE_VAR}=1 (the\n"
        "output shas will diverge from the paper).",
        file=sys.stderr,
    )
    sys.exit(64)
