# Accounts audit — 12 November 2026

The accounts module has substantial tested functionality, but still has confirmed
bugs and incomplete account lifecycle features. Passing tests do not establish
that the application is free of bugs or ready for deployment.

This is a review, not a remediation change. Authentication behavior, existing
accounts, provider configuration and Git history were not changed by this audit.
All probes used disposable PostgreSQL schemas and synthetic identities. No live
Google, email or SMS authentication/delivery request was made.

## Confirmed defects

Follow-up: A1 is now fixed. Registration preserves password whitespace, rejects
whitespace-only passwords, and passes the exact value to contextual validators.
Twelve password/login tests passed on PostgreSQL, including registration through
email verification and exact-password login. A3 is also fixed: phone normalization
is shared across model/manager/admin/API/OTP helpers, canonical phone storage is
database-enforced, and migration aborts on collisions or invalid legacy numbers.
All 111 tests passed on PostgreSQL after the phone fix. A2 is now fixed with a
dedicated resend serializer: 22 resend/email/throttling tests passed on PostgreSQL,
including malformed roots, invalid types and invalid JSON without delivery calls.
A4 is fixed for model saves and admin edits: actual contact changes reset flags
and invalidate local challenges atomically; manual overrides are superuser-only
with a required reason and admin audit entry. All 131 tests passed on PostgreSQL,
including contact-change/check concurrency. Runtime bulk identity/proof writes are
now blocked as part of A6; privileged SQL requires reviewed maintenance. Other findings
remain open. A5 is now fixed with expiring, session-bound database challenges and
atomic consumption. All 142 tests passed on PostgreSQL, including three concurrent
web exchange cases, rollback/expiry checks and unchanged native token reuse.
A6 is now fixed with an explicit bulk-write policy, canonical email database
constraint, collision-safe migration and case-insensitive authentication lookup.
All 153 tests passed on PostgreSQL. See BULK_ACCOUNT_WRITES.md for supported paths.
A7 is now fixed with required-contact model validation and a database constraint;
all 160 tests passed on PostgreSQL after this change.
L1 is now implemented under the policy in JWT_LIFECYCLE.md. All 178 tests passed
on PostgreSQL, including rotation replay, rollback, revocation and password-change
checks. Expired-token cleanup still needs to be scheduled by deployment.
L2 self-service flows passed the 206-test PostgreSQL regression suite. Two
additional concurrency checks then confirmed parallel first email/phone sends
deliver once and return a cooldown to the competing account. Recovery redemption
also permits exactly one success. Run cleanup_account_challenges daily; see
SELF_SERVICE_ACCOUNTS.md for request bodies and deployment limitations.
L3 GET/session controls passed all 214 PostgreSQL tests. The local cleanup task
was triggered and completed successfully (LastTaskResult 0).
L7 response standardization passed all 226 PostgreSQL tests, including one shared
rendered-JSON/JWT contract check across every token issuer and existing concurrency
and rollback coverage. Client response paths must follow AUTH_RESPONSE_CONTRACT.md.

Priority definitions: P1 = address before production; P2 = next development work;
P3 = lower-impact developer/maintenance issue. Some P2 items should still be
completed before the affected feature is exposed to users.

