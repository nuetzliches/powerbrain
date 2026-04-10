"""
Tests for provider key resolution, extraction, and integration.

Note: load_provider_key_config() tests are in test_proxy_config.py.
This file covers _extract_provider, _resolve_provider_key, and integration.
"""
import pytest
from proxy import _extract_provider


class TestExtractProvider:
    """Tests for _extract_provider() helper function."""
    
    def test_extracts_provider_from_provider_model_format(self):
        """Should extract provider from 'provider/model' format."""
        assert _extract_provider("anthropic/claude-opus-4-20250514") == "anthropic"
        assert _extract_provider("openai/gpt-4o-mini") == "openai"
        assert _extract_provider("github/copilot-chat") == "github"
    
    def test_returns_none_for_alias_no_slash(self):
        """Should return None for alias format (no slash)."""
        assert _extract_provider("gpt-4o") is None
        assert _extract_provider("claude-opus") is None
        assert _extract_provider("gemini-pro") is None
    
    def test_handles_multiple_slashes(self):
        """Should extract first part before slash when multiple slashes exist."""
        assert _extract_provider("anthropic/claude/v1") == "anthropic"
        assert _extract_provider("openai/models/gpt-4") == "openai"
    
    def test_handles_empty_string(self):
        """Should return None for empty string."""
        assert _extract_provider("") is None
    
    def test_handles_slash_only(self):
        """Should return empty string for slash-only input."""
        assert _extract_provider("/") == ""
        assert _extract_provider("/model") == ""


