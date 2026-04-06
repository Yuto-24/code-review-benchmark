# Online Benchmark Quality: Changes, Variables, and Analysis Plan

## 1. Data Integrity Fixes (Completed)

Changes to ensure the underlying data is correct before any analysis.

### 1A. PR Author (`pr_author`)


| Metric                                | Before   | After                                             |
| ------------------------------------- | -------- | ------------------------------------------------- |
| PRs with `pr_author`                  | ~493k    | ~652k                                             |
| PRs missing `pr_author`               | ~176,552 | ~17,097 (deleted repos, permanently unresolvable) |
| Roles corrected (`target_user_roles`) | —        | 123,312                                           |


**What changed**: Extracted author from BQ events, enrichment API fallback, one-time backfill via GitHub API. Future PRs get author automatically during enrichment.

**Why it matters**: `pr_author` is the foundation for bot-authored detection, self-authored detection, reviewer count, and concentration caps. Without it, these filters are blind.

### 1B. PR Merged Status (`pr_merged`)


| Metric                       | Before  | After                                               |
| ---------------------------- | ------- | --------------------------------------------------- |
| `pr_merged = TRUE`           | 0       | ~371k+                                              |
| `pr_merged = FALSE`          | 8,294   | ~82k+                                               |
| `pr_merged = NULL`           | 486,006 | ~114k (PRs without `pr_api_raw`, mostly unenriched) |
| `assembled.pr_merged` synced | —       | 240,589                                             |


**What changed**: Fixed extraction bug (True always wins over False), backfilled from `pr_api_raw`, synced `assembled.pr_merged`, fetched `pr_api_raw` for 275k PRs.

**Why it matters**: Analysis now gates on `pr_merged = TRUE`. Un-merged PRs (human never acted on review) are excluded from scoring — prevents unfairly penalizing bots for PRs where nobody engaged.

### 1C. Repo ID (`repo_id`)


| Metric                | Before | After                   |
| --------------------- | ------ | ----------------------- |
| PRs with `repo_id`    | 0      | ~553k+                  |
| PRs missing `repo_id` | all    | ~116k (no `pr_api_raw`) |


**What changed**: Added `repo_id BIGINT` column, extracted from BQ and `pr_api_raw`, backfilled existing rows.

**Why it matters**: Stable identity for repos across renames/transfers. Future: change unique constraint from `(chatbot_id, repo_name, pr_number)` to `(chatbot_id, repo_id, pr_number)`.

### 1D. BQ Events Merging

**What changed**: `ON CONFLICT DO NOTHING` → merge events on conflict (deduped by `event_id`, sorted by timestamp). If new events arrive, status resets to `pending` for re-processing.

**Why it matters**: Daily discover runs now capture merge/close events that happened after initial discovery. PRs get re-analyzed with complete timelines.

---

## 2. Pipeline Lifecycle Fixes (Completed)

Changes to how PRs flow through the pipeline and what gets analyzed.

### 2A. Merged-Only Analysis Gate

**Change**: Analysis now requires `pr_merged = TRUE`. Open PRs and closed-not-merged PRs stay in `assembled` status.

**Impact**: Open PRs where the human hasn't acted yet are no longer scored. Closed PRs where the human abandoned the work are excluded. Only completed review cycles are judged.

**Rust API**: Query also filters `WHERE p.pr_merged = TRUE`.

### 2B. Reply Comment Filtering

**Change**: `_format_bot_comments()` in `analyze.py` skips `review_comment` events where `in_reply_to_id` is set.

**Impact**: When a bot replies to another reviewer's thread (e.g., Devin responding to CodeRabbit), the reply is no longer sent to the LLM judge as if it were an original review suggestion. Only new threads started by the bot are judged.

### 2C. Stale PR Re-processing

**Change**: When discover merges new BQ events and the event count increases, PR status resets to `pending` → triggers re-enrich/assemble/analyze.

**Impact**: PRs initially discovered as open/unmerged can later be re-scored after merge events arrive.

### 2D. Enrichment Stores Full Metadata

**Change**: `_fetch_pr_summary()` now stores `pr_api_raw`, `pr_merged`, `repo_id` during enrichment.

**Impact**: Newly discovered PRs get `pr_merged` set immediately, unblocking the merged-only analysis gate.

---

