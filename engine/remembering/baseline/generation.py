# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Answer generation: a capable reader over admitted context.

The reader is instructed to answer from supplied evidence, cite the
evidence identifiers it relied on, and abstain when the evidence does
not determine an answer. It never receives hidden ground truth.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .config import GeneratorConfig
from .context import ContextTrace
from .embeddings import _post_json


@dataclass
class Answer:
    text: str
    cited_chunk_ids: list[str] = field(default_factory=list)
    cited_source_ids: list[str] = field(default_factory=list)
    abstained: bool = False
    model: str = ""
    latency_ms: float = 0.0


ABSTAIN_MARKERS = (
    "cannot determine",
    "not enough evidence",
    "does not determine",
    "no evidence",
    "i don't know",
    "insufficient evidence",
    "abstain",
)


class OllamaGenerator:
    name = "ollama"

    def __init__(self, cfg: GeneratorConfig) -> None:
        self.cfg = cfg

    def answer(self, question: str, context: ContextTrace) -> Answer:
        started = time.perf_counter()
        evidence = context.render() or "(no evidence retrieved)"
        prompt = (
            f"PROJECT EVIDENCE:\n{evidence}\n\n"
            f"QUESTION: {question}\n\n"
            "Respond in JSON with keys: answer (string), "
            "cited_ids (list of [source:...] identifiers you used), "
            "abstain (boolean, true when the evidence does not "
            "determine an answer)."
        )
        try:
            out = _post_json(
                f"{self.cfg.ollama_host}/api/chat",
                {
                    "model": self.cfg.model,
                    "stream": False,
                    "options": {"temperature": self.cfg.temperature},
                    "messages": [
                        {"role": "system", "content": self.cfg.system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=600,
            )
            raw = out["message"]["content"]
        except Exception as exc:
            return Answer(
                text=f"",
                abstained=True,
                model=f"{self.cfg.model} (call failed: {type(exc).__name__})",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        text, cited, abstained = _parse_response(raw)
        lowered = text.lower()
        if not abstained and any(m in lowered for m in ABSTAIN_MARKERS):
            abstained = True
        return Answer(
            text=text,
            cited_chunk_ids=[],
            cited_source_ids=cited,
            abstained=abstained,
            model=self.cfg.model,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )


def _parse_response(raw: str) -> tuple[str, list[str], bool]:
    """Best-effort JSON parse with a plain-text fallback."""
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
            answer = str(payload.get("answer", "")).strip()
            cited = [str(c) for c in payload.get("cited_ids", [])]
            abstained = bool(payload.get("abstain", False))
            if answer:
                return answer, cited, abstained
        except (json.JSONDecodeError, AttributeError):
            pass
    return text, [], False


class ExtractiveReader:
    """Test-double reader: quotes the top admitted passage.

    Clearly labelled wherever used. Exercises the plumbing —
    retrieval trace, context trace, citations — without spending
    model calls.
    """

    name = "extractive"

    def __init__(self, model_label: str = "extractive-test-double") -> None:
        self.model_label = model_label

    def answer(self, question: str, context: ContextTrace) -> Answer:
        if not context.admitted:
            return Answer(
                text="No evidence was retrieved; cannot determine an answer.",
                abstained=True,
                model=self.model_label,
            )
        top = context.admitted[0]
        return Answer(
            text=f"Top evidence [{top.source_id}]: {top.text[:600]}",
            cited_chunk_ids=[top.chunk_id],
            cited_source_ids=[top.source_id],
            abstained=False,
            model=self.model_label,
        )
