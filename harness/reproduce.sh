#!/usr/bin/env bash
# Cold-start reproduction script for the selfgraph paper artifacts.
#
# Wipes every persisted DB and harness result, regenerates all three
# result files from scratch, and prints a sha-match table against the
# canonical shas recorded in harness/results/CANONICAL_SHAS.txt.
#
# Requires:  Python 3.11+, `pip install -r requirements.txt`, and NO
#            ANTHROPIC_API_KEY in the environment. The measured loop
#            is LLM-free by construction (see REPRODUCE.md).
#
# Exit non-zero if any regenerated sha doesn't match the canonical
# record — that means the run wasn't reproducible.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "[reproduce] ANTHROPIC_API_KEY is set; harness will refuse." >&2
  echo "[reproduce] Unset it: 'unset ANTHROPIC_API_KEY' and re-run." >&2
  exit 64
fi

EXPECTED_CORPUS_SHA=""
EXPECTED_ADVERSARIAL_SHA=""
EXPECTED_ROLLBACK_SHA=""
if [[ -f harness/results/CANONICAL_SHAS.txt ]]; then
  # shellcheck disable=SC1091
  source harness/results/CANONICAL_SHAS.txt
fi

echo "[reproduce] wiping persisted DB state and previous result files"
rm -rf .selfgraph .selfgraph-demo .selfgraph-harness \
       .selfgraph-rollback .selfgraph-adversarial \
       harness/results/*.jsonl harness/results/*.meta.json \
       2>/dev/null || true
mkdir -p harness/results

export PYTHONPATH="${PYTHONPATH:-.}"

echo
echo "[reproduce] (1/3) benign corpus"
python -m harness.run_corpus > /tmp/selfgraph-corpus.log 2>&1
echo "    → harness/results/corpus.jsonl"

echo
echo "[reproduce] (2/3) adversarial guardrail slice"
python -m harness.run_adversarial > /tmp/selfgraph-adv.log 2>&1
echo "    → harness/results/adversarial.jsonl"

echo
echo "[reproduce] (3/3) rollback precondition"
python -m harness.rollback_precondition > /tmp/selfgraph-rb.log 2>&1
echo "    → harness/results/rollback.jsonl"

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

check "corpus.jsonl"       harness/results/corpus.jsonl       "$EXPECTED_CORPUS_SHA"
check "adversarial.jsonl"  harness/results/adversarial.jsonl  "$EXPECTED_ADVERSARIAL_SHA"
check "rollback.jsonl"     harness/results/rollback.jsonl     "$EXPECTED_ROLLBACK_SHA"

echo
echo "  ok=$ok  fail=$fail"
if [[ "$fail" -gt 0 ]]; then
  echo "[reproduce] FAIL — one or more regenerated shas do not match." >&2
  echo "            See logs in /tmp/selfgraph-*.log" >&2
  exit 1
fi
echo "[reproduce] all regenerated shas match the canonical record."