## 3. Quality Filters & Variables (Available in Rust API)

All filters are toggleable query parameters on `/leaderboard`, `/daily`, and `/volumes` endpoints. Dashboard UI has controls for each.

### 3A. Structural Filters (Tier 2 — Computed at Load Time)


| Filter                  | Parameter               | Type | Default | Description                                             |
| ----------------------- | ----------------------- | ---- | ------- | ------------------------------------------------------- |
| Exclude self-authored   | `exclude_self_authored` | bool | `false` | PR author == reviewing bot (bot reviews its own PR)     |
| Exclude bot-authored    | `exclude_bot_authored`  | bool | `false` | PR author is any bot (Copilot, dependabot, Devin, etc.) |
| Min repo contributors   | `min_repo_contributors` | int  | none    | Repos with fewer unique PR authors are excluded         |
| Max author-repo-bot PRs | `max_author_repo_prs`   | int  | none    | Cap on PRs per (repo, author, bot) triple               |
| Require reviews         | `require_reviews`       | bool | `false` | PR must have non-empty `reviews` field                  |


**Data points (from exploration):**


| Signal                                    | PRs Affected | % of Analyzed |
| ----------------------------------------- | ------------ | ------------- |
| Self-authored (bot reviews own PR)        | 55,736       | 10.5%         |
| Bot-authored (any bot opened PR)          | 40,332       | 7.6%          |
| Bot-authored, reviewed by *different* bot | 23,340       | 4.4%          |
| Solo-contributor repos                    | 268,199      | 50.8%         |
| No formal GitHub reviews                  | 119,337      | 22.5%         |


**Score impact of concentration cap:**


| Bot                     | Concentrated F1 | Diverse F1 | Delta             |
| ----------------------- | --------------- | ---------- | ----------------- |
| chatgpt-codex-connector | 0.594           | 0.501      | +0.093 (inflated) |
| kiloconnect             | 0.347           | 0.506      | -0.159 (deflated) |
| sentry                  | 0.420           | 0.483      | -0.063            |
| sourcery-ai             | 0.560           | 0.529      | +0.031            |
| Most others             | ~same           | ~same      | <0.02             |


### 3B. Engagement Filters (Tier 4 — From `engagement_signals` JSON Column)

Per-PR signals computed from assembled timelines. Scoped to activity after the bot's first review event.


| Filter                   | Parameter                  | Type | Default | Description                                         |
| ------------------------ | -------------------------- | ---- | ------- | --------------------------------------------------- |
| Require human engagement | `require_human_engagement` | bool | `false` | At least 1 human comment or commit after bot review |
| Min human reviewers      | `min_human_reviewers`      | int  | none    | Distinct non-bot commenters (excluding PR author)   |
| Min commits after review | `min_commits_after_review` | int  | none    | Commits pushed after bot's first review             |


**Stored signals (per PR):**


| Signal                       | Type | Description                                      |
| ---------------------------- | ---- | ------------------------------------------------ |
| `human_reviewer_count`       | int  | Distinct non-bot commenters, excluding PR author |
| `human_comment_count`        | int  | Total human comments after bot review            |
| `human_comment_total_length` | int  | Sum of comment body lengths                      |
| `back_and_forth_rounds`      | int  | Bot→human→bot review cycles                      |
| `commits_after_review`       | int  | Commit events after bot's first review           |
| `has_human_engagement`       | bool | Any human activity after review                  |


**Caveat**: These signals correlate with issue severity and PR complexity. Low engagement ≠ bad quality (a minor nit might not warrant action). High engagement ≠ good quality (confusing suggestions cause more rounds). Best used as minimum-bar filters, not continuous weights.

### 3C. Label/Content Filters (Pre-existing)


| Filter          | Parameter                          | Type  | Description                              |
| --------------- | ---------------------------------- | ----- | ---------------------------------------- |
| Date range      | `start_date`, `end_date`           | date  | PR review date range                     |
| Chatbot         | `chatbot`                          | CSV   | Specific bots to include                 |
| Language        | `language`                         | CSV   | Programming language from labels         |
| Domain          | `domain`                           | CSV   | frontend/backend/infra/fullstack/docs    |
| PR type         | `pr_type`                          | CSV   | feature/bugfix/refactor/chore/docs/test  |
| Severity        | `severity`                         | CSV   | low/medium/high/critical                 |
| Diff lines      | `diff_lines_min`, `diff_lines_max` | int   | PR size by changed lines                 |
| Min PRs per day | `min_prs_per_day`                  | int   | Minimum sample size per day              |
| Min total PRs   | `min_total_prs`                    | int   | Minimum volume threshold                 |
| F-beta          | `beta`                             | float | Weight recall vs precision (default 1.0) |


