#!/usr/bin/env python3
"""Update one installed manifest dependency with its native manager, then test."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from orchestrator import package_updates


RESULT_MARKER = 'ORCHESTRATOR_PACKAGE_UPDATE_RESULT='


def _test_command(root: Path) -> str | None:
    from orchestrator.project_config import load_project_config
    config = load_project_config(root)
    if config.test_command:
        return config.test_command
    document = root / 'docs' / 'build-test-commands.md'
    if document.is_file():
        content = document.read_text()
        match = re.search(r'^## (?:iOS app )?(?:Tests?|Test suite)\s*$((?:(?!^## ).)*?)^```bash\s*\n(.*?)^```',
                          content, re.MULTILINE | re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(2).strip()
    return config.stack.test_command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=os.environ.get('ORCHESTRATOR_PROJECT_ROOT') or os.getcwd())
    parser.add_argument('--ecosystem', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    try:
        # Validate before loading configuration/detecting test commands.
        package_updates.plan_update(root, args.ecosystem, args.name, args.manifest)
        result = package_updates.execute_update(root, args.ecosystem, args.name, args.manifest, _test_command(root),
                                                say=lambda line: print(line, flush=True))
    except (package_updates.PackageUpdateError, OSError, ValueError) as exc:
        result = {'ok': False, 'stage': 'validation', 'message': str(exc), 'returncode': 1, 'commands': []}
        print(str(exc), flush=True)
    print(RESULT_MARKER + json.dumps(result), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
