#!/usr/bin/env python3
"""PreToolUse hook script for Claude Code.

Claude Code calls this script before executing any tool.
It forwards the tool info to the Python server via HTTP,
which sends a Feishu approval card and blocks until the user responds.

Input:  JSON on stdin  {"tool_name": "...", "tool_input": {...}, "session_id": "..."}
Output: JSON on stdout {"decision": "allow"/"deny", "reason": "..."}
"""
import json
import os
import sys

# Use urllib to avoid needing requests/pip install
import urllib.request
import urllib.error

# Server URL — same host/port as the main FastAPI app
HOST = os.environ.get("OPENCLAW_HOST", "localhost")
PORT = os.environ.get("OPENCLAW_PORT", "8080")
HOOK_URL = f"http://{HOST}:{PORT}/hooks/pre_tool_use"
TIMEOUT = int(os.environ.get("OPENCLAW_HOOK_TIMEOUT", "1800"))


def main():
    # Read hook input from stdin
    try:
        data = json.load(sys.stdin)
    except Exception:
        # Can't parse — allow by default
        print(json.dumps({"decision": "allow", "reason": "invalid input"}))
        return

    # Forward to Python server
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        HOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(json.dumps(result))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(json.dumps({
            "decision": "allow",
            "reason": f"hook server error {e.code}: {body[:200]}",
        }))
    except Exception as e:
        # On any error (connection, timeout), allow the tool
        # to avoid blocking Claude Code entirely
        print(json.dumps({
            "decision": "allow",
            "reason": f"hook error: {type(e).__name__}: {e}",
        }))


if __name__ == "__main__":
    main()
