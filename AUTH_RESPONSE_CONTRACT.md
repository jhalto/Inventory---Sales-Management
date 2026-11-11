# Authentication success contract

Every successful app-token response is HTTP 200 with `Cache-Control: no-store`
and the same JSON shape:

```json
{
  "status": true,
  "message": "Login successful",
  "data": {
    "user": {
      "id": 1,
      "email": "person@example.com",
      "phone": null,
      "role": "customer"
    },
    "access_token": "<app access JWT>",
    "refresh_token": "<app refresh JWT>"
  }
}
```

`status` is a boolean, `message` is display text, and `data` is an object.
`user.id` is an integer; email and phone are strings or null; role is one of
`admin`, `staff`, `customer`. Tokens are nonempty strings. The public user object
contains only these four fields. Clients should select behavior by HTTP status
and fields, rather than matching the message text.

This contract applies to:

- `POST /api/v1/login/`
- `POST /api/v1/phone/verify-otp/`
- `POST /api/v1/auth/google/`
- `POST /api/v1/auth/google/link/`
- `POST /api/v1/auth/google/mobile/`
- `POST /api/v1/auth/google/mobile/link/`
- `POST /api/v1/token/refresh/`

Refresh still accepts `{"refresh":"<current refresh token>"}`; persist both
replacement tokens from the common response. See JWT_LIFECYCLE.md for rotation,
logout and password revocation. Google SDK credentials are never app JWTs.

## Client migration

This deliberately replaces the previous inconsistent success bodies within the
development v1 API. Update clients together with the backend:

| Previous response path | Common response path |
| --- | --- |
| Email `data.id/email/phone/role` | `data.user.id/email/phone/role` |
| Email `data.access_token/refresh_token` | Same paths |
| Google `data.id/email/phone/role` | `data.user.id/email/phone/role` |
| Google top-level `access_token/refresh_token` | `data.access_token/refresh_token` |
| Phone top-level `user` | `data.user` |
| Phone top-level `access_token/refresh_token` | `data.access_token/refresh_token` |
| Refresh top-level `access/refresh` | `data.access_token/refresh_token` |

There are no duplicate legacy token aliases. The browser sign-in page and
FLUTTER_GOOGLE_AUTH.md example use the common paths. One Dart parser can handle
all seven success responses after checking HTTP 200:

```dart
class AuthUser {
  final int id;
  final String? email;
  final String? phone;
  final String role;

  AuthUser.fromJson(Map<String, dynamic> json)
      : id = json['id'] as int,
        email = json['email'] as String?,
        phone = json['phone'] as String?,
        role = json['role'] as String;
}

class AuthResult {
  final AuthUser user;
  final String accessToken;
  final String refreshToken;

  AuthResult._(this.user, this.accessToken, this.refreshToken);

  factory AuthResult.fromJson(Map<String, dynamic> json) {
    if (json['status'] != true) {
      throw const FormatException('Expected an authentication success');
    }
    final data = Map<String, dynamic>.from(json['data'] as Map);
    return AuthResult._(
      AuthUser.fromJson(Map<String, dynamic>.from(data['user'] as Map)),
      data['access_token'] as String,
      data['refresh_token'] as String,
    );
  }
}
```

## Other responses

Registration and email verification do not issue tokens. OTP requests, password
recovery/change and contact management retain their documented response bodies.
Logout and Google unlink succeed with 204 and no body. Existing failure bodies
and Google `google_link_required` handling remain unchanged; parse errors using
the endpoint documentation, not this success model.

Contract tests exercise every issuing route using rendered JSON and validate the
returned JWTs against the expected account. Transaction/concurrency tests retain
their one-time OTP/browser challenge and refresh-consumption assertions.
