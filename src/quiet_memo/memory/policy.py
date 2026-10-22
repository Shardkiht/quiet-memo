from __future__ import annotations

from dataclasses import dataclass

from .models import Candidate


@dataclass(slots=True)
class Decision:
    admit: bool
    reason: str


def decide(candidate: Candidate, threshold: float = 0.8) -> Decision:
    if candidate.kind in ("update", "conflict"):
        return Decision(False, candidate.reason or f"kind={candidate.kind}")
    if candidate.sensitive:
        return Decision(False, candidate.reason or "sensitive")
    if candidate.confidence < threshold:
        return Decision(False, f"low confidence {candidate.confidence:.2f}")
    return Decision(True, "auto")
