#!/usr/bin/env bash
# Bootstrap the outbound-only updater once; future releases need no SSH session.
set -Eeuo pipefail
umask 0077
[[ "$EUID" -eq 0 ]] || { printf 'Run as root.\n' >&2; exit 1; }
release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
for executable in python3 git systemctl flock; do command -v "$executable" >/dev/null; done
install -d -m 0700 /etc/lab-management-autoupdate /var/cache/lab-management-autoupdate /var/lib/lab-management-autoupdate
install -d -m 0755 /usr/local/lib/lab-management-autoupdate
install -m 0644 "$release_dir/scripts/autoupdate.py" /usr/local/lib/lab-management-autoupdate/autoupdate.py.next
mv /usr/local/lib/lab-management-autoupdate/autoupdate.py.next /usr/local/lib/lab-management-autoupdate/autoupdate.py
if [[ ! -f /etc/lab-management-autoupdate/config.json ]]; then
    cat > /etc/lab-management-autoupdate/config.json <<'CONFIG'
{
  "repository": "Maotechh/Lab-Management-Copilot",
  "branch": "codex/dsh-lab-delivery",
  "workflow": ".github/workflows/ci.yml",
  "minimum_free_bytes": 2147483648
}
CONFIG
fi
for unit in consumable-autoupdate.service consumable-autoupdate.timer; do
    install -m 0644 "$release_dir/deploy/$unit" "/etc/systemd/system/$unit"
done
systemd-analyze verify /etc/systemd/system/consumable-autoupdate.service /etc/systemd/system/consumable-autoupdate.timer
systemctl daemon-reload
systemctl enable --now consumable-autoupdate.timer
printf 'Automatic updates enabled. Check: systemctl status consumable-autoupdate.timer\n'
