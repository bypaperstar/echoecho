#!/usr/bin/env bash
# THE merge gate: run all three scripted demos headlessly (scripted agent +
# FakeLLM; demos 2 and 3 hit the LIVE keyless endpoints) and assert the final
# workspace/*.md contents + the .tasks.jsonl event sequence, then run the full
# pytest suite. Prints a per-demo PASS/FAIL summary; exits non-zero on any FAIL.
set -u
cd "$(dirname "$0")/.."
PY=${PY:-python3}
LOGDIR=${TMPDIR:-/tmp}
results=()
fail=0

run_demo() {
  local n="$1" script="$2" log="$LOGDIR/echo_demo$1.log"
  echo "== demo $n: $script =="
  rm -rf workspace/*.md workspace/*/ workspace/.tasks.jsonl
  if ECHO_FAKE_LLM=1 "$PY" echo.py --script "$script" >"$log" 2>&1 \
     && "$PY" scripts/check_demo.py "$n" >>"$log" 2>&1; then
    results+=("demo $n: PASS")
    grep '^  ok:' "$log"
  else
    results+=("demo $n: FAIL   (log: $log)")
    fail=1
    tail -25 "$log"
  fi
}

run_demo 1 fixtures/demo1.txt
run_demo 2 fixtures/demo2.txt
run_demo 3 fixtures/demo3.txt
# the generic gate: same asks, ONE kind (agent.run), keyless agent replay
ECHO_FAKE_AGENT_SCRIPT=fixtures/agent/demo_generic \
  run_demo generic fixtures/demo_generic.txt

echo "== full pytest suite =="
if "$PY" -m pytest tests/ -q; then
  results+=("pytest: PASS")
else
  results+=("pytest: FAIL")
  fail=1
fi

echo
echo "== demo_check summary =="
for r in "${results[@]}"; do echo "  $r"; done
[ "$fail" -eq 0 ] && echo "MERGE GATE: PASS" || echo "MERGE GATE: FAIL"
exit "$fail"
