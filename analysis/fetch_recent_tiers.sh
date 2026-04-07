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

OUT="recent_analysis"
mkdir -p "$OUT"

echo "=== Fetching tier leaderboards ==="

# -- Tier queries (same filters as section 7A) --
declare -A TIERS
TIERS[baseline]=""
TIERS[no_bot]="exclude_bot_authored=true"
TIERS[no_self]="exclude_bot_authored=true&exclude_self_authored=true"
TIERS[engaged]="exclude_bot_authored=true&exclude_self_authored=true&require_human_engagement=true"
TIERS[capped]="exclude_bot_authored=true&exclude_self_authored=true&require_human_engagement=true&max_author_repo_prs=50"
TIERS[silver]="exclude_bot_authored=true&exclude_self_authored=true&require_human_engagement=true&max_author_repo_prs=50&min_repo_contributors=2"
TIERS[gold]="exclude_bot_authored=true&exclude_self_authored=true&require_human_engagement=true&max_author_repo_prs=50&min_repo_contributors=3&min_human_reviewers=1&min_commits_after_review=1"

for tier in baseline no_bot no_self engaged capped silver gold; do
  params="${TIERS[$tier]}"

  # All-time
  url="${API}/api/leaderboard?${params}"
  echo "  ${tier} (all-time)..."
  curl -s "$url" > "${OUT}/${tier}_alltime.json"

  # Recent
  sep=$( [ -z "$params" ] && echo "" || echo "&" )
  url="${API}/api/leaderboard?${params}${sep}${DATE_PARAMS}"
  echo "  ${tier} (recent ${START} to ${END})..."
  curl -s "$url" > "${OUT}/${tier}_recent.json"
done

echo ""
echo "=== Fetching per-dimension breakdowns (Silver tier, recent) ==="

SILVER="exclude_bot_authored=true&exclude_self_authored=true&require_human_engagement=true&max_author_repo_prs=50&min_repo_contributors=2&${DATE_PARAMS}"

# By PR size
for size in "1,50" "51,200" "201,500" "501,1000" "1001,99999"; do
  min="${size%,*}"
  max="${size#*,}"
  label="${min}-${max}"
  echo "  size ${label}..."
  curl -s "${API}/api/leaderboard?${SILVER}&diff_lines_min=${min}&diff_lines_max=${max}" > "${OUT}/silver_recent_size_${label}.json"
done

# By severity
for sev in low medium high critical; do
  echo "  severity ${sev}..."
  curl -s "${API}/api/leaderboard?${SILVER}&severity=${sev}" > "${OUT}/silver_recent_severity_${sev}.json"
done

# By domain
for domain in backend frontend infra fullstack; do
  echo "  domain ${domain}..."
  curl -s "${API}/api/leaderboard?${SILVER}&domain=${domain}" > "${OUT}/silver_recent_domain_${domain}.json"
done

# By language (top languages)
for lang in TypeScript Python Rust Go JavaScript Java Ruby; do
  echo "  language ${lang}..."
  curl -s "${API}/api/leaderboard?${SILVER}&language=${lang}" > "${OUT}/silver_recent_lang_${lang}.json"
done

echo ""
echo "=== Done! Results in ${OUT}/ ==="
echo "Copy to local with: scp -r vm:~/crb/analysis/${OUT} ."
