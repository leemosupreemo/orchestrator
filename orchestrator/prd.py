"""The product requirements doc: one short, living document that tells every AI what the product is.

`docs/product/prd.md` has five sections, in the person's own words wherever possible:

  Pitch            what it is, as if explained to a friend (and what it is built for)
  Who it's for     an ideal user, or the situations it is for; fine to not know yet
  Core features    the things it must do, as user stories if they like
  Look and feel    what it should feel like: a description, designs, sketches, Figma items
  Not this         what it should not be or include; optional

Everything that reads or judges work is handed this document (`context_block`). It is kept true: when a job finishes
(`Prd.run_update`) a model checks whether what the job learned changes what the document says, and edits it only if
so, keeping the person's words. Every change, theirs or the model's, is a version in a history that can be restored,
and a switch turns automatic updates off. Nothing here talks to a model itself; callers pass the model in.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import time
import zipfile
from html import unescape
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

PATH = "docs/product/prd.md"
DESIGNS_DIR = "docs/product/designs"
MAX_DOC_CHARS = 100_000
MAX_VERSIONS = 60
MAX_IMPORT_CHARS = 40_000


class PrdError(ValueError):
    pass


SECTIONS: list[dict[str, Any]] = [
    {"id": "pitch", "title": "Pitch", "optional": False,
     "hint": "Say what you have in mind as briefly as you can, the way you'd explain it to a friend. Rough is fine."},
    {"id": "who", "title": "Who it's for", "optional": False,
     "hint": "Do you have an ideal user in mind, or some situations it's for? If you don't know yet, say so; we'll fill in the blanks and refine as we go."},
    {"id": "features", "title": "Core features", "optional": False,
     "hint": "The things it has to do. One per line. If it helps, write each as a story: \"As a ..., I can ... so that ...\". Optional."},
    {"id": "look", "title": "Look and feel", "optional": True,
     "hint": "What should it look and feel like? Describe it (\"feels like Google Docs\"), or add designs, sketches or Figma items. Optional."},
    {"id": "not", "title": "Not this", "optional": True,
     "hint": "Anything it should not be or include: features you don't want, competitors not to copy exactly. Optional."},
]
BY_ID = {s["id"]: s for s in SECTIONS}
HEADING = {s["id"]: f"## {s['title']}" for s in SECTIONS}


def _guidance(section: dict[str, Any]) -> str:
    return f"_{section['hint']}_"


def template(name: str = "") -> str:
    out = [f"# {name + ': ' if name else ''}product requirements", "",
           "_A short, living description of this product. Every AI working on it reads this first, and it is kept up to date as jobs teach us more._", ""]
    for s in SECTIONS:
        out += [HEADING[s["id"]], "", _guidance(s), ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- parsing


def split(text: str) -> dict[str, str]:
    """Section id -> body (guidance lines removed), for the five known sections."""
    found: dict[str, list[str]] = {}
    current: str | None = None
    by_heading = {s["title"].lower(): s["id"] for s in SECTIONS}
    for line in (text or "").splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            current = by_heading.get(m.group(1).lower())
            if current:
                found.setdefault(current, [])
            continue
        if current is not None:
            found[current].append(line)
    out = {}
    for sid, lines in found.items():
        kept = [l for l in lines if not _GUIDANCE_LINE.fullmatch(l)]
        out[sid] = "\n".join(kept).strip()
    return out


_GUIDANCE_LINE = re.compile(r"\s*_[^_].*_\s*")


def is_filled(body: str | None) -> bool:
    """Real content: not empty, not just guidance text, not just "TBD"."""
    return any(l.strip() and not _GUIDANCE_LINE.fullmatch(l) and not re.match(r"\s*(?:[-*]\s*)?TBD\b", l) for l in (body or "").splitlines())


def sections_view(text: str | None) -> list[dict[str, Any]]:
    parts = split(text or "")
    return [{"id": s["id"], "title": s["title"], "hint": s["hint"], "optional": s["optional"], "body": parts.get(s["id"], ""),
             "filled": is_filled(parts.get(s["id"]))} for s in SECTIONS]


def replace_section(text: str, section_id: str, body: str) -> str:
    """The document with one section's body replaced; everything else (including headings the person added) is kept."""
    section = BY_ID[section_id]
    body = body.strip() or _guidance(section)
    pattern = re.compile(rf"(^##\s+{re.escape(section['title'])}\s*\n)(.*?)(?=^##\s|\Z)", re.M | re.S | re.I)
    if pattern.search(text):
        return pattern.sub(lambda m: f"{m.group(1)}\n{body}\n\n", text, count=1).rstrip() + "\n"
    return text.rstrip() + f"\n\n{HEADING[section_id]}\n\n{body}\n"


