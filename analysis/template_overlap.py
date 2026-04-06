"""Cross-reference template/scripted flags with engagement and contributor filters.

Key question: does applying Silver-tier filters already eliminate most
template/scripted pairs, making a separate blocklist unnecessary?

Usage (from online/etl/):
    PYTHONPATH=. uv run python ../../analysis/template_overlap.py --report ../../quality_report_full.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from typing import Any


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, help="Path to quality_report_full.json")
    args = parser.parse_args()

    with open(args.report) as f:
        report = json.load(f)

    from config import DBConfig
    from db.connection import DBAdapter
    from pipeline.quality import is_bot_username

    db = DBAdapter(DBConfig().database_url)
    await db.connect()

    # Load repo contributor counts and engagement stats
    print("Loading repo contributor counts...")
    contrib_rows = await db.fetchall("""
        SELECT repo_name, COUNT(DISTINCT pr_author) AS n_authors
        FROM prs
        WHERE pr_author IS NOT NULL AND pr_author != ''
        GROUP BY repo_name
    """)
    repo_contribs: dict[str, int] = {r["repo_name"]: r["n_authors"] for r in contrib_rows}

    # Load engagement stats per (repo, bot) pair
    print("Loading per-pair engagement stats...")
    pair_rows = await db.fetchall("""
        SELECT p.repo_name, c.github_username AS bot,
               COUNT(*) AS total_prs,
               SUM(CASE WHEN p.pr_merged = TRUE THEN 1 ELSE 0 END) AS merged_prs,
               SUM(CASE WHEN p.pr_author IS NOT NULL AND NOT (
                   LOWER(p.pr_author) LIKE '%[bot]' OR LOWER(p.pr_author) IN (
                       'dependabot','renovate','github-actions','copilot'
                   )
               ) THEN 1 ELSE 0 END) AS human_authored
        FROM prs p
        JOIN chatbots c ON c.id = p.chatbot_id
        WHERE p.status = 'analyzed'
        GROUP BY p.repo_name, c.github_username
    """)
    pair_stats: dict[tuple[str, str], dict[str, int]] = {}
    for r in pair_rows:
        pair_stats[(r["repo_name"], r["bot"])] = {
            "total_prs": r["total_prs"],
            "merged_prs": r["merged_prs"] or 0,
            "human_authored": r["human_authored"] or 0,
        }

    await db.close()

    # Analyze each flagged pair
    flagged_pairs = report.get("flagged_pairs", [])
    all_pairs = report.get("all_pairs", [])

    print(f"\nTotal pairs in report: {len(all_pairs)}")
    print(f"Flagged pairs: {len(flagged_pairs)}")

    # For each flag type, check how many would be caught by existing filters
    flag_types = ["scripted", "template_prefix", "no_human_comments",
                  "low_diversity", "user_template", "user_scripted", "mostly_short"]

    # Group flagged pairs by flag
    pairs_by_flag: dict[str, list[dict]] = defaultdict(list)
    for pair in flagged_pairs:
        for flag in pair.get("flags", []):
            pairs_by_flag[flag].append(pair)

    print(f"\n{'='*100}")
    print(f"  TEMPLATE FLAG OVERLAP WITH QUALITY FILTERS")
    print(f"{'='*100}")
    print(
        f"  {'Flag':<25} {'Total':>6} {'Solo repo':>10} {'<2 ctrb':>8} "
        f"{'<3 ctrb':>8} {'Bot auth':>9} {'Caught by':>10}"
    )
    print(f"  {'-'*25} {'-'*6} {'-'*10} {'-'*8} {'-'*8} {'-'*9} {'-'*10}")

    for flag in flag_types:
        pairs = pairs_by_flag.get(flag, [])
        if not pairs:
            continue

        n = len(pairs)
        solo = 0
        lt2 = 0
        lt3 = 0
        bot_auth = 0

        for p in pairs:
            repo = p.get("repo", "")
            contribs = repo_contribs.get(repo, 1)

            if contribs <= 1:
                solo += 1
            if contribs < 2:
                lt2 += 1
            if contribs < 3:
                lt3 += 1

            # Check if most PRs in this pair are bot-authored
            stats = pair_stats.get((repo, p.get("bot", "")), {})
            if stats.get("human_authored", 0) == 0:
                bot_auth += 1

        # "Caught by" = would be eliminated by Silver-tier filters
        # (solo repo OR bot-authored)
        caught = sum(
            1 for p in pairs
            if repo_contribs.get(p.get("repo", ""), 1) < 2
            or pair_stats.get((p.get("repo", ""), p.get("bot", "")), {}).get("human_authored", 0) == 0
        )

        print(
            f"  {flag:<25} {n:>6} {solo:>9} ({100*solo/n:.0f}%) "
            f"{lt2:>5} ({100*lt2/n:.0f}%) {lt3:>5} ({100*lt3/n:.0f}%) "
            f"{bot_auth:>6} ({100*bot_auth/n:.0f}%) {caught:>7} ({100*caught/n:.0f}%)"
        )

    # Summary: how many unique flagged pairs are NOT caught by Silver filters?
    print(f"\n{'='*100}")
    print(f"  PAIRS NOT CAUGHT BY SILVER-TIER FILTERS")
    print(f"{'='*100}")

    uncaught: list[dict] = []
    for pair in flagged_pairs:
        repo = pair.get("repo", "")
        bot = pair.get("bot", "")
        contribs = repo_contribs.get(repo, 1)
        stats = pair_stats.get((repo, bot), {})
        human_authored = stats.get("human_authored", 0)

        if contribs >= 2 and human_authored > 0:
            uncaught.append({**pair, "_contribs": contribs, "_human_authored": human_authored})

    print(f"\n  Flagged pairs: {len(flagged_pairs)}")
    print(f"  Caught by min_repo_contributors=2 OR bot-authored: {len(flagged_pairs) - len(uncaught)}")
    print(f"  NOT caught (need blocklist): {len(uncaught)}")

    if uncaught:
        print(f"\n  {'Repo':<45} {'Bot':<30} {'Flags':<30} {'Contribs':>8} {'PRs':>6}")
        print(f"  {'-'*45} {'-'*30} {'-'*30} {'-'*8} {'-'*6}")
        uncaught.sort(key=lambda x: x.get("pr_count", 0), reverse=True)
        for p in uncaught[:30]:
            flags_str = ", ".join(p.get("flags", []))[:29]
            print(
                f"  {p.get('repo', '')[:44]:<45} {p.get('bot', '')[:29]:<30} "
                f"{flags_str:<30} {p.get('_contribs', 0):>8} {p.get('pr_count', 0):>6}"
            )


if __name__ == "__main__":
    asyncio.run(main())
