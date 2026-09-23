# Bundled remembering engine (opencode-remembering product code).
"""ProjectFrame loading and strict validation.

ProjectFrame is declared configuration, never inferred output. A
malformed frame disables framing (baseline memory keeps working);
a project-identity mismatch fails closed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .model import PROJECT_FRAME_SCHEMA, ProjectFrame, WorkTypeSpec

PROJECT_FRAME_REL = Path(".remembering") / "project-frame.json"

VALID_EVIDENCE = {
    "source_code", "test_result", "decision", "architecture",
    "release_note", "open_issue", "historical", "config", "prose",
}


class FrameError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def frame_digest(raw: dict) -> str:
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def validate_frame_dict(raw: dict) -> ProjectFrame:
    """Strict validation. Unknown schema versions and bad shapes raise;
    nothing is silently repaired or defaulted into meaning."""
    if not isinstance(raw, dict):
        raise FrameError("FRAME_INVALID", "project frame must be an object")
    if raw.get("schema_version", PROJECT_FRAME_SCHEMA) != PROJECT_FRAME_SCHEMA:
        raise FrameError(
            "FRAME_INVALID",
            f"unsupported project-frame schema "
            f"{raw.get('schema_version')!r}; expected {PROJECT_FRAME_SCHEMA!r}")
    project_id = raw.get("project_id")
    version = raw.get("version")
    if not isinstance(project_id, str) or not project_id.strip():
        raise FrameError("FRAME_INVALID", "project_id must be set")
    if not isinstance(version, str) or not version.strip():
        raise FrameError("FRAME_INVALID", "version must be set")

    def str_list(value: Any, field: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value):
            raise FrameError("FRAME_INVALID",
                             f"{field} must be a list of strings")
        return tuple(value)

    work_types: list[WorkTypeSpec] = []
    raw_types = raw.get("work_types", [])
    if not isinstance(raw_types, list):
        raise FrameError("FRAME_INVALID", "work_types must be a list")
    for entry in raw_types:
        if not isinstance(entry, dict) or not isinstance(
                entry.get("name"), str) or not entry["name"].strip():
            raise FrameError("FRAME_INVALID",
                             "each work_types entry needs a name")
        terms = entry.get("match_terms", [])
        if not isinstance(terms, list) or not all(
                isinstance(t, str) and t.strip() for t in terms):
            raise FrameError("FRAME_INVALID",
                             f"match_terms for {entry['name']!r} must be "
                             "non-empty strings")
        work_types.append(WorkTypeSpec(
            name=entry["name"].strip(),
            match_terms=tuple(t.strip() for t in terms)))

    prefs: list[tuple[str, tuple[str, ...]]] = []
    raw_prefs = raw.get("evidence_preferences", {})
    if not isinstance(raw_prefs, dict):
        raise FrameError("FRAME_INVALID",
                         "evidence_preferences must be an object")
    for name, classes in raw_prefs.items():
        if not isinstance(classes, list) or not all(
                isinstance(c, str) for c in classes):
            raise FrameError("FRAME_INVALID",
                             f"evidence_preferences[{name!r}] must be a "
                             "list of strings")
        unknown = [c for c in classes if c not in VALID_EVIDENCE]
        if unknown:
            raise FrameError("FRAME_INVALID",
                             f"unknown evidence classes {unknown} "
                             f"for work type {name!r}")
        prefs.append((name, tuple(classes)))

    hard_exclude = raw.get("hard_exclude", False)
    if not isinstance(hard_exclude, bool):
        raise FrameError("FRAME_INVALID", "hard_exclude must be a boolean")

    return ProjectFrame(
        project_id=project_id.strip(),
        version=version.strip(),
        purpose=raw.get("purpose", "") if isinstance(
            raw.get("purpose", ""), str) else "",
        objectives=str_list(raw.get("objectives"), "objectives"),
        constraints=str_list(raw.get("constraints"), "constraints"),
        include_projects=str_list(raw.get("include_projects"),
                                  "include_projects"),
        work_types=tuple(work_types),
        evidence_preferences=tuple(prefs),
        hard_exclude=hard_exclude,
    )


def load_project_frame(project_dir: Path, schema: str) -> dict:
    """Load result: present/valid/frame/digest, or a disabling error.

    Returns {"present": bool, "valid": bool, "frame": ProjectFrame|None,
    "digest": str|None, "error": str|None}. Identity mismatch raises
    FrameError (fail closed); malformed content disables framing.
    """
    path = Path(project_dir) / PROJECT_FRAME_REL
    if not path.is_file():
        return {"present": False, "valid": False, "frame": None,
                "digest": None, "error": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"present": True, "valid": False, "frame": None,
                "digest": None,
                "error": f"FRAME_INVALID:unreadable frame file: {exc}"}
    try:
        frame = validate_frame_dict(raw)
    except FrameError as exc:
        return {"present": True, "valid": False, "frame": None,
                "digest": None, "error": f"{exc.code}:{exc.message}"}
    if frame.project_id != schema:
        raise FrameError(
            "FRAME_PROJECT_MISMATCH",
            f"project frame claims {frame.project_id!r} but this "
            f"project resolves to {schema!r}; refusing another "
            "project's frame.")
    return {"present": True, "valid": True, "frame": frame,
            "digest": frame_digest(raw), "error": None}
