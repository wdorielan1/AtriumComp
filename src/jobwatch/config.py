"""Loads competitors.yaml / categories.yaml and runtime settings from env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(os.environ.get("JOBWATCH_CONFIG_DIR", Path(__file__).resolve().parent / "config"))


@dataclass
class Competitor:
    name: str
    homepage: str = ""
    careers_url: str = ""
    type: str = "generic"
    board: str = ""
    max_pages: int = 5
    enabled: bool = True
    verify: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@lru_cache
def load_competitors() -> list[Competitor]:
    data = yaml.safe_load((CONFIG_DIR / "competitors.yaml").read_text())
    out = []
    for raw in data["competitors"]:
        raw = dict(raw)
        known = {k: raw.pop(k) for k in list(raw) if k in Competitor.__dataclass_fields__}
        c = Competitor(**known, extra=raw)
        if c.enabled and (c.homepage or c.careers_url):
            out.append(c)
    return out


@lru_cache
def load_categories() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "categories.yaml").read_text())


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}
