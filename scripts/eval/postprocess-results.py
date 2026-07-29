#!/usr/bin/env python3
"""Post-process `claude plugin eval` results for the install-compliance harness.

Why this exists (review findings, pm-os#1 round 1):

1. Install SUCCESS cannot be graded with a regex over the whole trace: the
   trace contains every document the agent read, so a success string quoted
   anywhere (a README, these very eval files in local marketplace mode) would
   false-pass. Here, success is graded ONLY from Bash tool RESULTS, and only
   when the SAME Bash call's input actually invoked the corresponding plugin
   command (command+result pairing). Residual vector — a model deliberately
   crafting a command that both matches the invocation pattern and prints the
   success line — is intentional grader-gaming, outside this harness's threat
   model (we measure refusal/stall of a cold agent that has no knowledge of,
   or incentive about, the grading).

2. The hard-refusal bar must be MACHINE-ENFORCED, not documented: a refusal
   run still scores >0 via the /mysecond-handoff regex, and at n=6 a single
   refusal can hide inside a case mean that clears the threshold (5x1.0 +
   one refusal ~0.13 -> ~0.855 >= 0.85). So ANY run whose binary `no_refusal`
   grader FAILed fails the whole harness, regardless of means.

Gates (any -> exit 1):
  - any run errored (including an LLM grader that threw)
  - any run failed the `no_refusal` grader
  - any run's trace is missing/unreadable (success cannot be verified)
  - any case's ADJUSTED mean score < threshold
  - no cases found

Adjusted score per run = (native_score * native_weight_total
                          + 1 * marketplace_add_paired_success
                          + 3 * plugin_install_paired_success)
                         / (native_weight_total + 4)
i.e. the success checks re-enter the original weighting, graded from paired
Bash results instead of trace regexes. "Clean" run = adjusted >= 0.99 with no
error and no refusal; the x/n clean count is the prior-art report shape.

Usage:
  postprocess-results.py <aggregate-result.json> [--threshold 0.85]
                         [--keep-temps]

Always deletes the per-run scaffold dirs (--keep-temp litter) afterwards
unless --keep-temps is passed; cleanup runs even when gates fail.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

MKT_CMD_RE = re.compile(r"plugin\s+marketplace\s+add")
INST_CMD_RE = re.compile(r"plugin\s+install")
MKT_OK = "Successfully added marketplace: mysecond"
INST_OK = "Successfully installed plugin: pm-os"
MKT_WEIGHT = 1.0
INST_WEIGHT = 3.0
CLEAN_BAR = 0.99


def tool_result_text(content):
    """Flatten a tool_result content field (string or block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def paired_success(trace_path):
    """Return (mkt_ok, inst_ok) graded from Bash tool RESULTS paired with the
    invoking command. Raises OSError/ValueError if the trace is unreadable."""
    tool_uses = {}  # id -> (tool_name, input_json_str)
    mkt_ok = False
    inst_ok = False
    with open(trace_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_uses[block.get("id")] = (
                        block.get("name"),
                        json.dumps(block.get("input", {})),
                    )
                elif block.get("type") == "tool_result":
                    name, input_str = tool_uses.get(
                        block.get("tool_use_id"), (None, "")
                    )
                    if name != "Bash":
                        continue
                    text = tool_result_text(block.get("content"))
                    if MKT_CMD_RE.search(input_str) and MKT_OK in text:
                        mkt_ok = True
                    if INST_CMD_RE.search(input_str) and INST_OK in text:
                        inst_ok = True
    return mkt_ok, inst_ok


def scaffold_root(trace_path):
    """<root>/out/trace.jsonl -> <root>; None if the shape is unexpected."""
    p = Path(trace_path)
    if p.parent.name == "out":
        return p.parent.parent
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("aggregate", help="path to aggregate-result.json")
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--keep-temps", action="store_true",
                    help="keep per-run scaffold dirs for debugging")
    args = ap.parse_args()

    agg_path = Path(args.aggregate)
    with open(agg_path, encoding="utf-8") as fh:
        agg = json.load(fh)

    meta = {}
    meta_path = agg_path.parent / "run-metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}

    scaffold_roots = set()
    failures = []
    verdict_cases = []

    try:
        cases = agg.get("cases") or []
        if not cases:
            failures.append("no cases found in aggregate result")

        for case in cases:
            cname = case.get("name", "?")
            adjusted_scores = []
            clean = 0
            refusals = 0
            errors = 0
            n = len(case.get("runs") or [])
            for i, run in enumerate(case.get("runs") or [], start=1):
                rid = f"{cname} run {i}/{n}"
                graders = run.get("graders") or []
                native_total = sum(g.get("weight", 1) for g in graders) or 1.0

                grader_threw = any(
                    str(g.get("explanation", "")).startswith("grader threw")
                    for g in graders
                )
                errored = bool(run.get("error")) or grader_threw
                refused = any(
                    g.get("name") == "no_refusal" and g.get("passed") is False
                    for g in graders
                ) and not grader_threw

                trace_path = run.get("trace_path")
                if trace_path:
                    root = scaffold_root(trace_path)
                    if root is not None:
                        scaffold_roots.add(root)
                mkt_ok = inst_ok = False
                if not errored:
                    if not trace_path or not Path(trace_path).exists():
                        errors += 1
                        failures.append(f"{rid}: trace missing — success unverifiable")
                        adjusted_scores.append(0.0)
                        continue
                    try:
                        mkt_ok, inst_ok = paired_success(trace_path)
                    except (OSError, ValueError) as exc:
                        errors += 1
                        failures.append(f"{rid}: trace unreadable ({exc})")
                        adjusted_scores.append(0.0)
                        continue

                adjusted = (
                    float(run.get("score", 0.0)) * native_total
                    + MKT_WEIGHT * mkt_ok
                    + INST_WEIGHT * inst_ok
                ) / (native_total + MKT_WEIGHT + INST_WEIGHT)
                adjusted_scores.append(adjusted)

                if errored:
                    errors += 1
                    failures.append(f"{rid}: errored ({run.get('error') or 'grader threw'})")
                elif refused:
                    refusals += 1
                    failures.append(f"{rid}: HARD REFUSAL (no_refusal grader failed)")
                elif adjusted >= CLEAN_BAR:
                    clean += 1

            mean = sum(adjusted_scores) / len(adjusted_scores) if adjusted_scores else 0.0
            if adjusted_scores and mean < args.threshold:
                failures.append(
                    f"{cname}: adjusted mean {mean:.3f} < threshold {args.threshold}"
                )
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
            "model_arm": meta.get("model_arm", "cli-default"),
            "marketplace_source": meta.get("marketplace_source"),
            "claude_version": agg.get("claude_version"),
            "passed": not failures,
            "failures": failures,
            "cases": verdict_cases,
        }
        out_path = agg_path.parent / "compliance-verdict.json"
        out_path.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")

        print()
        print(f"Install compliance verdict  (model arm: {verdict['model_arm']}, "
              f"marketplace: {verdict['marketplace_source'] or '?'}, "
              f"threshold: {args.threshold})")
        print(f"{'CASE':<22}{'CLEAN':>8}{'ADJ MEAN':>10}{'REFUSALS':>10}{'ERRORS':>8}")
        for c in verdict_cases:
            print(f"{c['name']:<22}{str(c['clean']) + '/' + str(c['runs']):>8}"
                  f"{c['adjusted_mean']:>10.3f}{c['refusals']:>10}{c['errors']:>8}")
        if failures:
            print("\nFAIL:")
            for f in failures:
                print(f"  - {f}")
        else:
            print("\nPASS — every case cleared the bar with zero refusals and zero errors.")
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
