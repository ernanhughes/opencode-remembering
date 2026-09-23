# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Ingestion: discovery, parsing, chunking.

Every chunk is traceable to source, span, content hash, and the exact
chunker version that produced it. Ordinary metadata only — no memory
semantics are attached at any stage.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ChunkingConfig

TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".js", ".ts", ".go", ".rs", ".java", ".sql",
    ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".log",
}
# Build outputs and other generated trees are never project memory.
# This set stays narrow on purpose: ordinary source, docs, configs and
# logs are ingested; regenerable artifacts are not.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "coverage", "target", ".next", ".turbo",
}


@dataclass(frozen=True)
class Source:
    source_id: str  # path relative to corpus root, posix style
    artifact_type: str
    content: str
    content_hash: str
    timestamp: str | None = None


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    source_id: str
    ordinal: int
    text: str
    char_start: int
    char_end: int
    content_hash: str
    section: str | None = None


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def artifact_type(path: Path) -> str:
    name = path.name.lower()
    if name.startswith("adr-") or "decision" in name:
        return "decision-record"
    if name.startswith("session-"):
        return "session"
    if name.startswith("issue-"):
        return "issue"
    if name in ("commits.log",) or path.suffix == ".log":
        return "commit-log"
    if path.suffix in (".json",):
        return "record"
    if path.suffix in (".py", ".js", ".ts", ".go", ".rs", ".java", ".sql"):
        return "code"
    if path.suffix in (".yaml", ".yml", ".toml", ".cfg", ".ini"):
        return "config"
    return "document"


def discover(root: Path) -> list[Path]:
    """All ingestible files under root, sorted for determinism."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            found.append(path)
    return found


_DATE_RE = re.compile(r"(?m)^(?:date|effective)[:\s]+(\d{4}-\d{2}-\d{2})")


def parse(path: Path, root: Path) -> Source | None:
    """Read one file. Returns None when content is empty or undecodable."""
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    if not content.strip():
        return None
    source_id = path.relative_to(root).as_posix()
    match = _DATE_RE.search(content[:500])
    return Source(
        source_id=source_id,
        artifact_type=artifact_type(path),
        content=content,
        content_hash=sha1(content),
        timestamp=match.group(1) if match else None,
    )


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9#\-*`])")
_HEADING_RE = re.compile(r"(?m)^(#{1,4}\s+.+)$")


def _split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def chunk_fixed(text: str, target: int, overlap: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    size = len(text)
    while start < size:
        end = min(start + target, size)
        spans.append((start, end))
        if end == size:
            break
        start = max(end - overlap, start + 1)
    return spans


def chunk_section(text: str) -> list[tuple[int, int, str | None]]:
    """Split on markdown headings; untitled lead keeps section None."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(0, len(text), None)]
    spans: list[tuple[int, int, str | None]] = []
    if matches[0].start() > 0:
        spans.append((0, matches[0].start(), None))
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        spans.append((start, end, match.group(1).strip()[:120]))
    return spans


def chunk_sentence_aware(
    text: str, target: int, overlap: int
) -> list[tuple[int, int]]:
    """Greedy sentence packing to ~target chars with sentence overlap."""
    sentences = _split_sentences(text)
    if not sentences:
        return []
    # Map sentences back to offsets for provenance.
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for sentence in sentences:
        at = text.find(sentence, cursor)
        if at < 0:
            at = cursor
        offsets.append((at, at + len(sentence)))
        cursor = at + len(sentence)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(sentences):
        start = offsets[i][0]
        end = offsets[i][1]
        j = i
        while j + 1 < len(sentences) and offsets[j + 1][1] - start < target:
            j += 1
            end = offsets[j][1]
        spans.append((start, end))
        if j + 1 >= len(sentences):
            break
        # Overlap: step back while the tail covers roughly `overlap` chars.
        k = j
        while k > i and end - offsets[k][0] < overlap:
            k -= 1
        i = max(k, i + 1) if k < j else j + 1
        if i <= j and k == j:
            i = j + 1
    return spans


def chunk_source(source: Source, cfg: ChunkingConfig) -> list[Chunk]:
    if cfg.policy == "section":
        raw = [
            (s, e, section) for s, e, section in chunk_section(source.content)
        ]
        spans = [(s, e) for s, e, _ in raw]
        sections = [section for _, _, section in raw]
    elif cfg.policy == "sentence":
        spans = chunk_sentence_aware(
            source.content, cfg.target_chars, cfg.overlap_chars
        )
        sections = [None] * len(spans)
    else:
        spans = chunk_fixed(
            source.content, cfg.target_chars, cfg.overlap_chars
        )
        sections = [None] * len(spans)
    chunks: list[Chunk] = []
    for ordinal, ((start, end), section) in enumerate(zip(spans, sections)):
        text = source.content[start:end].strip()
        if not text:
            continue
        chunk_id = sha1(
            f"{source.source_id}|{cfg.policy}|{cfg.chunker_version}|"
            f"{ordinal}|{sha1(text)}"
        )[:16]
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                source_id=source.source_id,
                ordinal=ordinal,
                text=text,
                char_start=start,
                char_end=end,
                content_hash=sha1(text),
                section=section,
            )
        )
    return chunks
