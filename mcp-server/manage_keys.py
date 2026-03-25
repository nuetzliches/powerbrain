"""
API-Key Management CLI for the Powerbrain MCP Server.

Usage:
    python manage_keys.py create --agent-id my-agent --role analyst [--description "..."] [--expires-in-days 90]
    python manage_keys.py list
    python manage_keys.py revoke --agent-id my-agent
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.config import build_postgres_url

POSTGRES_URL = build_postgres_url()
KEY_PREFIX = "pb_"
KEY_BYTES = 32  # 32 bytes = 64 hex chars


def generate_key() -> str:
    """Generate a new API key with pb_ prefix."""
    return KEY_PREFIX + secrets.token_hex(KEY_BYTES)


def hash_key(key: str) -> str:
    """SHA-256 hash of the API key."""
    return hashlib.sha256(key.encode()).hexdigest()


async def cmd_create(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        key = generate_key()
        key_h = hash_key(key)
        expires = None
        if args.expires_in_days is not None:
            expires = datetime.now(timezone.utc) + timedelta(days=args.expires_in_days)

        await conn.execute(
            """
            INSERT INTO api_keys (key_hash, agent_id, agent_role, description, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            key_h, args.agent_id, args.role, args.description, expires,
        )
        print(f"API key created for agent '{args.agent_id}' (role: {args.role})")
        print(f"Key: {key}")
        print()
        print("Store this key securely — it cannot be retrieved again.")
        if expires:
            print(f"Expires: {expires.isoformat()}")
    except asyncpg.UniqueViolationError:
        print(f"Error: agent_id '{args.agent_id}' already exists.", file=sys.stderr)
        sys.exit(1)
    finally:
        await conn.close()


async def cmd_list(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT agent_id, agent_role, description, active,
                   created_at, expires_at, last_used_at
            FROM api_keys ORDER BY created_at
            """
        )
        if not rows:
            print("No API keys found.")
            return

        fmt = "{:<20} {:<10} {:<6} {:<20} {:<20} {:<20} {}"
        print(fmt.format("AGENT_ID", "ROLE", "ACTIVE", "CREATED", "EXPIRES", "LAST_USED", "DESCRIPTION"))
        print("-" * 120)
        for r in rows:
            print(fmt.format(
                r["agent_id"],
                r["agent_role"],
                str(r["active"]),
                r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "-",
                r["expires_at"].strftime("%Y-%m-%d %H:%M") if r["expires_at"] else "never",
                r["last_used_at"].strftime("%Y-%m-%d %H:%M") if r["last_used_at"] else "never",
                r["description"] or "",
            ))
    finally:
        await conn.close()


async def cmd_revoke(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        result = await conn.execute(
            "UPDATE api_keys SET active = false WHERE agent_id = $1 AND active = true",
            args.agent_id,
        )
        if result == "UPDATE 1":
            print(f"API key for agent '{args.agent_id}' revoked.")
        else:
            print(f"No active key found for agent '{args.agent_id}'.", file=sys.stderr)
            sys.exit(1)
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Powerbrain MCP Server API Key Management")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a new API key")
    create.add_argument("--agent-id", required=True, help="Unique agent identifier")
    create.add_argument("--role", required=True, choices=["analyst", "developer", "admin"])
    create.add_argument("--description", default=None, help="What this key is for")
    create.add_argument("--expires-in-days", type=int, default=None)

    sub.add_parser("list", help="List all API keys")

    revoke = sub.add_parser("revoke", help="Revoke an API key")
    revoke.add_argument("--agent-id", required=True, help="Agent to revoke")

    args = parser.parse_args()

    coro = {"create": cmd_create, "list": cmd_list, "revoke": cmd_revoke}[args.command]
    asyncio.run(coro(args))


if __name__ == "__main__":
    main()
