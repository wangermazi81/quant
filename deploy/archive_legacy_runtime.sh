#!/usr/bin/env bash
# Archive legacy root-level runtime files left by early deployments.
# Default mode is dry-run. Use --apply to move files into backups/legacy-root-files-<stamp>/.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quant}"
APPLY=0
if [ "${1:-}" = "--apply" ]; then
  APPLY=1
fi

cd "$APP_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"
archive_dir="backups/legacy-root-files-$stamp"

patterns=(
  "ai_config.json"
  "search_config.json"
  "users.db"
  "users.db-wal"
  "users.db-shm"
  "paper_trading.db"
  "paper_trading.db-wal"
  "paper_trading.db-shm"
  "app.log"
  "broker_interface.py"
  "db_migration.py"
  "kronos_predictor.py"
  "market_regime.py"
  "news_fetcher.py"
  "notification_manager.py"
  "portfolio_optimizer.py"
  "search_manager.py"
  "test_ai_api.py"
  "test_mimo.py"
  "tmp_*.py"
  "_insert_local.py"
  "_verify_routes.py"
  "_urlmap_baseline.json"
)

files=()
for pattern in "${patterns[@]}"; do
  while IFS= read -r -d '' file; do
    if git ls-files --error-unmatch "$file" >/dev/null 2>&1; then
      continue
    fi
    files+=("$file")
  done < <(find . -maxdepth 1 -type f -name "$pattern" -printf '%P\0')
done

if [ "${#files[@]}" -eq 0 ]; then
  echo "No legacy root runtime files found."
  exit 0
fi

printf 'Legacy root runtime files:\n'
printf '  %s\n' "${files[@]}"

if [ "$APPLY" != "1" ]; then
  echo "Dry-run only. Re-run with --apply to archive them."
  exit 0
fi

mkdir -p "$archive_dir"
chmod 700 "$archive_dir" 2>/dev/null || true

for file in "${files[@]}"; do
  mv -- "$file" "$archive_dir/"
done

echo "Archived ${#files[@]} files to $APP_DIR/$archive_dir"
