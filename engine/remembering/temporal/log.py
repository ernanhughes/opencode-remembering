# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Append-only durable event log (JSONL) with deterministic replay.

The log is canonical. Projections are derived and rebuildable. Recorder
metadata (received_at, ingest_seq) describes observation, never the event.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .model import (EventEnvelope, check_causal_temporal, reference_errors,
                  validate_event)


class EventLog:
    def __init__(self) -> None:
        self._records: list[dict] = []  # {"envelope": dict, "received_at", "ingest_seq"}
        self._by_id: dict[str, EventEnvelope] = {}
        self._source_seqs: dict[str, set[int]] = {}
        self._gaps: list[dict] = []

    # -- ingest ------------------------------------------------------
    def append(self, event: EventEnvelope, received_at: str) -> dict:
        """Append one envelope with recorder observation metadata."""
        report = validate_event(event, set(self._by_id), self._source_seqs)
        if not report.ok:
            raise ValueError(f"invalid event {event.event_id}: {report.errors}")
        seqs = self._source_seqs.setdefault(event.source_id, set())
        if seqs:
            expected = max(seqs) + 1
            if event.source_seq > expected:
                self._gaps.append({
                    "source_id": event.source_id,
                    "expected_seq": expected,
                    "observed_seq": event.source_seq,
                    "status": "GAP_DETECTED",
                })
        seqs.add(event.source_seq)
        ingest_seq = len(self._records)
        record = {
            "envelope": event.to_dict(),
            "received_at": received_at,
            "ingest_seq": ingest_seq,
        }
        self._records.append(record)
        self._by_id[event.event_id] = event
        return record

    # -- access ------------------------------------------------------
    def __len__(self) -> int:
        return len(self._records)

    def records(self) -> list[dict]:
        return list(self._records)

    def events(self) -> list[EventEnvelope]:
        return [self._by_id[r["envelope"]["event_id"]] for r in self._records]

    def by_id(self) -> dict[str, EventEnvelope]:
        return dict(self._by_id)

    def gaps(self) -> list[dict]:
        return list(self._gaps)

    def arrival_order(self) -> list[str]:
        return [r["envelope"]["event_id"] for r in self._records]

    def event_time_order(self) -> list[str]:
        return [e.event_id for e in
                sorted(self.events(), key=lambda e: (e.event_time, e.event_id))]

    def unknown_refs(self) -> list[str]:
        """Deferred referential check over the frozen log."""
        out: list[str] = []
        known = set(self._by_id)
        for event in self.events():
            out.extend(f"{event.event_id}:{e}" for e in
                       reference_errors(event, known))
        return out

    def causality_violations(self) -> list[str]:
        out: list[str] = []
        for event in self.events():
            out.extend(check_causal_temporal(event, self._by_id))
        return out

    def digest(self) -> str:
        """Deterministic digest over semantic content (order-independent)."""
        canonical = sorted(
            json.dumps(r["envelope"], sort_keys=True) for r in self._records)
        return hashlib.sha256("\n".join(canonical).encode()).hexdigest()[:16]

    # -- persistence -------------------------------------------------
    def write_jsonl(self, path: str | Path) -> Path:
        target = Path(path)
        with target.open("w", encoding="utf-8") as handle:
            for record in self._records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return target

    @classmethod
    def read_jsonl(cls, path: str | Path) -> "EventLog":
        log = cls()
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                log.append(EventEnvelope.from_dict(raw["envelope"]),
                           raw.get("received_at", ""))
        return log
