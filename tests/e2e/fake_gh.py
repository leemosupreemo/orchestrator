#!/usr/bin/env python3
"""A stand-in for the GitHub CLI, for end-to-end tests that must not touch GitHub.

Issues and pull requests live in the JSON file named by $FAKE_GH_STATE. It speaks only the
subcommands the orchestrator uses, with the output shapes it parses. Anything else exits 1
and is recorded in `unsupported`, so a test can tell when the orchestrator starts using a
command the fake doesn't know.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/e2e/dummy"
STATE = Path(os.environ.get("FAKE_GH_STATE", "fake-gh-state.json"))


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"next": 1, "issues": {}, "prs": {}, "calls": [], "unsupported": []}


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2))


def flag(args: list[str], name: str, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def flags(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)]


def emit(data, args: list[str]) -> None:
    jq = flag(args, "--jq")
    if jq and jq.startswith(".") and isinstance(data, dict):
        print(data.get(jq[1:], ""))
    else:
        print(json.dumps(data))


def pick(obj: dict, args: list[str]) -> dict:
    wanted = (flag(args, "--json", "") or "").split(",")
    return {k: v for k, v in obj.items() if not wanted or wanted == [""] or k in wanted}


def main(argv: list[str]) -> int:
    state = load()
    state["calls"].append(argv)
    args = list(argv)
    if args[:2] == ["auth", "status"]:
        print("github.com\n  ✓ Logged in to github.com account e2e-bot")
        save(state)
        return 0
    if args[:2] == ["repo", "view"]:
        emit({"url": REPO_URL, "name": "dummy"}, args)
        save(state)
        return 0
    group, action = (args + ["", ""])[:2]
    rest = args[2:]
    number = rest[0] if rest and rest[0].isdigit() else None
    if group == "issue" and action == "create":
        n = state["next"]; state["next"] += 1
        state["issues"][str(n)] = {"number": n, "title": flag(rest, "--title", ""), "body": flag(rest, "--body", ""), "labels": flags(rest, "--label"),
                                   "state": "OPEN", "url": f"{REPO_URL}/issues/{n}", "comments": []}
        save(state)
        print(f"{REPO_URL}/issues/{n}")
        return 0
    if group == "issue" and number and str(number) in state["issues"]:
        issue = state["issues"][number]
        if action == "view":
            emit(pick(issue, rest), rest)
        elif action == "edit":
            if flag(rest, "--body") is not None:
                issue["body"] = flag(rest, "--body")
            issue["labels"] = [l for l in issue["labels"] if l not in flags(rest, "--remove-label")] + flags(rest, "--add-label")
        elif action == "comment":
            issue["comments"].append(flag(rest, "--body", ""))
        elif action == "close":
            issue["state"] = "CLOSED"
        else:
            state["unsupported"].append(argv); save(state); return 1
        save(state)
        return 0
    if group == "issue" and action == "list":
        want = (flag(rest, "--state", "open") or "open").upper()
        print(json.dumps([{"number": i["number"]} for i in state["issues"].values() if want == "ALL" or i["state"] == want]))
        save(state)
        return 0
    if group == "pr" and action == "create":
        n = state["next"]; state["next"] += 1
        state["prs"][str(n)] = {"number": n, "title": flag(rest, "--title", ""), "body": flag(rest, "--body", ""), "headRefName": flag(rest, "--head", ""),
                                "baseRefName": flag(rest, "--base", "main"), "isDraft": "--draft" in rest, "state": "OPEN", "url": f"{REPO_URL}/pull/{n}", "comments": []}
        save(state)
        print(f"{REPO_URL}/pull/{n}")
        return 0
    if group == "pr" and action == "list":
        head = flag(rest, "--head")
        print(json.dumps([pick(p, rest) for p in state["prs"].values() if not head or p["headRefName"] == head]))
        save(state)
        return 0
    if group == "pr" and action == "checks":
        print("no checks reported")
        save(state)
        return 0
    if group == "pr" and number and number in state["prs"]:
        pr = state["prs"][number]
        if action == "view":
            data = dict(pr, files=[], commits=[])
            emit(pick(data, rest), rest)
        elif action == "edit":
            pr["body"] = flag(rest, "--body", pr["body"])
        elif action == "comment":
            pr["comments"].append(flag(rest, "--body", ""))
        elif action == "diff":
            out = subprocess.run(["git", "diff", f"{pr['baseRefName']}...{pr['headRefName']}"], capture_output=True, text=True)
            print(out.stdout)
        elif action == "merge":
            subprocess.run(["git", "checkout", "-q", pr["baseRefName"]], check=False)
            subprocess.run(["git", "merge", "--no-ff", "-q", "-m", f"Merge PR #{number}", pr["headRefName"]], check=False)
            pr["state"] = "MERGED"
        else:
            state["unsupported"].append(argv); save(state); return 1
        save(state)
        return 0
    state["unsupported"].append(argv)
    save(state)
    print(f"fake gh: unsupported command: gh {' '.join(argv)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