### 3D. Template/Scripted Detection (Tier 3 — Offline Analysis)

Detects repos where human responses to bot reviews are canned/automated.

**Full scan results (2,842 pairs with ≥20 merged PRs):**


| Flag                | Count     | % of Pairs | Status                                      |
| ------------------- | --------- | ---------- | ------------------------------------------- |
| `no_human_comments` | 658       | 23.2%      | Investigate overlap with engagement filters |
| `user_template`     | 102       | 3.6%       | Likely filter — validate examples           |
| `user_scripted`     | 95        | 3.3%       | Likely filter — validate examples           |
| `mostly_short`      | 87        | 3.1%       | Likely filter — validate examples           |
| `low_diversity`     | 35        | 1.2%       | Likely filter — validate examples           |
| `scripted`          | 25        | 0.9%       | **Filter out**                              |
| `template_prefix`   | 15        | 0.5%       | **Filter out**                              |
| **no_flags**        | **1,951** | **68.6%**  | Clean                                       |


**Flag definitions:**

- `**scripted`**: <20% unique comments across all humans in a (repo, bot) pair. 80%+ exact duplicates.
- `**template_prefix**`: ≥50% of comments share a 50+ char common prefix. Boilerplate with variable suffixes.
- `**no_human_comments**`: Zero human comments after bot review. Nobody ever looked at it.
- `**low_diversity**`: 20-40% unique comments. Partial scripting.
- `**user_template**`: Per-commenter: ≥50% share a long prefix (≥50 chars, min 5 comments).
- `**user_scripted**`: Per-commenter: <30% unique (min 5 comments). Canned responses.
- `**mostly_short**`: >80% of comments under 50 chars. Low-effort engagement.

Not yet integrated as API filters — stored in `quality_report_full.json` for investigation.

---

## 4. Bot Detection (`is_bot_username`)

Comprehensive bot username detection used across Python ETL and Rust API:

- Matches `[bot]` suffix (e.g., `coderabbitai[bot]`)
- Matches stripped chatbot names (e.g., `coderabbitai` without `[bot]`)
- Matches general bots: `dependabot`, `renovate`, `github-actions`, `codecov`, `mergify`, `snyk-bot`, `greenkeeper`, `imgbot`, `stale`, `allcontributors`, `semantic-release-bot`, `github-advanced-security`, `llamapreview`, `ai-coding-guardrails`, `qodo-free-for-open-source-projects`, `amazon-q-developer`, `sourceryai`, `github-code-quality`, `copilot-pull-request-reviewer`, `raycastbot`, `cometactions`, `kilo-code-bot`, `codecov-comment`, `clawdbot`
- Case-insensitive

Used in: engagement signal computation, template detection, Rust API `pr_author_is_bot` flag, `exclude_bot_authored` filter.

---

## 5. Analysis Plan: Correlations and Stratification

### 5A. Score Impact Analysis

For each filter/variable, compute how leaderboard rankings change:


| Comparison                                      | What it shows                                    |
| ----------------------------------------------- | ------------------------------------------------ |
| **Baseline** (no filters) vs **merged-only**    | Impact of excluding un-merged PRs                |
| Merged vs **merged + exclude_bot_authored**     | How much bot-authored PRs inflate/deflate scores |
| Merged vs **merged + exclude_self_authored**    | Self-dealing impact                              |
| Merged vs **merged + min_repo_contributors=2**  | Solo-contributor repo impact                     |
| Merged vs **merged + max_author_repo_prs=50**   | Concentration cap impact                         |
| Merged vs **merged + require_human_engagement** | Automated pipeline exclusion impact              |
| Merged vs **merged + min_human_reviewers=1**    | "At least one non-author human looked at it"     |
| **All filters on** (strictest) vs **baseline**  | Cumulative impact on rankings                    |


For each: compute per-bot avg precision, recall, F1, and sample size. Report ranking changes.

### 5B. Variable Distributions

