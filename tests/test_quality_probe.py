# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quality_probe import (
    DEFAULT_PROMPT,
    apply_quality_fields,
    classify_failure_kind,
    classify_sample,
    load_auth_records,
    parse_sse_quality,
    probe_account,
    public_row,
    run_quality_scan,
    stamp_quality_on_record,
)
from webui import quality_ops


class FakeResp:
    def __init__(self, status=200, lines=None, text=""):
        self.status_code = status
        self._lines = list(lines or [])
        self.text = text

    def iter_lines(self):
        for line in self._lines:
            yield line

    def close(self):
        return None


def sse(*payloads: dict, done: bool = True) -> list[str]:
    lines = [f"data: {json.dumps(item, ensure_ascii=False)}" for item in payloads]
    if done:
        lines.append("data: [DONE]")
    return lines


def test_default_prompt_is_short_but_not_trivial_arithmetic():
    text = DEFAULT_PROMPT.lower()
    assert "quality_ok" in text
    assert "17*23" not in text
    assert "clock" in text or "angle" in text
    assert len(DEFAULT_PROMPT) < 400


def test_classify_sample_thinking_and_tps():
    assert classify_sample(10, 80, True, 4000) == "healthy"
    assert classify_sample(250, 80, True, 4000) == "healthy"
    assert classify_sample(1200, 80, True, 4000) == "healthy"
    assert classify_sample(10, 80, False, 4000) == "hard"
    assert classify_sample(10, 7, True, 4000) == "healthy"
    assert classify_sample(10, 8, True, 4000) == "healthy"
    assert classify_sample(400, 80, True, 200) == "healthy"
    assert classify_sample(10, 80, False, 4000, require_thinking=False) == "healthy"


def test_classify_failure_kind_account_vs_transport():
    assert classify_failure_kind(403, "permission-denied") == "account_error"
    assert classify_failure_kind(401, "") == "account_error"
    assert classify_failure_kind(407, "proxy") == "transport_error"
    assert classify_failure_kind(502, "bad gateway") == "upstream_error"


def test_parse_sse_quality_detects_thinking():
    parsed = parse_sse_quality(
        sse(
            {"choices": [{"delta": {"thinking_content": "plan"}}]},
            {"choices": [{"delta": {"content": "TCP slow start is a congestion control mechanism. " * 4}}]},
            {"usage": {"completion_tokens": 64, "reasoning_tokens": 12}},
        )
    )
    assert parsed["has_thinking"] is True
    assert parsed["usage_out"] == 64
    assert parsed["usage_reason"] == 12
    assert "TCP" in parsed["preview"]


def test_probe_account_healthy_and_risk(monkeypatch_clock=None):
    times = iter([0.0, 0.2, 2.2, 2.2, 2.2])

    def fake_clock():
        try:
            return next(times)
        except StopIteration:
            return 2.2

    record = {
        "email": "ok@example.test",
        "access_token": "token-ok",
        "base_url": "https://cli-chat-proxy.grok.com/v1",
        "headers": {},
    }

    def healthy_post(_url, **_kwargs):
        return FakeResp(
            200,
            sse(
                {"choices": [{"delta": {"thinking_content": "step"}}]},
                {"choices": [{"delta": {"content": "A" * 160}}]},
                {"usage": {"completion_tokens": 80, "reasoning_tokens": 16}},
            ),
        )

    healthy = probe_account(
        record,
        post_fn=healthy_post,
        monotonic=fake_clock,
        early_stop_on_thinking=False,
    )
    assert healthy["verdict"] == "healthy"
    assert healthy["has_thinking"] is True
    assert healthy["output_tokens"] == 80
    assert healthy["early_stop"] is False

    def denied_post(_url, **_kwargs):
        return FakeResp(403, text='{"error":"permission-denied"}')

    denied = probe_account(record, post_fn=denied_post, monotonic=lambda: 1.0)
    assert denied["verdict"] == "risk"
    assert denied["error_kind"] == "account_error"


def test_probe_account_early_stops_after_thinking():
    lines = sse(
        {"choices": [{"delta": {"thinking_content": "plan"}}]},
        {"choices": [{"delta": {"content": "A" * 80}}]},
        {"choices": [{"delta": {"content": "B" * 80}}]},
        {"usage": {"completion_tokens": 80, "reasoning_tokens": 16}},
    )
    resp = FakeResp(200, lines)
    original_iter = resp.iter_lines

    def counting_iter():
        for line in original_iter():
            resp.reads = getattr(resp, "reads", 0) + 1
            yield line

    resp.iter_lines = counting_iter
    record = {"email": "early@example.test", "access_token": "tok"}

    probed = probe_account(
        record,
        post_fn=lambda *_a, **_k: resp,
        monotonic=lambda: 1.0,
        early_stop_on_thinking=True,
        early_stop_ms=0,
    )
    assert probed["verdict"] == "healthy"
    assert probed["has_thinking"] is True
    assert probed["early_stop"] is True
    assert getattr(resp, "reads", 0) == 1


