"""Exploratory script for template/scripted response detection.

Run on the VM to understand human comment patterns before building the blocklist.
Read-only — does not modify any data.

Usage:
    cd ~/crb/online/etl
    PYTHONPATH=. uv run python scripts/explore_template_responses.py --show-comments
    PYTHONPATH=. uv run python scripts/explore_template_responses.py --bot "cubic-dev-ai[bot]" --include-unmerged --show-comments
    PYTHONPATH=. uv run python scripts/explore_template_responses.py --repo "Voornaamenachternaam/exchange_gateway" --include-unmerged --show-comments --min-prs 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
) -> list[tuple[str, str]]:
    """Extract (actor, body) pairs for human comments posted after the bot's first review."""
    bot_lower = chatbot_username.lower()

    bot_first_ts: str | None = None
    for e in events:
        actor = (e.get("actor") or "").lower()
        if actor == bot_lower and e.get("event_type") in COMMENT_EVENT_TYPES:
            bot_first_ts = e.get("timestamp")
            break

    if bot_first_ts is None:
        return []

    comments: list[tuple[str, str]] = []
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
                comments.append((actor, body))

    return comments


def _longest_common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    shortest = min(strings, key=len)
    for i, char in enumerate(shortest):
        if any(s[i] != char for s in strings):
            return shortest[:i]
    return shortest


def _compute_prefix_ratio(comments: list[str], min_prefix_len: int = 50) -> tuple[float, str]:
    """Find the most common long prefix among comments.

    Returns (fraction of comments sharing a prefix >= min_prefix_len chars, the prefix).
    """
    if len(comments) < 3:
        return 0.0, ""

    normalized = [_normalize(c) for c in comments]

    # Try each comment as a potential template prefix
    best_ratio = 0.0
    best_prefix = ""
    for candidate in normalized:
        if len(candidate) < min_prefix_len:
            continue
        prefix = candidate[:min_prefix_len]
        matching = sum(1 for c in normalized if c.startswith(prefix))
        ratio = matching / len(normalized)
        if ratio > best_ratio:
            best_ratio = ratio
            # Find actual shared prefix among matching comments
            matching_comments = [c for c in normalized if c.startswith(prefix)]
            actual_prefix = _longest_common_prefix(matching_comments)
            best_prefix = actual_prefix

    return best_ratio, best_prefix


