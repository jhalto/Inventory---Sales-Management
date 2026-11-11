# Supported account identity writes

Runtime bulk identity/proof writes are deliberately prohibited. Changing an email
or phone must run User.save so canonicalization, verification resets, challenge
invalidation and recipient/user locking stay together.

These operations raise ValueError before writing:

- QuerySet.update with email, phone, email_verified or phone_verified.
- bulk_update with any of those fields, including expression-based updates.
- bulk_create for User objects, including conflict-update modes.
- Async counterparts aupdate, abulk_update and abulk_create.

The policy applies to the default and base user managers and .using()/filtered
querysets. Ordinary bulk updates of unrelated fields remain supported.

Use these supported paths instead:

```python
user = User.objects.create_user(email='Person@Example.COM', password=password)

user.email = 'Changed@Example.COM'
user.save(update_fields=['email'])
# Stores changed@example.com, resets email_verified and invalidates the old OTP.

User.objects.filter(is_active=True).update(first_name='Updated')
```

Proof handlers may set verification flags on the same identity through save after
validating ownership. Django admin offers a separate superuser-only manual
verification form with a required audit reason. Never use bulk flags to bypass
those proof flows.

## Database and lookup protection

The email constraint permits NULL or a nonempty lowercase value with no
whitespace. The existing case-insensitive unique constraint remains. The phone
constraint requires the canonical +880 representation. These reject malformed
representations even through raw SQL; they do not prove full email syntax,
deliverability, or identity ownership.

Email natural-key lookups are case-insensitive after input normalization, in both
sync and async implementations. This also supports case-only legacy values while
the canonicalization migration is pending. Legacy whitespace must be repaired
through the reviewed migration, not silently ignored by authentication.

## Migration and administrative repairs

Historical migration models are exempt from runtime manager guards so reviewed
RunPython data repairs continue to work on both fresh installs and upgrades.
They do not carry the runtime policy marker. The new migration audits/normalizes
legacy values before adding the canonical-email check, refuses collisions or
internal whitespace with account IDs, and does not merge/delete accounts.

Direct SQL, save_base(raw=True), custom unmanaged querysets and fixture loaders
are privileged operations outside these runtime hooks. Canonical database
constraints still apply, but changing a canonical identity through raw SQL can
bypass verification resets and OTP invalidation. Use reviewed data migrations or
an explicit maintenance procedure that handles those effects. Do not use raw SQL
as an application identity-update API.

## Async account creation

Use `await User.objects.acreate_user(email=..., password=...)` or supply `phone`
for a phone-only account. For administrators, use
`await User.objects.acreate_superuser(email=..., password=...)`.
These methods use the same normalization, validation and transactional model
save as their synchronous counterparts, through a thread-sensitive worker.
They preserve a selected manager database alias. Async bulk creation remains
unsupported.
