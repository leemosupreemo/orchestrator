"""Connections to Jira, Trello, Sentry and Figma.

Each provider can: validate credentials (`test`), find things (`search`), and
turn one item into plain-markdown context (`lookup`) that gets written into a
job's spec. Markdown is the common denominator, so any LLM can use it; Figma
frames also come with a PNG for vision-capable models.

Everything goes through `http_json` / `http_bytes`, so tests replace those two
functions and never touch the network. Credentials are never returned to
callers; errors are scrubbed of them.
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

TIMEOUT = 15
# Trello's "Allow" sign-in needs one app key (public, not a secret) from a Trello Power-Up registered for
# Orchestrator, with the hosted app and http://127.0.0.1:8765 listed as allowed origins. Set it here once it exists,
# or per computer with ORCHESTRATOR_TRELLO_APP_KEY (or `trello_app_key` in settings). Without it, Trello falls back to
# pasting a key and token.
TRELLO_APP_KEY = ""
EXPIRY_WARN_DAYS = 14
MAX_CONTEXT_CHARS = 20_000
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class IntegrationError(Exception):
    """A problem worth showing to the user as-is."""


# --------------------------------------------------------------------------- http


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _request(url: str, headers: dict[str, str], secrets: list[str], method: str = "GET", body: Any = None) -> bytes:
    if urllib.parse.urlparse(url).scheme != "https":
        raise IntegrationError("Only https addresses are supported.")
    data = None
    extra = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        extra["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Accept": "application/json", "User-Agent": "orchestrator", **extra, **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as resp:
            return resp.read(MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        reason = {400: "The service rejected the request.", 401: "The credentials were rejected.",
                  403: "That account isn't allowed to do this.", 404: "Not found. Check the address or id.",
                  429: "Rate limited. Try again shortly."}.get(exc.code, f"The service answered {exc.code}.")
        if exc.code == 401:
            raise RejectedCredentials(reason) from None
        raise IntegrationError(reason) from None
    except urllib.error.URLError as exc:
        raise IntegrationError(_scrub(f"Couldn't reach the service: {exc.reason}", secrets)) from None
    except TimeoutError:
        raise IntegrationError("The service took too long to answer.") from None


class RejectedCredentials(IntegrationError):
    """The service said the token is wrong or expired: the connection needs reconnecting."""


def _scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def http_json(url: str, headers: dict[str, str] | None = None, secrets: list[str] | None = None,
              method: str = "GET", body: Any = None) -> Any:
    raw = _request(url, headers or {}, secrets or [], method, body)
    if not raw.strip():
        return {}  # e.g. 204 No Content from a transition or a comment delete
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrationError("The service sent something that isn't JSON. Check the address.") from None


def http_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    data = _request(url, headers or {}, [])
    if len(data) > MAX_IMAGE_BYTES:
        raise IntegrationError("That image is too large to attach.")
    return data


# --------------------------------------------------------------------------- helpers


def _clip(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]…\n"


def _base_url(value: str, label: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        raise IntegrationError(f"{label} is required.")
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise IntegrationError(f"{label} must be a plain https address, like https://yourteam.atlassian.net.")
    return f"https://{parsed.netloc}"


def _adf_to_text(node: Any) -> str:
    """Atlassian Document Format (Jira descriptions) to readable text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    kind = node.get("type")
    inner = _adf_to_text(node.get("content"))
    if kind == "text":
        return node.get("text", "")
    if kind in ("paragraph", "heading"):
        prefix = "#" * int(node.get("attrs", {}).get("level", 2)) + " " if kind == "heading" else ""
        return f"{prefix}{inner}\n\n"
    if kind == "hardBreak":
        return "\n"
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in ("bulletList", "orderedList"):
        return inner + "\n"
    if kind == "codeBlock":
        return f"```\n{inner}\n```\n\n"
    if kind == "mention":
        return node.get("attrs", {}).get("text", "")
    return inner


@dataclass
class Item:
    """One thing found in a connected app."""
    provider: str
    ref: str
    title: str
    url: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "ref": self.ref, "title": self.title, "url": self.url, "detail": self.detail}


@dataclass
class Context:
    """An item rendered for a job: markdown for any LLM, plus optional image bytes."""
    item: Item
    markdown: str
    image: bytes | None = None
    image_name: str = ""


# --------------------------------------------------------------------------- providers


