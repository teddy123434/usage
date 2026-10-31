# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from usage_hooks import usage_acp_tap as tap

# Shape captured from a real claude-agent-sdk 0.3.312 stream (stream-json mode).
REAL_INFO = {
    "status": "allowed",
    "resetsAt": 1793473200,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.64, "resetsAt": 1793473200, "status": "allowed"},
        "seven_day": {"utilization": 0.34, "resetsAt": 1793826000, "status": "allowed"},
    },
}


def _usage_update_line(info: dict) -> bytes:
    message = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "usage_update",
                "used": 1234,
                "size": 200000,
                "_meta": {"_claude/rateLimit": info},
            },
        },
    }
    return (json.dumps(message) + "\n").encode()


def test_unified_windows_convert_fraction_to_percent() -> None:
    assert tap.windows_from_rate_limit_info(REAL_INFO) == {
        "five_hour": {"used_percentage": 64.0, "resets_at": 1793473200},
        "seven_day": {"used_percentage": 34.0, "resets_at": 1793826000},
    }


def test_single_window_fallback_without_unified_windows() -> None:
    info = {"rateLimitType": "seven_day", "utilization": 0.5, "resetsAt": 1700000000}
    assert tap.windows_from_rate_limit_info(info) == {
        "seven_day": {"used_percentage": 50.0, "resets_at": 1700000000},
    }


def test_unknown_or_missing_values_are_ignored() -> None:
    assert tap.windows_from_rate_limit_info({"status": "allowed"}) == {}
    assert (
        tap.windows_from_rate_limit_info({"unifiedWindows": {"five_hour": {"utilization": "x"}}})
        == {}
    )
    assert tap.windows_from_rate_limit_info("nope") == {}


def test_extract_ignores_other_messages() -> None:
    assert tap.extract_rate_limit_info(b'{"jsonrpc":"2.0","id":1,"result":{}}\n') is None
    assert tap.extract_rate_limit_info(b"not json\n") is None
    assert tap.extract_rate_limit_info(_usage_update_line(REAL_INFO)) == REAL_INFO


def test_extract_reads_raw_claude_stream_json_line() -> None:
    # Verbatim line from claude --output-format stream-json (2026-11-01).
    line = (
        b'{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1793473200,'
        b'"rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":'
        b'"org_level_disabled","isUsingOverage":false,"unifiedWindows":{"five_hour":'
        b'{"utilization":0.64,"resetsAt":1793473200,"status":"allowed"},"seven_day":'
        b'{"utilization":0.34,"resetsAt":1793826000,"status":"allowed"}}},'
        b'"uuid":"7f634bd4-be6c-4cf3-8109-ca2689f73e81","session_id":"e1e4efcb"}\n'
    )
    assert tap.windows_from_rate_limit_info(tap.extract_rate_limit_info(line)) == {
        "five_hour": {"used_percentage": 64.0, "resets_at": 1793473200},
        "seven_day": {"used_percentage": 34.0, "resets_at": 1793826000},
    }
    # A user message merely mentioning the event name is not an event.
    assert tap.extract_rate_limit_info(b'{"type":"user","text":"rate_limit_event"}\n') is None


def test_save_merges_into_existing_status_file(tmp_path: Path) -> None:
    status = tmp_path / "usage-status.json"
    status.write_text(
        json.dumps(
            {
                "session_id": "tui",
                "context_window": {"used_percentage": 10},
                "rate_limits": {"seven_day": {"used_percentage": 20, "resets_at": 1}},
            }
        )
    )
    tap.save_rate_limits(
        {"five_hour": {"used_percentage": 64.0, "resets_at": 2}},
        status_file=str(status),
        lock_file=str(tmp_path / "usage-status.lock"),
        now=1793400000.0,
    )
    data = json.loads(status.read_text())
    assert data["session_id"] == "tui"
    assert data["context_window"] == {"used_percentage": 10}
    assert data["rate_limits"] == {
        "five_hour": {"used_percentage": 64.0, "resets_at": 2},
        "seven_day": {"used_percentage": 20, "resets_at": 1},
    }
    assert data["_received_at_ts"] == 1793400000.0
    assert data["_source"] == "acp"


def test_save_creates_missing_file(tmp_path: Path) -> None:
    status = tmp_path / "usage-status.json"
    tap.save_rate_limits(
        tap.windows_from_rate_limit_info(REAL_INFO),
        status_file=str(status),
        lock_file=str(tmp_path / "usage-status.lock"),
        now=1.0,
    )
    data = json.loads(status.read_text())
    assert data["rate_limits"]["five_hour"]["used_percentage"] == 64.0
    assert data["rate_limits"]["seven_day"]["used_percentage"] == 34.0


def test_relay_passes_bytes_through_unchanged_and_records(tmp_path: Path) -> None:
    lines = [
        b'{"jsonrpc":"2.0","id":0,"result":{"x":"\xe4\xb8\xad"}}\n',
        _usage_update_line(REAL_INFO),
        b"tail-without-newline",
    ]
    child = tmp_path / "child.py"
    child.write_text(
        "import sys\n"
        f"sys.stdout.buffer.write({b''.join(lines)!r})\n"
        "sys.stdout.flush()\n"
        "sys.exit(3)\n"
    )
    status = tmp_path / "usage-status.json"
    env = {"USAGE_ACP_TAP_STATUS_FILE": str(status), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, str(Path(tap.__file__)), sys.executable, str(child)],
        input=b"",
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert result.stdout == b"".join(lines)
    assert result.returncode == 3
    assert json.loads(status.read_text())["rate_limits"]["five_hour"]["used_percentage"] == 64.0
