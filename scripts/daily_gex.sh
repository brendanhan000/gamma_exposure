#!/usr/bin/env bash
# scripts/daily_gex.sh [daily|weekly|monthly]  — run the GEX tool for each ticker
# at the cadence's expiration window and push the levels + charts to your phone.
#
#   daily   -> --all-days 45   (near-term, tactical)      [launchd: Mon–Fri]
#   weekly  -> --all-days 90    (out ~a quarter)          [launchd: Monday]
#   monthly -> --all-days 150  (structural)               [launchd: 1st of month]
#
# Config from .env (see .env.example). Set ONE notifier (PUSHOVER_*/TELEGRAM_*/NTFY_*).
# Overrides: GEX_TICKERS ("SPY QQQ"), GEX_{DAILY,WEEKLY,MONTHLY}_DAYS, GEX_SEND_CHARTS,
#            GEX_TICKER (legacy single ticker), GEX_PY, GEX_DIR.
set -uo pipefail

CADENCE="${1:-daily}"
case "$CADENCE" in
    daily)   DAYS="${GEX_DAILY_DAYS:-45}";   TAG="DAILY" ;;
    weekly)  DAYS="${GEX_WEEKLY_DAYS:-90}";  TAG="WEEKLY" ;;
    monthly) DAYS="${GEX_MONTHLY_DAYS:-150}"; TAG="MONTHLY" ;;
    *) echo "usage: daily_gex.sh [daily|weekly|monthly]" >&2; exit 2 ;;
esac

GEX_DIR="${GEX_DIR:-/Users/brendanhan/Desktop/Quant_Projects/gamma_exposure}"
GEX_PY="${GEX_PY:-/opt/anaconda3/bin/python}"
cd "$GEX_DIR" || exit 1
if [ -f .env ]; then set -a; . ./.env; set +a; fi
TICKERS="${GEX_TICKERS:-${GEX_TICKER:-SPY QQQ}}"   # GEX_TICKER kept for back-compat
SEND_CHARTS="${GEX_SEND_CHARTS:-1}"
mkdir -p "$GEX_DIR/logs"
LOG="$GEX_DIR/logs/daily_gex.log"
ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }
echo "[$(ts)] start $TAG (${DAYS}d) tickers=[$TICKERS]" >>"$LOG"

# Preflight: warn 1.5 days BEFORE the 7-day Schwab refresh token dies, so the
# re-login happens on your schedule instead of as a morning outage.
AGE_WARN="$("$GEX_PY" -c 'import json,time; d=json.load(open(".schwab_token.json")); ct=d.get("creation_timestamp") or 0; age=(time.time()-ct)/86400.0; age>5.5 and print("WARNING: Schwab token is {:.1f} days old (dies at 7) -> run scripts/schwab_setup.py".format(age))' 2>/dev/null)"

BODY=""
CHARTS=()
N_TOTAL=0
N_FAIL=0
AUTH_DEAD=0
for T in $TICKERS; do
    N_TOTAL=$((N_TOTAL + 1))
    OUT="$("$GEX_PY" gex.py --ticker "$T" --expiry all --all-days "$DAYS" --levels-only 2>&1)"; RC=$?
    printf '%s\n' "$OUT" >>"$LOG"
    if printf '%s' "$OUT" | grep -qiE "refresh token is invalid|unsupported_token_type|OAuthError"; then
        AUTH_DEAD=1
    fi
    [ "$RC" -ne 0 ] && N_FAIL=$((N_FAIL + 1))
    BLOCK="$(printf '%s\n' "$OUT" | grep -E '\| spot |regime |flip |call wall |put wall |net GEX ')"
    [ -z "$BLOCK" ] && BLOCK="$T | run failed (rc=$RC): $(printf '%s\n' "$OUT" | grep -m1 'ERROR' | cut -c1-90)"
    BODY="${BODY}${BLOCK}"$'\n\n'
    if [ "$SEND_CHARTS" = "1" ]; then
        CH="$(printf '%s\n' "$OUT" | sed -n 's/.*chart saved: //p' | tail -n 1)"
        [ -n "$CH" ] && [ -f "$CH" ] && CHARTS+=("$CH")
    fi
done
[ -n "$AGE_WARN" ] && BODY="${BODY}${AGE_WARN}"$'\n'

TITLE="GEX $TAG (${DAYS}d) $(TZ=America/New_York date +%m/%d)"
if [ "$AUTH_DEAD" -eq 1 ]; then
    TITLE="GEX $TAG - SCHWAB TOKEN EXPIRED: run scripts/schwab_setup.py"
elif [ "$N_FAIL" -eq "$N_TOTAL" ]; then
    TITLE="GEX $TAG - RUN FAILED (all $N_TOTAL tickers; see logs/daily_gex.log)"
elif [ "$N_FAIL" -gt 0 ]; then
    TITLE="GEX $TAG - PARTIAL ($((N_TOTAL - N_FAIL))/$N_TOTAL ok)"
fi
RC_ALL=$N_FAIL

# ---- notifiers (first configured one wins) ----
send_text() {  # title body
    local title="$1" body="$2"
    if [ -n "${PUSHOVER_TOKEN:-}" ] && [ -n "${PUSHOVER_USER:-}" ]; then
        curl -s --form-string "token=$PUSHOVER_TOKEN" --form-string "user=$PUSHOVER_USER" \
             --form-string "title=$title" --form-string "message=$body" \
             https://api.pushover.net/1/messages.json >/dev/null && echo pushover
    elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -s -F "chat_id=$TELEGRAM_CHAT_ID" \
             -F "text=$(printf '%s\n%s' "$title" "$body")" \
             "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" >/dev/null && echo telegram
    elif [ -n "${NTFY_TOPIC:-}" ]; then
        curl -s -H "Title: $title" -d "$body" \
             "${NTFY_SERVER:-https://ntfy.sh}/$NTFY_TOPIC" >/dev/null && echo ntfy
    else
        echo none
    fi
}
send_image() {  # title imgpath
    local title="$1" img="$2"
    [ -f "$img" ] || return 0
    if [ -n "${PUSHOVER_TOKEN:-}" ] && [ -n "${PUSHOVER_USER:-}" ]; then
        curl -s --form-string "token=$PUSHOVER_TOKEN" --form-string "user=$PUSHOVER_USER" \
             --form-string "title=$title" --form-string "message=$(basename "$img" .png)" \
             -F "attachment=@$img" https://api.pushover.net/1/messages.json >/dev/null
    elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -s -F "chat_id=$TELEGRAM_CHAT_ID" -F "photo=@$img" -F "caption=$title" \
             "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendPhoto" >/dev/null
    elif [ -n "${NTFY_TOPIC:-}" ]; then
        curl -s -H "Title: $title" -H "Filename: $(basename "$img")" -T "$img" \
             "${NTFY_SERVER:-https://ntfy.sh}/$NTFY_TOPIC" >/dev/null
    fi
}

USED="$(send_text "$TITLE" "$BODY")"
if [ "$USED" != none ] && [ "${#CHARTS[@]}" -gt 0 ]; then
    for ch in "${CHARTS[@]}"; do send_image "$TITLE — $(basename "$ch" .png)" "$ch"; done
fi
echo "[$(ts)] $TAG notified via $USED (rc=$RC_ALL, charts=${#CHARTS[@]}, title=\"$TITLE\")" >>"$LOG"
[ "$USED" = none ] && echo "[$(ts)] WARNING: no notifier configured — set PUSHOVER_*/TELEGRAM_*/NTFY_* in .env" >>"$LOG"
exit 0
