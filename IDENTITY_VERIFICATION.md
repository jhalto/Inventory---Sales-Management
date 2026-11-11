# Verification state when identities change

Changing a user's actual normalized email or phone through model save/admin now
resets that identity's verification flag in the same database transaction.
Case/format-only changes preserve verification. Partial saves of unrelated fields
do not restore stale verification flags. A missing identity cannot be verified.

- Email changes delete that user's pending EmailOTP challenge.
- Phone changes delete pending PhoneOTP challenges for both previous and new
  numbers. Recipient lock/cooldown rows remain intact.
- New accounts assigned a phone also invalidate a pre-existing phone challenge;
  an OTP verification that creates an account already validated that challenge
  inside the same locked transaction.
- A fresh OTP can verify the new identity normally.

Phone issuance, checks and edits coordinate using persistent recipient locks.
Identity changes acquire recipient locks in sorted order, then the user row, then
OTP rows. Email verification and changes lock the user before its email challenge.
Identity edits retry if a competing edit changes the phone before the user lock
is acquired. State updates and invalidation roll back together on save failure.

## Manual verification in Django admin

The email_verified and phone_verified fields are read-only on normal user forms.
For a deliberate exception, a superuser can open a user's change page and choose
"Verify identity with an audit reason". Select email or phone and provide the
ownership evidence/reason. The form refuses a changed/empty identity, requires
CSRF protection, invalidates its pending OTP, and writes a Django admin LogEntry
with the actor, timestamp, target identity and reason. Non-superusers cannot use
the override even with ordinary user-change permission.

Trusted application proof handlers may still mark an unchanged identity verified
after validating an OTP or Google identity. Google sub links are not deleted when
contact details change: they identify the same account independently of its
current email. JWT refresh rotation, logout and password-change revocation are
documented in JWT_LIFECYCLE.md; logout retains the access token's expiry window.

This policy covers model save/full_clean, manager creation and admin forms.
Every account must retain at least one nonempty email or phone. Model validation,
save (including partial saves), admin forms and a database check enforce this.
Google-created accounts retain their verified Google email; a Google subject link
alone does not exempt an account from the contact requirement. A linked account
can retain only a phone if its email is removed through a supported save.
Migration 0011 stops with account IDs for review if legacy accounts lack both
contacts; it never invents identities or deletes accounts.
Runtime QuerySet.update/bulk_update of identities or verification flags and bulk
user creation are now explicitly blocked, including async counterparts. See
BULK_ACCOUNT_WRITES.md. Privileged raw SQL/fixture operations still bypass hooks
and require a reviewed repair procedure. Existing flags cannot be retrospectively proven correct;
review any identities manually changed before this fix rather than guessing or
resetting every account automatically.