class Provider:
    id = ""
    name = ""
    blurb = ""
    # (key, label, secret?, placeholder, help)
    fields: list[tuple[str, str, bool, str, str]] = []
    token_url = ""                 # where to create the token, linked from the connect form
    token_max_days: int | None = None  # tokens there expire after at most this many days (None: they don't)

    def __init__(self, creds: dict[str, str]):
        self.c = {k: str(v).strip() for k, v in creds.items() if v is not None}

    def secrets(self) -> list[str]:
        return [self.c.get(k, "") for k, _, secret, _, _ in self.fields if secret]

    def test(self) -> str:
        raise NotImplementedError

    def search(self, query: str) -> list[Item]:
        raise NotImplementedError

    def lookup(self, ref: str) -> Context:
        raise NotImplementedError

    def describe(self) -> str:
        """Non-secret summary of what's connected."""
        return ""

    # Write-back. Providers that can't (Figma) leave `can_write` False and are skipped.
    can_write = False
    move_label = ""          # what "move on merge" means here, shown in the UI
    move_default = ""

    def comment(self, ref: str, text: str) -> str:
        raise IntegrationError(f"{self.name} can't be written to.")

    def move(self, ref: str, target: str) -> str:
        raise IntegrationError(f"{self.name} can't be moved.")


class Jira(Provider):
    id, name = "jira", "Jira"
    blurb = "Tie jobs to Jira tickets. The ticket's text goes to the AI with the job."
    fields = [
        ("site", "Jira site", False, "yourteam.atlassian.net", "Your Jira Cloud address."),
        ("email", "Account email", False, "you@company.com", "The email you sign in to Atlassian with."),
        ("token", "API token", True, "", "Create one at id.atlassian.com → Security → API tokens."),
    ]
    token_url = "https://id.atlassian.com/manage-profile/security/api-tokens"
    token_max_days = 365
    KEY = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

    def _site(self) -> str:
        return _base_url(self.c.get("site", ""), "Jira site")

    def _headers(self) -> dict[str, str]:
        raw = f"{self.c.get('email', '')}:{self.c.get('token', '')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def _get(self, path: str) -> Any:
        return http_json(self._site() + path, self._headers(), self.secrets())

    def describe(self) -> str:
        return f"{self.c.get('email', '')} · {urllib.parse.urlparse(self._site()).hostname}"

    def test(self) -> str:
        me = self._get("/rest/api/3/myself")
        return me.get("displayName") or me.get("emailAddress") or "connected"

    def search(self, query: str) -> list[Item]:
        q = query.strip()
        if self.KEY.fullmatch(q.upper()):
            jql = f'key = "{q.upper()}"'
        elif q:
            safe = q.replace("\\", " ").replace('"', " ")
            jql = f'text ~ "{safe}" ORDER BY updated DESC'
        else:
            jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
        data = self._get("/rest/api/3/search/jql?" + urllib.parse.urlencode(
            {"jql": jql, "maxResults": 10, "fields": "summary,status"}))
        site = self._site()
        return [Item("jira", i["key"], i["fields"].get("summary", ""), f"{site}/browse/{i['key']}",
                     (i["fields"].get("status") or {}).get("name", "")) for i in data.get("issues", [])]

    can_write = True
    move_label = "Move ticket to this status when merged"
    move_default = "Done"

    def _key(self, ref: str) -> str:
        match = self.KEY.search(ref.upper() if "://" not in ref else ref)
        if not match:
            raise IntegrationError("That doesn't look like a Jira ticket.")
        return match.group(1)

    def _write(self, path: str, body: Any) -> Any:
        return http_json(self._site() + path, self._headers(), self.secrets(), "POST", body)

    def comment(self, ref: str, text: str) -> str:
        key = self._key(ref)
        adf = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}
        self._write(f"/rest/api/3/issue/{urllib.parse.quote(key)}/comment", {"body": adf})
        return f"Commented on {key}"

    def move(self, ref: str, target: str) -> str:
        key = self._key(ref)
        data = self._get(f"/rest/api/3/issue/{urllib.parse.quote(key)}/transitions")
        options = data.get("transitions", [])
        wanted = (target or self.move_default).strip().lower()
        match = next((t for t in options if t.get("name", "").lower() == wanted or (t.get("to") or {}).get("name", "").lower() == wanted), None)
        if not match:
            names = ", ".join(sorted({t.get("name", "") for t in options})) or "none available"
            raise IntegrationError(f"{key} has no transition to '{target or self.move_default}' (available: {names}).")
        self._write(f"/rest/api/3/issue/{urllib.parse.quote(key)}/transitions", {"transition": {"id": match["id"]}})
        return f"Moved {key} to {(match.get('to') or {}).get('name') or match.get('name')}"

    def lookup(self, ref: str) -> Context:
        match = self.KEY.search(ref.upper() if "://" not in ref else ref)
        if not match:
            raise IntegrationError("That doesn't look like a Jira ticket. Use a key like ABC-123 or its link.")
        key = match.group(1)
        d = self._get(f"/rest/api/3/issue/{urllib.parse.quote(key)}?fields=summary,description,status,issuetype,priority,labels,assignee,comment")
        f = d.get("fields", {})
        title = f.get("summary", key)
        url = f"{self._site()}/browse/{key}"
        lines = [f"## Jira {key}: {title}", f"- Link: {url}",
                 f"- Type: {(f.get('issuetype') or {}).get('name', '?')} · Status: {(f.get('status') or {}).get('name', '?')} · Priority: {(f.get('priority') or {}).get('name', '?')}"]
        if f.get("labels"):
            lines.append(f"- Labels: {', '.join(f['labels'])}")
        body = _adf_to_text(f.get("description")).strip()
        lines += ["", body or "_No description._"]
        comments = ((f.get("comment") or {}).get("comments") or [])[-5:]
        if comments:
            lines += ["", "### Recent comments"]
            lines += [f"- **{(c.get('author') or {}).get('displayName', 'Someone')}**: {_adf_to_text(c.get('body')).strip()[:600]}" for c in comments]
        return Context(Item("jira", key, title, url, (f.get("status") or {}).get("name", "")), _clip("\n".join(lines)))


