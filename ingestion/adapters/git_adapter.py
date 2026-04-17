"""Git adapter — orchestrates repository content fetching across providers.

Resolves the provider from the repo URL (github.com → GitHubProvider),
applies include/exclude filters, and produces NormalizedDocument instances.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from ingestion.adapters.base import FileChange, NormalizedDocument, SourceAdapter
from ingestion.adapters.providers.github import (
    GitHubAppAuth,
    GitHubProvider,
    PATAuth,
    detect_content_type,
    detect_language,
    is_document_path,
    should_skip_path,
)

log = logging.getLogger("pb-git-adapter")


@dataclass
class RepoConfig:
    """Configuration for a single repository to sync."""

    name: str
    url: str
    branch: str = "main"
    collection: str = "pb_general"
    project: str = ""
    classification: str = "internal"
    auth: str = "pat"  # "pat" or "github-app"
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    poll_interval_seconds: int = 300
    # When true, Office documents (PDF/DOCX/XLSX/PPTX/MSG/...) are fetched as
    # bytes and extracted via the shared ContentExtractor. Default off: code
    # repos normally should not ingest docs.
    allow_documents: bool = False
    # GitHub App fields (only when auth="github-app")
    app_id: int | None = None
    installation_id: int | None = None
    private_key_path: str | None = None


def _parse_owner_repo(url: str) -> tuple[str, str]:
    """Extract owner and repo from a GitHub URL."""
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse owner/repo from URL: {url}")
    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    return owner, repo


def _detect_provider(url: str) -> str:
    """Detect the Git provider from URL hostname."""
    host = urlparse(url).hostname or ""
    if "github.com" in host:
        return "github"
    # Future: gitlab.com → gitlab, bitbucket.org → bitbucket
    raise ValueError(f"Unsupported Git provider for URL: {url}")


def _matches_patterns(path: str, patterns: list[str]) -> bool:
    """Check if a path matches any of the glob patterns."""
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def _should_include(
    path: str,
    include: list[str],
    exclude: list[str],
    allow_documents: bool = False,
) -> bool:
    """Determine if a file path should be included based on config patterns."""
    # Default skip patterns always apply (hard binaries + optionally docs)
    if should_skip_path(path, allow_documents=allow_documents):
        return False

    # If exclude patterns match, skip
    if exclude and _matches_patterns(path, exclude):
        return False

    # If include patterns are set, file must match at least one
    if include and not _matches_patterns(path, include):
        return False

    return True


class GitAdapter(SourceAdapter):
    """Adapter that fetches repository content from Git providers."""

    def __init__(self, config: RepoConfig, client: httpx.AsyncClient):
        self.config = config
        self._provider_type = _detect_provider(config.url)
        owner, repo = _parse_owner_repo(config.url)

        if self._provider_type == "github":
            auth = self._create_github_auth(config)
            self._provider = GitHubProvider(client, owner, repo, auth)
        else:
            raise ValueError(f"Unsupported provider: {self._provider_type}")

        self._owner = owner
        self._repo = repo

    @staticmethod
    def _create_github_auth(config: RepoConfig) -> PATAuth | GitHubAppAuth:
        """Create the appropriate auth object."""
        if config.auth == "github-app":
            if not all([config.app_id, config.installation_id, config.private_key_path]):
                raise ValueError(
                    "GitHub App auth requires app_id, installation_id, and private_key_path"
                )
            with open(config.private_key_path) as f:
                private_key = f.read()
            return GitHubAppAuth(config.app_id, private_key, config.installation_id)

        # Default: PAT auth
        from shared.config import _read_secret
        token = _read_secret("GITHUB_PAT", "")
        if not token:
            raise ValueError(
                "GitHub PAT not configured. Set GITHUB_PAT env var or "
                "create secrets/github_pat.txt"
            )
        return PATAuth(token)

    def _make_document(
        self,
        path: str,
        content: str,
        sha: str,
        source_type: str = "github",
        content_type: str | None = None,
    ) -> NormalizedDocument:
        """Create a NormalizedDocument from file content."""
        return NormalizedDocument(
            content=content,
            content_type=content_type or detect_content_type(path),
            source_ref=f"github:{self._owner}/{self._repo}:{path}@{sha}",
            source_type=source_type,
            language=detect_language(path),
            metadata={
                "repo_url": self.config.url,
                "repo_name": self.config.name,
                "file_path": path,
                "commit_sha": sha,
                "branch": self.config.branch,
                "owner": self._owner,
                "repo": self._repo,
            },
        )

    async def _fetch_document_text(self, path: str, sha: str) -> tuple[str | None, str]:
        """Fetch a document as bytes and extract its text via ContentExtractor.

        Returns ``(text, extractor)`` where ``extractor`` is the backend used.
        Returns ``(None, ...)`` on fetch or extraction failure.
        """
        raw = await self._provider.get_file_bytes(path, sha)
        if raw is None:
            return None, "fetch_failed"

        # Lazy-import to keep GitAdapter importable without markitdown installed
        from content_extraction import ContentExtractor  # noqa: WPS433
        extractor = ContentExtractor()
        try:
            text, backend = extractor.extract_from_bytes_detailed(raw, path)
        except Exception:
            log.warning("Document extraction failed for %s", path, exc_info=True)
            return None, "failed"
        return text, backend

    async def get_current_sha(self) -> str:
        return await self._provider.get_branch_sha(self.config.branch)

    async def fetch_all_files(self) -> list[NormalizedDocument]:
        """Fetch all files from the repo (initial sync)."""
        sha = await self.get_current_sha()
        tree = await self._provider.get_tree(sha)

        docs: list[NormalizedDocument] = []
        included = [
            e for e in tree
            if _should_include(
                e.path, self.config.include, self.config.exclude,
                allow_documents=self.config.allow_documents,
            )
        ]

        log.info(
            "Initial sync %s/%s: %d files after filtering (%d total in tree)",
            self._owner, self._repo, len(included), len(tree),
        )

        for entry in included:
            doc = await self._fetch_as_document(entry.path, sha)
            if doc is not None:
                docs.append(doc)

        return docs

    async def fetch_changed_files(self, since_sha: str) -> list[NormalizedDocument]:
        """Fetch only added/modified files since a commit SHA."""
        current_sha = await self.get_current_sha()
        changes = await self._provider.get_compare(since_sha, current_sha)

        docs: list[NormalizedDocument] = []
        for change in changes:
            if change.status in ("added", "modified", "renamed"):
                if not _should_include(
                    change.path, self.config.include, self.config.exclude,
                    allow_documents=self.config.allow_documents,
                ):
                    continue
                doc = await self._fetch_as_document(change.path, current_sha)
                if doc is not None:
                    docs.append(doc)

        return docs

    async def _fetch_as_document(self, path: str, sha: str) -> NormalizedDocument | None:
        """Fetch a single file and turn it into a NormalizedDocument.

        Text files go through ``get_file_content`` as before. Office documents
        (PDF/DOCX/XLSX/...) are fetched as bytes and extracted via the shared
        ContentExtractor when ``allow_documents`` is enabled.
        """
        if self.config.allow_documents and is_document_path(path):
            text, extractor = await self._fetch_document_text(path, sha)
            if text is None or not text.strip():
                log.info(
                    "Skipping document %s@%s (no extractable text, backend=%s)",
                    path, sha, extractor,
                )
                return None
            return self._make_document(
                path, text, sha,
                source_type="github-document",
                content_type="markdown",
            )

        content = await self._provider.get_file_content(path, sha)
        if content is None:
            return None
        return self._make_document(path, content, sha)

    async def get_file_changes(self, since_sha: str) -> list[FileChange]:
        """Get all file changes (add/modify/remove) since a SHA."""
        current_sha = await self.get_current_sha()
        compare_entries = await self._provider.get_compare(since_sha, current_sha)

        return [
            FileChange(
                path=e.path,
                status=e.status,
                previous_path=e.previous_path,
            )
            for e in compare_entries
        ]
