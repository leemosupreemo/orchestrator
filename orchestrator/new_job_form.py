"""What each kind of job asks for, and how the answers become the planner's input.

The terminal flow asks different questions per job type (a bug: summary, repro
steps, expected behaviour; a design: vision and a look and feel; a test job: the
area and subsystems; a quick change: one instruction). The web form asks the same
and uses this module to turn the answers into the same text the planner reads in
the terminal, plus any attached logs and reference files.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

VIBES = {
    "minimalist": "clean, focused, white space",
    "glassmorphism": "frosted glass, depth, vibrant colours",
    "brutalist": "bold, raw, high contrast",
    "high-energy": "animations, playful, dynamic",
    "gothic-noir": "dark, moody, elegant",
}
DEFAULT_VIBE = "minimalist"
# Where the planner has real choices to make, so it should pick sensible defaults and say what it assumed.
LATITUDE_TYPES = ("feature", "design")
LATITUDE_NOTE = (
    "\n\n## Anything left open\n"
    "Where the request doesn't say (approach, UX details, naming, libraries), pick the most conventional option "
    "for this codebase and record each choice in `assumptions` with a one-line reason so it can be reviewed. "
    "Only stop to ask when a wrong guess would be costly to undo or the goal itself is unclear."
)

UPLOAD_LIMIT = 25 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
LOG_EXTS = {".log", ".txt", ".json", ".md", ".crash", ".ips"}
DESIGN_EXTS = {".pdf", ".fig", ".html", ".htm"}
UPLOAD_EXTS = IMAGE_EXTS | LOG_EXTS | DESIGN_EXTS
LOG_EXCERPT_CHARS = 8000
MAX_LOG_BLOCKS = 3


class FormError(ValueError):
    pass


def upload_kind(name: str) -> str:
    """image | log | design, or raises FormError for a file type we don't take."""
    ext = PurePosixPath(name.lower()).suffix
    if ext in IMAGE_EXTS:
        return "image"
    if ext in LOG_EXTS:
        return "log"
    if ext in DESIGN_EXTS:
        return "design"
    raise FormError("That file type isn't supported. Use an image, PDF, Figma (.fig) or HTML file, or a text log.")


def reference_type(path: str) -> str:
    ext = PurePosixPath(path.lower()).suffix
    if ext in IMAGE_EXTS:
        return "image_reference"
    if ext in (".html", ".htm"):
        return "html_mockup"
    if ext == ".pdf":
        return "pdf_reference"
    if ext in (".md", ".txt"):
        return "text_reference"
    return "file_reference"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def compose(job_type: str, f: dict[str, Any]) -> tuple[str, str]:
    """(the CLI's --summary, the spec text). For a quick change the summary is the whole instruction."""
    summary = _clean(f.get("summary"))
    if not summary:
        raise FormError("Say what this is about.")
    details = _clean(f.get("details"))
    if job_type == "quick":
        return summary, ""
    short = summary.splitlines()[0][:500]
    if job_type == "bug":
        spec = f"SUMMARY: {summary}\n\nREPRO STEPS:\n{_clean(f.get('repro'))}\n\nEXPECTED BEHAVIOR:\n{_clean(f.get('expected'))}"
    elif job_type == "coverage":
        spec = f"COVERAGE FOCUS: {summary}\n\nSUBSYSTEMS: {_clean(f.get('subsystems')) or summary}"
        test_cases = _clean(f.get("test_cases"))
        if test_cases:
            spec += f"\n\nSPECIFIC TEST CASES:\n{test_cases}"
    elif job_type == "design":
        vibe = _clean(f.get("vibe")) or DEFAULT_VIBE
        spec = f"DESIGN VISION: {summary}\nPREFERRED VIBE: {vibe}"
    else:
        spec = f"VISION: {summary}"
    if details:
        spec += f"\n\nADDITIONAL DETAILS:\n{details}"
    return short, spec


def attachments_block(logs: list[tuple[str, str]], files: list[tuple[str, str, str]]) -> str:
    """Markdown for the planner. logs: (name, text); files: (name, kind, path)."""
    parts = []
    for name, text in logs[:MAX_LOG_BLOCKS]:
        excerpt = text[-LOG_EXCERPT_CHARS:]
        cut = "(showing the end of the log)\n" if len(text) > LOG_EXCERPT_CHARS else ""
        parts.append(f"### Log: {name}\n{cut}```\n{excerpt}\n```")
    if len(logs) > MAX_LOG_BLOCKS:
        parts.append(f"(+{len(logs) - MAX_LOG_BLOCKS} more logs are attached to the job)")
    if files:
        lines = ["### Reference files (open them for the real picture)"]
        lines += [f"- {name} ({kind}): `{path}`" for name, kind, path in files]
        parts.append("\n".join(lines))
    return ("\n\n## Attachments\n\n" + "\n\n".join(parts)) if parts else ""
