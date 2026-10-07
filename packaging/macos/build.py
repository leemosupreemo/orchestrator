"""Assemble an architecture-specific app from verified inputs, never user state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ARCHES = ("arm64", "x86_64")


def run(argv, **kwargs):
    subprocess.run(list(map(str, argv)), check=True, **kwargs)


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text())
    if data.get("schema") != 1:
        raise ValueError("Unsupported dependency manifest.")
    for item in [data[arch][name] for arch in ARCHES for name in ("python", "cloudflared")] + [data["certificates"]]:
        parsed = urlsplit(item["url"])
        if parsed.scheme != "https" or parsed.username or not parsed.hostname or "latest" in parsed.path or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) or item["sha256"] == "0" * 64 or not item.get("license"):
            raise ValueError("Dependencies need exact HTTPS URLs, real digests and licenses.")
    return data


def verify_digest(path: Path, expected: str) -> None:
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"Dependency digest mismatch: {path.name}")


def download(item: dict, cache: Path) -> Path:
    path = cache / item["sha256"]
    if not path.exists():
        temporary = path.with_suffix(".download")
        # System curl uses the host's trust store, even when the builder's Python lacks certificates.
        run(["/usr/bin/curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https", "--retry", "3", "--output", temporary, item["url"]])
        verify_digest(temporary, item["sha256"])
        temporary.replace(path)
    verify_digest(path, item["sha256"])
    return path


def extract_archive(archive: Path, target: Path) -> None:
    base = target.resolve()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            destination = base / member.name
            if Path(member.name).is_absolute() or not destination.resolve().is_relative_to(base) or not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("Unsafe archive member.")
            if member.issym() or member.islnk():
                link = (destination.parent if member.issym() else base) / member.linkname
                if Path(member.linkname).is_absolute() or not link.resolve().is_relative_to(base):
                    raise ValueError("Unsafe archive link.")
        tar.extractall(target, filter="data")


def stage_source(source: Path, target: Path, version: str) -> None:
    target.mkdir(parents=True)
    project = re.sub(r'(?m)^version = "[^"]+"$', f'version = "{version}"', (source / "pyproject.toml").read_text(), count=1)
    (target / "pyproject.toml").write_text(project)
    for path in (source / "orchestrator").rglob("*"):
        relative = path.relative_to(source)
        if not path.is_file() or path.is_symlink() or "__pycache__" in relative.parts:
            continue
        allowed = path.suffix == ".py" or (relative.parts[1] in ("config", "prompts", "templates") and path.suffix in (".json", ".md", ".sh", ".swift", ".yml")) or (relative.parts[1:3] == ("web", "static") and path.suffix in (".js", ".css", ".html", ".svg", ".woff2", ".png", ".json", ".webmanifest"))
        if allowed:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    initializer = target / "orchestrator/__init__.py"
    if initializer.exists():
        initializer.write_text(re.sub(r'(?m)^__version__ = "[^"]+"$', f'__version__ = "{version}"', initializer.read_text(), count=1))
    for name in ("README.md", "LICENSE"):
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)


def install_wheel(wheel: Path, runtime: Path) -> None:
    target = runtime / "lib/python3.12/site-packages"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
        if len(metadata) != 1 or "Root-Is-Purelib: true" not in archive.read(metadata[0]).decode() or not wheel.name.endswith("-py3-none-any.whl"):
            raise ValueError("Cross-architecture assembly requires a pure Python wheel.")
        for name in archive.namelist():
            if not (target / name).resolve().is_relative_to(target.resolve()) or Path(name).is_absolute() or any(part.endswith(".data") for part in Path(name).parts):
                raise ValueError("Unsafe or unsupported wheel member.")
        archive.extractall(target)
    (runtime / "bin").mkdir(exist_ok=True)
    entry = runtime / "bin/orchestrator"
    entry.write_text('#!/bin/sh\nexec "$(dirname "$0")/python3" -P -m orchestrator "$@"\n')
    entry.chmod(0o755)


def macho_files(bundle: Path):
    for path in bundle.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
                    yield path


def validate_macho(bundle: Path, architecture: str) -> None:
    for path in macho_files(bundle):
        arch = subprocess.check_output(["/usr/bin/lipo", "-archs", str(path)], text=True).split()
        if architecture not in arch:
            raise ValueError(f"Wrong executable architecture: {path.name}")
        # Inspect only the target slice: a universal binary prints a header line per slice.
        linked = subprocess.check_output(["/usr/bin/otool", "-arch", architecture, "-L", str(path)], text=True).splitlines()[1:]
        for line in linked:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
                raise ValueError(f"Nonportable runtime dependency: {path.name}")


def service_identity(integration: bool = False) -> tuple[dict, dict]:
    info = plistlib.loads((HERE / "Info.plist").read_bytes())
    agent = plistlib.loads((HERE / "agent.plist").read_bytes())
    if integration:
        info.update(CFBundleIdentifier="com.orchestrator.desktop.integration", CFBundleDisplayName="Orchestrator Integration", OrchestratorIntegrationFixture=True)
        agent["Label"] = "com.orchestrator.desktop.integration.agent"
    info["OrchestratorAgentPlist"] = agent["Label"] + ".plist"
    return info, agent


def source_identity() -> tuple[str, bool]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO, text=True).strip())
    return commit, dirty


def build_bundle(architecture: str, output_dir: Path, manifest: Path, version: str, build_number: int, integration: bool = False) -> Path:
    if architecture not in ARCHES or not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.-]+)?", version) or type(build_number) is not int or build_number <= 0:
        raise ValueError("Choose arm64/x86_64, an explicit release version and a positive build number.")
    dependencies = load_manifest(manifest)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache = output_dir / ".downloads"
    cache.mkdir(exist_ok=True)
    destination = output_dir / architecture / "Orchestrator.app"
    if destination.exists():
        raise ValueError("Output app already exists. Choose a new output directory.")
    source_commit, source_dirty = source_identity()
    with tempfile.TemporaryDirectory(prefix="orchestrator-build-", dir=output_dir) as folder:
        stage = Path(folder)
        bundle = stage / "Orchestrator.app"
        resources = bundle / "Contents/Resources"
        macos = bundle / "Contents/MacOS"
        resources.mkdir(parents=True); macos.mkdir()
        runtime_stage = stage / "python-input"
        extract_archive(download(dependencies[architecture]["python"], cache), runtime_stage)
        shutil.move(runtime_stage / "python", resources / "runtime")
        run(["/usr/bin/install_name_tool", "-id", "@rpath/libpython3.12.dylib", resources / "runtime/lib/libpython3.12.dylib"])
        tunnel_stage = stage / "tunnel-input"
        extract_archive(download(dependencies[architecture]["cloudflared"], cache), tunnel_stage)
        (resources / "bin").mkdir()
        shutil.copy2(tunnel_stage / "cloudflared", resources / "bin/cloudflared")
        (resources / "bin/cloudflared").chmod(0o755)
        (resources / "certificates").mkdir()
        shutil.copy2(download(dependencies["certificates"], cache), resources / "certificates/cacert.pem")
        source = stage / "wheel-source"
        stage_source(REPO, source, version)
        wheel = stage / "wheels"
        run(["python3", "-m", "pip", "wheel", source, "--no-deps", "--no-build-isolation", "--wheel-dir", wheel])
        wheels = list(wheel.glob("orchestrator-*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected exactly one Orchestrator wheel.")
        python = resources / "runtime/bin/python3"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", SSL_CERT_FILE=str(resources / "certificates/cacert.pem"))
        env.pop("PYTHONPATH", None); env.pop("PYTHONHOME", None)
        install_wheel(wheels[0], resources / "runtime")
        swift = ["swift", "build", "--package-path", REPO / "desktop/macos", "--scratch-path", stage / "swift", "-c", "release", "--triple", f"{architecture}-apple-macosx13.0"]
        run(swift)
        binary_dir = Path(subprocess.check_output(list(map(str, swift + ["--show-bin-path"])), text=True).strip())
        for name in ("Orchestrator", "OrchestratorAgentLauncher"):
            shutil.copy2(binary_dir / name, macos / name)
        sparkle = list((stage / "swift/artifacts").glob("sparkle/Sparkle/Sparkle.xcframework/macos*/Sparkle.framework"))
        if len(sparkle) != 1:
            raise ValueError("The pinned Sparkle framework is missing.")
        frameworks = bundle / "Contents/Frameworks"
        frameworks.mkdir()
        shutil.copytree(sparkle[0], frameworks / "Sparkle.framework", symlinks=True)
        shutil.copy2(sparkle[0].parents[2] / "LICENSE", resources / "Sparkle-LICENSE.txt")
        load_commands = subprocess.check_output(["/usr/bin/otool", "-l", str(macos / "Orchestrator")], text=True)
        if "@executable_path/../Frameworks" not in load_commands:
            run(["/usr/bin/install_name_tool", "-add_rpath", "@executable_path/../Frameworks", macos / "Orchestrator"])
        info, agent = service_identity(integration)
        info.update(CFBundleShortVersionString=version, CFBundleVersion=str(build_number), OrchestratorArchitecture=architecture, OrchestratorDevelopmentBuild=True)
        final_commit, final_dirty = source_identity()
        info["OrchestratorSourceCommit"] = source_commit
        info["OrchestratorSourceDirty"] = source_dirty or final_dirty or final_commit != source_commit
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        agents = bundle / "Contents/Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / info["OrchestratorAgentPlist"]).write_bytes(plistlib.dumps(agent))
        shutil.copy2(manifest, resources / "dependencies.json")
        shutil.copy2(HERE / "THIRD_PARTY_NOTICES.md", resources / "THIRD_PARTY_NOTICES.md")
        validate_macho(bundle, architecture)
        if platform.machine() == architecture:
            run([python, "-P", "-c", "import importlib.metadata, importlib.resources, orchestrator.web; from orchestrator import __version__; assert __version__ == importlib.metadata.version('orchestrator') == '" + version + "'; assert importlib.resources.files('orchestrator.web').joinpath('static/setup.js').is_file()"], env=env)
        else:
            print("Foreign architecture assembled; runtime acceptance requires a matching Mac.", flush=True)
        run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", bundle])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(bundle, destination)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=ARCHES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=HERE / "dependencies.json")
    parser.add_argument("--version", default=tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"])
    parser.add_argument("--build-number", type=int, required=True)
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--integration-fixture", action="store_true")
    args = parser.parse_args()
    if not args.development:
        parser.error("Assembly requires --development. Sign and notarize verified artifacts with release.py.")
    print(build_bundle(args.arch, args.output, args.manifest, args.version, args.build_number, args.integration_fixture))


if __name__ == "__main__":
    main()