def normalise(text: str) -> str:
    """The text with any of the five sections that went missing put back, so the structure always holds."""
    present = split(text).keys()
    for s in SECTIONS:
        if s["id"] not in present:
            text = text.rstrip() + f"\n\n{HEADING[s['id']]}\n\n{_guidance(s)}\n"
    return text.rstrip() + "\n"


def platforms(text: str | None) -> tuple[list[str], bool]:
    """(platforms the pitch says it is built for, whether that is still to be decided)."""
    m = re.search(r"^Built for:\s*(.+)$", text or "", re.M | re.I)
    if not m:
        return [], False
    value = m.group(1).strip()
    if re.search(r"not decided|recommend|undecided|not sure", value, re.I):
        return [], True
    return [p.strip() for p in re.split(r"[,;]", value) if p.strip()], False


# --------------------------------------------------------------------------- what the AIs are told

# Who is reading and how much room they get. Builders and debuggers work on one task, so they get less and a rule about when to stop and ask.
AUDIENCES: dict[str, dict[str, Any]] = {
    "planner": {"limits": {"pitch": 1500, "who": 1500, "features": 3000, "look": 1500, "not": 1500},
                "rule": "This document defines the product. Serve the people and features it describes, stay inside \"Not this\", and do not contradict it "
                        "without saying so in the plan's assumptions or risks."},
    "builder": {"limits": {"pitch": 600, "who": 600, "features": 1200, "look": 1000, "not": 1000},
                "rule": "This document defines the product. Build only what this task asks. Match the look and feel it describes and stay inside \"Not this\". "
                        "If the task cannot be done without contradicting it, use `clarification_needed` instead of guessing."},
    "reviewer": {"limits": {"pitch": 800, "who": 800, "features": 1500, "look": 1000, "not": 1000},
                 "rule": "Judge the change against this document too. Flag work that serves none of the features, builds something under \"Not this\", "
                         "or departs from the look and feel it describes, even if the code is correct."},
    "verifier": {"limits": {"pitch": 800, "who": 800, "features": 1500, "look": 1000, "not": 1000},
                 "rule": "Check the plan against this document too. Reject or flag a plan that builds something under \"Not this\", serves no listed feature, "
                         "or departs from the look and feel."},
}


def context_block(root: Path, audience: str = "planner") -> str:
    """The filled-in sections, trimmed, with the rule that fits who is reading them. Empty until something is written."""
    cfg = AUDIENCES.get(audience) or AUDIENCES["planner"]
    try:
        text = (Path(root) / PATH).read_text(encoding="utf-8")
    except OSError:
        return ""
    parts = split(text)
    blocks = []
    for s in SECTIONS:
        body = parts.get(s["id"], "")
        if not is_filled(body):
            continue
        limit = cfg["limits"][s["id"]]
        if len(body) > limit:
            body = body[:limit].rstrip() + "\n...(trimmed)"
        blocks.append(f"### {s['title']}\n{body}")
    if not blocks:
        return ""
    return f"\n\n## Product context (source of truth, `{PATH}`)\n{cfg['rule']}\n\n" + "\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------- the document, its history and settings


def _now() -> float:
    return time.time()


