#!/usr/bin/env bash
# Fetch leaderboard data for all tiers (all-time + recent 2 months)
# Run on the VM where the API is running.
#
# Usage: bash fetch_recent_tiers.sh [API_BASE]
# Default API_BASE: http://localhost:3000

set -euo pipefail

API="${1:-http://localhost:3000}"
START="2026-02-01"
END="2026-04-01"
DATE_PARAMS="start_date=${START}&end_date=${END}"
IGNORED="include_ignored=true"

OUT="recent_analysis"
mkdir -p "$OUT"

echo "=== Fetching tier leaderboards ==="

# -- Cumulative tiers (each adds more filters) --
declare -A TIERS
TIERS[baseline]="${IGNORED}"
TIERS[no_bot]="${IGNORED}&exclude_bot_authored=true"
TIERS[no_self]="${IGNORED}&exclude_bot_authored=true&exclude_self_authored=true"
TIERS[team]="${IGNORED}&exclude_bot_authored=true&exclude_self_authored=true&min_repo_contributors=2"
TIERS[capped]="${IGNORED}&exclude_bot_authored=true&exclude_self_authored=true&min_repo_contributors=2&max_author_repo_prs=50"
TIERS[engaged]="${IGNORED}&exclude_bot_authored=true&exclude_self_authored=true&min_repo_contributors=2&max_author_repo_prs=50&require_human_engagement=true"
TIERS[strict]="${IGNORED}&exclude_bot_authored=true&exclude_self_authored=true&min_repo_contributors=3&max_author_repo_prs=50&require_human_engagement=true&min_human_reviewers=1&min_commits_after_review=1"

for tier in baseline no_bot no_self team capped engaged strict; do
  params="${TIERS[$tier]}"

  # All-time
  url="${API}/api/leaderboard?${params}"
  echo "  ${tier} (all-time)..."
  curl -s "$url" > "${OUT}/${tier}_alltime.json"

  # Recent
  url="${API}/api/leaderboard?${params}&${DATE_PARAMS}"
  echo "  ${tier} (recent ${START} to ${END})..."
  curl -s "$url" > "${OUT}/${tier}_recent.json"
done

echo ""
echo "=== Fetching per-dimension breakdowns ==="

# Primary breakdown base: human-authored, team project, concentration-capped
# This is the proposed default filter set for reports.
CAPPED_RECENT="${IGNORED}&exclude_bot_authored=true&exclude_self_authored=true&min_repo_contributors=2&max_author_repo_prs=50&${DATE_PARAMS}"

echo "  --- capped tier breakdowns (recent) ---"

# By PR size
for size in "1,50" "51,200" "201,500" "501,1000" "1001,99999"; do
  min="${size%,*}"
  max="${size#*,}"
  label="${min}-${max}"
  echo "  size ${label}..."
  curl -s "${API}/api/leaderboard?${CAPPED_RECENT}&diff_lines_min=${min}&diff_lines_max=${max}" > "${OUT}/capped_recent_size_${label}.json"
done

# By severity
for sev in low medium high critical; do
  echo "  severity ${sev}..."
  curl -s "${API}/api/leaderboard?${CAPPED_RECENT}&severity=${sev}" > "${OUT}/capped_recent_severity_${sev}.json"
done

# By domain
for domain in backend frontend infra fullstack; do
  echo "  domain ${domain}..."
  curl -s "${API}/api/leaderboard?${CAPPED_RECENT}&domain=${domain}" > "${OUT}/capped_recent_domain_${domain}.json"
done

# By language (top languages)
for lang in TypeScript Python Rust Go JavaScript Java Ruby; do
  echo "  language ${lang}..."
  curl -s "${API}/api/leaderboard?${CAPPED_RECENT}&language=${lang}" > "${OUT}/capped_recent_lang_${lang}.json"
done

echo ""
echo "=== Done! Results in ${OUT}/ ==="
echo "Copy to local with: scp -r vm:~/crb/analysis/${OUT} ."
