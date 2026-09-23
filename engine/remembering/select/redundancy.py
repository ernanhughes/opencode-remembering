# Bundled remembering engine (opencode-remembering product code).
"""Redundancy grouping over explicit structure first. Similarity is
never the primary rule: same claim keys, shared derivation roots,
same-source identical spans, and byte-identical normalized content.
Prefer false negatives over deleting distinct decisive evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .model import REDUNDANCY_POLICY_VERSION, SelectionCandidate

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_text(text: str) -> str:
    collapsed = _WS_RE.sub(" ", (text or "").strip().lower())
    return _PUNCT_RE.sub("", collapsed)


def content_digest(text: str) -> str:
    return hashlib.sha256(
        normalize_text(text).encode("utf-8")).hexdigest()[:16]


@dataclass
class RedundancyGroup:
    group_id: str
    kind: str  # claim | echo | duplicate_span | identical_content
    members: list[str] = field(default_factory=list)
    representative: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "kind": self.kind,
            "members": list(self.members),
            "representative": self.representative,
            "reason": self.reason,
        }


def group_candidates(candidates: list[SelectionCandidate],
                     roots_of) -> list[RedundancyGroup]:
    """Build redundancy groups. roots_of(source_id) -> frozenset."""
    groups: list[RedundancyGroup] = []
    by_id = {c.chunk_id: c for c in candidates}

    # Claim groups: shared explicit claim keys.
    claim_members: dict[str, list[str]] = {}
    for candidate in candidates:
        if candidate.claim_key:
            claim_members.setdefault(candidate.claim_key, []).append(
                candidate.chunk_id)
    for key in sorted(claim_members):
        members = sorted(claim_members[key])
        if len(members) > 1:
            groups.append(RedundancyGroup(
                group_id=f"claim:{key}", kind="claim", members=members,
                reason="shared explicit claim key"))

    # Echo groups: shared derivation roots (same lineage, not
    # independent). Singletons excluded.
    root_members: dict[frozenset, list[str]] = {}
    for candidate in candidates:
        roots = roots_of(candidate.source_id)
        root_members.setdefault(roots, []).append(candidate.chunk_id)
    for roots in sorted(root_members, key=lambda r: sorted(r)):
        members = sorted(root_members[roots])
        if len(members) > 1 and not any(
                g.kind == "claim" and set(g.members) == set(members)
                for g in groups):
            groups.append(RedundancyGroup(
                group_id="echo:" + ",".join(sorted(roots) or ["noroot"]),
                kind="echo", members=members,
                reason="shared derivation roots"))

    # Same-source identical spans and byte-identical content.
    seen: dict[tuple, list[str]] = {}
    for candidate in candidates:
        digest = content_digest(candidate.text)
        seen.setdefault((candidate.source_id, digest), []).append(
            candidate.chunk_id)
    for (source, _), members in sorted(seen.items()):
        members = sorted(members)
        if len(members) > 1:
            groups.append(RedundancyGroup(
                group_id=f"duplicate-span:{source}", kind="duplicate_span",
                members=members,
                reason="identical normalized content in one source"))
    content_members: dict[str, list[str]] = {}
    for candidate in candidates:
        content_members.setdefault(
            content_digest(candidate.text), []).append(candidate.chunk_id)
    for digest in sorted(content_members):
        members = sorted(content_members[digest])
        if len(members) > 1 and not any(
                set(g.members) == set(members) for g in groups):
            groups.append(RedundancyGroup(
                group_id=f"identical:{digest}", kind="identical_content",
                members=members,
                reason="byte-identical normalized content"))
    _ = by_id
    return groups


def choose_representative(group: RedundancyGroup,
                          candidates: list[SelectionCandidate],
                          priority_of) -> str:
    """Deterministic representative: highest priority, then fused
    rank, then source id, then chunk id."""
    by_id = {c.chunk_id: c for c in candidates}
    ordered = sorted(
        group.members,
        key=lambda cid: (priority_of(by_id[cid]), by_id[cid].fused_rank,
                         by_id[cid].source_id, cid))
    return ordered[0]


def policy_version() -> str:
    return REDUNDANCY_POLICY_VERSION