class Prd:
    """One project's document plus its history, settings and the latest automatic-update notice."""

    def __init__(self, root: Path, runtime: Path):
        self.root = Path(root)
        self.runtime = Path(runtime)

    # ---- files
    @property
    def path(self) -> Path:
        return self.root / PATH

    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _state_file(self) -> Path:
        return self.runtime / "prd.json"

    def _history_file(self) -> Path:
        return self.runtime / "prd-history.json"

    def _json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _save_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ---- settings and notice
    def state(self) -> dict[str, Any]:
        s = self._json(self._state_file(), {})
        return s if isinstance(s, dict) else {}

    def auto_update(self) -> bool:
        return self.state().get("auto_update", True) is not False  # on unless switched off

    def set_auto_update(self, on: bool) -> None:
        s = self.state()
        s["auto_update"] = bool(on)
        self._save_json(self._state_file(), s)

    def notice(self) -> dict[str, Any] | None:
        n = self.state().get("notice")
        return n if isinstance(n, dict) and not n.get("seen") else None

    def dismiss_notice(self) -> None:
        s = self.state()
        if isinstance(s.get("notice"), dict):
            s["notice"]["seen"] = True
            self._save_json(self._state_file(), s)

    def _set_notice(self, summary: str, job_id: str, title: str, version_id: str) -> None:
        s = self.state()
        s["notice"] = {"id": version_id, "at": _now(), "summary": summary, "job": job_id, "job_title": title, "seen": False}
        self._save_json(self._state_file(), s)

    # ---- history
    def _versions(self) -> list[dict[str, Any]]:
        data = self._json(self._history_file(), {})
        v = data.get("versions") if isinstance(data, dict) else None
        return [x for x in v if isinstance(x, dict) and "content" in x] if isinstance(v, list) else []

    def history(self) -> list[dict[str, Any]]:
        """Newest first, without the full text."""
        return [{k: v for k, v in x.items() if k != "content"} for x in reversed(self._versions())]

    def version(self, version_id: str) -> dict[str, Any]:
        for x in self._versions():
            if x.get("id") == version_id:
                return x
        raise PrdError("That version isn't in the history any more.")

    def _record(self, content: str, source: str, summary: str, job: str = "") -> dict[str, Any]:
        versions = self._versions()
        entry = {"id": f"{int(_now() * 1000)}-{len(versions)}", "at": _now(), "source": source, "summary": summary[:300], "job": job, "content": content}
        versions.append(entry)
        self._save_json(self._history_file(), {"versions": versions[-MAX_VERSIONS:]})
        return entry

    def write(self, text: str, source: str = "you", summary: str = "", job: str = "", record: bool = True) -> dict[str, Any] | None:
        """Saves the document. Returns the new version, or None when nothing changed."""
        if len(text) > MAX_DOC_CHARS:
            raise PrdError("That document is too long.")
        text = normalise(text) if text.strip() else template()
        before = self.read()
        if before is not None and before == text:
            return None
        if record and before is not None and not self._versions():
            self._record(before, "earlier", "The document before changes were tracked")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")
        return self._record(text, source, summary, job) if record else None

    def set_section(self, section_id: str, body: str, source: str = "you", summary: str = "") -> dict[str, Any] | None:
        if section_id not in BY_ID:
            raise PrdError("Unknown section.")
        text = self.read() or template()
        return self.write(replace_section(text, section_id, body), source, summary or f"Edited {BY_ID[section_id]['title']}")

    def revert(self, version_id: str) -> dict[str, Any] | None:
        v = self.version(version_id)
        when = time.strftime("%b %d, %H:%M", time.localtime(v["at"]))
        return self.write(v["content"], "revert", f"Restored the version from {when}")

    def add_reference(self, label: str, target: str) -> None:
        """Adds a design, sketch or link as a bullet under Look and feel."""
        label = re.sub(r"[\[\]\n]", " ", label).strip()[:120] or "Reference"
        text = self.read() or template()
        body = split(text).get("look", "")
        line = f"- [{label}]({target})"
        if line in body:
            return
        self.write(replace_section(text, "look", (body + "\n" if body else "") + line), "you", f"Added {label} to Look and feel")

    def sections(self) -> list[dict[str, Any]]:
        return sections_view(self.read())

    def overview(self) -> dict[str, Any]:
        text = self.read()
        return {"exists": text is not None, "text": text if text is not None else template(), "sections": sections_view(text),
                "auto_update": self.auto_update(), "notice": self.notice(), "path": PATH,
                "history": self.history()}

    # ---- an old project's documents become the PRD (the old files are left where they are)
    def migrate_legacy(self) -> bool:
        if self.exists():
            return False
        def read(rel: str) -> str:
            try:
                return (self.root / rel).read_text(encoding="utf-8")
            except OSError:
                return ""
        brief = read("docs/product-brief.md")
        uses = read("docs/product/use-cases.md")
        if not brief.strip() and not uses.strip():
            return False
        pitch = _body_after_heading(brief, "What it is") or _strip_title(brief)
        platforms_line = _body_after_heading(brief, "Platforms")
        who = _body_after_heading(brief, "Who it's for")
        users = _body_after_heading(uses, "Primary user")
        cases = _body_after_heading(uses, "Core use cases")
        feats = _body_after_heading(brief, "Version 1 must do")
        nots = _body_after_heading(uses, "Non-goals for version 1") or _body_after_heading(brief, "Out of scope for version 1")
        problem = _body_after_heading(brief, "The problem")
        text = template()
        if is_filled(pitch):
            text = replace_section(text, "pitch", "\n\n".join(x for x in (pitch, ("Problem: " + problem) if is_filled(problem) else "", ("Built for: " + _one_line(platforms_line)) if is_filled(platforms_line) else "") if x))
        who_text = "\n\n".join(x for x in (who, users, cases) if is_filled(x))
        if who_text:
            text = replace_section(text, "who", who_text)
        if is_filled(feats):
            text = replace_section(text, "features", feats)
        if is_filled(nots):
            text = replace_section(text, "not", nots)
        self.write(text, "migration", "Built from the earlier product documents")
        return True


