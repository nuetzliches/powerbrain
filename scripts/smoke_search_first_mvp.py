#!/usr/bin/env python3
"""Run the Search-first MVP smoke verification against the Docker stack."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO_QUERY = "Search-first MVP smoke test document"
DEMO_DOC_ID = "search-first-mvp-doc"
REQUIRED_SERVICES = {"postgres", "qdrant", "opa", "ollama", "mcp-server"}


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def ensure_seed_data() -> None:
    completed = run(["python3", "scripts/seed_demo_search_data.py"])
    sys.stdout.write(completed.stdout)


def get_mcp_server_image() -> str:
    completed = run(["docker", "compose", "images", "-q", "mcp-server"])
    image = completed.stdout.strip()
    if not image:
        raise RuntimeError("No built mcp-server image found. Run `docker compose build mcp-server` first.")
    return image


def call_search() -> None:
    client_code = f"""
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

QUERY = {DEMO_QUERY!r}
DOC_ID = {DEMO_DOC_ID!r}
URL = 'http://127.0.0.1:8080/mcp'

async def main():
    async with streamable_http_client(URL) as streams:
        read_stream, write_stream = streams[0], streams[1]
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert any(tool.name == 'search_knowledge' for tool in tools.tools), tools.tools

            result = await session.call_tool(
                'search_knowledge',
                arguments={{
                    'query': QUERY,
                    'collection': 'pb_general',
                    'top_k': 3,
                    'agent_id': 'smoke-test',
                    'agent_role': 'analyst',
                }},
            )
            payload = json.loads(result.content[0].text)
            assert payload['total'] >= 1, payload
            assert any(item['id'] == DOC_ID for item in payload['results']), payload
            print(json.dumps(payload, indent=2, ensure_ascii=False))

asyncio.run(main())
"""

    image = get_mcp_server_image()
    completed = run([
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        image,
        "python",
        "-c",
        client_code,
    ])
    sys.stdout.write(completed.stdout)


def check_services() -> None:
    completed = run(["docker", "compose", "ps", "--services", "--status", "running"])
    sys.stdout.write(completed.stdout)
    running_services = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    missing = REQUIRED_SERVICES - running_services
    if missing:
        raise RuntimeError(f"Missing running services: {', '.join(sorted(missing))}")


def run_fallback_check() -> None:
    run(["docker", "compose", "stop", "reranker"])
    try:
        call_search()
    finally:
        run(["docker", "compose", "start", "reranker"], check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-reranker-fallback", action="store_true")
    args = parser.parse_args()

    try:
        check_services()
        ensure_seed_data()
        if args.check_reranker_fallback:
            run_fallback_check()
        else:
            call_search()
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            sys.stdout.write(exc.stdout)
        if exc.stderr:
            sys.stderr.write(exc.stderr)
        return exc.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