| ID | Priority | Finding | Reproduction/effect | Fix and acceptance check |
| --- | --- | --- | --- | --- |
| A1 | Resolved | Registration previously trimmed password whitespace while login preserved it. | The audit reproduced original-password failure and stripped-password success. | Fixed with trim_whitespace=False and PostgreSQL round-trip tests for leading, trailing, surrounding and internal spaces. Whitespace-only passwords remain invalid. |
| A2 | Resolved | Resend previously assumed an object JSON root and crashed on other types. | Array, null and number request bodies returned HTTP 500 during audit. | Fixed with ResendVerificationSerializer; invalid roots, email values and JSON return 400 without delivery. Valid normalization, existing verification behavior and throttles remain tested. |
| A3 | Resolved | Phone formats previously produced different account identities outside the API. | The audit reproduced a local-format staff account becoming a separate customer account on OTP login. | Fixed with shared E.164 normalization, canonical database storage, collision-safe data migration and regression tests proving both formats authenticate the existing staff account. |
| A4 | Resolved for supported saves/admin | Actual identity edits previously inherited verification. | The audit reproduced verified flags surviving admin contact changes. | Fixed with transactional resets/challenge invalidation, coordinated phone locks and read-only admin flags. Superuser manual verification requires a reason and records actor/target in LogEntry. Runtime bulk identity/proof writes are blocked; privileged SQL requires reviewed maintenance. |
| A5 | Resolved | Browser nonce consumption previously permitted concurrent replay. | The audit reproduced two successful exchanges using the same browser session and nonce. | Fixed with ten-minute session-bound challenges, row-locked revalidation and transactional consumption/token encoding. PostgreSQL tests prove one 200/one 400 and one token pair for existing login, new login and linking. Native reuse remains intentional. |
| A6 | Resolved | Bulk ORM writes previously bypassed email normalization and authentication required canonical storage. | Mixed-case persisted emails could not authenticate through normalized input. | Runtime sync/async bulk identity/proof writes and bulk user creation now fail explicitly. Database constraints enforce canonical email storage; authentication lookup is case-insensitive. Migration reviews collisions before normalization. PostgreSQL tests cover bulk paths, constraints and historical repair. |
| A7 | Resolved | Accounts previously allowed neither email nor phone. | Direct ORM creation bypassed the manager's identity requirement. | Model clean/save, admin validation and a database constraint now require at least one contact. Partial saves validate the effective stored values. Google subject links do not exempt accounts; Google registration supplies a verified email. Existing data review found no missing identities. Migration 0011 stops for review rather than changing or deleting invalid legacy accounts. PostgreSQL tests cover validation, SQL enforcement and migration recovery. |
| A8 | Resolved | Inherited async creation methods previously required username. | Email-based acreate_user calls raised TypeError; acreate_superuser inherited the same mismatch. | Explicit email-based async methods now delegate to synchronous creation in a thread-sensitive worker, preserving normalization, password hashing, database selection, validation and transactional save hooks. Seven PostgreSQL tests cover email/phone creation, password handling, superuser defaults/rejections and uniqueness enforcement. |

## Missing functionality and remaining risks

| ID | Priority | Gap | Evidence and required work |
| --- | --- | --- | --- |
| L1 | Resolved with documented expiry policy | JWT lifecycle now includes refresh rotation, logout and password-change invalidation. | Refresh/logout endpoints use blacklist integration and account locks to prevent simultaneous reuse. Password changes invalidate access and refresh tokens. | See JWT_LIFECYCLE.md. Logout revokes the submitted refresh token; existing access tokens retain their 30-minute expiry window. Other devices remain signed in. Legacy tokens require new sign-in. Immediate logout access revocation, logout-all and token-family replay revocation are not provided by this policy. |
| L2 | Implemented | Self-service recovery and identity management now have explicit proof/policy checks. | Added password recovery/change, purpose-bound reauthentication, verified email/phone add/change and Google unlink APIs. | See SELF_SERVICE_ACCOUNTS.md. New contacts require destination proof plus current-password or current-contact OTP proof. Recovery only uses verified contacts; Google unlink requires another working login. Phone-only users can attach verified email and link Google without creating another account. Contact removal/account deletion remain unexposed; delivery/outbox limitations remain tracked separately. |
| L3 | Resolved locally; production schedule required at deployment | Google challenge GET endpoints now share pre-session IP throttling. | Page/config requests share an atomic 30/hour limit, returning 429 before session/challenge creation. Valid session challenges are reused without extending expiry; anonymous Google sessions expire after ten minutes. | See AUTH_MAINTENANCE.md. Combined cleanup preserves active records and removes expired sessions/challenges/JWT records. Local Windows task InventorySales-AuthCleanup is registered daily at 03:00 Bangladesh time while the user is logged in; future hosting needs its own scheduler. |
| L4 | Resolved for application per-key limits | Account throttles now use PostgreSQL rolling-window reservations with unique keys, row locks and database time. | Eight simultaneous mixed anonymous/authenticated login requests with a budget of two admit exactly two. Four email resends with API scopes disabled deliver once and return three cooldowns. | Persistent delivery cooldowns are shared by registration/resend/self-service email and survive cache resets. Limiter failures return 503. Cleanup preserves concurrently renewed buckets. The 223-test regression suite and 13 final targeted checks passed on PostgreSQL; migration 0013 is applied. Gateway/provider-wide spending controls remain external deployment work; per-key limits are not a billing cap. See ACCOUNT_RATE_LIMITS.md. |
| L5 | Implemented with explicit uncertainty policy | Provider calls now execute outside transactions/row locks, backed by durable delivery intent and proof receipts. | Challenge plus encrypted outbox job commit before submission; leased workers retry the same intent, fence acknowledgements and cancel superseded work. Remote approvals commit before local consumption and can survive a local token failure. | DELIVERY_WORKFLOW.md documents duplicate-delivery limits, bounded retries, expiry, changed API acceptance statuses and fail-closed unknown Twilio approvals. Migration 0014 is applied. Local InventorySales-DeliveryWorker is running with logon/restart settings; future hosting requires its own worker. All 240 PostgreSQL tests passed, followed by final targeted lease/receipt checks. No real provider delivery was performed. |
| L6 | Resolved | Google login/link now handles a user disappearing before the row-lock lookup. | Four PostgreSQL concurrency tests commit deletion on an independent backend after identity lookup and before the user lock, covering browser/native login and linking. | User.DoesNotExist returns 403 with code account_unavailable outside the rolled-back transaction. No tokens or replacement account are created; browser challenges remain unconsumed. All 42 targeted Google/authentication tests passed. |
| L7 | Resolved; clients must update response paths | All seven app-token issuers now share one authentication success envelope. | Email, phone, browser/native Google login/link and refresh return status/message/data with data.user, data.access_token and data.refresh_token, plus no-store headers. | Shared formatter and rendered-JSON/JWT contract checks cover every issuer. Browser sign-in and Flutter examples use the new paths; AUTH_RESPONSE_CONTRACT.md includes a typed Dart model and migration table. Existing failure contracts and non-token responses remain unchanged. Phone token encoding remains inside the OTP transaction so a failure preserves the challenge. |
| L8 | Implemented locally; production scheduler required at deployment | Daily cleanup covers expired legacy OTPs, challenges, sessions, JWT/throttle/delivery artifacts and inactive recipient rows. | Recipient rows require thirty-day inactivity plus send/window/live-proof rechecks. Stable advisory keys are shared by issuance and pruning, preventing split locks during row deletion/recreation; busy keys/OTP rows are skipped. | See ACCOUNT_RETENTION.md. Migration 0015 is applied and grants existing rows a fresh retention baseline. All 254 PostgreSQL tests passed, including live-state preservation, concurrent renewal, active-lock skipping and two competing sends during pruning yielding one accepted request and one cooldown. Existing InventorySales-AuthCleanup runs daily at 03:00 Bangladesh time while the user is logged in; future hosting needs its own scheduler. |

