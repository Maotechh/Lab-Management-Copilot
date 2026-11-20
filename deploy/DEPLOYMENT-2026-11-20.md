# Website and conversation recovery, 2026-11-20

HTTPS returned 502 while nginx remained active. The application, DSH and
notification services had been stopped during a failed automatic deployment.
The model check failed, then restoration refused to proceed because recovery
directories from an earlier restoration already existed. The backup timer was
also left stopped. Existing recovery material and backups were preserved.

The application model health check passed, but an actual DSH conversation
failed with HTTP 401 and no business tool calls. The school provider override
had been written to `cordis.yml`, which DSH recreates as an empty profile root
during startup. Consequently, the bundled obsolete model endpoint remained
selected. Profile preparation now writes the override to `cordis.patch.yml`,
applied after the laboratory bundle, and copies the ordinary empty root.

After restoring the writer services and applying the profile patch in the
volatile profile, HTTPS returned 200 and the actual school query passed:
run `09763888b9d343caa5707ab9ebb0860c`, tool `inventory_search`, with no inventory
writes by the verification actor. This check used the authenticated school
Responses gateway and the installed DSH runtime.

Repeated restorations now preserve previous roots under a distinct identifier,
prepare every replacement before switching directories, and reverse completed
directory switches if a later switch fails. Legacy recovery directories remain
untouched. The updater reports a previously failed selected candidate as a
failure requiring inspection, rather than reporting it as current. Backup logs
print concise version information instead of the complete release file index.

Validation covers repeated restoration, preservation of older recovery material,
an injected directory-switch failure, failed selected release detection and
the profile file distinction. Deployment continues through CI authorization and
`deploy/install.sh`, with a full snapshot and isolated restoration rehearsal.
The installed commit and actual deployment evidence are recorded in
`/etc/lab-management/release.json`,
`/var/lib/lab-management-autoupdate/last-success.json` and the corresponding
`/var/backups/lab-management/release-*` directory.
