#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>
#
# Part of "usage". Free software licensed under the GNU Affero General Public
# License v3.0 only; see the LICENSE file for full terms and the warranty disclaimer.

# ruff: noqa: UP006, UP017, UP035, UP045
"""ACP 額度旁聽器：讓 Zed 等 ACP 客戶端裡的 Claude Agent 也能更新 usage-status.json。

ACP 模式的 Claude（claude-agent-acp → Agent SDK → claude stream-json）沒有終端畫面，
statusLine 永遠不會被呼叫；hook 的 payload 也沒有 rate_limits。claude 本身會在
stream-json 輸出 ``rate_limit_event``，但它在第一個回覆之前就送出，
claude-agent-acp 0.99.1 因此把它丟掉，客戶端收不到。

用法：讓 Agent SDK 透過 ``CLAUDE_CODE_EXECUTABLE`` 啟動一支包裝 script，
包裝 script 再 ``exec`` 這支程式並交出真正的 claude 路徑，例如
    exec /usr/bin/python3 ~/.claude/usage-acp-tap.py /path/to/claude "$@"
也可以直接包 ACP 轉接層（讀 ``usage_update`` 的 ``_meta["_claude/rateLimit"]``），
但那層會漏掉上面那筆事件。

stdin / stderr 直接交給子程序；stdout 逐位元組原樣轉送，順便讀出額度寫檔。
不發任何網路請求。任何解析或寫檔錯誤都吞掉，絕不影響轉送。

刻意只用標準庫，方便用系統 python3 跑。
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

STATUS_FILE = os.path.expanduser("~/.claude/usage-status.json")
LOCK_FILE = os.path.expanduser("~/.claude/usage-status.lock")
_LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_INTERVAL_S = 0.001
_MARKERS = (b"rate_limit_event", b"_claude/rateLimit")
_WINDOWS = ("five_hour", "seven_day")


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _window(utilization: Any, resets_at: Any) -> Optional[Dict[str, Any]]:
    fraction = _finite(utilization)
    if fraction is None:
        return None
    entry: Dict[str, Any] = {"used_percentage": round(fraction * 100, 1)}
    reset = _finite(resets_at)
    if reset is not None:
        entry["resets_at"] = int(reset)
    return entry


def windows_from_rate_limit_info(info: Any) -> Dict[str, Dict[str, Any]]:
    """Convert SDK rate_limit_info (utilization 0-1) to usage-status rate_limits (0-100)."""
    if not isinstance(info, dict):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for name in _WINDOWS:
            raw = unified.get(name)
            if isinstance(raw, dict):
                entry = _window(raw.get("utilization"), raw.get("resetsAt"))
                if entry is not None:
                    result[name] = entry
    if not result and info.get("rateLimitType") in _WINDOWS:
        entry = _window(info.get("utilization"), info.get("resetsAt"))
        if entry is not None:
            result[str(info["rateLimitType"])] = entry
    return result


def _has_marker(line: bytes) -> bool:
    return any(marker in line for marker in _MARKERS)


def extract_rate_limit_info(line: bytes) -> Optional[Dict[str, Any]]:
    """Read rate_limit_info from a raw claude stream-json line or an ACP usage_update."""
    if not _has_marker(line):
        return None
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    info: Any = None
    if message.get("type") == "rate_limit_event":
        info = message.get("rate_limit_info")
    else:
        try:
            info = message["params"]["update"]["_meta"]["_claude/rateLimit"]
        except (KeyError, TypeError):
            return None
    return info if isinstance(info, dict) else None


@contextmanager
def _exclusive_lock(lock_fd: int) -> Iterator[None]:
    """Same lock file and protocol as usage_statusline.py, so writers never interleave."""
    locked = False
    if fcntl is not None:
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_INTERVAL_S)
    try:
        yield
    finally:
        if locked and fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def save_rate_limits(
    windows: Dict[str, Dict[str, Any]],
    *,
    status_file: str = STATUS_FILE,
    lock_file: str = LOCK_FILE,
    now: Optional[float] = None,
) -> None:
    """Merge windows into the status file, keeping every other field the statusLine wrote."""
    if not windows:
        return
    ts = time.time() if now is None else now
    target_dir = os.path.dirname(status_file)
    os.makedirs(target_dir, exist_ok=True)
    tmp_path: Optional[str] = None
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with _exclusive_lock(lock_fd):
            try:
                with open(status_file, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, UnicodeDecodeError, ValueError):
                data = None
            if not isinstance(data, dict):
                data = {}
            rate_limits = data.get("rate_limits")
            if not isinstance(rate_limits, dict):
                rate_limits = {}
            rate_limits.update(windows)
            data["rate_limits"] = rate_limits
            data["_received_at"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
            data["_received_at_ts"] = ts
            data["_source"] = "acp"
            fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, status_file)
            tmp_path = None
    finally:
        os.close(lock_fd)
        if tmp_path and os.path.exists(tmp_path):
            with suppress(OSError):
                os.unlink(tmp_path)


def _record(line: bytes, status_file: str) -> None:
    try:
        windows = windows_from_rate_limit_info(extract_rate_limit_info(line))
        if windows:
            save_rate_limits(
                windows,
                status_file=status_file,
                lock_file=os.path.join(os.path.dirname(status_file), os.path.basename(LOCK_FILE)),
            )
    except Exception:  # noqa: BLE001 - the relay must never die because of bookkeeping
        if os.environ.get("USAGE_DEBUG") == "1":
            import traceback

            traceback.print_exc(file=sys.stderr)


def main(argv: List[str]) -> int:
    if not argv:
        sys.stderr.write("usage: usage-acp-tap.py <agent command> [args...]\n")
        return 2
    status_file = os.environ.get("USAGE_ACP_TAP_STATUS_FILE") or STATUS_FILE
    child = subprocess.Popen(argv, stdin=None, stdout=subprocess.PIPE, stderr=None)

    def _forward(signum: int, _frame: Any) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _forward)

    out = sys.stdout.buffer
    assert child.stdout is not None
    try:
        for line in iter(child.stdout.readline, b""):
            out.write(line)
            out.flush()
            if _has_marker(line):
                _record(line, status_file)
    except BrokenPipeError:
        # The client went away; don't leave the agent running.
        if child.poll() is None:
            child.terminate()
    code = child.wait()
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