def _body_after_heading(text: str, heading: str) -> str:
    m = re.search(rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s|\Z)", text or "", re.M | re.S | re.I)
    body = m.group(1) if m else ""
    return "\n".join(l for l in body.splitlines() if not re.fullmatch(r"\s*_[^_].*_\s*", l)).strip()


def _strip_title(text: str) -> str:
    return "\n".join(l for l in (text or "").splitlines() if not l.startswith("# ") and not re.fullmatch(r"\s*_[^_].*_\s*", l)).strip()


def _one_line(text: str) -> str:
    return ", ".join(re.sub(r"^\s*[-*]\s*", "", l).strip() for l in text.splitlines() if l.strip())


# --------------------------------------------------------------------------- asking a model


def _json_reply(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise PrdError("The model didn't answer in the expected form. Try again.")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise PrdError("The model didn't answer in the expected form. Try again.")
    if not isinstance(data, dict):
        raise PrdError("The model didn't answer in the expected form. Try again.")
    return data


SHAPE = ("Keep exactly these five sections, with these headings, in this order: " + "; ".join(f"\"## {s['title']}\"" for s in SECTIONS) +
         ". Keep the person's own words wherever you can: add and adjust, don't rewrite. Do not invent facts; where something is unknown write \"TBD:\" "
         "and the question that would settle it. Short lists, no marketing language.")


def questions_prompt(current: str, instruction: str = "") -> str:
    return f"""You are a product manager helping someone define what they are building. Find out what you need to know before anything is written.
{('What the person wants: ' + instruction) if instruction.strip() else ''}

Current document:
{current}

Ask at most 6 questions. Only ask what materially changes the product, and skip anything the document already answers. Prefer questions a person can answer in a sentence, in plain words (they may not be technical). For each, say why it matters.

Reply with ONLY JSON: {{"questions": [{{"question": "...", "why": "..."}}]}}"""


def refine_prompt(current: str, instruction: str, answers: list[dict[str, str]] | None = None) -> str:
    qa = "\n".join(f"- Q: {a.get('question', '')}\n  A: {a.get('answer', '')}" for a in (answers or []) if str(a.get("answer", "")).strip())
    return f"""You are a product manager keeping a product's one-page requirements document accurate and useful to the people and AIs who build from it.

Current document:
{current}

Request: {instruction.strip() or 'Fill in the empty sections from what the document already says and tighten the rest.'}
{('Answers from the person:' + chr(10) + qa) if qa else ''}

Rules:
- Return the COMPLETE updated document in Markdown. {SHAPE}
- Keep version 1 small. Anything not clearly needed belongs under "Not this".

Reply with ONLY JSON: {{"summary": "one or two sentences on what changed and why", "markdown": "the complete document"}}"""


def import_prompt(source: str, name: str = "") -> str:
    return f"""Someone already has a product requirements document{(' (' + name + ')') if name else ''}. Restructure it into this short format without losing what they wrote.

Their document:
{source[:MAX_IMPORT_CHARS]}

{SHAPE}
Put each part of their document under the closest section. Anything that doesn't fit (technical design, schedules) can be summarised in one line under "Core features" or left out; say what you left out in the summary.

Reply with ONLY JSON: {{"summary": "what you kept, moved and left out", "markdown": "the complete document"}}"""


def parse_questions(reply: str) -> list[dict[str, str]]:
    items = _json_reply(reply).get("questions")
    out = []
    for q in items if isinstance(items, list) else []:
        if isinstance(q, dict) and str(q.get("question", "")).strip():
            out.append({"question": str(q["question"]).strip()[:400], "why": str(q.get("why", "")).strip()[:400]})
    if not out:
        raise PrdError("The model had no questions. Go ahead and describe what you want.")
    return out[:6]


def parse_proposal(reply: str) -> dict[str, str]:
    data = _json_reply(reply)
    markdown = data.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise PrdError("The model returned no document. Try again.")
    if len(markdown) > MAX_DOC_CHARS:
        raise PrdError("The proposed document is too long. Ask for a shorter one.")
    return {"markdown": normalise(markdown), "summary": str(data.get("summary", "")).strip()[:500]}


def unified_diff(old: str, new: str, old_label: str = "current", new_label: str = "proposed") -> list[str]:
    return list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), old_label, new_label, lineterm="", n=2))


