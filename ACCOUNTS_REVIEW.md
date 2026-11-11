# Accounts review — 11 November 2026

Scope: account models and manager, serializers, authentication endpoints, OTP
services, Django admin, settings, routes, repository hygiene, and existing tests.
This review changes no application behavior or account data.

Priority: P1 = fix before production; P2 = next development work; P3 = maintenance.

| Priority | Finding and evidence | Effect | Fix | Acceptance check |
| --- | --- | --- | --- | --- |
| P1 | Plain ModelAdmin registration in `accounts/admin.py:4`. Reproduced a valid add form saving the submitted password unchanged. | Admin-created or edited passwords can be stored as plaintext and fail Django authentication. | Use a custom UserAdmin and creation/change forms adapted to the email-only model; hash passwords with set_password and display existing hashes read-only. | Admin add/change tests assert check_password succeeds and stored values differ from inputs. |
| P1 | Git tracks `db.sqlite3`, two database backups, and `data-transfer.json`. Confirmed with git ls-files; contents were not printed. | Account data and password hashes can be shared through repository history. Ignore rules do not remove already tracked files. | Untrack data files while preserving local copies, broaden backup/fixture ignore rules, and assess history exposure before any history cleanup. | git ls-files returns no private exports or database snapshots. |
| P1 | Register and login have no throttles. Phone send/check and email resend inherit AnonRateThrottle, which skips authenticated callers. | Unlimited password guessing/registration and authenticated bypass of provider rate controls; email resend also has no per-recipient cooldown. | Apply limits to authenticated and anonymous callers, add recipient/account limits and cooldowns, and use a shared cache and deployment/provider limits. | Anonymous and authenticated abuse tests return 429; separate clients cannot bypass recipient limits. |
| P1 | Registration performs an exists check, then inserts in a separate step (`accounts/serializers.py:37`, `api/views/accounts/views.py:59`). IntegrityError is not handled. | Simultaneous registrations for one email can produce HTTP 500. Case-insensitive uniqueness is not enforced by the database. | Normalize identities consistently, add a case-insensitive database uniqueness constraint after data review, and translate identity conflicts into the existing email validation error. | Concurrent PostgreSQL registration produces one account and a controlled 400 for the competing request. |
| P2 | Login calls email.strip() without serializer/type validation (`api/views/accounts/views.py:99`). Reproduced email supplied as a JSON array returning HTTP 500. | Malformed client input crashes the endpoint. | Add a login serializer for email/password and handle non-object request bodies. | Arrays, numbers, nulls, missing fields and invalid emails return 400. |
| P2 | Manager uses normalize_email, which preserves the local part's case (`accounts/managers.py:14`), while login lowercases the whole address. Reproduced mixed-case manager-created account failing normalized authentication. | Accounts created outside registration can fail email login or disagree with verification identity matching. | Centralize the application's chosen normalization policy across creation, lookup and updates; review existing collisions before migration. | Manager, API, admin and createsuperuser identities behave consistently for mixed-case input. |
| P2 | Password validator is called without a user (`accounts/serializers.py:17`). Reproduced registration accepting an email address as its own password although contextual similarity validation rejects it. | UserAttributeSimilarityValidator cannot compare against the registering user's email. | Validate with an unsaved User containing the normalized email; retain the requested email-first error behavior. | Email-similar passwords fail after a new email passes validation; duplicate email skips password validation. |
| P2 | Phone cooldown checks select_for_update on a record that may not exist (`api/views/accounts/views.py:211`). | Concurrent first sends have no common row to lock; multiple messages can be submitted and one code can supersede another. | Serialize issuance using a stable recipient lock row, an atomic shared-cache lock, or PostgreSQL advisory locks; enforce cooldown inside that lock. | Concurrent first requests submit one message and return a cooldown response for the other. |
| P2 | SMTP and SMS HTTP calls execute inside database transactions; email holds a user row lock (`accounts/email_service.py:26`, `api/views/accounts/views.py:210`). | Provider latency holds connections and locks. Delivery may succeed before a later database commit fails. | Use an explicit delivery state/outbox and bounded background worker; preserve recoverable resend and failed-delivery behavior. Do not just move sending after commit without a failure design. | Slow-provider and worker-failure tests show short database transactions and recoverable delivery state. |
| P2 | Login returns a seven-day refresh token, but no refresh route exists (`api/urls.py`). | Clients cannot use the issued refresh token after the access token expires. | Add and test a JWT refresh endpoint; define rotation, revocation and logout behavior for the product. | Valid refresh yields an access token; expired/invalid refresh is rejected. |
| P2 | Loaded setup has DEBUG=True, the development signing key, and no explicit production signing secrets. check --deploy reports seven warnings. | Current local configuration is unsuitable for deployment. | Set production secrets, hosts, secure cookies, HTTPS/proxy settings and a deliberate HSTS policy; provision shared throttle cache. | Deployment checks pass for actual production settings, with any proxy-managed exceptions documented. |
| P2 | No Twilio credentials/sender are present in the loaded environment. Implemented adapter uses custom Programmable Messaging, not Twilio Verify (`accounts/sms_service.py:25`). | Console testing works; real SMS is not configured. The user's trial account is not integrated with the Verify trial flow discussed earlier. | Choose funded Programmable Messaging with an approved sender, or implement Verify issuance/checking as a separate backend. Configure secrets locally and retain console mode. | A controlled recipient receives and verifies a real OTP using the chosen backend; no credentials or OTPs appear in API responses. |
| P2 | Existing tests pass using an isolated SQLite database and mocked mail/Twilio. No PostgreSQL concurrency coverage was found. | Passing tests do not verify row locking, deployment cache behavior, or real delivery. | Add dedicated PostgreSQL transaction/concurrency tests and a safe production-like configuration check; run delivery smoke tests separately. | Parallel issuance, attempt counting, duplicate registration, one-time consumption and authenticated throttling tests pass on PostgreSQL. |
| P3 | requirements.txt has no version constraints. | New installations can resolve different dependency versions and change authentication behavior. | Record a tested dependency set and define an update process. | A clean environment reproduces the passing checks. |

Recommended order: fix admin password handling and repository data tracking;
then identity validation, database constraints and rate limits; then delivery
concurrency, refresh lifecycle and PostgreSQL coverage; finally deployment setup
and dependency reproducibility.

Verification performed:

- All 16 existing API tests passed on an isolated in-memory SQLite database.
- makemigrations --check --dry-run reported no model changes pending.
- check --deploy reported seven security/configuration warnings in local settings.
- Separate isolated probes reproduced the admin password issue, malformed login
  crash, mixed-case identity mismatch and missing password similarity context.
- Confirmed absence of a refresh route and tracked private database/export files.
- Gmail username and password are present in the loaded configuration now. Their
  values were not displayed. SMTP authentication and actual email delivery were
  not tested.
- Existing PostgreSQL data was not changed. PostgreSQL concurrency and actual
  Twilio delivery were not tested. Concurrency findings above are code-derived
  risks, not reproduced PostgreSQL failures.

The duplicate-email-first response is intentional per the user's earlier request
and is not reported as a bug. Phone OTP login and email/password login are separate
authentication paths; requiring email verification only on the latter is not
treated as an accidental bypass.
