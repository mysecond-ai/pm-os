---
name: mysecond
description: Connect this machine to your mySecond team workspace (device login), or show connection status if already connected. Use when the user runs /mysecond, asks to log in to mySecond, connect their mySecond workspace, reconnect, or fix a mySecond sync that says it is not authenticated.
---

# /mysecond — connect this machine to your mySecond workspace

You (Claude) run this flow with bash + curl. No other tools or runtimes are required.

**Security rules — absolute:**
- NEVER print, echo, log, or quote the `access_token` (or any part of it) in the terminal or in chat. It moves from the HTTP response file straight into the credentials file and nowhere else.
- NEVER put the user's code or token into a URL.
- The only secrets file you write is `~/.mysecond/credentials`, mode 0600.

API base: `https://app.mysecond.ai` (override with `$COMPANION_API_URL` only if the user explicitly asks to target a non-production environment).

## Step 0 — already connected?

If `~/.mysecond/credentials` exists, check it before starting a new login:

```bash
BASE="${COMPANION_API_URL:-https://app.mysecond.ai}"
mkdir -p -m 700 ~/.mysecond/tmp
if [ -f ~/.mysecond/credentials ]; then
  TOKEN_LINE=$(grep -m1 '^COMPANION_API_KEY=' ~/.mysecond/credentials || head -n1 ~/.mysecond/credentials)
  curl -sS -o ~/.mysecond/tmp/whoami.json -w '%{http_code}' \
    -H "Authorization: Bearer ${TOKEN_LINE#COMPANION_API_KEY=}" \
    "$BASE/api/companion/whoami"
fi
```

- HTTP 200 → read `email` and `team_slug` from `~/.mysecond/tmp/whoami.json` and report: "Connected to **<team_slug>** as <email>." Then stop — no login needed. Suggest `/welcome` if they haven't used it yet.
- HTTP 401 → the stored token is dead. Continue to Step 1 (fresh login).
- HTTP 403 with `"error":"subscription_required"` → say exactly: "Start your trial at https://app.mysecond.ai/activate — then run /mysecond again." (Use the `activate_url` field from the response if present.) Stop.
- No credentials file → continue to Step 1.

## Step 1 — mint a device code

```bash
BASE="${COMPANION_API_URL:-https://app.mysecond.ai}"
mkdir -p -m 700 ~/.mysecond/tmp
HTTP=$(curl -sS -o ~/.mysecond/tmp/code.json -w '%{http_code}' -X POST "$BASE/api/companion/device/code")
echo "$HTTP"; cat ~/.mysecond/tmp/code.json
```

(The mint response contains no secrets the user must not see — `device_code` is only useful from this machine within its expiry window — but do not paste it into chat; show only `user_code` and `verification_uri`.)

- 200 → the JSON has: `user_code` (e.g. `WXYZ-1234` — for the human), `device_code` (for polling), `verification_uri` (`$BASE/device`), `verification_uri_complete` (identical — the code is deliberately never embedded in the URL), `expires_in` (seconds the code lives), `interval` (seconds between polls).
- 429 → body has `retry_after_seconds`. Wait that long, then retry once.

## Step 2 — show the code and open the browser

Present the code to the user prominently, e.g.:

> **Your code is `WXYZ-1234`**
> I've opened <verification_uri> in your browser. Enter the code there and click **Approve**.
> (You'll be asked to sign in to your mySecond account first if you aren't already.)

Open the browser (pick what exists on this OS):

```bash
open "$URI" 2>/dev/null || xdg-open "$URI" 2>/dev/null || start "$URI" 2>/dev/null || echo "Open this URL yourself: $URI"
```

## Step 3 — poll for the token

Poll `POST $BASE/api/companion/device/token` with body `{"device_code":"<device_code>"}` every `interval` seconds (default 5). Run the loop in bounded chunks so no single bash call runs longer than ~90 seconds — then re-run the chunk until a terminal state or the code's `expires_in` window has fully elapsed.

