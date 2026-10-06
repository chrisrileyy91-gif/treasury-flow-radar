"""Central research-evidence records with support and provenance requirements."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from treasury_flow_radar.analytics.descriptive import EvidenceType


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_type: EvidenceType
    statement: str
    supporting_observations: tuple[Mapping[str, Any], ...] = ()
    source: str | None = None
    observation_dates: tuple[str, ...] = ()
    retrieval_dates: tuple[str, ...] = ()
    confidence: float | None = None
    confidence_basis: str | None = None

    def __post_init__(self) -> None:
        if not self.statement.strip():
            raise ValueError("evidence statement cannot be empty")
        if self.confidence is not None:
            if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
                raise ValueError("confidence must be finite and between 0 and 1")
            if not self.confidence_basis or not self.confidence_basis.strip():
                raise ValueError("confidence requires a defined statistical basis")
        elif self.confidence_basis is not None:
            raise ValueError("confidence_basis requires a confidence value")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_type"] = self.evidence_type.value
        return result


def evidence_from_rows(
    evidence_type: EvidenceType,
    statement: str,
    rows: Iterable[Mapping[str, Any]],
) -> EvidenceRecord:
    """Create a statement whose listed observation/provenance inputs are auditable."""
    supports = tuple(dict(row) for row in rows)
    sources = sorted({str(r.get("source_identifier")) for r in supports if r.get("source_identifier")})
    dates = sorted({str(r.get("observation_time", ""))[:10] for r in supports
                    if r.get("observation_time")})
    retrievals = sorted({str(r.get("retrieval_time")) for r in supports if r.get("retrieval_time")})
    return EvidenceRecord(
        evidence_type=evidence_type,
        statement=statement,
        supporting_observations=supports,
        source=", ".join(sources) or None,
        observation_dates=tuple(dates),
        retrieval_dates=tuple(retrievals),
    )

