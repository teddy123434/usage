#!/bin/bash
# Zed claude-acp -> CLAUDE_CODE_EXECUTABLE: tee claude's rate_limit_event into
# ~/.claude/usage-status.json for the usage app. Revert: remove the env key in Zed settings.
REAL="$HOME/Library/Application Support/Zed/external_agents/registry/npx/claude-acp/node_modules/@anthropic-ai/claude-agent-sdk-darwin-arm64/claude"
if [ ! -x "$REAL" ]; then
  echo "usage-claude-tap: claude binary not found at $REAL; remove CLAUDE_CODE_EXECUTABLE from Zed settings" >&2
  exit 127
fi
exec /usr/bin/python3 "$HOME/.claude/usage-acp-tap.py" "$REAL" "$@"
