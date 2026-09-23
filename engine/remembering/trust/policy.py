# Bundled remembering engine (opencode-remembering product code).
"""Trust policy: explicit source classes, roles, and deterministic
path-rule matching. Authority is declared configuration, never
inferred from similarity. Missing policy falls back to a documented
conservative builtin; malformed explicit policy fails visibly."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .model import TRUST_POLICY_SCHEMA, TRUST_POLICY_VERSION

TRUST_POLICY_REL = Path(".remembering") / "trust" / "policy.json"

BUILTIN_POLICY_VERSION = "builtin-default-v0.1"

# Roles that may direct action vs merely inform it.
DIRECTING_ROLES = ("decision", "production_state", "directive")
NON_GUIDING_ROLES = ("preference",)


class PolicyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SourceClass:
    name: str
    may_inform: bool
    may_direct: bool


@dataclass(frozen=True)
class SourceRule:
    id: str
    match: str  # path prefix or glob with ** and *
    source_class: str
    role: str
    restricted_to: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrustPolicy:
    version: str
    default_source_class: str
    source_classes: tuple[SourceClass, ...]
    source_rules: tuple[SourceRule, ...]
    schema_version: str = TRUST_POLICY_SCHEMA
    policy_version: str = TRUST_POLICY_VERSION
    policy_source: str = "explicit"
    digest: str = ""

    def class_of(self, name: str) -> SourceClass | None:
        for cls in self.source_classes:
            if cls.name == name:
                return cls
        return None


def builtin_policy() -> TrustPolicy:
    """Conservative default: ordinary local material informs but does
    not direct; detectable external material is untrusted; nothing is
    authoritative unless explicitly configured."""
    return TrustPolicy(
        version=BUILTIN_POLICY_VERSION,
        default_source_class="informational",
        source_classes=(
            SourceClass("authoritative", True, True),
            SourceClass("informational", True, False),
            SourceClass("untrusted", False, False),
        ),
        source_rules=(
            SourceRule("external", "external/**", "untrusted", "external"),
            SourceRule("vendor", "vendor/**", "untrusted", "external"),
            SourceRule("third-party", "third-party/**", "untrusted",
                       "external"),
        ),
        policy_source="builtin_default",
        digest="builtin",
    )


def _match_rule(pattern: str, source_id: str) -> tuple[bool, int]:
    """Deterministic glob: ** crosses separators, * does not.
    Returns (matched, specificity) for most-specific-wins."""
    import re

    text = source_id.strip()
    pat = pattern.strip()
    if pat == text:
        return True, 10_000 + len(pat)
    if pat.endswith("/**"):
        prefix = pat[:-3]
        if text == prefix or text.startswith(prefix + "/"):
            return True, 1000 + len(prefix)
        return False, 0
    regex = ""
    i = 0
    while i < len(pat):
        if pat[i:i + 2] == "**":
            regex += ".*"
            i += 2
        elif pat[i] == "*":
            regex += "[^/]*"
            i += 1
        else:
            regex += re.escape(pat[i])
            i += 1
    if re.fullmatch(regex, text):
        return True, len(pat)
    return False, 0


def match_rule(policy: TrustPolicy,
               source_id: str) -> tuple[SourceRule | None, str]:
    """Most-specific matching rule wins; ties break by file order
    (first wins, deterministic). Returns (rule|None, matched_rule_id)."""
    best: SourceRule | None = None
    best_spec = -1
    for rule in policy.source_rules:
        matched, spec = _match_rule(rule.match, source_id)
        if matched and spec > best_spec:
            best, best_spec = rule, spec
    return best, (best.id if best is not None else "")


def policy_digest(raw: dict) -> str:
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def validate_policy_dict(raw: dict) -> TrustPolicy:
    if not isinstance(raw, dict):
        raise PolicyError("TRUST_POLICY_INVALID",
                          "trust policy must be an object")
    if raw.get("schema_version", TRUST_POLICY_SCHEMA) != TRUST_POLICY_SCHEMA:
        raise PolicyError(
            "TRUST_POLICY_INVALID",
            f"unsupported schema {raw.get('schema_version')!r}; "
            f"expected {TRUST_POLICY_SCHEMA!r}")
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PolicyError("TRUST_POLICY_INVALID", "version must be set")

    classes: list[SourceClass] = []
    raw_classes = raw.get("source_classes", {})
    if not isinstance(raw_classes, dict):
        raise PolicyError("TRUST_POLICY_INVALID",
                          "source_classes must be an object")
    for name, spec in raw_classes.items():
        if not isinstance(spec, dict) or not isinstance(
                spec.get("may_inform"), bool) or not isinstance(
                spec.get("may_direct"), bool):
            raise PolicyError(
                "TRUST_POLICY_INVALID",
                f"source_classes[{name!r}] needs may_inform/may_direct "
                "booleans")
        classes.append(SourceClass(name, spec["may_inform"],
                                   spec["may_direct"]))

    default = raw.get("default_source_class", "informational")
    if not isinstance(default, str) or not any(
            c.name == default for c in classes):
        raise PolicyError("TRUST_POLICY_INVALID",
                          "default_source_class must name a defined class")

    rules: list[SourceRule] = []
    raw_rules = raw.get("source_rules", [])
    if not isinstance(raw_rules, list):
        raise PolicyError("TRUST_POLICY_INVALID",
                          "source_rules must be a list")
    seen: set[str] = set()
    for entry in raw_rules:
        if not isinstance(entry, dict) or not isinstance(
                entry.get("id"), str) or not entry["id"].strip():
            raise PolicyError("TRUST_POLICY_INVALID",
                              "each source rule needs an id")
        if entry["id"] in seen:
            raise PolicyError("TRUST_POLICY_INVALID",
                              f"duplicate rule id {entry['id']!r}")
        seen.add(entry["id"])
        for req in ("match", "source_class", "role"):
            if not isinstance(entry.get(req), str) or \
                    not entry[req].strip():
                raise PolicyError(
                    "TRUST_POLICY_INVALID",
                    f"rule {entry['id']!r} needs {req}")
        if not any(c.name == entry["source_class"] for c in classes):
            raise PolicyError(
                "TRUST_POLICY_INVALID",
                f"rule {entry['id']!r} names unknown class "
                f"{entry['source_class']!r}")
        restricted = entry.get("restricted_to", [])
        if not isinstance(restricted, list) or not all(
                isinstance(r, str) for r in restricted):
            raise PolicyError(
                "TRUST_POLICY_INVALID",
                f"rule {entry['id']!r} restricted_to must be strings")
        rules.append(SourceRule(entry["id"].strip(),
                                entry["match"].strip(),
                                entry["source_class"].strip(),
                                entry["role"].strip(),
                                tuple(restricted)))

    return TrustPolicy(
        version=version.strip(),
        default_source_class=default,
        source_classes=tuple(classes),
        source_rules=tuple(rules),
        policy_source="explicit",
        digest=policy_digest(raw),
    )


def load_trust_policy(project_dir: Path) -> dict:
    """Load result: policy (explicit or builtin), or a disabling error.
    Malformed explicit files fail visibly (valid=False); absence yields
    the conservative builtin (configured=False, still healthy)."""
    path = Path(project_dir) / TRUST_POLICY_REL
    if not path.is_file():
        return {"policy": builtin_policy(), "configured": False,
                "valid": True, "error": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"policy": builtin_policy(), "configured": True,
                "valid": False,
                "error": f"TRUST_POLICY_INVALID:unreadable file: {exc}"}
    try:
        policy = validate_policy_dict(raw)
    except PolicyError as exc:
        return {"policy": builtin_policy(), "configured": True,
                "valid": False, "error": f"{exc.code}:{exc.message}"}
    return {"policy": policy, "configured": True, "valid": True,
            "error": None}
