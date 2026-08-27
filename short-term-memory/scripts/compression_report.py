"""Report compression effectiveness from a session's Redis summary envelope.

Usage:
    uv run python scripts/compression_report.py --user-id u-001 --session-id s-002

Prints, for every Headroom compression generation:
    from..through sequence, tokens_before -> tokens_after, ratio, saved%
And a session-level total.
"""

from __future__ import annotations

import argparse
import json
import os

import redis


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()

    client = redis.Redis.from_url(_redis_url(), decode_responses=True)
    key = f"dream:session:{args.user_id}:{args.session_id}:summary"
    raw = client.get(key)
    if not raw:
        print(f"没有找到 summary（{key}），可能还没触发压缩。")
        return

    envelope = json.loads(raw)
    generations = envelope.get("compression_generations", [])
    print(f"session={args.user_id}:{args.session_id}  version={envelope.get('version')}")
    print(f"compressed_through_sequence={envelope.get('compressed_through_sequence')}")
    print()
    print(f"{'generation':<10}{'seq range':<20}{'before':>8}{'after':>8}{'ratio':>8}{'saved%':>8}")
    print("-" * 62)

    total_before = 0
    total_after = 0
    for gen in generations:
        before = int(gen.get("tokens_before", 0))
        after = int(gen.get("tokens_after", 0))
        ratio = float(gen.get("compression_ratio", 1.0)) if before else 1.0
        saved = (1 - after / before) * 100 if before else 0.0
        seq_range = f"{gen.get('from_sequence')}..{gen.get('through_sequence')}"
        print(
            f"{gen.get('generation', '?'):<10}"
            f"{seq_range:<20}"
            f"{before:>8}{after:>8}{ratio:>8.2f}{saved:>7.1f}%"
        )
        total_before += before
        total_after += after

    print("-" * 62)
    if total_before:
        print(
            f"{'TOTAL':<10}{'':<20}{total_before:>8}{total_after:>8}"
            f"{total_after / total_before:>8.2f}{(1 - total_after / total_before) * 100:>7.1f}%"
        )

    # Look for CCR markers to confirm recall is armed.
    marker_count = 0
    for gen in generations:
        text = json.dumps(gen, ensure_ascii=False)
        if "hash=" in text or "ccr" in text.lower():
            marker_count += 1
    print(f"\n带 CCR marker 的 generation: {marker_count}/{len(generations)}")


if __name__ == "__main__":
    main()
