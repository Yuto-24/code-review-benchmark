"""Per-bot distributions of key variables.

Shows histograms of engagement signals, PR sizes, repo characteristics,
and contributor counts — broken down by bot.

Usage (from online/etl/):
    PYTHONPATH=. uv run python ../../analysis/variable_distributions.py

"""

from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from typing import Any


def _pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total > 0 else "—"


def _print_histogram(label: str, counter: Counter, buckets: list[Any] | None = None) -> None:
    keys = buckets if buckets is not None else sorted(counter.keys())
    total = sum(counter.values())
    for k in keys:
        c = counter.get(k, 0)
        bar = "█" * int(40 * c / max(counter.values())) if counter else ""
        print(f"    {str(k):>8}: {c:>8} ({_pct(c, total):>6}) {bar}")


async def main() -> None:
    from config import DBConfig
    from db.connection import DBAdapter

    db = DBAdapter(DBConfig().database_url)
    await db.connect()

    print("Loading data (this may take a minute)...")

    rows = await db.fetchall("""
        SELECT c.github_username AS bot,
               p.engagement_signals,
               p.diff_lines,
               p.repo_name,
               p.pr_author,
               la.precision,
               la.recall
        FROM prs p
        JOIN llm_analyses la ON la.pr_id = p.id
        JOIN chatbots c ON la.chatbot_id = c.id
        WHERE p.engagement_signals IS NOT NULL
          AND p.status = 'analyzed'
          AND p.pr_merged = TRUE
    """)

    await db.close()

    print(f"Loaded {len(rows)} rows\n")

    # Per-bot accumulators
    BotData = dict[str, Any]
    bot_data: dict[str, BotData] = defaultdict(lambda: {
        "reviewer_count": Counter(),
        "comment_count": Counter(),
        "rounds": Counter(),
        "commits": Counter(),
        "diff_lines": Counter(),
        "repos": set(),
        "repo_pr_counts": Counter(),
        "repo_authors": defaultdict(set),
        "total": 0,
    })

    # Global accumulators
    global_reviewers = Counter()
    global_comments = Counter()
    global_rounds = Counter()
    global_commits = Counter()
    global_diff = Counter()

    def _reviewer_bucket(n: int) -> str:
        if n == 0: return "0"
        if n == 1: return "1"
        if n == 2: return "2"
        return "3+"

    def _comment_bucket(n: int) -> str:
        if n == 0: return "0"
        if n <= 2: return "1-2"
        if n <= 5: return "3-5"
        if n <= 10: return "6-10"
        return "11+"

    def _round_bucket(n: int) -> str:
        if n == 0: return "0"
        if n == 1: return "1"
        if n == 2: return "2"
        if n <= 5: return "3-5"
        return "6+"

    def _commit_bucket(n: int) -> str:
        if n == 0: return "0"
        if n == 1: return "1"
        if n <= 3: return "2-3"
        if n <= 5: return "4-5"
        return "6+"

    def _diff_bucket(d: int | None) -> str:
        if d is None: return "unknown"
        if d <= 50: return "1-50"
        if d <= 200: return "51-200"
        if d <= 500: return "201-500"
        if d <= 1000: return "501-1k"
        if d <= 2000: return "1k-2k"
        return "2k+"

    reviewer_buckets = ["0", "1", "2", "3+"]
    comment_buckets = ["0", "1-2", "3-5", "6-10", "11+"]
    round_buckets = ["0", "1", "2", "3-5", "6+"]
    commit_buckets = ["0", "1", "2-3", "4-5", "6+"]
    diff_buckets = ["1-50", "51-200", "201-500", "501-1k", "1k-2k", "2k+", "unknown"]

    for r in rows:
        bot = r["bot"]
        s = json.loads(r["engagement_signals"])
        d = bot_data[bot]
        d["total"] += 1

        rb = _reviewer_bucket(s["human_reviewer_count"])
        cb = _comment_bucket(s["human_comment_count"])
        rnb = _round_bucket(s["back_and_forth_rounds"])
        cmb = _commit_bucket(s["commits_after_review"])
        db_ = _diff_bucket(r["diff_lines"])

        d["reviewer_count"][rb] += 1
        d["comment_count"][cb] += 1
        d["rounds"][rnb] += 1
        d["commits"][cmb] += 1
        d["diff_lines"][db_] += 1
        d["repos"].add(r["repo_name"])
        d["repo_pr_counts"][(r["repo_name"],)] += 1
        if r["pr_author"]:
            d["repo_authors"][r["repo_name"]].add(r["pr_author"])

        global_reviewers[rb] += 1
        global_comments[cb] += 1
        global_rounds[rnb] += 1
        global_commits[cmb] += 1
        global_diff[db_] += 1

    # Sort bots by total PRs
    sorted_bots = sorted(bot_data.items(), key=lambda x: x[1]["total"], reverse=True)
    top_bots = sorted_bots[:15]

    # --- Global distributions ---
    print("=" * 80)
    print("  GLOBAL DISTRIBUTIONS (all bots)")
    print("=" * 80)

    print("\n  Human Reviewer Count:")
    _print_histogram("reviewers", global_reviewers, reviewer_buckets)

    print("\n  Human Comment Count:")
    _print_histogram("comments", global_comments, comment_buckets)

    print("\n  Back-and-Forth Rounds:")
    _print_histogram("rounds", global_rounds, round_buckets)

    print("\n  Commits After Review:")
    _print_histogram("commits", global_commits, commit_buckets)

    print("\n  Diff Lines (PR size):")
    _print_histogram("diff", global_diff, diff_buckets)

    # --- Per-bot distributions ---
    print(f"\n{'=' * 80}")
    print(f"  PER-BOT REVIEWER COUNT DISTRIBUTION")
    print(f"{'=' * 80}")
    header = f"  {'Bot':<30} {'Total':>7}"
    for b in reviewer_buckets:
        header += f" {b + ' rev':>8}"
    print(header)
    print(f"  {'-' * 30} {'-' * 7}" + "".join(f" {'-' * 8}" for _ in reviewer_buckets))
    for bot, d in top_bots:
        t = d["total"]
        line = f"  {bot:<30} {t:>7}"
        for b in reviewer_buckets:
            line += f" {_pct(d['reviewer_count'].get(b, 0), t):>8}"
        print(line)

    print(f"\n{'=' * 80}")
    print(f"  PER-BOT COMMENT COUNT DISTRIBUTION")
    print(f"{'=' * 80}")
    header = f"  {'Bot':<30} {'Total':>7}"
    for b in comment_buckets:
        header += f" {b + ' cmt':>8}"
    print(header)
    print(f"  {'-' * 30} {'-' * 7}" + "".join(f" {'-' * 8}" for _ in comment_buckets))
    for bot, d in top_bots:
        t = d["total"]
        line = f"  {bot:<30} {t:>7}"
        for b in comment_buckets:
            line += f" {_pct(d['comment_count'].get(b, 0), t):>8}"
        print(line)

    print(f"\n{'=' * 80}")
    print(f"  PER-BOT ROUNDS DISTRIBUTION")
    print(f"{'=' * 80}")
    header = f"  {'Bot':<30} {'Total':>7}"
    for b in round_buckets:
        header += f" {b + ' rnd':>8}"
    print(header)
    print(f"  {'-' * 30} {'-' * 7}" + "".join(f" {'-' * 8}" for _ in round_buckets))
    for bot, d in top_bots:
        t = d["total"]
        line = f"  {bot:<30} {t:>7}"
        for b in round_buckets:
            line += f" {_pct(d['rounds'].get(b, 0), t):>8}"
        print(line)

    print(f"\n{'=' * 80}")
    print(f"  PER-BOT COMMITS AFTER REVIEW DISTRIBUTION")
    print(f"{'=' * 80}")
    header = f"  {'Bot':<30} {'Total':>7}"
    for b in commit_buckets:
        header += f" {b + ' cmt':>8}"
    print(header)
    print(f"  {'-' * 30} {'-' * 7}" + "".join(f" {'-' * 8}" for _ in commit_buckets))
    for bot, d in top_bots:
        t = d["total"]
        line = f"  {bot:<30} {t:>7}"
        for b in commit_buckets:
            line += f" {_pct(d['commits'].get(b, 0), t):>8}"
        print(line)

    print(f"\n{'=' * 80}")
    print(f"  PER-BOT DIFF LINES (PR SIZE) DISTRIBUTION")
    print(f"{'=' * 80}")
    header = f"  {'Bot':<30} {'Total':>7}"
    for b in diff_buckets:
        header += f" {b:>8}"
    print(header)
    print(f"  {'-' * 30} {'-' * 7}" + "".join(f" {'-' * 8}" for _ in diff_buckets))
    for bot, d in top_bots:
        t = d["total"]
        line = f"  {bot:<30} {t:>7}"
        for b in diff_buckets:
            line += f" {_pct(d['diff_lines'].get(b, 0), t):>8}"
        print(line)

    # --- PR count and repo stats ---
    print(f"\n{'=' * 80}")
    print(f"  PER-BOT: PRs vs REPOS vs CONTRIBUTOR DISTRIBUTION")
    print(f"{'=' * 80}")

    contrib_buckets = ["1", "2", "3-5", "6-10", "11+"]

    def _contrib_bucket(n: int) -> str:
        if n == 1: return "1"
        if n == 2: return "2"
        if n <= 5: return "3-5"
        if n <= 10: return "6-10"
        return "11+"

    print(
        f"  {'Bot':<30} {'PRs':>7} {'Repos':>6} {'PR/Repo':>8} "
        + "".join(f" {b + ' ctrb':>8}" for b in contrib_buckets)
    )
    print(
        f"  {'-' * 30} {'-' * 7} {'-' * 6} {'-' * 8} "
        + "".join(f" {'-' * 8}" for _ in contrib_buckets)
    )

    for bot, d in top_bots:
        t = d["total"]
        n_repos = len(d["repos"])
        pr_per_repo = t / n_repos if n_repos > 0 else 0

        # Contributor distribution across this bot's repos
        contrib_dist = Counter()
        for repo in d["repos"]:
            n_authors = len(d["repo_authors"].get(repo, set()))
            contrib_dist[_contrib_bucket(max(n_authors, 1))] += 1

        line = f"  {bot:<30} {t:>7} {n_repos:>6} {pr_per_repo:>7.1f} "
        for b in contrib_buckets:
            line += f" {_pct(contrib_dist.get(b, 0), n_repos):>8}"
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
