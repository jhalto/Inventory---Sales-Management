# Browser Google challenge limits and cleanup

`GET /api/v1/auth/google/config/` and `GET /api/v1/auth/google/sign-in/`
share an IP limit of 30 requests/hour. This applies to anonymous and signed-in
browsers and runs before session/challenge creation. Rejected requests return
429 with Retry-After and no new session or CSRF cookie. Configure the rate via
`ACCOUNT_THROTTLE_RATES['google_challenge_ip']`. The configured trusted-proxy
count controls IP selection; do not trust arbitrary forwarded headers.

Repeated requests with the same valid browser session reuse the existing nonce
and challenge without extending its ten-minute expiry or rewriting that session.
Expired, consumed, missing or mismatched challenges are replaced. Anonymous
Google sessions expire after ten minutes; existing authenticated Django sessions
retain their configured lifetime. OAuth signature, CSRF, session binding and
atomic one-time consumption checks remain in force.

Limits use atomic PostgreSQL rolling-window reservations shared by all workers.
Distributed clients can still use
different IPs; deployment/provider request limits remain a separate control.

## Combined maintenance command

Run `python manage.py cleanup_auth_artifacts` daily. It runs:

- `clearsessions` for expired Django database sessions.
- `cleanup_account_artifacts` for expired legacy OTPs and safely pruned inactive recipient rows.
- `cleanup_google_challenges` for expired Google challenges.
- `cleanup_account_challenges` for expired self-service proofs.
- `flushexpiredtokens` for expired outstanding/blacklisted JWT records.
- `cleanup_throttle_buckets` for expired rate-limit histories.
- `cleanup_delivery_state` for expired encrypted payloads/receipts and old job metadata.

Active sessions/challenges/tokens and recipient cooldown rows are retained.
Unused recipient rows can be pruned after thirty days under stable advisory
locks, with cooldown/rate-window/live-proof rechecks. See ACCOUNT_RETENTION.md.
Rows becoming expired between runs remain unusable and are removed on the next
run. No mail, SMS or Google provider requests are made by cleanup.

## Local Windows schedule

The task `InventorySales-AuthCleanup` is registered daily at **03:00 Bangladesh
time** (the machine's Windows timezone is Bangladesh Standard Time). It runs
with the current user's limited privileges, requires that user to be logged in,
starts when available after missed runs, and allows only one instance at a time.
It uses `scripts/cleanup-auth.ps1`, the project's venv and its local configuration.
No passwords or provider credentials are stored in task arguments.

Check the task's Last Run Result in Windows Task Scheduler; 0 means success.
The task was triggered during setup and completed with LastTaskResult 0. This local schedule
does not run on a future hosting server or when this Windows account cannot run
interactive tasks. Re-register after moving the project or virtual environment:

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\scripts\install-auth-cleanup.ps1
```

The process-only execution policy option does not change Windows' persistent
execution policy. The installer refuses to replace a different action using the
same task name. To remove this project's schedule:

```powershell
Unregister-ScheduledTask -TaskName InventorySales-AuthCleanup -Confirm:$false
```

## Future production deployment

Schedule the combined command with the deployment service account and production
environment. For a cron server configured in UTC, 21:00 UTC corresponds to
03:00 Bangladesh time the next day; adapt paths to the actual deployment:

```cron
0 21 * * * cd /srv/inventory && DJANGO_ENV=production /srv/inventory/venv/bin/python manage.py cleanup_auth_artifacts
```

Configure scheduler failure monitoring and use the deployment's shared PostgreSQL database and
trusted-proxy settings. Production hosting is not currently configured; the
registered Windows task provides local development maintenance only.
