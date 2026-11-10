# Phone OTP setup

## Free local testing

Local mode defaults to `DJANGO_DEBUG=true` and
`SMS_BACKEND=accounts.sms_service.console_send_sms`. Start Django normally:

```powershell
python manage.py runserver
```

Send `POST /api/v1/phone/send-otp/`:

```json
{"phone": "01712345678"}
```

Read the six-digit code from the server terminal, then send
`POST /api/v1/phone/verify-otp/`:

```json
{"phone": "01712345678", "otp": "123456"}
```

Replace `123456` with the printed code. Verification returns JWT tokens.
No real SMS is sent. Bangladeshi local numbers and +880 numbers are accepted.

## Production SMS through Twilio

Use a funded Twilio account with Programmable Messaging enabled for Bangladesh
and a sender approved for your destination. Trial accounts restrict message
content and recipients, so do not assume they support this custom OTP body.
See [Twilio's Messages API](https://www.twilio.com/docs/messaging/api/message-resource).

Configure these environment variables on the server before starting Django:

```powershell
$env:DJANGO_DEBUG = 'false'
$env:DJANGO_ALLOWED_HOSTS = 'api.yourdomain.com'
$env:DJANGO_SECRET_KEY = '<unique-random-secret>'
$env:PHONE_OTP_SECRET = '<separate-unique-random-secret>'
$env:SMS_BACKEND = 'accounts.sms_service.twilio_send_sms'
$env:TWILIO_ACCOUNT_SID = '<account-sid>'
$env:TWILIO_AUTH_TOKEN = '<auth-token>'
$env:TWILIO_FROM_NUMBER = '<approved-sender>'
python manage.py migrate
python manage.py createcachetable
```

Enter real values using your deployment's secret manager; do not commit secrets.
You can generate each secret separately with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Instead of `TWILIO_FROM_NUMBER`, you may set `TWILIO_MESSAGING_SERVICE_SID`
to your Messaging Service SID. The service takes precedence when both are set.
No extra SMS package is required. Restart the application after settings change.
Use your production application server behind HTTPS; these settings configure
SMS, not the entire deployment.

Production uses a database-backed throttle cache, so all application workers
must share the same database. Run `createcachetable` before serving requests.
For concurrent production OTP traffic use a database supporting row locks,
such as PostgreSQL; the project's default SQLite database has limited concurrency.
If behind a proxy, configure DRF's trusted proxy count to match your deployment.

Both modes use hashed OTPs, five-minute expiry, five verification attempts,
a 60-second resend cooldown, and IP limits of 10 sends/hour and 30 checks/hour.
DRF cache throttles are approximate under concurrency; configure provider spending
limits and deployment rate limits as well. Console SMS refuses to run when
`DEBUG=False`. Production never falls back to printing codes.

Provider submission failures return HTTP 503 and roll back the new code,
preserving any previous code. HTTP 200 means the provider accepted submission;
check Twilio delivery logs for final delivery. A network timeout can leave
delivery uncertain; request a new code after the cooldown if needed.
