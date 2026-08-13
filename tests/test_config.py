"""Phase 0 smoke tests: settings load, role ordering, offline fallback."""

from __future__ import annotations

import pytest

from nl2api.config import Role, Settings


class TestRoleOrdering:
    """Privilege is cumulative, and only ``implies`` encodes that."""

    def test_billing_admin_satisfies_every_role(self) -> None:
        admin = Role.BILLING_ADMIN
        assert admin.implies(Role.VIEWER)
        assert admin.implies(Role.SUPPORT_AGENT)
        assert admin.implies(Role.BILLING_ADMIN)

    def test_viewer_cannot_write(self) -> None:
        viewer = Role.VIEWER
        assert viewer.implies(Role.VIEWER)
        assert not viewer.implies(Role.SUPPORT_AGENT)
        assert not viewer.implies(Role.BILLING_ADMIN)

    def test_support_agent_is_not_a_billing_admin(self) -> None:
        assert Role.SUPPORT_AGENT.implies(Role.VIEWER)
        assert not Role.SUPPORT_AGENT.implies(Role.BILLING_ADMIN)


class TestSettings:
    def test_defaults_are_usable_without_any_environment(self) -> None:
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.model == "claude-haiku-4-5"
        assert s.default_role is Role.SUPPORT_AGENT
        assert s.max_write_steps == 3
        assert s.database_url.startswith("sqlite:///")

    def test_service_urls_lose_trailing_slashes(self) -> None:
        s = Settings(mock_api_url="http://localhost:8000/", _env_file=None)  # type: ignore[call-arg]
        assert s.mock_api_url == "http://localhost:8000"

    def test_log_level_is_normalised(self) -> None:
        s = Settings(log_level="debug", _env_file=None)  # type: ignore[call-arg]
        assert s.log_level == "DEBUG"

    def test_bad_log_level_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="log_level"):
            Settings(log_level="chatty", _env_file=None)  # type: ignore[call-arg]

    def test_max_write_steps_must_be_at_least_one(self) -> None:
        with pytest.raises(ValueError):
            Settings(max_write_steps=0, _env_file=None)  # type: ignore[call-arg]


class TestOfflineFallback:
    """Without a key we must never claim we can call a provider."""

    def test_no_key_means_no_credentials(self) -> None:
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.has_llm_credentials is False

    def test_blank_key_is_treated_as_absent(self) -> None:
        s = Settings(ANTHROPIC_API_KEY="   ", _env_file=None)  # type: ignore[call-arg]
        assert s.has_llm_credentials is False

    def test_anthropic_downgrades_to_rules_without_a_key(self) -> None:
        s = Settings(llm_provider="anthropic", _env_file=None)  # type: ignore[call-arg]
        assert s.effective_llm_provider == "rules"

    def test_explicit_cassette_provider_is_respected(self) -> None:
        s = Settings(llm_provider="cassette", _env_file=None)  # type: ignore[call-arg]
        assert s.effective_llm_provider == "cassette"

    def test_key_present_keeps_anthropic(self) -> None:
        s = Settings(ANTHROPIC_API_KEY="sk-ant-test", _env_file=None)  # type: ignore[call-arg]
        assert s.has_llm_credentials is True
        assert s.effective_llm_provider == "anthropic"

    def test_api_key_never_prints_in_the_clear(self) -> None:
        s = Settings(ANTHROPIC_API_KEY="sk-ant-supersecret", _env_file=None)  # type: ignore[call-arg]
        assert "supersecret" not in repr(s)
