"""Data model shared by scrapers, storage, and the dashboard."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Job:
    competitor: str
    title: str
    url: str
    location: str = ""
    description: str = ""
    posted: str = ""  # as reported by the site (ISO date when available)
    salary: str = ""
    employment_type_raw: str = ""  # what the site itself says, e.g. JSON-LD "CONTRACTOR"
    ref: str = ""  # site's own job reference/id, if any
    source: str = ""  # which adapter found it

    # Filled in by the pipeline
    categories: list[str] = field(default_factory=list)
    employment_type: str = "Unknown"  # Temp / Contract | Contract-to-hire | Direct hire | Unknown
    first_seen: str = ""
    last_seen: str = ""
    decode: dict[str, Any] | None = None

    @property
    def job_id(self) -> str:
        key = f"{self.competitor}|{self.ref or self.url}".lower()
        return hashlib.sha256(key.encode()).hexdigest()[:24]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["job_id"] = self.job_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
