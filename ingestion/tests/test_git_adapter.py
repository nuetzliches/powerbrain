"""Tests for the Git adapter orchestration layer."""

from __future__ import annotations

import pytest

from ingestion.adapters.git_adapter import (
    RepoConfig,
    _detect_provider,
    _matches_patterns,
    _parse_owner_repo,
    _should_include,
)


class TestParseOwnerRepo:
    def test_github_https(self):
        assert _parse_owner_repo("https://github.com/org/repo") == ("org", "repo")

    def test_github_with_git_suffix(self):
        assert _parse_owner_repo("https://github.com/org/repo.git") == ("org", "repo")

    def test_github_with_trailing_slash(self):
        assert _parse_owner_repo("https://github.com/org/repo/") == ("org", "repo")

    def test_invalid_url(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_owner_repo("https://github.com/only-org")


class TestDetectProvider:
    def test_github(self):
        assert _detect_provider("https://github.com/org/repo") == "github"

    def test_unsupported(self):
        with pytest.raises(ValueError, match="Unsupported"):
            _detect_provider("https://gitlab.com/org/repo")


class TestPatternMatching:
    def test_matches_glob(self):
        assert _matches_patterns("docs/guide.md", ["docs/**"]) is True

    def test_matches_extension(self):
        assert _matches_patterns("README.md", ["*.md"]) is True

    def test_no_match(self):
        assert _matches_patterns("src/main.py", ["docs/**"]) is False

    def test_empty_patterns(self):
        assert _matches_patterns("anything.txt", []) is False


class TestShouldInclude:
    def test_default_skip_node_modules(self):
        assert _should_include("node_modules/foo.js", [], []) is False

    def test_default_skip_binary(self):
        assert _should_include("logo.png", [], []) is False

    def test_include_all_when_no_patterns(self):
        assert _should_include("src/main.py", [], []) is True

    def test_include_filter(self):
        assert _should_include("src/main.py", ["src/**"], []) is True
        assert _should_include("tests/test.py", ["src/**"], []) is False

    def test_exclude_filter(self):
        assert _should_include("drafts/wip.md", [], ["drafts/**"]) is False

    def test_include_and_exclude(self):
        # Included by include pattern but excluded by exclude
        assert _should_include("docs/draft.md", ["docs/**"], ["docs/draft*"]) is False

    def test_include_only_md(self):
        assert _should_include("README.md", ["*.md"], []) is True
        assert _should_include("main.py", ["*.md"], []) is False

    def test_lock_files_always_skipped(self):
        assert _should_include("package-lock.json", ["*"], []) is False


class TestRepoConfig:
    def test_defaults(self):
        cfg = RepoConfig(name="test", url="https://github.com/o/r")
        assert cfg.branch == "main"
        assert cfg.collection == "pb_general"
        assert cfg.classification == "internal"
        assert cfg.auth == "pat"
        assert cfg.include == []
        assert cfg.exclude == []
        assert cfg.poll_interval_seconds == 300
