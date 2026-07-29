#!/usr/bin/env bash
# One-command CLI pin bump: updates .cli-pin (the single source) and rewrites
# every @mysecond/cli@<version> reference (plugin.json hooks + README) to
# match. check-allowlist.sh rule 5 fails CI if they ever drift.
#
#   scripts/set-cli-pin.sh 1.12.1
#
# The hooks' npx fallback is PINNED by policy — never @latest. Rationale
# (from real installing-agent reviews, 2026-07-29): an unpinned fallback
# means trusting the npm account continuously rather than the audited
# version; plugin marketplace updates propagate pin bumps fine.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

NEW="${1:-}"
case "$NEW" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "usage: scripts/set-cli-pin.sh <semver, e.g. 1.12.1>" >&2; exit 2 ;;
esac

printf '%s\n' "$NEW" > .cli-pin
export NEW
perl -pi -e 's#\@mysecond/cli\@[0-9][0-9A-Za-z.-]*#\@mysecond/cli\@$ENV{NEW}#g' \
  .claude-plugin/plugin.json README.md

bash scripts/check-allowlist.sh
echo "CLI pin set to $NEW (plugin.json + README + .cli-pin in sync)."
