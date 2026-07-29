#!/usr/bin/env bash
# Install compliance-eval harness — plan-simple-install §3.9 flip gate.
#
# Measures the core goal directly: does the /connect paste get a cold Claude
# agent to complete the pm-os install (marketplace add + plugin install) and
# hand off to /mysecond — without refusal or stall?
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
#                        slug. Use for the flip-gate scoring run once the repo
#                        is reachable (public, or a machine with git access).
#
# Other knobs (env): RUNS (default 6 — the prior-art n), MODEL (default: your
# Claude Code default; sweep e.g. MODEL=opus for a high-reasoning arm),
# CASE_GLOB (filter, e.g. paste-wary-user), THRESHOLD (default 0.85), JSON=1
# (emit aggregate JSON to stdout for CI).
#
# Isolation: `claude plugin eval` scaffolds a fresh CLAUDE_CONFIG_DIR + HOME +
# cwd per run and deletes them afterward (verified on 2.1.207) — the nested
# `claude plugin marketplace add` / `claude plugin install` the agent executes
# land in that scratch config, never in your user-scope ~/.claude.
#
# Auth: the eval spawns real agent sessions, so `claude` must be logged in in
# THIS shell (nested/proxied sessions may fail OAuth refresh — run from a
# normal terminal). CI: provide ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN.
#
# NOTE: `claude plugin eval` is early-access on 2.1.207, gated behind
# CLAUDE_CODE_WALNUT_SPIRE=1 (set below). When the command GAs, drop the var.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROD_SLUG="mysecond-ai/pm-os"

export MARKETPLACE_SOURCE="${MARKETPLACE_SOURCE:-$REPO_ROOT}"
RUNS="${RUNS:-6}"
MODEL="${MODEL:-}"
CASE_GLOB="${CASE_GLOB:-}"
THRESHOLD="${THRESHOLD:-0.85}"
JSON="${JSON:-}"

command -v claude >/dev/null 2>&1 || { echo "ERROR: claude CLI not found on PATH" >&2; exit 2; }

if [ "$MARKETPLACE_SOURCE" != "$PROD_SLUG" ] && [ ! -f "$MARKETPLACE_SOURCE/.claude-plugin/marketplace.json" ]; then
  echo "ERROR: MARKETPLACE_SOURCE=$MARKETPLACE_SOURCE has no .claude-plugin/marketplace.json" >&2
  exit 2
fi

# --- Auth preflight: one cheap call so 18 agent runs don't burn on a dead login.
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
WORK="$(mktemp -d -t pm-os-install-eval)"
trap 'rm -rf "$WORK"' EXIT
cp -R "$REPO_ROOT/evals" "$WORK/evals"
rm -rf "$WORK/evals/results"

if [ "$MARKETPLACE_SOURCE" != "$PROD_SLUG" ]; then
  echo "Mode: LOCAL marketplace source ($MARKETPLACE_SOURCE) — hermetic pre-flip run."
  echo "      Flip-gate scoring must also pass with MARKETPLACE_SOURCE=$PROD_SLUG once reachable."
  find "$WORK/evals" -name case.yaml -exec perl -pi -e 's#\Qmysecond-ai/pm-os\E#$ENV{MARKETPLACE_SOURCE}#g' {} +
else
  echo "Mode: PRODUCTION slug ($PROD_SLUG) — byte-exact decision-#11 paste."
fi

RESULTS_DIR="$REPO_ROOT/evals/results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS_DIR"

CMD=(claude plugin eval --runs "$RUNS" --threshold "$THRESHOLD" --allow-tools Bash WebFetch WebSearch --output-dir "$RESULTS_DIR")
[ -n "$MODEL" ] && CMD+=(--model "$MODEL")
[ -n "$CASE_GLOB" ] && CMD+=(--case "$CASE_GLOB")
[ -n "$JSON" ] && CMD+=(--json)

echo "Cases: $(find "$WORK/evals" -name case.yaml | wc -l | tr -d ' ')  Runs/case: $RUNS  Threshold: $THRESHOLD"
echo "Results -> $RESULTS_DIR"
cd "$WORK"
STATUS=0
CLAUDE_CODE_WALNUT_SPIRE=1 "${CMD[@]}" || STATUS=$?
echo "Aggregate results: $RESULTS_DIR/aggregate-result.json"
exit $STATUS
