"""GitHub REST API provider for the Git adapter.

Supports two authentication modes:
- PAT (Personal Access Token) — for individual users
- GitHub App (JWT → installation token) — for organizations

Uses the GitHub REST API v3. Rate-limit aware with automatic backoff.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("pb-github")

API_BASE = "https://api.github.com"

# File extensions considered "hard binary" — always skipped regardless of
# the adapter's `allow_documents` setting (images, archives, native binaries).
HARD_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".bmp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
    ".pyc", ".pyo", ".class", ".o", ".a",
    ".db", ".sqlite", ".sqlite3",
})

# Office document extensions — skipped by default for code repos, but can be
# opt-in via `allow_documents: true` in repos.yaml. Extraction runs through the
# shared ContentExtractor (markitdown + fallbacks).
DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".msg", ".eml", ".rtf",
})

# Back-compat alias — old callers (tests, external imports) see the legacy
# union of both sets, preserving the historical default of skipping documents.
BINARY_EXTENSIONS = HARD_BINARY_EXTENSIONS | DOCUMENT_EXTENSIONS

# Directories to always skip
SKIP_DIRS = frozenset({
    ".git", "node_modules", "vendor", "__pycache__", ".venv",
    "venv", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt",
})

# Files to always skip
SKIP_FILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "Gemfile.lock",
    "composer.lock", "cargo.lock", "go.sum",
})

# Content type mapping from file extension
CONTENT_TYPE_MAP: dict[str, str] = {
    ".md": "markdown", ".markdown": "markdown", ".rst": "restructuredtext",
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript",
    ".java": "java", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".scala": "scala", ".r": "r", ".R": "r",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml",
    ".xml": "xml", ".html": "html", ".css": "css", ".scss": "scss",
    ".sql": "sql", ".graphql": "graphql",
    ".dockerfile": "dockerfile", ".tf": "terraform", ".hcl": "hcl",
    ".rego": "rego", ".proto": "protobuf",
    ".txt": "text", ".csv": "csv", ".tsv": "tsv",
    ".env": "dotenv", ".ini": "ini", ".cfg": "ini",
}


def detect_content_type(path: str) -> str:
    """Detect content type from file path extension."""
    name = os.path.basename(path).lower()
    if name == "dockerfile":
        return "dockerfile"
    if name == "makefile":
        return "makefile"
    _, ext = os.path.splitext(name)
    return CONTENT_TYPE_MAP.get(ext, "text")


def detect_language(path: str) -> str | None:
    """Detect programming language from file extension, or None."""
    _, ext = os.path.splitext(path.lower())
    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".java": "java", ".go": "go", ".rs": "rust", ".rb": "ruby",
        ".c": "c", ".cpp": "cpp", ".cs": "csharp", ".php": "php",
        ".swift": "swift", ".kt": "kotlin", ".scala": "scala",
        ".sh": "shell", ".sql": "sql", ".r": "r",
    }
    return lang_map.get(ext)


def should_skip_path(path: str, allow_documents: bool = False) -> bool:
    """Check if a path should be skipped based on default patterns.

    When ``allow_documents`` is True, Office-document extensions
    (``DOCUMENT_EXTENSIONS``) are **not** considered skip-worthy — the caller
    is expected to fetch them as bytes and run them through the shared
    ContentExtractor. Hard-binary extensions remain blocked unconditionally.
    """
    parts = path.split("/")

    # Skip directories
    for part in parts[:-1]:
        if part in SKIP_DIRS:
            return True

    # Skip by filename
    filename = parts[-1]
    if filename in SKIP_FILES:
        return True

    # Skip hard-binary extensions unconditionally
    _, ext = os.path.splitext(filename.lower())
    if ext in HARD_BINARY_EXTENSIONS:
        return True

    # Skip document extensions unless opt-in
    if ext in DOCUMENT_EXTENSIONS and not allow_documents:
        return True

    return False


def is_document_path(path: str) -> bool:
    """Return True if the file is an Office/PDF document eligible for extraction."""
    _, ext = os.path.splitext(path.lower())
    return ext in DOCUMENT_EXTENSIONS


@dataclass
class TreeEntry:
    """A file entry from the Git tree API."""
    path: str
    sha: str
    size: int


@dataclass
class CompareEntry:
    """A file entry from the compare API."""
    path: str
    status: str  # "added", "modified", "removed", "renamed"
    previous_path: str | None = None


class PATAuth:
    """Authentication via Personal Access Token."""

    def __init__(self, token: str):
        self._token = token

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


class GitHubAppAuth:
    """Authentication via GitHub App (JWT → installation token).

    Requires pyjwt[crypto] for RS256 JWT creation.
    """

    def __init__(self, app_id: int, private_key: str, installation_id: int):
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires_at: float = 0

    async def get_token(self, client: httpx.AsyncClient) -> str:
        """Return a valid installation token, refreshing if needed."""
        if self._token and time.time() < self._expires_at - 300:
            return self._token

        try:
            import jwt
        except ImportError:
            raise ImportError(
                "pyjwt[crypto] is required for GitHub App auth. "
                "Install with: pip install 'pyjwt[crypto]'"
            )

        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 600, "iss": self._app_id}
        jwt_token = jwt.encode(payload, self._private_key, algorithm="RS256")

        resp = await client.post(
            f"{API_BASE}/app/installations/{self._installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["token"]
        # GitHub installation tokens expire in 1 hour
        self._expires_at = time.time() + 3600
        return self._token

    def headers(self) -> dict[str, str]:
        if not self._token:
            raise RuntimeError("Call get_token() before headers()")
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


class GitHubProvider:
    """GitHub REST API client for repository content access."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        owner: str,
        repo: str,
        auth: PATAuth | GitHubAppAuth,
    ):
        self.client = client
        self.owner = owner
        self.repo = repo
        self.auth = auth

    async def _headers(self) -> dict[str, str]:
        if isinstance(self.auth, GitHubAppAuth):
            await self.auth.get_token(self.client)
        return self.auth.headers()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an authenticated request with rate-limit handling."""
        headers = await self._headers()
        url = f"{API_BASE}{path}"

        for attempt in range(3):
            resp = await self.client.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 429 or (
                resp.status_code == 403
                and "rate limit" in resp.text.lower()
            ):
                retry_after = int(resp.headers.get("Retry-After", "60"))
                wait = min(retry_after, 120)
                log.warning("Rate limited, waiting %ds (attempt %d)", wait, attempt + 1)
                await asyncio.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        raise httpx.HTTPStatusError(
            "Rate limit exceeded after retries",
            request=resp.request,
            response=resp,
        )

    async def get_branch_sha(self, branch: str = "main") -> str:
        """Get the latest commit SHA for a branch."""
        resp = await self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/commits/{branch}",
        )
        return resp.json()["sha"]

    async def get_tree(self, sha: str) -> list[TreeEntry]:
        """Get the full recursive tree for a commit SHA. Returns only blobs (files)."""
        resp = await self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/git/trees/{sha}",
            params={"recursive": "1"},
        )
        data = resp.json()
        return [
            TreeEntry(path=item["path"], sha=item["sha"], size=item.get("size", 0))
            for item in data.get("tree", [])
            if item["type"] == "blob"
        ]

    async def get_compare(self, base_sha: str, head_sha: str) -> list[CompareEntry]:
        """Compare two commits and return changed files."""
        resp = await self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/compare/{base_sha}...{head_sha}",
        )
        data = resp.json()
        return [
            CompareEntry(
                path=f["filename"],
                status=f["status"],
                previous_path=f.get("previous_filename"),
            )
            for f in data.get("files", [])
        ]

    async def get_file_content(self, path: str, ref: str) -> str | None:
        """Fetch file content decoded as UTF-8. Returns None for binary/errors."""
        try:
            resp = await self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/contents/{path}",
                params={"ref": ref},
            )
            data = resp.json()

            if data.get("encoding") == "base64":
                raw = base64.b64decode(data["content"])
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    log.debug("Skipping binary file: %s", path)
                    return None

            # Some files are returned directly as string
            return data.get("content")

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.debug("File not found: %s@%s", path, ref)
                return None
            raise
        except Exception:
            log.warning("Failed to fetch %s@%s", path, ref, exc_info=True)
            return None

    async def get_file_bytes(self, path: str, ref: str) -> bytes | None:
        """Fetch the raw file bytes for binary-aware consumers (e.g. document
        extraction via markitdown). Returns ``None`` on 404 or fetch error.
        """
        try:
            resp = await self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/contents/{path}",
                params={"ref": ref},
            )
            data = resp.json()
            if data.get("encoding") == "base64":
                return base64.b64decode(data["content"])
            # Non-base64 variants (e.g. submodule symlinks) are not usable here
            return None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.debug("File not found: %s@%s", path, ref)
                return None
            raise
        except Exception:
            log.warning("Failed to fetch bytes for %s@%s", path, ref, exc_info=True)
            return None
