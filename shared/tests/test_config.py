"""Tests for shared/config.py — read_secret and build_postgres_url."""

from shared.config import read_secret, build_postgres_url


class TestReadSecret:
    def test_reads_from_file(self, tmp_path, monkeypatch):
        secret_file = tmp_path / "my_secret.txt"
        secret_file.write_text("s3cret\n")
        monkeypatch.setenv("MY_VAR_FILE", str(secret_file))
        assert read_secret("MY_VAR") == "s3cret"

    def test_file_not_found_fallback(self, monkeypatch):
        monkeypatch.setenv("MY_VAR_FILE", "/nonexistent/path.txt")
        monkeypatch.setenv("MY_VAR", "from_env")
        assert read_secret("MY_VAR") == "from_env"

    def test_no_file_uses_env_var(self, monkeypatch):
        monkeypatch.delenv("MY_VAR_FILE", raising=False)
        monkeypatch.setenv("MY_VAR", "direct")
        assert read_secret("MY_VAR") == "direct"

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("MY_VAR_FILE", raising=False)
        monkeypatch.delenv("MY_VAR", raising=False)
        assert read_secret("MY_VAR", "fallback") == "fallback"

    def test_strips_whitespace(self, tmp_path, monkeypatch):
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("  token123  \n")
        monkeypatch.setenv("TOK_FILE", str(secret_file))
        assert read_secret("TOK") == "token123"


class TestBuildPostgresUrl:
    def test_explicit_postgres_url(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_URL", "postgresql://u:p@h:1/d")
        assert build_postgres_url() == "postgresql://u:p@h:1/d"

    def test_component_assembly(self, monkeypatch):
        monkeypatch.delenv("POSTGRES_URL", raising=False)
        monkeypatch.setenv("POSTGRES_HOST", "db.local")
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        monkeypatch.setenv("POSTGRES_USER", "myuser")
        monkeypatch.setenv("POSTGRES_DB", "mydb")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pw123")
        monkeypatch.delenv("POSTGRES_PASSWORD_FILE", raising=False)
        assert build_postgres_url() == "postgresql://myuser:pw123@db.local:5433/mydb"

    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("POSTGRES_URL", raising=False)
        monkeypatch.delenv("POSTGRES_HOST", raising=False)
        monkeypatch.delenv("POSTGRES_PORT", raising=False)
        monkeypatch.delenv("POSTGRES_USER", raising=False)
        monkeypatch.delenv("POSTGRES_DB", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD_FILE", raising=False)
        url = build_postgres_url()
        assert url == "postgresql://pb_admin:changeme@localhost:5432/powerbrain"
