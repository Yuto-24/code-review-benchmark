"""Per-bot engagement signal breakdown.

Queries Postgres directly to show engagement distributions per bot,
and whether engagement correlates with scores differently across bots.

Usage:
    PYTHONPATH=. uv run python analysis/engagement_by_bot.py

Run from online/etl/ directory (needs config.py and db imports).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any


async def main() -> None:
    from config import DBConfig
    from db.connection import DBAdapter

    db = DBAdapter(DBConfig().database_url)
    await db.connect()

    rows = await db.fetchall("""
        SELECT c.github_username AS bot,
               p.engagement_signals,
               la.precision,
               la.recall
        FROM prs p
        JOIN llm_analyses la ON la.pr_id = p.id
        JOIN chatbots c ON la.chatbot_id = c.id
        WHERE p.engagement_signals IS NOT NULL
          AND p.status = 'analyzed'
          AND p.pr_merged = TRUE
    """)

    BotStats = dict[str, Any]

    bot_data: dict[str, BotStats] = defaultdict(lambda: {
        "total": 0,
        "engaged": 0,
        "has_reviewer": 0,
        "has_rounds": 0,
        "has_commits": 0,
        "scored_engaged": [],
        "scored_not_engaged": [],
    })

    for r in rows:
        bot = r["bot"]
        s = json.loads(r["engagement_signals"])
        d = bot_data[bot]
        d["total"] += 1

        eng = s["has_human_engagement"]
        if eng:
            d["engaged"] += 1
        if s["human_reviewer_count"] > 0:
            d["has_reviewer"] += 1
        if s["back_and_forth_rounds"] > 0:
            d["has_rounds"] += 1
        if s["commits_after_review"] > 0:
            d["has_commits"] += 1

        if r["precision"] is not None and r["recall"] is not None:
            p, rc = r["precision"], r["recall"]
            f1 = 2 * p * rc / (p + rc) if (p + rc) > 0 else 0
            if eng:
                d["scored_engaged"].append(f1)
            else:
                d["scored_not_engaged"].append(f1)

    await db.close()

    # Sort by total PRs descending, show top bots
    sorted_bots = sorted(bot_data.items(), key=lambda x: x[1]["total"], reverse=True)

    print(f"\n{'='*120}")
    print(f"  PER-BOT ENGAGEMENT BREAKDOWN (merged + analyzed PRs)")
    print(f"{'='*120}")
    print(
        f"  {'Bot':<35} {'Total':>7} {'Engaged%':>9} {'Reviewer%':>10} "
        f"{'Rounds%':>8} {'Commits%':>9} {'F1(eng)':>8} {'F1(no)':>8} {'Delta':>7}"
    )
    print(f"  {'-'*35} {'-'*7} {'-'*9} {'-'*10} {'-'*8} {'-'*9} {'-'*8} {'-'*8} {'-'*7}")

    for bot, d in sorted_bots[:30]:
        t = d["total"]
        eng_pct = 100 * d["engaged"] / t
        rev_pct = 100 * d["has_reviewer"] / t
        rnd_pct = 100 * d["has_rounds"] / t
        cmt_pct = 100 * d["has_commits"] / t

        f1_eng = sum(d["scored_engaged"]) / len(d["scored_engaged"]) if d["scored_engaged"] else None
        f1_no = sum(d["scored_not_engaged"]) / len(d["scored_not_engaged"]) if d["scored_not_engaged"] else None

        f1e_s = f"{f1_eng:.3f}" if f1_eng is not None else "—"
        f1n_s = f"{f1_no:.3f}" if f1_no is not None else "—"
        delta_s = f"{f1_eng - f1_no:+.3f}" if f1_eng is not None and f1_no is not None else "—"

        print(
            f"  {bot:<35} {t:>7} {eng_pct:>8.1f}% {rev_pct:>9.1f}% "
            f"{rnd_pct:>7.1f}% {cmt_pct:>8.1f}% {f1e_s:>8} {f1n_s:>8} {delta_s:>7}"
        )

    # Summary
    total_all = sum(d["total"] for d in bot_data.values())
    engaged_all = sum(d["engaged"] for d in bot_data.values())
    print(f"\n  Total PRs: {total_all}  |  Engaged: {engaged_all} ({100*engaged_all/total_all:.1f}%)")
    print(f"  Bots with data: {len(bot_data)}")


if __name__ == "__main__":
    asyncio.run(main())
