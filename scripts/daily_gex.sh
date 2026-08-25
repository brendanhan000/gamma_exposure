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
# curl exits 0 on HTTP 4xx/5xx unless told otherwise, so a rejected push used to
# be logged as a SUCCESS ("notified via ntfy") while nothing was delivered.
# Every send now checks the actual status code and reports the failure.
# send_text runs in a command substitution, so a shell variable set inside it
# cannot reach the caller. The reason is written to a temp file instead, or the
# failure would always be reported as "unknown".
NOTIFY_ERR_FILE="$(mktemp -t gexnotify)"
trap 'rm -f "$NOTIFY_ERR_FILE"' EXIT

http_send() {   # description, curl args...  -> echoes "ok" on 2xx, else records why
    local what="$1"; shift
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 45 "$@" 2>/dev/null)" || code="000"
    if [ "${code:0:1}" = "2" ]; then
        echo ok
    else
        # 000 = curl could not complete the request at all (DNS, TLS, timeout).
        if [ "$code" = "000" ]; then
            printf '%s unreachable (DNS/TLS/timeout)' "$what" >"$NOTIFY_ERR_FILE"
        else
            printf '%s HTTP %s' "$what" "$code" >"$NOTIFY_ERR_FILE"
        fi
        echo ""
    fi
}

# HTTP header values must be ASCII; a non-ASCII title can be rejected or mangled.
ascii() { printf '%s' "$1" | LC_ALL=C tr -cd '\11\12\40-\176'; }

send_text() {  # title body
    local title body
    title="$(ascii "$1")"; body="$2"
    if [ -n "${PUSHOVER_TOKEN:-}" ] && [ -n "${PUSHOVER_USER:-}" ]; then
        [ -n "$(http_send pushover --form-string "token=$PUSHOVER_TOKEN" \
              --form-string "user=$PUSHOVER_USER" --form-string "title=$title" \
              --form-string "message=$body" https://api.pushover.net/1/messages.json)" ] \
            && echo pushover || echo FAILED
    elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        [ -n "$(http_send telegram -F "chat_id=$TELEGRAM_CHAT_ID" \
              -F "text=$(printf '%s\n%s' "$title" "$body")" \
              "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage")" ] \
            && echo telegram || echo FAILED
    elif [ -n "${NTFY_TOPIC:-}" ]; then
        [ -n "$(http_send ntfy -H "Title: $title" -H "X-Priority: ${NTFY_PRIORITY:-default}" \
              --data-binary "$body" "${NTFY_SERVER:-https://ntfy.sh}/$NTFY_TOPIC")" ] \
            && echo ntfy || echo FAILED
    else
        echo none
    fi
}

send_image() {  # title imgpath
    local title img
    title="$(ascii "$1")"; img="$2"
    [ -f "$img" ] || return 0
    if [ -n "${PUSHOVER_TOKEN:-}" ] && [ -n "${PUSHOVER_USER:-}" ]; then
        http_send pushover-img --form-string "token=$PUSHOVER_TOKEN" \
            --form-string "user=$PUSHOVER_USER" --form-string "title=$title" \
            --form-string "message=$(basename "$img" .png)" \
            -F "attachment=@$img" https://api.pushover.net/1/messages.json >/dev/null
    elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        http_send telegram-img -F "chat_id=$TELEGRAM_CHAT_ID" -F "photo=@$img" \
            -F "caption=$title" \
            "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendPhoto" >/dev/null
    elif [ -n "${NTFY_TOPIC:-}" ]; then
        http_send ntfy-img -H "Title: $title" -H "Filename: $(basename "$img")" \
            -T "$img" "${NTFY_SERVER:-https://ntfy.sh}/$NTFY_TOPIC" >/dev/null
    fi
}

: >"$NOTIFY_ERR_FILE"
USED="$(send_text "$TITLE" "$BODY")"
NOTIFY_ERR="$(cat "$NOTIFY_ERR_FILE" 2>/dev/null)"
if [ "$USED" != none ] && [ "$USED" != FAILED ] && [ "${#CHARTS[@]}" -gt 0 ]; then
    for ch in "${CHARTS[@]}"; do send_image "$TITLE - $(basename "$ch" .png)" "$ch"; done
fi

if [ "$USED" = none ]; then
    echo "[$(ts)] WARNING: no notifier configured -- set PUSHOVER_*/TELEGRAM_*/NTFY_* in .env" >>"$LOG"
elif [ "$USED" = FAILED ]; then
    # Loud, because a silently-dropped push is indistinguishable from "no news".
    echo "[$(ts)] *** NOTIFY FAILED (${NOTIFY_ERR:-unknown}) *** $TAG was NOT delivered." >>"$LOG"
    echo "[$(ts)]     title was: \"$TITLE\"" >>"$LOG"
    echo "NOTIFY FAILED: ${NOTIFY_ERR:-unknown} -- $TAG push was not delivered." >&2
else
    echo "[$(ts)] $TAG notified via $USED (rc=$RC_ALL, charts=${#CHARTS[@]}, title=\"$TITLE\")" >>"$LOG"
    [ -n "$NOTIFY_ERR" ] && echo "[$(ts)]     note: chart upload issue ($NOTIFY_ERR)" >>"$LOG"
fi
exit 0
