# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine.
"""Baseline health: is the index itself sound?

System health is not memory quality. These checks verify that the
baseline deserves to be measured at all; Chapter 2 then measures
whether its memory is any good.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .pipeline import Baseline


@dataclass
class HealthIssue:
    check: str
    detail: str


@dataclass
class HealthReport:
    healthy: bool
    sources: int = 0
    chunks: int = 0
    duplicate_chunks: int = 0
    orphan_chunks: int = 0
    embedding_versions: list[dict] = field(default_factory=list)
    fts_index: str | None = None
    hnsw_index: str | None = None
    issues: list[HealthIssue] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "RAG Baseline Health",
            "",
            f"Sources indexed:        {self.sources}",
            f"Chunks:                 {self.chunks}",
            f"Duplicate chunks:       {self.duplicate_chunks}",
            f"Orphan chunks:          {self.orphan_chunks}",
            f"FTS coverage:           {'indexed' if self.fts_index else 'MISSING'}",
            f"Vector coverage:        {'indexed' if self.hnsw_index else 'MISSING'}",
        ]
        for item in self.embedding_versions:
            lines.append(
                f"Embedding {item['version']}: {item['chunks']} chunks"
            )
        lines.append("")
        if self.issues:
            lines.append("Issues:")
            for issue in self.issues:
                lines.append(f"  [{issue.check}] {issue.detail}")
        else:
            lines.append("No issues detected.")
        lines.append(f"Verdict: {'HEALTHY' if self.healthy else 'UNHEALTHY'}")
        return "\n".join(lines) + "\n"


def check_health(baseline: Baseline) -> HealthReport:
    stats = baseline.store.stats()
    report = HealthReport(
        healthy=True,
        sources=stats["sources"],
        chunks=stats["chunks"],
        duplicate_chunks=stats["duplicate_chunks"],
        orphan_chunks=baseline.store.orphan_chunks(),
        embedding_versions=stats["embedding_versions"],
        fts_index=stats["fts_index"],
        hnsw_index=stats["hnsw_index"],
    )

    def flag(check: str, detail: str) -> None:
        report.issues.append(HealthIssue(check, detail))
        report.healthy = False

    if report.sources == 0:
        flag("empty-store", "no sources indexed")
    if report.duplicate_chunks:
        flag(
            "duplicate-chunks",
            f"{report.duplicate_chunks} chunks share content hashes",
        )
    if report.orphan_chunks:
        flag(
            "orphan-chunks",
            f"{report.orphan_chunks} chunks lack a source record",
        )
    if len(report.embedding_versions) > 1:
        flag(
            "stale-embeddings",
            "multiple embedding versions present; re-embed to one version",
        )
    if report.fts_index is None:
        flag("missing-fts", "full-text index absent")
    if report.hnsw_index is None and report.chunks > 1000:
        # Below this scale exact search is fast enough that a missing
        # ANN index is a scale limitation, not a defect. Above it, the
        # baseline cannot claim production-grade latency without one.
        flag("missing-hnsw", "vector index absent on a large store; dense "
             "search falls back to exact scan")
    current = baseline.embedding_version
    if report.embedding_versions and any(
        item["version"] != current for item in report.embedding_versions
    ):
        flag(
            "embedding-mismatch",
            f"store holds other versions than configured ({current})",
        )
    return report
