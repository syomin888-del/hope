#!/bin/bash
# One-time zero-subscription installation on macOS 13+ (Apple Silicon or Intel).
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]];then echo 'Python 3.10+ required. Install from python.org or Homebrew.';exit 1;fi
"$PY" -c 'import sys; assert sys.version_info >= (3,10), "Requires Python 3.10+"'
if [[ ! -d .venv ]];then "$PY" -m venv .venv;fi
VPY="$ROOT/.venv/bin/python"
"$VPY" -m pip install --disable-pip-version-check -r requirements-ml.txt
"$VPY" collect.py init
"$VPY" research.py init
"$VPY" oracle.py init
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs" "$ROOT/model_artifacts"
# Safely XML-escape the actual location (spaces, accented folder names).
"$VPY" - "$ROOT" "$VPY" "$HOME/Library/LaunchAgents" <<'PYCODE'
from pathlib import Path
from xml.sax.saxutils import escape
import sys
root,python,folder=sys.argv[1:]
for label,program,argument in [('com.oracle.research','daemon.py','run'),
                               ('com.oracle.brain','brain_daemon.py','run'),
                               ('com.oracle.dashboard','dashboard.py',None)]:
    array='<string>'+escape(python)+'</string><string>'+escape(root+'/'+program)+'</string>'
    if argument:array+='<string>'+argument+'</string>'
    plist=f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>{label}</string>
<key>ProgramArguments</key><array>{array}</array>
<key>WorkingDirectory</key><string>{escape(root)}</string>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{escape(root+'/logs/'+label+'.out')}</string>
<key>StandardErrorPath</key><string>{escape(root+'/logs/'+label+'.err')}</string>
</dict></plist>'''
    Path(folder,label+'.plist').write_text(plist,encoding='utf8')
PYCODE
for label in com.oracle.research com.oracle.brain com.oracle.dashboard;do
    P="$HOME/Library/LaunchAgents/$label.plist"
    launchctl bootout "gui/$(id -u)" "$P" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$P"
done
printf '\nORACLE v1.1 installed.\nDashboard: http://localhost:8765/#intelligence\n'
echo 'Three background services restart automatically when you log in.'
echo 'Mac must stay awake; true 24/7 when Mac is off requires a server.'
