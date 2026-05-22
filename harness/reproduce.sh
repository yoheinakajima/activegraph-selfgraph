#!/usr/bin/env bash
# Cold-start reproduction script for the selfgraph paper artifacts.
#
# Wipes every persisted DB and harness result, regenerates all FOUR
# result files from scratch — the two corpus conditions (literal
# extractor / BEFORE; relaxed extractor / AFTER), the adversarial
# guardrail slice, and the rollback precondition — then prints the
# A/B table from the two committed corpora and a sha-match table
# against harness/results/CANONICAL_SHAS.txt.
#
# Requires:  Python 3.11+, `pip install -r requirements.txt`, and NO
#            ANTHROPIC_API_KEY in the environment. The measured loop
#            is LLM-free by construction (see REPRODUCE.md).
#
# Exit non-zero if any regenerated sha doesn't match the canonical
# record OR the A/B cleanliness invariant (selfgraph-derived
# grounding identical across conditions) fails.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "[reproduce] ANTHROPIC_API_KEY is set; harness will refuse." >&2
  echo "[reproduce] Unset it: 'unset ANTHROPIC_API_KEY' and re-run." >&2
  exit 64
fi

EXPECTED_CORPUS_LITERAL_SHA=""
EXPECTED_CORPUS_RELAXED_SHA=""
EXPECTED_ADVERSARIAL_SHA=""
EXPECTED_ROLLBACK_SHA=""
EXPECTED_FUTURE_EVENT_SHA=""
EXPECTED_EXTRACTOR_RECALL_SHA=""
if [[ -f harness/results/CANONICAL_SHAS.txt ]]; then
  # shellcheck disable=SC1091
  source harness/results/CANONICAL_SHAS.txt
fi

echo "[reproduce] wiping persisted DB state and previous result files"
rm -rf .selfgraph .selfgraph-demo .selfgraph-harness \
       .selfgraph-rollback .selfgraph-adversarial \
       .selfgraph-future-event \
       harness/results/*.jsonl harness/results/*.meta.json \
       harness/results/extractor_recall.json \
       2>/dev/null || true
mkdir -p harness/results

export PYTHONPATH="${PYTHONPATH:-.}"

echo
echo "[reproduce] (1/4) benign corpus  —  MATCH=literal  (BEFORE)"
SELFGRAPH_OBJECTTYPE_MATCH=literal \
  python -m harness.run_corpus harness/results/corpus.literal.jsonl \
  > /tmp/selfgraph-literal.log 2>&1
echo "    → harness/results/corpus.literal.jsonl"

echo
echo "[reproduce] (2/4) benign corpus  —  MATCH=relaxed (AFTER)"
SELFGRAPH_OBJECTTYPE_MATCH=relaxed \
  python -m harness.run_corpus harness/results/corpus.relaxed.jsonl \
  > /tmp/selfgraph-relaxed.log 2>&1
echo "    → harness/results/corpus.relaxed.jsonl"

echo
echo "[reproduce] (3/4) adversarial guardrail slice (MATCH=relaxed)"
SELFGRAPH_OBJECTTYPE_MATCH=relaxed \
  python -m harness.run_adversarial > /tmp/selfgraph-adv.log 2>&1
echo "    → harness/results/adversarial.jsonl"

echo
echo "[reproduce] (4/6) rollback precondition (MATCH=relaxed)"
SELFGRAPH_OBJECTTYPE_MATCH=relaxed \
  python -m harness.rollback_precondition > /tmp/selfgraph-rb.log 2>&1
echo "    → harness/results/rollback.jsonl"

echo
echo "[reproduce] (5/6) future-event mechanism test (MATCH=relaxed)"
SELFGRAPH_OBJECTTYPE_MATCH=relaxed \
  python -m harness.run_future_event > /tmp/selfgraph-fe.log 2>&1
echo "    → harness/results/future_event.jsonl"

echo
echo "[reproduce] (6/6) extractor discovery recall (MATCH read inside script)"
python -m harness.extractor_recall > /tmp/selfgraph-recall.log 2>&1
echo "    → harness/results/extractor_recall.json"

echo
echo "============================================================"
echo "  A/B table — BEFORE (literal) vs AFTER (relaxed)"
echo "============================================================"
python -m harness.compare \
       harness/results/corpus.literal.jsonl \
       harness/results/corpus.relaxed.jsonl
ab_status=$?

echo
echo "============================================================"
echo "  sha-match table"
echo "============================================================"

ok=0
fail=0
check () {
  local name="$1" path="$2" expected="$3"
  local actual
  actual=$(sha256sum "$path" | head -c 16)
  if [[ -z "$expected" ]]; then
    printf "  %-22s %s   (no expected sha — first run or unrecorded)\n" \
           "$name" "$actual"
    ok=$((ok + 1))
  elif [[ "$actual" == "$expected" ]]; then
    printf "  %-22s %s == %s  MATCH\n" "$name" "$actual" "$expected"
    ok=$((ok + 1))
  else
    printf "  %-22s %s != %s  MISMATCH\n" "$name" "$actual" "$expected"
    fail=$((fail + 1))
  fi
}

check "corpus.literal.jsonl"  harness/results/corpus.literal.jsonl  "$EXPECTED_CORPUS_LITERAL_SHA"
check "corpus.relaxed.jsonl"  harness/results/corpus.relaxed.jsonl  "$EXPECTED_CORPUS_RELAXED_SHA"
check "adversarial.jsonl"     harness/results/adversarial.jsonl     "$EXPECTED_ADVERSARIAL_SHA"
check "rollback.jsonl"        harness/results/rollback.jsonl        "$EXPECTED_ROLLBACK_SHA"
check "future_event.jsonl"    harness/results/future_event.jsonl    "$EXPECTED_FUTURE_EVENT_SHA"
check "extractor_recall.json" harness/results/extractor_recall.json "$EXPECTED_EXTRACTOR_RECALL_SHA"

echo
echo "  ok=$ok  fail=$fail"
if [[ "$fail" -gt 0 ]]; then
  echo "[reproduce] FAIL — one or more regenerated shas do not match." >&2
  echo "            See logs in /tmp/selfgraph-*.log" >&2
  exit 1
fi
if [[ "$ab_status" -ne 0 ]]; then
  echo "[reproduce] FAIL — A/B cleanliness invariant violated." >&2
  exit "$ab_status"
fi
echo "[reproduce] all regenerated shas match and A/B invariant holds."
