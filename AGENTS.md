# Repository work

- Keep management-system changes synchronized to `origin` (`Maotechh/Lab-Management-Copilot`) after validation, as explicitly requested by the owner. Never force-push. Preserve unrelated local documents and reports.
- Do not commit credentials, environment files, production databases, backup archives, uploaded location photos or one-time bastion connection scripts.
- Run `ruff check consumable_assistant scripts tests`, `python -m pytest -q tests`, `python scripts/smoke_test.py`, JavaScript syntax checks and `bash -n deploy/install.sh` for a release. Use isolated databases for tests.
- Deploy an identified Git commit through `deploy/install.sh`, retain its verified backup, check actual institutional AI availability and campus HTTP access, and record the deployed commit.
- Keep access without login and within campus/school VPN networks. Trusted HTTPS, VPN and real-phone background push require actual verification. Do not describe those conditions as complete before verification.
