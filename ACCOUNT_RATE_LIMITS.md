# Account rate limits

Limits cover anonymous and authenticated requests. IP limits always use the
request IP, even when a JWT is supplied. Recipient keys are hashed and normalize
email case and Bangladeshi phone formats, so changing IPs or accounts does not
reset the recipient's budget.

| Endpoint | Per IP | Per recipient |
| --- | --- | --- |
| Registration | 5/hour | Email sending: 5/hour and 1/minute, shared with resend |
| Email/password login | 20/hour | 10/hour per submitted email |
| Google sign-in/link | 20/hour | Valid Google token, session nonce and CSRF protection required |
| Google page/config GET | 30/hour shared | Reuses valid ten-minute browser challenges |
| Token refresh/logout | 60/hour shared | Single-use refresh rotation and revocation |
| Self-service management/recovery | 60/hour shared | 30/hour per authenticated account; persistent delivery limits below |
| Email resend | 5/hour | Email sending: 5/hour and 1/minute, shared with registration |
| Email verification | 30/hour | 10/hour per email, plus 5 wrong attempts per code |
| Phone OTP send | 10/hour | 5/hour and 1/minute per phone |
| Phone OTP verify | 30/hour | 10/hour per phone, plus 5 wrong attempts per code |

Exceeding a limit returns HTTP 429 with Retry-After. Limits count requests,
including invalid credentials and delivery failures; verification/resend limits
also apply to unknown recipients. Wait at least 60 seconds after registration
before requesting another email code. Provider uncertainty retains the newly
committed challenge and durable job; retries reuse that same intent. The request
still consumes its throttle budget.

Override selected defaults with ACCOUNT_THROTTLE_RATES in Django settings:

```python
ACCOUNT_THROTTLE_RATES = {
    'login_ip': '20/hour',
    'login_email': '10/hour',
    'email_send': '5/hour',
    'email_cooldown': '1/minute',
}
```

See accounts/throttles.py for all scopes. Keep recipient limits independent of
caller identity; IP-only throttles do not protect against distributed requests.

## Atomic PostgreSQL enforcement

AccountThrottle now uses BaseThrottle and a PostgreSQL ThrottleBucket, not DRF's
cache history. Each scope/identity has a unique key containing a salted digest.
One transaction locks or creates that row, reads the database clock after locking,
prunes expired timestamps, checks the limit and appends an accepted reservation.
The unique key serializes concurrent first requests too. This is a rolling window:
a 5/hour budget accepts at most five requests in any trailing hour per key.
Denied requests return Retry-After until the earliest accepted request expires.

All workers must share the same PostgreSQL primary database and signing secret.
Limits persist through application restarts and cache clears/replacements in both
development and production. PostgreSQL is required; there is no unsafe SQLite or
process-memory fallback. Database errors return 503 before protected work. A None
rate explicitly disables that scope. Limit reservations commit before endpoint
logic under the current ATOMIC_REQUESTS=False configuration; keep that setting.

Multiple scopes reserve independently, as with DRF's previous behavior: a denied
recipient request may also consume its IP budget. There is no atomic all-scopes
refund. These are per-key request limits, not a global provider billing budget.

## Persistent delivery limits

Legacy registration/email resend and self-service email challenges share a stable
AccountChallengeRecipient row. With the user lock held, delivery locks this row
and enforces a 60-second cooldown plus ten submissions per recipient in a fixed
hourly window. Normal registration/resend also retain their stricter 5/hour
rolling API limit. The delivery checks remain active if API scopes are disabled
or the helper is called directly. Reservations, OTP updates and encrypted outbox
jobs commit before submission. Provider failures retain this new intent for
bounded recovery retries; budgets/cooldowns stay consumed. The recipient row
remains after challenge consumption. See DELIVERY_WORKFLOW.md.

Self-service phone sends also use this row and the existing persistent phone
recipient lock/cooldown. Legacy phone sends retain their phone lock/cooldown and
atomic API budgets. All phone paths acquire recipient locks before user locks;
email-only delivery locks the user before its delivery recipient row.

PostgreSQL tests synchronize independent backend connections: eight simultaneous
mixed anonymous/authenticated login requests with a budget of two admit exactly
two. Four email resends with all API scopes disabled deliver once and return
three cooldown responses. Tests also cover rolling-window boundaries, cache
replacement, fail-closed behavior and cleanup preserving live limits.

## Deployment

Run `python manage.py migrate` before serving traffic. The first migration for
the database limiter starts fresh counters; old cache histories are not imported.
Changing rate policies or signing secrets can change effective budgets; deploy
consistent settings across workers. The configured cache remains available for
other application uses but does not enforce account limits.

`cleanup_auth_artifacts` now also runs `cleanup_throttle_buckets`, which deletes
only expired bucket rows. PostgreSQL locking/rechecking protects reservations
against concurrent expiry cleanup. Active cooldown recipient rows are retained.
The existing local Windows maintenance task automatically runs this updated
command; provision the same daily command on future hosting.
Inactive recipient rows have a thirty-day retention policy with stable advisory
locks protecting deletion/recreation; live cooldowns/windows remain preserved.
See ACCOUNT_RETENTION.md. Expired legacy email/phone OTPs are now also removed.

Before public deployment, configure:

- Ingress/proxy rate limits for /api/v1/register/, /api/v1/login/ and OTP routes.
- Trusted proxy handling: strip client-supplied forwarding headers at ingress and
  set REST_FRAMEWORK['NUM_PROXIES'] to the actual trusted proxy count. Use 0 when
  directly exposed without a proxy. Do not guess a proxy count.
- SMS provider spending controls/alerts and destination restrictions, plus mail
  provider sending quotas and delivery monitoring.
- Monitoring for HTTP 429/503 responses and provider spending.

Proxy and provider settings live outside this repository and have not been
changed. Atomic per-key limits do not impose a global SMS/mail spending cap:
many recipients/IPs can each consume their own budgets. Ingress limits should
also protect the database from unbounded traffic and bucket creation.
