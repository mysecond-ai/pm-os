#!/usr/bin/env bash
# IP allowlist gate — permanent leak prevention (plan-simple-install Part 2b).
#
# This repo is the PUBLIC shell. Product content (skills, agents, workflows)
# is runtime-delivered after /mysecond login and must NEVER be committed here.
# Anything ever pushed to a public repo is published forever (forks, mirrors,
# archives) — so this gate fails the build the moment a disallowed path
# appears, before it can land on a public ref.
#
# Rules:
#   1. The ONLY allowed path under skills/ is skills/mysecond/**.
#   2. No agents/ or workflows/ directory may exist anywhere in the tree.
#   3. No SKILL.md may exist outside skills/mysecond/.
#   4. .claude-plugin/plugin.json must NOT contain a "version" key —
#      version lives in marketplace.json ONLY (a plugin.json version
#      silently takes precedence and breaks release discipline).
#   5. REPO-WIDE: every @mysecond/cli@<ref> occurrence in tracked files must
#      equal the pinned version in .cli-pin — @latest (or any other version)
#      anywhere is a violation. The hooks' npx fallback is PINNED, never
#      @latest (supply-chain: an unpinned fallback means trusting the npm
#      account continuously; agents inspecting the plugin flagged exactly
#      this). Bump with scripts/set-cli-pin.sh <version>.
#      Documented exemptions (and the ONLY ones):
#        - scripts/check-allowlist.sh, scripts/set-cli-pin.sh — the
#          enforcement tooling itself must be able to name the forbidden
#          string.
#      (The former tests/fixtures/** exemption left with the eval suite —
#      now in github.com/mysecond-ai/pm-os-evals; no tests/ dir ships here.)
#
# Runs on tracked files (git ls-files) so it checks exactly what ships.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

fail=0

# Rule 1: skills/ allowlist.
while IFS= read -r f; do
  case "$f" in
    skills/mysecond/*) ;;
    *)
      echo "ALLOWLIST VIOLATION (rule 1): disallowed path under skills/: $f" >&2
      fail=1
      ;;
  esac
done < <(git ls-files 'skills/*')

# Rule 2: no agents/ or workflows/ directories anywhere.
# (.github/workflows/ is GitHub Actions CI, not plugin content — exempt.)
while IFS= read -r f; do
  case "$f" in
    .github/*) continue ;;
  esac
  case "/$f" in
    */agents/*|*/workflows/*)
      echo "ALLOWLIST VIOLATION (rule 2): agents/ or workflows/ path: $f" >&2
      fail=1
      ;;
  esac
done < <(git ls-files)

# Rule 3: no SKILL.md outside skills/mysecond/.
while IFS= read -r f; do
  case "$f" in
    skills/mysecond/SKILL.md) ;;
    *)
      echo "ALLOWLIST VIOLATION (rule 3): SKILL.md outside skills/mysecond/: $f" >&2
      fail=1
      ;;
  esac
done < <(git ls-files | grep -E '(^|/)SKILL\.md$' || true)

# Rule 4: plugin.json must not declare a version.
if [ -f .claude-plugin/plugin.json ]; then
  if command -v jq >/dev/null 2>&1; then
    if jq -e 'has("version")' .claude-plugin/plugin.json >/dev/null; then
      echo "ALLOWLIST VIOLATION (rule 4): .claude-plugin/plugin.json declares a \"version\" key. Version lives in marketplace.json ONLY." >&2
      fail=1
    fi
  else
    if grep -q '"version"' .claude-plugin/plugin.json; then
      echo "ALLOWLIST VIOLATION (rule 4): .claude-plugin/plugin.json contains \"version\". Version lives in marketplace.json ONLY." >&2
      fail=1
    fi
  fi
else
  echo "ALLOWLIST VIOLATION: .claude-plugin/plugin.json missing" >&2
  fail=1
fi

# Rule 5: repo-wide CLI version pin consistency (single source: .cli-pin).
# Scope: EVERY tracked file except the documented exemptions in the header.
if [ -f .cli-pin ]; then
  PIN="$(tr -d '[:space:]' < .cli-pin)"
  if [ -z "$PIN" ]; then
    echo "ALLOWLIST VIOLATION (rule 5): .cli-pin is empty" >&2
    fail=1
  fi
  while IFS= read -r f; do
    case "$f" in
      scripts/check-allowlist.sh|scripts/set-cli-pin.sh) continue ;;  # enforcement tooling
    esac
    while IFS= read -r ref; do
      [ -z "$ref" ] && continue
      ver="${ref#@mysecond/cli@}"
      if [ "$ver" != "$PIN" ]; then
        echo "ALLOWLIST VIOLATION (rule 5): $f references @mysecond/cli@$ver but .cli-pin says $PIN (use scripts/set-cli-pin.sh)" >&2
        fail=1
      fi
    done < <(grep -ho '@mysecond/cli@[0-9A-Za-z][0-9A-Za-z.-]*' "$f" 2>/dev/null | sort -u)
  done < <(git ls-files)
  if ! grep -q "@mysecond/cli@$PIN" .claude-plugin/plugin.json; then
    echo "ALLOWLIST VIOLATION (rule 5): plugin.json has no @mysecond/cli@$PIN fallback — hooks must pin the CLI" >&2
    fail=1
  fi
else
  echo "ALLOWLIST VIOLATION (rule 5): .cli-pin missing — the CLI fallback version must have a single source" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "" >&2
  echo "check-allowlist.sh FAILED — see violations above." >&2
  exit 1
fi

echo "check-allowlist.sh OK — shell repo contains only allowed paths."
