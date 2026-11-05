# V2 Acceptance Record

This release retains FastAPI, SQLite, the current web interface, campus/VPN scope, and access without login. Operator names are self-declared and are not verified identities. Production acceptance remains incomplete until the external checks below pass.

## Implementation And Evidence

1. Inventory states: Decimal TEXT balances distinguish ready, borrowed, cleaning, repair, quarantine, owned, reserved and allocatable quantities. Reservations do not increase ownership. Exports and statistics use these balances.
2. Migration: numbered migrations preserve all original tables and freeze legacy item/location descriptions. Consistent backups precede migration. Negative quantities and uncertain legacy loans require a physical count. Each item has a migration difference record.
3. Units and specifications: confirmed conversions are maintained per item. Incompatible units and explicit capacity/specification mismatches are rejected. Ambiguous preparation rows require a chosen item.
4. Natural language and AI: actual institutional model requests are mandatory by default. Both model and local parsing receive intent checks for negation, plans, conditions and corrections. Three bounded attempts and configured fallback models are supported. No heuristic output is presented as a successful AI request.
5. Transactions and concurrency: explicit SQLite transactions, version checks, cumulative allocation, atomic batches, durable request IDs, content fingerprints and operation-result queries protect writes.
6. Undo: inverse events preserve later additions. Loan returns, transfers, reservations and other dependent changes restrict undo. Purchase receipts cannot be reversed through the generic stock undo operation.
7. Upload and history: preview/confirmation distinguishes new/existing/overwrite/duplicate/error rows. Repeated files require explicit confirmation and a reason. Locations are transferred or archived, preserving snapshots and history.
8. Loans: borrower, purpose, destination and due date are required. Partial returns, overdue filtering, cleaning/repair/quarantine, and loss/damage handling are supported. Borrowed damage never subtracts cabinet stock a second time.
9. Teaching: templates calculate people, groups, demonstration and spare quantities. Each line requires consume/borrow. Time reservations, combined allocation, version checks and execution records are supported. Later reuse requires actual return and processing.
10. Procurement: owner, expected date, partial receipts and cancellation are recorded. Outstanding orders affect procurement suggestions but never ready stock before arrival.
11. Maintenance and audit: physical counts with reasons, item metadata, confirmed packaging, partial transfers, location notes/photos, actor/device, history filters and Excel exports are available. Time is stored in UTC and displayed in Shanghai time.
12. Mobile and uncertain requests: responsive forms, local drafts, saved previews, stale-input invalidation, durable pending requests, explicit query/retry and local static assets are implemented. Server-sent events update views; disconnected views check every thirty seconds.
13. Alerts and push: per-item state changes generate durable events. Acknowledgement, snooze, reminders, retries and expired subscriptions are supported. The independent worker sends fixed generic payloads only. Unsupported browsers, missing permissions and missing HTTPS/configuration have visible states. Unset thresholds are listed explicitly.
14. Operations: systemd application/worker restart policies, hourly backups for seven days, daily backups for thirty days, restore verification, release snapshots, migration reports, and rollback of code plus database are included. Nginx HTTP and trusted-HTTPS templates retain private-network scope.

## Local Verification

- Automated workflow suite covers the approved teacher scenarios, negation/plans, packaging, specification ambiguity, duplicate imports, concurrent teachers, inverse undo, failed batches, alert transitions, notification retry/expiry and database restoration.
- The original attachments pass the updated smoke test: at least 890 items and 1000 original source rows.
- A copy of the production data contains 894 items, 1073 transactions and 1073 source rows. All three table-content SHA-256 values match the current production database. Migration preserves their contents and row counts. Restoring the release snapshot produces identical SQL records.
- Item 407 has original quantity `-2.38000011444092`. Its available quantity is zero and it requires physical verification. No positive replacement quantity is inferred.
- Playwright exercises real forms for borrow/partial return/damage/cleaning, partial procurement, uncertain-request retry, manual preparation matching, lesson draft restoration, templates/reservation/execution, duplicate upload, photos, history and actual institutional AI parsing.
- Desktop 1440x1000, mobile 390x844 and landscape 844x390 have no page-level horizontal overflow. Screenshots are generated in the isolated review-data directory.
- The final browser check uses the America/New_York browser timezone. An entered Shanghai lesson time of `2026-12-02 10:00` is stored as `2026-12-02T02:00:00.000000+00:00`. The complete workflow and real AI checks pass in all three viewports with no JavaScript errors.

## Production Verification

- Release `/opt/lab-management/releases/20261022-02` is deployed. Application, notification worker, backup timer and Nginx are active and enabled.
- The workflow suite passes as the production service user: **37 passed**. The updated attachment import smoke test also passes as that user.
- Server-local Nginx requests succeed. AI reports configured, required and available, and a real `deepseek-pro` parse classifies `100mL烧杯用了2个不放回` as consuming two items.
- The release backup is `/var/backups/lab-management/release-20261022T132928Z`. Original item, transaction and source-row tables retain 894, 1073 and 1073 records respectively; their content hashes match the verified migration copy.
- An initial installation permission failure exercised rollback to the previous code/database. After fixing release-directory group access, installation and the scheduled backup service succeeded.
- A fresh campus HTTP request after deployment timed out after eight seconds. Simultaneous server capture recorded zero matching packets. Campus access remains incomplete.
- See `DEPLOYMENT-V2-2026-10-22.md` for the deployment and recovery record.

## Required External Checks

- Campus HTTP passed on 2026-11-05: real desktop/mobile page checks and actual institutional AI parsing succeeded. See `DEPLOYMENT-2026-11-05.md`. Campus HTTPS still has no configured listener/certificate and remains incomplete. Earlier timeout evidence is retained in the historical deployment records.
- School VPN client: confirm the approved VPN source range and verify page load, AI parsing and the main stock workflows.
- Institution: assign a school DNS name and install a trusted HTTPS certificate. Use `nginx-https.conf.example`, then configure `PUBLIC_BASE_URL` and a valid `VAPID_SUBJECT` contact.
- Authorized phone: authorize Web Push, close the page, generate an alert, and verify generic notification delivery. Verify opening details only from campus/VPN. This cannot be established with a desktop simulation.
- Push-provider connectivity: allow outbound HTTPS from the notification worker to supported browser push services. No inventory descriptions or quantities are sent.

Neither the implementation nor automated checks can guarantee that the institutional model or browser push provider never has an outage. AI availability is checked every five minutes, requests retry with limits, failures are visible, and entered text is retained. Configure only institution-approved fallback models in `GENAI_FALLBACK_MODELS`.

Until the campus, VPN, trusted HTTPS and real-device checks are complete, this document does not mark all fourteen items accepted for production.
