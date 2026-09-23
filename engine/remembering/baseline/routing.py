# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Query routing: historical recall vs present-action influence.

A central empirical result of the book (Chapter 6 Nexus, capstone
``simulator/pipeline.py``): historical recall and present influence
are different optimization problems. Recall asks what the record
says; influence asks what should steer present behavior. Authority
must not erase history: a record denied standing to direct action
may still be legitimate evidence for a recall question
(``tests/test_routing.py::test_recall_route_keeps_preference_q000018``).

This module answers only the routing question — which use the caller
is asking memory to serve. It implements no temporal validity, no
trust admission, no frame policy. Both routes use the same strong
hybrid retriever; what differs is the semantic contract downstream,
which later stages attach to ``RoutingDecision.route``.

Frozen Stage 3 boundary (do not weaken it when adding temporal
resolution): recall preserves historical candidates and does not
suppress superseded evidence merely because it is no longer current.
Influence resolves evidence toward the applicable current state
before it is allowed to steer behaviour. Recall therefore bypasses
*current-state suppression*, not temporal interpretation itself: a
recall question such as "what was true on 11 July" may still require
valid-time/known-time reasoning, while "what did we use before
PostgreSQL" must keep the superseded SQLite evidence available.

The router is deliberately deterministic, cheap, and replaceable:
explicit caller intent wins, then a small documented pattern
function, then an observable ambiguous fallback. No model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class MemoryRoute(str, Enum):
    RECALL = "recall"
    INFLUENCE = "influence"


EXPLICIT = "explicit"
DETERMINISTIC = "deterministic"

ROUTING_POLICY_VERSION = "routing-v0.1"


@dataclass(frozen=True)
class RoutingDecision:
    route: MemoryRoute
    source: str  # "explicit" | "deterministic"
    reason: str
    ambiguous: bool = False

    def to_dict(self) -> dict:
        return {
            "route": self.route.value,
            "route_source": self.source,
            "route_reason": self.reason,
            "route_ambiguous": self.ambiguous,
        }


# Recall: interrogative-past forms ("what did …", "why was …") and
# explicit history nouns ("previously", "originally", "show me").
# Each pattern is anchored to question structure, not to lone nouns:
# "the old design" carries no signal, "show me the old plan" does.
_RECALL_PATTERNS = (
    r"\bwhat\s+(did|was|were|changed|happened)\b",
    r"\bwhere\s+(did|was|were)\b",
    r"\bwhen\s+(did|was|were)\b",
    r"\bwhy\s+(did|was|were)\b",
    r"\bhow\s+did\b",
    r"\bprevious\b|\bpreviously\b",
    r"\bearlier\b",
    r"\boriginally\b|\boriginal\b",
    r"\bhistory\b|\bhistorical\b",
    r"\bused\s+to\b",
    r"\blast\s+time\b",
    r"\bshow\s+me\b",
)

# Influence: present-action verbs and modals. Bare state nouns
# ("current state", "the configuration") carry no signal on their
# own: influence requires an action to steer.
_INFLUENCE_PATTERNS = (
    r"\bshould\b",
    r"\bhow\s+(do|can|to)\b",
    r"\bimplement\b",
    r"\bfix\b",
    r"\bchange\b|\bupdate\b",
    r"\bcreate\b|\bbuild\b",
    r"\bdeploy\b",
    r"\brefactor\b",
    r"\bchoose\b|\brecommend\b",
    r"\bgenerate\b",
    r"\bnext\b.*\b(question|step|work|do|task)\b",
)

_RECALL_RES = [re.compile(p, re.IGNORECASE) for p in _RECALL_PATTERNS]
_INFLUENCE_RES = [re.compile(p, re.IGNORECASE) for p in _INFLUENCE_PATTERNS]


def _first_match(compiled: list, text: str) -> str | None:
    for rx in compiled:
        match = rx.search(text)
        if match:
            return match.group(0).strip().lower()
    return None


def parse_explicit_route(value: str | MemoryRoute | None) -> MemoryRoute | None:
    """Validate an explicit route request. Unknown values raise:
    routing must be explicit, never silently reinterpreted."""
    if value is None:
        return None
    if isinstance(value, MemoryRoute):
        return value
    if isinstance(value, str) and value.strip().lower() == "recall":
        return MemoryRoute.RECALL
    if isinstance(value, str) and value.strip().lower() == "influence":
        return MemoryRoute.INFLUENCE
    raise ValueError(
        f"unknown route {value!r}: expected 'recall' or 'influence'."
    )


def classify_route(
    query: str, explicit: str | MemoryRoute | None = None
) -> RoutingDecision:
    """Route one request. Explicit intent wins; then recall-past
    interrogatives (which take precedence over action verbs, since
    "why did we choose X" asks about the past choice); then
    influence signals; otherwise an observable ambiguous fallback to
    INFLUENCE, because injected context can affect present behavior
    and must be labeled as the riskier use."""
    route = parse_explicit_route(explicit)
    if route is not None:
        return RoutingDecision(
            route=route,
            source=EXPLICIT,
            reason=f"caller requested route {route.value!r}",
        )
    text = (query or "").strip()
    hit = _first_match(_RECALL_RES, text)
    if hit is not None:
        return RoutingDecision(
            route=MemoryRoute.RECALL,
            source=DETERMINISTIC,
            reason=f"matched recall signal {hit!r}",
        )
    hit = _first_match(_INFLUENCE_RES, text)
    if hit is not None:
        return RoutingDecision(
            route=MemoryRoute.INFLUENCE,
            source=DETERMINISTIC,
            reason=f"matched influence signal {hit!r}",
        )
    return RoutingDecision(
        route=MemoryRoute.INFLUENCE,
        source=DETERMINISTIC,
        reason=("no strong recall or influence signal; conservative "
                "fallback to influence, flagged ambiguous"),
        ambiguous=True,
    )


