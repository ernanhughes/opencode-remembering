# Bundled remembering engine (opencode-remembering product code).
"""Write authorization policy: who may append what kind of memory.

Deterministic and inspectable: no models, no scores. Absence of an
explicit file yields the conservative builtin (ordinary REMEMBER only,
capped at untrusted standing, no relationship actions, no backdating).
A malformed explicit file fails closed for writes (WRITE_POLICY_INVALID)
without silently falling back to the builtin.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import WRITE_POLICY_SCHEMA
from .model import BUILTIN_ROLES, CLASS_ORDER, KNOWN_ROLES, WRITE_ACTIONS

WRITE_POLICY_REL = Path(".remembering") / "write-policy.json"

BUILTIN_POLICY_VERSION = "builtin-writes-v0.1"
BUILTIN_GRANT_ID = "builtin-untrusted-remember"


class PolicyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WriteGrant:
    id: str
    surfaces: tuple[str, ...]
    caller_scopes: tuple[str, ...]
    actions: tuple[str, ...]
    roles: tuple[str, ...]
    standing_ceiling: str
    target_classes: tuple[str, ...]
    allow_backdating: bool = False

    def allows_surface(self, surface: str) -> bool:
        return "*" in self.surfaces or surface in self.surfaces

    def allows_scope(self, scope: str) -> bool:
        return "*" in self.caller_scopes or scope in self.caller_scopes


@dataclass(frozen=True)
class WritePolicy:
    version: str
    grants: tuple[WriteGrant, ...]
    schema_version: str = WRITE_POLICY_SCHEMA
    policy_source: str = "explicit"
    digest: str = ""

    def grant(self, grant_id: str) -> WriteGrant | None:
        for item in self.grants:
            if item.id == grant_id:
                return item
        return None


def builtin_policy() -> WritePolicy:
    """Conservative default: an agent may preserve ordinary history as
    attributed untrusted evidence, but the write itself earns no
    behavioral authority. Relationship actions need an explicit grant."""
    return WritePolicy(
        version=BUILTIN_POLICY_VERSION,
        grants=(
            WriteGrant(
                id=BUILTIN_GRANT_ID,
                surfaces=("opencode", "cli", "import"),
                caller_scopes=("*",),
                actions=("remember",),
                roles=BUILTIN_ROLES,
                standing_ceiling="untrusted",
                target_classes=(),
                allow_backdating=False,
            ),
        ),
        policy_source="builtin_default",
        digest="builtin",
    )


def policy_digest(raw: dict) -> str:
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _string_list(raw, label: str, grant_id: str,
                 allow_star: bool = True) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or not all(
            isinstance(v, str) and v.strip() for v in raw):
        raise PolicyError("WRITE_POLICY_INVALID",
                          f"grant {grant_id!r}: {label} must be a "
                          "non-empty list of strings")
    values = tuple(v.strip() for v in raw)
    if not allow_star and "*" in values:
        raise PolicyError("WRITE_POLICY_INVALID",
                          f"grant {grant_id!r}: {label} must not use '*'")
    return values


def validate_policy_dict(raw: dict) -> WritePolicy:
    if not isinstance(raw, dict):
        raise PolicyError("WRITE_POLICY_INVALID",
                          "write policy must be an object")
    if raw.get("schema_version", WRITE_POLICY_SCHEMA) != WRITE_POLICY_SCHEMA:
        raise PolicyError(
            "WRITE_POLICY_INVALID",
            f"unsupported schema {raw.get('schema_version')!r}; "
            f"expected {WRITE_POLICY_SCHEMA!r}")
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PolicyError("WRITE_POLICY_INVALID",
                          "write policy version must be set")
    grants_raw = raw.get("grants")
    if not isinstance(grants_raw, list) or not grants_raw:
        raise PolicyError("WRITE_POLICY_INVALID",
                          "write policy grants must be a non-empty list")
    grants: list[WriteGrant] = []
    seen: set[str] = set()
    for entry in grants_raw:
        if not isinstance(entry, dict) or not isinstance(
                entry.get("id"), str) or not entry["id"].strip():
            raise PolicyError("WRITE_POLICY_INVALID",
                              "each write grant needs an id")
        grant_id = entry["id"].strip()
        if grant_id in seen:
            raise PolicyError("WRITE_POLICY_INVALID",
                              f"duplicate grant id {grant_id!r}")
        seen.add(grant_id)
        surfaces = _string_list(entry.get("surfaces"), "surfaces",
                                grant_id)
        scopes = _string_list(entry.get("caller_scopes"),
                              "caller_scopes", grant_id)
        actions = _string_list(entry.get("actions"), "actions",
                               grant_id, allow_star=False)
        for action in actions:
            if action.strip().lower() not in WRITE_ACTIONS:
                raise PolicyError(
                    "WRITE_POLICY_INVALID",
                    f"grant {grant_id!r} names unknown action "
                    f"{action!r}")
        roles = _string_list(entry.get("roles"), "roles", grant_id,
                             allow_star=False)
        for role in roles:
            if role.strip().lower() not in KNOWN_ROLES:
                raise PolicyError(
                    "WRITE_POLICY_INVALID",
                    f"grant {grant_id!r} names unknown role {role!r}")
        ceiling = entry.get("standing_ceiling")
        if not isinstance(ceiling, str) or ceiling.strip() not in \
                CLASS_ORDER:
            raise PolicyError(
                "WRITE_POLICY_INVALID",
                f"grant {grant_id!r} needs standing_ceiling one of "
                f"{CLASS_ORDER}")
        targets = entry.get("target_classes", list(CLASS_ORDER))
        if not isinstance(targets, list) or not targets or not all(
                isinstance(v, str) and v.strip() in CLASS_ORDER
                for v in targets):
            raise PolicyError(
                "WRITE_POLICY_INVALID",
                f"grant {grant_id!r}: target_classes must be a "
                f"non-empty subset of {list(CLASS_ORDER)}")
        backdating = entry.get("allow_backdating", False)
        if not isinstance(backdating, bool):
            raise PolicyError("WRITE_POLICY_INVALID",
                              f"grant {grant_id!r}: allow_backdating "
                              "must be a boolean")
        grants.append(WriteGrant(
            id=grant_id,
            surfaces=tuple(s.strip() for s in surfaces),
            caller_scopes=tuple(s.strip() for s in scopes),
            actions=tuple(a.strip().lower() for a in actions),
            roles=tuple(r.strip().lower() for r in roles),
            standing_ceiling=ceiling.strip(),
            target_classes=tuple(t.strip() for t in targets),
            allow_backdating=backdating,
        ))
    return WritePolicy(
        version=version.strip(),
        grants=tuple(grants),
        policy_source="explicit",
        digest=policy_digest(raw),
    )


def load_write_policy(project_dir: Path) -> dict:
    """Absence yields the conservative builtin (configured=False,
    healthy). Malformed explicit files fail visibly (valid=False):
    the caller must disable writes, never silently use the builtin."""
    path = Path(project_dir) / WRITE_POLICY_REL
    if not path.is_file():
        return {"policy": builtin_policy(), "configured": False,
                "valid": True, "error": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"policy": builtin_policy(), "configured": True,
                "valid": False,
                "error": f"WRITE_POLICY_INVALID:unreadable file: {exc}"}
    try:
        policy = validate_policy_dict(raw)
    except PolicyError as exc:
        return {"policy": builtin_policy(), "configured": True,
                "valid": False, "error": f"{exc.code}:{exc.message}"}
    return {"policy": policy, "configured": True, "valid": True,
            "error": None}


def _specificity(grant: WriteGrant, surface: str,
                 scope: str) -> tuple[int, int] | None:
    """Specificity for deterministic precedence: exact surface +
    exact scope outranks wildcards. Returns None when the grant does
    not match at all."""
    if "*" in grant.surfaces:
        surface_score = 1
    elif surface in grant.surfaces:
        surface_score = 2
    else:
        return None
    if "*" in grant.caller_scopes:
        scope_score = 1
    elif scope in grant.caller_scopes:
        scope_score = 2
    else:
        return None
    return (surface_score, scope_score)


def match_grant(policy: WritePolicy, surface: str,
                caller_scope: str, action: str) -> WriteGrant | None:
    """Most-specific matching grant wins; file order is the final
    deterministic tie-break (first wins). Only grants listing the
    action are candidates."""
    best: WriteGrant | None = None
    best_key: tuple[int, int] | None = None
    for grant in policy.grants:
        if action not in grant.actions:
            continue
        key = _specificity(grant, surface, caller_scope)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best, best_key = grant, key
    return best
