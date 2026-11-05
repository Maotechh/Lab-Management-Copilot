# Deployment Record: 2026-11-05

## Source And Checks

The management-system source is synchronized to `Maotechh/Lab-Management-Copilot`, branch `master`, commit `76e7d799db92ced82619d707d0bb34b76ac3cead`.

GitHub Actions run [41977626717](https://github.com/Maotechh/Lab-Management-Copilot/actions/runs/41977626717) passed: Ruff, original-spreadsheet smoke test, 37 workflow tests, JavaScript syntax and installer syntax. The same workflow tests and smoke test also pass locally in an isolated environment. The updated repository instructions require future management-system changes to be synchronized after validation.

## Campus Verification

- Campus HTTP at `http://10.15.21.115/` is now reachable.
- Inventory statistics return 894 items, 1073 original source rows and 1073 original transactions.
- A real AI check reports configured, required and available, using `deepseek-pro`.
- An actual preview request for `100mL烧杯用了2个不放回` returns `consume`, quantity 2, with engine `deepseek-pro`. No stock write was submitted.
- Browser checks at 1440x1000 and 390x844 visit all ten application tabs without page-level horizontal overflow or JavaScript errors. These checks do not modify stock.
- The served `app.js` matches the committed file, SHA-256 `8ab529cf2b3f8b022aac105cdf05de3a768bf4d6e6bd499d2c925d63eccc71d1`.

## HTTPS And Other External Conditions

The campus HTTPS connection to port 443 times out. Server inspection shows Nginx listening on `10.15.21.115:80`, with no 443 listener and no configured certificate directory. Upstream port approval therefore does not yet establish HTTPS service. A school domain and matching trusted certificate are required before applying `nginx-https.conf.example`.

School VPN and authorized-phone background notification delivery remain unverified. Retain the campus/VPN scope and access without login.

## Release Execution

The existing `/opt/lab-management/releases/20261022-02` release is running, and the application, notification worker, backup timer and Nginx were active when checked. The new source commit is published and validated. New release installation is pending restoration of the authorized one-time bastion SSH session; no successful new installation is claimed by this record yet.
