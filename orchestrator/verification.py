"""Local baseline measurements for independent verification checks."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator import test_commands


def command_digest(command: test_commands.ExtraTestCommand) -> str:
    definition = json.dumps(asdict(command), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(definition.encode()).hexdigest()


def observe(
    commands: list[test_commands.ExtraTestCommand], root: Path, out_dir: Path, job_id: str,
) -> list[dict[str, Any]]:
    """Run the normal local sequence and time the opted-in pair separately."""
    invocation_id = uuid4().hex
    checks: list[dict[str, Any]] = []
    pair_started = 0.0
    elapsed = 0.0

    def run(command: test_commands.ExtraTestCommand, root: Path) -> dict[str, Any]:
        nonlocal pair_started, elapsed
        if command.parallel_verify and not checks:
            pair_started = time.monotonic()
        result = test_commands.run_one(command, root)
        if command.parallel_verify:
            checks.append({
                "name": command.name,
                "digest": command_digest(command),
                **{key: result[key] for key in ("status", "seconds", "attempts", "flaky", "reason") if key in result},
            })
            elapsed = time.monotonic() - pair_started
        return result

    results = test_commands.run_all(commands, root, out_dir, run_command=run)
    directory = out_dir / "verification"
    try:
        directory.mkdir(exist_ok=True)
        record = {"job_id": job_id, "invocation_id": invocation_id, "mode": "observe",
                  "checks": checks, "elapsed_seconds": round(elapsed, 3)}
        temporary = directory / f"{invocation_id}.tmp"
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temporary.replace(directory / f"{invocation_id}.json")
    except OSError as exc:
        print(f"      - Warning: unable to save verification measurements: {exc}")
    return results
