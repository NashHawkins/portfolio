#!/bin/bash
# Run by launchd every day at 08:00 (see install-daily-batch.sh): posts yesterday's LipSync videos
# (R2 lipsync/<yesterday>/) to @nashhawkins as Trial Reels. Pass --dry-run to check without posting.
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

# Right after a scheduled wake, Wi-Fi can take a while to reconnect. Wait up to 3 minutes.
for _ in $(seq 1 36); do
  curl -s -o /dev/null --max-time 5 https://lipsync-upload.reelsworker.workers.dev/ && break
  sleep 5
done

mode=(--all --unattended)
[ "${1:-}" = "--dry-run" ] && mode=(--dry-run)

output=$(.venv/bin/python -W ignore publish.py --account personal --batch yesterday "${mode[@]}" 2>&1)
rc=$?
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z')  publish.py --batch yesterday ${mode[*]}"
  echo "$output"
  echo "=== exit $rc"
  echo
} >> logs/daily-batch.log

# One-line summary as a Mac notification.
summary=$(echo "$output" | grep -E "^(Done:|Nothing to post|Stopped after|ERROR:|NOT READY|A real --all)" | tail -1)
case $rc in
  0) title="Trial Reels: daily batch OK" ;;
  *) title="Trial Reels: NEEDS ATTENTION (exit $rc)" ;;
esac
osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title (item 2 of argv)' -e 'end run' \
  "${summary:-See logs/daily-batch.log}" "$title" >/dev/null 2>&1
exit $rc
