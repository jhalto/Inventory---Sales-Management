# Authentication artifact retention

Run `python manage.py cleanup_auth_artifacts` daily. The existing local Windows
task InventorySales-AuthCleanup runs this command at **03:00 Bangladesh time**
while the Windows user is logged in. It starts when available after a missed run;
future hosting requires its own scheduler. Expiry is enforced by authentication
code immediately; deletion waits for maintenance, and a powered-off machine can
retain expired rows until its next successful run.

| Artifact | Expiry/retention | Cleanup behavior |
| --- | --- | --- |
| Legacy email/phone OTPs | Five-minute challenge lifetime | Remove expired rows in locked batches; skip rows in use |
| Self-service challenges | Five minutes | Remove expired rows |
| Browser Google challenges | Ten minutes | Remove expired rows |
| Django sessions | Configured expiry; anonymous Google handshake ten minutes | clearsessions removes expired sessions |
| JWT outstanding/blacklist records | Token expiry | flushexpiredtokens removes expired records |
| Atomic throttle buckets | Last accepted request plus rate window | Remove expired buckets; concurrent renewal remains protected |
| Delivery payloads | Successful/cancelled submission, otherwise challenge expiry | Erase immediately on success/cancellation, or at daily expiry cleanup |
| Delivery metadata | One day after challenge expiry | Prune on the next cleanup run |
| Remote verification receipts | Challenge expiry | Remove expired receipts |
| Phone/delivery recipient rows | Thirty days since recorded use | Prune only after the checks below |

Cleanup does not delete users, Google links or active tokens, change passwords,
or modify verification flags. It makes no email/SMS/Google provider requests.
An expired in-use OTP may survive a run because cleanup skips its row lock; it
remains unusable and will be reconsidered next time. Each OTP batch commits
separately, so an interrupted cleanup can safely resume later.

## Safe recipient pruning

A row lock alone is insufficient if maintenance can delete the row: a waiting
issuer could otherwise lock an old row while another creates a replacement.
Every supported phone-recipient operation now takes a deterministic PostgreSQL
transaction advisory lock before looking up/creating its row. Delivery recipients
use a separate stable advisory key; phone delivery acquires the phone key first.
These keys survive row deletion and recreation and are independent of signing
secret rotation.

Usage updates last_used_at with database time. Pruning considers only rows whose
last use and last send are older than thirty days. Under the same advisory key
it then rechecks:

- No currently held advisory/row lock; busy recipients are skipped without waiting.
- No fresh use/send or retained delivery rate-window timestamp.
- No live email OTP, phone OTP or self-service challenge for that contact.
- A recognized recipient namespace. Unknown legacy keys are retained for review.

Only then is the row deleted. Subsequent issuers acquire the same stable key and
recreate one row. Concurrent requests still share one cooldown. There is no
automatic deletion at OTP consumption, and cache resets do not affect this policy.
Do not manually delete recipient rows or bypass the supported locking helpers.

Migration 0015 gives existing recipient rows a fresh last_used_at baseline,
preserving their keys/counters and granting a full retention interval before
they can become candidates. Deploy the migration and restart all API processes
using the old locking code before enabling pruning. The local scheduled cleanup
loads fresh code each run; delivery workers can be restarted during deployment
according to DELIVERY_WORKFLOW.md.

## Commands and operations

The combined daily command includes cleanup_account_artifacts for legacy OTPs
and recipient rows, alongside existing session, Google/self-service challenge,
JWT, throttle and delivery cleanup commands. To run just this portion:

```powershell
python manage.py cleanup_account_artifacts
python manage.py cleanup_account_artifacts --recipient-retention-days 30 --batch-size 500
```

Retention must be at least one day, longer than current cooldown/rate windows;
batch size must be positive. Defaults are thirty days and 500 OTPs per batch.
Recipient candidates are scanned in bounded batches and pruned in individual
short transactions. Output reports counts without contacts, codes or credentials.
The recipient activity and legacy OTP expiry fields are indexed.

Monitor the scheduled task's Last Run Result (0 means successful completion).
The local task requires a logged-in Windows user; configure a supervised daily
production job with the deployment environment and alert on failed/missed runs.
No separate worker or provider credentials are needed by the cleanup command.

PostgreSQL tests cover expired/live code retention, cooldown/window preservation,
usage refresh, active lock skipping, concurrent OTP renewal, and deleting an old
row while two issuers wait: one recreated row and one accepted send plus one
cooldown response result. Existing session and outbox cleanup tests remain in place.
