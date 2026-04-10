"""Tests for pb-proxy/config.py — _read_secret and load_provider_key_config."""

import yaml

from config import _read_secret, load_provider_key_config


class TestReadSecret:
    def test_reads_from_file(self, tmp_path, monkeypatch):
        f = tmp_path / "secret.txt"
        f.write_text("my_token\n")
        monkeypatch.setenv("TOKEN_FILE", str(f))
        assert _read_secret("TOKEN") == "my_token"

    def test_fallback_to_env(self, monkeypatch):
        monkeypatch.setenv("TOKEN_FILE", "/no/such/file.txt")
        monkeypatch.setenv("TOKEN", "env_value")
        assert _read_secret("TOKEN") == "env_value"

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("TOKEN_FILE", raising=False)
        monkeypatch.delenv("TOKEN", raising=False)
        assert _read_secret("TOKEN", "default") == "default"


class TestLoadProviderKeyConfig:
    def test_loads_valid_config(self, tmp_path):
        cfg = {"provider_keys": {"anthropic": "central", "openai": "user"}}
        f = tmp_path / "config.yaml"
        f.write_text(yaml.dump(cfg))
        result = load_provider_key_config(str(f))
        assert result == {"anthropic": "central", "openai": "user"}

    def test_file_not_found_returns_empty(self):
        assert load_provider_key_config("/nonexistent/config.yaml") == {}

    def test_invalid_key_source_defaults_to_central(self, tmp_path):
        cfg = {"provider_keys": {"anthropic": "invalid_mode"}}
        f = tmp_path / "config.yaml"
        f.write_text(yaml.dump(cfg))
        result = load_provider_key_config(str(f))
        assert result["anthropic"] == "central"

    def test_empty_provider_keys(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text(yaml.dump({"provider_keys": {}}))
        assert load_provider_key_config(str(f)) == {}

    def test_empty_yaml(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("")
        assert load_provider_key_config(str(f)) == {}

    def test_no_provider_keys_section(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text(yaml.dump({"model_list": []}))
        assert load_provider_key_config(str(f)) == {}
