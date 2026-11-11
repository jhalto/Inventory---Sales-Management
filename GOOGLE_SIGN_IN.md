# Sign in with Google

For native Flutter Android/iOS use FLUTTER_GOOGLE_AUTH.md and the separate
POST /api/v1/auth/google/mobile/ endpoint. The browser handshake below remains
required for browser and Flutter Web clients.

## Local setup

1. In Google Cloud Console, configure the Google Auth Platform branding/audience
   and create an OAuth client of type Web application. If using testing mode,
   add your Google account as a test user where required by the console.
2. Add http://localhost and http://localhost:8000 as authorized JavaScript origins.
   This implementation uses the JavaScript callback, not a Google redirect URI.
3. Put GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com in .env and
   restart Django. No Google client secret is used or needed for this flow.
4. Run migrations, start the server, and open:

```text
http://localhost:8000/api/v1/auth/google/sign-in/
```

The page displays Google's official button. A new supported Google identity
creates a customer account with verified email and an unusable local password.
No email OTP or Google password is sent to this application. The page confirms
sign-in and holds app tokens only in memory; the project has no dashboard to
redirect to yet. Reloading the page clears those frontend tokens.

Existing email/password users must explicitly link Google: after the Google
button identifies an existing email, the page offers an account confirmation
form. Use your app registration password, not a Gmail password/app password.
The local account must already have a verified matching email. If it is
unverified, complete the existing email OTP flow first, then return to link.
No existing password or account role is replaced during linking.

For production, configure the real HTTPS JavaScript origin in Google Console and
GOOGLE_CLIENT_ID in the production environment. This page deliberately permits
Google sign-in popups through a page-specific COOP header. It does not enable
cross-origin API access; keep this flow same-origin or design CORS/cookie
deployment rules separately for a future frontend.

## API handshake

GET /api/v1/auth/google/config/ establishes a session and CSRF cookie, returning
client_id, nonce and csrf_token. Keep the same cookie jar for the handshake.
Pass client_id and nonce to google.accounts.id.initialize. The Google callback's
response.credential is the ID token to submit:

```http
POST /api/v1/auth/google/
Content-Type: application/json
X-CSRFToken: <csrf_token from config>

{"credential": "<Google ID token>"}
```

Include the session/CSRF cookies. Success returns data.user, data.access_token and
data.refresh_token, shared with email/phone/refresh (AUTH_RESPONSE_CONTRACT.md).
Never submit a Google access token, app JWT, client ID, email
address or decoded token claims in place of the Google ID token.

For an existing verified local account, POST /api/v1/auth/google/link/ with the
same payload, cookies and CSRF header plus Authorization: Bearer <app access
token>. The bearer must authenticate the matching local account. A different
Google subject cannot replace an existing link, and a subject cannot link to
two accounts. Conflicts return 409; inactive accounts return 403.

The session nonce is consumed on successful sign-in/linking. Start another
handshake before another sign-in attempt after success. Failed attempts are
limited to 20 requests/hour per IP, including authenticated link requests.

Browser challenges expire after 10 minutes and are bound to an internal random
session binding. The challenge table stores keyed hashes of that binding and the
nonce, not the Google credential. A new handshake replaces the previous challenge
for that session. After Google verifies the token, Django locks and re-checks the
challenge row, then consumes it in the same transaction that creates/links the
account and issues app tokens. Concurrent exchanges can produce only one success.
Failed account/token transactions roll back challenge consumption so a valid
request can retry. Existing pre-migration browser sessions must start a new
handshake because they have no database challenge record.

Periodically remove expired challenge records with:

```powershell
python manage.py cleanup_google_challenges
```

This preserves active challenges. Continue scheduling Django's clearsessions
separately for expired sessions. These browser challenge rules do not apply to
native Flutter bearer ID-token exchanges, which remain reusable until token expiry.

## Identity validation and limitations

Google's official google-auth library verifies signature, audience, expiry and
issuer. The application also checks the session nonce, verified email and stable
Google sub claim. Subsequent logins identify users by sub, not changing email
addresses. Google credentials are never returned or logged by these endpoints.

Supported addresses are Gmail and Google Workspace addresses whose hd claim
matches the email domain. Other third-party addresses used for Google Accounts
must use the normal email/password verification flow; Google is not authoritative
for their current mailbox ownership. See Google's guidance:

- https://developers.google.com/identity/gsi/web/guides/verify-google-id-token
- https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid

Automated tests use locally signed RSA tokens to check real signature/audience/
expiry verification, plus mocked Google responses for account creation, linking,
inactive accounts, nonce replay, CSRF and provider failures. They do not simulate
a real Google browser consent interaction. Perform that final test in your
browser using the configured client and an account you control.
