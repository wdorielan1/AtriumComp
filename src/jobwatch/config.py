"""Loads competitors.yaml / categories.yaml and runtime settings from env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
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
def builtin_competitors() -> list[Competitor]:
    """Everything in competitors.yaml, including paused and suggested entries."""
    data = yaml.safe_load((CONFIG_DIR / "competitors.yaml").read_text())
    out = []
    for raw in data["competitors"]:
        raw = dict(raw)
        known = {k: raw.pop(k) for k in list(raw) if k in Competitor.__dataclass_fields__}
        out.append(Competitor(**known, extra=raw))
    return out


def load_competitors() -> list[Competitor]:
    """Built-in competitors that are switched on (ignores dashboard changes; see merged_competitors)."""
    return [c for c in builtin_competitors() if c.enabled and (c.homepage or c.careers_url)]


def merged_competitors(overrides: list[dict] | None = None) -> list[tuple[Competitor, str]]:
    """competitors.yaml plus what was added or changed on the dashboard, as (competitor, source) pairs.

    source is "built-in", "suggested" (in the file but switched off, ready to add) or "added" (dashboard only).
    A dashboard entry with the same name as a built-in one overrides its enabled flag and URLs.
    """
    merged: dict[str, tuple[Competitor, str]] = {}
    for c in builtin_competitors():
        merged[c.name.lower()] = (c, "suggested" if c.extra.get("suggested") and not c.enabled else "built-in")
    for o in overrides or []:
        key = o["name"].lower()
        if key in merged:
            base, source = merged[key]
            c = replace(base, enabled=bool(o.get("enabled", True)),
                        homepage=o.get("homepage") or base.homepage, careers_url=o.get("careers_url") or base.careers_url)
            merged[key] = (c, source if not c.enabled else "built-in" if source == "suggested" else source)
        else:
            merged[key] = (Competitor(name=o["name"], homepage=o.get("homepage", ""), careers_url=o.get("careers_url", ""),
                                      enabled=bool(o.get("enabled", True))), "added")
    return list(merged.values())


def effective_competitors(overrides: list[dict] | None = None) -> list[Competitor]:
    return [c for c, _ in merged_competitors(overrides) if c.enabled and (c.homepage or c.careers_url)]


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
