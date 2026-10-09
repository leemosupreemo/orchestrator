from __future__ import annotations

import mimetypes
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from common import OUTPUT_DIR, ROOT

TEXT_EXTENSIONS = {".html", ".htm", ".md", ".txt", ".css", ".json", ".log", ".crash", ".ips"}  # logs attached to a job are read too
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {".pdf", ".fig"}
MAX_REFERENCE_PREVIEW_CHARS = 20_000


def is_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def safe_filename(name: str, fallback: str = "reference") -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return clean or fallback


def classify_reference(path_or_url: str) -> str:
    parsed_path = Path(urlparse(path_or_url).path if is_url(path_or_url) else path_or_url)
    ext = parsed_path.suffix.lower()
    if "figma.com" in urlparse(path_or_url).netloc:
        return "figma_url"
    if ext in {".html", ".htm"}:
        return "html_mockup"
    if ext in IMAGE_EXTENSIONS:
        return "image_reference"
    if ext == ".pdf":
        return "pdf_reference"
    if ext in {".md", ".txt"}:
        return "text_reference"
    return "url_reference" if is_url(path_or_url) else "file_reference"


def references_dir(job: dict) -> Path:
    job_id = job.get("job_id", "unknown")
    path = OUTPUT_DIR / job_id / "references"
    path.mkdir(parents=True, exist_ok=True)
    return path


def attach_reference_artifact(job: dict, source: str, note: str = "") -> dict:
    source = source.strip()
    if not source:
        raise ValueError("reference source is required")

    artifact_type = classify_reference(source)
    artifact: dict[str, str] = {
        "type": artifact_type,
        "note": note.strip(),
    }

    if is_url(source):
        artifact["url"] = source
        parsed = urlparse(source)
        name = safe_filename(Path(parsed.path).name or parsed.netloc, "reference-url")
        sidecar = references_dir(job) / f"{name}.url.txt"
        sidecar.write_text(f"{source}\n\n{note.strip()}\n", encoding="utf-8")
        artifact["path"] = str(sidecar.relative_to(ROOT))
        return artifact

    source_path = Path(source).expanduser()
    if not source_path.is_absolute():
        source_path = ROOT / source_path
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"reference file not found: {source}")

    ext = source_path.suffix.lower()
    if ext and ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported reference file type: {ext}")

    destination = references_dir(job) / safe_filename(source_path.name)
    if destination.exists():
        stem = destination.stem
        suffix = destination.suffix
        index = 2
        while destination.exists():
            destination = references_dir(job) / f"{stem}-{index}{suffix}"
            index += 1

    shutil.copy2(source_path, destination)
    artifact["path"] = str(destination.relative_to(ROOT))
    artifact["source"] = str(source_path)
    mime_type, _ = mimetypes.guess_type(destination)
    if mime_type:
        artifact["mime_type"] = mime_type
    return artifact


def reference_context(job: dict) -> str:
    artifacts = job.get("reference_artifacts", [])
    if not artifacts:
        return ""

    sections = ["\n## Reference Artifacts\n"]
    for index, artifact in enumerate(artifacts, start=1):
        artifact_type = artifact.get("type", "reference")
        path = artifact.get("path")
        url = artifact.get("url")
        note = artifact.get("note")
        sections.append(f"\n### Reference {index}: {artifact_type}\n")
        if path:
            sections.append(f"- Path: `{path}`\n")
        if url:
            sections.append(f"- URL: `{url}`\n")
        if note:
            sections.append(f"- Note: {note}\n")

        if path:
            full_path = ROOT / path if not Path(path).is_absolute() else Path(path)
            ext = full_path.suffix.lower()
            if full_path.exists() and ext in TEXT_EXTENSIONS:
                text = full_path.read_text(encoding="utf-8", errors="replace")
                if len(text) > MAX_REFERENCE_PREVIEW_CHARS:
                    text = text[:MAX_REFERENCE_PREVIEW_CHARS] + "\n...[reference truncated]...\n"
                sections.append("\n```text\n")
                sections.append(text)
                sections.append("\n```\n")
            elif full_path.exists():
                sections.append("- Preview: binary or non-text artifact; inspect the file directly.\n")

    return "".join(sections)
