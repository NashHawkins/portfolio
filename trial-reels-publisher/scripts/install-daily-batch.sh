#!/bin/bash
# Installs (or reinstalls) the 08:00 launchd job. Uninstall: ./install-daily-batch.sh --uninstall
set -eu
label=local.trial-reels.daily-batch
plist="$HOME/Library/LaunchAgents/$label.plist"
project="$(cd "$(dirname "$0")/.." && pwd)"

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$plist"; echo "Removed $label."; exit 0
fi

cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <!-- caffeinate keeps a just-woken Mac from going back to sleep until the run finishes. -->
  <array><string>/usr/bin/caffeinate</string><string>-i</string><string>/bin/bash</string><string>$project/scripts/daily-batch.sh</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$project/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$project/logs/launchd.log</string>
</dict>
</plist>
PLIST
plutil -lint "$plist" >/dev/null
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "Installed $label: every day at 08:00 (Mac time; it runs on wake if the Mac was asleep)."
