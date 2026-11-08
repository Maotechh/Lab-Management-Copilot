# Server deployment

The release uses FastAPI and SQLite for all business rules, a separate loopback DSH service for conversation and official SenseVoice recognition, and a separate rootless Podman account for temporary analysis. Nginx listens only on the private campus address. There is no login. Preserve the existing campus and school VPN access restrictions.

## Build and identify the release

Use Node.js `24.22.2`, `pnpm@11.28.5`, a committed application revision and a committed DSH revision based on the designated official source. On Linux x86_64, place the pinned Node distribution on PATH and run:

```sh
python scripts/build_dsh_runtime.py --source /path/to/committed/deepseek-harness --destination /path/to/application/runtime --node-prefix /path/to/node-v24.22.2-linux-x64
```

The build installs the frozen source lock, compiles the DSH artifacts, creates a dependency-only deployment importer, checks that no registry resolution changed, and materializes the dependency files. The explicit profile contains only the laboratory bundle. Do not copy a developer's `node_modules` or home directory.

Copy the verified official voice files into `runtime/voice/sensevoice-onnx/model.int8.onnx`, `runtime/voice/sensevoice-onnx/tokens.txt` and `runtime/voice/silero/silero_vad.onnx`. Then run:

```sh
python scripts/release_manifest.py create --root /path/to/application
```

The manifest records both commits, the exact Node.js and pnpm versions, lockfile hashes and release file checksums. It also validates the pinned voice models. The generated runtime, manifest and deployment archives are local release artifacts and must not be committed. Preserve the immutable release directory for restoration.

## Install

Ubuntu requires Python with venv support, Nginx, rootless Podman, `uidmap`, systemd user sessions and cgroup v2. Install the private-IP server certificate, server key and public root certificate as `/etc/lab-management-tls/server.crt`, `server.key` and `lab-root-ca.crt`. Keep the CA signing key outside the server and preserve it encrypted under the maintainer's control. The CA signing key is not an application backup input.

Extract the identified application and runtime into `/opt/lab-management/releases/<release-id>`. For a first installation only, provide the existing inventory and environment under `bootstrap/`; never reimport the original three attachments. Run:

```sh
bash deploy/install.sh
```

Dependencies are prepared before business writes pause. The installer creates and verifies a complete previous-version snapshot, restores it into an isolated directory, migrates that copy and checks preservation before migrating production. A later failure restores matching code, application state, DSH state and settings. A failed restoration keeps durable writers stopped.

The application account owns `/var/lib/lab-management`. The DSH account owns `/var/lib/lab-dsh`, including Session events and image attachments. Its replaceable profile is recreated under `/run/lab-dsh`; dependencies remain in the release directory. DSH cannot read the production database or application credentials. The analysis account owns `/var/lib/lab-analysis` and its rootless image storage; it cannot access either production data directory.

The application and DSH use the school `deepseek-pro` endpoint at `POST http://10.15.171.204:30969/v1/responses`, with high reasoning effort and `store: false`. The endpoint requires no API key; deployment clears the application's old key and never copies it into DSH. The DSH transport uses a nonsecret placeholder authorization header required by its OpenAI client. Unverified alternative model configurations fail before any request. VAPID keys in `/etc/lab-management/push` are reused. The public entry is `https://10.15.21.115`; HTTP presents certificate installation instructions. The notification contact is the repository's issues URL. Push payloads contain generic reminders only.

## Complete backup and restoration

The hourly timer pauses the application, notification worker and DSH, creates one complete verified snapshot, and resumes previously active writers. Snapshots include database rows, uploaded attachments, location photos, exports, DSH events, configuration, TLS server files and recorded service configuration. Existing backups are retained. Monitor available disk space; complete snapshots are not automatically deleted.

```sh
sudo /opt/lab-management/current/.venv/bin/python /opt/lab-management/current/scripts/consistent_backup.py create
sudo /opt/lab-management/current/.venv/bin/python /opt/lab-management/current/scripts/consistent_backup.py restore --snapshot /var/backups/lab-management/<snapshot>
```

Restore requires the recorded immutable release directory. The coordinator verifies its files, stops writers, restores all named roots, selects the matching release, restores service configuration, and resumes the recorded services. Existing replaced roots remain with `.restore-previous` names. Preserve or move those directories before another restoration. For a rehearsal on an isolated destination, use `scripts/full_backup.py restore --destination-root <directory> --services-stopped` instead; it never changes the selected production release.

## Required actual verification

The additional lifecycle and history-export checks use newly created fixture databases beneath the selected application's `data/acceptance/` directory. They never accept a production URL or reuse an existing database. Run a dedicated DSH instance on loopback 8767 with its callback on 8001, using the same identified runtime as the release. Run these checks sequentially because each creates a temporary application on 8001:

```sh
python scripts/acceptance_school_lifecycle.py --database data/acceptance/lifecycle-<unique-id>.db --report /var/backups/lab-management/verification/lifecycle-<unique-id>.json
python scripts/acceptance_history_export.py --database data/acceptance/history-<unique-id>.db --report /var/backups/lab-management/verification/history-<unique-id>.json
```

Lifecycle checks cover twenty scenarios three times, inspect previews before confirming, compare business-state digests and verify exact quantities and repeated confirmations. The export check repeats three times and verifies filters, the downloaded workbook, exact event identifiers and decimal quantities without changing business data. Preserve failed and successful reports separately.

Installation checks actual school-model availability, loopback DSH, rootless isolation/resource limits and HTTPS from the server. These checks do not establish teacher-device certificate trust, campus/VPN access, microphone permission or background push delivery. Verify those on a computer, iPhone Safari and Android Chrome, recording system/browser versions. Both phones must record, upload a photo, share a conversation, confirm a plan and receive a generic test notification after the page is closed and the screen locked. Verify that clicking the notification displays the campus detail page. iPhone Web Push requires a supported iOS version and installation on the home screen.

The attachment inventory remains awaiting teacher verification. Physical stock confirmation, actual reagent-system integration and actual floorplan production are outside this release's completion conditions. Deployment evidence must distinguish passed checks from work that remains.

### Selected-device notification checks

Use `scripts/check_device_notification.py` as the application account with its service environment. With no arguments it lists device IDs, actor names and subscription status without exposing provider URLs or keys. `--device-id ID --delay-seconds 45` creates a marked generic test for that device only, allowing the teacher to close the page and lock the screen. `--event-id ID` reads the delivery state. This does not change inventory. For example:

```sh
sudo systemd-run --pipe --wait --collect \
  --property=User=lab-management \
  --property=EnvironmentFile=/etc/lab-management/environment \
  --working-directory=/opt/lab-management/current \
  /opt/lab-management/current/.venv/bin/python scripts/check_device_notification.py
```

Append the selected action after the script path. `sent` means the notification provider accepted the request; record actual device receipt and navigation separately. An HTTP status in the delivery error helps distinguish expired subscriptions from provider rejection. A connection timeout requires checking the server's access to the actual provider. Follow [PHONE-VALIDATION.md](PHONE-VALIDATION.md) for the device checks.

Run `node scripts/check_notification_worker.cjs` to verify generic notification contents, explicitly marked tests and navigation with both open and closed pages. This uses the actual worker source with browser APIs substituted in memory; actual locked-screen phone delivery remains a separate required check.