class Trello(Provider):
    id, name = "trello", "Trello"
    blurb = "Tie jobs to Trello cards. The card's text and checklists go to the AI with the job."
    fields = [
        ("key", "API key", False, "", "From trello.com/power-ups/admin (create a Power-Up to get a key)."),
        ("token", "API token", True, "", "Generate a token from the same page."),
    ]
    token_url = "https://trello.com/power-ups/admin"
    CARD = re.compile(r"trello\.com/c/([A-Za-z0-9]+)")

    def _get(self, path: str, **params: str) -> Any:
        q = urllib.parse.urlencode({"key": self.c.get("key", ""), "token": self.c.get("token", ""), **params})
        return http_json(f"https://api.trello.com/1{path}?{q}", {}, self.secrets() + [self.c.get("key", "")])

    def describe(self) -> str:
        return self.c.get("member", "") or "Trello account"

    def test(self) -> str:
        me = self._get("/members/me", fields="fullName,username")
        self.c["member"] = me.get("fullName") or me.get("username") or ""
        return self.c["member"] or "connected"

    def search(self, query: str) -> list[Item]:
        q = query.strip()
        if q:
            data = self._get("/search", query=q, modelTypes="cards", cards_limit="10",
                             card_fields="name,url,shortLink,idList")
            cards = data.get("cards", [])
        else:
            cards = self._get("/members/me/cards", filter="open", fields="name,url,shortLink")[:10]
        return [Item("trello", c["shortLink"], c.get("name", ""), c.get("url", "")) for c in cards]

    can_write = True
    move_label = "Move card to this list when merged"
    move_default = "Done"

    def _card_id(self, ref: str) -> str:
        match = self.CARD.search(ref)
        card_id = match.group(1) if match else ref.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{6,32}", card_id):
            raise IntegrationError("That doesn't look like a Trello card.")
        return card_id

    def _send(self, method: str, path: str, **params: str) -> Any:
        q = urllib.parse.urlencode({"key": self.c.get("key", ""), "token": self.c.get("token", ""), **params})
        return http_json(f"https://api.trello.com/1{path}?{q}", {}, self.secrets() + [self.c.get("key", "")], method)

    def comment(self, ref: str, text: str) -> str:
        self._send("POST", f"/cards/{self._card_id(ref)}/actions/comments", text=text)
        return "Commented on the Trello card"

    def move(self, ref: str, target: str) -> str:
        card_id = self._card_id(ref)
        board = self._get(f"/cards/{card_id}", fields="idBoard").get("idBoard")
        lists = self._get(f"/boards/{board}/lists", filter="open", fields="name")
        wanted = (target or self.move_default).strip().lower()
        match = next((l for l in lists if l.get("name", "").lower() == wanted), None)
        if not match:
            raise IntegrationError(f"The board has no list named '{target or self.move_default}' (lists: {', '.join(l.get('name', '') for l in lists)}).")
        self._send("PUT", f"/cards/{card_id}", idList=match["id"])
        return f"Moved the card to {match['name']}"

    def lookup(self, ref: str) -> Context:
        match = self.CARD.search(ref)
        card_id = match.group(1) if match else ref.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{6,32}", card_id):
            raise IntegrationError("That doesn't look like a Trello card. Paste its link.")
        c = self._get(f"/cards/{card_id}", fields="name,desc,url,labels,due,shortLink", checklists="all")
        title = c.get("name", card_id)
        lines = [f"## Trello card: {title}", f"- Link: {c.get('url', '')}"]
        labels = [l.get("name") or l.get("color") for l in c.get("labels", []) if l.get("name") or l.get("color")]
        if labels:
            lines.append(f"- Labels: {', '.join(labels)}")
        if c.get("due"):
            lines.append(f"- Due: {c['due']}")
        lines += ["", (c.get("desc") or "").strip() or "_No description._"]
        for cl in c.get("checklists", []):
            lines += ["", f"### {cl.get('name', 'Checklist')}"]
            lines += [f"- [{'x' if i.get('state') == 'complete' else ' '}] {i.get('name', '')}" for i in cl.get("checkItems", [])]
        return Context(Item("trello", c.get("shortLink", card_id), title, c.get("url", "")), _clip("\n".join(lines)))


