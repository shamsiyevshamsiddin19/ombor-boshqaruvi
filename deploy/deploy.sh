#!/usr/bin/env bash
# Runs on the server after CI has synced code into a release directory.
#
# Layout:
#   /var/www/wms/releases/<timestamp>/   ← each deploy
#   /var/www/wms/app                     ← symlink → latest release
#   /var/www/wms/venv                    ← shared virtualenv
#   /var/www/wms/.env                    ← shared env file
#
# CI uploads to $RELEASE_DIR, then calls this script.
# On health-check failure the previous release is restored automatically.
set -euo pipefail

APP_ROOT=/var/www/wms
RELEASES_DIR=$APP_ROOT/releases
VENV=$APP_ROOT/venv
APP_PORT=8001
KEEP_RELEASES=5

RELEASE_DIR=${RELEASE_DIR:-$APP_ROOT/app}
cd "$RELEASE_DIR"
set -a; . "$APP_ROOT/.env"; set +a

PREV_RELEASE=""
if [ -L "$APP_ROOT/app" ]; then
  PREV_RELEASE=$(readlink -f "$APP_ROOT/app")
fi

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --disable-pip-version-check -r requirements.txt

"$VENV/bin/python" manage.py migrate --noinput
"$VENV/bin/python" manage.py collectstatic --noinput --verbosity 0
"$VENV/bin/python" manage.py check

# Atomically switch the symlink to this release.
ln -sfn "$RELEASE_DIR" "$APP_ROOT/app.new"
mv -Tf "$APP_ROOT/app.new" "$APP_ROOT/app"
echo "Symlink: app → $(basename "$RELEASE_DIR")"

sudo systemctl restart wms

health_ok=false
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-Proto: https' \
    "http://127.0.0.1:$APP_PORT/accounts/login/" || true)
  if [ "$code" = 200 ]; then
    health_ok=true
    break
  fi
  sleep 1
done

if $health_ok; then
  echo "Deployed: login page answers 200."
  # Prune old releases, keeping the N most recent.
  if [ -d "$RELEASES_DIR" ]; then
    cd "$RELEASES_DIR"
    # shellcheck disable=SC2012
    ls -1dt */ 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) | while read -r old; do
      echo "Pruning old release: $old"
      rm -rf "$old"
    done
  fi
  exit 0
fi

# Health check failed — roll back to previous release if available.
echo "Health check failed (last status: ${code:-none})" >&2
if [ -n "$PREV_RELEASE" ] && [ -d "$PREV_RELEASE" ] && [ "$PREV_RELEASE" != "$RELEASE_DIR" ]; then
  echo "Rolling back to $(basename "$PREV_RELEASE")..." >&2
  ln -sfn "$PREV_RELEASE" "$APP_ROOT/app.new"
  mv -Tf "$APP_ROOT/app.new" "$APP_ROOT/app"
  sudo systemctl restart wms
  echo "Rolled back to $(basename "$PREV_RELEASE")." >&2
fi
sudo journalctl -u wms -n 60 --no-pager >&2
exit 1
