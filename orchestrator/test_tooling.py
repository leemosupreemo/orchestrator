"""One-click setup for the test frameworks listed on the Tests page.

Pure text helpers (no ROOT, no subprocess) so they can be tested against a temp directory; the console-side entry
point in scripts/job_actions.py supplies the project root and runs brew / swift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Package:
    url: str
    requirement: str
    products: tuple[str, ...]
    package_name: str


# Frameworks added by editing Package.swift. Anything else is installed with brew or scaffolded as a file.
SPM_FRAMEWORKS = {
    "snapshot_testing": Package(
        "https://github.com/pointfreeco/swift-snapshot-testing.git", 'from: "1.17.0"', ("SnapshotTesting",), "swift-snapshot-testing"),
    "quick_nimble": Package("https://github.com/Quick/Quick.git", 'from: "7.6.0"', ("Quick",), "Quick"),
}
# Nimble is its own package; Quick specs need both.
EXTRA_PACKAGES = {
    "quick_nimble": [Package("https://github.com/Quick/Nimble.git", 'from: "13.0.0"', ("Nimble",), "Nimble")],
}

STARTER_FILES = {
    "snapshot_testing": ("SampleSnapshotTests.swift", """import XCTest
import SnapshotTesting

// The first run records the reference snapshot and fails on purpose; run again and it passes.
final class SampleSnapshotTests: XCTestCase {
    func testStructuredDataSnapshot() {
        let user = ["name": "Ada", "role": "admin"]
        assertSnapshot(of: user, as: .dump)
    }
}
"""),
    "quick_nimble": ("SampleSpec.swift", """import Quick
import Nimble

final class SampleSpec: QuickSpec {
    override class func spec() {
        describe("Arithmetic") {
            context("when adding two numbers") {
                it("returns their sum") {
                    expect(2 + 2).to(equal(4))
                }
            }
        }
    }
}
"""),
}


def _matching(text: str, open_at: int) -> int:
    """Index of the bracket that closes the one at open_at, skipping string literals. -1 if unbalanced."""
    pairs = {"(": ")", "[": "]"}
    stack: list[str] = []
    in_string = False
    i = open_at
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return i
        i += 1
    return -1


def _append_entry(text: str, close_at: int, entry: str) -> str:
    """Add entry as the last element of the array whose closing bracket is at close_at."""
    before = text[:close_at].rstrip()
    line_start = text.rfind("\n", 0, close_at) + 1
    closing_indent = text[line_start:close_at]
    if closing_indent.strip():  # the array is written on one line
        sep = " " if before.endswith(("[", ",")) else ", "
        return f"{before}{sep}{entry}{text[close_at:]}"
    sep = "" if before.endswith(("[", ",")) else ","
    return f"{before}{sep}\n{closing_indent}    {entry}\n{closing_indent}{text[close_at:]}"


def find_test_target(manifest: str, preferred: str | None = None) -> str | None:
    names = re.findall(r'\.testTarget\(\s*name:\s*"([^"]+)"', manifest)
    if preferred and preferred in names:
        return preferred
    return names[0] if names else None


def _test_target_call(manifest: str, test_target: str) -> tuple[int, int] | None:
    found = re.search(r'\.testTarget\(\s*name:\s*"%s"' % re.escape(test_target), manifest)
    if not found:
        return None
    open_at = manifest.index("(", found.start())
    close_at = _matching(manifest, open_at)
    return (open_at, close_at) if close_at >= 0 else None


def add_spm_packages(manifest: str, packages: list[Package], test_target: str) -> str | None:
    """Package.swift text with the packages added and their products linked into test_target, or None when the manifest
    isn't shaped the way this expects (the caller then falls back to an AI job instead of guessing)."""
    targets_at = manifest.find("targets:")
    if targets_at < 0 or not _test_target_call(manifest, test_target):
        return None
    for package in packages:
        if package.url in manifest:
            continue
        entry = f'.package(url: "{package.url}", {package.requirement})'
        targets_at = manifest.find("targets:")
        deps = re.search(r"dependencies:\s*\[", manifest[:targets_at])
        if deps:
            close = _matching(manifest, deps.end() - 1)
            if close < 0:
                return None
            manifest = _append_entry(manifest, close, entry)
        else:
            line_start = manifest.rfind("\n", 0, targets_at) + 1
            indent = manifest[line_start:targets_at]
            if indent.strip():
                return None
            manifest = f"{manifest[:line_start]}{indent}dependencies: [\n{indent}    {entry}\n{indent}],\n{manifest[line_start:]}"
    for package in packages:
        for product in package.products:
            item = f'.product(name: "{product}", package: "{package.package_name}")'
            open_at, close_at = _test_target_call(manifest, test_target)
            if item in manifest[open_at:close_at]:
                continue
            deps = re.search(r"dependencies:\s*\[", manifest[open_at:close_at])
            if deps:
                close = _matching(manifest, open_at + deps.end() - 1)
                if close < 0:
                    return None
                manifest = _append_entry(manifest, close, item)
                continue
            name = re.search(r'name:\s*"[^"]+"\s*,?', manifest[open_at:close_at])
            at = open_at + name.end()
            comma = "" if manifest[:at].endswith(",") else ","
            trailing = "" if manifest[at:].lstrip().startswith(")") else ","
            manifest = f"{manifest[:at]}{comma} dependencies: [{item}]{trailing}{manifest[at:]}"
    return manifest


def starter_dir(root: Path, test_target: str) -> Path:
    """Where the target's sources live: SwiftPM's Tests/<target> unless the project keeps them at <target>/."""
    if (root / "Tests" / test_target).exists() or not (root / test_target).exists() and (root / "Package.swift").exists():
        return root / "Tests" / test_target
    return root / test_target