For each variable, compute distributions and correlations with scores:


| Variable                     | Distribution to compute | Correlation with score                         |
| ---------------------------- | ----------------------- | ---------------------------------------------- |
| `human_reviewer_count`       | Histogram (0, 1, 2, 3+) | Does more reviewers → higher/lower F1?         |
| `human_comment_count`        | Histogram + percentiles | Does more comments → higher/lower F1?          |
| `back_and_forth_rounds`      | Histogram (0, 1, 2, 3+) | Does more iteration → better scores?           |
| `commits_after_review`       | Histogram (0, 1, 2, 5+) | Does more post-review commits → higher recall? |
| `diff_lines`                 | Histogram + percentiles | Score vs PR size                               |
| Language                     | Bar chart by language   | Which languages have highest/lowest scores?    |
| Domain                       | Bar chart by domain     | Backend vs frontend vs infra                   |
| Severity                     | Bar chart by severity   | Low vs medium vs high vs critical              |
| PR type                      | Bar chart by type       | Feature vs bugfix vs refactor                  |
| Repo contributor count       | Histogram               | Score vs team size                             |
| Author-repo-bot triple count | Histogram               | Score vs concentration                         |


### 5C. Per-Bot Stratified Analysis

For each code review tool, break down performance by:

1. **PR size buckets**: small (<100 lines), medium (100-500), large (500-2000), xlarge (2000+)
2. **Language**: top 10 languages by volume
3. **Domain**: backend / frontend / infra / fullstack
4. **Severity**: low / medium / high / critical
5. **Engagement depth**: no engagement, shallow (1 comment), moderate (2-5), deep (6+)
6. **Review cycles**: 0 rounds, 1 round, 2+ rounds
7. **Repo quality tiers**:
  - **Tier A**: ≥3 contributors, ≤50 author-repo PRs, has human engagement, no template flags
  - **Tier B**: 2+ contributors OR has human engagement, no template flags
  - **Tier C**: everything else (solo repos, no engagement, template/scripted)

### 5D. Review Speed (From Offline)

Review speed is best measured from the offline head-to-head comparisons since the online benchmark doesn't have controlled timing conditions. Use standardized distributions from the offline dataset for per-tool speed metrics.

### 5E. Template/Quality Flag Impact

For each template flag type, compute:

- How many PRs are affected per bot
- Whether flagged PRs have systematically different scores
- Whether removing flagged pairs changes bot rankings
- Overlap between flags (e.g., are `no_human_comments` pairs also bot-authored?)

### 5F. Quality Tier Framework

Proposed stratification into quality tiers for benchmarking:


| Tier           | Criteria                                                                                   | Purpose                                         |
| -------------- | ------------------------------------------------------------------------------------------ | ----------------------------------------------- |
| **Gold**       | Merged, ≥3 contributors, ≥1 non-author reviewer, ≥1 commit after review, no template flags | Highest confidence — human meaningfully engaged |
| **Silver**     | Merged, ≥2 contributors, has_human_engagement=true, no template flags                      | Good confidence — some human involvement        |
| **Bronze**     | Merged, not bot-authored, not self-authored                                                | Minimum bar — at least a human opened the PR    |
| **Unfiltered** | All merged PRs                                                                             | Maximum sample size, lowest confidence          |


Compare per-bot rankings across tiers. Stable rankings across tiers = robust signal. Rankings that shift dramatically = the bot's score depends on data quality.

---

## 6. Initial Engagement Signal Results

Backfill completed: 534,765 analyzed PRs. Sample of 200k merged+analyzed PRs:

### Engagement Overview

| Metric | Count | % |
|--------|-------|---|
| Has human engagement | 94,470 | 47.2% |
| No engagement | 105,530 | 52.8% |

Over half of merged, analyzed PRs have zero human activity after the bot reviewed.

### Human Reviewer Count (Excluding PR Author)

| Reviewers | Count | % |
|-----------|-------|---|
| 0 | 168,341 | 84.2% |
| 1 | 25,361 | 12.7% |
| 2 | 5,142 | 2.6% |
| 3 | 915 | 0.5% |
| 4 | 176 | 0.1% |
| 5+ | 65 | <0.1% |

84% of PRs have no non-author human reviewer. Setting `min_human_reviewers=1` would be too aggressive as a default.

