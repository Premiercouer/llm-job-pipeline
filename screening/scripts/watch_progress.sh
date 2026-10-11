#!/bin/bash
# Follow a 02_run_screening.py log and emit only progress / failure / completion lines.
# Exits after the DONE line, so a Monitor on this script ends with the screening run.
#
# Usage: scripts/watch_progress.sh .runtime/<date>/screening.log
log="$1"
pattern='^(PROGRESS|FAILED|DONE)|Traceback'
seen=0
until [ -f "$log" ]; do sleep 1; done
while true; do
  total=$(wc -l < "$log" | tr -d ' ')
  if [ "$total" -gt "$seen" ]; then
    new=$(sed -n "$((seen + 1)),${total}p" "$log")
    seen=$total
    printf '%s\n' "$new" | grep -E "$pattern"
    printf '%s\n' "$new" | grep -q '^DONE' && exit 0
  fi
  sleep 2
done
