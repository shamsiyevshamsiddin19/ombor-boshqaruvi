#!/usr/bin/env bash
# One-time server bootstrap for the WMS on Ubuntu (safe to re-run).
#
#   ssh ubuntu@SERVER 'PUBLIC_HOST=3.27.91.152 bash -s' < deploy/server-setup.sh
#
# Creates: PostgreSQL role + database, /var/www/wms/.env (secrets generated here and
# never printed), the wms systemd service (gunicorn on 127.0.0.1:8001), the nginx site
# for PUBLIC_HOST, and the `wms-manage` helper. Code itself arrives via CI (deploy.sh).
set -euo pipefail

PUBLIC_HOST=${PUBLIC_HOST:?Set PUBLIC_HOST, e.g. PUBLIC_HOST=3.27.91.152}
APP_USER=${APP_USER:-ubuntu}
APP_ROOT=/var/www/wms
APP_PORT=8001
DB_NAME=wms_db
DB_USER=wms_user
ENV_FILE=$APP_ROOT/.env

cd /tmp  # so `sudo -u postgres` does not complain about the working directory

# --- Packages ----------------------------------------------------------------------
need=()
for pkg in postgresql nginx python3-venv rsync curl openssl; do
  dpkg -s "$pkg" >/dev/null 2>&1 || need+=("$pkg")
done
if ((${#need[@]})); then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
fi
sudo systemctl enable --now postgresql nginx >/dev/null

# --- Directories -------------------------------------------------------------------
sudo mkdir -p "$APP_ROOT/releases"
sudo chown "$APP_USER:$APP_USER" "$APP_ROOT" "$APP_ROOT/releases"
sudo chmod 750 "$APP_ROOT"
# Ensure app exists for first deploy (CI creates the first release and switches the symlink).
if [ ! -e "$APP_ROOT/app" ]; then
  mkdir -p "$APP_ROOT/app"
fi

# --- Database + env file -----------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
  db_pass=$(openssl rand -hex 24)
  secret_key=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')
  role_exists=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'")
  verb=CREATE; [ "$role_exists" = 1 ] && verb=ALTER
  # Password goes in on stdin so it never shows up in the process list.
  sudo -u postgres psql -q -v ON_ERROR_STOP=1 <<SQL
$verb ROLE $DB_USER WITH LOGIN PASSWORD '$db_pass';
SQL
  (
    umask 077
    cat >"$ENV_FILE" <<EOF
# Production settings for the WMS. Read by systemd (wms.service), deploy.sh and wms-manage.
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=$secret_key
DJANGO_ALLOWED_HOSTS=$PUBLIC_HOST,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=http://$PUBLIC_HOST
DJANGO_TIME_ZONE=Asia/Tashkent

DATABASE_URL=postgres://$DB_USER:$db_pass@127.0.0.1:5432/$DB_NAME
DB_CONN_MAX_AGE=60

# Served over plain HTTP on the bare IP. When a domain with TLS is in front, set
# SSL_REDIRECT and SECURE_COOKIES to True and BEHIND_PROXY to True.
DJANGO_SECURE_SSL_REDIRECT=False
DJANGO_SECURE_COOKIES=False
DJANGO_BEHIND_PROXY=False
DJANGO_HSTS_SECONDS=0

WMS_BRAND_NAME="Mebel Ombori"
REGISTRATION_THROTTLE_RATE=10/hour
DJANGO_LOG_LEVEL=INFO
EOF
  )
  sudo chown "$APP_USER:$APP_USER" "$ENV_FILE"
  echo "Created $ENV_FILE"
else
  echo "Keeping existing $ENV_FILE"
fi

db_exists=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'")
if [ "$db_exists" != 1 ]; then
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
  echo "Created database $DB_NAME"
fi

# --- systemd service ---------------------------------------------------------------
sudo tee /etc/systemd/system/wms.service >/dev/null <<EOF
[Unit]
Description=Mebel Ombori WMS (Django + gunicorn)
After=network.target postgresql.service
Wants=postgresql.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_ROOT/app
EnvironmentFile=$ENV_FILE
ExecStart=$APP_ROOT/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:$APP_PORT --workers 2 --timeout 60 --access-logfile -
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable wms >/dev/null

# --- manage.py helper --------------------------------------------------------------
sudo tee /usr/local/bin/wms-manage >/dev/null <<EOF
#!/usr/bin/env bash
# Run Django management commands for the WMS with the production env, e.g.
#   wms-manage createsuperuser
set -a; . $ENV_FILE; set +a
cd $APP_ROOT/app && exec $APP_ROOT/venv/bin/python manage.py "\$@"
EOF
sudo chmod 755 /usr/local/bin/wms-manage

# --- nginx -------------------------------------------------------------------------
if sudo grep -q ssl_certificate /etc/nginx/sites-available/wms 2>/dev/null; then
  echo "nginx site wms already serves HTTPS; leaving it unchanged."
  echo "Server ready."
  exit 0
fi
sudo tee /etc/nginx/sites-available/wms >/dev/null <<EOF
# Mebel Ombori WMS on http://$PUBLIC_HOST (static files are served by WhiteNoise).
server {
    listen 80;
    listen [::]:80;
    server_name $PUBLIC_HOST;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
EOF
sudo ln -sfn /etc/nginx/sites-available/wms /etc/nginx/sites-enabled/wms

# Another enabled site may also claim the IP in its server_name; nginx would then pick
# that one. Drop the IP there (domain names stay), keeping a backup.
host_re=${PUBLIC_HOST//./\\.}
backups=()
for site in /etc/nginx/sites-enabled/*; do
  real=$(readlink -f "$site")
  [ "$real" = /etc/nginx/sites-available/wms ] && continue
  if grep -Eq "server_name[^;]*[[:space:]]$host_re([[:space:];]|$)" "$real"; then
    backup=/var/backups/nginx-$(basename "$real")-$(date +%Y%m%d%H%M%S).conf
    sudo cp "$real" "$backup"
    backups+=("$real=$backup")
    sudo sed -i -E "/server_name/ s/[[:space:]]$host_re([[:space:];])/\1/g" "$real"
    echo "Removed $PUBLIC_HOST from server_name in $real (backup: $backup)"
  fi
done

if ! sudo nginx -t 2>/tmp/nginx-test.log; then
  cat /tmp/nginx-test.log >&2
  for pair in "${backups[@]}"; do sudo cp "${pair#*=}" "${pair%%=*}"; done
  sudo rm -f /etc/nginx/sites-enabled/wms
  echo "nginx config test failed; changes rolled back" >&2
  exit 1
fi
sudo systemctl reload nginx

echo "Server ready. Next: push to main (CI runs deploy/deploy.sh)."
