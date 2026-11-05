# Server Deployment

The application runs as the `lab-management` system user under systemd. Nginx listens on the server's private IPv4 address and proxies to `127.0.0.1:8000`.

On Ubuntu 24.04, install `python3-venv`, `nginx`, and `libreoffice-calc`. Extract the current application, requirements, attachments, and this deployment directory into `/opt/lab-management/releases/<release-id>`.

For the initial deployment, include a consistent SQLite backup as `bootstrap/consumables.db` and the existing application environment as `bootstrap/environment`. Treat this directory and transfer archives as secrets. Run `bash deploy/install.sh` as root from the extracted release. Remove bootstrap files after installation. Subsequent deployments preserve the existing database and environment.

- Application data: `/var/lib/lab-management/consumables.db`
- Environment: `/etc/lab-management/environment`
- Initial database backup: `/var/backups/lab-management/initial-consumables.db`
- Logs: `journalctl -u consumable-assistant`
- Service status: `systemctl status consumable-assistant nginx`
- Optional Nginx access authentication: `/etc/nginx/snippets/lab-management-auth.conf`

Access remains without login, as requested. The installer preserves the existing firewall. Permit HTTP/HTTPS only from approved campus and school VPN source networks. Do not expose Uvicorn or add a public IPv6 listener. Existing campus routing has not yet passed client verification.

The V2 installer installs dependencies before pausing writes. It creates a verified release snapshot, checks migration on a copy, migrates the actual database, validates systemd/Nginx and requires a successful real AI health check. A failure restores the previous code, environment and database snapshot. Do not change the current symlink alone across schema versions.

Additional units are `consumable-notifications.service`, `consumable-backup.service` and `consumable-backup.timer`. The worker checks AI every five minutes and processes persistent generic Web Push jobs. The backup timer runs hourly, retaining seven days of hourly databases and thirty days of daily databases/photos. Every database backup includes a test restoration and row-count/integrity check.

For a manual restore, stop the application, notification worker and backup timer, then run `scripts/restore_database.py --snapshot <release-backup>/database.db --database /var/lib/lab-management/consumables.db --services-stopped` using the release Python. Select the matching code release before restarting services. Restore the matching `photos.tar.gz` when recovering photos onto a replacement machine.

Protected environment configuration is preserved. `GENAI_REQUIRED=1` is enforced. AI requests use the existing institutional GenAI endpoint first, then the built-in school endpoints in this order: Qwen (`qwen-instruct`), GLM (`glm-chat`), DeepSeek (`deepseek-pro`) and Kimi (`kimi`). Set `GENAI_FALLBACK_PROVIDERS_JSON` only when the institution changes an endpoint or model. The ASR endpoint is not used because the application has no voice input. Check `/api/ai/status` and `journalctl -u consumable-notifications`; use `/api/ai/check` for a real request, limited to one health check per minute per process. Campus connectivity and successful structured output must be verified on the deployed host.

VAPID signing keys are generated once in `/etc/lab-management/push`, with access restricted to root and the service group. Set `VAPID_SUBJECT=mailto:<institutional-contact>` and `PUBLIC_BASE_URL=https://<school-domain>`. A school DNS record, trusted certificate and outbound access to browser push providers are required. Apply the HTTPS Nginx example only after replacing its domain/certificate values. Confirm a notification on an authorized phone with the page closed.

See `ACCEPTANCE-V2.md` for the fourteen-item evidence and outstanding real-network/device requirements. Generated migration reports include each item's original quantity and new states and must remain protected because they contain business data.
