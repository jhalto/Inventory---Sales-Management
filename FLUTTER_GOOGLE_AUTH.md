# Flutter Android/iOS Google sign-in

The native API is ready. It reuses the existing GOOGLE_CLIENT_ID (the Web OAuth
Client ID). No new Django secret, Google client secret, cookie middleware or CORS
package is required for native Flutter clients.

## Django configuration

Keep GOOGLE_CLIENT_ID in .env, restart Django and use:

```http
POST /api/v1/auth/google/mobile/
Content-Type: application/json

{"credential": "<Google ID token from the Flutter SDK>"}
```

No Authorization header is required for sign-in. Do not send cookies, an Origin
header, a CSRF token, or a browser nonce. Requests carrying browser origins or
cookies are rejected; Flutter Web must use the browser flow in GOOGLE_SIGN_IN.md.
This native endpoint ignores ambient browser/session authentication and only
returns JWTs in JSON. It never establishes a Django login session.

Success has this shape:

```json
{
  "status": true,
  "message": "Google sign-in successful.",
  "data": {
    "user": {"id": 1, "email": "person@gmail.com", "phone": null, "role": "customer"},
    "access_token": "<app access token>",
    "refresh_token": "<app refresh token>"
  }
}
```

Google signature, expiry, issuer, audience and email ownership checks are shared
with the browser implementation. Gmail and matching Google Workspace hosted
domains are supported. A new Google account receives a verified email and no
local password; no email OTP is required for that new account. Native Google ID
tokens are bearer credentials and can be exchanged again while valid, unlike
the one-time browser session nonce. Use HTTPS and never log ID tokens or JWTs.

The Google sign-in/link endpoints share the existing 20/hour IP budget across web
and native routes. Throttle keys do not trust a client-supplied email
instead of verifying the Google token.

## Local server address

- Android emulator: http://10.0.2.2:8000/api/v1/
- iOS simulator on the same development machine: http://localhost:8000/api/v1/
- Physical device: the computer's LAN address; run Django on 0.0.0.0:8000 and add
  that exact LAN address to DJANGO_ALLOWED_HOSTS in .env. Permit access only on
  your trusted development network.

Default development ALLOWED_HOSTS includes localhost, 127.0.0.1, [::1] and
10.0.2.2 when no explicit hosts are configured. An explicit DJANGO_ALLOWED_HOSTS
overrides those defaults. Production continues to require explicit hosts and
HTTPS; no production wildcard was added. Android/iOS may require debug-only
platform networking configuration to allow local HTTP. Do not enable cleartext
HTTP for production.

## Flutter setup and example

Register Android package/signing fingerprints and the iOS bundle/URL scheme in
Google's platform configuration. Use the same Web Client ID as Django's
GOOGLE_CLIENT_ID for serverClientId, not the Android Client ID or a secret.

```bash
flutter pub add google_sign_in dio flutter_secure_storage
```

```dart
import 'package:dio/dio.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:google_sign_in/google_sign_in.dart';

class GoogleAuthService {
  final google = GoogleSignIn.instance;
  final storage = const FlutterSecureStorage();
  final api = Dio(BaseOptions(
    baseUrl: 'http://10.0.2.2:8000/api/v1/', // Android emulator only.
    headers: {'Content-Type': 'application/json'},
  ));

  // Call exactly once at app/service startup, before a button can invoke sign-in.
  Future<void> initialize() => google.initialize(
    serverClientId: 'YOUR_WEB_CLIENT_ID.apps.googleusercontent.com',
  );

  Future<Map<String, dynamic>> signIn() async {
    final account = await google.authenticate();
    final token = account.authentication.idToken;
    if (token == null) throw StateError('No Google ID token returned');

    final response = await api.post('auth/google/mobile/', data: {
      'credential': token,
    });
    final envelope = Map<String, dynamic>.from(response.data as Map);
    final data = Map<String, dynamic>.from(envelope['data'] as Map);
    await storage.write(key: 'access_token', value: data['access_token'] as String);
    await storage.write(key: 'refresh_token', value: data['refresh_token'] as String);
    return Map<String, dynamic>.from(data['user'] as Map);
  }
}
```

Do not attach a CookieManager/interceptor or Origin header to these native auth
requests. Handle SDK cancellation and DioException in the UI; do not print their
request body because it contains an ID token. The example uses google_sign_in
7.x and initialize-once semantics. Platform configuration is still required.

## Existing account linking

HTTP 409 with code google_link_required means that email already belongs to a
local account. Ask the user to authenticate using the existing method and verify
the matching email if necessary. Then explicitly link Google:

```http
POST /api/v1/auth/google/mobile/link/
Content-Type: application/json
Authorization: Bearer <existing app access token>

{"credential": "<Google ID token>"}
```

No cookie/CSRF bootstrap is needed. The endpoint uses JWTAuthentication explicitly,
requires the same verified email, and never changes the existing password or
role. It returns app tokens on success. Persist those returned tokens and handle
409 if another Google identity/account is already linked.

| HTTP status | Meaning |
| --- | --- |
| 400 | Missing or malformed JSON/credential |
| 401 | Invalid Google ID token, or missing/invalid app JWT for linking |
| 403 | Inactive/mismatched/unverified account, concurrent deletion (code account_unavailable), or browser headers/cookies |
| 409 | Existing email requires explicit linking, or a linking conflict |
| 429 | Shared Google endpoint rate limit exceeded |
| 503 | Missing Google Client ID or Google verification temporarily unavailable |

Backend tests cover native sign-in/linking, invalid claims, real RSA audience/
expiry verification, repeat exchanges, browser isolation and shared throttles.
A live Flutter-device Google consent/login test is still required on your app.
Email, phone, Google and refresh now share the same success envelope. See
AUTH_RESPONSE_CONTRACT.md for the common Dart model and client migration paths.
