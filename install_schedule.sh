#!/usr/bin/env bash
# Installs the launchd job that runs the scraper at 09:00 and 17:00 daily.
set -e
PLIST="$HOME/Library/LaunchAgents/com.sms.scraper.plist"
SRC="/Users/fredanaman/Documents/claudecode/sms-scraper/com.sms.scraper.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cp "$SRC" "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ Installed. Runs daily at 09:00 and 17:00."
echo "   list:   launchctl list | grep sms"
echo "   unload: launchctl unload $PLIST"