async def run(args: argparse.Namespace) -> None:
    cfg = DBConfig()
    db = DBAdapter(cfg.database_url)
    await db.connect()

    try:
        min_prs = args.min_prs
        limit = args.limit

        bot_clause = f"AND c.github_username = '{args.bot}'" if args.bot else ""
        repo_clause = f"AND p.repo_name = '{args.repo}'" if args.repo else ""
        merged_clause = "" if args.include_unmerged else "AND p.pr_merged = TRUE"

        filters = " ".join(filter(None, [
            f"bot={args.bot}" if args.bot else None,
            f"repo={args.repo}" if args.repo else None,
            "include-unmerged" if args.include_unmerged else "merged-only",
        ]))
        print(f"Finding repos with >= {min_prs} analyzed PRs [{filters}]...\n")

        # Group by (repo, bot) instead of (repo, author, bot)
        # so we catch template comments from non-authors (e.g., repo owner commenting on bot-authored PRs)
        pairs = await db.fetchall(f"""
            SELECT p.repo_name, c.github_username AS chatbot,
                   COUNT(*) AS pr_count
            FROM prs p
            JOIN chatbots c ON c.id = p.chatbot_id
            WHERE p.status = 'analyzed'
              AND p.assembled IS NOT NULL
              {merged_clause}
              {bot_clause}
              {repo_clause}
            GROUP BY p.repo_name, c.github_username
            HAVING COUNT(*) >= {min_prs}
            ORDER BY COUNT(*) DESC
            LIMIT {limit}
        """)

        print(f"Found {len(pairs)} (repo, bot) pairs with >= {min_prs} PRs\n")
        if not pairs:
            return

        results: list[dict] = []

        for i, pair in enumerate(pairs):
            repo = pair["repo_name"]
            chatbot = pair["chatbot"]
            pr_count = pair["pr_count"]

            merged_clause2 = "" if args.include_unmerged else "AND p.pr_merged = TRUE"
            rows = await db.fetchall(*db._translate_params(
                f"""
                SELECT p.assembled
                FROM prs p
                JOIN chatbots c ON c.id = p.chatbot_id
                WHERE p.repo_name = $1
                  AND c.github_username = $2
                  AND p.status = 'analyzed'
                  AND p.assembled IS NOT NULL
                  {merged_clause2}
                ORDER BY p.id
                LIMIT 300
                """,
                (repo, chatbot),
            ))

            # Collect comments grouped by commenter
            commenter_comments: dict[str, list[str]] = defaultdict(list)
            prs_with_comments = 0
            for row in rows:
                assembled = json.loads(row["assembled"])
                comments = _extract_human_comments_after_bot(
                    assembled.get("events", []), chatbot
                )
                if comments:
                    prs_with_comments += 1
                for actor, body in comments:
                    commenter_comments[actor].append(body)

            all_comments = [body for bodies in commenter_comments.values() for body in bodies]

            if not all_comments:
                results.append({
                    "repo": repo, "chatbot": chatbot,
                    "pr_count": pr_count, "total_comments": 0,
                    "unique_ratio": 0, "short_ratio": 0, "prefix_ratio": 0,
                    "prs_with_comments": 0, "commenters": {},
                })
                continue

            # Overall metrics
            normalized = [_normalize(c) for c in all_comments]
            total = len(normalized)
            unique_count = len(set(normalized))
            unique_ratio = unique_count / total

            short_count = sum(1 for c in all_comments if len(c) < 50)
            short_ratio = short_count / total

            # Prefix detection across all comments
            prefix_ratio, shared_prefix = _compute_prefix_ratio(all_comments)

            # Per-commenter breakdown
            commenter_stats: dict[str, dict] = {}
            for actor, bodies in commenter_comments.items():
                n = [_normalize(b) for b in bodies]
                u = len(set(n))
                pr_ratio, pr_prefix = _compute_prefix_ratio(bodies)
                commenter_stats[actor] = {
                    "count": len(bodies),
                    "unique": u,
                    "diversity": u / len(n) if n else 0,
                    "prefix_ratio": pr_ratio,
                    "prefix": pr_prefix[:80] if pr_prefix else "",
                    "top": Counter(n).most_common(3),
                }

            counter = Counter(normalized)

            results.append({
                "repo": repo, "chatbot": chatbot,
                "pr_count": pr_count, "total_comments": total,
                "unique_count": unique_count, "unique_ratio": unique_ratio,
                "short_count": short_count, "short_ratio": short_ratio,
                "prefix_ratio": prefix_ratio, "shared_prefix": shared_prefix,
                "prs_with_comments": prs_with_comments,
                "commenters": commenter_stats,
                "top_comments": counter.most_common(5),
            })

            # Flag logic
            flags: list[str] = []
            if unique_ratio < 0.2 and total >= 5:
                flags.append("SCRIPTED")
            elif unique_ratio < 0.4 and total >= 5:
                flags.append("LOW DIVERSITY")
            if prefix_ratio >= 0.5 and total >= 5:
                flags.append(f"TEMPLATE PREFIX ({prefix_ratio:.0%})")
            if short_ratio > 0.8 and total >= 5:
                flags.append("MOSTLY SHORT")

            # Per-commenter flags
            for actor, cs in commenter_stats.items():
                if cs["count"] >= 5 and cs["prefix_ratio"] >= 0.5:
                    flags.append(f"USER TEMPLATE: {actor} ({cs['prefix_ratio']:.0%} share prefix)")
                elif cs["count"] >= 5 and cs["diversity"] < 0.3:
                    flags.append(f"USER SCRIPTED: {actor} (div={cs['diversity']:.0%})")

            flag_str = " | ".join(flags) if flags else ""

            print(f"[{i+1}/{len(pairs)}] {repo} | {chatbot}")
            print(f"  PRs: {pr_count} | with human comments: {prs_with_comments}")
            print(f"  Comments: {total} total, {unique_count} unique "
                  f"(diversity: {unique_ratio:.2f})")
            print(f"  Short (<50 chars): {short_count}/{total} ({short_ratio:.1%})")
            print(f"  Shared prefix: {prefix_ratio:.1%} of comments share a 50+ char prefix")
            if flag_str:
                print(f"  *** {flag_str} ***")

            if args.show_comments:
                for actor, cs in sorted(commenter_stats.items(),
                                        key=lambda x: x[1]["count"], reverse=True):
                    print(f"  [{actor}] {cs['count']} comments, "
                          f"{cs['unique']} unique (div={cs['diversity']:.1%}), "
                          f"prefix_share={cs['prefix_ratio']:.1%}")
                    if cs["prefix"] and cs["prefix_ratio"] >= 0.3:
                        print(f"    Prefix: \"{cs['prefix'][:100]}...\"")
                    for comment, count in cs["top"]:
                        if count > 1 or cs["count"] <= 5:
                            preview = comment[:100] + "..." if len(comment) > 100 else comment
                            print(f"    [{count}x] \"{preview}\"")
            print()

        # Summary
        print(f"\n{'='*130}")
        print("SUMMARY")
        print(f"{'='*130}\n")

        print(f"{'Repo':<40} {'Bot':<25} {'PRs':>5} "
              f"{'Cmts':>5} {'Uniq':>5} {'Div%':>6} {'Pfx%':>6} {'Short%':>7} {'Flag':<30}")
        print("-" * 145)

        for r in sorted(results, key=lambda x: (-x["prefix_ratio"], x["unique_ratio"])):
            flags = []
            if r["total_comments"] == 0:
                flags.append("NO COMMENTS")
            else:
                if r["unique_ratio"] < 0.2 and r["total_comments"] >= 5:
                    flags.append("SCRIPTED")
                elif r["unique_ratio"] < 0.4 and r["total_comments"] >= 5:
                    flags.append("LOW DIV")
                if r["prefix_ratio"] >= 0.5 and r["total_comments"] >= 5:
                    flags.append(f"TEMPLATE")
                if r["short_ratio"] > 0.8 and r["total_comments"] >= 5:
                    flags.append("SHORT")

                # Check per-commenter flags
                for actor, cs in r.get("commenters", {}).items():
                    if cs["count"] >= 5 and (cs["prefix_ratio"] >= 0.5 or cs["diversity"] < 0.3):
                        flags.append(f"USR:{actor[:15]}")

            flag_str = " ".join(flags)

            print(f"{r['repo']:<40} {r['chatbot']:<25} "
                  f"{r['pr_count']:>5} {r['total_comments']:>5} "
                  f"{r.get('unique_count', 0):>5} {r['unique_ratio']:>5.1%} "
                  f"{r['prefix_ratio']:>5.1%} "
                  f"{r['short_ratio']:>6.1%} {flag_str:<30}")

        # Overall stats
        scripted = sum(1 for r in results
                       if r["unique_ratio"] < 0.2 and r["total_comments"] >= 5)
        low_div = sum(1 for r in results
                      if 0.2 <= r["unique_ratio"] < 0.4 and r["total_comments"] >= 5)
        template_prefix = sum(1 for r in results
                              if r["prefix_ratio"] >= 0.5 and r["total_comments"] >= 5)
        no_comments = sum(1 for r in results if r["total_comments"] == 0)
        user_flagged = sum(1 for r in results
                           for cs in r.get("commenters", {}).values()
                           if cs["count"] >= 5 and (cs["prefix_ratio"] >= 0.5 or cs["diversity"] < 0.3))

        print(f"\nTotal pairs analyzed: {len(results)}")
        print(f"  Likely scripted (diversity < 0.2): {scripted}")
        print(f"  Low diversity (0.2-0.4): {low_div}")
        print(f"  Template prefix (>= 50% share prefix): {template_prefix}")
        print(f"  Per-user template/scripted flags: {user_flagged}")
        print(f"  No human comments at all: {no_comments}")
        print(f"  Normal: {len(results) - scripted - low_div - template_prefix - no_comments}")

    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Explore template/scripted response patterns")
    parser.add_argument("--min-prs", type=int, default=20,
                        help="Minimum PRs per (repo, bot) pair (default: 20)")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max pairs to analyze (default: 50)")
    parser.add_argument("--bot", type=str, default=None,
                        help="Filter to a specific bot username")
    parser.add_argument("--repo", type=str, default=None,
                        help="Filter to a specific repo (e.g. 'owner/repo')")
    parser.add_argument("--include-unmerged", action="store_true",
                        help="Include PRs regardless of merge status (default: merged only)")
    parser.add_argument("--show-comments", action="store_true",
                        help="Show per-commenter breakdown with top repeated comments")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
