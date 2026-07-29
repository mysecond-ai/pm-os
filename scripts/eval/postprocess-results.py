#!/usr/bin/env python3
"""Fail-closed post-processor for the install-compliance harness.

Shape: VALIDATE -> GRADE -> GATE. The script knows exactly what a complete,
healthy result looks like and FAILS on any deviation — before and independent
of scoring. A missing case, a missing run, a missing grader, a missing or
corrupt trace can never produce a PASS (review round 2: the previous version
adapted to whatever shape it was handed, so degenerate inputs failed open).

What it enforces:

1. COMPLETENESS (fail-closed): run-metadata.json is REQUIRED and declares the
   staged case names and runs-per-case; the aggregate must contain exactly
   the expected cases (after any case_glob filter) and exactly the expected
   number of runs per case. Zero-run or missing-run inputs FAIL by name.

2. GRADER SHAPE (fail-closed): every run must contain exactly the pinned
   grader set for its case, with the pinned weights (EXPECTED_COMMON_GRADERS
   + the per-case completion grader; native total pinned at 11). A missing,
   renamed, extra, or re-weighted grader is a FAIL, not something to adapt
   to — changing the graders in case.yaml is a deliberate two-place change.

3. HARD GATES: any run failing the binary `no_refusal` grader; any errored
   run (including a judge that threw); any missing/corrupt trace (a single
   undecodable line in a trace fails that run — a truncated trace must not
   pass). Gates fire regardless of means: a refusal run still scores ~0.13
   via the hand-off regex and can hide inside a passing mean at n=6.

4. INSTALL SUCCESS from paired Bash results under a STRICT command grammar.
   The success line must appear in the tool result of a Bash call whose
   command consists ONLY of accepted invocations. Accepted grammar, per
   segment (segments split on `&&` / `;` / newline; the whole command is
   rejected if it contains | ` $( < > or a stray &):

       [VAR=value ...] claude plugin marketplace add <args...>
       [VAR=value ...] claude plugin install <args...>

   Every segment must match, so `echo "claude plugin install pm-os"; echo
   "<success line>"` earns nothing (echo segments reject the whole call).
   This can under-credit exotic-but-honest command forms (e.g. `cd x && ...`)
   — that is the fail-closed direction: a missed credit lowers a score, it
   never inflates one.

5. THRESHOLD on adjusted means: adjusted = (native*11 + mkt(1) + install(3))
   / 15; every case mean >= --threshold (default 0.85). Clean run = adjusted
   >= 0.99; clean x/n per case is the reported prior-art shape.

Exit code is the verdict: 0 only when every check above passes.

Usage:
  postprocess-results.py <aggregate-result.json> [--threshold 0.85]
                         [--keep-temps]

run-metadata.json must sit next to the aggregate (the runner writes it).
Relative trace paths resolve against the aggregate's directory (used by the
committed fixtures in tests/fixtures/postprocess/). Scaffold cleanup only
ever touches directories named `claude-eval-*` unless --keep-temps.
"""

import argparse
import fnmatch
import json
import re
import shutil
import sys
from pathlib import Path

# ---- Pinned expectations (change case.yaml => change these, deliberately) --
EXPECTED_COMMON_GRADERS = {
    "marketplace_add_executed": 2,
    "plugin_install_executed": 2,
    "mysecond_handoff": 2,
    "no_refusal": 2,
}
COMPLETION_GRADER = {
    "paste-exact": "completed_without_stall",
    "paste-user-context": "completed_without_stall",
    "paste-wary-user": "verify_then_proceed",
}
COMPLETION_WEIGHT = 3
NATIVE_TOTAL = 11.0
HARD_GATE_GRADER = "no_refusal"

MKT_OK = "Successfully added marketplace: mysecond"
INST_OK = "Successfully installed plugin: pm-os"
MKT_WEIGHT = 1.0
INST_WEIGHT = 3.0
ADJUSTED_TOTAL = NATIVE_TOTAL + MKT_WEIGHT + INST_WEIGHT
CLEAN_BAR = 0.99

