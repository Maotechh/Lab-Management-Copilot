# Automatic updates from GitHub

The production branch is `codex/dsh-lab-delivery` in `Maotechh/Lab-Management-Copilot`. Pushing a commit to that branch runs `.github/workflows/ci.yml`. Master/main and pull requests also run checks, but do not authorize production deployment.

After every required check succeeds, the workflow publishes an annotated, immutable `lab-ci/<full-commit>` tag containing the repository, branch, commit and CI run URL. It never force-pushes. The server checks the latest branch commit and its matching authorization every five minutes. It uses outbound public Git, so there is no inbound deployment port, GitHub credential on the server, self-hosted runner or GitHub API rate-limit dependency. Permission to push to the production branch or create `lab-ci/` tags is production deployment authority; keep those repository permissions restricted to maintainers. Do not create deployment tags manually.

The server stages an identified immutable release and verifies the complete current release before copying its pinned DSH/voice runtime. `deploy/runtime.json` records the required DSH commit, Node.js and pnpm versions and lockfile checksums. Application commits normally reuse those verified runtime files. A changed DSH pin builds the exact DSH commit using `scripts/build_dsh_runtime.py`, the frozen dependency lock and local voice files. Node.js/pnpm toolchain changes require provisioning their specified versions first. There is no floating DSH branch deployment.

Immediately before installing, the coordinator checks that the candidate is still the latest branch commit. It invokes `deploy/install.sh`, which preserves a complete verified previous snapshot, rehearses restoration, migrates an isolated copy, installs, checks the actual school model and HTTPS, and restores the previous matching code and data on failure. Existing immutable releases and backups are retained. At least 2 GiB of free disk space must remain after staging. Insufficient disk space stops the attempt before production writes pause.

Only one automatic updater runs at a time. An unsuccessful production installation is not repeated every five minutes: a new commit or an explicit retry is required. Network, disk or preparation failures can retry on the next scheduled check. A superseded commit does not install. The timer does not cancel an installation when another commit appears; it processes the latest commit on its next check. A successful repeat poll makes no changes.

## One-time setup

From a reviewed checkout of the implementation, as root on the existing server:

```sh
bash deploy/install-autoupdate.sh
systemctl start consumable-autoupdate.service
```

The setup installs the coordinator under `/usr/local/lib/lab-management-autoupdate`, configuration under `/etc/lab-management-autoupdate`, and a systemd service/timer. The next successful application installation refreshes these files. Complete backups include their configuration, program and units. No production database, credentials, uploaded photos or backups enter GitHub.

## Observe and control

```sh
systemctl list-timers consumable-autoupdate.timer
journalctl -u consumable-autoupdate.service -n 100 --no-pager
cat /var/lib/lab-management-autoupdate/status.json
cat /var/lib/lab-management-autoupdate/last-success.json
cat /etc/lab-management/release.json
```

`status.json` reports waiting-for-CI, ready, installing, deployed, current, superseded or failure state. CI evidence is the matching GitHub Actions URL recorded in the authorization and successful deployment. Installer backup and check reports remain under `/var/backups/lab-management/release-*`. The status file does not substitute for device notification, certificate trust or VPN verification.

To inspect without deploying, or retry after correcting an operational failure:

```sh
python3 /usr/local/lib/lab-management-autoupdate/autoupdate.py --check
python3 /usr/local/lib/lab-management-autoupdate/autoupdate.py --retry
```

To pause future deployments, including before restoring a previous backup:

```sh
systemctl disable --now consumable-autoupdate.timer
```

Wait for any active `consumable-autoupdate.service` to finish before restoration; stopping the timer does not interrupt an ongoing installation. Restore with the complete snapshot instructions in [README.md](README.md). Keep automatic updates paused until the production branch points to the intended fixed version, otherwise the restored version will be upgraded again. Re-enable with `systemctl enable --now consumable-autoupdate.timer`.
