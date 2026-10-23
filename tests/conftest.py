import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobwatch import fetch  # noqa: E402


class FakeWeb:
    """Maps URL -> HTML/JSON so scrapers run without the network."""

    def __init__(self):
        self.pages: dict[str, object] = {}
        self.requested: list[str] = []

    def get(self, url, respect_robots=True, **kw):
        self.requested.append(url)
        body = self.pages.get(url)
        if body is None:
            return None
        return SimpleNamespace(text=body if isinstance(body, str) else "", url=url, ok=True, status_code=200)

    def get_json(self, url, **kw):
        self.requested.append(url)
        body = self.pages.get(url)
        return body if not isinstance(body, str) else None


@pytest.fixture
def web(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(fetch, "get", w.get)
    monkeypatch.setattr(fetch, "get_json", w.get_json)
    return w


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for var in ("TABLE_NAME", "EMAIL_FROM", "EMAIL_TO", "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_PARAM",
                "DASHBOARD_PASSWORD", "DASHBOARD_PASSWORD_PARAM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JOBWATCH_DATA", str(tmp_path / "jobs.json"))