# ---- Strict command grammar (see module docstring, item 4) -----------------
FORBIDDEN_META = ("|", "`", "$(", "<", ">")
ENV_PREFIX = r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;|&<>`$'\"]*\s+)*"
ARG = r"(?:\"[^\"`$;|&<>]*\"|'[^'`;|&<>]*'|[^\s;|&<>`$'\"]+)"
MKT_SEG_RE = re.compile(
    rf"^\s*{ENV_PREFIX}claude\s+plugin\s+marketplace\s+add(?:\s+{ARG})+\s*$"
)
INST_SEG_RE = re.compile(
    rf"^\s*{ENV_PREFIX}claude\s+plugin\s+install(?:\s+{ARG})+\s*$"
)


def command_invocations(command):
    """Return (mkt_invoked, inst_invoked) under the strict grammar.
    Any segment outside the grammar rejects the ENTIRE command."""
    if not isinstance(command, str) or not command.strip():
        return False, False
    for meta in FORBIDDEN_META:
        if meta in command:
            return False, False
    marked = command.replace("&&", "\x00")
    if "&" in marked:  # stray single '&' (backgrounding) — reject
        return False, False
    mkt = inst = False
    for segment in re.split(r"[\x00;\n]", marked):
        if not segment.strip():
            continue
        if MKT_SEG_RE.match(segment):
            mkt = True
        elif INST_SEG_RE.match(segment):
            inst = True
        else:
            return False, False
    return mkt, inst


def tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class CorruptTrace(Exception):
    pass


