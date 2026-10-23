"""Prepare website download metadata from verified release images and acceptance evidence.

This does not publish anything or create clean-machine evidence. The evidence JSON
must name the source commit and passing clean-machine results for both architectures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def prepare(folder: Path, base_url: str, commit: str, acceptance: dict) -> dict:
    url = urlsplit(base_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Artifact base URL must be HTTPS without credentials, query or fragment.")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Select an exact source commit.")
    if not isinstance(acceptance, dict) or acceptance.get("commit") != commit:
        raise ValueError("Clean-machine evidence must match the release commit.")
    result = {"commit": commit, "minimum_macos": "13.0"}
    identity = None
    for arch in ("arm64", "x86_64"):
        evidence = acceptance.get(arch)
        if not isinstance(evidence, dict) or evidence.get("passed") is not True or not evidence.get("notes"):
            raise ValueError(f"Missing clean-machine acceptance evidence for {arch}.")
        candidates = []
        for path in folder.glob("*.json"):
            receipt = json.loads(path.read_text())
            if receipt.get("architecture") == arch:
                candidates.append(receipt)
        if len(candidates) != 1:
            raise ValueError(f"Expected exactly one release receipt for {arch}.")
        receipt = candidates[0]
        if receipt.get("commit") != commit:
            raise ValueError("Both release images must be built from the selected commit.")
        if receipt.get("development") is not False:
            raise ValueError("Development images cannot become public downloads.")
        version, build = receipt.get("version"), receipt.get("build")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version) or type(build) is not int or build < 1:
            raise ValueError("Release version and build are required.")
        if identity and identity != (version, build):
            raise ValueError("Both architectures must have the same version and build.")
        identity = version, build
        filename = receipt.get("file", "")
        if filename != f"Orchestrator-{version}-{arch}.dmg":
            raise ValueError("Unexpected release artifact name.")
        artifact = folder / filename
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if receipt.get("sha256") != digest or receipt.get("size") != artifact.stat().st_size:
            raise ValueError("Release image does not match its receipt.")
        result[arch] = {"url": base_url.rstrip("/") + "/" + filename, "sha256": digest}
    result.update(version=identity[0], build=identity[1])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = prepare(args.artifacts, args.base_url, args.commit, json.loads(args.acceptance.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        stream.write(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
