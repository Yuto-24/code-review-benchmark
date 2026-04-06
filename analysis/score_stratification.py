"""Per-bot F1 scores stratified by PR size, severity, language, and domain.

Shows where each bot excels vs struggles.

Usage (from online/etl/):
    PYTHONPATH=. uv run python ../../analysis/score_stratification.py
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any


def _avg_f1(scores: list[tuple[float, float]]) -> float | None:
    if not scores:
        return None
    avg_p = sum(p for p, _ in scores) / len(scores)
    avg_r = sum(r for _, r in scores) / len(scores)
    return 2 * avg_p * avg_r / (avg_p + avg_r) if (avg_p + avg_r) > 0 else 0


def _f1_str(scores: list[tuple[float, float]], min_n: int = 20) -> str:
    if len(scores) < min_n:
        return f"  —({len(scores):>3})"
    f1 = _avg_f1(scores)
    return f"{f1:.3f}({len(scores):>3})" if f1 is not None else "  —"


def _diff_bucket(d: int | None) -> str:
    if d is None:
        return "unknown"
    if d <= 50:
        return "1-50"
    if d <= 200:
        return "51-200"
    if d <= 500:
        return "201-500"
    if d <= 1000:
        return "501-1k"
    return "1k+"


async def main() -> None:
    from config import DBConfig
    from db.connection import DBAdapter

    db = DBAdapter(DBConfig().database_url)
    await db.connect()

    print("Loading data...")

    rows = await db.fetchall("""
        SELECT c.github_username AS bot,
               p.diff_lines,
               la.precision,
               la.recall,
               pl.labels AS pr_labels_json
        FROM prs p
        JOIN llm_analyses la ON la.pr_id = p.id
        JOIN chatbots c ON la.chatbot_id = c.id
        LEFT JOIN pr_labels pl ON pl.pr_id = la.pr_id AND pl.chatbot_id = la.chatbot_id
        WHERE p.status = 'analyzed'
          AND p.pr_merged = TRUE
          AND la.precision IS NOT NULL
          AND la.recall IS NOT NULL
    """)

    await db.close()

    print(f"Loaded {len(rows)} scored rows\n")

    Scores = list[tuple[float, float]]
    BotStrat = dict[str, dict[str, Scores]]

    bot_strats: dict[str, BotStrat] = defaultdict(lambda: {
        "size": defaultdict(list),
        "severity": defaultdict(list),
        "language": defaultdict(list),
        "domain": defaultdict(list),
    })

    # Track language/domain counts globally for top-N selection
    lang_counts: dict[str, int] = defaultdict(int)
    domain_counts: dict[str, int] = defaultdict(int)

    for r in rows:
        bot = r["bot"]
        score = (r["precision"], r["recall"])

        # PR size
        size_b = _diff_bucket(r["diff_lines"])
        bot_strats[bot]["size"][size_b].append(score)

        # Labels
        labels_json = r.get("pr_labels_json")
        if labels_json:
            try:
                labels = json.loads(labels_json)
            except (json.JSONDecodeError, TypeError):
                labels = {}
        else:
            labels = {}

        sev = (labels.get("severity") or "").strip().lower()
        if sev in ("low", "medium", "high", "critical"):
            bot_strats[bot]["severity"][sev].append(score)

        lang = (labels.get("language") or "").strip().lower()
        if lang:
            bot_strats[bot]["language"][lang].append(score)
            lang_counts[lang] += 1

        dom = (labels.get("domain") or "").strip().lower()
        if dom:
            bot_strats[bot]["domain"][dom].append(score)
            domain_counts[dom] += 1

    # Sort bots by total scored PRs
    bot_totals = {
        bot: sum(len(v) for v in strat["size"].values())
        for bot, strat in bot_strats.items()
    }
    sorted_bots = sorted(bot_totals.items(), key=lambda x: x[1], reverse=True)
    top_bots = [b for b, _ in sorted_bots[:15]]

    # Top languages by count
    top_langs = [l for l, _ in sorted(lang_counts.items(), key=lambda x: x[1], reverse=True)[:8]]

    # ---- PR SIZE ----
    size_buckets = ["1-50", "51-200", "201-500", "501-1k", "1k+"]
    print("=" * 100)
    print("  F1 BY PR SIZE (diff lines)  — format: F1(n_scored), min 20 PRs")
    print("=" * 100)
    header = f"  {'Bot':<30}"
    for b in size_buckets:
        header += f" {b:>12}"
    header += f" {'ALL':>12}"
    print(header)
    print(f"  {'-'*30}" + "".join(f" {'-'*12}" for _ in size_buckets) + f" {'-'*12}")

    for bot in top_bots:
        strat = bot_strats[bot]
        line = f"  {bot:<30}"
        all_scores: Scores = []
        for b in size_buckets:
            scores = strat["size"].get(b, [])
            all_scores.extend(scores)
            line += f" {_f1_str(scores):>12}"
        line += f" {_f1_str(all_scores, 1):>12}"
        print(line)

    # ---- SEVERITY ----
    sev_buckets = ["low", "medium", "high", "critical"]
    print(f"\n{'='*100}")
    print("  F1 BY SEVERITY  — format: F1(n_scored), min 20 PRs")
    print("=" * 100)
    header = f"  {'Bot':<30}"
    for b in sev_buckets:
        header += f" {b:>12}"
    print(header)
    print(f"  {'-'*30}" + "".join(f" {'-'*12}" for _ in sev_buckets))

    for bot in top_bots:
        strat = bot_strats[bot]
        line = f"  {bot:<30}"
        for b in sev_buckets:
            line += f" {_f1_str(strat['severity'].get(b, [])):>12}"
        print(line)

    # ---- LANGUAGE ----
    print(f"\n{'='*100}")
    print(f"  F1 BY LANGUAGE (top {len(top_langs)})  — format: F1(n_scored), min 20 PRs")
    print("=" * 100)
    header = f"  {'Bot':<30}"
    for lang in top_langs:
        header += f" {lang[:10]:>12}"
    print(header)
    print(f"  {'-'*30}" + "".join(f" {'-'*12}" for _ in top_langs))

    for bot in top_bots:
        strat = bot_strats[bot]
        line = f"  {bot:<30}"
        for lang in top_langs:
            line += f" {_f1_str(strat['language'].get(lang, [])):>12}"
        print(line)

    # ---- DOMAIN ----
    domain_buckets = ["backend", "frontend", "infra", "fullstack"]
    print(f"\n{'='*100}")
    print("  F1 BY DOMAIN  — format: F1(n_scored), min 20 PRs")
    print("=" * 100)
    header = f"  {'Bot':<30}"
    for b in domain_buckets:
        header += f" {b:>12}"
    print(header)
    print(f"  {'-'*30}" + "".join(f" {'-'*12}" for _ in domain_buckets))

    for bot in top_bots:
        strat = bot_strats[bot]
        line = f"  {bot:<30}"
        for b in domain_buckets:
            line += f" {_f1_str(strat['domain'].get(b, [])):>12}"
        print(line)

    # ---- ALL BOTS ACROSS FILTER TIERS (full table) ----
    print(f"\n{'='*100}")
    print(f"  ALL {len(sorted_bots)} BOTS — OVERALL F1 + SCORED PRs")
    print("=" * 100)
    print(f"  {'Bot':<35} {'Scored':>8} {'F1':>8} {'Prec':>8} {'Recall':>8}")
    print(f"  {'-'*35} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for bot, total in sorted_bots:
        all_scores: Scores = []
        for bucket_scores in bot_strats[bot]["size"].values():
            all_scores.extend(bucket_scores)
        if not all_scores:
            continue
        avg_p = sum(p for p, _ in all_scores) / len(all_scores)
        avg_r = sum(r for _, r in all_scores) / len(all_scores)
        f1 = 2 * avg_p * avg_r / (avg_p + avg_r) if (avg_p + avg_r) > 0 else 0
        print(f"  {bot:<35} {len(all_scores):>8} {f1:>8.3f} {avg_p:>8.3f} {avg_r:>8.3f}")


if __name__ == "__main__":
    asyncio.run(main())
