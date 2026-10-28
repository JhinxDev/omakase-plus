"""Check only public PyPI lock entries with OSV; never send local package names."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import httpx
import tomllib


def audit(lock_path: Path) -> dict:
    raw = lock_path.read_bytes()
    packages = [
        row
        for row in tomllib.loads(raw.decode())["package"]
        if row.get("source", {}).get("registry") == "https://pypi.org/simple"
    ]
    queries = [
        {"package": {"ecosystem": "PyPI", "name": row["name"]}, "version": row["version"]}
        for row in packages
    ]
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        with client.stream(
            "POST", "https://api.osv.dev/v1/querybatch", json={"queries": queries}
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > 6 * 1024 * 1024:
                    raise ValueError("OSV response exceeded the bounded audit size")
                body.extend(chunk)
    results = json.loads(body)["results"]
    if len(results) != len(packages) or any(row.get("next_page_token") for row in results):
        raise ValueError("OSV returned incomplete audit results")
    findings = [
        {
            "package": package["name"],
            "version": package["version"],
            "advisories": [item["id"] for item in result.get("vulns", [])],
        }
        for package, result in zip(packages, results)
        if result.get("vulns")
    ]
    return {
        "lock_sha256": hashlib.sha256(raw).hexdigest(),
        "source": "https://api.osv.dev/v1/querybatch",
        "public_packages_checked": len(packages),
        "findings": findings,
        "complete": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = audit(args.lock)
    except (httpx.HTTPError, ValueError, KeyError, OSError):
        receipt = {
            "complete": False,
            "detail": "Dependency advisory lookup failed; no clean result is claimed",
        }
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    raise SystemExit(0 if receipt.get("complete") and not receipt.get("findings") else 1)
