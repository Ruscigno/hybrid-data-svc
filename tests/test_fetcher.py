"""
Tests for data_svc.fetcher JSON parsing — BUG 1 (tv CLI returned non-JSON).

These cover _parse_tv_json / _dump_tv_failure, the observability + tolerant-parse
half of the BUG_tv_non_json.md fix. Pure functions: no DB, no subprocess — the
tv CLI output is supplied as a fake subprocess.CompletedProcess.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from data_svc import fetcher
from data_svc.fetcher import DataFetchError, _parse_tv_json


def _result(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Build a fake CompletedProcess mimicking a `tv` CLI invocation."""
    return subprocess.CompletedProcess(
        args=["tv", "ohlcv", "-n", "500"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture
def tmp_dump(tmp_path, monkeypatch):
    """Redirect failure dumps into a per-test temp dir."""
    monkeypatch.setattr(fetcher.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def test_valid_json_parses(tmp_dump):
    result = _result(stdout='{"success": true, "bar_count": 3}')
    payload = _parse_tv_json(result, context="ohlcv")
    assert payload == {"success": True, "bar_count": 3}


def test_surrounding_whitespace_is_stripped(tmp_dump):
    result = _result(stdout='\n   {"success": true}\n\n')
    assert _parse_tv_json(result, context="ohlcv")["success"] is True


def test_trailing_garbage_is_tolerated(tmp_dump, caplog):
    """A stray log line appended after the JSON must not break the parse."""
    result = _result(stdout='{"success": true}\n[cdp_discover] stray line\n')
    with caplog.at_level(logging.WARNING):
        payload = _parse_tv_json(result, context="ohlcv")
    assert payload == {"success": True}
    assert any("trailing byte" in r.message for r in caplog.records)


def test_truncated_json_raises_with_diagnostics(tmp_dump):
    """The real BUG 1 symptom: a large payload cut off mid-stream."""
    truncated = '{"success": true, "bar_count": 500, "bars": [{"time": 17786'
    with pytest.raises(DataFetchError) as excinfo:
        _parse_tv_json(_result(stdout=truncated), context="ohlcv")
    msg = str(excinfo.value)
    assert "non-JSON" in msg
    assert "pos=" in msg                         # decode position reported
    assert f"{len(truncated)} bytes" in msg      # full length, not [:200]
    assert "dumped to" in msg


def test_empty_stdout_raises(tmp_dump):
    result = _result(stdout="", stderr="boom", returncode=2)
    with pytest.raises(DataFetchError, match="empty stdout"):
        _parse_tv_json(result, context="ohlcv")


def test_failure_writes_full_dump(tmp_dump):
    """On a hard failure the complete stdout+stderr are persisted to disk."""
    bad_stdout = '{"truncated": '
    with pytest.raises(DataFetchError):
        _parse_tv_json(
            _result(stdout=bad_stdout, stderr="some stderr noise"),
            context="ohlcv",
        )
    dumps = list(tmp_dump.glob("tv_fail_ohlcv_*.txt"))
    assert len(dumps) == 1
    content = dumps[0].read_text()
    assert bad_stdout in content
    assert "some stderr noise" in content
