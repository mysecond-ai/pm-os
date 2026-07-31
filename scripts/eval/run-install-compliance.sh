#!/usr/bin/env bash
# Install compliance-eval harness — plan-simple-install §3.9 flip gate.
#
# Measures the core goal directly: does the /connect paste get a cold Claude
# agent to complete the pm-os install (marketplace add + plugin install) and
# hand off to /mysecond — without refusal or stall? The wary case
# (paste-wary-user) is scored under RUBRIC V3: its user asked "is this
# safe?", so verification reported accurately that ends one explicit user
# confirmation away from an agent-executed install passes alongside a
# completed install — see evals/install-compliance/README.md.
#
# One command:
#   scripts/eval/run-install-compliance.sh
#
# Modes (MARKETPLACE_SOURCE):
#   default            = this checkout's absolute path. The paste's canonical
#                        `mysecond-ai/pm-os` slug is substituted with the local
#                        path so the eval runs hermetically while the GitHub
#                        repo is private (same technique as the Track B smoke).
#   mysecond-ai/pm-os  = byte-exact production paste against the real GitHub
#                        slug. Use for the flip-day scoring run once the repo
#                        is reachable (public, or a machine with git access).
#
# Other knobs (env): RUNS (default 6 — the prior-art n), MODEL (default: your
# Claude Code default; the flip-qualifying bar ALSO requires a high-reasoning
# arm, e.g. MODEL=opus — see evals/install-compliance/README.md), CASE_GLOB
# (filter, e.g. paste-wary-user), THRESHOLD (default 0.85, enforced by the
# post-processor over ADJUSTED scores), KEEP_TEMP=1 (preserve per-run scaffold
# dirs for debugging), JSON=1 (also emit the native aggregate JSON to stdout).
#
# Scoring: `claude plugin eval` produces the native per-run scores; then
# scripts/eval/postprocess-results.py (a) grades install SUCCESS for the two
# strict cases from Bash tool RESULTS paired with a strict pinned-grammar
# invocation in the same call (safelisted env prefixes, marketplace source
# pinned to this run's staged source, install target pinned, anchored
# success lines; the wary case is judge-scored with a deterministic
# consistency gate instead), and
# (b) machine-enforces the hard gates: ANY run failing the binary
# `no_refusal` grader, ANY errored run, or ANY case adjusted-mean below
# THRESHOLD fails the harness. The post-processor's exit code is the
# verdict AND this script's exit code: 0 = flip-qualifying pass; 2 = all
# gates passed but CASE_GLOB-partial (completed, NOT flip-qualifying — CI
# treats non-zero as red); 1 = failed.
#
# Isolation: `claude plugin eval` scaffolds a fresh CLAUDE_CONFIG_DIR + HOME +
# cwd per run (verified on 2.1.207) — the nested `claude plugin marketplace
# add` / `claude plugin install` the agent executes land in that scratch
# config, so user-scope plugin state (your real ~/.claude marketplaces and
# plugins) is never touched. We run with --keep-temp so the post-processor
# can read each run's trace, then it deletes the scaffold dirs itself
# (KEEP_TEMP=1 preserves them).
#
# Auth — stated exactly: the AUTH PREFLIGHT below runs one 1-turn `claude -p`
# under your normal login (your regular user config, like any `claude`
# invocation; it does not read or modify plugin state). The eval agent
# sessions also authenticate with your existing Claude login, but execute
# inside the per-run scratch scaffolds above. Nested/proxied sessions may
# fail OAuth refresh — run from a normal terminal. CI: provide
# ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN (dispatch-only; see workflow).
#
# NOTE: `claude plugin eval` is early-access on 2.1.207, gated behind
# CLAUDE_CODE_WALNUT_SPIRE=1 (set below). When the command GAs, drop the var.
# If a future CLI renames the gate, this script fails loudly at the eval call.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROD_SLUG="mysecond-ai/pm-os"

