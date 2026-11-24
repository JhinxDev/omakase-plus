"""Measure authenticated live server latencies and headless Edge Web Vitals.

Creates a short-lived session for a single inspection pass (unless --cookie is supplied),
records cold + warm HTTP response times across primary routes plus real browser TTFB, FCP,
LCP, CLS, and slow resources over Edge CDP, revokes the temporary session in a finally
block, and supports --compare BEFORE.json AFTER.json diff tables.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets

CORE_ROUTES = (
    "/plus/health",
    "/plus/home",
    "/plus/library",
    "/plus/movies",
    "/plus/tv",
    "/plus/discover",
    "/plus/discover/rows",
    "/plus/discover/rows?kind=movies",
    "/plus/discover/rows?kind=tv",
    "/plus/requests",
    "/plus/requests/status",
    "/plus/activity",
    "/plus/settings",
)

BROWSER_ROUTES = (
    "/plus/home",
    "/plus/library",
    "/plus/movies",
    "/plus/tv",
    "/plus/discover",
    "/plus/requests",
    "/plus/activity",
)

_MOVIE_LINK = re.compile(r'href="(/plus/movies/tt[0-9]{7,10})"')
_TV_LINK = re.compile(r'href="(/plus/tv/[1-9][0-9]{0,9})"')

_VITALS_PRELOAD_JS = """
window.__omakaseVitals = { lcp: null, lcpElement: null, cls: 0 };
try {
  new PerformanceObserver((list) => {
    const entries = list.getEntries();
    const last = entries[entries.length - 1];
    if (last) {
      window.__omakaseVitals.lcp = Math.round(last.startTime * 10) / 10;
      const el = last.element;
      window.__omakaseVitals.lcpElement = el
        ? el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).trim().split(/\\s+/)[0] : '')
        : null;
    }
  }).observe({ type: 'largest-contentful-paint', buffered: true });
} catch {}
try {
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (!entry.hadRecentInput) {
        window.__omakaseVitals.cls += entry.value;
      }
    }
  }).observe({ type: 'layout-shift', buffered: true });
} catch {}
"""

_COLLECT_METRICS_JS = """
(() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const paints = {};
  for (const p of performance.getEntriesByType('paint')) {
    paints[p.name] = Math.round(p.startTime * 10) / 10;
  }
  const resources = performance.getEntriesByType('resource').map((r) => {
    let cleanPath = r.name;
    try {
      const u = new URL(r.name);
      cleanPath = u.origin === location.origin ? u.pathname : (u.host + u.pathname);
    } catch {}
    return {
      path: cleanPath,
      initiator: r.initiatorType || 'other',
      duration_ms: Math.round(r.duration * 10) / 10,
      transfer_bytes: Number(r.transferSize || 0),
    };
  });
  resources.sort((a, b) => b.duration_ms - a.duration_ms);
  const transferBytes = resources.reduce((acc, item) => acc + item.transfer_bytes, 0)
    + Number(nav ? nav.transferSize || 0 : 0);
  const v = window.__omakaseVitals || { lcp: null, lcpElement: null, cls: 0 };
  return {
    final_path: location.pathname + location.search,
    ttfb_ms: nav ? Math.round((nav.responseStart - nav.requestStart) * 10) / 10 : null,
    dcl_ms: nav ? Math.round((nav.domContentLoadedEventEnd - nav.startTime) * 10) / 10 : null,
    load_ms: nav ? Math.round((nav.loadEventEnd - nav.startTime) * 10) / 10 : null,
    fcp_ms: paints['first-contentful-paint'] ?? null,
    lcp_ms: v.lcp,
    lcp_element: v.lcpElement,
    cls: Math.round((v.cls || 0) * 10000) / 10000,
    resource_count: resources.length,
    transfer_bytes: transferBytes,
    overflow_px: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
    top_slow_resources: resources.slice(0, 5),
  };
})()
"""


def percentile(values: list[float], pct: float) -> float:
    """Return linear-interpolated percentile from a non-empty list of floats."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(ordered[int(rank)], 1)
    fraction = rank - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * fraction, 1)


