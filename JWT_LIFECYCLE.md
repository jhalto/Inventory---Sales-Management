# JWT lifecycle

Email, phone and Google sign-in issue access tokens valid for 30 minutes and
refresh tokens valid for seven days. Both contain a password-revocation claim.
Changing the stored password hash invalidates all previously issued access and
refresh tokens, including password changes in the admin. Inactive/deleted users
cannot authenticate or refresh. Tokens issued before this policy lack the claim
and require a fresh sign-in after deployment.

## Refresh

POST `/api/v1/token/refresh/` with JSON:

```json
{"refresh": "<current refresh token>"}
```

Success (200) returns the common authentication envelope with `data.user`,
`data.access_token` and `data.refresh_token` (see AUTH_RESPONSE_CONTRACT.md). Store the new pair
securely and replace the old refresh token. Rotation blacklists the submitted
refresh token; it cannot be used again. Each replacement has a new seven-day
expiry; there is no absolute session-duration limit. Serialize refresh operations
in the client; concurrent use of the same refresh token produces one success.
The database transaction locks the account and rechecks revocation after waiting.

## Logout

POST `/api/v1/logout/` with the same JSON shape and the latest refresh token.
Success returns 204 with no body. The submitted refresh token is blacklisted.
Other devices' refresh tokens remain valid. The client must delete its stored
pair even if logout fails or the network is unavailable.

Logout does not immediately revoke already issued access tokens: they remain
usable until their 30-minute expiry, unless the password changes or the account
is deactivated/deleted. Rotation likewise does not revoke existing access tokens.
This policy does not provide logout-all-devices or theft/replay family revocation.
Requests already authenticated before a password/account change may complete.

Both endpoints authenticate through the refresh token in the body; an expired
Authorization header does not block them. They return 400 for invalid request
shapes, 401 for expired/invalid/revoked tokens or unavailable accounts, and 429
for the shared token IP throttle (60/hour by default). A repeated logout with
the same token returns 401. HTTPS and secure client token storage are required
in production; do not log tokens or place them in URLs.

## Deployment and maintenance

Run `python manage.py migrate` before serving traffic to create Simple JWT's
outstanding/blacklisted token tables. Schedule
`python manage.py flushexpiredtokens` daily to remove expired records. Existing
All app-token issuing routes use AUTH_RESPONSE_CONTRACT.md, including refresh.
Production must use the shared PostgreSQL limiter
and deliberate proxy settings already described in deployment documentation.
