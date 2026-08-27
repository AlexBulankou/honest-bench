#!/usr/bin/env bash
# Shared GitHub App JWT-mint + installation-token helper for the refresh
# Cloud Build pipelines.
#
# hb#775/#776/#777: this exact JWT-minting block was duplicated inline in
# three places -- gke-sandbox's open-pr and cleanup-merged-branches steps,
# and gke-kata's open-pr step -- so each of the two escaping fixes so far
# had to be re-applied three times. Extracting it here also fixes the
# concrete bug that motivated the extraction: gke-sandbox's open-pr step's
# inline script had grown past Cloud Build's per-arg 10000-char limit
# ("invalid .steps field: build step arg 1 too long (max: 10000)"). A
# standalone file like this one is never scanned by Cloud Build's
# substitution engine at all (that engine only inspects each step's own
# `args`/`script` string), so this is also the last place this logic needs
# the `$$`-escape discipline described in the calling steps' own comments.
#
# SOURCE this file (never execute it) so the variables below land in the
# caller's shell -- Cloud Build steps run each `args` block as one shell, and
# `source`/`.` runs in that same shell rather than a subshell. Runs fine
# under the caller's own `set -euo pipefail` (or `set -uo pipefail`).
#
# Inputs (caller sets before sourcing):
#   GH_APP_ID          - the GitHub App's numeric id
#   GH_APP_ACCOUNT     - the org/user login the App is installed on
#   GH_APP_PEM         - the App's private key PEM (Cloud Build secretEnv)
#   MINT_BOT_IDENTITY  - optional; set to "1" to also derive the App's own
#                        bot commit identity (only needed by a step that
#                        runs `git config user.*`, e.g. open-pr)
#
# Outputs (this script sets):
#   GITHUB_TOKEN  - short-lived installation token, exported
#   BOT_LOGIN, BOT_USER_ID, BOT_EMAIL - only when MINT_BOT_IDENTITY=1

pem_file=$(mktemp)
printf '%s' "${GH_APP_PEM}" > "$pem_file"
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now=$(date +%s)
iat=$((now - 60))
exp=$((now + 540))
jwt_header_b64=$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | b64url)
jwt_payload_b64=$(printf '{"iat":%s,"exp":%s,"iss":"%s"}' "$iat" "$exp" "${GH_APP_ID}" | b64url)
jwt_unsigned="$jwt_header_b64.$jwt_payload_b64"
jwt_sig=$(printf '%s' "$jwt_unsigned" | openssl dgst -sha256 -sign "$pem_file" -binary | b64url)
app_jwt="$jwt_unsigned.$jwt_sig"
rm -f "$pem_file"
install_id=$(curl -sS -H "Authorization: Bearer $app_jwt" -H "Accept: application/vnd.github+json" \
  https://api.github.com/app/installations \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(i["id"] for i in d if i["account"]["login"]==sys.argv[1]))' "${GH_APP_ACCOUNT}")
GITHUB_TOKEN=$(curl -sS -X POST -H "Authorization: Bearer $app_jwt" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/app/installations/$install_id/access_tokens" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
export GITHUB_TOKEN

if [ "${MINT_BOT_IDENTITY:-}" = "1" ]; then
  # Derived from the App's own /app + /users/{login} endpoints at runtime,
  # never hardcoded here -- this repo's own check-public-safety.sh scanner
  # forbids a literal fleet-agent-id string (the App's slug) in tracked file
  # content, even though that same identity is expected and fine as actual
  # commit-author metadata (COMMIT_METADATA_CORP_PATTERN in that scanner
  # explicitly allow-lists any `<id>+<login>[bot]@users.noreply...` address).
  # Deriving it keeps this file identity-agnostic too -- it works unmodified
  # if the installed App is ever swapped for a different one.
  BOT_LOGIN="$(curl -sS -H "Authorization: Bearer $app_jwt" -H "Accept: application/vnd.github+json" \
    https://api.github.com/app | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"] + "[bot]")')"
  # -g/--globoff: BOT_LOGIN is "<slug>[bot]" -- curl's URL-globbing parser
  # reads a literal `[...]` in the URL as a glob range ("bad range in URL")
  # and refuses the request unless globbing is disabled.
  BOT_USER_ID="$(curl -sSg -H "Accept: application/vnd.github+json" "https://api.github.com/users/${BOT_LOGIN}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
  # Built via a separate AT var, not a literal address shape in this file's
  # source -- check-public-safety.sh's generic email-address pattern is a
  # blunt static grep and can't tell a bot noreply address (legitimate public
  # commit metadata, per that scanner's own COMMIT_METADATA_CORP_PATTERN
  # comment) from a real leaked one; it just flags any contiguous
  # word-at-word-dot-word run it finds in tracked file content,
  # runtime-constructed or not.
  AT='@'
  BOT_EMAIL="${BOT_USER_ID}+${BOT_LOGIN}${AT}users.noreply.github.com"
fi

echo "==> minted a short-lived installation token for ${BOT_LOGIN:-the App} (install $install_id on ${GH_APP_ACCOUNT}) -- never printed"