def redact_secret(text: str, secret: str | None) -> str:
    """Strip an ephemeral session secret if present anywhere in text output."""
    if secret and secret in text:
        return text.replace(secret, "[REDACTED]")
    return text


def find_edge_binary(explicit: str | None = None) -> str | None:
    """Locate an installed Microsoft Edge binary on Windows or PATH."""
    if explicit:
        path = Path(explicit)
        return str(path) if path.is_file() else None
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    for name in ("msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _remote_python(ssh_host: str, script: str) -> str:
    completed = subprocess.run(
        ["ssh", ssh_host, "docker", "exec", "-i", "omakase-plus", "python", "-"],
        input=script.replace("\r\n", "\n"),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Remote python exited {completed.returncode}: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def mint_temp_session(ssh_host: str, user_id: int, *, ttl_minutes: int = 10) -> str:
    """Create a short-lived authenticated session inside the CT 101 container as uid 1000."""
    token = secrets.token_hex(32)
    expires_at = (datetime.now(UTC) + timedelta(minutes=ttl_minutes)).isoformat()
    script = (
        "import sqlite3\n"
        "from omakase.plus import db as plus_db\n"
        "conn = sqlite3.connect(str(plus_db._database_path(None)))\n"
        "conn.execute('PRAGMA busy_timeout = 5000')\n"
        f"conn.execute('INSERT INTO sessions(id, user_id, expires_at) VALUES (?, ?, ?)', ({token!r}, {int(user_id)}, {expires_at!r}))\n"
        "conn.commit()\n"
        "conn.close()\n"
        "print('MINTED_OK')\n"
    )
    out = _remote_python(ssh_host, script)
    if "MINTED_OK" not in out:
        raise RuntimeError("Failed to mint temporary session cleanly.")
    return token


def revoke_temp_session(ssh_host: str, token: str) -> int:
    """Delete a previously minted temporary session and return remaining matching row count."""
    script = (
        "import sqlite3\n"
        "from omakase.plus import db as plus_db\n"
        "conn = sqlite3.connect(str(plus_db._database_path(None)))\n"
        "conn.execute('PRAGMA busy_timeout = 5000')\n"
        f"conn.execute('DELETE FROM sessions WHERE id = ?', ({token!r},))\n"
        "conn.commit()\n"
        f"remaining = conn.execute('SELECT COUNT(*) FROM sessions WHERE id = ?', ({token!r},)).fetchone()[0]\n"
        "conn.close()\n"
        "print(remaining)\n"
    )
    out = _remote_python(ssh_host, script)
    return int(out.splitlines()[-1].strip())


def measure_http_routes(
    base_url: str,
    cookie_value: str,
    *,
    warm_runs: int = 5,
    routes: tuple[str, ...] = CORE_ROUTES,
) -> list[dict[str, Any]]:
    """Measure 1 cold request and N warm requests across authenticated server routes."""
    client_routes = list(routes)
    results: list[dict[str, Any]] = []
    discovered: list[str] = []
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        cookies={"omakase_session": cookie_value},
        follow_redirects=False,
        timeout=30.0,
    ) as client:
        index = 0
        while index < len(client_routes):
            route = client_routes[index]
            index += 1
            start = time.perf_counter()
            cold = client.get(route)
            cold_ms = round((time.perf_counter() - start) * 1000.0, 1)
            body = cold.text if cold.status_code == 200 else ""
            if route == "/plus/movies" and body:
                match = _MOVIE_LINK.search(body)
                if match and match.group(1) not in client_routes:
                    discovered.append(match.group(1))
                    client_routes.append(match.group(1))
            elif route == "/plus/tv" and body:
                match = _TV_LINK.search(body)
                if match and match.group(1) not in client_routes:
                    discovered.append(match.group(1))
                    client_routes.append(match.group(1))

            warm_samples: list[float] = []
            warm_statuses: set[int] = set()
            for _ in range(max(1, warm_runs)):
                t0 = time.perf_counter()
                resp = client.get(route)
                warm_samples.append((time.perf_counter() - t0) * 1000.0)
                warm_statuses.add(resp.status_code)
            results.append(
                {
                    "route": route,
                    "status": cold.status_code,
                    "warm_statuses": sorted(warm_statuses),
                    "bytes": len(cold.content),
                    "cold_ms": cold_ms,
                    "warm_min_ms": round(min(warm_samples), 1),
                    "warm_p50_ms": round(statistics.median(warm_samples), 1),
                    "warm_p95_ms": percentile(warm_samples, 95.0),
                    "warm_max_ms": round(max(warm_samples), 1),
                    "cache_control": cold.headers.get("cache-control", ""),
                }
            )
    return results


class _CDPSession:
    """Minimal asyncio CDP client over websockets."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 1
        self._events: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=30.0)
            msg = json.loads(raw)
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})
            self._events.append(msg)

    async def wait_event(self, method: str, *, timeout: float = 30.0) -> dict[str, Any]:
        for idx, ev in enumerate(self._events):
            if ev.get("method") == method:
                return self._events.pop(idx)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for CDP event {method}")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            if msg.get("method") == method:
                return msg
            self._events.append(msg)

    def clear_events(self) -> None:
        self._events.clear()


def _wait_devtools_port(user_data_dir: Path, timeout: float = 15.0) -> int:
    port_file = user_data_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_file.is_file():
            lines = port_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            if lines and lines[0].strip().isdigit():
                return int(lines[0].strip())
        time.sleep(0.1)
    raise TimeoutError("Timed out waiting for Edge DevToolsActivePort file.")


async def _measure_browser_async(
    edge_path: str,
    base_url: str,
    cookie_value: str,
    *,
    routes: tuple[str, ...] = BROWSER_ROUTES,
    screenshot_dir: Path | None = None,
) -> dict[str, Any]:
    parsed = urlparse(base_url)
    host = parsed.hostname or "localhost"
    secure = parsed.scheme == "https"
    tmpdir = Path(tempfile.mkdtemp(prefix="omakase-edge-bench-"))
    proc = subprocess.Popen(
        [
            edge_path,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--remote-debugging-port=0",
            f"--user-data-dir={tmpdir}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pages: list[dict[str, Any]] = []
    screenshots: dict[str, str] = {}
    mobile_requests_overflow = None
    try:
        port = _wait_devtools_port(tmpdir)
        async with httpx.AsyncClient(timeout=10.0) as client:
            targets_resp = await client.get(f"http://127.0.0.1:{port}/json/list")
            targets = targets_resp.json()
            page_target = next((t for t in targets if t.get("type") == "page"), None)
            if page_target is None:
                created = await client.put(f"http://127.0.0.1:{port}/json/new?about:blank")
                page_target = created.json()
            ws_url = page_target["webSocketDebuggerUrl"]

        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
            cdp = _CDPSession(ws)
            await cdp.call("Page.enable")
            await cdp.call("Network.enable")
            await cdp.call("Runtime.enable")
            await cdp.call(
                "Network.setCookie",
                {
                    "name": "omakase_session",
                    "value": cookie_value,
                    "domain": host,
                    "path": "/",
                    "secure": secure,
                    "httpOnly": True,
                },
            )
            await cdp.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
            )
            await cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": _VITALS_PRELOAD_JS})

            for route in routes:
                cdp.clear_events()
                await cdp.call("Page.navigate", {"url": f"{base_url.rstrip('/')}{route}"})
                await cdp.wait_event("Page.loadEventFired", timeout=30.0)
                await asyncio.sleep(1.5)
                evaluated = await cdp.call(
                    "Runtime.evaluate",
                    {"expression": _COLLECT_METRICS_JS, "returnByValue": True},
                )
                metrics = evaluated.get("result", {}).get("value") or {}
                pages.append({"route": route, **metrics})

                if screenshot_dir is not None and route == "/plus/requests":
                    screenshot_dir.mkdir(parents=True, exist_ok=True)
                    desktop_shot = await cdp.call(
                        "Page.captureScreenshot", {"format": "png", "fromSurface": True}
                    )
                    desktop_path = screenshot_dir / "requests-desktop.png"
                    desktop_path.write_bytes(base64.b64decode(desktop_shot["data"]))
                    screenshots["requests_desktop"] = str(desktop_path)

                    await cdp.call(
                        "Emulation.setDeviceMetricsOverride",
                        {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True},
                    )
                    await asyncio.sleep(0.5)
                    mobile_check = await cdp.call(
                        "Runtime.evaluate",
                        {
                            "expression": "Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)",
                            "returnByValue": True,
                        },
                    )
                    mobile_requests_overflow = mobile_check.get("result", {}).get("value")
                    mobile_shot = await cdp.call(
                        "Page.captureScreenshot", {"format": "png", "fromSurface": True}
                    )
                    mobile_path = screenshot_dir / "requests-mobile.png"
                    mobile_path.write_bytes(base64.b64decode(mobile_shot["data"]))
                    screenshots["requests_mobile"] = str(mobile_path)

                    await cdp.call(
                        "Emulation.setDeviceMetricsOverride",
                        {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
                    )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {
        "edge_path": edge_path,
        "pages": pages,
        "screenshots": screenshots,
        "requests_mobile_overflow_px": mobile_requests_overflow,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Format one live benchmark report into readable Markdown tables."""
    lines = [
        f"# Omakase Plus Live Benchmark ({report.get('measured_at', '')})",
        "",
        f"Target: `{report.get('base_url', '')}`",
        "",
        "## Authenticated Server Latencies",
        "",
        "| Route | Status | Size | Cold (ms) | Warm p50 (ms) | Warm p95 (ms) | Warm Max (ms) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.get("http", []):
        lines.append(
            f"| `{row['route']}` | {row['status']} | {row['bytes']:,} B | "
            f"{row['cold_ms']:.1f} | {row['warm_p50_ms']:.1f} | {row['warm_p95_ms']:.1f} | {row['warm_max_ms']:.1f} |"
        )
    browser = report.get("browser") or {}
    if browser.get("pages"):
        lines.extend(
            [
                "",
                "## Headless Edge Web Vitals (1440x900)",
                "",
                "| Route | TTFB (ms) | FCP (ms) | LCP (ms) | CLS | Load (ms) | Resources | Transfer | Overflow |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for page in browser["pages"]:
            lcp = f"{page['lcp_ms']:.1f}" if page.get("lcp_ms") is not None else "n/a"
            fcp = f"{page['fcp_ms']:.1f}" if page.get("fcp_ms") is not None else "n/a"
            ttfb = f"{page['ttfb_ms']:.1f}" if page.get("ttfb_ms") is not None else "n/a"
            load = f"{page['load_ms']:.1f}" if page.get("load_ms") is not None else "n/a"
            lines.append(
                f"| `{page['route']}` | {ttfb} | {fcp} | {lcp} | {page.get('cls', 0):.4f} | "
                f"{load} | {page.get('resource_count', 0)} | {page.get('transfer_bytes', 0):,} B | {page.get('overflow_px', 0)} px |"
            )
    return "\n".join(lines) + "\n"


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Format a Before vs After Markdown comparison table for HTTP and Edge metrics."""
    lines = [
        "# Before vs After Live Benchmark Comparison",
        "",
        f"Before: `{before.get('measured_at', '')}` | After: `{after.get('measured_at', '')}`",
        "",
        "## Server Latencies (Warm p50 & Cold)",
        "",
        "| Route | Before Warm p50 | After Warm p50 | Delta | Before Cold | After Cold |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    before_http = {row["route"]: row for row in before.get("http", [])}
    after_http = {row["route"]: row for row in after.get("http", [])}
    for route in before_http.keys() | after_http.keys():
        if route not in before_http or route not in after_http:
            continue
        b, a = before_http[route], after_http[route]
        diff = a["warm_p50_ms"] - b["warm_p50_ms"]
        pct = ((diff / b["warm_p50_ms"]) * 100.0) if b["warm_p50_ms"] > 0 else 0.0
        lines.append(
            f"| `{route}` | {b['warm_p50_ms']:.1f} ms | {a['warm_p50_ms']:.1f} ms | "
            f"{diff:+.1f} ms ({pct:+.1f}%) | {b['cold_ms']:.1f} ms | {a['cold_ms']:.1f} ms |"
        )

    before_pages = {p["route"]: p for p in (before.get("browser") or {}).get("pages", [])}
    after_pages = {p["route"]: p for p in (after.get("browser") or {}).get("pages", [])}
    if before_pages and after_pages:
        lines.extend(
            [
                "",
                "## Headless Edge Web Vitals (FCP / LCP / CLS)",
                "",
                "| Route | Before FCP | After FCP | Before LCP | After LCP | Before CLS | After CLS |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for route in sorted(before_pages.keys() & after_pages.keys()):
            bp, ap = before_pages[route], after_pages[route]

            def _fmt(val, suffix=" ms"):
                return f"{val:.1f}{suffix}" if val is not None else "n/a"

            lines.append(
                f"| `{route}` | {_fmt(bp.get('fcp_ms'))} | {_fmt(ap.get('fcp_ms'))} | "
                f"{_fmt(bp.get('lcp_ms'))} | {_fmt(ap.get('lcp_ms'))} | "
                f"{bp.get('cls', 0):.4f} | {ap.get('cls', 0):.4f} |"
            )
    return "\n".join(lines) + "\n"


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    """Run authenticated live measurements with guaranteed ephemeral session revocation."""
    token: str | None = args.cookie
    minted = False
    try:
        if not token:
            token = mint_temp_session(args.ssh_host, args.user_id)
            minted = True
        http_results = measure_http_routes(args.base_url, token, warm_runs=args.warm_runs)
        browser_results: dict[str, Any] | None = None
        if not args.no_browser:
            edge = find_edge_binary(args.edge_path)
            if not edge:
                raise RuntimeError("Microsoft Edge executable could not be found.")
            screenshot_dir = Path(args.screenshot_dir) if args.screenshot_dir else None
            browser_results = asyncio.run(
                _measure_browser_async(edge, args.base_url, token, screenshot_dir=screenshot_dir)
            )
        report: dict[str, Any] = {
            "measured_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "base_url": args.base_url,
            "warm_runs": args.warm_runs,
            "http": http_results,
            "browser": browser_results,
        }
        return json.loads(redact_secret(json.dumps(report), token))
    finally:
        if minted and token:
            remaining = revoke_temp_session(args.ssh_host, token)
            if remaining != 0:
                print(
                    f"WARNING: failed to confirm deletion of temporary session ({remaining} remaining)",
                    file=sys.stderr,
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live server and headless Edge Web Vitals benchmark."
    )
    parser.add_argument("--base-url", default="https://anime.jhinx.dev")
    parser.add_argument("--ssh-host", default="omakase-ct")
    parser.add_argument("--user-id", type=int, default=2)
    parser.add_argument("--cookie", default=os.getenv("OMAKASE_BENCH_COOKIE") or None)
    parser.add_argument("--warm-runs", type=int, default=5)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--edge-path", default=None)
    parser.add_argument("--screenshot-dir", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--markdown", default=None)
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE_JSON", "AFTER_JSON"))
    args = parser.parse_args(argv)

    if args.compare:
        before = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        after = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        md = compare_reports(before, after)
        if args.markdown:
            Path(args.markdown).write_text(md, encoding="utf-8")
        print(md)
        return 0

    report = run_benchmark(args)
    md = render_markdown(report)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        md_path = Path(args.markdown)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
