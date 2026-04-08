#!/usr/bin/env bash
# Fetch monthly leaderboard data for time-series analysis.
# Run on the VM where the API is running.
#
# Usage: bash fetch_monthly.sh [API_BASE]

set -euo pipefail

API="${1:-http://localhost:3000}"
IGNORED="include_ignored=true"
CAPPED="${IGNORED}&exclude_bot_authored=true&exclude_self_authored=true&min_repo_contributors=2&max_author_repo_prs=50"

OUT="monthly_data"
mkdir -p "$OUT"

echo "=== Fetching monthly leaderboards (capped tier) ==="

# Monthly windows from Jun 2025 through Mar 2026
MONTHS=(
  "2025-06-01,2025-07-01"
  "2025-07-01,2025-08-01"
  "2025-08-01,2025-09-01"
  "2025-09-01,2025-10-01"
  "2025-10-01,2025-11-01"
  "2025-11-01,2025-12-01"
  "2025-12-01,2026-01-01"
  "2026-01-01,2026-02-01"
  "2026-02-01,2026-03-01"
  "2026-03-01,2026-04-01"
)

for m in "${MONTHS[@]}"; do
  start="${m%,*}"
  end="${m#*,}"
  label="${start}"
  echo "  ${label}..."
  curl -s "${API}/api/leaderboard?${CAPPED}&start_date=${start}&end_date=${end}" > "${OUT}/capped_${label}.json"
done

# Also fetch quarterly for larger windows
echo ""
echo "=== Fetching quarterly leaderboards ==="
QUARTERS=(
  "2025-06-01,2025-09-01,Q3_2025"
  "2025-09-01,2025-12-01,Q4_2025"
  "2025-12-01,2026-04-01,Q1_2026"
)

for q in "${QUARTERS[@]}"; do
  IFS=',' read -r start end label <<< "$q"
  echo "  ${label}..."
  curl -s "${API}/api/leaderboard?${CAPPED}&start_date=${start}&end_date=${end}" > "${OUT}/capped_${label}.json"
done

echo ""
echo "=== Done! Results in ${OUT}/ ==="
echo "Copy to local with: scp -r vm:~/crb/analysis/${OUT} ."
