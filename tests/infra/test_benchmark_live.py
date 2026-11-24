"""Offline unit checks for ops/benchmark_live.py."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from ops import benchmark_live


def test_percentile_and_secret_redaction():
    assert benchmark_live.percentile([], 95) == 0.0
    assert benchmark_live.percentile([42.0], 95) == 42.0
    assert benchmark_live.percentile([10.0, 20.0, 30.0, 40.0, 100.0], 95) == 88.0
    secret = "deadbeef" * 8
    payload = f'{{"cookie": "{secret}", "ok": true}}'
    assert secret not in benchmark_live.redact_secret(payload, secret)
    assert "[REDACTED]" in benchmark_live.redact_secret(payload, secret)


def test_render_markdown_and_compare_reports():
    before = {
        "measured_at": "2026-11-24T01:00:00Z",
        "base_url": "https://anime.jhinx.dev",
        "http": [
            {
                "route": "/plus/requests",
                "status": 200,
                "bytes": 12000,
                "cold_ms": 950.0,
                "warm_p50_ms": 400.0,
                "warm_p95_ms": 480.0,
                "warm_max_ms": 500.0,
            }
        ],
        "browser": {
            "pages": [
                {
                    "route": "/plus/requests",
                    "ttfb_ms": 380.0,
                    "fcp_ms": 620.0,
                    "lcp_ms": 1800.0,
                    "cls": 0.012,
                    "load_ms": 2100.0,
                    "resource_count": 12,
                    "transfer_bytes": 95000,
                    "overflow_px": 0,
                }
            ]
        },
    }
    after = {
        "measured_at": "2026-11-24T02:00:00Z",
        "base_url": "https://anime.jhinx.dev",
        "http": [
            {
                "route": "/plus/requests",
                "status": 200,
                "bytes": 11500,
                "cold_ms": 320.0,
                "warm_p50_ms": 150.0,
                "warm_p95_ms": 190.0,
                "warm_max_ms": 210.0,
            }
        ],
        "browser": {
            "pages": [
                {
                    "route": "/plus/requests",
                    "ttfb_ms": 140.0,
                    "fcp_ms": 310.0,
                    "lcp_ms": 900.0,
                    "cls": 0.0,
                    "load_ms": 1100.0,
                    "resource_count": 10,
                    "transfer_bytes": 80000,
                    "overflow_px": 0,
                }
            ]
        },
    }
    md = benchmark_live.render_markdown(before)
    assert "## Authenticated Server Latencies" in md
    assert "| `/plus/requests` | 200 |" in md
    assert "## Headless Edge Web Vitals" in md

    diff = benchmark_live.compare_reports(before, after)
    assert "-250.0 ms (-62.5%)" in diff
    assert "1800.0 ms" in diff and "900.0 ms" in diff


def test_temporary_session_is_always_revoked_on_failure(monkeypatch):
    events = []

    monkeypatch.setattr(
        benchmark_live,
        "mint_temp_session",
        lambda host, uid: events.append(("mint", host, uid)) or "ephemeral-secret-token",
    )

    def broken(*_args, **_kwargs):
        raise RuntimeError("network boom")

    monkeypatch.setattr(benchmark_live, "measure_http_routes", broken)
    monkeypatch.setattr(
        benchmark_live,
        "revoke_temp_session",
        lambda host, token: events.append(("revoke", host, token)) or 0,
    )
    args = argparse.Namespace(
        base_url="https://anime.jhinx.dev",
        ssh_host="omakase-ct",
        user_id=2,
        cookie=None,
        warm_runs=2,
        no_browser=True,
        edge_path=None,
        screenshot_dir=None,
    )
    with pytest.raises(RuntimeError, match="network boom"):
        benchmark_live.run_benchmark(args)
    assert events == [("mint", "omakase-ct", 2), ("revoke", "omakase-ct", "ephemeral-secret-token")]


def test_run_benchmark_strips_ephemeral_token_if_echoed(monkeypatch):
    monkeypatch.setattr(benchmark_live, "mint_temp_session", lambda *_: "secret-abc-123")
    monkeypatch.setattr(
        benchmark_live,
        "measure_http_routes",
        lambda *_, **__: [
            {
                "route": "/plus/home",
                "status": 200,
                "warm_statuses": [200],
                "bytes": 100,
                "cold_ms": 10.0,
                "warm_min_ms": 5.0,
                "warm_p50_ms": 6.0,
                "warm_p95_ms": 9.0,
                "warm_max_ms": 9.0,
                "cache_control": "secret-abc-123",
            }
        ],
    )
    monkeypatch.setattr(benchmark_live, "revoke_temp_session", lambda *_: 0)
    args = SimpleNamespace(
        base_url="https://anime.jhinx.dev",
        ssh_host="omakase-ct",
        user_id=2,
        cookie=None,
        warm_runs=2,
        no_browser=True,
        edge_path=None,
        screenshot_dir=None,
    )
    report = benchmark_live.run_benchmark(args)
    assert report["http"][0]["cache_control"] == "[REDACTED]"
