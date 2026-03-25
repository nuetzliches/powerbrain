"""
Shared configuration utilities for Powerbrain services.
Provides consistent Docker Secret reading and PostgreSQL URL construction.
"""

import logging
import os

log = logging.getLogger("pb-config")


def read_secret(env_var: str, default: str = "") -> str:
    """Read from Docker Secret file (*_FILE) if available, else fall back to env var.

    Convention: for ``FORGEJO_TOKEN`` the function checks ``FORGEJO_TOKEN_FILE``
    first.  If the file exists its contents (stripped) are returned; otherwise
    the plain ``FORGEJO_TOKEN`` env var is used.
    """
    file_path = os.getenv(f"{env_var}_FILE")
    if file_path:
        try:
            return open(file_path).read().strip()
        except FileNotFoundError:
            log.warning(
                "Secret file %s not found, falling back to env var %s",
                file_path,
                env_var,
            )
    return os.getenv(env_var, default)


def build_postgres_url() -> str:
    """Build a PostgreSQL connection URL.

    Resolution order:
    1. ``POSTGRES_URL`` env var (backward-compat, e.g. local dev or old compose)
    2. Individual ``POSTGRES_HOST/PORT/USER/DB`` + ``POSTGRES_PASSWORD`` via
       :func:`read_secret` (supports ``POSTGRES_PASSWORD_FILE`` Docker Secret).
    """
    url = os.getenv("POSTGRES_URL")
    if url:
        return url

    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "pb_admin")
    db = os.getenv("POSTGRES_DB", "powerbrain")
    password = read_secret("POSTGRES_PASSWORD", "changeme")

    return f"postgresql://{user}:{password}@{host}:{port}/{db}"
