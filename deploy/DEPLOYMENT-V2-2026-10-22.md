# V2 Deployment Record: 2026-10-22

## Running Release

- Server: `10.15.21.115`, Ubuntu 24.04.2 LTS.
- Release: `/opt/lab-management/releases/20261022-02`.
- Current symlink: `/opt/lab-management/current`.
- Database: `/var/lib/lab-management/consumables.db`.
- Protected environment: `/etc/lab-management/environment`.
- Service user: `lab-management`.
- Application: `127.0.0.1:8000`, proxied by Nginx at `10.15.21.115:80`.
- Active and enabled: `consumable-assistant`, `consumable-notifications`, `consumable-backup.timer`, `nginx`.
- Access remains without login. Operator names are self-declared and unverified.

The existing V1 deployment record is retained in `DEPLOYMENT-2026-10-22.md`.

## Data And Recovery

The verified release snapshot is `/var/backups/lab-management/release-20261022T132928Z`. Migration preserves all original tables and creates versioned balances, operations and workflow records.

| Original table | Rows | Content SHA-256 |
| --- | ---: | --- |
| items | 894 | `d7dee9a52185c33c40b19bc3208d897d497f64c4d95a848aaa672cdaebb1f126` |
| transactions | 1073 | `c9c0c5c105a734361a2391b97164cb9a199f0fb964e45c282f4671458d1b1ebe` |
| source_rows | 1073 | `f956e1f851c2239ebc76a61c959c8752995fec153322b9c8b79a564e09eba50f` |

These hashes match the verified local production-data copy. Item 407 originally has quantity `-2.38000011444092`; it remains preserved, contributes zero available stock, and requires a physical count. No positive quantity was inferred.

An initial release installation failed because root-created virtualenv directories were inaccessible to the service group. Automatic rollback restored the previous code and database. The installer now assigns the complete release to the service group with group read/traverse permissions. The subsequent installation passed.

The backup service subsequently completed with result `success` and exit status `0`. Hourly backups retain seven days; daily backups retain thirty days. Each backup is checked by restoration and database integrity/row counts. Follow `README.md` when restoring a matching code/database release.

## Verification

- Production service-user workflow suite: **37 passed**, 3 warnings, 4.50 seconds.
- Updated spreadsheet import smoke test: passed locally and as the production service user.
- Migration/restore on a formal-data copy: passed with original table contents and SQL records preserved.
- Application, worker, backup timer and Nginx service checks: passed.
- Server-local Nginx inventory requests: passed.
- Playwright on an isolated data copy: teacher workflows, loan 20/return 18/damage 2, cleaning, partial procurement, uncertain-request retry, manual item matching, lesson drafts/templates/reservations/execution, duplicate upload, photos and history passed.
- Viewports 1440x1000, 390x844 and 844x390: no page-level horizontal overflow or JavaScript errors.
- Browser timezone America/New_York: Shanghai form time `2026-12-02 10:00` is stored as UTC `2026-12-02T02:00:00.000000+00:00`.

Browser tests only modify the isolated review database. The production inventory was not changed by these browser checks.

## AI

Production configuration requires actual AI requests. A real institutional `deepseek-pro` request parsed `100mL烧杯用了2个不放回` as consuming two items. Browser checks also verified that planned procurement requires review and never becomes a received-stock operation.

Requests have bounded retries, support configured institution-approved fallback models, preserve entered text on failure, and display service state. The independent worker checks actual AI availability every five minutes. systemd restarts failed application and worker processes. Current model health is available at `/api/ai/status`; `/api/ai/check` performs a real check with a one-minute limit per process.

The external model provider can still have an outage. These controls detect failures and support recovery; they cannot guarantee uninterrupted provider availability.

## Remaining External Requirements

A fresh direct campus request using `curl --noproxy '*'` timed out after 8006 ms with HTTP status `000`. A simultaneous capture on `ens160` observed zero packets for the tested application port. Server evidence is saved at `/root/lab-management-v2-access-check.log`. The request did not reach the server; the observations do not identify the upstream routing or access-control rule responsible.

The institution must verify access from approved campus client networks and school VPN egress networks to `10.15.21.115` on TCP 80 and, after certificate configuration, TCP 443. The server currently permits private `10.0.0.0/8` sources for HTTP. Confirm and explicitly configure any approved VPN range outside that network. Uvicorn remains bound to loopback.

A school domain and trusted HTTPS certificate are still required. The HTTPS Nginx template is prepared. VAPID signing keys have been generated with root/service-group access; `PUBLIC_BASE_URL` and a valid institutional `VAPID_SUBJECT` remain to be configured.

School VPN access and authorized-phone notification delivery after closing the page have not been tested. Browser push services also require permitted outbound HTTPS. Notifications contain generic text only.

All fourteen items remain subject to the external acceptance conditions in `ACCEPTANCE-V2.md`. This release is deployed, but complete production acceptance is not declared.
