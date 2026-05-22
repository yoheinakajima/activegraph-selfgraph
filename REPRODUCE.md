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

| file                      | sha256[:16]        | condition                                |
| ------------------------- | ------------------ | ---------------------------------------- |
| `corpus.literal.jsonl`    | `57a86e94ba5e211d` | `SELFGRAPH_OBJECTTYPE_MATCH=literal` (BEFORE) |
| `corpus.relaxed.jsonl`    | `74bd52ff901bc1bc` | `SELFGRAPH_OBJECTTYPE_MATCH=relaxed` (AFTER)  |
| `adversarial.jsonl`       | `09b408bd369dc89d` | (relaxed; flag doesn't affect this run)  |
| `rollback.jsonl`          | `4e6333398e82e127` | (relaxed; flag doesn't affect this run)  |

Multiple consecutive cold runs on the reference machine produce
identical shas. If the script reports `MISMATCH` on yours, capture
the diff and file an issue — that's a reproducibility regression and
we want to know about it.

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

| file                                       | what it records                                                                          |
| ------------------------------------------ | ---------------------------------------------------------------------------------------- |
| `harness/results/corpus.literal.jsonl`     | 45 mechanical goals (BEFORE) × propose → validate → sandbox(promote=False)               |
| `harness/results/corpus.relaxed.jsonl`     | 72 mechanical goals (AFTER) × propose → validate → sandbox(promote=False)                |
| `harness/results/adversarial.jsonl`        | 28 mechanical adversarial attempts, one row per attempt, with caught/expected            |
| `harness/results/rollback.jsonl`           | 5 promote=True runs with byte-identical replay-to-before-promote                         |
| `harness/results/*.meta.json`              | per-run aggregate (counts, llm-active flag, objecttype-match-mode, jsonl sha)            |

The aggregate tables are produced by:

```bash
PYTHONPATH=. python -m harness.report
PYTHONPATH=. python -m harness.compare \
    harness/results/corpus.literal.jsonl \
    harness/results/corpus.relaxed.jsonl
PYTHONPATH=. python -m harness.query_self_authored \
    harness/results/corpus.relaxed.jsonl
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

## The extractor A/B (now first-class reproducible)

The paper's key causal result compares the ObjectType extractor in
two conditions, controlled by `SELFGRAPH_OBJECTTYPE_MATCH`:

* `literal`: only the original `add_object("Cap", ...)` capitalized
  literal regex (the BEFORE condition).
* `relaxed` (default): adds the lowercase `ObjectType(name="...")`
  constructor-call regex used by activegraph runtime packs (the
  AFTER condition).

Any other value raises `ValueError` at extraction time — a typo
cannot silently shift the canonical shas.

`harness/reproduce.sh` runs the corpus pipeline twice on every cold
run — once with each flag value — and emits `corpus.literal.jsonl`
and `corpus.relaxed.jsonl`. The committed A/B table on the reference
machine:

| metric                                    | BEFORE (literal) | AFTER (relaxed)  |
| ----------------------------------------- | ---------------- | ---------------- |
| n_goals                                   | 45               | 72               |
| derived_from_path_class = runtime         | 0                | 27               |
| derived_from_path_class = selfgraph       | 45               | 45               |
| grounding rate, runtime-derived           | n/a (0 denom)    | 18/27 (66.7%)    |
| grounding rate, selfgraph-derived         | 27/45 (60.0%)    | 27/45 (60.0%)    |
| origin mix — grounded-in-extracted        | 27/477 (5.7%)    | 54/747 (7.2%)    |
| origin mix — built-in-scaffold            | 90/477 (18.9%)   | 126/747 (16.9%)  |
| origin mix — self-authored                | 270/477 (56.6%)  | 432/747 (57.8%)  |
| origin mix — domain-new                   | 90/477 (18.9%)   | 135/747 (18.1%)  |
| fork_path == sqlite                       | 45/45 (100%)     | 72/72 (100%)     |
| live_graph_unchanged                      | 45/45 (100%)     | 72/72 (100%)     |

The cleanliness invariant `harness/compare.py` enforces at the end of
every reproduce run: the **selfgraph-derived grounding row is
byte-identical across conditions** (27/45 == 27/45). That single-
variable guarantee — only the extractor rule moves — is what makes
the runtime-derived 18/27 finding a causal result, not a confound.
If that invariant fails on your machine `reproduce.sh` exits non-zero
with a `MISMATCH` line.

## PatchProposal lifecycle, recapped

`draft → validated → applied` (happy path) or `draft → rejected`. The
transitions are enforced by convention at two call sites
(`validate_proposal` and `sandbox_apply`) rather than by a state
machine; `cmd_promote` re-runs validation with `mutate_status=False`
before applying so a stale `validated` marker can't bypass the
guardrail. See `selfgraph/cli.py` and `tests/test_smoke.py` for both
paths.