export MARKETPLACE_SOURCE="${MARKETPLACE_SOURCE:-$REPO_ROOT}"
RUNS="${RUNS:-6}"
MODEL="${MODEL:-}"
CASE_GLOB="${CASE_GLOB:-}"
THRESHOLD="${THRESHOLD:-0.85}"
KEEP_TEMP="${KEEP_TEMP:-}"
JSON="${JSON:-}"

command -v claude >/dev/null 2>&1 || { echo "ERROR: claude CLI not found on PATH" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found on PATH" >&2; exit 2; }

if [ "$MARKETPLACE_SOURCE" != "$PROD_SLUG" ] && [ ! -f "$MARKETPLACE_SOURCE/.claude-plugin/marketplace.json" ]; then
  echo "ERROR: MARKETPLACE_SOURCE=$MARKETPLACE_SOURCE has no .claude-plugin/marketplace.json" >&2
  exit 2
fi

# --- Auth preflight: one cheap 1-turn call under your normal login, so 18
# --- agent runs don't burn on a dead login. (Uses your user config; plugin
# --- state untouched. The eval runs themselves are isolated — see header.)
PREFLIGHT="$(claude -p "Reply with exactly: OK" --model haiku --max-turns 1 2>&1 || true)"
case "$PREFLIGHT" in
  *"Not logged in"*|*"Failed to authenticate"*|*"OAuth"*)
    echo "ERROR: claude is not usable headlessly in this shell:" >&2
    echo "  $PREFLIGHT" >&2
    echo "Run from a terminal where 'claude -p hi' works (nested sessions may fail OAuth refresh)." >&2
    exit 3
    ;;
esac

# --- Stage a working copy of the eval suite; substitute the marketplace source.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/pm-os-work.XXXXXX")"
MP_ROOT=""
trap 'rm -rf "$WORK"; if [ -n "${MP_ROOT:-}" ]; then rm -rf "$MP_ROOT"; fi' EXIT
cp -R "$REPO_ROOT/evals" "$WORK/evals"
rm -rf "$WORK/evals/results"

# --- De-contaminated marketplace staging (adopted 2026-07-29): agents under
# eval READ the marketplace source. Staging the checkout verbatim let them
# read this very eval suite — their own prompt and rubric — which measurably
# skewed wary-case behavior in the first scoring run (5/6 wary runs commented
# on it). Default local mode therefore stages a copy WITHOUT evals/, tests/,
# and .git (keeping .git would either carry the suite in history or show it
# as deletions in `git status` — a worse artifact), in its OWN temp root,
# SEPARATE from $WORK: if the marketplace lived next to the staged eval
# suite, an inspecting agent walking dirname(marketplace) would find the
# rubric anyway (codex finding, pm-os#2). Its parent is the shared system
# temp dir, which holds no eval content of ours by name-walkable adjacency.
# evals/ deliberately stays in the PUBLIC repo (transparency asset);
# slug-mode runs measure that full reality. An explicit non-default
# MARKETPLACE_SOURCE is used as-is.
if [ "$MARKETPLACE_SOURCE" = "$REPO_ROOT" ]; then
  MP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pm-os-marketplace.XXXXXX")"
  cp -R "$REPO_ROOT/." "$MP_ROOT/"
  rm -rf "$MP_ROOT/.git" "$MP_ROOT/evals" "$MP_ROOT/tests" "$MP_ROOT/.memory"
  [ -f "$MP_ROOT/.claude-plugin/marketplace.json" ] || { echo "ERROR: de-contaminated staging lost .claude-plugin — aborting" >&2; exit 4; }
  MARKETPLACE_SOURCE="$MP_ROOT"
  export MARKETPLACE_SOURCE
  echo "Marketplace staged de-contaminated (no evals/, tests/, .git; separate temp root): $MP_ROOT"
fi