def route_for_request(
    query: str, route: str | MemoryRoute | None = None
) -> RoutingDecision:
    """``route`` is None/'auto' for inference, or an explicit route."""
    if route is None or (
        isinstance(route, str) and route.strip().lower() == "auto"
    ):
        return classify_route(query)
    return classify_route(query, explicit=route)


# -- route-aware bundle semantics ---------------------------------------
# Boundary language only, not enforcement. Later temporal/trust stages
# attach real policy to RoutingDecision.route; these strings merely
# label which contract the bundle is served under.

RECALL_GUIDANCE = (
    "Route: recall. This evidence is supplied for historical "
    "reconstruction. Superseded or obsolete material may still be "
    "relevant because the task is asking about the past. Do not "
    "reinterpret historical evidence as current policy unless "
    "separately established."
)

INFLUENCE_GUIDANCE = (
    "Route: influence. This evidence may affect a present action. "
    "Retrieval relevance alone does not establish that remembered "
    "material is current, authoritative, or safe. Temporal validity "
    "and trust controls are not yet implemented: treat retrieved "
    "evidence as evidence, not automatically as current instruction."
)


def guidance_for(route: MemoryRoute) -> str:
    return RECALL_GUIDANCE if route is MemoryRoute.RECALL else INFLUENCE_GUIDANCE


# -- deterministic contract fixtures -------------------------------------
# (query, expected_route, expected_ambiguous). The routing evaluation:
# recall correct, influence correct, ambiguity surfaced, override wins.

ROUTING_CONTRACT_FIXTURES: list[tuple[str, str, bool]] = [
    # clear recall
    ("Where did we discuss pgvector?", "recall", False),
    ("What did we decide about embeddings?", "recall", False),
    ("Why did we originally choose PostgreSQL?", "recall", False),
    ("What happened in the previous implementation?", "recall", False),
    ("Show me the old migration plan.", "recall", False),
    ("What changed between the first design and the final one?", "recall", False),
    ("What did we decide about Redis in July?", "recall", False),
    # clear influence
    ("How should I implement the storage layer?", "influence", False),
    ("Fix the current migration.", "influence", False),
    ("What should we do next?", "influence", False),
    ("Update the plugin to support this.", "influence", False),
    ("Which configuration should I use now?", "influence", False),
    ("Which database should I use for cache metadata now?", "influence", False),
    ("Generate the deployment plan.", "influence", False),
    # ambiguous: observable, conservative fallback
    ("PostgreSQL schema", "influence", True),
    ("Embedding configuration", "influence", True),
    ("Memory architecture", "influence", True),
    ("The old design", "influence", True),
    ("Current state", "influence", True),
]


def evaluate_router() -> dict:
    """Deterministic routing contract: counts, never a model judgment."""
    recall_ok = influence_ok = ambiguous_ok = 0
    recall_n = influence_n = ambiguous_n = 0
    failures: list[dict] = []
    for query, expected, expected_ambiguous in ROUTING_CONTRACT_FIXTURES:
        decision = classify_route(query)
        if expected_ambiguous:
            ambiguous_n += 1
            if (decision.route.value == expected and decision.ambiguous
                    and decision.source == DETERMINISTIC):
                ambiguous_ok += 1
            else:
                failures.append({"query": query, "got": decision.to_dict()})
        elif expected == "recall":
            recall_n += 1
            if decision.route is MemoryRoute.RECALL and not decision.ambiguous:
                recall_ok += 1
            else:
                failures.append({"query": query, "got": decision.to_dict()})
        else:
            influence_n += 1
            if (decision.route is MemoryRoute.INFLUENCE
                    and not decision.ambiguous):
                influence_ok += 1
            else:
                failures.append({"query": query, "got": decision.to_dict()})
    # Explicit override wins in both directions. These are separate
    # checks, not fixtures: 19 contract fixtures + 2 override checks =
    # 21 total checks.
    override_ok = 0
    if classify_route("Where did we discuss pgvector?",
                      explicit="influence").route is MemoryRoute.INFLUENCE:
        override_ok += 1
    if classify_route("Fix the current migration.",
                      explicit="recall").route is MemoryRoute.RECALL:
        override_ok += 1
    checks_total = len(ROUTING_CONTRACT_FIXTURES) + 2
    checks_passed = (recall_ok + influence_ok + ambiguous_ok + override_ok)
    return {
        "examples": len(ROUTING_CONTRACT_FIXTURES),
        "recall_correct": recall_ok,
        "recall_total": recall_n,
        "influence_correct": influence_ok,
        "influence_total": influence_n,
        "ambiguous_correct": ambiguous_ok,
        "ambiguous_total": ambiguous_n,
        "override_correct": override_ok,
        "override_total": 2,
        "checks_total": checks_total,
        "checks_passed": checks_passed,
        "failures": failures,
        "passed": not failures and override_ok == 2,
    }
