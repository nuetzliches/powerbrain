"""
Tests for provider key configuration loading and provider extraction.
"""
import tempfile
import os
import pytest
import yaml
from config import load_provider_key_config
from proxy import _extract_provider


class TestLoadProviderKeyConfig:
    """Tests for load_provider_key_config() function."""
    
    def test_loads_valid_config(self):
        """Should load provider_keys section from YAML."""
        config_data = {
            "provider_keys": {
                "anthropic": "central",
                "openai": "hybrid", 
                "github": "user"
            }
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.safe_dump(config_data, f)
            f.flush()
            
            result = load_provider_key_config(f.name)
            
        os.unlink(f.name)
        
        expected = {
            "anthropic": "central",
            "openai": "hybrid",
            "github": "user"
        }
        assert result == expected
    
    def test_missing_file_returns_empty_dict(self):
        """Should return empty dict when config file doesn't exist."""
        result = load_provider_key_config("/nonexistent/path.yaml")
        assert result == {}
    
    def test_invalid_key_source_falls_back_to_central(self):
        """Should use 'central' for invalid key_source values."""
        config_data = {
            "provider_keys": {
                "anthropic": "invalid_source",
                "openai": "hybrid",
                "github": "another_invalid"
            }
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.safe_dump(config_data, f)
            f.flush()
            
            result = load_provider_key_config(f.name)
            
        os.unlink(f.name)
        
        expected = {
            "anthropic": "central",  # invalid_source → central
            "openai": "hybrid",      # valid
            "github": "central"      # another_invalid → central
        }
        assert result == expected
    
    def test_no_provider_keys_section_returns_empty_dict(self):
        """Should return empty dict when provider_keys section is missing."""
        config_data = {
            "other_section": {
                "key": "value"
            }
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.safe_dump(config_data, f)
            f.flush()
            
            result = load_provider_key_config(f.name)
            
        os.unlink(f.name)
        
        assert result == {}
    
    def test_empty_provider_keys_section_returns_empty_dict(self):
        """Should return empty dict when provider_keys section is None or empty."""
        config_data = {
            "provider_keys": None
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.safe_dump(config_data, f)
            f.flush()
            
            result = load_provider_key_config(f.name)
            
        os.unlink(f.name)
        
        assert result == {}


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