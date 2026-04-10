"""Base abstractions for source adapters.

All adapters produce NormalizedDocument instances that feed into the
standard ingestion pipeline (PII scan → OPA → quality gate → embedding).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class NormalizedDocument:
    """A single document normalized from any source adapter."""

    content: str
    content_type: str  # e.g. "markdown", "python", "yaml", "text"
    source_ref: str  # unique reference URI (e.g. "github:org/repo:path@sha")
    source_type: str  # e.g. "github", "gitlab", "file"
    language: str | None = None  # programming language if applicable
    metadata: dict = field(default_factory=dict)


@dataclass
class FileChange:
    """A single file change from a diff/compare operation."""

    path: str
    status: str  # "added", "modified", "removed", "renamed"
    previous_path: str | None = None  # for renames


class SourceAdapter(ABC):
    """Abstract base for source adapters."""

    @abstractmethod
    async def get_current_sha(self) -> str:
        """Return the current HEAD SHA of the configured branch."""

    @abstractmethod
    async def fetch_all_files(self) -> list[NormalizedDocument]:
        """Fetch all files (initial sync). Applies include/exclude filters."""

    @abstractmethod
    async def fetch_changed_files(self, since_sha: str) -> list[NormalizedDocument]:
        """Fetch only files changed since the given SHA (incremental sync)."""

    @abstractmethod
    async def get_file_changes(self, since_sha: str) -> list[FileChange]:
        """Return the list of file changes (add/modify/remove) since a SHA."""
