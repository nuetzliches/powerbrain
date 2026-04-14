"""Tests for the GitHub REST API provider."""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from ingestion.adapters.providers.github import (
    BINARY_EXTENSIONS,
    CONTENT_TYPE_MAP,
    GitHubProvider,
    PATAuth,
    TreeEntry,
    detect_content_type,
    detect_language,
    should_skip_path,
)


# ── Pattern / utility tests ────────────────────────────────


class TestSkipPatterns:
    def test_skip_node_modules(self):
        assert should_skip_path("node_modules/foo/bar.js") is True

    def test_skip_pycache(self):
        assert should_skip_path("src/__pycache__/module.cpython-312.pyc") is True

    def test_skip_binary_png(self):
        assert should_skip_path("assets/logo.png") is True

    def test_skip_lock_file(self):
        assert should_skip_path("package-lock.json") is True

    def test_allow_python_file(self):
        assert should_skip_path("src/main.py") is False

    def test_allow_markdown(self):
        assert should_skip_path("docs/README.md") is False

    def test_allow_yaml(self):
        assert should_skip_path("config/settings.yaml") is False

    def test_skip_vendor_dir(self):
        assert should_skip_path("vendor/lib/something.go") is True

    def test_skip_dotgit(self):
        assert should_skip_path(".git/HEAD") is True

    def test_skip_venv(self):
        assert should_skip_path(".venv/lib/python3.12/site.py") is True


class TestContentTypeDetection:
    def test_python(self):
        assert detect_content_type("src/main.py") == "python"

    def test_markdown(self):
        assert detect_content_type("README.md") == "markdown"

    def test_yaml(self):
        assert detect_content_type("config.yaml") == "yaml"

    def test_dockerfile(self):
        assert detect_content_type("Dockerfile") == "dockerfile"

    def test_makefile(self):
        assert detect_content_type("Makefile") == "makefile"

    def test_unknown(self):
        assert detect_content_type("something.xyz") == "text"

    def test_typescript(self):
        assert detect_content_type("app.tsx") == "typescript"

    def test_rego(self):
        assert detect_content_type("policy.rego") == "rego"


class TestLanguageDetection:
    def test_python(self):
        assert detect_language("main.py") == "python"

    def test_go(self):
        assert detect_language("main.go") == "go"

    def test_markdown_no_language(self):
        assert detect_language("README.md") is None

    def test_yaml_no_language(self):
        assert detect_language("config.yaml") is None


# ── GitHub API tests (mocked) ──────────────────────────────


@pytest.fixture
def pat_auth():
    return PATAuth("ghp_test_token_12345")


@pytest.fixture
def github_provider(pat_auth):
    client = httpx.AsyncClient()
    return GitHubProvider(client, "test-org", "test-repo", pat_auth)


class TestPATAuth:
    def test_headers(self, pat_auth):
        h = pat_auth.headers()
        assert h["Authorization"] == "Bearer ghp_test_token_12345"
        assert "github" in h["Accept"]


@respx.mock
async def test_get_branch_sha(github_provider):
    respx.get("https://api.github.com/repos/test-org/test-repo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "abc123def456"})
    )
    sha = await github_provider.get_branch_sha("main")
    assert sha == "abc123def456"


@respx.mock
async def test_get_tree(github_provider):
    respx.get(
        "https://api.github.com/repos/test-org/test-repo/git/trees/abc123",
        params={"recursive": "1"},
    ).mock(
        return_value=httpx.Response(200, json={
            "tree": [
                {"path": "README.md", "type": "blob", "sha": "sha1", "size": 100},
                {"path": "src/main.py", "type": "blob", "sha": "sha2", "size": 200},
                {"path": "src", "type": "tree", "sha": "sha3"},  # directory, should be excluded
            ]
        })
    )
    entries = await github_provider.get_tree("abc123")
    assert len(entries) == 2
    assert entries[0].path == "README.md"
    assert entries[1].path == "src/main.py"


@respx.mock
async def test_get_compare(github_provider):
    respx.get(
        "https://api.github.com/repos/test-org/test-repo/compare/aaa...bbb"
    ).mock(
        return_value=httpx.Response(200, json={
            "files": [
                {"filename": "new.py", "status": "added"},
                {"filename": "changed.py", "status": "modified"},
                {"filename": "old.py", "status": "removed"},
                {"filename": "renamed.py", "status": "renamed", "previous_filename": "old_name.py"},
            ]
        })
    )
    entries = await github_provider.get_compare("aaa", "bbb")
    assert len(entries) == 4
    assert entries[0].status == "added"
    assert entries[2].status == "removed"
    assert entries[3].previous_path == "old_name.py"


@respx.mock
async def test_get_file_content(github_provider):
    content_b64 = base64.b64encode(b"# Hello World\n").decode()
    respx.get(
        "https://api.github.com/repos/test-org/test-repo/contents/README.md",
        params={"ref": "abc123"},
    ).mock(
        return_value=httpx.Response(200, json={
            "encoding": "base64",
            "content": content_b64,
        })
    )
    content = await github_provider.get_file_content("README.md", "abc123")
    assert content == "# Hello World\n"


@respx.mock
async def test_get_file_content_binary_returns_none(github_provider):
    # Binary content that can't be decoded as UTF-8
    content_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00").decode()
    respx.get(
        "https://api.github.com/repos/test-org/test-repo/contents/image.png",
        params={"ref": "abc123"},
    ).mock(
        return_value=httpx.Response(200, json={
            "encoding": "base64",
            "content": content_b64,
        })
    )
    content = await github_provider.get_file_content("image.png", "abc123")
    assert content is None


@respx.mock
async def test_get_file_content_404_returns_none(github_provider):
    respx.get(
        "https://api.github.com/repos/test-org/test-repo/contents/missing.py",
        params={"ref": "abc123"},
    ).mock(return_value=httpx.Response(404))
    content = await github_provider.get_file_content("missing.py", "abc123")
    assert content is None


@respx.mock
async def test_rate_limit_retry(github_provider):
    route = respx.get("https://api.github.com/repos/test-org/test-repo/commits/main")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"sha": "finally_got_it"}),
    ]
    sha = await github_provider.get_branch_sha("main")
    assert sha == "finally_got_it"
    assert route.call_count == 2
