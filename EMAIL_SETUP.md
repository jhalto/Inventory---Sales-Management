# Email verification setup

Registration emails a six-digit OTP and does not issue login tokens until the
address is verified. Codes expire after 5 minutes and allow at most 5 attempts.
Successful verification consumes the code. Resending replaces the previous code.

The default mailer uses Gmail SMTP on port 587 with STARTTLS.
Enable 2-Step Verification on your Google account, then create an
[app password](https://support.google.com/accounts/answer/185833).
Use that app password rather than your normal Gmail password. If the app password
option is unavailable, check Google's eligibility restrictions on that page.

Set these environment variables in the PowerShell session that starts Django.
Replace the address with your own Gmail address. The password prompt keeps the
app password out of your shell command history:

```powershell
$env:EMAIL_HOST_USER = 'yourname@gmail.com'
$gmailCredential = Get-Credential -UserName $env:EMAIL_HOST_USER -Message 'Enter your Gmail app password'
$env:EMAIL_HOST_PASSWORD = $gmailCredential.GetNetworkCredential().Password.Replace(' ', '')
python manage.py runserver
```

The sender defaults to `EMAIL_HOST_USER`; `DEFAULT_FROM_EMAIL` can override it.
Never commit passwords. Existing `EMAIL_BACKEND` or `EMAIL_HOST` environment
variables override the defaults; remove old overrides or set them to
`django.core.mail.backends.smtp.EmailBackend` and `smtp.gmail.com` respectively.
Environment variables are read when Django starts; restart after changing them.

For local testing without sending real email, set
`$env:EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'`.
Messages will appear only in the server terminal.

- Register: `POST /api/v1/register/` with `email` and `password`.
- Verify: `POST /api/v1/email/verify/` with `email` and `otp` (a six-digit string).
- Resend for existing unverified accounts:
  `POST /api/v1/email/resend-verification/` with `email`.
- After verification: `POST /api/v1/login/` with `email` and `password`.

Registration returns HTTP 503 if email sending fails, and rolls back account
creation so registration can be retried. SMTP acceptance does not guarantee inbox
delivery; check your provider's delivery logs if a sent message does not arrive.
