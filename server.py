from flask import Flask, request, jsonify, render_template, Response
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

app = Flask(__name__)
DATA_FILE = Path(__file__).parent / "data.json"


def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"members": {}, "sessions": [], "messages": []}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


@app.route("/")
def dashboard():
    return render_template("index.html")


@app.route("/api/ping", methods=["POST"])
def ping():
    """Called by the wrapper script when claude starts/stops."""
    body = request.get_json(force=True)
    hostname = body.get("hostname", "unknown")
    event = body.get("event", "start")  # "start" or "stop"
    ts = datetime.now(timezone.utc).isoformat()

    data = load_data()

    if hostname not in data["members"]:
        data["members"][hostname] = {"alias": hostname}

    if event == "start":
        data["sessions"].append({
            "hostname": hostname,
            "start": ts,
            "end": None,
        })
    elif event == "stop":
        # close the most recent open session for this hostname
        for s in reversed(data["sessions"]):
            if s["hostname"] == hostname and s["end"] is None:
                s["end"] = ts
                break

    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/message", methods=["POST"])
def message():
    """Called by the Claude Code hook on every response."""
    body = request.get_json(force=True)
    hostname = body.get("hostname", "unknown")
    ts = datetime.now(timezone.utc).isoformat()

    data = load_data()

    if hostname not in data["members"]:
        data["members"][hostname] = {"alias": hostname}

    if "messages" not in data:
        data["messages"] = []

    data["messages"].append({
        "hostname": hostname,
        "time": ts,
    })

    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/data")
def get_data():
    """Returns all usage data for the dashboard."""
    return jsonify(load_data())


@app.route("/api/rename", methods=["POST"])
def rename_member():
    """Rename a hostname to a friendly name."""
    body = request.get_json(force=True)
    hostname = body.get("hostname")
    alias = body.get("alias")
    if not hostname or not alias:
        return jsonify({"error": "hostname and alias required"}), 400

    data = load_data()
    if hostname in data["members"]:
        data["members"][hostname]["alias"] = alias
    else:
        data["members"][hostname] = {"alias": alias}
    save_data(data)
    return jsonify({"ok": True})


@app.route("/install")
def install_script():
    """Serves the install script that friends run once."""
    server_url = request.host_url.rstrip("/")
    if request.headers.get("X-Forwarded-Proto") == "https":
        server_url = server_url.replace("http://", "https://")
    script = f"""#!/bin/bash
# Claude Usage Tracker - One-time install
# This wraps the claude command to track session usage.

SERVER="{server_url}"
WRAPPER="$HOME/.claude-tracker.sh"
REAL_CLAUDE=$(which claude 2>/dev/null || echo "claude")

# Create the wrapper script
cat > "$WRAPPER" << 'SCRIPT'
#!/bin/bash
SERVER="{server_url}"
HOSTNAME=$(hostname)
REAL_CLAUDE=$(grep '^# REAL_CLAUDE=' "$0" | cut -d= -f2)

# Ping start
curl -s -X POST "$SERVER/api/ping" \\
  -H "Content-Type: application/json" \\
  -d "{{\\"hostname\\": \\"$HOSTNAME\\", \\"event\\": \\"start\\"}}" > /dev/null 2>&1 &

# Run the real claude with all arguments
$REAL_CLAUDE "$@"
EXIT_CODE=$?

# Ping stop
curl -s -X POST "$SERVER/api/ping" \\
  -H "Content-Type: application/json" \\
  -d "{{\\"hostname\\": \\"$HOSTNAME\\", \\"event\\": \\"stop\\"}}" > /dev/null 2>&1 &

exit $EXIT_CODE
SCRIPT

# Embed the real claude path
echo "# REAL_CLAUDE=$REAL_CLAUDE" >> "$WRAPPER"
chmod +x "$WRAPPER"

# Add alias to shell profile
ALIAS_LINE='alias claude="$HOME/.claude-tracker.sh"'
ADDED=false

for RC in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$RC" ]; then
        if ! grep -q "claude-tracker" "$RC"; then
            echo "" >> "$RC"
            echo "# Claude Usage Tracker" >> "$RC"
            echo "$ALIAS_LINE" >> "$RC"
            ADDED=true
        fi
    fi
done

# Also try to add to current shell
eval "$ALIAS_LINE"

# Set up Claude Code hook to count messages
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
HOOK_SCRIPT="$HOME/.claude-tracker-hook.sh"

# Create the hook script
cat > "$HOOK_SCRIPT" << HOOKEOF
#!/bin/bash
curl -s -X POST "{server_url}/api/message" \\
  -H "Content-Type: application/json" \\
  -d "{{\\"hostname\\": \\"$(hostname)\\"}}}" > /dev/null 2>&1
HOOKEOF
chmod +x "$HOOK_SCRIPT"

# Add hook to Claude Code settings
mkdir -p "$HOME/.claude"
if [ -f "$CLAUDE_SETTINGS" ]; then
    # Check if hooks already configured
    if ! grep -q "claude-tracker-hook" "$CLAUDE_SETTINGS"; then
        # Use python/node to merge JSON if available
        if command -v python3 &> /dev/null; then
            python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    cfg = json.load(f)
cfg.setdefault('hooks', {{}})
cfg['hooks']['Notification'] = [{{
    'matcher': '',
    'hooks': [{{
        'type': 'command',
        'command': '$HOOK_SCRIPT'
    }}]
}}]
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(cfg, f, indent=2)
"
        fi
    fi
else
    cat > "$CLAUDE_SETTINGS" << SETTINGSEOF
{{
  "hooks": {{
    "Notification": [
      {{
        "matcher": "",
        "hooks": [
          {{
            "type": "command",
            "command": "$HOOK_SCRIPT"
          }}
        ]
      }}
    ]
  }}
}}
SETTINGSEOF
fi

echo ""
echo "Claude Usage Tracker installed!"
echo "The 'claude' command now tracks sessions and messages automatically."
echo "Dashboard: $SERVER"
echo ""
echo "Restart your terminal or run: source ~/.bashrc"
"""
    return Response(script, mimetype="text/plain")


if not DATA_FILE.exists():
    save_data({"members": {}, "sessions": []})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