```bash
BASE="${COMPANION_API_URL:-https://app.mysecond.ai}"
DC=$(sed -n 's/.*"device_code":"\([^"]*\)".*/\1/p' ~/.mysecond/tmp/code.json)
INTERVAL=$(sed -n 's/.*"interval":\([0-9]*\).*/\1/p' ~/.mysecond/tmp/code.json); INTERVAL=${INTERVAL:-5}
DEADLINE=$(( $(date +%s) + 85 ))
STATUS=pending
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  HTTP=$(curl -sS -o ~/.mysecond/tmp/token.json -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"device_code\":\"$DC\"}" \
    "$BASE/api/companion/device/token")
  if [ "$HTTP" = "200" ]; then STATUS=ok; break; fi
  ERR=$(sed -n 's/.*"error":"\([^"]*\)".*/\1/p' ~/.mysecond/tmp/token.json)
  case "$ERR" in
    authorization_pending) sleep "$INTERVAL" ;;
    rate_limited)
      RA=$(sed -n 's/.*"retry_after_seconds":\([0-9]*\).*/\1/p' ~/.mysecond/tmp/token.json)
      sleep "${RA:-$INTERVAL}" ;;
    expired|already_exchanged|invalid) STATUS="$ERR"; break ;;
    *) STATUS="unexpected:$ERR:http_$HTTP"; break ;;
  esac
done
echo "STATUS=$STATUS"
```

Branch on `STATUS` (these are the server's exact error codes — do not invent others):

- `ok` → go to Step 4. **Do not cat token.json** — it contains the access token.
- `pending` (chunk budget ran out) → tell the user you're still waiting for them to click Approve, and run the chunk again. Keep going until the code's `expires_in` (typically ~15 minutes) has elapsed in total.
- `expired` → the code timed out before approval. Re-mint ONCE (repeat Steps 1–3 with a fresh code). If the second code also expires, stop and tell the user what the browser page most likely showed, because an approval the server refuses leaves the code unapproved:
  - If the page said a subscription is needed: "Start your trial at https://app.mysecond.ai/activate — then run /mysecond again."
  - If the page said no team / account not set up: "Your account isn't part of a workspace yet — finish signup at https://app.mysecond.ai (or accept your team's invite email), then run /mysecond again."
  - Otherwise: ask them to try again when they're ready to click Approve within a few minutes.
- `already_exchanged` → a token was already issued for this code (an earlier poll from this flow won the race). Check `~/.mysecond/credentials`: if it was just written, continue to Step 5; if not, the token from that exchange is lost — restart from Step 1.
- `invalid` → the device_code the server received is unknown (mangled or reaped). Restart from Step 1.

## Step 4 — store the token (0600, never displayed)

Extract the token from the response file directly into the credentials file. The token never touches stdout or chat.

```bash
umask 177
mkdir -p -m 700 ~/.mysecond
sed -n 's/.*"access_token":"\([^"]*\)".*/COMPANION_API_KEY=\1/p' ~/.mysecond/tmp/token.json > ~/.mysecond/credentials
chmod 600 ~/.mysecond/credentials
rm -f ~/.mysecond/tmp/token.json ~/.mysecond/tmp/code.json
# sanity: file is non-empty without revealing contents
[ -s ~/.mysecond/credentials ] && echo "credentials written" || echo "WRITE FAILED"
```

If the write failed, say so and do not proceed; do not attempt to print the token as a fallback.

## Step 5 — confirm with whoami and greet

```bash
BASE="${COMPANION_API_URL:-https://app.mysecond.ai}"
TOKEN_LINE=$(grep -m1 '^COMPANION_API_KEY=' ~/.mysecond/credentials)
HTTP=$(curl -sS -o ~/.mysecond/tmp/whoami.json -w '%{http_code}' \
  -H "Authorization: Bearer ${TOKEN_LINE#COMPANION_API_KEY=}" \
  "$BASE/api/companion/whoami")
echo "$HTTP"; cat ~/.mysecond/tmp/whoami.json; rm -f ~/.mysecond/tmp/whoami.json
```

(The whoami response contains no secrets — it's identity metadata: `email`, `team_id`, `user_id`, `scopes`, `team_slug`, `team_membership_role`, `is_invited_pm`, `workspace_scope`.)

- 200 → greet the user by team: "Connected to **<team_slug>** ✓ as <email>. Your workspace will sync at the start of your next session — run `/welcome` to get started."
- 401 → the token was rejected immediately after issue (clock skew or a revocation). Delete `~/.mysecond/credentials` and restart from Step 1; if it happens twice, tell the user to contact support@mysecond.ai.
- 403 `subscription_required` → "Start your trial at https://app.mysecond.ai/activate — then run /mysecond again." (prefer the response's `activate_url`).
- 400/403 `no_team` (defensive — normally surfaces in the browser, not here) → "Your account isn't part of a workspace yet — finish signup at https://app.mysecond.ai (or accept your team's invite email), then run /mysecond again."

## Notes

- The `interval` and `retry_after_seconds` values come from the server — honor them; do not poll faster.
- This flow is safe to re-run at any time; re-running replaces the stored credential.
- The credential is a 90-day device token that renews on use. If sync ever starts failing with 401s months later, `/mysecond` again is the fix.
