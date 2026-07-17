#!/usr/bin/env bash
# scripts/daily_gex.sh — run the GEX tool pre-open and push the levels + chart to
# your phone. Driven by the launchd agent (com.brendanhan.gex-daily), Mon–Fri.
#
# Credentials + notifier config come from .env (see .env.example). Set ONE notifier:
#   PUSHOVER_TOKEN + PUSHOVER_USER          (pushover.net — polished, sends the chart)
#   TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   (telegram bot — free, private, sends chart)
#   NTFY_TOPIC [+ NTFY_SERVER]              (ntfy.sh — free, no account)
# Optional: GEX_TICKER (default QQQ), GEX_PY (interpreter), GEX_DIR (project dir).
set -uo pipefail

GEX_DIR="${GEX_DIR:-/Users/brendanhan/Desktop/Quant_Projects/gamma_exposure}"
GEX_PY="${GEX_PY:-/opt/anaconda3/bin/python}"
cd "$GEX_DIR" || exit 1

# Load Schwab creds + notifier config from .env (exported to child processes).
if [ -f .env ]; then set -a; . ./.env; set +a; fi
TICKER="${GEX_TICKER:-QQQ}"
mkdir -p "$GEX_DIR/logs"
LOG="$GEX_DIR/logs/daily_gex.log"
ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }
echo "[$(ts)] start daily GEX ($TICKER)" >>"$LOG"

# 1) One run: --levels-only prints the compact block AND still saves the chart PNG.
OUT="$("$GEX_PY" gex.py --ticker "$TICKER" --expiry all --levels-only 2>&1)"; RC=$?
printf '%s\n' "$OUT" >>"$LOG"

# 2) Compact levels for the message body (just the block, not the fetch chatter).
BODY="$(printf '%s\n' "$OUT" | grep -E '\| spot |regime |flip |call wall |put wall |net GEX ')"
[ -z "$BODY" ] && BODY="$(printf '%s\n' "$OUT" | tail -n 15)"

# 3) Chart path from the "chart saved:" line (may be empty).
CHART="$(printf '%s\n' "$OUT" | sed -n 's/.*chart saved: //p' | tail -n 1)"
[ -n "$CHART" ] && [ ! -f "$CHART" ] && CHART=""

TITLE="$TICKER GEX $(TZ=America/New_York date +%m/%d)"
if [ "$RC" -ne 0 ]; then
    TITLE="$TICKER GEX — RUN FAILED"
    BODY="$(printf 'Daily run failed (rc=%s). Last lines:\n%s' "$RC" "$(printf '%s\n' "$OUT" | tail -n 8)")"
    CHART=""   # nothing trustworthy to attach
fi

# 4) Send via whichever notifier is configured (first match wins).
send() {  # title body image
    local title="$1" body="$2" img="$3"
    if [ -n "${PUSHOVER_TOKEN:-}" ] && [ -n "${PUSHOVER_USER:-}" ]; then
        local args=(--form-string "token=$PUSHOVER_TOKEN" --form-string "user=$PUSHOVER_USER"
                    --form-string "title=$title" --form-string "message=$body")
        [ -n "$img" ] && args+=(-F "attachment=@$img")
        curl -s "${args[@]}" https://api.pushover.net/1/messages.json >/dev/null && echo pushover
    elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        local base="https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN"
        if [ -n "$img" ]; then
            curl -s -F "chat_id=$TELEGRAM_CHAT_ID" -F "photo=@$img" \
                 -F "caption=$(printf '%s\n%s' "$title" "$body")" "$base/sendPhoto" >/dev/null
        else
            curl -s -F "chat_id=$TELEGRAM_CHAT_ID" \
                 -F "text=$(printf '%s\n%s' "$title" "$body")" "$base/sendMessage" >/dev/null
        fi
        echo telegram
    elif [ -n "${NTFY_TOPIC:-}" ]; then
        local server="${NTFY_SERVER:-https://ntfy.sh}"
        curl -s -H "Title: $title" -d "$body" "$server/$NTFY_TOPIC" >/dev/null
        [ -n "$img" ] && curl -s -H "Title: $title (chart)" -H "Filename: $(basename "$img")" \
                              -T "$img" "$server/$NTFY_TOPIC" >/dev/null
        echo ntfy
    else
        echo none
    fi
}

USED="$(send "$TITLE" "$BODY" "$CHART")"
if [ "$USED" = none ]; then
    echo "[$(ts)] WARNING: no notifier configured — set PUSHOVER_*/TELEGRAM_*/NTFY_* in .env" >>"$LOG"
else
    echo "[$(ts)] notified via $USED (rc=$RC, chart=${CHART:-none})" >>"$LOG"
fi
exit 0
