"""Build/discover the offline renderer source offer; assemble and relink it locally.

No network, native build, installation or deployment is performed by this helper.
Python 3.12+ is required for tar's safe data extraction filter.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

RIGHTS_BANNER = "/*! Modified JASSUB 2.5.23 derivative. Mixed native/JavaScript licenses; not MIT-only.\n * Full copyright and license notices: ./LICENSES.txt\n * Complete local preferred source, build and relink information: ./SOURCE.json\n * Source archive: ./corresponding-source.tar.gz\n */\n"

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "THIRD-PARTY/jassub-2.5.23"
ASSETS = ROOT / "src/omakase/plus/static/ass"
PROJECTS = ("jassub", "brotli", "freetype", "fribidi", "harfbuzz", "libass")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def verified_archives(source_dir, manifest):
    result = {}
    if set(manifest) != set(PROJECTS):
        raise ValueError("Complete six-project preferred source is required")
    for project in PROJECTS:
        captured = manifest[project]["captured_preferred_source"]
        name = captured["archive_name"]
        if PurePosixPath(name).name != name or "\\" in name:
            raise ValueError("Source archive must have a local basename")
        file = source_dir / name
        data = file.read_bytes()
        if len(data) != captured["bytes"] or sha256(data) != captured["sha256"]:
            raise ValueError("Preferred source archive integrity mismatch")
        result[project] = file
    return result


def extract_source(archive, destination):
    """Strip one archive root, preserve modes, reject escaping/link/device data."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        members = []
        roots = set()
        for original in source.getmembers():
            path = PurePosixPath(original.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe preferred source archive")
            roots.add(path.parts[0])
            if len(path.parts) == 1:
                continue
            item = copy.copy(original)
            item.name = PurePosixPath(*path.parts[1:]).as_posix()
            if item.islnk():
                item.linkname = item.linkname.split("/", 1)[-1]
            members.append(item)
        if len(roots) != 1:
            raise ValueError("Expected exactly one upstream archive root")
        source.extractall(destination, members=members, filter="data")


def assemble(bundle_root, output):
    manifest = json.loads(
        (bundle_root / "THIRD-PARTY/jassub-2.5.23/native-source-access.json").read_text()
    )
    sources = verified_archives(bundle_root / "sources/native", manifest)
    if output.exists():
        raise ValueError("Assembly destination must not exist; preserve previous work")
    extract_source(sources["jassub"], output)
    for project in PROJECTS[1:]:
        destination = output / "lib" / project
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("Refusing to overwrite existing dependency source")
        extract_source(sources[project], destination)
    for required in (
        "Dockerfile",
        "Makefile",
        "functions.mk",
        "build/license_extract.sh",
        "build/patches",
        "lib/fribidi/lib/fribidi.c",
    ):
        if not (output / required).exists():
            raise ValueError("Assembled source is missing a required native build input")
    return output


def relink(bundle_root, native_tree, output):
    """Rebundle original derivative with both user-built native variants/loader.

    Uses the captured Windows esbuild executable, as the original derivative did.
    Native compilation is a separate, explicit user command documented in REBUILD.
    """
    if output.exists():
        raise ValueError("Relink output must not exist")
    lock = json.loads((bundle_root / "THIRD-PARTY/jassub-2.5.23/npm-source-lock.json").read_text())
    with tempfile.TemporaryDirectory(prefix="ass-relink-") as temporary:
        work = Path(temporary)
        modules = work / "node_modules"
        import base64

        for name, metadata in lock.items():
            version = metadata["version"]
            archive = (
                bundle_root
                / "sources/npm"
                / (name.replace("/", "__").replace("@", "") + "-" + version + ".tgz")
            )
            integrity = (
                "sha512-" + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
            )
            if integrity != metadata["dist"]["integrity"]:
                raise ValueError("npm source integrity mismatch")
            extract_source(archive, modules / name)
        package = modules / "jassub"
        for name in (
            "jassub-worker.js",
            "jassub-worker.wasm",
            "jassub-worker-modern.js",
            "jassub-worker-modern.wasm",
        ):
            shutil.copyfile(native_tree / "src/wasm" / name, package / "dist/wasm" / name)
        entry = package / "dist/jassub.js"
        text = entry.read_text()
        if text.count("import 'rvfc-polyfill';") != 1:
            raise ValueError("Unexpected original browser entry")
        entry.write_text(
            text.replace("import 'rvfc-polyfill';", "// Native requestVideoFrameCallback required.")
        )
        util = package / "dist/worker/util.js"
        text, count = re.subn(
            r"export const THREAD_COUNT = [^;]+;",
            "export const THREAD_COUNT = 1;",
            util.read_text(),
        )
        if count != 1:
            raise ValueError("Unexpected worker thread expression")
        util.write_text(text)
        loader = package / "dist/wasm/jassub-worker.js"
        text, count = re.subn(r"maximum\s*:\s*32768\b", "maximum:8192", loader.read_text())
        if count != 1:
            raise ValueError("Generated loader needs explicit memory-limit review")
        loader.write_text(text)
        output.mkdir(parents=True)
        guard = (
            "const originalFetch=globalThis.fetch.bind(globalThis);"
            "globalThis.fetch=(input,init)=>{const u=new URL(typeof input==='string'?input:input.url||input.href,self.location.href);"
            "if(!['http:','https:'].includes(u.protocol)||u.origin!==self.location.origin)throw new Error('Off-origin ASS fetch blocked');"
            "return originalFetch(u.href,{...init,credentials:'same-origin',mode:'same-origin',redirect:'error'});};"
        )
        executable = modules / "@esbuild/win32-x64/esbuild.exe"
        for original, target in (
            (entry, "jassub.js"),
            (package / "dist/worker/worker.js", "worker.js"),
        ):
            subprocess.run(
                [
                    str(executable),
                    str(original),
                    "--bundle",
                    "--format=esm",
                    "--platform=browser",
                    "--target=es2022",
                    "--legal-comments=inline",
                    "--banner:js=" + RIGHTS_BANNER + (guard if target == "worker.js" else ""),
                    "--outfile=" + str(output / target),
                ],
                cwd=work,
                check=True,
            )
        for name in ("jassub-worker.wasm", "jassub-worker-modern.wasm"):
            shutil.copyfile(package / "dist/wasm" / name, output / name)
        shutil.copyfile(package / "dist/default.woff2", output / "default.woff2")
        shutil.copyfile(
            bundle_root / "THIRD-PARTY/jassub-2.5.23/THIRD-PARTY-NOTICES.txt",
            output / "LICENSES.txt",
        )


def build_bundle(source_dir, *, root=ROOT, assets=ASSETS):
    provenance = root / "THIRD-PARTY/jassub-2.5.23"
    manifest = json.loads((provenance / "native-source-access.json").read_text())
    sources = verified_archives(source_dir, manifest)
    files = {f"sources/native/{file.name}": file for file in sources.values()}
    for file in provenance.rglob("*"):
        if file.is_file() and file.suffix != ".tgz" and file.name != "asset-manifest.json":
            files["THIRD-PARTY/jassub-2.5.23/" + file.relative_to(provenance).as_posix()] = file
    for file in provenance.glob("*.tgz"):
        files["sources/npm/" + file.name] = file
    for name in ("vendor_ass.py", "vendor_ass_notices.py", "build_ass_source_bundle.py"):
        files["scripts/" + name] = root / "scripts" / name
    files["LICENSE"] = root / "LICENSE"
    assets.mkdir(parents=True, exist_ok=True)
    target = assets / "corresponding-source.tar.gz"
    with (
        target.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=1) as zipped,
    ):
        with tarfile.open(fileobj=zipped, mode="w") as bundle:
            for name, path in sorted(files.items()):
                data = path.read_bytes()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                bundle.addfile(info, io.BytesIO(data))
    index = {
        "schema": "omakase-ass-source-offer-v1",
        "bundle": target.name,
        "sha256": sha256(target.read_bytes()),
        "bytes": target.stat().st_size,
        "instructions": "THIRD-PARTY/jassub-2.5.23/REBUILD.md",
        "native_projects": {
            name: {
                "commit": manifest[name]["commit"],
                **manifest[name]["captured_preferred_source"],
            }
            for name in PROJECTS
        },
        "complete_build_inputs": True,
        "native_binary_correspondence_proven": False,
        "toolchain": "docker.io/emscripten/emsdk:6.0.12",
    }
    (assets / "SOURCE.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(provenance / "THIRD-PARTY-NOTICES.txt", assets / "LICENSES.txt")
    hashes = {
        path.name: {"sha256": sha256(path.read_bytes()), "bytes": path.stat().st_size}
        for path in assets.iterdir()
        if path.is_file()
    }
    (provenance / "asset-manifest.json").write_text(
        json.dumps(hashes, indent=2) + "\n", encoding="utf-8"
    )
    return index


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("bundle")
    build.add_argument("--source-dir", type=Path, required=True)
    extract = commands.add_parser("assemble")
    extract.add_argument("--bundle-root", type=Path, default=ROOT)
    extract.add_argument("--output", type=Path, required=True)
    native = commands.add_parser("relink")
    native.add_argument("--bundle-root", type=Path, default=ROOT)
    native.add_argument("--native-tree", type=Path, required=True)
    native.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "bundle":
        print(json.dumps(build_bundle(args.source_dir)))
    elif args.command == "assemble":
        assemble(args.bundle_root, args.output)
    else:
        relink(args.bundle_root, args.native_tree, args.output)


if __name__ == "__main__":
    main()
