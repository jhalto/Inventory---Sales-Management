from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.http.request import split_domain_port


def production_security(environ):
    secrets = [environ.get(name, '') for name in ('DJANGO_SECRET_KEY', 'PHONE_OTP_SECRET')]
    if any(len(value) < 50 or len(set(value)) < 5 or value.startswith('django-insecure-') for value in secrets):
        raise ImproperlyConfigured('Production requires two strong signing secrets of at least 50 characters.')
    if secrets[0] == secrets[1]:
        raise ImproperlyConfigured('DJANGO_SECRET_KEY and PHONE_OTP_SECRET must be different.')
    hosts = [host.strip().lower() for host in environ.get('DJANGO_ALLOWED_HOSTS', '').split(',') if host.strip()]
    if not hosts or any(not split_domain_port(host)[0] or split_domain_port(host)[1] or host.startswith('.') or '*' in host for host in hosts):
        raise ImproperlyConfigured('Set explicit DJANGO_ALLOWED_HOSTS without schemes, ports, or wildcards.')

    proxy_value = environ.get('DJANGO_TRUST_PROXY_HEADERS', 'false').lower()
    if proxy_value not in ('true', 'false'):
        raise ImproperlyConfigured('DJANGO_TRUST_PROXY_HEADERS must be true or false.')
    trust_proxy = proxy_value == 'true'
    try:
        proxies = int(environ.get('DJANGO_NUM_PROXIES', '0'))
        hsts = int(environ.get('DJANGO_HSTS_SECONDS', '3600'))
    except ValueError:
        raise ImproperlyConfigured('Proxy count and HSTS seconds must be integers.') from None
    if proxies < 0 or (trust_proxy and proxies < 1) or (not trust_proxy and proxies != 0):
        raise ImproperlyConfigured('Set an explicit positive proxy count only when trusted proxy headers are enabled.')
    if hsts < 1:
        raise ImproperlyConfigured('Production HSTS must be a positive number of seconds.')

    origins = [origin.strip() for origin in environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if origin.strip()]
    if any(urlsplit(origin).scheme != 'https' or not urlsplit(origin).hostname or urlsplit(origin).path or urlsplit(origin).query or urlsplit(origin).fragment or urlsplit(origin).username for origin in origins):
        raise ImproperlyConfigured('CSRF trusted origins must be explicit HTTPS origins without paths or credentials.')

    return {
        'ALLOWED_HOSTS': hosts,
        'SECURE_SSL_REDIRECT': True,
        'SESSION_COOKIE_SECURE': True,
        'CSRF_COOKIE_SECURE': True,
        'CSRF_TRUSTED_ORIGINS': origins,
        'SECURE_HSTS_SECONDS': hsts,
        'SECURE_HSTS_INCLUDE_SUBDOMAINS': False,
        'SECURE_HSTS_PRELOAD': False,
        # No domain is deployed yet: do not commit all subdomains to HTTPS or
        # request browser preload. These two warnings reflect that policy only.
        'SILENCED_SYSTEM_CHECKS': ['security.W005', 'security.W021'],
        'SECURE_CONTENT_TYPE_NOSNIFF': True,
        'SECURE_PROXY_SSL_HEADER': ('HTTP_X_FORWARDED_PROTO', 'https') if trust_proxy else None,
        'USE_X_FORWARDED_HOST': False,
        'ACCOUNT_NUM_PROXIES': proxies,
    }
