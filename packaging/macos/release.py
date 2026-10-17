"""Turn an assembled Orchestrator.app into a disk image.

Development mode wraps the ad-hoc signed app for testing on this Mac; those images are never client downloads.
Release mode signs every nested executable with a Developer ID identity and the hardened runtime, then notarizes,
staples and verifies the image. It refuses to start unless every input is present, and never edits the input app.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from build import ARCHES, HERE, load_manifest, macho_files, run as run_tool, validate_macho

BUNDLE_SUFFIXES = (".app", ".framework", ".xpc")


def validate_bundle(bundle: Path, manifest: Path = HERE / "dependencies.json") -> dict:
    """{version, build, architecture} of an app whose native and Python halves, target and inputs all agree."""
    contents = bundle / "Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    architecture = info.get("OrchestratorArchitecture")
    if architecture not in ARCHES:
        raise ValueError("The app doesn't say which architecture it was built for.")
    executable = contents / "MacOS/Orchestrator"
    archs = subprocess.check_output(["/usr/bin/lipo", "-archs", str(executable)], text=True).split()
    if archs != [architecture]:
        raise ValueError(f"The app executable's architecture is {' '.join(archs)}, not {architecture}.")
    validate_macho(bundle, architecture)
    if info.get("LSMinimumSystemVersion") != "13.0":
        raise ValueError("The app must target macOS 13.0.")
    version = info.get("CFBundleShortVersionString", "")
    build = info.get("CFBundleVersion", "")
    if not re.fullmatch(r"[1-9]\d*", build):
        raise ValueError("The app needs a positive build number.")
    metadata = list((contents / "Resources/runtime/lib").glob("python3.*/site-packages/orchestrator-*.dist-info/METADATA"))
    python_version = next((line.split(":", 1)[1].strip() for line in metadata[0].read_text().splitlines() if line.startswith("Version:")), "") if len(metadata) == 1 else ""
    if not version or python_version != version:
        raise ValueError(f"The app's version ({version}) and its bundled Orchestrator version ({python_version or 'missing'}) differ.")
    bundled = contents / "Resources/dependencies.json"
    load_manifest(bundled)
    if json.loads(bundled.read_text()) != json.loads(manifest.read_text()):
        raise ValueError("The app's bundled dependencies don't match the verified dependency manifest.")
    return {"version": version, "build": int(build), "architecture": architecture}


def check_release_inputs(identity: str, notary_profile: str, feed_url: str, public_key: str) -> None:
    if not identity.startswith("Developer ID Application: "):
        raise ValueError("Release signing needs a 'Developer ID Application' identity; development and App Store identities can't be notarized for direct download.")
    if not notary_profile.strip():
        raise ValueError("Release needs a notarytool keychain profile (xcrun notarytool store-credentials).")
    parsed = urlsplit(feed_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("The update feed must be an https:// URL.")
    try:
        key = base64.b64decode(public_key, validate=True)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise ValueError("The update public key must be Sparkle's base64 EdDSA public key.")


def _nested(bundle: Path) -> list[Path]:
    """Every Mach-O file and nested bundle, deepest first, so each is signed before whatever contains it."""
    items = set(macho_files(bundle)) | {path for path in bundle.rglob("*") if path.suffix in BUNDLE_SUFFIXES and path.is_dir() and not path.is_symlink()}
    return sorted(items, key=lambda path: (-len(path.relative_to(bundle).parts), str(path)))


def sign_bundle(bundle: Path, identity: str, run) -> None:
    sign = ["/usr/bin/codesign", "--force", "--timestamp", "--options", "runtime", "--sign", identity]
    for path in _nested(bundle):
        run(sign + [path])
    run(sign + [bundle])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", bundle])


def make_dmg(bundle: Path, dmg: Path, run) -> None:
    with tempfile.TemporaryDirectory(prefix="orchestrator-dmg-") as folder:
        stage = Path(folder)
        shutil.copytree(bundle, stage / bundle.name, symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        run(["/usr/bin/hdiutil", "create", "-volname", "Orchestrator", "-srcfolder", stage, "-fs", "HFS+", "-format", "UDZO", "-ov", dmg])


def release(bundle: Path, output: Path, development: bool, identity: str = "", notary_profile: str = "", feed_url: str = "",
            public_key: str = "", run=run_tool) -> Path:
    facts = validate_bundle(bundle)
    if not development:
        check_release_inputs(identity, notary_profile, feed_url, public_key)
        source_info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        if not re.fullmatch(r"[a-f0-9]{40}", source_info.get("OrchestratorSourceCommit", "")) or source_info.get("OrchestratorSourceDirty") is not False:
            raise ValueError("Public releases must be assembled from a clean, identified source commit.")
    output.mkdir(parents=True, exist_ok=True)
    name = f"Orchestrator-{facts['version']}-{facts['architecture']}{'-development' if development else ''}"
    dmg = output / f"{name}.dmg"
    if dmg.exists():
        raise ValueError(f"{dmg.name} already exists. Choose a new output directory.")
    with tempfile.TemporaryDirectory(prefix="orchestrator-release-") as folder:
        app = Path(folder) / "Orchestrator.app"
        shutil.copytree(bundle, app, symlinks=True)
        if not development:
            info_path = app / "Contents/Info.plist"
            info = plistlib.loads(info_path.read_bytes())
            info.update(SUFeedURL=feed_url, SUPublicEDKey=public_key, OrchestratorDevelopmentBuild=False)
            info_path.write_bytes(plistlib.dumps(info))
            sign_bundle(app, identity, run)
        make_dmg(app, dmg, run)
    if not development:
        run(["/usr/bin/codesign", "--force", "--timestamp", "--sign", identity, dmg])
        run(["/usr/bin/xcrun", "notarytool", "submit", dmg, "--keychain-profile", notary_profile, "--wait"])
        run(["/usr/bin/xcrun", "stapler", "staple", dmg])
        run(["/usr/sbin/spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=2", dmg])
    digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
    record = {**facts, "development": development, "file": dmg.name, "sha256": digest, "size": dmg.stat().st_size}
    source_info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    record["commit"] = source_info.get("OrchestratorSourceCommit", "")
    (output / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")
    return dmg


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--development", action="store_true", help="unsigned test image; never a client download")
    parser.add_argument("--signing-identity", default="")
    parser.add_argument("--notary-profile", default="")
    parser.add_argument("--feed-url", default="")
    parser.add_argument("--public-ed-key", default="")
    args = parser.parse_args()
    print(release(args.bundle, args.output, args.development, args.signing_identity, args.notary_profile, args.feed_url, args.public_ed_key))


if __name__ == "__main__":
    main()
