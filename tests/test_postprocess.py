#!/usr/bin/env python3
"""Fixture tests pinning the fail-closed properties of the install-compliance
post-processor (scripts/eval/postprocess-results.py).

Each directory under tests/fixtures/postprocess/ is one scenario:
  aggregate-result.json  - the native eval output under test
  run-metadata.json      - the completeness contract the runner writes
  traces/*.jsonl         - per-run traces (referenced RELATIVELY)
  expected.json          - {"exit": 0|1, "stdout_contains": [...]}

Scenarios (why each exists — review round 2, pm-os#1):
  healthy         PASS is reachable: complete, clean results score 1.0
  echo-attack     forged success (echo'd commands + success strings in a Read
                  result) earns ZERO credit under the strict command grammar
  refusal-hiding  a refusal run inside a case whose MEAN clears the bar is
                  still caught by the no_refusal hard gate
  zero-run        a case with runs: [] FAILS by name (no silent skip)
  corrupt-trace   one undecodable trace line fails that run
  missing-grader  a run without the no_refusal grader FAILS (the gate cannot
                  silently vanish through a rename/removal)
  missing-case    an absent case FAILS (no passing by omission)

Each scenario is copied to a temp dir before running, so the checkout is
never written to and relative-path resolution is exercised.

Run: python3 tests/test_postprocess.py   (exit 0 = all pinned properties hold)
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POST = REPO / "scripts" / "eval" / "postprocess-results.py"
FIXTURES = REPO / "tests" / "fixtures" / "postprocess"


def run_scenario(src):
    expected = json.loads((src / "expected.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="postprocess-test-") as tmp:
        work = Path(tmp) / src.name
        shutil.copytree(src, work)
        proc = subprocess.run(
            [sys.executable, str(POST), str(work / "aggregate-result.json"),
             "--threshold", "0.85", "--keep-temps"],
            capture_output=True, text=True, timeout=60,
        )
        problems = []
        if proc.returncode != expected["exit"]:
            problems.append(
                f"exit {proc.returncode}, expected {expected['exit']}")
        for needle in expected.get("stdout_contains", []):
            if needle not in proc.stdout:
                problems.append(f"stdout missing: {needle!r}")
        verdict_path = work / "compliance-verdict.json"
        if not verdict_path.exists():
            problems.append("compliance-verdict.json not written")
        else:
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            if verdict.get("passed") is not (expected["exit"] == 0):
                problems.append(
                    f"verdict passed={verdict.get('passed')} disagrees with "
                    f"expected exit {expected['exit']}")
        return problems, proc.stdout


def main():
    scenarios = sorted(p for p in FIXTURES.iterdir() if p.is_dir())
    if not scenarios:
        print("FAIL: no fixture scenarios found — the pinned properties are "
              "not being tested")
        return 1
    failed = 0
    for src in scenarios:
        problems, stdout = run_scenario(src)
        if problems:
            failed += 1
            print(f"FAIL  {src.name}")
            for p in problems:
                print(f"      - {p}")
            print("      --- post-processor stdout ---")
            for line in stdout.splitlines():
                print(f"      | {line}")
        else:
            print(f"ok    {src.name}")
    print()
    if failed:
        print(f"{failed}/{len(scenarios)} scenario(s) failed")
        return 1
    print(f"all {len(scenarios)} fail-closed scenarios hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