# --------------------------------------------------------------------------- reading an existing PRD


def extract_text(name: str, data: bytes) -> str:
    """Text from a markdown, text, Word or PDF file the person already has."""
    ext = Path(name or "").suffix.lower()
    if ext in {".md", ".markdown", ".txt", ".text", ""}:
        return data.decode("utf-8", errors="replace")
    if ext == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, KeyError):
            raise PrdError("That doesn't look like a Word document.")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        return unescape(re.sub(r"<[^>]+>", "", xml)).strip()
    if ext == ".pdf":
        exe = shutil.which("pdftotext")
        if not exe:
            raise PrdError("Reading PDFs needs the pdftotext tool (brew install poppler). Or paste the text, or save it as Word or markdown.")
        res = subprocess.run([exe, "-layout", "-", "-"], input=data, capture_output=True, timeout=30)
        if res.returncode != 0 or not res.stdout.strip():
            raise PrdError("Couldn't read any text from that PDF (is it a scan?). Paste the text instead.")
        return res.stdout.decode("utf-8", errors="replace")
    raise PrdError("Use a markdown, text, Word (.docx) or PDF file, or paste the text.")


# --------------------------------------------------------------------------- keeping it true as jobs finish

UPDATE_JOB_TYPES = {"feature-plan", "feature-design"}


def should_check(job: dict[str, Any]) -> bool:
    """Jobs that can teach us something about the product: features and designs, or anything the person answered questions on."""
    if job.get("prd_checked"):
        return False
    return job.get("type") in UPDATE_JOB_TYPES or bool(job.get("clarification_history"))


