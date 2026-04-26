#!/usr/bin/env python3
"""Benchmark harness — markitdown vs. Docling on a corpus of documents.

Supports the ADR in `docs/technology-decisions.md` (T-6, B-52). Runs
each extractor on every supported file in a directory and prints a
table of per-file character counts and wall-clock latencies.

Usage:

    pip install markitdown docling
    python3 scripts/benchmark_extractors.py [PATH]            # default: testdata/documents/
    python3 scripts/benchmark_extractors.py --json [PATH]     # machine-readable output
    python3 scripts/benchmark_extractors.py --skip-docling    # markitdown-only smoke

Design notes:
- The script lives outside the production codepath; it is **not**
  imported by any service. Both extractors are imported lazily so a
  missing install only fails the test that needs it (e.g. omitting
  Docling lets you collect markitdown numbers in isolation).
- We measure only end-to-end latency (`extractor.convert()` wall-clock).
  Memory and image-size are out of scope — `docker images` and
  `/usr/bin/time` cover those better than this script.
- The harness reads files from disk and feeds bytes; the I/O is the
  same for both backends, so the diff is the extractor itself.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

SUPPORTED_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".md", ".txt", ".csv", ".rtf",
}


@dataclass
class Result:
    file: str
    backend: str
    bytes_in: int
    chars_out: int
    duration_ms: float
    error: str | None = None


def _list_corpus(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_SUFFIXES else []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        and not p.name.startswith(".")
    )


def _run_markitdown(path: Path) -> Result:
    from markitdown import MarkItDown

    raw = path.read_bytes()
    md = MarkItDown()
    t0 = time.perf_counter()
    try:
        out = md.convert_stream(io.BytesIO(raw), file_extension=path.suffix)
        text = out.text_content if hasattr(out, "text_content") else str(out)
    except Exception as exc:
        return Result(
            file=str(path), backend="markitdown",
            bytes_in=len(raw), chars_out=0,
            duration_ms=(time.perf_counter() - t0) * 1000,
            error=type(exc).__name__ + ": " + str(exc)[:160],
        )
    return Result(
        file=str(path), backend="markitdown",
        bytes_in=len(raw), chars_out=len(text),
        duration_ms=(time.perf_counter() - t0) * 1000,
    )


def _run_docling(path: Path) -> Result:
    from docling.document_converter import DocumentConverter

    raw = path.read_bytes()
    converter = DocumentConverter()
    t0 = time.perf_counter()
    try:
        result = converter.convert(str(path))
        text = result.document.export_to_markdown()
    except Exception as exc:
        return Result(
            file=str(path), backend="docling",
            bytes_in=len(raw), chars_out=0,
            duration_ms=(time.perf_counter() - t0) * 1000,
            error=type(exc).__name__ + ": " + str(exc)[:160],
        )
    return Result(
        file=str(path), backend="docling",
        bytes_in=len(raw), chars_out=len(text),
        duration_ms=(time.perf_counter() - t0) * 1000,
    )


def _format_table(results: list[Result]) -> str:
    by_file: dict[str, dict[str, Result]] = {}
    for r in results:
        by_file.setdefault(r.file, {})[r.backend] = r
    backends = sorted({r.backend for r in results})

    header = ["file", "size"] + [f"{b}_chars" for b in backends] + [
        f"{b}_ms" for b in backends
    ]
    rows = [header]
    for file_path, per_backend in sorted(by_file.items()):
        row = [Path(file_path).name]
        any_r = next(iter(per_backend.values()))
        row.append(_human_size(any_r.bytes_in))
        for b in backends:
            r = per_backend.get(b)
            row.append(str(r.chars_out) if r and r.error is None else "ERR")
        for b in backends:
            r = per_backend.get(b)
            row.append(f"{r.duration_ms:.0f}" if r else "-")
        rows.append(row)

    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for i, row in enumerate(rows):
        line = "  ".join(c.ljust(widths[idx]) for idx, c in enumerate(row))
        lines.append(line)
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))

    errors = [r for r in results if r.error]
    if errors:
        lines.append("")
        lines.append("Errors:")
        for r in errors:
            lines.append(f"  {Path(r.file).name} [{r.backend}] → {r.error}")

    return "\n".join(lines)


def _human_size(n: int) -> str:
    for unit in ("B", "K", "M"):
        if n < 1024 or unit == "M":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n/1024:.1f}{unit}"
        n /= 1024
    return f"{n:.0f}M"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "path", nargs="?", default="testdata/documents/",
        help="Directory or single file to benchmark.",
    )
    parser.add_argument(
        "--skip-markitdown", action="store_true",
        help="Run Docling only (useful when comparing across runs).",
    )
    parser.add_argument(
        "--skip-docling", action="store_true",
        help="Run markitdown only (useful when Docling isn't installed).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit one JSON object per result line instead of the table.",
    )
    args = parser.parse_args()

    root = Path(args.path)
    if not root.exists():
        print(f"path does not exist: {root}", file=sys.stderr)
        return 2

    corpus = _list_corpus(root)
    if not corpus:
        print(f"No supported files under {root}", file=sys.stderr)
        return 1

    results: list[Result] = []
    for path in corpus:
        if not args.skip_markitdown:
            results.append(_run_markitdown(path))
        if not args.skip_docling:
            results.append(_run_docling(path))

    if args.json:
        for r in results:
            print(json.dumps(asdict(r)))
    else:
        print(_format_table(results))

    return 0


if __name__ == "__main__":
    sys.exit(main())
