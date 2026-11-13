"""Make new Swift test files part of an Xcode project's test target, and tell whether a test run actually ran them.

Xcode only builds the files its project lists, so a test file written to disk next to the others is silently ignored: the run passes
without running it. `register` adds files to the project's unit-test target (using the `xcodeproj` Ruby gem Xcode projects are usually
managed with), and `not_run` reads a test run's output for the files whose test classes never started.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

RUBY = r"""
require 'json'
require 'xcodeproj'
root, files = ARGV[0], JSON.parse(ARGV[1])
path = Dir[File.join(root, '*.xcodeproj')].first or abort('no project')
project = Xcodeproj::Project.open(path)
targets = project.targets.select { |t| t.product_type == 'com.apple.product-type.bundle.unit-test' }
abort('no test target') if targets.empty?
added = []
files.each do |rel|
  next unless rel.end_with?('.swift')
  dir = File.dirname(rel)
  target = targets.find { |t| t.source_build_phase.files_references.compact.any? { |r| File.dirname(r.real_path.to_s) == File.join(root, dir) } } ||
           (targets.size == 1 ? targets.first : nil)
  next unless target
  group = project.main_group
  dir.split('/').reject { |part| part == '.' }.each { |part| group = group[part] || group.new_group(part, part) }
  ref = group.files.find { |f| f.path == File.basename(rel) } || group.new_file(File.basename(rel))
  unless target.source_build_phase.files_references.include?(ref)
    target.source_build_phase.add_file_reference(ref)
    added << rel
  end
end
project.save unless added.empty?
puts JSON.generate(added)
"""

CLASS = re.compile(r"^\s*(?:final\s+)?(?:public\s+|open\s+)?class\s+(\w+)\s*:[^{]*\bXCTestCase\b", re.M)


def project_file(root: Path) -> Path | None:
    found = sorted(Path(root).glob("*.xcodeproj"))
    return found[0] / "project.pbxproj" if found else None


def test_classes(path: Path) -> list[str]:
    """The XCTestCase classes a Swift file declares."""
    try:
        return CLASS.findall(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def register(root: Path, files: list[str]) -> tuple[list[str], str]:
    """Add Swift test `files` (paths relative to `root`) to the project's test target. Returns the files newly added, and a reason
    when nothing could be done: no Xcode project, no Swift files, or no Ruby with the xcodeproj gem."""
    root = Path(root)
    swift = [f for f in files if f.endswith(".swift")]
    if not swift or project_file(root) is None:
        return [], ""
    ruby = shutil.which("ruby")
    if not ruby:
        return [], "Ruby isn't installed, so the new test files couldn't be added to the Xcode test target."
    try:
        res = subprocess.run([ruby, "-e", RUBY, str(root), json.dumps(swift)], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"Adding the new test files to the Xcode test target failed ({exc})."
    if res.returncode:
        reason = (res.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
        return [], f"The new test files couldn't be added to the Xcode test target ({reason})."
    try:
        return [str(f) for f in json.loads(res.stdout.strip().splitlines()[-1])], ""
    except (ValueError, IndexError):
        return [], "The new test files couldn't be added to the Xcode test target."


def not_run(root: Path, files: list[str], output: str) -> list[str]:
    """Swift test files whose test classes never started in this test run's `output`: the project doesn't build them."""
    ran = set(re.findall(r"Test Suite '([^']+)' (?:started|passed|failed)", output))
    missing = []
    for rel in files:
        if not rel.endswith(".swift"):
            continue
        classes = test_classes(Path(root) / rel)
        if classes and not any(c in ran for c in classes):
            missing.append(rel)
    return missing
