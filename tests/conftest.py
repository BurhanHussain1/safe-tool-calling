"""Shared test fixtures.

The suite must run with no network access and no API key — every fixture here
keeps that invariant.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nl2api.config import Settings, get_settings

CASSETTE_DIR = Path(__file__).parent / "cassettes"
GOLDEN_DIR = Path(__file__).parent / "golden"


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stop tests from picking up a developer's local ``.env`` or API key."""
    for var in ("ANTHROPIC_API_KEY", "NL2API_LLM_PROVIDER", "NL2API_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Offline settings pointing at a throwaway database."""
    return Settings(
        llm_provider="cassette",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        _env_file=None,  # type: ignore[call-arg]
    )
