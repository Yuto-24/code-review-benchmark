"""Compare leaderboard rankings across different filter configurations.

Queries the Rust API with progressively stricter quality filters and
shows how bot rankings change.

Usage:
    python analysis/compare_leaderboards.py [--api-url http://localhost:3000]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any

_BASE = {"include_ignored": "true"}

FILTER_CONFIGS: list[tuple[str, dict[str, str]]] = [
    ("baseline", {**_BASE}),
    ("no_bot_author", {**_BASE, "exclude_bot_authored": "true"}),
    ("no_self_deal", {
        **_BASE,
        "exclude_bot_authored": "true",
        "exclude_self_authored": "true",
    }),
    ("engaged", {
        **_BASE,
        "exclude_bot_authored": "true",
        "exclude_self_authored": "true",
        "require_human_engagement": "true",
    }),
    ("capped", {
        **_BASE,
        "exclude_bot_authored": "true",
        "exclude_self_authored": "true",
        "require_human_engagement": "true",
        "max_author_repo_prs": "50",
    }),
    ("silver", {
        **_BASE,
        "exclude_bot_authored": "true",
        "exclude_self_authored": "true",
        "require_human_engagement": "true",
        "max_author_repo_prs": "50",
        "min_repo_contributors": "2",
    }),
    ("gold", {
        **_BASE,
        "exclude_bot_authored": "true",
        "exclude_self_authored": "true",
        "require_human_engagement": "true",
        "max_author_repo_prs": "50",
        "min_repo_contributors": "3",
        "min_human_reviewers": "1",
    }),
]


def _fetch_leaderboard(api_url: str, params: dict[str, str]) -> list[dict[str, Any]]:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{api_url}/api/leaderboard" + (f"?{qs}" if qs else "")
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["rows"]


def _top_n(rows: list[dict[str, Any]], n: int = 20) -> list[dict[str, Any]]:
    """Return top N by f_score descending, filtering bots with 0 scored PRs."""
    scored = [r for r in rows if r.get("scored_prs", 0) > 0 and r.get("f_score") is not None]
    scored.sort(key=lambda r: r["f_score"], reverse=True)
    return scored[:n]


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare leaderboard rankings across filters")
    parser.add_argument("--api-url", default="http://localhost:3000")
    parser.add_argument("--top", type=int, default=15, help="Show top N bots")
    parser.add_argument("--output", help="Write JSON results to file")
    args = parser.parse_args()

    results: dict[str, list[dict[str, Any]]] = {}

    for name, params in FILTER_CONFIGS:
        try:
            rows = _fetch_leaderboard(args.api_url, params)
            results[name] = rows
            top = _top_n(rows, args.top)
            print(f"\n{'='*80}")
            print(f"  {name.upper()} ({len(rows)} bots with data)")
            print(f"{'='*80}")
            print(f"  {'Rank':<5} {'Bot':<35} {'F1':>6} {'Prec':>6} {'Rec':>6} {'Scored':>8} {'Total':>8}")
            print(f"  {'-'*5} {'-'*35} {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*8}")
            for i, r in enumerate(top, 1):
                print(
                    f"  {i:<5} {r['chatbot']:<35} "
                    f"{r['f_score']:.3f} {r['precision']:.3f} {r['recall']:.3f} "
                    f"{r['scored_prs']:>8} {r['total_prs']:>8}"
                )
        except Exception as e:
            print(f"\n  ERROR fetching {name}: {e}", file=sys.stderr)
            continue

    # Ranking comparison table
    if len(results) >= 2:
        print(f"\n{'='*80}")
        print(f"  RANKING COMPARISON (top {args.top} from baseline)")
        print(f"{'='*80}")

        baseline_top = _top_n(results.get("baseline", []), args.top)
        bot_names = [r["chatbot"] for r in baseline_top]

        header = f"  {'Bot':<35}"
        for name, _ in FILTER_CONFIGS:
            if name in results:
                header += f" {name[:8]:>8}"
        print(header)
        print(f"  {'-'*35}" + "".join(f" {'-'*8}" for name, _ in FILTER_CONFIGS if name in results))

        for bot in bot_names:
            row = f"  {bot:<35}"
            for name, _ in FILTER_CONFIGS:
                if name not in results:
                    continue
                config_top = _top_n(results[name], 50)
                rank = next(
                    (i + 1 for i, r in enumerate(config_top) if r["chatbot"] == bot),
                    None,
                )
                row += f" {rank if rank else '-':>8}"
            print(row)

        # Score comparison
        print(f"\n  {'Bot':<35}", end="")
        for name, _ in FILTER_CONFIGS:
            if name in results:
                print(f" {name[:8]:>8}", end="")
        print(" (F1 scores)")
        print(f"  {'-'*35}" + "".join(f" {'-'*8}" for name, _ in FILTER_CONFIGS if name in results))

        for bot in bot_names:
            row = f"  {bot:<35}"
            for name, _ in FILTER_CONFIGS:
                if name not in results:
                    continue
                bot_row = next(
                    (r for r in results[name] if r["chatbot"] == bot),
                    None,
                )
                if bot_row and bot_row.get("f_score") is not None and bot_row.get("scored_prs", 0) > 0:
                    row += f" {bot_row['f_score']:.3f}  "
                else:
                    row += f" {'—':>8}"
            print(row)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nFull results written to {args.output}")


if __name__ == "__main__":
    main()
