"""Exploratory script for template/scripted response detection.

Run on the VM to understand human comment patterns before building the blocklist.
Read-only — does not modify any data.

Usage:
    cd ~/crb/online/etl
    uv run python scripts/explore_template_responses.py --limit 50
    uv run python scripts/explore_template_responses.py --min-prs 30 --show-comments
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict

from config import DBConfig
from db.connection import DBAdapter

COMMENT_EVENT_TYPES = frozenset({"review", "review_comment", "issue_comment"})

_KNOWN_BOTS = frozenset({
    "dependabot", "renovate", "github-actions", "codecov",
    "mergify", "snyk-bot", "greenkeeper", "imgbot",
    "stale", "allcontributors", "semantic-release-bot",
})


def is_bot_username(username: str) -> bool:
    lower = username.lower()
    return lower.endswith("[bot]") or lower in _KNOWN_BOTS


def _normalize(text: str) -> str:
    """Normalize comment text for comparison: lowercase, strip whitespace/punctuation."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _extract_human_comments_after_bot(
    events: list[dict],
    chatbot_username: str,
) -> list[str]:
    """Extract human comment bodies posted after the bot's first review activity."""
    bot_lower = chatbot_username.lower()

    # Find bot's first review/comment timestamp
    bot_first_ts: str | None = None
    for e in events:
        actor = (e.get("actor") or "").lower()
        if actor == bot_lower and e.get("event_type") in COMMENT_EVENT_TYPES:
            bot_first_ts = e.get("timestamp")
            break

    if bot_first_ts is None:
        return []

    comments: list[str] = []
    for e in events:
        ts = e.get("timestamp", "")
        if ts <= bot_first_ts:
            continue
        actor = e.get("actor") or ""
        if not actor or actor.lower() == bot_lower:
            continue
        if is_bot_username(actor):
            continue
        etype = e.get("event_type", "")
        if etype in COMMENT_EVENT_TYPES:
            body = (e.get("data") or {}).get("body") or ""
            body = body.strip()
            if body:
                comments.append(body)

    return comments