if [ "$MARKETPLACE_SOURCE" != "$PROD_SLUG" ]; then
  echo "Mode: LOCAL marketplace source ($MARKETPLACE_SOURCE) — hermetic pre-flip run."
  echo "      The flip-day scoring run must also pass with MARKETPLACE_SOURCE=$PROD_SLUG once reachable."
  find "$WORK/evals" -name case.yaml -exec perl -pi -e 's#\Qmysecond-ai/pm-os\E#$ENV{MARKETPLACE_SOURCE}#g' {} +
else
  echo "Mode: PRODUCTION slug ($PROD_SLUG) — byte-exact decision-#11 paste."
fi

RESULTS_DIR="$REPO_ROOT/evals/results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS_DIR"

# Record the arm AND the completeness contract (staged case names + runs per
# case) — the post-processor validates the aggregate against this metadata
# fail-closed: a missing case or missing run can never pass.
STAGED_CASES="$(find "$WORK/evals" -name case.yaml -exec perl -ne 'print "$1\n" if /^name:\s*"?([\w-]+)"?\s*$/' {} + | sort | paste -sd, -)"
if [ -z "$STAGED_CASES" ]; then
  echo "ERROR: no case names found in staged evals — refusing to run" >&2
  exit 2
fi
export META_MODEL="${MODEL:-cli-default}" META_RUNS="$RUNS" META_THRESHOLD="$THRESHOLD" META_CASES="$STAGED_CASES" META_CASE_GLOB="$CASE_GLOB"
python3 - "$RESULTS_DIR/run-metadata.json" <<'PYEOF'
import json, os, subprocess, sys
meta = {
    "model_arm": os.environ["META_MODEL"],
    "marketplace_source": os.environ["MARKETPLACE_SOURCE"],
    "runs_per_case": int(os.environ["META_RUNS"]),
    "threshold": os.environ["META_THRESHOLD"],
    "staged_cases": os.environ["META_CASES"].split(","),
    "case_glob": os.environ["META_CASE_GLOB"],
    "claude_version": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip(),
}
with open(sys.argv[1], "w") as fh:
    json.dump(meta, fh, indent=2)
PYEOF

# Native threshold is disabled (0): the post-processor owns the bar, because
# it grades over ADJUSTED scores (paired install success) and enforces the
# zero-hard-refusal gate that a mean can hide.
CMD=(claude plugin eval --runs "$RUNS" --threshold 0 --keep-temp --allow-tools Bash WebFetch WebSearch --output-dir "$RESULTS_DIR")
[ -n "$MODEL" ] && CMD+=(--model "$MODEL")
[ -n "$CASE_GLOB" ] && CMD+=(--case "$CASE_GLOB")
[ -n "$JSON" ] && CMD+=(--json)

echo "Cases: $(find "$WORK/evals" -name case.yaml | wc -l | tr -d ' ')  Runs/case: $RUNS  Model arm: ${MODEL:-cli-default}"
echo "Results -> $RESULTS_DIR"
cd "$WORK"
EVAL_STATUS=0
CLAUDE_CODE_WALNUT_SPIRE=1 "${CMD[@]}" || EVAL_STATUS=$?
if [ ! -f "$RESULTS_DIR/aggregate-result.json" ]; then
  echo "ERROR: eval produced no aggregate-result.json (exit $EVAL_STATUS) — likely the early-access gate or a CLI change." >&2
  [ "$EVAL_STATUS" -eq 0 ] && EVAL_STATUS=4
  exit "$EVAL_STATUS"
fi

# --- Verdict: paired-success grading + hard gates. Exit code = verdict.
POST_ARGS=("$RESULTS_DIR/aggregate-result.json" --threshold "$THRESHOLD")
[ -n "$KEEP_TEMP" ] && POST_ARGS+=(--keep-temps)
python3 "$REPO_ROOT/scripts/eval/postprocess-results.py" "${POST_ARGS[@]}"
