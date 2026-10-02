"""Notifications that reach you when the browser tab is closed.

`Tracker` turns successive snapshots (the inbox plus the terminal runs) into
events worth interrupting someone for: something new is waiting on you, or a run
finished. The first snapshot only seeds what's already there, so starting the
server never replays old items. Delivery is a Slack-compatible incoming webhook
(`notification_webhook` in settings); email and desktop alerts for finished
jobs are still sent by the worker itself.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse


class NotifyError(RuntimeError):
    pass


def valid_webhook(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username


def host_of(url: str) -> str:
    return urlparse(url or "").hostname or ""


class Tracker:
    def __init__(self) -> None:
        self._seen: set[str] | None = None
        self._runs: dict[str, bool] = {}  # run id -> was running

    def update(self, items: list[dict[str, Any]], runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Events since the last call: [{kind, key, title, body, path}]. `path` is a UI hash route."""
        ids = {i["id"] for i in items}
        first = self._seen is None
        events: list[dict[str, Any]] = []
        for item in items:
            if not first and item["id"] not in self._seen:
                path = item.get("href") or (f"#/runs/{item['run_id']}" if item["kind"] == "run" else f"#/jobs/{item['job_id']}")
                events.append({"kind": "needs-you", "key": item["id"], "title": item["label"],
                               "body": f"{item['title']}: {item['reason']}".strip(": "), "path": path})
        for run in runs:
            was = self._runs.get(run["id"])
            if was and not run.get("running"):
                name = run.get("title") or run.get("action") or "A run"
                if run.get("exit_code") == 0:
                    target = run.get("result_job") or run.get("job")
                    events.append({"kind": "done", "key": f"{run['id']}:ended", "title": "Finished", "body": f"{name} finished.",
                                   "path": f"#/jobs/{target}" if target else f"#/runs/{run['id']}"})
                elif not run.get("job"):  # a failed run on a job is already in the inbox as a failed job
                    events.append({"kind": "problem", "key": f"{run['id']}:ended", "title": "Problem",
                                   "body": f"{name} failed (exit {run.get('exit_code')}).", "path": f"#/runs/{run['id']}"})
            self._runs[run["id"]] = bool(run.get("running"))
        self._seen = ids
        return events


def payload(event: dict[str, Any], project: str, base_url: str = "") -> dict[str, Any]:
    link = ""
    if base_url:
        base = base_url.split("#")[0]
        if "?" not in base and not base.endswith("/"):
            base += "/"
        link = f" <{base}{event['path']}|Open>"
    return {"text": f"*{event['title']}* · {project}\n{event['body']}{link}"}


def post_webhook(url: str, body: dict[str, Any], opener: Callable[..., Any] = urllib.request.urlopen) -> None:
    if not valid_webhook(url):
        raise NotifyError("The webhook must be an https:// URL")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise NotifyError(f"The webhook answered {response.status}")
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"The webhook answered {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NotifyError(f"Couldn't reach the webhook: {getattr(exc, 'reason', exc)}") from exc
