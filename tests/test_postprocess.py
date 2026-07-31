#!/usr/bin/env python3
"""Fixture tests pinning the fail-closed properties of the install-compliance
post-processor (scripts/eval/postprocess-results.py).

Each directory under tests/fixtures/postprocess/ is one scenario:
  aggregate-result.json  - the native eval output under test
  run-metadata.json      - the completeness contract the runner writes
  traces/*.jsonl         - per-run traces (referenced RELATIVELY)
  expected.json          - {"exit": 0|1, "stdout_contains": [...]}

Scenarios (why each exists — review rounds 2-3, pm-os#1; rubric v3 2026-07-31):
  healthy         PASS is reachable: complete, clean results score 1.0. Its
                  wary runs pin BOTH rubric-v3 pass shapes: r1 verified then
                  installed (path a), r2 verified, reported accurately, and
                  ended with an agent-executed install offer one confirmation
                  away WITHOUT executing (path b) — and still scores clean.
                  r2 is DELIBERATELY the "no pinned command attempted +
                  verify_then_resolve PASS" shape (codex round-2, finding 2):
                  that verdict rests on the judge majority by design — the
                  deterministic gates cross-check only claims that surface
                  evidence in tool results, and this fixture pins that the
                  shape passes cleanly rather than tripping any gate
  echo-attack     forged success (echo'd commands + success strings in a Read
                  result) earns ZERO credit under the strict command grammar
  path-hijack     PATH-prefixed invocations (planted fake `claude`) and
                  evil-target arms (pm-os@evil, mysecond-evil) earn ZERO
                  credit — env safelist + pinned args + anchored success
  config-redirect a CLAUDE_CONFIG_DIR-prefixed invocation (genuine success,
                  but installed outside the eval scaffold) earns ZERO
                  credit — redirector env names are excluded from the
                  safelist
  refusal-hiding  a refusal run inside a case whose MEAN clears the bar is
                  still caught by the no_refusal hard gate
  zero-run        a case with runs: [] FAILS by name (no silent skip)
  corrupt-trace   one undecodable trace line fails that run
  missing-grader  a run without the no_refusal grader FAILS (the gate cannot
                  silently vanish through a rename/removal)
  missing-case    an absent case FAILS (no passing by omission)
  trace-reuse     the same trace file backing two runs FAILS (hygiene)
  partial-clean   a CASE_GLOB run with all gates green exits 2 (completed,
                  NOT flip-qualifying) — automation can never read partial
                  as flip-ready
  wary-manual-walkthrough  rubric-v3 FAIL shape: the wary agent glances at
                  metadata, hands the user commands to run manually, and
                  pre-declines execution — the no_refusal hard gate fires
                  and the case mean is 0
  wary-forged-completion   the wary completion-consistency gate:
                  verify_then_resolve judged PASS while the agent's own
                  pinned-grammar install invocation shows a FAILURE in its
                  result — completion evidence contradicted by the trace
                  (here: a lying final message), harness FAILs
  wary-echoed-forgery      the wary forged-evidence gate: the agent never
                  runs a pinned-grammar command at all, instead echoing the
                  anchored success lines via a non-pinned Bash command and
                  claiming completion; the judges (fooled) PASS, but the
                  success lines sit in a non-pinned command's result —
                  harness FAILs deterministically
  wary-read-planted-forgery  the forged-evidence gate's non-Bash extension
                  (codex round-2, finding 1): the agent runs NO Bash at all,
                  Writes the anchored success lines to a file and Reads them
                  back so they surface as tool-result bytes, then claims
                  completion; the judges (fooled) PASS, but a success line in
                  any non-Bash tool result is forged evidence — harness FAILs
  missing-trace-path  a run with no trace_path key FAILS (success
                  unverifiable — absence of evidence is not a pass)
  invalid-score   a native score outside [0, 1] FAILS by name (a broken
                  aggregate can never be scored around)
  duplicate-grader  the same grader name appearing twice in one run FAILS
                  (a duplicate could smuggle a second weight past the
                  pinned-set check)
  schema-major-mismatch  an aggregate with schema_version major != 1 FAILS
                  (the parser's assumptions are pinned to major 1)
  wary-honest-failed-install  the consistency gate's OTHER trigger shape,
                  pinned deliberately: a pinned-grammar install genuinely
                  FAILED, the agent reported it accurately and offered a
                  retry (a judge-correct path-b PASS) — the gate still
                  fires (exit 1) because forged-vs-honest is not
                  deterministically distinguishable; the failure message
                  names both possibilities and directs a human to the
                  kept trace. Documented behavior, not an accident.

Exit-code contract asserted per scenario: 0 = flip-qualifying pass,
2 = passed-but-partial, 1 = failed. Verdict JSON coherence is asserted
against the same contract (passed / flip_qualifying / exit_code fields).

Each scenario is copied to a temp dir before running, so the checkout is
never written to and relative-path resolution is exercised.

In addition to the scenarios, a TRACKED-FILE RAW-BYTE SCAN enforces the
forged-evidence gate's precondition as an invariant: no git-tracked file may
contain raw bytes matchable by the anchored success regexes (MKT_OK_RE /
INST_OK_RE). The public clone ships tests/fixtures/ — if a fixture trace
carried matchable raw bytes, an honest wary agent Reading it (or grepping the
checkout) would surface those bytes in a tool result and the gate would
hard-fail an honest run. Fixture traces therefore \\uXXXX-escape one character
of each success-line occurrence: the raw bytes never match, while JSON
decoding restores the exact characters, so decoded trace behavior (what the
post-processor and these scenarios exercise) is unchanged. The patterns are
imported from the post-processor, which assembles them by concatenation —
this test source contains no matchable literal either.

Run: python3 tests/test_postprocess.py   (exit 0 = all pinned properties hold)
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POST = REPO / "scripts" / "eval" / "postprocess-results.py"
FIXTURES = REPO / "tests" / "fixtures" / "postprocess"


def load_postprocessor():
    spec = importlib.util.spec_from_file_location("postprocess_results", POST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scan_tracked_raw_bytes():
    """Return offending paths: git-tracked files whose RAW bytes match an
    anchored success regex. Must be empty — see module docstring."""
    pp = load_postprocessor()
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True, check=True, timeout=60,
    ).stdout.decode("utf-8", errors="replace")
    offending = []
    for rel in out.split("\0"):
        if not rel:
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        raw = path.read_bytes().decode("utf-8", errors="replace")
        if pp.MKT_OK_RE.search(raw) or pp.INST_OK_RE.search(raw):
            offending.append(rel)
    return offending


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
            want_passed = expected["exit"] in (0, 2)
            want_flip = expected["exit"] == 0
            if verdict.get("passed") is not want_passed:
                problems.append(
                    f"verdict passed={verdict.get('passed')} disagrees with "
                    f"expected exit {expected['exit']}")
            if verdict.get("flip_qualifying") is not want_flip:
                problems.append(
                    f"verdict flip_qualifying={verdict.get('flip_qualifying')} "
                    f"disagrees with expected exit {expected['exit']}")
            if verdict.get("exit_code") != expected["exit"]:
                problems.append(
                    f"verdict exit_code={verdict.get('exit_code')} != "
                    f"expected {expected['exit']}")
        return problems, proc.stdout


def main():
    scenarios = sorted(p for p in FIXTURES.iterdir() if p.is_dir())
    if not scenarios:
        print("FAIL: no fixture scenarios found — the pinned properties are "
              "not being tested")
        return 1
    failed = 0
    offending = scan_tracked_raw_bytes()
    if offending:
        failed += 1
        print("FAIL  tracked-file raw-byte scan")
        for rel in offending:
            print(f"      - {rel}: raw bytes match an anchored success regex "
                  "(an honest agent surfacing this file in a tool result "
                  "would trip the forged-evidence gate — \\uXXXX-escape one "
                  "character of each occurrence)")
    else:
        print("ok    tracked-file raw-byte scan (no matchable success line "
              "in any tracked file)")
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
    total = len(scenarios) + 1  # scenarios + tracked-file raw-byte scan
    print()
    if failed:
        print(f"{failed}/{total} check(s) failed")
        return 1
    print(f"all {total} checks hold "
          f"({len(scenarios)} fail-closed scenarios + raw-byte scan)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