### Back-and-Forth Rounds (Bot→Human→Bot Cycles)

| Rounds | Count | % |
|--------|-------|---|
| 0 | 163,748 | 81.9% |
| 1 | 19,629 | 9.8% |
| 2 | 7,594 | 3.8% |
| 3 | 3,903 | 2.0% |
| 4 | 2,112 | 1.1% |
| 5+ | 3,014 | 1.5% |

Only 18% of PRs have any review cycle. 1.5% have deep conversations (5+ rounds).

### Commits After Review

| Commits | Count | % |
|---------|-------|---|
| 0 | 121,992 | 61.0% |
| 1 | 33,178 | 16.6% |
| 2-5 | 34,404 | 17.2% |
| 6+ | 10,426 | 5.2% |

39% have at least 1 commit after review — this is the most common form of engagement (author pushes a fix without commenting).

### Average Scores by Engagement

| Engagement | Precision | Recall | F1 | Scored PRs |
|------------|-----------|--------|----|------------|
| engaged=True | 0.576 | 0.402 | 0.474 | 57,404 |
| engaged=False | 0.519 | 0.418 | 0.463 | 6,273 |

F1 difference is small (0.474 vs 0.463). Notable: only 6k non-engaged PRs have both precision and recall scored — most non-engaged PRs likely have NULL scores (bot found nothing, or no human actions to judge).

### Key Takeaways

- **Commits are the main engagement signal**: 39% have post-review commits vs only 16% having a non-author reviewer. Common pattern: bot reviews → author pushes fix → merge, no comments.
- **`require_human_engagement=true` cuts ~53%** — reasonable as an optional quality filter.
- **`min_human_reviewers=1` cuts 84%** — too aggressive as default, but useful for "Gold tier" analysis.
- **Score gap is small** but sample size imbalance is large — need per-bot breakdown to see if specific bots are disproportionately affected.

### Follow-up Analysis Needed

- Per-bot engagement metrics — do some bots get systematically more/less human engagement?
- Overlap between 0-reviewer PRs and bot-authored / solo-contributor repos
- Distribution of the 6k non-engaged PRs with scores — concentrated in specific repos/bots?

---

## 7. Files and Tools


| File                                               | Purpose                                                                                   |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `online/etl/pipeline/quality.py`                   | `is_bot_username()`, `compute_engagement_signals()`                                       |
| `online/etl/pipeline/assemble.py`                  | Assembly step (computes engagement signals for new PRs)                                   |
| `online/etl/main.py`                               | CLI: `backfill-engagement`, `backfill-metadata`, `backfill-api-raw`, `backfill-pr-author` |
| `online/etl/scripts/explore_template_responses.py` | Interactive template/scripted detection                                                   |
| `online/etl/scripts/generate_quality_report.py`    | Full scan → `quality_report_full.json`                                                    |
| `online/api_service/src/db.rs`                     | Rust API: loads data + engagement signals                                                 |
| `online/api_service/src/compute.rs`                | Rust API: all filter logic                                                                |
| `online/api_service/src/model.rs`                  | Rust API: `PrRecord`, `FilterParams` structs                                              |
| `online/api_service/src/handlers.rs`               | Rust API: query param parsing                                                             |
| `online/api_service/static/index.html`             | Dashboard UI                                                                              |
| `online/etl/tests/test_quality_signals.py`         | 38 tests (unit + property-based)                                                          |
| `online/api_service/src/tests.rs`                  | 26 Rust tests                                                                             |
| `quality_report_full.json`                         | Template detection results (2,842 pairs)                                                  |


---

## 7. Outstanding Work


| Item                                                          | Status  | Priority |
| ------------------------------------------------------------- | ------- | -------- |
| Backfill engagement signals for all analyzed PRs              | Running | High     |
| Investigate `no_human_comments` overlap with existing filters | Pending | Medium   |
| Validate `user_template`/`user_scripted` examples             | Pending | Medium   |
| Integrate template blocklist into Rust API                    | Pending | Medium   |
| Build analysis notebook for score impact / correlations       | Pending | High     |
| Define quality tiers and compare rankings                     | Pending | High     |
| Weighted scoring (continuous instead of binary filters)       | Future  | Low      |
| De-correlate engagement signals with severity                 | Future  | Low      |
| Review speed integration from offline data                    | Future  | Low      |