class TestResolveProviderKey:
    """Tests for _resolve_provider_key() provider key resolution logic."""
    
    def test_central_key_source_uses_env_key(self, monkeypatch):
        """Central mode should use PROVIDER_KEY_MAP when key is available."""
        # Mock config module and proxy module imports
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test123")
        
        from proxy import _resolve_provider_key
        import config
        
        # Mock PROVIDER_KEY_MAP
        monkeypatch.setattr(config, "PROVIDER_KEY_MAP", {"anthropic": "sk-ant-test123"})
        
        provider_key_config = {"anthropic": "central"}
        acompletion, extra_kwargs = _resolve_provider_key(
            model="anthropic/claude-opus-4-20250514",
            provider_key_header=None,
            provider_key_config=provider_key_config
        )
        
        assert extra_kwargs["api_key"] == "sk-ant-test123"
    
    def test_central_key_source_no_key_returns_401(self):
        """Central mode without env key should raise HTTPException with 401."""
        from proxy import _resolve_provider_key
        from fastapi import HTTPException
        
        provider_key_config = {"anthropic": "central"}
        
        with pytest.raises(HTTPException) as exc_info:
            _resolve_provider_key(
                model="anthropic/claude-opus-4-20250514",
                provider_key_header=None,
                provider_key_config=provider_key_config
            )
        
        assert exc_info.value.status_code == 401
        assert "No API key configured for provider 'anthropic'" in exc_info.value.detail
        assert "Configure ANTHROPIC_API_KEY" in exc_info.value.detail
    
    def test_user_key_source_uses_header(self):
        """User mode should use X-Provider-Key header when provided."""
        from proxy import _resolve_provider_key
        
        provider_key_config = {"anthropic": "user"}
        acompletion, extra_kwargs = _resolve_provider_key(
            model="anthropic/claude-opus-4-20250514",
            provider_key_header="sk-ant-user123",
            provider_key_config=provider_key_config
        )
        
        assert extra_kwargs["api_key"] == "sk-ant-user123"
    
    def test_user_key_source_no_header_returns_401(self):
        """User mode without header should raise HTTPException with 401."""
        from proxy import _resolve_provider_key
        from fastapi import HTTPException
        
        provider_key_config = {"anthropic": "user"}
        
        with pytest.raises(HTTPException) as exc_info:
            _resolve_provider_key(
                model="anthropic/claude-opus-4-20250514",
                provider_key_header=None,
                provider_key_config=provider_key_config
            )
        
        assert exc_info.value.status_code == 401
        assert "Provider 'anthropic' requires X-Provider-Key header" in exc_info.value.detail
    
    def test_hybrid_prefers_header_over_central(self, monkeypatch):
        """Hybrid mode should prefer X-Provider-Key over env key when both available."""
        from proxy import _resolve_provider_key
        import config
        
        # Mock PROVIDER_KEY_MAP
        monkeypatch.setattr(config, "PROVIDER_KEY_MAP", {"anthropic": "sk-ant-central123"})
        
        provider_key_config = {"anthropic": "hybrid"}
        acompletion, extra_kwargs = _resolve_provider_key(
            model="anthropic/claude-opus-4-20250514",
            provider_key_header="sk-ant-user123",
            provider_key_config=provider_key_config
        )
        
        assert extra_kwargs["api_key"] == "sk-ant-user123"
    
    def test_hybrid_falls_back_to_central(self, monkeypatch):
        """Hybrid mode should fall back to env key when no header provided."""
        from proxy import _resolve_provider_key
        import config
        
        # Mock PROVIDER_KEY_MAP
        monkeypatch.setattr(config, "PROVIDER_KEY_MAP", {"anthropic": "sk-ant-central123"})
        
        provider_key_config = {"anthropic": "hybrid"}
        acompletion, extra_kwargs = _resolve_provider_key(
            model="anthropic/claude-opus-4-20250514",
            provider_key_header=None,
            provider_key_config=provider_key_config
        )
        
        assert extra_kwargs["api_key"] == "sk-ant-central123"
    
    def test_hybrid_no_key_anywhere_returns_401(self):
        """Hybrid mode with neither header nor env key should raise HTTPException."""
        from proxy import _resolve_provider_key
        from fastapi import HTTPException
        
        provider_key_config = {"anthropic": "hybrid"}
        
        with pytest.raises(HTTPException) as exc_info:
            _resolve_provider_key(
                model="anthropic/claude-opus-4-20250514",
                provider_key_header=None,
                provider_key_config=provider_key_config
            )
        
        assert exc_info.value.status_code == 401
        assert "No API key available for provider 'anthropic'" in exc_info.value.detail
        assert "Supply X-Provider-Key header or configure ANTHROPIC_API_KEY" in exc_info.value.detail
    
    def test_default_is_central(self, monkeypatch):
        """Unconfigured provider should default to central behavior."""
        from proxy import _resolve_provider_key
        import config
        
        # Mock PROVIDER_KEY_MAP
        monkeypatch.setattr(config, "PROVIDER_KEY_MAP", {"anthropic": "sk-ant-test123"})
        
        # No config for anthropic provider (should default to central)
        provider_key_config = {}
        acompletion, extra_kwargs = _resolve_provider_key(
            model="anthropic/claude-opus-4-20250514",
            provider_key_header="sk-ant-user123",  # Header provided but should be ignored
            provider_key_config=provider_key_config
        )
        
        assert extra_kwargs["api_key"] == "sk-ant-test123"
    
    def test_aliases_unaffected(self, monkeypatch):
        """Aliases should still route through Router regardless of provider key config."""
        from proxy import _resolve_provider_key
        
        # Mock known_aliases
        from proxy import known_aliases
        monkeypatch.setattr("proxy.known_aliases", {"gpt-4o", "claude-opus"})
        
        provider_key_config = {"openai": "user"}  # This should not affect aliases
        
        acompletion, extra_kwargs = _resolve_provider_key(
            model="gpt-4o",  # Known alias
            provider_key_header=None,
            provider_key_config=provider_key_config
        )
        
        # Should return empty extra_kwargs for aliases (Router handles the key)
        assert "api_key" not in extra_kwargs


class TestProviderKeyIntegration:
    """Integration tests for X-Provider-Key header processing."""
    
    def test_x_provider_key_header_reaches_resolver(self, monkeypatch):
        """X-Provider-Key header value should be used by resolver in user mode."""
        from proxy import _resolve_provider_key
        
        # Mock known_aliases as empty (force passthrough routing)
        monkeypatch.setattr("proxy.known_aliases", set())
        
        provider_key_config = {"anthropic": "user"}
        provider_key_header = "sk-ant-user123"
        
        acompletion, extra_kwargs = _resolve_provider_key(
            model="anthropic/claude-opus-4-20250514",
            provider_key_header=provider_key_header,
            provider_key_config=provider_key_config,
        )
        
        assert extra_kwargs["api_key"] == "sk-ant-user123"