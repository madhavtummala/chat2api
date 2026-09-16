"""Summarise prompt audits: ``python -m src.audit_report [path] [--top N]``.

The per-request report in the server log answers "what filled *this* prompt".
This answers the question you actually act on: across a whole session, which
categories keep showing up, and which single requests blew the limit.

Point it at the audit directory (default ``logs/audit``), at an ``audit.jsonl``,
or at one saved request body to re-derive that request's breakdown in full.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .api.audit import audit_request, format_entry, format_report


def _load(path: Path) -> list[dict]:
    jsonl = path / "audit.jsonl" if path.is_dir() else path
    if not jsonl.exists():
        sys.exit(f"No audit log at {jsonl}. Set CHAT2API_AUDIT_PROMPTS=true and retry.")
    with jsonl.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def summarise(records: list[dict], top: int, limit: int) -> str:
    if not records:
        return "No requests recorded yet."
    totals: dict[str, int] = {}
    for rec in records:
        for category, chars in rec["categories"].items():
            totals[category] = totals.get(category, 0) + chars
    grand = sum(totals.values()) or 1
    prompts = sorted(r["prompt_chars"] for r in records)
    over = [r for r in records if limit and r["prompt_chars"] > limit]

    lines = [
        f"{len(records)} requests · prompt chars: "
        f"min {prompts[0]:,} · median {prompts[len(prompts) // 2]:,} · max {prompts[-1]:,}",
    ]
    if limit:
        lines.append(f"{len(over)} over the {limit:,}-char limit")
    lines.append("")
    lines.append("Total characters by category (all requests):")
    for category, chars in sorted(totals.items(), key=lambda kv: -kv[1])[:top]:
        lines.append(f"  {chars:>10,}  {100 * chars / grand:5.1f}%  {category}")

    lines.append("")
    lines.append("Largest requests:")
    for rec in sorted(records, key=lambda r: -r["prompt_chars"])[:top]:
        biggest = max(rec["categories"].items(), key=lambda kv: kv[1], default=("-", 0))
        lines.append(
            f"  {rec['prompt_chars']:>10,}  {rec['request_id']}  {rec['path']}  "
            f"top: {biggest[0]} ({biggest[1]:,})"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="logs/audit", type=Path)
    parser.add_argument("--top", type=int, default=15, help="rows per table")
    parser.add_argument(
        "--limit", type=int, default=100_000, help="prompt char limit to flag (0=off)"
    )
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="with a body file, also print the composed prompt verbatim",
    )
    args = parser.parse_args(argv)

    # A saved body is a whole request, not a summary line: re-audit it so the
    # per-segment detail (which the JSONL keeps only in aggregate) is available.
    if args.path.is_file() and args.path.suffix == ".json":
        audit = audit_request(json.loads(args.path.read_text(encoding="utf-8")))
        render = format_entry if args.prompt else format_report
        print(render(audit, args.limit, args.top))
        return

    print(summarise(_load(args.path), args.top, args.limit))


if __name__ == "__main__":
    main()
