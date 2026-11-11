# Self-service account flows

All routes below start with `/api/v1/` and accept JSON. Authenticated routes use
`Authorization: Bearer <access token>`. Password fields preserve whitespace.
New passwords run Django's configured validators with the account as context.
Codes expire after five minutes, permit five checks, and are single-use.
Challenge IDs are random UUIDs; responses never include OTPs or passwords.

## Recover a password

1. POST `password/reset/request/` with
   `{"channel":"email","email":"person@example.com"}` or
   `{"channel":"phone","phone":"01712345678"}`.
2. POST `password/reset/confirm/` with
   `{"challenge_id":"<returned UUID>","otp":"123456","new_password":"<new password>"}`.
3. Sign in again. Recovery does not issue tokens.

Only an active account's currently verified contact is eligible. This can set a
first usable password for a Google/phone-only account as well as replace a lost
password. Resetting a phone-only account does not add email/password login; that
also requires attaching a verified email.

Request responses always have the same 200 message and a challenge_id for valid
input, including unknown/unverified contacts, recipient cooldowns and delivery
failures. Fake IDs cannot be redeemed. This reduces response-based enumeration;
it does not guarantee identical response timing. Delivery failures are logged
by exception class without contacts/codes. Clients should show the generic
message and offer retry after a minute. IP throttling can still return 429.

Successful password reset/change invalidates all existing JWT access/refresh
tokens and pending self-service challenges. Existing Django session behavior
also invalidates sessions after password hash changes. Sign-in with Google and
verified-phone OTP remains available if configured; resetting a password does
not unlink those methods.

## Fresh proof for authenticated changes

Password change, contact confirmation and Google unlink require either:

```json
{"current_password":"<current password>"}
```

or a code sent to an existing verified contact:

1. POST `account/reauth/request/` with `{"channel":"email"}` or
   `{"channel":"phone"}`. The server chooses the current verified contact;
   the caller cannot supply a different recipient.
2. Include `{"reauth_challenge_id":"<UUID>","reauth_otp":"123456"}` in the
   sensitive operation, instead of current_password.

Reauthentication codes are single-use and bound to the signed-in account and
its current contacts/password. Passwordless users must use this path. A bearer
token alone never authorizes a sensitive write. Proof failures return 400.

## Change or set a password

POST `password/change/` with `new_password` plus one fresh-proof option above.
The response instructs the client to sign in again. A passwordless authenticated
user can set a first password using an OTP to a verified contact.

## Add or replace an email or phone

1. POST `account/email/request/` with `{"email":"new@example.com"}`, or
   `account/phone/request/` with `{"phone":"01712345678"}`.
2. The code is sent to that new contact. The current contact remains unchanged.
3. POST `account/email/confirm/` or `account/phone/confirm/` with the returned
   `challenge_id`, the new contact's `otp`, and one fresh-proof option above.

Only after both proofs succeed does the server atomically update and verify the
new contact. Email is canonicalized and phone uses supported Bangladeshi E.164
normalization. Occupied contacts return 409; accounts are never merged. Existing
staff/admin roles remain unchanged. Confirmation invalidates other pending
self-service challenges and the replaced contact's legacy OTP challenges.

For a phone-only user attaching email: request the email code, request a phone
reauthentication code, then confirm email with both proofs. The same account now
has a verified email suitable for the existing Google-link endpoint. Obtain a
Google credential for that exact email and call the existing authenticated
`auth/google/mobile/link/` flow described in the Google/Flutter setup guide.

Contact changes retain existing JWT sessions and Google subject links. A Google
subject identifies the linked account independently of a later email change.
Contact removal and account deletion are not exposed by these routes.

## Unlink Google

POST `auth/google/unlink/` with one fresh-proof option. Success is 204.
Unlinking is allowed only if the account retains either a verified phone for
OTP sign-in or a verified email with a usable password. Otherwise it returns
400; set a password or attach a verified phone first. Unlinking does not revoke
existing JWTs. Missing Google links return 204 once proof/policy checks pass.

## Delivery, limits and deployment

Email uses the configured Django mailer. Phone uses PHONE_OTP_BACKEND:
development local codes use the configured SMS_BACKEND (console by default),
and production twilio_verify uses stored verification/service SIDs for checks.
Legacy phone-login OTPs and new phone challenges are separate proofs. Issuance
invalidates prior challenges for that recipient, including phone-login codes;
requesting a new phone-login code also invalidates pending self-service phone
challenges. Do not request two codes for the same recipient simultaneously.
If reauthentication uses the contact being reverified, use current_password or
a different verified contact to avoid replacing the pending code.

Issuance locks stable recipient rows and enforces a 60-second cooldown and ten
self-service sends per recipient per hour across accounts. Phone sends share
the existing phone-login cooldown. API limits also apply to anonymous IPs and
authenticated accounts (60/hour per IP and 30/hour per account by default),
using atomic PostgreSQL rolling-window reservations. Provider/gateway spending
limits remain separate production controls. Uncertain remote checks return 503
and may require a new challenge; provider check rate limits return 429. Accepted
delivery requests retain durable jobs on submission failures. Invalid JSON/types return 400.

Run `python manage.py migrate` before serving these routes. Schedule
`python manage.py cleanup_account_challenges` daily, alongside
`flushexpiredtokens` and Django's `clearsessions`. Cleanup retains recipient
rows so it cannot erase live cooldowns/rate counters.

Provider calls use DELIVERY_WORKFLOW.md: encrypted jobs commit with challenges,
submission runs outside locks, and workers recover pending work. Remote checks
use durable receipts and revalidate ownership before local consumption.
Duplicate delivery remains possible; exactly-once delivery is not promised. Store tokens securely and use
HTTPS in production. Do not log request bodies containing passwords or OTPs.
