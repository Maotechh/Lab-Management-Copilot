#!/usr/bin/env bash
set -Eeuo pipefail
umask 0027

if [[ "$EUID" -ne 0 ]]; then
    printf 'Run this installer as root.\n' >&2
    exit 1
fi

release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
app_root=/opt/lab-management
state_dir=/var/lib/lab-management
service_user=lab-management
database="$state_dir/consumables.db"
release_backup="/var/backups/lab-management/release-$(date -u +%Y%m%dT%H%M%SZ)"
previous_release="$(readlink -f "$app_root/current" || true)"
changed=0
paused=0

if [[ "$release_dir" != "$app_root"/releases/* ]]; then
    printf 'Extract the release into %s/releases/<release-id> first.\n' "$app_root" >&2
    exit 1
fi
if [[ -e "$app_root/current" && ! -L "$app_root/current" ]]; then
    printf 'The current release must be a symbolic link.\n' >&2
    exit 1
fi
chown -hR root:root "$release_dir"
if ! id "$service_user" >/dev/null 2>&1; then
    useradd --system --home-dir "$state_dir" --shell /usr/sbin/nologin "$service_user"
fi
install -d -m 0750 -o "$service_user" -g "$service_user" "$state_dir"
install -d -m 0750 -o root -g "$service_user" /etc/lab-management /etc/lab-management/push
install -d -m 0700 /var/backups/lab-management "$release_backup"
if [[ ! -f "$database" ]]; then
    install -m 0640 -o "$service_user" -g "$service_user" "$release_dir/bootstrap/consumables.db" "$database"
fi
if [[ ! -f /etc/lab-management/environment ]]; then
    install -m 0640 -o root -g "$service_user" "$release_dir/bootstrap/environment" /etc/lab-management/environment
fi
if [[ ! -e "$release_dir/data" ]]; then ln -s "$state_dir" "$release_dir/data"; fi
python3 -m venv "$release_dir/.venv"
python_bin="$release_dir/.venv/bin/python"
"$python_bin" -m pip install -r "$release_dir/requirements.txt"
"$python_bin" -m pip freeze > "$release_dir/installed-requirements.txt"
chown -hR root:"$service_user" "$release_dir"
chmod -R g+rX "$release_dir"

saved_paths=()
for path in /etc/systemd/system/consumable-{assistant,notifications,backup}.{service,timer} /etc/nginx/sites-available/lab-management /etc/lab-management/environment; do
    if [[ -f "$path" ]]; then saved_paths+=("${path#/}"); fi
done
tar -C / -czf "$release_backup/configuration.tar.gz" "${saved_paths[@]}"

rollback() {
    local result=$?
    trap - ERR
    if [[ "$changed" == 1 ]]; then
        systemctl stop consumable-notifications consumable-assistant consumable-backup.timer || true
        "$python_bin" "$release_dir/scripts/restore_database.py" --snapshot "$release_backup/database.db" --database "$database" --services-stopped
        tar -C / -xzf "$release_backup/configuration.tar.gz"
        if [[ -n "$previous_release" && -d "$previous_release" ]]; then
            ln -sfn "$previous_release" "$app_root/current.next"
            mv -Tf "$app_root/current.next" "$app_root/current"
        fi
        systemctl daemon-reload
        systemctl restart consumable-assistant || true
        if tar -tzf "$release_backup/configuration.tar.gz" | grep -q 'consumable-notifications.service'; then
            systemctl restart consumable-notifications || true
            systemctl restart consumable-backup.timer || true
        else
            systemctl disable consumable-notifications consumable-backup.timer || true
        fi
        nginx -t && systemctl reload-or-restart nginx || true
        printf 'Release failed. Previous code and database were restored from %s.\n' "$release_backup" >&2
    elif [[ "$paused" == 1 ]]; then
        systemctl start consumable-assistant || true
        systemctl start consumable-notifications consumable-backup.timer || true
    fi
    exit "$result"
}
trap rollback ERR

if [[ ! -f /etc/lab-management/push/vapid-private.pem ]]; then
    VAPID_KEY_DIRECTORY=/etc/lab-management/push "$python_bin" "$release_dir/scripts/generate_push_keys.py" > "$release_backup/push-public-settings.txt"
fi
chown root:"$service_user" /etc/lab-management/push/*
chmod 0640 /etc/lab-management/push/*
"$python_bin" "$release_dir/scripts/configure_environment.py" /etc/lab-management/environment

paused=1
systemctl stop consumable-backup.timer consumable-notifications consumable-assistant || true
"$python_bin" "$release_dir/scripts/backup_database.py" --database "$database" --directory "$release_backup"
snapshot_files=("$release_backup"/hourly-*.db)
cp -- "${snapshot_files[0]}" "$release_backup/database.db"
if [[ -d "$state_dir/photos" ]]; then tar -C "$state_dir" -czf "$release_backup/photos.tar.gz" photos; fi
changed=1

candidate="$state_dir/release-check-$(date -u +%Y%m%dT%H%M%SZ).db"
install -m 0600 -o "$service_user" -g "$service_user" "$release_backup/database.db" "$candidate"
runuser -u "$service_user" -- "$python_bin" "$release_dir/scripts/migrate_database.py" --database "$candidate" --report "$state_dir/migration-check.json"
runuser -u "$service_user" -- "$python_bin" "$release_dir/scripts/migrate_database.py" --database "$database" --report "$state_dir/migration-latest.json"
cp "$state_dir/migration-latest.json" "$release_backup/migration.json"

for unit in consumable-assistant.service consumable-notifications.service consumable-backup.service consumable-backup.timer; do
    install -m 0644 "$release_dir/deploy/$unit" "/etc/systemd/system/$unit"
done
systemd-analyze verify /etc/systemd/system/consumable-assistant.service /etc/systemd/system/consumable-notifications.service /etc/systemd/system/consumable-backup.service /etc/systemd/system/consumable-backup.timer
if [[ ! -f /etc/nginx/snippets/lab-management-auth.conf ]]; then install -m 0644 /dev/null /etc/nginx/snippets/lab-management-auth.conf; fi
install -m 0644 "$release_dir/deploy/nginx.conf" /etc/nginx/sites-available/lab-management
ln -sfn /etc/nginx/sites-available/lab-management /etc/nginx/sites-enabled/lab-management
if [[ -L /etc/nginx/sites-enabled/default ]]; then unlink /etc/nginx/sites-enabled/default; fi
nginx -t
ln -sfn "$release_dir" "$app_root/current.next"
mv -Tf "$app_root/current.next" "$app_root/current"
systemctl daemon-reload
systemctl enable consumable-assistant consumable-notifications consumable-backup.timer nginx
systemctl restart consumable-assistant
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8000/api/stats > "$release_backup/health-stats.json"
curl --fail --silent --show-error --max-time 120 -X POST http://127.0.0.1:8000/api/ai/check > "$release_backup/health-ai.json"
"$python_bin" -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "available", "AI availability check failed"' "$release_backup/health-ai.json"
systemctl restart consumable-notifications consumable-backup.timer
systemctl start consumable-backup.service
systemctl reload-or-restart nginx
changed=0
trap - ERR
printf 'Deployment and AI checks passed. Backup: %s\nCampus, VPN, trusted HTTPS and authorized-device push checks remain required.\n' "$release_backup"
