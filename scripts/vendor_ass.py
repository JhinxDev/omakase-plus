"""Build the explicitly derived serial JASSUB candidate from integrity-locked npm sources.

Python stdlib only. First --capture-lock records exact official registry metadata;
subsequent builds use it, never floating versions. No npm lifecycle scripts run.
Requires an esbuild executable from the pinned platform artifact (Windows x64).
"""

import argparse
import base64
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RIGHTS_BANNER = "/*! Modified JASSUB 2.5.23 derivative. Mixed native/JavaScript licenses; not MIT-only.\n * Full copyright and license notices: ./LICENSES.txt\n * Complete local preferred source, build and relink information: ./SOURCE.json\n * Source archive: ./corresponding-source.tar.gz\n */\n"

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "THIRD-PARTY/jassub-2.5.23"
OUTPUT = ROOT / "src/omakase/plus/static/ass"
PINS = {
    "jassub": "2.5.23",
    "abslink": "1.3.1",
    "throughput": "1.0.2",
    "lfa-ponyfill": "1.1.1",
    "esbuild": "0.28.2",
    "@esbuild/win32-x64": "0.28.2",
}


def fetch(url):
    with urllib.request.urlopen(url, timeout=40) as response:
        return response.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-lock", action="store_true")
    args = parser.parse_args()
    PROVENANCE.mkdir(parents=True, exist_ok=True)
    lockfile = PROVENANCE / "npm-source-lock.json"
    if args.capture_lock:
        if lockfile.exists():
            raise SystemExit("Existing lock must not be silently replaced")
        lock = {}
        for name, version in PINS.items():
            meta = json.loads(fetch(f"https://registry.npmjs.org/{name}/{version}"))
            lock[name] = {
                "version": version,
                "dist": meta["dist"],
                "license": meta.get("license"),
                "dependencies": meta.get("dependencies", {}),
            }
        lockfile.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    lock = json.loads(lockfile.read_text())
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omakase-ass-build-") as temp:
        work = Path(temp)
        modules = work / "node_modules"
        for name, version in PINS.items():
            item = lock[name]
            assert item["version"] == version
            archive = fetch(item["dist"]["tarball"])
            actual = "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode()
            assert actual == item["dist"]["integrity"], name
            if name == "jassub":
                assert (
                    actual
                    == "sha512-KvWvkZOQvLUZKDoJTfAZzbmxCN2Avs37d1VnZlscKvmfPIjcS+l2guou0WdGW6z/+Uxb8F7ha8Nrq5h5+k9cLw=="
                )
            # Keep exact package source archives, including every published notice.
            archive_path = PROVENANCE / (
                name.replace("/", "__").replace("@", "") + "-" + version + ".tgz"
            )
            archive_path.write_bytes(archive)
            target = modules / name
            target.mkdir(parents=True)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for member in tar.getmembers():
                    parts = Path(member.name).parts[1:]
                    if not parts or member.isdir():
                        continue
                    assert member.isfile() and ".." not in parts
                    path = target.joinpath(*parts)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(tar.extractfile(member).read())
        package = modules / "jassub"
        entry = package / "dist/jassub.js"
        source = entry.read_text()
        assert source.count("import 'rvfc-polyfill';") == 1
        entry.write_text(
            source.replace(
                "import 'rvfc-polyfill';",
                "// Derivative: native requestVideoFrameCallback required; no polyfill shipped.",
            )
        )
        util = package / "dist/worker/util.js"
        source = util.read_text()
        # Exact substitution guarded below after inspecting the published module.
        import re

        source, count = re.subn(
            r"export const THREAD_COUNT = [^;]+;", "export const THREAD_COUNT = 1;", source
        )
        assert count == 1
        util.write_text(source)
        loader = package / "dist/wasm/jassub-worker.js"
        source = loader.read_text()
        assert source.count("maximum: 32768") == 1
        loader.write_text(source.replace("maximum: 32768", "maximum: 8192"))
        executable = modules / "@esbuild/win32-x64/esbuild.exe"
        for original, name in [
            (entry, "jassub.js"),
            (package / "dist/worker/worker.js", "worker.js"),
        ]:
            guard = (
                "const originalFetch=globalThis.fetch.bind(globalThis);"
                "globalThis.fetch=(input,init)=>{const u=new URL(typeof input==='string'?input:input.url||input.href,self.location.href);"
                "if(!['http:','https:'].includes(u.protocol)||u.origin!==self.location.origin)throw new Error('Off-origin ASS fetch blocked');"
                "return originalFetch(u.href,{...init,credentials:'same-origin',mode:'same-origin',redirect:'error'});};"
            )
            subprocess.run(
                [
                    str(executable),
                    str(original),
                    "--bundle",
                    "--format=esm",
                    "--platform=browser",
                    "--target=es2022",
                    "--legal-comments=inline",
                    "--banner:js=" + RIGHTS_BANNER + (guard if name == "worker.js" else ""),
                    "--outfile=" + str(OUTPUT / name),
                ],
                check=True,
                cwd=work,
            )
        for name in ["jassub-worker.wasm", "jassub-worker-modern.wasm"]:
            shutil.copyfile(package / "dist/wasm" / name, OUTPUT / name)
        shutil.copyfile(package / "dist/default.woff2", OUTPUT / "default.woff2")
        if (PROVENANCE / "THIRD-PARTY-NOTICES.txt").exists():
            shutil.copyfile(PROVENANCE / "THIRD-PARTY-NOTICES.txt", OUTPUT / "LICENSES.txt")
        # Preserve aggregate native notices in an independently readable deployed asset.
        for candidate in package.rglob("*"):
            if candidate.is_file() and (
                "licen" in candidate.name.lower() or "copying" in candidate.name.lower()
            ):
                print("NOTICE", candidate.relative_to(package), candidate.stat().st_size)
                shutil.copyfile(candidate, PROVENANCE / ("jassub-" + candidate.name))
        advisory = urllib.request.Request(
            "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk",
            data=json.dumps({k: [v] for k, v in PINS.items()}).encode(),
            headers={"Content-Type": "application/json"},
        )
        (PROVENANCE / "npm-advisories.json").write_bytes(
            urllib.request.urlopen(advisory, timeout=40).read()
        )
        hashes = {
            p.name: {
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "bytes": p.stat().st_size,
            }
            for p in OUTPUT.iterdir()
            if p.is_file()
        }
        (PROVENANCE / "asset-manifest.json").write_text(json.dumps(hashes, indent=2) + "\n")


if __name__ == "__main__":
    main()
