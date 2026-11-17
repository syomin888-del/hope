#!/bin/bash
set -euo pipefail
for label in com.oracle.research com.oracle.brain com.oracle.dashboard; do
   P="$HOME/Library/LaunchAgents/$label.plist"
   if [ -f "$P" ]; then launchctl bootout "gui/$(id -u)" "$P" 2>/dev/null || true; rm -f "$P"; fi
done
echo 'Services stopped. Collected data and models have NOT been deleted.'