## Repository and deployment status

- Private database snapshots/export files are removed from the current Git index,
  but remain in three historical commits. This is not erased by ignore rules.
  Assess who could access those commits before separately planning history/data
  remediation. No history rewrite was performed.
- Twenty-four Python bytecode files remain tracked despite ignore rules. Remove
  them from the index while preserving local development as appropriate.
- Many implementation files and migrations remain untracked/unstaged. A commit
  using only git commit -am would omit new required modules and can break a fresh
  checkout. Review and stage the complete change set before sharing/deploying it.
- Current settings intentionally remain development mode with console phone OTPs
  and a process-local cache. This matches the user's request and is not reported
  as an accidental production deployment.
- A separate production profile exists, but there is no hosting/hostname/TLS
  deployment yet. Actual proxy, client-IP handling, secure cookies over the public
  endpoint, provider spending controls and production DB connectivity remain
  operational checks. Production-like fixture checks are not evidence of a
  deployed system.
- Real Flutter Google consent/sign-in and end-to-end inbox/handset delivery are
  still unverified by this audit. Twilio trial recipient verification was the
  previously diagnosed 21608 account restriction; local mode intentionally does
  not send real SMS.

## What was verified

- Re-ran all 100 existing tests on PostgreSQL using the pinned isolated dependency
  environment, shared database cache and disposable-schema runner: all passed.
- pip check reported no broken dependencies. makemigrations --check --dry-run
  reported no pending model changes.
- The suite includes concurrent phone issuance, concurrent email registration,
  attempt-counter locking, one-time OTP consumption, native/web Google validation,
  authenticated throttling and a production-like configuration subprocess check.
- Extra disposable-schema probes independently reproduced A1–A8 (async creation
  probe covered acreate_user), the password-change JWT behavior in L1 and the
  unthrottled session creation in L3. These are not covered by the passing suite.
- The nonce probe synchronized two web requests after session nonce loading and
  before their transaction work; both had the same session/cookies/nonce and
  returned 200. Provider verification was mocked; no real Google login occurred.
- No account identities, passwords, tokens or provider credentials from the live
  database were printed. Existing application account records were not modified.

Recommended sequence: fix A1/A2/A3 first; complete L1 and decide recovery/linking
flows; address verification-state invariants and atomic web challenges; then
delivery durability, strict resource limits, bulk/async APIs and repository
hygiene. Add failing regression tests for each confirmed defect before its fix.

This audit does not certify zero defects. It separates observed failures from
code-derived risks and deliberate behavior so follow-up work can be reviewed
without treating passing tests as a completeness guarantee.
