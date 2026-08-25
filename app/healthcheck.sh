#!/usr/bin/env ash
# Fails if no sync cycle has completed successfully within 2x the configured INTERVAL.
# run.py writes a unix timestamp to HEARTBEAT_FILE after every cycle that reaches
# Proxmox successfully (even one with nothing to sync); it is not written on a
# config or total-Proxmox-failure exit.

to_seconds() {
    case "$1" in
        *s) echo "${1%s}" ;;
        *m) echo $(( ${1%m} * 60 )) ;;
        *h) echo $(( ${1%h} * 3600 )) ;;
        *d) echo $(( ${1%d} * 86400 )) ;;
        *)  echo "$1" ;;
    esac
}

HEARTBEAT_FILE="/app/.last_success"

if [ ! -f "$HEARTBEAT_FILE" ]; then
    echo "No successful sync recorded yet"
    exit 1
fi

interval_seconds=$(to_seconds "${INTERVAL:-1h}")
max_age=$(( interval_seconds * 2 ))
last=$(cat "$HEARTBEAT_FILE")
now=$(date +%s)
age=$(( now - last ))

if [ "$age" -gt "$max_age" ]; then
    echo "Last successful sync was ${age}s ago, exceeding the ${max_age}s threshold"
    exit 1
fi

echo "Last successful sync was ${age}s ago"
exit 0