async def run(args: argparse.Namespace) -> None:
    cfg = DBConfig()
    db = DBAdapter(cfg.database_url)
    await db.connect()

    try:
        # Find (repo, author) pairs with many PRs
        min_prs = args.min_prs
        limit = args.limit

        bot_filter = args.bot
        bot_label = f" for bot={bot_filter}" if bot_filter else ""
        print(f"Finding (repo_name, pr_author) pairs with >= {min_prs} analyzed PRs{bot_label}...\n")

        bot_clause = ""
        if bot_filter:
            bot_clause = f"AND c.github_username = '{bot_filter}'"

        pairs = await db.fetchall(f"""
            SELECT p.repo_name, p.pr_author, c.github_username AS chatbot,
                   COUNT(*) AS pr_count
            FROM prs p
            JOIN chatbots c ON c.id = p.chatbot_id
            WHERE p.status = 'analyzed'
              AND p.assembled IS NOT NULL
              AND p.pr_author IS NOT NULL
              AND p.pr_author != ''
              AND p.pr_merged = TRUE
              {bot_clause}
            GROUP BY p.repo_name, p.pr_author, c.github_username
            HAVING COUNT(*) >= {min_prs}
            ORDER BY COUNT(*) DESC
            LIMIT {limit}
        """)

        print(f"Found {len(pairs)} pairs with >= {min_prs} PRs\n")
        if not pairs:
            return

        print(f"{'Repo':<45} {'Author':<25} {'Bot':<30} {'PRs':>5}")
        print("-" * 110)
        for p in pairs:
            print(f"{p['repo_name']:<45} {p['pr_author']:<25} {p['chatbot']:<30} {p['pr_count']:>5}")

        print(f"\n{'='*110}")
        print("COMMENT DIVERSITY ANALYSIS")
        print(f"{'='*110}\n")

        results: list[dict] = []

        for i, pair in enumerate(pairs):
            repo = pair["repo_name"]
            author = pair["pr_author"]
            chatbot = pair["chatbot"]
            pr_count = pair["pr_count"]

            # Fetch assembled timelines for this pair
            rows = await db.fetchall(*db._translate_params(
                """
                SELECT p.assembled
                FROM prs p
                JOIN chatbots c ON c.id = p.chatbot_id
                WHERE p.repo_name = $1
                  AND p.pr_author = $2
                  AND c.github_username = $3
                  AND p.status = 'analyzed'
                  AND p.assembled IS NOT NULL
                  AND p.pr_merged = TRUE
                ORDER BY p.id
                LIMIT 200
                """,
                (repo, author, chatbot),
            ))

            all_comments: list[str] = []
            prs_with_comments = 0
            for row in rows:
                assembled = json.loads(row["assembled"])
                comments = _extract_human_comments_after_bot(
                    assembled.get("events", []), chatbot
                )
                if comments:
                    prs_with_comments += 1
                all_comments.extend(comments)

            if not all_comments:
                results.append({
                    "repo": repo, "author": author, "chatbot": chatbot,
                    "pr_count": pr_count, "total_comments": 0,
                    "unique_ratio": 0, "short_ratio": 0,
                    "prs_with_comments": 0,
                })
                continue

            # Normalize and compute metrics
            normalized = [_normalize(c) for c in all_comments]
            unique_normalized = set(normalized)
            total = len(normalized)
            unique_count = len(unique_normalized)
            unique_ratio = unique_count / total if total > 0 else 0

            # Short comments (under 50 chars raw)
            short_count = sum(1 for c in all_comments if len(c) < 50)
            short_ratio = short_count / total if total > 0 else 0

            # Most common comments
            counter = Counter(normalized)
            top_comments = counter.most_common(5)

            results.append({
                "repo": repo, "author": author, "chatbot": chatbot,
                "pr_count": pr_count, "total_comments": total,
                "unique_count": unique_count, "unique_ratio": unique_ratio,
                "short_count": short_count, "short_ratio": short_ratio,
                "prs_with_comments": prs_with_comments,
                "top_comments": top_comments,
            })

            # Print per-pair summary
            flag = ""
            if unique_ratio < 0.2 and total >= 5:
                flag = " *** LIKELY SCRIPTED ***"
            elif unique_ratio < 0.4 and total >= 5:
                flag = " * LOW DIVERSITY *"
            elif short_ratio > 0.8 and total >= 5:
                flag = " * MOSTLY SHORT *"

            print(f"[{i+1}/{len(pairs)}] {repo} | {author} | {chatbot}")
            print(f"  PRs: {pr_count} | with human comments: {prs_with_comments}")
            print(f"  Comments: {total} total, {unique_count} unique "
                  f"(diversity: {unique_ratio:.2f}){flag}")
            print(f"  Short (<50 chars): {short_count}/{total} ({short_ratio:.1%})")

            if args.show_comments and top_comments:
                print(f"  Top comments:")
                for comment, count in top_comments:
                    preview = comment[:80] + "..." if len(comment) > 80 else comment
                    print(f"    [{count}x] \"{preview}\"")
            print()

        # Summary table
        print(f"\n{'='*110}")
        print("SUMMARY")
        print(f"{'='*110}\n")

        print(f"{'Repo':<40} {'Author':<20} {'Bot':<25} {'PRs':>5} "
              f"{'Cmts':>5} {'Uniq':>5} {'Div%':>6} {'Short%':>7} {'Flag':<20}")
        print("-" * 140)

        for r in sorted(results, key=lambda x: x["unique_ratio"]):
            flag = ""
            if r["total_comments"] == 0:
                flag = "NO COMMENTS"
            elif r["unique_ratio"] < 0.2 and r["total_comments"] >= 5:
                flag = "SCRIPTED"
            elif r["unique_ratio"] < 0.4 and r["total_comments"] >= 5:
                flag = "LOW DIVERSITY"
            elif r["short_ratio"] > 0.8 and r["total_comments"] >= 5:
                flag = "MOSTLY SHORT"

            print(f"{r['repo']:<40} {r['author']:<20} {r['chatbot']:<25} "
                  f"{r['pr_count']:>5} {r['total_comments']:>5} "
                  f"{r.get('unique_count', 0):>5} {r['unique_ratio']:>5.1%} "
                  f"{r['short_ratio']:>6.1%} {flag:<20}")

        # Overall stats
        scripted = sum(1 for r in results
                       if r["unique_ratio"] < 0.2 and r["total_comments"] >= 5)
        low_div = sum(1 for r in results
                      if 0.2 <= r["unique_ratio"] < 0.4 and r["total_comments"] >= 5)
        no_comments = sum(1 for r in results if r["total_comments"] == 0)

        print(f"\nTotal pairs analyzed: {len(results)}")
        print(f"  Likely scripted (diversity < 0.2): {scripted}")
        print(f"  Low diversity (0.2-0.4): {low_div}")
        print(f"  No human comments at all: {no_comments}")
        print(f"  Normal (>= 0.4 diversity): {len(results) - scripted - low_div - no_comments}")

    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Explore template/scripted response patterns")
    parser.add_argument("--min-prs", type=int, default=20,
                        help="Minimum PRs per (repo, author, bot) triple (default: 20)")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max pairs to analyze (default: 50)")
    parser.add_argument("--bot", type=str, default=None,
                        help="Filter to a specific bot username (e.g. 'coderabbitai[bot]')")
    parser.add_argument("--show-comments", action="store_true",
                        help="Show top repeated comments for each pair")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
