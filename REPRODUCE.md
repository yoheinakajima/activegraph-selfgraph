# Reproducing the selfgraph paper artifacts

This document is the contract for reproducing every number the paper
cites from this repository. A clone, a clean Python env, and one
command should be enough.

## TL;DR

```bash
pip install -r requirements.txt
bash harness/reproduce.sh
```

The script wipes any persisted state, runs the full pipeline cold,
and asserts the regenerated files match these recorded shas:

| file                      | sha256[:16]        |
| ------------------------- | ------------------ |
| `corpus.jsonl`            | `74bd52ff901bc1bc` |
| `adversarial.jsonl`       | `09b408bd369dc89d` |
| `rollback.jsonl`          | `4e6333398e82e127` |

Three consecutive cold runs on the reference machine produce identical
shas. If the script reports `MISMATCH` on yours, capture the diff and
file an issue — that's a reproducibility regression and we want to
know about it.

## Environment

* **Python**: 3.11 (the reference machine runs 3.11.15)
* **activegraph**: `1.0.5.post2` (pinned in `requirements.txt`)
* **OS**: Linux (any modern distribution); macOS should also work.
* **API key**: **none required**. The measured loop is LLM-free by
  construction (see `LLM-free invariant` below). The harness *refuses*
  to run while `ANTHROPIC_API_KEY` is set, unless you explicitly
  override with `SELFGRAPH_HARNESS_ALLOW_LLM=1` (which produces an
  LLM-augmented variant whose shas will not match the paper).

Expected runtime end-to-end on a 2024-era laptop: **under one minute**.

## LLM-free invariant

The paper's claim is *safe* self-modification — log-grounded rollback
and fork-and-diff — not "better proposals from a bigger model." The
measured pipeline therefore makes zero model calls. Specifically:

* `selfgraph/propose.py` — graph queries only, no LLM.
* `selfgraph/guardrails.py` — substring + structural checks, no LLM.
* `selfgraph/sandbox.py` — `Runtime.fork` + structural diff, no LLM.
* `selfgraph/query.py::classify_change` — pure data classifier, no LLM.
* `harness/*.py` — instrumentation only, no LLM.

`selfgraph/extract.py` has an *optional* LLM augmentation pass behind
`ANTHROPIC_API_KEY`; the harness refuses to run when the key is set
so the canonical shas can never be silently shaped by it. Every
result file's `*.meta.json` carries `llm_augment_active: false` as an
audit stamp.

Grep proof, runnable yourself:

```bash
grep -nE 'anthropic|Anthropic|messages\.create|claude|llm_provider|LLMProvider' \
     selfgraph/propose.py selfgraph/guardrails.py selfgraph/sandbox.py \
     harness/*.py
# (no output expected)
```

## What gets generated

| file                               | what it records                                                                   |
| ---------------------------------- | --------------------------------------------------------------------------------- |
| `harness/results/corpus.jsonl`     | 72 mechanically-generated goals × propose → validate → sandbox(promote=False)     |
| `harness/results/adversarial.jsonl`| 28 mechanical adversarial attempts, one row per attempt, with caught/expected     |
| `harness/results/rollback.jsonl`   | 5 promote=True runs with byte-identical replay-to-before-promote                  |
| `harness/results/*.meta.json`      | per-run aggregate (counts, llm-active flag, jsonl sha)                            |

The aggregate tables are produced by:

```bash
PYTHONPATH=. python -m harness.report
PYTHONPATH=. python -m harness.compare \
    harness/results/corpus.jsonl harness/results/corpus.jsonl
PYTHONPATH=. python -m harness.query_self_authored
```

## Determinism — what we did to make the shas portable

Two sources of cross-machine non-determinism existed in earlier
warm-instance measurements and were closed in this pass; both are
listed here so a reader can audit:

1. **Wall-clock fields removed.** The original corpus JSONL rows
   carried `t_start` / `t_end` (floats from `time.time()`). They are
   gone — the harness writes timestamp-free rows.
2. **Filesystem walk order pinned.** `os.walk` in `ingest_paths`
   and `pkgutil.walk_packages` in `ingest_module_docs` now sort
   their output before processing, so ingestion order — and therefore
   the monotonic `Object#N` IDs that proposals later cite — is
   stable across machines. A `set()` of regex captures in
   `extract.py` is also sorted before iteration (otherwise
   `PYTHONHASHSEED` randomization moves it).

None of these change agent behavior. They change the *order* in which
the same set of inputs is processed, which downstream pins the IDs
and therefore the JSONL bytes.

## Historical A/B (one-time research result, not reproduced here)

An earlier measurement run compared the ObjectType extractor *before*
its constructor-call relaxation against *after*. The relaxation is now
the only extractor in the tree, so cold-run reproduction produces only
the post-relaxation corpus. The A/B numbers — preserved for paper
reference — were:

| metric                                    | BEFORE         | AFTER           |
| ----------------------------------------- | -------------- | --------------- |
| n_goals                                   | 45             | 72              |
| derived_from_path_class = runtime         | 0              | 27              |
| derived_from_path_class = selfgraph       | 45             | 45              |
| grounding rate, runtime-derived           | n/a (0 denom)  | 18/27 (66.7%)   |
| grounding rate, selfgraph-derived         | 27/45 (60.0%)  | 27/45 (60.0%)   |
| fork_path == sqlite                       | 45/45 (100%)   | 72/72 (100%)    |
| live_graph_unchanged                      | 45/45 (100%)   | 72/72 (100%)    |

The headline finding is that the selfgraph-derived row of the
grounding table is byte-identical across the A/B — the BEFORE goals
are unaffected by the relaxation, so the ONLY moving variable is the
extractor rule.

## PatchProposal lifecycle, recapped

`draft → validated → applied` (happy path) or `draft → rejected`. The
transitions are enforced by convention at two call sites
(`validate_proposal` and `sandbox_apply`) rather than by a state
machine; `cmd_promote` re-runs validation with `mutate_status=False`
before applying so a stale `validated` marker can't bypass the
guardrail. See `selfgraph/cli.py` and `tests/test_smoke.py` for both
paths.