def job_digest(job: dict[str, Any]) -> str:
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    lines = [f"Job: {job.get('title') or job.get('job_id') or ''} ({job.get('type') or 'job'})"]
    if plan.get("summary"):
        lines.append(f"What it did: {plan['summary']}")
    if job.get("builder_summary"):
        lines.append(f"Builder's summary: {job['builder_summary']}")
    assumptions = [str(a) for a in plan.get("assumptions") or []][:8]
    if assumptions:
        lines.append("Assumptions it made:\n" + "\n".join(f"- {a}" for a in assumptions))
    criteria = [str(c) for t in plan.get("tasks") or [] if isinstance(t, dict) for c in t.get("acceptance_criteria") or []][:10]
    if criteria:
        lines.append("What it was built to do:\n" + "\n".join(f"- {c}" for c in criteria))
    answers = [f"- Q: {h.get('question', '')}\n  The person said: {h.get('answer', '')}" for h in job.get("clarification_history") or [] if isinstance(h, dict)][:6]
    if answers:
        lines.append("Questions the person answered (their decisions):\n" + "\n".join(answers))
    return "\n".join(lines)[:6000]


def update_prompt(current: str, digest: str) -> str:
    return f"""PRODUCT DOCUMENT UPDATE. A job just finished on this product. Decide whether what it shows changes what the product requirements document says.

Current document:
{current}

What the job showed:
{digest}

Rules:
- Change the document ONLY where the job clearly adds to it or contradicts it: a feature that now exists or was dropped, a decision the person made, a user or constraint that came up. If nothing needs to change, say so.
- The person's own words stay. Add or adjust a line; never rewrite their wording, and never remove anything under "Not this".
- {SHAPE}
- Do not record implementation detail (files, libraries). Only what the product is, who it is for, what it does, how it should feel, and what it is not.

Reply with ONLY JSON: {{"changed": true or false, "summary": "one sentence on what you changed and why (empty if unchanged)", "markdown": "the complete updated document (empty if unchanged)"}}"""


def parse_update(reply: str) -> dict[str, Any]:
    data = _json_reply(reply)
    changed = data.get("changed") is True
    markdown = data.get("markdown") if isinstance(data.get("markdown"), str) else ""
    return {"changed": bool(changed and markdown.strip()), "summary": str(data.get("summary", "")).strip()[:300], "markdown": markdown}


def unsafe_update(old: str, new: str) -> str | None:
    """Why an automatic edit must not be applied, or None. Edits may add and adjust, but not gut the document."""
    old_parts, new_parts = split(old), split(new)
    for s in SECTIONS:
        if is_filled(old_parts.get(s["id"])) and not is_filled(new_parts.get(s["id"])):
            return f"it emptied {s['title']}"
    for line in (l.strip() for l in old_parts.get("not", "").splitlines()):
        if line and line not in new_parts.get("not", ""):
            return "it removed something under Not this"
    if len(new) < 0.6 * len(old):
        return "it cut the document down too far"
    return None


def run_update(prd: Prd, job: dict[str, Any], llm: Callable[[str], str]) -> dict[str, Any]:
    """After a job finishes: ask whether it changes the document, and apply a safe edit. Never raises."""
    if not prd.auto_update():
        return {"status": "off"}
    if not should_check(job):
        return {"status": "skipped"}
    prd.migrate_legacy()
    current = prd.read()
    if current is None or not is_filled(split(current).get("pitch")):
        return {"status": "empty"}  # nothing written yet, so nothing to keep true
    try:
        update = parse_update(llm(update_prompt(current, job_digest(job))))
    except PrdError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # the model or its tool failed; the job must not
        return {"status": "error", "error": str(exc)[:200]}
    if not update["changed"]:
        return {"status": "nochange"}
    new = normalise(update["markdown"])
    why = unsafe_update(current, new)
    if why:
        return {"status": "rejected", "error": f"The suggested update was not applied: {why}.", "summary": update["summary"]}
    if new == current:
        return {"status": "nochange"}
    version = prd.write(new, "auto", update["summary"] or "Updated from a finished job", job=str(job.get("job_id") or ""))
    if version:
        prd._set_notice(update["summary"] or "Updated from a finished job", str(job.get("job_id") or ""), str(job.get("title") or ""), version["id"])
    return {"status": "updated", "summary": update["summary"]}
