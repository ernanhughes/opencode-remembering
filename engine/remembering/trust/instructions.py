# Bundled remembering engine (opencode-remembering product code).
"""Deterministic instruction screen: narrow bypass/skip/ignore-class
patterns over remembered text. Standing rules worded as requirements
("require a passing rollback test") must not trip it."""

from __future__ import annotations

import re

from .model import INSTRUCTION_SCREEN_VERSION

INSTRUCTION_PATTERNS = (
    r"skip (\w+ ){0,3}(validation|verification|checks|testing|review)",
    r"ignore (\w+ ){0,3}(normal|the|all|any|migration checks)",
    r"bypass",
    r"disable (\w+ ){0,3}(validation|verification|checks|foreign-key)",
    r"never verify",
    r"do not (\w+ ){0,3}(verify|check|test|validate)",
    r"without (\w+ ){0,3}(validation|verification|review|approval)",
    r"regardless of (\w+ ){0,3}(existing|prior|current)",
    r"always skip",
)
_INSTRUCTION_RE = re.compile("|".join(INSTRUCTION_PATTERNS),
                             re.IGNORECASE)

# A requirement for verification nearby cancels the bypass reading:
# "require a passing rollback test" is a guard, not an evasion.
_GUARD_RE = re.compile(
    r"(require|requires|required|must|should|ensure)[^.]{0,60}"
    r"(test|verif|valid|check|review|approv)", re.IGNORECASE)


def screen_instruction(text: str) -> dict:
    """Return {directive_like, matched, guarded}. Deterministic."""
    match = _INSTRUCTION_RE.search(text or "")
    if match is None:
        return {"directive_like": False, "matched": None, "guarded": False,
                "screen_version": INSTRUCTION_SCREEN_VERSION}
    guarded = _GUARD_RE.search(text or "") is not None
    return {"directive_like": not guarded,
            "matched": match.group(0).strip().lower() if not guarded else None,
            "guarded": guarded,
            "screen_version": INSTRUCTION_SCREEN_VERSION}
