#!/bin/bash
# =============================================================================
# Weekly/Daily Valuation Screener Runner
# Runs fetch_fundamentals.py, writes TSV + diff, sends macOS notification.
# Scheduled via launchd — see com.kensielecki.valuations.plist
# =============================================================================

set -euo pipefail

SCRIPTS_DIR="$HOME/Claude projects/2603_token-project-valuations"
OUTPUT_DIR="$SCRIPTS_DIR/output"
LOG_DIR="$OUTPUT_DIR/logs"
PYTHON="/usr/bin/python3"
DATE_PREFIX=$(date +"%y%m%d")
WEEKS_TO_KEEP=8
NETWORK_TIMEOUT=120
TOP_LEVEL_RETRIES=3
TOP_LEVEL_DELAY=300

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

LAUNCHD_LOG="$LOG_DIR/launchd_stdout.log"

log() {
    echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LAUNCHD_LOG"
}

# ---- Network wait ----
wait_for_network() {
    log "Waiting for network…"
    local elapsed=0
    while [ $elapsed -lt $NETWORK_TIMEOUT ]; do
        if curl -s --max-time 5 https://api.llama.fi >/dev/null 2>&1; then
            log "Network available."
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    log "ERROR: No network after ${NETWORK_TIMEOUT}s."
    return 1
}

# ---- macOS notification ----
NOTIFIER="/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier"
notify() {
    local title="$1" message="$2"
    if [ -x "$NOTIFIER" ]; then
        "$NOTIFIER" -title "$title" -message "$message" -sound default 2>/dev/null || true
    else
        osascript -e "display notification \"$message\" with title \"$title\"" 2>/dev/null || true
    fi
}

# ---- Log rotation ----
rotate_logs() {
    log "Rotating old logs (keeping last $WEEKS_TO_KEEP weeks)…"
    local cutoff_date
    cutoff_date=$(date -v-${WEEKS_TO_KEEP}w +"%y%m%d")

    for dir in "$LOG_DIR" "$OUTPUT_DIR/diffs"; do
        find "$dir" -maxdepth 1 -type f -name "??????_*" 2>/dev/null | while read -r f; do
            local file_date
            file_date=$(basename "$f" | grep -oE '^[0-9]{6}' || echo "")
            if [ -n "$file_date" ] && [ "$file_date" \< "$cutoff_date" ]; then
                rm -f "$f"
                log "  Deleted $(basename "$f")"
            fi
        done
    done

    find "$OUTPUT_DIR" -maxdepth 1 -type f -name "??????_fundamentals.tsv" 2>/dev/null | while read -r f; do
        local file_date
        file_date=$(basename "$f" | grep -oE '^[0-9]{6}' || echo "")
        if [ -n "$file_date" ] && [ "$file_date" \< "$cutoff_date" ]; then
            rm -f "$f"
            log "  Deleted $(basename "$f")"
        fi
    done
}

# =============================================================================
# MAIN
# =============================================================================

log "=========================================="
log "VALUATION SCREENER RUN — $(date '+%Y-%m-%d %H:%M %Z')"
log "=========================================="

# Early exit if today's TSV already exists
if [ -f "$OUTPUT_DIR/${DATE_PREFIX}_fundamentals.tsv" ]; then
    log "TSV already exists for today ($DATE_PREFIX) — skipping."
    exit 0
fi

if ! wait_for_network; then
    notify "Valuation Screener" "Failed: no network connection"
    exit 1
fi

# Top-level retry loop
round=1
success=false

while [ $round -le $TOP_LEVEL_RETRIES ] && [ "$success" = "false" ]; do
    if [ $round -gt 1 ]; then
        log ""
        log "=== RETRY ROUND $round/$TOP_LEVEL_RETRIES (waiting ${TOP_LEVEL_DELAY}s) ==="
        sleep $TOP_LEVEL_DELAY
        if ! wait_for_network; then
            log "Network still unavailable."
            round=$((round + 1))
            continue
        fi
    fi

    log ""
    log "--- fetch_fundamentals (attempt $round/$TOP_LEVEL_RETRIES) ---"

    exit_code=0
    $PYTHON "$SCRIPTS_DIR/fetch_fundamentals.py" >> "$LAUNCHD_LOG" 2>&1 || exit_code=$?

    # Check both exit code AND output file existence
    if [ -f "$OUTPUT_DIR/${DATE_PREFIX}_fundamentals.tsv" ]; then
        success=true
        log "fetch_fundamentals completed successfully."
    else
        log "fetch_fundamentals exited $exit_code but produced no TSV output."
    fi

    round=$((round + 1))
done

rotate_logs

if [ "$success" = "true" ]; then
    row_count=$(tail -n +2 "$OUTPUT_DIR/${DATE_PREFIX}_fundamentals.tsv" | wc -l | tr -d ' ')
    alert_count=0
    if [ -f "$OUTPUT_DIR/diffs/${DATE_PREFIX}_fundamentals_changes.tsv" ]; then
        alert_count=$(tail -n +2 "$OUTPUT_DIR/diffs/${DATE_PREFIX}_fundamentals_changes.tsv" | wc -l | tr -d ' ')
    fi
    notify "Valuation Screener" "Done: $row_count protocols, $alert_count alert(s)"
    log "Done."
else
    notify "Valuation Screener" "Failed after $TOP_LEVEL_RETRIES attempts"
    log "ERROR: All attempts failed — no TSV produced."
    exit 1
fi
