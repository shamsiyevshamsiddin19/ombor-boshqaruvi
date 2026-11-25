#!/usr/bin/env bash
# Manual rollback: switch the app symlink to the previous release and restart.
#
# Usage:
#   ssh ubuntu@SERVER 'bash /var/www/wms/app/deploy/rollback.sh'
#   ssh ubuntu@SERVER 'bash /var/www/wms/app/deploy/rollback.sh 20261126_143000'
#
# Without arguments: rolls back to the release immediately before the current one.
# With an argument: rolls back to the named release directory.
set -euo pipefail

APP_ROOT=/var/www/wms
RELEASES_DIR=$APP_ROOT/releases
APP_PORT=8001

if [ ! -L "$APP_ROOT/app" ]; then
  echo "Error: $APP_ROOT/app is not a symlink. Cannot determine current release." >&2
  exit 1
fi

CURRENT=$(readlink -f "$APP_ROOT/app")
echo "Current release: $(basename "$CURRENT")"

if [ -n "${1:-}" ]; then
  TARGET="$RELEASES_DIR/$1"
  if [ ! -d "$TARGET" ]; then
    echo "Error: release '$1' not found in $RELEASES_DIR" >&2
    echo "Available releases:"
    ls -1dt "$RELEASES_DIR"/*/ 2>/dev/null | while read -r d; do
      name=$(basename "$d")
      if [ "$(readlink -f "$d")" = "$CURRENT" ]; then
        echo "  $name  ← current"
      else
        echo "  $name"
      fi
    done
    exit 1
  fi
else
  # Find the release just before the current one (by directory modified time).
  TARGET=""
  found_current=false
  while read -r d; do
    real=$(readlink -f "$d")
    if $found_current; then
      TARGET="$real"
      break
    fi
    if [ "$real" = "$CURRENT" ]; then
      found_current=true
    fi
  done < <(ls -1dt "$RELEASES_DIR"/*/ 2>/dev/null)

  if [ -z "$TARGET" ]; then
    echo "Error: no previous release found to roll back to." >&2
    echo "Available releases:"
    ls -1dt "$RELEASES_DIR"/*/ 2>/dev/null | xargs -I{} basename {}
    exit 1
  fi
fi

if [ "$(readlink -f "$TARGET")" = "$CURRENT" ]; then
  echo "Target is already the current release. Nothing to do."
  exit 0
fi

echo "Rolling back to: $(basename "$TARGET")"
set -a; . "$APP_ROOT/.env"; set +a

ln -sfn "$TARGET" "$APP_ROOT/app.new"
mv -Tf "$APP_ROOT/app.new" "$APP_ROOT/app"

sudo systemctl restart wms

for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-Proto: https' \
    "http://127.0.0.1:$APP_PORT/accounts/login/" || true)
  if [ "$code" = 200 ]; then
    echo "Rollback complete: login page answers 200."
    exit 0
  fi
  sleep 1
done

echo "Warning: health check failed after rollback (last status: ${code:-none})" >&2
sudo journalctl -u wms -n 60 --no-pager >&2
exit 1
