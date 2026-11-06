# Deployment Record: 2026-11-07

## Release

- Repository: `Maotechh/Lab-Management-Copilot`, branch `master`.
- Deployed source commit: `1b91e8854b2afc32e1fa933742e65203bfde5852`.
- Installed release: `/opt/lab-management/releases/20261107-1b91e88`.
- GitHub Actions run [42250545192](https://github.com/Maotechh/Lab-Management-Copilot/actions/runs/42250545192) passed.
- `deploy/install.sh` completed. The application, notification worker, backup timer and Nginx are active.
- Release backup: `/var/backups/lab-management/release-20261106T161643Z`. The SQLite integrity and restore checks passed. Migration preserved all 894 inventory items and 1,073 transactions. The migration report identifies the existing negative source quantity for teacher review.

## Source History Repair

- Pre-repair backup: `/var/backups/lab-management/source-rows-20261106T162429Z/hourly-20261106T162520Z.db`, with integrity and restore checks passed.
- `scripts/backfill_source_rows.py` restored 44 duplicate spreadsheet source rows, bringing `source_rows` to 1,117.
- The 44 rows retain their `duplicate_of` relationship. Inventory-item and transaction row counts and hashes match the pre-repair backup. The database integrity check returned `ok`.
- No inventory balances were changed by this repair.

## Campus Verification

- Campus HTTP at `http://10.15.21.115/` returned HTTP 200. Nginx listens on `10.15.21.115:80`; Uvicorn remains bound to `127.0.0.1:8000`.
- `/api/config` reports `profile=production`, `inventory_confirmed=false` and `notifications_configured=false`.
- The actual AI health check reports the institutional GenAI provider and `deepseek-pro` available.
- A campus chat query for 100 mL beakers returned an inventory response from the institutional GenAI provider. It did not create an inventory transaction.
- The served `app.js` SHA-256 is `d6bfba8d31e3540d1fffa42f14c0dded2285d3f0578fa51c347678d5b9a2ed54`, matching the deployed release file.

## Outstanding External Verification

- The three spreadsheet attachments have not been confirmed as actual stock by a teacher. The application continues to show the inventory review notice; do not treat the imported quantities or locations as teacher-verified records.
- School domain, trusted HTTPS certificate and institutional notification contact have not been provided. Notifications remain unconfigured. HTTPS, school VPN access and background delivery to a real phone remain unverified.