class Sentry(Provider):
    id, name = "sentry", "Sentry"
    blurb = "Pull a Sentry issue's stack trace, breadcrumbs and tags into a job as logs."
    fields = [
        ("host", "Sentry address", False, "sentry.io", "Leave as sentry.io unless you self-host."),
        ("org", "Organization slug", False, "my-org", "From your Sentry URL: sentry.io/organizations/<slug>."),
        ("project", "Project slug (optional)", False, "my-app", "Limits the suggestion list to one project."),
        ("token", "Auth token", True, "", "A personal token with org:read, project:read, event:read and event:write "
                                           "(event:write lets jobs comment on and resolve issues). Device logs use it too."),
    ]
    token_url = "https://sentry.io/settings/account/api/auth-tokens/"
    ISSUE = re.compile(r"/issues/(\d+)")

    def _host(self) -> str:
        return _base_url(self.c.get("host") or "sentry.io", "Sentry address")

    def _get(self, path: str) -> Any:
        return http_json(self._host() + path, {"Authorization": f"Bearer {self.c.get('token', '')}"}, self.secrets())

    def describe(self) -> str:
        return f"{self.c.get('org', '')}" + (f" / {self.c['project']}" if self.c.get("project") else "")

    def test(self) -> str:
        org = urllib.parse.quote(self.c.get("org", ""), safe="")
        if not org:
            raise IntegrationError("Organization slug is required.")
        data = self._get(f"/api/0/organizations/{org}/")
        return data.get("name") or self.c.get("org", "connected")

    def search(self, query: str) -> list[Item]:
        org = urllib.parse.quote(self.c.get("org", ""), safe="")
        q = query.strip() or "is:unresolved"
        base = f"/api/0/projects/{org}/{urllib.parse.quote(self.c['project'], safe='')}/issues/" if self.c.get("project") \
            else f"/api/0/organizations/{org}/issues/"
        issues = self._get(base + "?" + urllib.parse.urlencode({"query": q, "limit": 10}))
        return [Item("sentry", str(i["id"]), i.get("title", ""), i.get("permalink", ""),
                     f"{i.get('count', '?')} events · {i.get('level', '')}") for i in issues]

    can_write = True
    move_label = "Resolve the issue when merged"
    move_default = ""

    def _issue_id(self, ref: str) -> str:
        match = self.ISSUE.search(ref)
        issue_id = match.group(1) if match else ref.strip()
        if not issue_id.isdigit():
            raise IntegrationError("Use a Sentry issue link or its numeric id.")
        return issue_id

    def _send(self, method: str, path: str, body: Any) -> Any:
        return http_json(self._host() + path, {"Authorization": f"Bearer {self.c.get('token', '')}"}, self.secrets(), method, body)

    def comment(self, ref: str, text: str) -> str:
        self._send("POST", f"/api/0/issues/{self._issue_id(ref)}/comments/", {"text": text})
        return "Commented on the Sentry issue"

    def move(self, ref: str, target: str) -> str:
        self._send("PUT", f"/api/0/issues/{self._issue_id(ref)}/", {"status": "resolved"})
        return "Resolved the Sentry issue"

    def lookup(self, ref: str) -> Context:
        match = self.ISSUE.search(ref)
        issue_id = match.group(1) if match else ref.strip()
        if not issue_id.isdigit():
            raise IntegrationError("Use a Sentry issue link or its numeric id.")
        issue = self._get(f"/api/0/issues/{issue_id}/")
        title = issue.get("title", issue_id)
        lines = [f"## Sentry issue {issue.get('shortId', issue_id)}: {title}", f"- Link: {issue.get('permalink', '')}",
                 f"- Level: {issue.get('level', '?')} · Events: {issue.get('count', '?')} · Users: {issue.get('userCount', '?')}",
                 f"- First seen: {issue.get('firstSeen', '?')} · Last seen: {issue.get('lastSeen', '?')}"]
        if issue.get("culprit"):
            lines.append(f"- Culprit: `{issue['culprit']}`")
        try:
            event = self._get(f"/api/0/issues/{issue_id}/events/latest/")
        except IntegrationError:
            event = {}
        lines += self._event_lines(event)
        return Context(Item("sentry", issue_id, title, issue.get("permalink", ""), issue.get("level", "")), _clip("\n".join(lines)))

    @staticmethod
    def _event_lines(event: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for entry in event.get("entries", []):
            if entry.get("type") == "exception":
                for exc in (entry.get("data") or {}).get("values", []):
                    out += ["", f"### {exc.get('type', 'Exception')}: {exc.get('value', '')}", "```"]
                    frames = ((exc.get("stacktrace") or {}).get("frames")) or []
                    for fr in frames[-15:][::-1]:  # innermost call first
                        where = f"{fr.get('filename') or fr.get('module') or '?'}:{fr.get('lineno', '?')}"
                        out.append(f"{where} in {fr.get('function') or '?'}" + (" [app]" if fr.get("inApp") else ""))
                        if fr.get("context_line"):
                            out.append(f"    {fr['context_line'].strip()}")
                    out.append("```")
            elif entry.get("type") == "breadcrumbs":
                crumbs = ((entry.get("data") or {}).get("values")) or []
                if crumbs:
                    out += ["", "### Breadcrumbs (latest 15)"]
                    out += [f"- {c.get('timestamp', '')} [{c.get('category', '')}] {c.get('message') or json.dumps(c.get('data') or {})[:200]}"
                            for c in crumbs[-15:]]
            elif entry.get("type") == "message":
                out += ["", "### Message", (entry.get("data") or {}).get("formatted", "")]
        tags = event.get("tags") or []
        if tags:
            out += ["", "### Tags", ", ".join(f"{t.get('key')}={t.get('value')}" for t in tags[:25])]
        return out


class Figma(Provider):
    id, name = "figma", "Figma"
    blurb = "Turn a Figma frame into a text spec (and a PNG) the AI can build from."
    fields = [("token", "Personal access token", True, "", "Figma → Settings → Security → Personal access tokens (file read access).")]
    token_url = "https://www.figma.com/settings"
    token_max_days = 90
    URL = re.compile(r"figma\.com/(?:design|file|proto|board)/([A-Za-z0-9]+)")

    def _get(self, path: str) -> Any:
        return http_json("https://api.figma.com" + path, {"X-Figma-Token": self.c.get("token", "")}, self.secrets())

    def describe(self) -> str:
        return self.c.get("handle", "") or "Figma account"

    def test(self) -> str:
        me = self._get("/v1/me")
        self.c["handle"] = me.get("handle") or me.get("email") or ""
        return self.c["handle"] or "connected"

    def search(self, query: str) -> list[Item]:
        return []  # Figma has no search API for a personal token: designs are linked by URL

    @classmethod
    def parse(cls, ref: str) -> tuple[str, str | None]:
        match = cls.URL.search(ref)
        if not match:
            raise IntegrationError("Paste a Figma link to a file or frame.")
        node = urllib.parse.parse_qs(urllib.parse.urlparse(ref).query).get("node-id", [None])[0]
        return match.group(1), (node.replace("-", ":") if node else None)

    def lookup(self, ref: str) -> Context:
        key, node_id = self.parse(ref)
        if node_id:
            data = self._get(f"/v1/files/{key}/nodes?ids={urllib.parse.quote(node_id)}&depth=6")
            name = data.get("name", key)
            roots = [n.get("document") for n in (data.get("nodes") or {}).values() if n and n.get("document")]
        else:
            data = self._get(f"/v1/files/{key}?depth=3")
            name = data.get("name", key)
            roots = (data.get("document") or {}).get("children", [])
        if not roots:
            raise IntegrationError("Couldn't find that frame in the file.")
        title = f"{name} / {roots[0].get('name', '')}" if node_id else name
        url = ref.split("#")[0]
        lines = [f"## Figma design: {title}", f"- Link: {url}", "", "Layer tree (name · size · text · colors):", "```"]
        for root in roots[:3]:
            self._describe(root, 0, lines)
        lines.append("```")
        image, image_name = None, ""
        if node_id:
            try:
                imgs = self._get(f"/v1/images/{key}?ids={urllib.parse.quote(node_id)}&format=png&scale=2").get("images") or {}
                img_url = next(iter(imgs.values()), None)
                if img_url:
                    image, image_name = http_bytes(img_url), f"figma-{re.sub(r'[^A-Za-z0-9]+', '-', node_id)}.png"
                    lines += ["", f"A rendered image of this frame is attached as `{image_name}`."]
            except IntegrationError:
                lines += ["", "_(Couldn't render an image of this frame.)_"]
        return Context(Item("figma", f"{key}{'#' + node_id if node_id else ''}", title, url), _clip("\n".join(lines)), image, image_name)

    @staticmethod
    def _hex(color: dict[str, float]) -> str:
        return "#%02X%02X%02X" % tuple(round(color.get(c, 0) * 255) for c in "rgb")

    def _describe(self, node: dict[str, Any], depth: int, out: list[str], budget: list[int] | None = None) -> None:
        budget = budget if budget is not None else [250]
        if budget[0] <= 0 or depth > 8:
            return
        budget[0] -= 1
        box = node.get("absoluteBoundingBox") or {}
        bits = [f"{node.get('type', '?')} {node.get('name', '')!r}"]
        if box:
            bits.append(f"{round(box.get('width', 0))}x{round(box.get('height', 0))}")
        if node.get("characters"):
            bits.append(f"text={node['characters'][:120]!r}")
        fills = [self._hex(f["color"]) for f in node.get("fills", []) if f.get("type") == "SOLID" and f.get("visible", True) and f.get("color")]
        if fills:
            bits.append("fill=" + ",".join(fills[:2]))
        if node.get("layoutMode"):
            bits.append(f"layout={node['layoutMode'].lower()} gap={node.get('itemSpacing', 0)}")
        out.append("  " * depth + " · ".join(bits))
        for child in node.get("children", []) or []:
            self._describe(child, depth + 1, out, budget)


PROVIDERS: dict[str, type[Provider]] = {p.id: p for p in (Jira, Trello, Sentry, Figma)}


def provider_for(provider_id: str, creds: dict[str, str]) -> Provider:
    cls = PROVIDERS.get(provider_id)
    if not cls:
        raise IntegrationError("Unknown connection.")
    return cls(creds)


WRITEBACK_DEFAULTS = {"comment_pr": True, "comment_merge": True, "move_on_merge": False, "target": ""}


def writeback_options(options: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(WRITEBACK_DEFAULTS)
    for key, default in WRITEBACK_DEFAULTS.items():
        if options and key in options and isinstance(options[key], type(default)):
            merged[key] = options[key]
    merged["target"] = str(merged["target"]).strip()[:80]
    return merged


def trello_app_key(settings: dict[str, Any] | None = None) -> str:
    return (os.environ.get("ORCHESTRATOR_TRELLO_APP_KEY") or str((settings or {}).get("trello_app_key") or "")
            or TRELLO_APP_KEY).strip()


def expiry_view(expires: str, today: date | None = None) -> dict[str, Any] | None:
    """When a saved token stops working, and whether to warn: None when it doesn't expire (or wasn't recorded)."""
    try:
        when = date.fromisoformat(expires)
    except (TypeError, ValueError):
        return None
    left = (when - (today or date.today())).days
    return {"on": when.isoformat(), "days_left": left, "expired": left < 0, "soon": 0 <= left <= EXPIRY_WARN_DAYS}


def public_catalog(saved: dict[str, dict[str, str]], options: dict[str, dict[str, Any]] | None = None,
                   *, status: dict[str, dict[str, Any]] | None = None, settings: dict[str, Any] | None = None,
                   suggest: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """What the UI shows: never any secret values."""
    out = []
    for pid, cls in PROVIDERS.items():
        creds = saved.get(pid) or {}
        connected = bool(creds)
        try:
            summary = cls(creds).describe() if connected else ""
        except IntegrationError:
            summary = ""
        out.append({
            "id": pid, "name": cls.name, "blurb": cls.blurb, "connected": connected, "summary": summary,
            "searchable": pid != "figma",
            "can_write": cls.can_write, "move_label": cls.move_label, "move_default": cls.move_default,
            "options": writeback_options((options or {}).get(pid)),
            "fields": [{"key": k, "label": label, "secret": secret, "placeholder": ph, "help": hlp}
                       for k, label, secret, ph, hlp in cls.fields],
            "token_url": cls.token_url, "token_max_days": cls.token_max_days,
            "expiry": expiry_view(creds.get("expires", "")) if connected else None,
            "rejected": bool(connected and (status or {}).get(pid, {}).get("rejected_at")),
            "suggest": (suggest or {}).get(pid) or {},
            **({"authorize_key": trello_app_key(settings)} if pid == "trello" and trello_app_key(settings) else {}),
        })
    return out


def connect(provider_id: str, values: dict[str, str], previous: dict[str, str] | None = None) -> tuple[dict[str, str], str]:
    """Validate against the live service; returns (creds to store, who we're connected as).
    Blank secret fields keep the previously saved value."""
    cls = PROVIDERS.get(provider_id)
    if not cls:
        raise IntegrationError("Unknown connection.")
    creds: dict[str, str] = {}
    for key, label, secret, _, _ in cls.fields:
        value = str(values.get(key) or "").strip() or (previous or {}).get(key, "")
        optional = "optional" in label.lower()
        if not value and not optional and not (key == "host"):
            raise IntegrationError(f"{label} is required.")
        if value:
            creds[key] = value
    if cls.token_max_days:
        raw = str(values.get("expires") or "").strip()
        latest = date.today() + timedelta(days=cls.token_max_days)
        if raw:
            try:
                when = date.fromisoformat(raw)
            except ValueError:
                raise IntegrationError("Expires on must be a date, like 2027-01-31.") from None
            if when < date.today():
                raise IntegrationError("That token has already expired. Create a new one.")
            creds["expires"] = min(when, latest).isoformat()
        elif (previous or {}).get("expires") and not str(values.get("token") or "").strip():
            creds["expires"] = previous["expires"]  # same token as before: same expiry
        else:
            creds["expires"] = latest.isoformat()  # the longest it can live; the person can set the real date
    provider = cls(creds)
    who = provider.test()
    # keep anything learned during the test (e.g. display name), minus nothing secret
    creds.update({k: v for k, v in provider.c.items() if k not in creds and v})
    return creds, who


def build_context(links: list[dict[str, str]], saved: dict[str, dict[str, str]],
                  fetch: Callable[[Provider, str], Context] | None = None) -> list[Context]:
    """Fetch every linked item; raises IntegrationError naming the one that failed."""
    contexts: list[Context] = []
    for link in links:
        pid, ref = str(link.get("provider", "")), str(link.get("ref", "")).strip()
        if pid not in PROVIDERS:
            raise IntegrationError("Unknown connection.")
        if pid not in saved:
            raise IntegrationError(f"{PROVIDERS[pid].name} isn't connected.")
        provider = PROVIDERS[pid](saved[pid])
        try:
            contexts.append(fetch(provider, ref) if fetch else provider.lookup(ref))
        except IntegrationError as exc:
            err = type(exc)(f"{PROVIDERS[pid].name}: {exc}")  # keeps RejectedCredentials a RejectedCredentials
            err.provider = pid
            raise err from None
    return contexts


def spec_block(contexts: list[Context]) -> str:
    if not contexts:
        return ""
    return "\n\n---\n# Context from connected apps\n\n" + "\n\n".join(c.markdown for c in contexts) + "\n"
