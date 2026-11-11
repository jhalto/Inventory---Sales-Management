# Production configuration

There is no hosting or production hostname yet. Local development remains on
`.env` with DEBUG enabled. Production is a separate profile and is not deployed.

Set DJANGO_ENV=production in the server environment before starting Django. This
selects the ignored `.env.production` file instead of `.env`. Existing process
environment variables take precedence over dotenv files. Do not carry development
variables into your production process.

`.env.production.example` lists the required configuration. The local
`.env.production` file has two separately generated signing secrets; the hostname,
production database, and delivery credentials still need configuration. Keep this
file private, restrict its permissions on your host, and use a secret manager
when available. Generate new deployment secrets if this local file is not used.

## When hosting is available

1. Set DJANGO_ALLOWED_HOSTS to the actual API hostname, without a scheme, path,
   port, or wildcard. Set production DB connection and delivery credentials.
2. Ensure both signing secrets are random, distinct, and at least 50 characters.
   Do not reuse development keys. Rotating them invalidates existing signed
   sessions/JWTs and OTP hashes; plan rotations deliberately.
3. Configure HTTPS and the proxy boundary as described below. Never expose the
   development server publicly as the production application server.
4. In the production environment, run:

```powershell
$env:DJANGO_ENV = 'production'
python manage.py migrate
python manage.py createcachetable
python manage.py collectstatic --noinput
python manage.py check --deploy --fail-level WARNING
```

Serve collected static files through the host or web server. Start your chosen
production WSGI/ASGI server with config.wsgi:application or config.asgi:application
and DJANGO_ENV=production. All workers must share the same secrets and PostgreSQL
primary database. Account limits use the migrated PostgreSQL ThrottleBucket
table with row locking, not the cache. The configured database cache remains
available for other cache users; no extra cache server is required. See
ACCOUNT_RATE_LIMITS.md for atomic limits and separate provider spending controls.

## Delivery worker

After migrations, supervise `python manage.py process_delivery_jobs --watch`
with the same production environment and secrets as the app. Recovery needs a
running worker even with OUTBOX_INLINE_DISPATCH=true; false queues all delivery.
Schedule cleanup_auth_artifacts daily and monitor failed/aged jobs. See
DELIVERY_WORKFLOW.md for encryption, leases, duplicate-delivery limits and remote
approval recovery. The local Windows worker task does not deploy to future hosting.

## HTTPS and trusted proxies

Production always redirects HTTP to HTTPS and uses secure session and CSRF
cookies. SECURE_PROXY_SSL_HEADER is disabled by default; forwarded host headers
are not trusted. If a trusted reverse proxy terminates HTTPS:

- Block direct public access to the Django application port.
- Have the proxy remove client-supplied X-Forwarded-Proto/X-Forwarded-For and set
  correct forwarding headers. Every trusted proxy must maintain that boundary.
- Set DJANGO_TRUST_PROXY_HEADERS=true and DJANGO_NUM_PROXIES to the actual number
  of trusted proxies. Do not guess the count. Otherwise HTTPS redirects and IP
  throttles can behave incorrectly.
- Allow only configured hosts at the proxy and redirect public HTTP to HTTPS.

For direct TLS termination, leave proxy trust false and proxy count 0. If a
browser frontend on a different trusted origin needs cookie-authenticated POSTs,
set DJANGO_CSRF_TRUSTED_ORIGINS to its explicit HTTPS origin. CORS is a separate
configuration and is not enabled by this change.

## Deliberate HSTS policy

The default policy is max-age=3600 (one hour), scoped to the current hostname.
Increase DJANGO_HSTS_SECONDS only after verifying reliable HTTPS in deployment.
Subdomain inclusion and browser preload remain disabled until domain ownership
and HTTPS for every relevant subdomain are confirmed. Consequently, only
security.W005 and security.W021 are explicitly silenced in production, with
these reasons; other deployment warnings are not silenced. No proxy-managed
HTTPS exception is used: Django also enforces HTTPS itself.

## Verification status

Automated tests cover missing/weak/reused signing secrets, host restrictions,
HTTP redirects, HTTPS HSTS headers, and opt-in proxy header trust. Deployment
checks are exercised using an explicit test hostname and generated test keys.
That demonstrates the profile's behavior, not a deployment readiness claim for
an actual host. After hosting is configured, run the commands above with the
real environment and verify TLS, host rejection, cookies, and client IPs at the
public endpoint.

The shared cache table has been provisioned and smoke-tested on the current local
PostgreSQL database. A future hosted database needs its own createcachetable run.
