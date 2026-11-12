#!/usr/bin/env bash
# Install the claude-capture POC for the current user.
#   ./install.sh            copy the script to ~/.claude-capture/bin and print next steps
#   ./install.sh --apply    also merge the hooks into ~/.claude/settings.json (a backup is kept)
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude-capture/bin"
mkdir -p "$DEST"
install -m 0755 "$HERE/capture.sh" "$DEST/capture.sh"
echo "Installed $DEST/capture.sh"

if [ "${1:-}" = "--apply" ]; then
  SETTINGS="$HOME/.claude/settings.json"
  mkdir -p "$HOME/.claude"
  [ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d%H%M%S)" && echo "Backed up $SETTINGS"
  python3 - "$SETTINGS" "$HERE/settings.example.json" << 'EOF'
import json, os, sys
path, example = sys.argv[1], sys.argv[2]
cur = json.load(open(path)) if os.path.exists(path) else {}
new = json.load(open(example))
cur.setdefault("env", {})
for k, v in new.get("env", {}).items():
    cur["env"].setdefault(k, v)          # never overwrite an existing value
hooks = cur.setdefault("hooks", {})
for event, groups in new["hooks"].items():
    existing = hooks.setdefault(event, [])
    cmds = {h.get("command") for g in existing for h in g.get("hooks", [])}
    for g in groups:
        if not any(h.get("command") in cmds for h in g["hooks"]):
            existing.append(g)
json.dump(cur, open(path, "w"), indent=2)
print("Merged capture hooks into", path)
EOF
  echo "Edit CAPTURE_REMOTE_BASE in $SETTINGS to point at your capture group."
else
  echo "Next: merge settings.example.json into ~/.claude/settings.json (or run ./install.sh --apply),"
  echo "then set CAPTURE_REMOTE_BASE to your GitLab capture group."
fi
