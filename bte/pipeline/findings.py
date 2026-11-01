from __future__ import annotations

from dataclasses import asdict, dataclass, field

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "critical": 3}


@dataclass
class Finding:
    layer: str  # L0 gates, L1 answer, L2 coverage, L5 judge, L6 audit, ...
    code: str
    severity: str  # info | minor | major | critical
    message: str
    segment: str | None = None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def blocking(self) -> bool:
        return SEVERITY_RANK[self.severity] >= SEVERITY_RANK["major"]


def worst(findings: list[Finding]) -> str:
    if not findings:
        return "none"
    return max(findings, key=lambda f: SEVERITY_RANK[f.severity]).severity
