# Automatic deployment verification, 2026-11-10

The first verified automatic production deployment installed application commit
`9850bc4725d7e56f37888d9937988670d581d83a` from the production branch
`codex/dsh-lab-delivery`. Its successful CI run is
https://github.com/Maotechh/Lab-Management-Copilot/actions/runs/42761381038.
The release is `/opt/lab-management/releases/auto-20261110T052943690294Z-9850bc4725d7`.

The installer retained and verified the complete snapshot at
`/var/backups/lab-management/release-20261110T053048.906857596Z` and rehearsed
restoration. Its reports include checks of the institutional `deepseek-pro`
Responses API, HTTPS and the analysis environment. HTTP and HTTPS returned 200
after deployment. The application, DSH, nginx and automatic update timer were
active. The business-table digests matched the snapshot; service health,
provider checks and migration bookkeeping are excluded from this comparison.
Checking the same commit again returned `current` without reinstalling it.

Releases and backups were moved to the server's data volume with verified copies
and bind mounts at their original paths. The pre-move snapshot is
`/var/backups/lab-management/storage-20261110T052736Z`. Existing releases and
backups were retained.

This documentation commit also triggers automatic deployment after CI passes.
The currently installed application commit is recorded in
`/etc/lab-management/release.json`; deployment results and CI references are
recorded in `/var/lib/lab-management-autoupdate/last-success.json`. See
[AUTOUPDATE.md](AUTOUPDATE.md) for operation, retry and restoration procedures.
