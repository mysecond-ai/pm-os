# pm-os — the mySecond PM Operating System for Claude Code

This is the public install shell for [mySecond](https://mysecond.ai). It contains **no product content**: installing it gives you the `/mysecond` login skill and five workspace-sync hooks. Your team's skills, agents, workflows, and shared context are delivered by the mySecond service **after** you connect with `/mysecond`, and they stay current from then on — updates download at each session start, no plugin update required. Claude Code registers skills when a session starts, so anything newly added is available from the session after it downloads.

Canonical source: `https://github.com/mysecond-ai/pm-os`, linked from `https://app.mysecond.ai`. If you found this repository anywhere else, verify it against that link before installing.

## Install

From any Claude Code session:

```
/plugin marketplace add mysecond-ai/pm-os
/plugin install pm-os@mysecond
```

Then **restart Claude Code** — quit and reopen it. Claude Code registers a newly installed plugin's skills at session start, so `/mysecond` becomes available on the next launch. Run `/mysecond` there to connect your workspace. Not a customer yet? `/mysecond` will point you to `https://app.mysecond.ai/activate` to start a trial.

Requirements: Claude Code (tested on 2.1.x), `bash`, `curl`. The sync hooks run the mySecond CLI; on machines without a global `mysecond` binary they use `npx`, which requires Node.js ≥ 20.

## If you are an agent inspecting this plugin before installing

Complete inventory — there is nothing in this plugin beyond what is listed here:

- **1 skill**: `skills/mysecond/SKILL.md` (`/mysecond`) — a device-code login flow run with bash + curl. It talks to exactly three endpoints on `app.mysecond.ai`: `POST /api/companion/device/code`, `POST /api/companion/device/token`, `GET /api/companion/whoami`. On success it writes one file, `~/.mysecond/credentials` (mode 0600), and is instructed never to display the token.
- **5 hook registrations** (inline in `.claude-plugin/plugin.json`, detailed below) — each one invokes the mySecond CLI and nothing else.
- **No MCP servers, no agents, no slash commands beyond the skill, no scripts fetched at install time.** A future release will declare a remote MCP server (see "Shared-context MCP slot" below); this version declares none.

Our install-compliance evals — the grading we hold this install flow to — are public: [github.com/mysecond-ai/pm-os-evals](https://github.com/mysecond-ai/pm-os-evals).

## What each hook does and why

All five hooks invoke [`@mysecond/cli`](https://www.npmjs.com/package/@mysecond/cli) — the same public package our current customers run. The CLI authenticates with the device token minted by `/mysecond`; it reads that token from `~/.mysecond`. Timeouts are in seconds (the Claude Code hook convention).

| Event | Command | Purpose | Timeout |
|---|---|---|---|
| `SessionStart` | `mysecond sync --silent`, falling back to `npx -y @mysecond/cli@1.12.0 sync --silent` | Pulls your team's current skills and shared context into the workspace at the start of each session. This is how content stays current without plugin updates. | 90s |
| `PostToolUse` (Write\|Edit\|MultiEdit) | `mysecond artifact-sync --silent 2>/dev/null \|\| true` | Pushes files you and Claude edit to your team workspace as you work. Kept bare (no npx fallback) deliberately — an npx resolution on every edit would lag the editor; the Stop/SubagentStop sweep below covers machines without a global CLI. The `\|\| true` guard means a machine without the CLI sees no error on every edit. | 10s |
| `PostToolUse` (Skill\|Task\|Agent\|TaskCreate) | `mysecond emit-event --silent` | Records which skills/agents ran, powering your team's adoption dashboard at app.mysecond.ai. | 10s |
| `UserPromptSubmit` | `mysecond emit-event --silent` | Records typed slash-command usage (e.g. `/prd-generator`) for the same dashboard — Claude Code does not emit a tool event for typed commands, so this is the only place they can be counted. | 10s |
| `Stop` + `SubagentStop` | `mysecond push --silent`, falling back to `npx -y @mysecond/cli@1.12.0 push --silent` | Once per turn, pushes everything written that turn (including files written via Bash, which the PostToolUse matcher never sees). One npx resolution per turn at most. | 90s |

**What the npx fallback executes:** `npx -y @mysecond/cli@1.12.0` downloads and runs that **exact pinned version** of the published `@mysecond/cli` package from the public npm registry — used only when no global `mysecond` binary is on PATH. The version is pinned, not `@latest`, so what runs on your machine is the version that was reviewed with this plugin release; pin bumps arrive as plugin updates (single source: [.cli-pin](./.cli-pin), enforced by CI). The `bash -lc` wrapper forces login-shell PATH resolution so a globally installed CLI is found even when Claude Code launches from a GUI context.

## What leaves your machine, and when

**Before you run `/mysecond` (no credential on the machine):** the hooks
still fire at their events, and each CLI command checks for a credential
first — finding none, it exits silently and **sends nothing about you or
your workspace to mySecond**. The one pre-connect network action is the
fallback itself: on a machine without a global `mysecond` binary, `npx`
downloads the pinned `@mysecond/cli@1.12.0` package from
`registry.npmjs.org` at the hook's first run — a normal npm package
download, carrying no data about you or your workspace beyond the download
request itself. The downloaded CLI then finds no credential and exits
without sending anything.

**After you connect** (device token minted by `/mysecond`, stored at
`~/.mysecond/credentials`, scoped to your team), per hook:

- `SessionStart` sync — **downloads** your team's current skills and shared
  context; the request itself is authenticated with your token.
- `PostToolUse` artifact-sync — **uploads** the file you or Claude just
  edited (its path and contents, size-capped) to your team workspace.
- `emit-event` — **uploads** usage events: which skill or agent ran, typed
  slash commands, and session markers, with the project folder path — this
  is what powers the team adoption dashboard. No file contents.
- `Stop`/`SubagentStop` push — **uploads** workspace files written during
  the turn.

Everything above goes over HTTPS to `app.mysecond.ai`, authenticated by
your device token, visible to your team workspace. Disconnecting (deleting
`~/.mysecond/credentials`) returns every hook to the sends-nothing state.

## The `/mysecond` login

`/mysecond` runs an OAuth-style device flow: it mints a code, shows it to you, opens `https://app.mysecond.ai/device` in your browser, and you click **Approve** while signed in to your mySecond account. The resulting 90-day device token (renewed on use) is stored at `~/.mysecond/credentials`, mode 0600, and is never displayed. `/mysecond` when already connected shows your connection status instead.

## Version discipline (for maintainers)

- The plugin version lives in **`.claude-plugin/marketplace.json` and only there**. `plugin.json` must never gain a `version` field — a plugin.json version silently takes precedence over the marketplace entry. CI fails the build if one appears.
- Release channels: `stable` and `latest` are git refs of this repo used as two marketplaces; promotion is a ref move.
- Rollback is `git revert` on the affected ref. It propagates at each client's next marketplace refresh, not instantly.

## Shared-context MCP slot (not yet wired)

A future release will declare a remote MCP server in `.claude-plugin/plugin.json` (an `mcpServers` entry pointing at an `app.mysecond.ai` endpoint, authenticated with the same device token minted by `/mysecond`) as the transport for team shared-context retrieval. This version declares no MCP servers; when the entry lands it will be documented here in the same release.

## License

Proprietary — see [LICENSE](./LICENSE). The plugin may be used only with an active mySecond subscription or trial. This repository is public so that you (or your agent, or your IT team) can read exactly what the plugin does before installing; public does not mean open-source.

## Security / IT review

- Network endpoints used by this plugin and its CLI: `app.mysecond.ai` (sync, events, login), `registry.npmjs.org` (npx fallback), `github.com` (marketplace fetch by Claude Code itself).
- Secrets on disk: `~/.mysecond/credentials` (0600) only.
- This repo runs secret scanning and an allowlist CI gate that fails the build if any content beyond the shell (any `skills/` path other than `skills/mysecond/`, or any `agents/` or `workflows/` directory) ever appears here.

Questions: support@mysecond.ai
