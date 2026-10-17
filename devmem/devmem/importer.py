"""Split markdown into importable sections.

Sections are synthetic inputs for the memory store. The hash covers the
resolved source path and the stored text, so importing the same file again
does not create another row.
"""

from __future__ import annotations

import glob
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from devmem.secrets_filter import reject_secrets

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class MarkdownSection:
    heading: str
    content: str


@dataclass(frozen=True)
class ImportSection:
    source_path: str
    heading: str
    content: str
    content_hash: str


def split_markdown(text: str) -> list[MarkdownSection]:
    """Split a document on ATX headings.

    Text before the first heading becomes its own section. A document with no
    headings is one section. Empty results are dropped.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_HEADING_RE.finditer(normalized))
    if not matches:
        body = normalized.strip()
        if not body:
            return []
        return [MarkdownSection(heading="", content=body)]

    sections: list[MarkdownSection] = []
    preamble = normalized[: matches[0].start()].strip()
    if preamble:
        sections.append(MarkdownSection(heading="", content=preamble))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        heading = match.group(2).strip()
        body = normalized[start:end].strip()
        content = f"{match.group(1)} {heading}"
        if body:
            content = f"{content}\n\n{body}"
        if content.strip():
            sections.append(MarkdownSection(heading=heading, content=content))
    return sections


def content_hash(source_path: str, content: str) -> str:
    payload = f"{source_path}\n{content}".encode()
    return hashlib.sha256(payload).hexdigest()


def collect_files(root: Path, path: str | None, pattern: str | None) -> list[Path]:
    """Resolve `--path` or `--glob` against the project root."""
    if (path is None) == (pattern is None):
        raise ValueError("pass exactly one of --path or --glob")
    if path is not None:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise ValueError(f"import path is not a file: {candidate}")
        return [candidate]
    assert pattern is not None
    if Path(pattern).is_absolute():
        matches = [Path(item) for item in glob.glob(pattern, recursive=True)]
    else:
        matches = [Path(item) for item in glob.glob(str(root / pattern), recursive=True)]
    files = sorted(item.resolve() for item in matches if item.is_file())
    if not files:
        raise ValueError(f"glob matched no files: {pattern}")
    return files


def prepare_file(path: Path) -> list[ImportSection]:
    """Read one markdown file and reject it before any database write if it has a secret."""
    text = path.read_text(encoding="utf-8")
    source_path = str(path.resolve())
    prepared: list[ImportSection] = []
    for section in split_markdown(text):
        reject_secrets(section.content)
        prepared.append(
            ImportSection(
                source_path=source_path,
                heading=section.heading,
                content=section.content,
                content_hash=content_hash(source_path, section.content),
            )
        )
    return prepared
