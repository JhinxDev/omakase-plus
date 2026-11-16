#!/usr/bin/env python3
"""Read saved-start startup phases from the server's own phase receipt.

The server already attributes a saved-position open to named phases. It writes
one line per media pass:

    native phases media=<media_id> resolve=<ms>ms plan=<ms>ms ... first_media=<ms>ms

That line is emitted by ``native_tv_test.py`` once, when the first playable
media segment completes the pass, from ``NativePlan.phases`` in
``native_hls.py``. This script does not add instrumentation: it parses that
existing line, validates it against the canonical key set, and reports the
attribution in a bounded, machine-readable form.

Nothing here touches the network, sleeps, or waits on a device. Feed it a log
file or stdin and it returns immediately.

Canonical phases (order preserved when present):
    resolve, plan, index, issue, captions, timeline, opening, first_media

Usage:
    python scripts/fixtures/native-startup-phases/native_startup_phases.py \
        --log /path/to/server.log --json build/qualification/phases.json

    # gate form: exit 2 unless every media pass recorded all eight phases
    python .../native_startup_phases.py --log server.log --require-complete \
        --max-first-media-ms 6000

    # prove the parser rejects the known-bad shapes (no server needed)
    python .../native_startup_phases.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field

PHASES = (
    "resolve",
    "plan",
    "index",
    "issue",
    "captions",
    "timeline",
    "opening",
    "first_media",
)

# The line may be prefixed by a logger name, level, timestamp or trace prefix.
# Only the ``native phases media=`` marker and the trailing key=value tokens are
# significant, so the prefix is deliberately permissive.
_LINE = re.compile(r"native phases media=(?P<media>[0-9a-fA-F]{1,64})\s*(?P<rest>.*)$")
_PHASE = re.compile(r"(?<![\w-])(?P<name>[a-z_]+)=(?P<value>\d+)ms(?![\w-])")


@dataclass
class Pass:
    media: str
    phases: dict[str, int] = field(default_factory=dict)
    unknown: dict[str, int] = field(default_factory=dict)
    malformed: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [name for name in PHASES if name not in self.phases]

    @property
    def complete(self) -> bool:
        return not self.missing and not self.unknown and not self.malformed

    def as_json(self) -> dict:
        total = self.phases.get("first_media")
        shares = {}
        if total:
            shares = {
                name: round(self.phases[name] / total, 4)
                for name in PHASES
                if name in self.phases and name != "first_media"
            }
        slowest = None
        attributed = {k: v for k, v in self.phases.items() if k != "first_media"}
        if attributed:
            name = max(attributed, key=lambda key: attributed[key])
            slowest = {"phase": name, "ms": attributed[name]}
        return {
            "media": self.media,
            "phases_ms": self.phases,
            "missing": self.missing,
            "unknown": self.unknown,
            "malformed": self.malformed,
            "complete": self.complete,
            "first_media_ms": total,
            "phase_share_of_first_media": shares,
            "slowest_phase": slowest,
        }


def parse_line(line: str) -> Pass | None:
    """Return a Pass for a phase line, or None when the line is unrelated."""
    match = _LINE.search(line)
    if not match:
        return None
    passed = Pass(media=match.group("media").lower())
    rest = match.group("rest").strip()
    if not rest:
        passed.malformed.append("no phase tokens after the media id")
        return passed
    covered = 0
    for token in _PHASE.finditer(rest):
        covered += 1
        name = token.group("name")
        value = int(token.group("value"))
        if name in PHASES:
            if name in passed.phases:
                passed.malformed.append(f"duplicate phase {name}")
            else:
                passed.phases[name] = value
        else:
            passed.unknown[name] = value
    leftover = _PHASE.sub("", rest).strip()
    if leftover:
        # A trailing fragment such as "opening=" or "first_media=abc" is a real
        # shape change, not noise: report it instead of dropping it.
        passed.malformed.append(f"unparsed tokens: {leftover[:120]}")
    if not covered:
        passed.malformed.append("no phase tokens after the media id")
    return passed


def scan(lines) -> list[Pass]:
    found = []
    for line in lines:
        passed = parse_line(line)
        if passed is not None:
            found.append(passed)
    return found


def summarize(passes: list[Pass], *, require_complete: bool,
              max_first_media_ms: int | None) -> dict:
    failures = []
    # Only the last pass per media id counts: a re-open writes a new line and the
    # older one is superseded, so it must not decide the gate.
    latest: dict[str, Pass] = {}
    for passed in passes:
        latest[passed.media] = passed
    for passed in latest.values():
        if require_complete and not passed.complete:
            detail = passed.missing + list(passed.unknown) + passed.malformed
            failures.append(f"media {passed.media} incomplete: {detail}")
        if max_first_media_ms is not None:
            total = passed.phases.get("first_media")
            if total is None:
                failures.append(f"media {passed.media} has no first_media")
            elif total > max_first_media_ms:
                failures.append(
                    f"media {passed.media} first_media={total}ms exceeds {max_first_media_ms}ms"
                )
    if not passes:
        failures.append(
            "no 'native phases media=...' line found: the saved-start open did not "
            "complete a first playable segment, or the log predates the phase receipt"
        )
    return {
        "schema": "native_startup_phases_v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "canonical_phases": list(PHASES),
        "line_count": len(passes),
        "media_count": len(latest),
        "require_complete": require_complete,
        "max_first_media_ms": max_first_media_ms,
        "passes": [passed.as_json() for passed in passes],
        "failures": failures,
        "ok": not failures,
    }


# --------------------------------------------------------------------------
# Self-test: the gate must reject the known-bad shapes, not only accept good.
# --------------------------------------------------------------------------

_GOOD = (
    "WARNING omakase.plus.native_tv_test: native phases media="
    "0123456789abcdef0123456789abcdef resolve=31ms plan=12ms index=240ms "
    "issue=9ms captions=410ms timeline=15ms opening=880ms first_media=1597ms"
)

_SELFTEST_CASES = [
    ("good-line-is-complete", _GOOD, True),
    ("missing-first-media-is-incomplete",
     "native phases media=0123456789abcdef0123456789abcdef resolve=31ms plan=12ms", False),
    ("unknown-key-is-rejected",
     "native phases media=0123456789abcdef0123456789abcdef " + " ".join(
         f"{name}=1ms" for name in PHASES) + " invented=5ms", False),
    ("malformed-token-is-rejected",
     "native phases media=0123456789abcdef0123456789abcdef " + " ".join(
         f"{name}=1ms" for name in PHASES) + " opening=abc", False),
    ("empty-tail-is-rejected",
     "native phases media=0123456789abcdef0123456789abcdef", False),
]


def self_test() -> int:
    failures = 0
    for name, line, expect_complete in _SELFTEST_CASES:
        parsed = parse_line(line)
        if parsed is None:
            print(f"FAIL {name}: line not recognized as a phase receipt")
            failures += 1
            continue
        if parsed.complete != expect_complete:
            print(
                f"FAIL {name}: complete={parsed.complete}, expected {expect_complete} "
                f"(missing={parsed.missing} unknown={parsed.unknown} "
                f"malformed={parsed.malformed})"
            )
            failures += 1
            continue
        print(f"ok   {name}")
    # A superseded pass must not fail the gate: only the latest opens one.
    older = parse_line(
        "native phases media=ffffffffffffffffffffffffffffffff resolve=1ms")
    newer = parse_line(_GOOD.replace("0123456789abcdef0123456789abcdef",
                                     "ffffffffffffffffffffffffffffffff"))
    report = summarize([older, newer], require_complete=True, max_first_media_ms=None)
    if not report["ok"]:
        print(f"FAIL superseded-pass: {report['failures']}")
        failures += 1
    else:
        print("ok   superseded-pass-does-not-fail-the-gate")
    # A budget gate must actually fire.
    budget = summarize([parse_line(_GOOD)], require_complete=True,
                       max_first_media_ms=1000)
    if budget["ok"]:
        print("FAIL budget-gate: a 1597ms first_media passed a 1000ms budget")
        failures += 1
    else:
        print("ok   budget-gate-rejects-overspend")
    # An unrelated log line must not be treated as a pass.
    if parse_line("native phases media=nope") is not None:
        print("FAIL non-hex media id was accepted")
        failures += 1
    else:
        print("ok   non-hex-media-id-rejected")
    print(f"self-test: {len(_SELFTEST_CASES) + 4 - failures}/"
          f"{len(_SELFTEST_CASES) + 4} checks passed")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", action="append", default=[],
                        help="log file to scan; repeatable")
    parser.add_argument("--json", help="write the report here instead of stdout")
    parser.add_argument("--require-complete", action="store_true",
                        help="fail unless every media pass recorded all phases")
    parser.add_argument("--max-first-media-ms", type=int,
                        help="fail when first_media exceeds this budget")
    parser.add_argument("--self-test", action="store_true",
                        help="verify the parser against known-good and known-bad shapes")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    lines: list[str] = []
    if args.log:
        for path in args.log:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    lines.extend(handle)
            except OSError as error:
                print(f"cannot read {path}: {error}", file=sys.stderr)
                return 3
    else:
        lines = sys.stdin.readlines()

    report = summarize(scan(lines), require_complete=args.require_complete,
                       max_first_media_ms=args.max_first_media_ms)
    rendered = json.dumps(report, indent=2, sort_keys=False)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
        print(f"wrote {args.json} (media={report['media_count']} ok={report['ok']})")
    else:
        print(rendered)
    if report["ok"]:
        return 0
    for failure in report["failures"]:
        print(f"FAIL {failure}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