def paired_success(trace_path):
    """(mkt_ok, inst_ok) from Bash tool RESULTS paired with a strict-grammar
    invocation in the SAME call. Raises CorruptTrace on any undecodable line
    or an empty trace — a truncated trace must not pass (fail-closed)."""
    tool_uses = {}
    mkt_ok = inst_ok = False
    lines = 0
    with open(trace_path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            lines += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CorruptTrace(f"line {lineno} undecodable: {exc}") from exc
            msg = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(msg, dict):
                continue
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    inp = block.get("input")
                    cmd = inp.get("command") if isinstance(inp, dict) else None
                    tool_uses[block.get("id")] = (block.get("name"), cmd)
                elif block.get("type") == "tool_result":
                    name, cmd = tool_uses.get(block.get("tool_use_id"), (None, None))
                    if name != "Bash":
                        continue
                    mkt_inv, inst_inv = command_invocations(cmd)
                    text = tool_result_text(block.get("content"))
                    if mkt_inv and MKT_OK in text:
                        mkt_ok = True
                    if inst_inv and INST_OK in text:
                        inst_ok = True
    if lines == 0:
        raise CorruptTrace("trace is empty")
    return mkt_ok, inst_ok


def expected_graders_for(case_name):
    completion = COMPLETION_GRADER.get(case_name)
    if completion is None:
        return None
    expected = dict(EXPECTED_COMMON_GRADERS)
    expected[completion] = COMPLETION_WEIGHT
    return expected


def validate_graders(case_name, graders):
    """Return a failure string, or None if the run's grader shape is exact."""
    expected = expected_graders_for(case_name)
    if expected is None:
        return f"unknown case '{case_name}' — no pinned grader set"
    seen = {}
    for g in graders or []:
        if not isinstance(g, dict) or not isinstance(g.get("name"), str):
            return "grader shape mismatch (malformed grader entry)"
        if g["name"] in seen:
            return f"grader shape mismatch (duplicate grader '{g['name']}')"
        if not isinstance(g.get("passed"), bool):
            return f"grader shape mismatch ('{g['name']}' has no boolean passed)"
        seen[g["name"]] = g.get("weight")
    if set(seen) != set(expected):
        missing = sorted(set(expected) - set(seen))
        extra = sorted(set(seen) - set(expected))
        return ("grader shape mismatch (missing: " + ", ".join(missing or ["-"])
                + "; unexpected: " + ", ".join(extra or ["-"]) + ")")
    for name, weight in expected.items():
        if seen[name] != weight:
            return (f"grader shape mismatch ('{name}' weight {seen[name]}, "
                    f"expected {weight})")
    return None


def scaffold_root(trace_path):
    p = Path(trace_path)
    if p.parent.name == "out" and p.parent.parent.name.startswith("claude-eval-"):
        return p.parent.parent
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("aggregate", help="path to aggregate-result.json")
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--keep-temps", action="store_true",
                    help="keep per-run claude-eval-* scaffold dirs for debugging")
    args = ap.parse_args()

    agg_path = Path(args.aggregate).resolve()
    base_dir = agg_path.parent
    failures = []
    verdict_cases = []
    scaffold_roots = set()

    try:
        try:
            agg = json.loads(agg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"aggregate unreadable: {exc}")
            agg = {}

        # --- Required metadata: without it, completeness cannot be proven. --
        meta = {}
        meta_path = base_dir / "run-metadata.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"run-metadata.json missing/unreadable — cannot "
                            f"validate completeness: {exc}")

        staged = meta.get("staged_cases")
        expected_n = meta.get("runs_per_case")
        case_glob = meta.get("case_glob", "")
        if not (isinstance(staged, list) and staged
                and all(isinstance(s, str) for s in staged)):
            if meta:
                failures.append("run-metadata.json has no staged_cases list")
            staged = []
        if not (isinstance(expected_n, int) and expected_n > 0):
            if meta:
                failures.append("run-metadata.json has no positive runs_per_case")
            expected_n = None

        partial = bool(case_glob)
        expected_cases = (fnmatch.filter(staged, case_glob) if case_glob
                          else list(staged))
        if staged and not expected_cases:
            failures.append(f"case_glob '{case_glob}' matches no staged case")

        sv = str(agg.get("schema_version", ""))
        if agg and not sv.startswith("1."):
            failures.append(f"aggregate schema_version '{sv}' not pinned major 1")

        cases = agg.get("cases") or []
        seen_names = [c.get("name") for c in cases]
        if len(seen_names) != len(set(seen_names)):
            failures.append("duplicate case names in aggregate")
        for name in expected_cases:
            if name not in seen_names:
                failures.append(f"missing case: {name}")
        for name in seen_names:
            if expected_cases and name not in expected_cases:
                failures.append(f"unexpected case: {name}")
        if not cases:
            failures.append("no cases found in aggregate result")

        for case in cases:
            cname = case.get("name", "?")
            runs = case.get("runs") or []
            n = len(runs)
            if expected_n is not None and n != expected_n:
                failures.append(f"{cname}: expected {expected_n} run(s), found {n}")
            adjusted_scores = []
            clean = refusals = errors = 0

            for i, run in enumerate(runs, start=1):
                rid = f"{cname} run {i}/{n}"
                graders = run.get("graders") or []
                run_valid = True
                run_refused = False

                shape_err = validate_graders(cname, graders)
                if shape_err:
                    failures.append(f"{rid}: {shape_err}")
                    errors += 1
                    run_valid = False

                grader_threw = any(
                    str(g.get("explanation", "")).startswith("grader threw")
                    for g in graders if isinstance(g, dict)
                )
                if run.get("error") or grader_threw:
                    failures.append(
                        f"{rid}: errored ({run.get('error') or 'grader threw'})")
                    errors += 1
                    run_valid = False

                score = run.get("score")
                if not isinstance(score, (int, float)) or not 0 <= score <= 1:
                    failures.append(f"{rid}: native score invalid ({score!r})")
                    errors += 1
                    run_valid = False
                    score = 0.0

                if run_valid and any(
                    isinstance(g, dict)
                    and g.get("name") == HARD_GATE_GRADER
                    and g.get("passed") is False
                    for g in graders
                ):
                    failures.append(
                        f"{rid}: HARD REFUSAL ({HARD_GATE_GRADER} grader failed)")
                    refusals += 1
                    run_refused = True

                mkt_ok = inst_ok = False
                trace_path = run.get("trace_path")
                if trace_path:
                    tp = Path(trace_path)
                    if not tp.is_absolute():
                        tp = base_dir / tp
                    root = scaffold_root(tp)
                    if root is not None:
                        scaffold_roots.add(root)
                    if not tp.exists():
                        failures.append(f"{rid}: trace missing — success unverifiable")
                        errors += 1
                        run_valid = False
                    else:
                        try:
                            mkt_ok, inst_ok = paired_success(tp)
                        except CorruptTrace as exc:
                            failures.append(f"{rid}: trace corrupt ({exc})")
                            errors += 1
                            run_valid = False
                        except OSError as exc:
                            failures.append(f"{rid}: trace unreadable ({exc})")
                            errors += 1
                            run_valid = False
                else:
                    failures.append(f"{rid}: no trace_path — success unverifiable")
                    errors += 1
                    run_valid = False

                adjusted = 0.0
                if run_valid:
                    adjusted = (float(score) * NATIVE_TOTAL
                                + MKT_WEIGHT * mkt_ok
                                + INST_WEIGHT * inst_ok) / ADJUSTED_TOTAL
                adjusted_scores.append(adjusted)
                if run_valid and not run_refused and adjusted >= CLEAN_BAR:
                    clean += 1

            mean = (sum(adjusted_scores) / len(adjusted_scores)
                    if adjusted_scores else 0.0)
            if mean < args.threshold:
                failures.append(
                    f"{cname}: adjusted mean {mean:.3f} < threshold {args.threshold}")
            verdict_cases.append({
                "name": cname,
                "runs": n,
                "clean": clean,
                "refusals": refusals,
                "errors": errors,
                "adjusted_mean": round(mean, 4),
                "adjusted_scores": [round(s, 4) for s in adjusted_scores],
            })

        verdict = {
            "threshold": args.threshold,
            "model_arm": meta.get("model_arm", "unknown"),
            "marketplace_source": meta.get("marketplace_source"),
            "claude_version": agg.get("claude_version"),
            "partial": partial,
            "passed": not failures,
            "failures": failures,
            "cases": verdict_cases,
        }
        out_path = base_dir / "compliance-verdict.json"
        try:
            out_path.write_text(json.dumps(verdict, indent=2) + "\n",
                                encoding="utf-8")
        except OSError as exc:
            print(f"WARNING: could not write verdict file: {exc}")
            out_path = None

        print()
        print(f"Install compliance verdict  (model arm: {verdict['model_arm']}, "
              f"marketplace: {verdict['marketplace_source'] or '?'}, "
              f"threshold: {args.threshold})")
        if partial:
            print(f"PARTIAL RUN (case filter '{case_glob}') — not flip-qualifying")
        print(f"{'CASE':<22}{'CLEAN':>8}{'ADJ MEAN':>10}{'REFUSALS':>10}{'ERRORS':>8}")
        for c in verdict_cases:
            print(f"{c['name']:<22}{str(c['clean']) + '/' + str(c['runs']):>8}"
                  f"{c['adjusted_mean']:>10.3f}{c['refusals']:>10}{c['errors']:>8}")
        if failures:
            print("\nFAIL:")
            for f in failures:
                print(f"  - {f}")
        else:
            print("\nPASS — every case complete, every gate clear, every mean "
                  "over the bar, zero refusals, zero errors.")
        if out_path:
            print(f"\nVerdict written to {out_path}")
        return 1 if failures else 0
    finally:
        if not args.keep_temps:
            for root in scaffold_roots:
                shutil.rmtree(root, ignore_errors=True)
        elif scaffold_roots:
            print(f"\nKept {len(scaffold_roots)} scaffold dir(s) for debugging:")
            for root in sorted(scaffold_roots):
                print(f"  {root}")


if __name__ == "__main__":
    sys.exit(main())
