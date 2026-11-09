#!/usr/bin/env bash
set -Eeuo pipefail
umask 0027

[[ "$EUID" -eq 0 ]] || { printf 'Run this installer as root.\n' >&2; exit 1; }
release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
app_root=/opt/lab-management
state_dir=/var/lib/lab-management
service_user=lab-management
release_backup="/var/backups/lab-management/release-$(date -u +%Y%m%dT%H%M%S.%NZ)"
previous_release="$(readlink -f "$app_root/current" || true)"
changed=0
paused=0

[[ "$release_dir" == "$app_root"/releases/* ]] || { printf 'Extract the release beneath %s/releases first.\n' "$app_root" >&2; exit 1; }
[[ ! -e "$app_root/current" || -L "$app_root/current" ]] || { printf 'The current release must be a symbolic link.\n' >&2; exit 1; }
for executable in python3 nginx openssl podman newuidmap newgidmap loginctl curl flock; do
    command -v "$executable" >/dev/null || { printf 'Missing required dependency: %s\n' "$executable" >&2; exit 1; }
done
python3 "$release_dir/scripts/release_manifest.py" verify --root "$release_dir"
for name in server.crt server.key lab-root-ca.crt; do
    [[ -f "/etc/lab-management-tls/$name" ]] || { printf 'Install the verified IP-SAN TLS file first: %s\n' "$name" >&2; exit 1; }
done
openssl verify -CAfile /etc/lab-management-tls/lab-root-ca.crt -verify_ip 10.15.21.115 /etc/lab-management-tls/server.crt
openssl x509 -in /etc/lab-management-tls/server.crt -checkend 604800 -noout
cmp <(openssl x509 -in /etc/lab-management-tls/server.crt -pubkey -noout) <(openssl pkey -in /etc/lab-management-tls/server.key -pubout)

chown -hR root:root "$release_dir"
if ! id "$service_user" >/dev/null 2>&1; then
    useradd --system --home-dir "$state_dir" --shell /usr/sbin/nologin "$service_user"
fi
install -d -m 0750 -o "$service_user" -g "$service_user" "$state_dir"
install -d -m 0750 -o root -g "$service_user" /etc/lab-management /etc/lab-management/push
install -d -m 0700 /var/backups/lab-management
if [[ ! -f "$state_dir/consumables.db" ]]; then
    install -m 0640 -o "$service_user" -g "$service_user" "$release_dir/bootstrap/consumables.db" "$state_dir/consumables.db"
fi
if [[ ! -f /etc/lab-management/environment ]]; then
    install -m 0640 -o root -g "$service_user" "$release_dir/bootstrap/environment" /etc/lab-management/environment
fi
[[ -e "$release_dir/data" ]] || ln -s "$state_dir" "$release_dir/data"
python3 -m venv "$release_dir/.venv"
python_bin="$release_dir/.venv/bin/python"
"$python_bin" -m pip install -r "$release_dir/requirements.txt"
"$python_bin" -m pip freeze > "$release_dir/installed-requirements.txt"
chown -hR root:"$service_user" "$release_dir"
chmod -R g+rX "$release_dir"
chmod 0755 "$release_dir" "$release_dir/runtime" "$release_dir/.venv" "$release_dir/.venv/bin"
# The two isolated users can read code, never the application's state or settings.
chmod -R o+rX "$release_dir/consumable_assistant" "$release_dir/scripts" "$release_dir/runtime" "$release_dir/.venv"
# Root may invoke the installer from /root, which the analysis account cannot
# traverse. Podman's process must inherit an accessible working directory.
cd "$release_dir"

rollback() {
    local result=$?
    trap - ERR
    if [[ "$changed" == 1 ]]; then
        systemctl stop consumable-notifications consumable-assistant consumable-dsh consumable-analysis || true
        flock -u 9 || true
        "$python_bin" "$release_dir/scripts/consistent_backup.py" restore --snapshot "$release_backup" || {
            printf 'Automatic restoration failed. Keep all writers stopped. Verified snapshot: %s\n' "$release_backup" >&2
            exit "$result"
        }
        printf 'Previous code, inventory, conversations, files and settings restored from %s.\n' "$release_backup" >&2
    elif [[ "$paused" == 1 ]]; then
        if [[ -n "$previous_release" ]]; then
            systemctl start consumable-assistant consumable-notifications || true
        fi
    fi
    systemctl start consumable-backup.timer || true
    exit "$result"
}
trap rollback ERR

# Finish any previous backup before pausing its schedule and all durable writers.
systemctl stop consumable-backup.timer || true
# A Type=oneshot service remains "activating" throughout its backup. Waiting
# only for "active" lets installation contend with that backup's release lock.
backup_wait_deadline=$((SECONDS + 900))
while [[ "$(systemctl show --property=ActiveState --value consumable-backup.service)" =~ ^(active|activating|reloading|deactivating)$ ]]; do
    if (( SECONDS >= backup_wait_deadline )); then
        printf 'The scheduled backup has not finished after 15 minutes. Installation stopped before changing the release.\n' >&2
        false
    fi
    sleep 1
done
paused=1
"$python_bin" "$release_dir/scripts/consistent_backup.py" create --snapshot "$release_backup" --leave-stopped
exec 9>/run/lab-management-release.lock
flock -n 9
changed=1

candidate_dir="$release_backup/migration-candidate"
install -d -m 0700 "$candidate_dir"
cp "$state_dir/consumables.db" "$candidate_dir/consumables.db"
"$python_bin" "$release_dir/scripts/migrate_database.py" --database "$candidate_dir/consumables.db" --report "$release_backup/migration-candidate.json" > "$release_backup/migration-candidate-summary.json"
# Rehearse extraction and restoration into an isolated directory before migration.
"$python_bin" "$release_dir/scripts/full_backup.py" restore --snapshot "$release_backup" --destination-root "$release_backup/restore-rehearsal" --services-stopped
"$python_bin" "$release_dir/scripts/migrate_database.py" --database "$state_dir/consumables.db" --report "$release_backup/migration.json" > "$release_backup/migration-summary.json"
chown -R "$service_user:$service_user" "$state_dir"

if ! id lab-dsh >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/lab-dsh --shell /usr/sbin/nologin lab-dsh
fi
if ! getent group lab-analysis-client >/dev/null; then groupadd --system lab-analysis-client; fi
if ! id lab-analysis >/dev/null 2>&1; then
    useradd --system --gid lab-analysis-client --home-dir /var/lib/lab-analysis --shell /usr/sbin/nologin lab-analysis
fi
usermod -a -G lab-analysis-client "$service_user"
install -d -m 0750 -o lab-dsh -g lab-dsh /var/lib/lab-dsh
install -d -m 0750 -o root -g lab-dsh /etc/lab-dsh
install -d -m 0700 -o lab-analysis -g lab-analysis-client /var/lib/lab-analysis
install -d -m 0750 -o root -g lab-analysis-client /etc/lab-analysis
analysis_uid="$(id -u lab-analysis)"
# Existing subordinate-ID ranges are retained; allocate only when absent.
"$python_bin" "$release_dir/scripts/configure_analysis_user.py"
loginctl enable-linger lab-analysis
systemctl start "user@$analysis_uid.service"
install -d -m 0750 -o lab-analysis -g lab-analysis-client /run/lab-analysis
install -d -m 0700 -o lab-analysis -g lab-analysis-client /var/lib/lab-analysis/.config /var/lib/lab-analysis/.config/containers
cat > /var/lib/lab-analysis/.config/containers/storage.conf <<'CONFIG'
[storage]
driver = "overlay"
runroot = "/run/lab-analysis/containers"
graphroot = "/var/lib/lab-analysis/.local/share/containers/storage"
CONFIG
chown lab-analysis:lab-analysis-client /var/lib/lab-analysis/.config/containers/storage.conf
chmod 0600 /var/lib/lab-analysis/.config/containers/storage.conf
image_digest=sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177
analysis_image="docker.io/library/python@$image_digest"
alternate_analysis_image="docker.1ms.run/library/python@$image_digest"
analysis_command=(runuser -u lab-analysis -- env "XDG_RUNTIME_DIR=/run/user/$analysis_uid" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$analysis_uid/bus" podman --cgroup-manager=systemd)
if ! "${analysis_command[@]}" image exists "$analysis_image" && ! "${analysis_command[@]}" image exists "$alternate_analysis_image"; then
    timeout --kill-after=15s 180s "${analysis_command[@]}" pull "$analysis_image" || timeout --kill-after=15s 180s "${analysis_command[@]}" pull "$alternate_analysis_image"
fi
# Config ID is shared by the identical official manifest at the alternate registry.
analysis_id="$("${analysis_command[@]}" image inspect --format '{{.Id}}' "$analysis_image" 2>/dev/null || "${analysis_command[@]}" image inspect --format '{{.Id}}' "$alternate_analysis_image")"
[[ "$analysis_id" == 78976a69c067520ef8be34cde307ef9f6fea6dcefa50edb57039773d241ca0db ]] || { printf 'Unexpected analysis image config.\n' >&2; false; }
printf 'LAB_ANALYSIS_STATE=/var/lib/lab-analysis\nLAB_ANALYSIS_SOCKET=/run/lab-analysis/broker.sock\nLAB_ANALYSIS_IMAGE=%s\n' "$analysis_id" > /etc/lab-analysis/environment
chown root:lab-analysis-client /etc/lab-analysis/environment
chmod 0640 /etc/lab-analysis/environment

if [[ ! -f /etc/lab-management/push/vapid-private.pem ]]; then
    VAPID_KEY_DIRECTORY=/etc/lab-management/push "$python_bin" "$release_dir/scripts/generate_push_keys.py" > "$release_backup/push-public-settings.txt"
fi
chown root:"$service_user" /etc/lab-management/push/*
chmod 0640 /etc/lab-management/push/*
"$python_bin" "$release_dir/scripts/configure_environment.py" /etc/lab-management/environment
cat > /etc/lab-dsh/environment <<CONFIG
LAB_CALLBACK_URL=http://127.0.0.1:8000
LAB_DSH_PORT=8766
LAB_VOICE_MODEL_DIR=$release_dir/runtime/voice/sensevoice-onnx
LAB_VOICE_VAD_PATH=$release_dir/runtime/voice/silero/silero_vad.onnx
CONFIG
chown root:lab-dsh /etc/lab-dsh/environment
chmod 0640 /etc/lab-dsh/environment
"$python_bin" - "$release_dir" <<'PY'
import json, pathlib, sys
release = pathlib.Path(sys.argv[1])
manifest = json.loads((release / 'release-manifest.json').read_text())
manifest['application_release'] = str(release)
path = pathlib.Path('/etc/lab-management/release.json')
path.write_text(json.dumps(manifest, indent=2) + '\n')
path.chmod(0o640)
PY
chown root:"$service_user" /etc/lab-management/release.json

for unit in consumable-assistant.service consumable-notifications.service consumable-dsh.service consumable-analysis.service consumable-backup.service consumable-backup.timer; do
    sed "s/@ANALYSIS_UID@/$analysis_uid/g" "$release_dir/deploy/$unit" > "/etc/systemd/system/$unit"
    chmod 0644 "/etc/systemd/system/$unit"
done
[[ -f /etc/nginx/snippets/lab-management-auth.conf ]] || install -m 0644 /dev/null /etc/nginx/snippets/lab-management-auth.conf
install -d -m 0755 /var/www/lab-management-trust
install -m 0644 /etc/lab-management-tls/lab-root-ca.crt /var/www/lab-management-trust/lab-root-ca.crt
fingerprint="$(openssl x509 -in /etc/lab-management-tls/lab-root-ca.crt -sha256 -fingerprint -noout | cut -d= -f2)"
sed "s/@@CA_FINGERPRINT@@/$fingerprint/g" "$release_dir/deploy/trust.html" > /var/www/lab-management-trust/index.html
chmod 0644 /var/www/lab-management-trust/index.html
install -m 0644 "$release_dir/deploy/nginx.conf" /etc/nginx/sites-available/lab-management
ln -sfn /etc/nginx/sites-available/lab-management /etc/nginx/sites-enabled/lab-management
if [[ -L /etc/nginx/sites-enabled/default ]]; then unlink /etc/nginx/sites-enabled/default; fi
nginx -t
if command -v ufw >/dev/null && LC_ALL=C ufw status | grep -q '^Status: active'; then
    ufw allow from 10.0.0.0/8 to 10.15.21.115 port 443 proto tcp comment 'Lab management campus and VPN HTTPS'
fi
ln -sfn "$release_dir" "$app_root/current.next"
mv -Tf "$app_root/current.next" "$app_root/current"
# ExecStart paths use current, so validate after selecting the candidate. The
# rollback trap restores the previous selection and units if validation fails.
systemd-analyze verify /etc/systemd/system/consumable-{assistant,notifications,dsh,analysis,backup}.service /etc/systemd/system/consumable-backup.timer
systemctl daemon-reload
systemctl enable consumable-assistant consumable-notifications consumable-dsh consumable-analysis consumable-backup.timer nginx
systemctl restart consumable-analysis consumable-dsh
curl --fail --silent --show-error --retry 20 --retry-connrefused --retry-delay 1 http://127.0.0.1:8766/health > "$release_backup/health-dsh.json"
systemctl restart consumable-assistant
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8000/api/stats > "$release_backup/health-stats.json"
curl --fail --silent --show-error --max-time 120 -X POST http://127.0.0.1:8000/api/ai/check > "$release_backup/health-ai.json"
"$python_bin" -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "available", "School model unavailable"' "$release_backup/health-ai.json"
"$python_bin" "$release_dir/scripts/verify_analysis_sandbox.py" --report "$release_backup/analysis-verification.json"
systemctl restart consumable-notifications
systemctl reload-or-restart nginx
curl --fail --silent --show-error --cacert /etc/lab-management-tls/lab-root-ca.crt https://10.15.21.115/api/stats > "$release_backup/health-https.json"
flock -u 9
"$python_bin" "$release_dir/scripts/consistent_backup.py" create --snapshot "$release_backup/installed-snapshot"
systemctl restart consumable-backup.timer
changed=0
trap - ERR
printf 'Release installed and server checks passed. Verified previous snapshot: %s\nActual device trust, recording, VPN and background push checks remain required.\n' "$release_backup"
