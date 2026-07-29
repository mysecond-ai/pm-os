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
#   5. Every @mysecond/cli@<version> reference (plugin.json hooks, README)
#      must equal the pinned version in .cli-pin — the hooks' npx fallback
#      is PINNED, never @latest (supply-chain: an unpinned fallback means
#      trusting the npm account continuously; agents inspecting the plugin
#      flagged exactly this). Bump with scripts/set-cli-pin.sh <version>.
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

# Rule 5: CLI version pin consistency (single source: .cli-pin).
if [ -f .cli-pin ]; then
  PIN="$(tr -d '[:space:]' < .cli-pin)"
  if [ -z "$PIN" ]; then
    echo "ALLOWLIST VIOLATION (rule 5): .cli-pin is empty" >&2
    fail=1
  fi
  while IFS= read -r ref; do
    ver="${ref#@mysecond/cli@}"
    if [ "$ver" != "$PIN" ]; then
      echo "ALLOWLIST VIOLATION (rule 5): found @mysecond/cli@$ver but .cli-pin says $PIN (use scripts/set-cli-pin.sh)" >&2
      fail=1
    fi
  done < <(git ls-files -z '*.json' '*.md' | xargs -0 grep -ho '@mysecond/cli@[0-9][0-9A-Za-z.-]*' 2>/dev/null | sort -u)
  if ! grep -q "@mysecond/cli@$PIN" .claude-plugin/plugin.json; then
    echo "ALLOWLIST VIOLATION (rule 5): plugin.json has no @mysecond/cli@$PIN fallback — hooks must pin the CLI" >&2
    fail=1
  fi
  if grep -q '@mysecond/cli@latest' .claude-plugin/plugin.json README.md 2>/dev/null; then
    echo "ALLOWLIST VIOLATION (rule 5): @mysecond/cli@latest found — the fallback must be pinned" >&2
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