def test_stamp_quality_on_record_writes_meta_not_token():
    record = {
        "email": "meta@example.test",
        "access_token": "secret-token",
        "base_url": "https://cli-chat-proxy.grok.com/v1",
    }

    def post(_url, **_kwargs):
        return FakeResp(
            200,
            sse(
                {"choices": [{"delta": {"thinking_content": "step"}}]},
                {"choices": [{"delta": {"content": "391"}}]},
            ),
        )

    probed = stamp_quality_on_record(
        record,
        post_fn=post,
        monotonic=lambda: 1.0,
        early_stop_on_thinking=True,
        early_stop_ms=0,
    )
    assert probed["verdict"] == "healthy"
    assert record["quality_verdict"] == "healthy"
    assert record["quality_has_thinking"] is True
    assert record["quality_early_stop"] is True
    assert "secret-token" not in json.dumps(apply_quality_fields({}, probed))
    assert record["access_token"] == "secret-token"


def test_run_quality_scan_exports_redacted_jsonl():
    records = [
        {
            "email": "good@example.test",
            "access_token": "tok-good",
            "_file": "xai-good.json",
        },
        {
            "email": "dumb@example.test",
            "access_token": "tok-dumb",
            "_file": "xai-dumb.json",
        },
        {
            "email": "blocked@example.test",
            "access_token": "tok-block",
            "_file": "xai-block.json",
        },
    ]

    def post(_url, **kwargs):
        token = kwargs["headers"]["Authorization"]
        if "tok-block" in token:
            return FakeResp(403, text="permission-denied")
        if "tok-dumb" in token:
            return FakeResp(
                200,
                sse(
                    {"choices": [{"delta": {"content": "B" * 200}}]},
                    {"usage": {"completion_tokens": 80, "reasoning_tokens": 0}},
                ),
            )
        return FakeResp(
            200,
            sse(
                {"choices": [{"delta": {"thinking_content": "think"}}]},
                {"choices": [{"delta": {"content": "C" * 200}}]},
                {"usage": {"completion_tokens": 80, "reasoning_tokens": 20}},
            ),
        )

    with tempfile.TemporaryDirectory() as temp:
        degraded = Path(temp) / "degraded.jsonl"
        risk = Path(temp) / "risk.jsonl"
        ticks = {"n": 0}

        def fake_clock():
            ticks["n"] += 1
            return float(ticks["n"])

        summary = run_quality_scan(
            records,
            ["http://127.0.0.1:8001"],
            workers=1,
            export=degraded,
            risk_export=risk,
            log=lambda *_a, **_k: None,
            post_fn=post,
            monotonic=fake_clock,
        )
        assert summary["total"] == 3
        assert summary["healthy_count"] == 1
        assert summary["hard_count"] == 1
        assert summary["risk_count"] == 1
        assert summary["degraded_count"] == 1
        degraded_text = degraded.read_text(encoding="utf-8")
        risk_text = risk.read_text(encoding="utf-8")
        assert "tok-dumb" not in degraded_text
        assert "tok-block" not in risk_text
        assert "good@example.test" not in degraded_text
        row = public_row(summary["items"][0])
        assert "access_token" not in row
        assert "***@" in row["email"] or row["email"].endswith("@example.test")


def test_load_auth_records_and_quality_ops_status(tmp_path, monkeypatch=None):
    folder = tmp_path if hasattr(tmp_path, "write_text") else Path(tempfile.mkdtemp())
    auth = folder / "xai-demo.json"
    auth.write_text(
        json.dumps(
            {
                "email": "demo@example.test",
                "access_token": "secret-token",
                "base_url": "https://cli-chat-proxy.grok.com/v1",
            }
        ),
        encoding="utf-8",
    )
    loaded = load_auth_records([folder])
    assert len(loaded) == 1
    assert loaded[0]["email"] == "demo@example.test"

    previous = (
        quality_ops.CPA_DIR,
        quality_ops.G2A_DIR,
        quality_ops.CONFIG_FILE,
        quality_ops.LOG_DIR,
        quality_ops.REPORT_FILE,
        quality_ops.DEGRADED_EXPORT,
        quality_ops.RISK_EXPORT,
    )
    quality_ops.CPA_DIR = folder
    quality_ops.G2A_DIR = folder / "missing"
    quality_ops.CONFIG_FILE = folder / "config.json"
    quality_ops.LOG_DIR = folder / "log"
    quality_ops.REPORT_FILE = quality_ops.LOG_DIR / "quality_scan_report.json"
    quality_ops.DEGRADED_EXPORT = quality_ops.LOG_DIR / "quality_degraded.jsonl"
    quality_ops.RISK_EXPORT = quality_ops.LOG_DIR / "quality_risk.jsonl"
    quality_ops.LOG_DIR.mkdir(exist_ok=True)
    try:
        status = quality_ops.quality_status()
        assert status["ok"] is True
        assert status["sources"]["cpa"] == 1
        assert "secret-token" not in json.dumps(status)
    finally:
        (
            quality_ops.CPA_DIR,
            quality_ops.G2A_DIR,
            quality_ops.CONFIG_FILE,
            quality_ops.LOG_DIR,
            quality_ops.REPORT_FILE,
            quality_ops.DEGRADED_EXPORT,
            quality_ops.RISK_EXPORT,
        ) = previous


if __name__ == "__main__":
    test_default_prompt_is_short_but_not_trivial_arithmetic()
    test_classify_sample_thinking_and_tps()
    test_classify_failure_kind_account_vs_transport()
    test_parse_sse_quality_detects_thinking()
    test_probe_account_healthy_and_risk()
    test_probe_account_early_stops_after_thinking()
    test_stamp_quality_on_record_writes_meta_not_token()
    test_run_quality_scan_exports_redacted_jsonl()
    with tempfile.TemporaryDirectory() as temp:
        test_load_auth_records_and_quality_ops_status(Path(temp))
    print("OK quality probe")
