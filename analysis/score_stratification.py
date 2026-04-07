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


Scores = list[tuple[float, float]]


def _avg_prf(scores: Scores) -> tuple[float, float, float] | None:
    if not scores:
        return None
    avg_p = sum(p for p, _ in scores) / len(scores)
    avg_r = sum(r for _, r in scores) / len(scores)
    f1 = 2 * avg_p * avg_r / (avg_p + avg_r) if (avg_p + avg_r) > 0 else 0.0
    return avg_p, avg_r, f1


def _metric_str(scores: Scores, metric: str = "f1", min_n: int = 20) -> str:
    """Format a single metric value. metric is 'f1', 'p', or 'r'."""
    if len(scores) < min_n:
        return f"  —({len(scores):>3})"
    prf = _avg_prf(scores)
    if prf is None:
        return "  —"
    p, r, f1 = prf
    val = {"f1": f1, "p": p, "r": r}[metric]
    return f"{val:.3f}({len(scores):>3})"


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

    def _print_dimension_tables(
        title: str,
        buckets: list[str],
        dim_key: str,
        include_all: bool = False,
    ) -> None:
        """Print F1, Precision, and Recall tables for a stratification dimension."""
        for metric, label in [("f1", "F1"), ("p", "PRECISION"), ("r", "RECALL")]:
            print(f"\n{'='*100}")
            print(f"  {label} BY {title}  — format: val(n_scored), min 20 PRs")
            print("=" * 100)
            header = f"  {'Bot':<30}"
            for b in buckets:
                header += f" {b[:10]:>12}"
            if include_all:
                header += f" {'ALL':>12}"
            print(header)
            sep = f"  {'-'*30}" + "".join(f" {'-'*12}" for _ in buckets)
            if include_all:
                sep += f" {'-'*12}"
            print(sep)

            for bot in top_bots:
                strat = bot_strats[bot]
                line = f"  {bot:<30}"
                all_scores: Scores = []
                for b in buckets:
                    scores = strat[dim_key].get(b, [])
                    if include_all:
                        all_scores.extend(scores)
                    line += f" {_metric_str(scores, metric):>12}"
                if include_all:
                    line += f" {_metric_str(all_scores, metric, min_n=1):>12}"
                print(line)

    size_buckets = ["1-50", "51-200", "201-500", "501-1k", "1k+"]
    _print_dimension_tables("PR SIZE (diff lines)", size_buckets, "size", include_all=True)

    sev_buckets = ["low", "medium", "high", "critical"]
    _print_dimension_tables("SEVERITY", sev_buckets, "severity")

    _print_dimension_tables("LANGUAGE (top 8)", top_langs, "language")

    domain_buckets = ["backend", "frontend", "infra", "fullstack"]
    _print_dimension_tables("DOMAIN", domain_buckets, "domain")

    # ---- ALL BOTS OVERALL ----
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
        prf = _avg_prf(all_scores)
        assert prf is not None
        avg_p, avg_r, f1 = prf
        print(f"  {bot:<35} {len(all_scores):>8} {f1:>8.3f} {avg_p:>8.3f} {avg_r:>8.3f}")


if __name__ == "__main__":
    asyncio.run(main())
