# Bundled remembering engine (opencode-remembering product code).
"""Evidence classification and WorkSignal construction.

Evidence classes are derived from baseline ingestion metadata
(artifact type + filename), never from LLM judgment. The vocabulary
is intentionally small: preferences, not a document ontology.
"""

from __future__ import annotations

import re

from .model import WorkSignal

_TEST_RE = re.compile(r"(^|[_\-.])test([_\-.]|$)|spec|_test\.|^test_")


def evidence_class(source_id: str, artifact_type: str | None) -> str:
    """Classify one source into the preference vocabulary."""
    name = (source_id or "").lower()
    base = name.rsplit("/", 1)[-1]
    kind = (artifact_type or "").lower()
    if kind == "code" or base.endswith(
            (".py", ".js", ".ts", ".go", ".rs", ".java", ".sql")):
        if _TEST_RE.search(base):
            return "test_result"
        return "source_code"
    if kind == "decision-record" or base.startswith("adr-") \
            or "decision" in base:
        return "decision"
    if kind == "commit-log" or kind == "session" \
            or base.endswith(".log"):
        return "historical"
    if kind == "issue":
        return "open_issue"
    if kind == "config":
        return "config"
    if "release" in base or "checklist" in base:
        return "release_note"
    if "architect" in base or "design" in base or "benchmark" in base:
        return "architecture"
    return "prose"


def make_signal(signal_id: str, kind: str, text: str,
                observed_at: str) -> WorkSignal:
    from .model import SIGNAL_KINDS

    if kind not in SIGNAL_KINDS:
        raise ValueError(f"FRAME_INVALID:unknown signal kind {kind!r}")
    if not text or not text.strip():
        raise ValueError("FRAME_INVALID:signal text must not be empty")
    return WorkSignal(signal_id=signal_id, kind=kind,
                      text=text.strip()[:2000], observed_at=observed_at)
