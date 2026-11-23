"""Security regression tests for safe-by-default Hub configuration."""

from pathlib import Path

from a2a_hub.adapters.cli_agents import ClaudeCodeAdapter
from a2a_hub.config import AgentSpec, Settings


def test_network_and_auth_defaults_are_safe(monkeypatch):
    for name in (
        "A2A_HOST",
        "A2A_REQUIRE_AUTH",
        "A2A_API_TOKEN",
        "A2A_CORS_ALLOWED_ORIGINS",
        "A2A_CORS_ALLOW_CREDENTIALS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)
    assert settings.host == "127.0.0.1"
    assert settings.require_auth is True
    assert settings.cors_allowed_origins == ""
    assert settings.cors_allow_credentials is False


def test_claude_adapter_does_not_bypass_permissions_by_default():
    adapter = ClaudeCodeAdapter(AgentSpec(id="claude-test", name="Claude", type="claude_code"))
    assert "--permission-mode default" in adapter.command
    assert "bypassPermissions" not in adapter.command


def test_repository_claude_agent_config_does_not_bypass_permissions():
    config = Path(__file__).resolve().parents[1] / "config" / "agents.yaml"
    content = config.read_text(encoding="utf-8")
    assert "bypassPermissions" not in content
