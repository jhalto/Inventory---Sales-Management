import secrets

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.validators import validate_email
from google.auth import exceptions
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from .identities import normalize_email


class GoogleUnavailable(Exception):
    pass


class InvalidGoogleToken(Exception):
    pass


class BoundedRequest(Request):
    def __call__(self, *args, **kwargs):
        kwargs['timeout'] = 5
        return super().__call__(*args, **kwargs)


def verify_google_token(credential, nonce):
    if not isinstance(nonce, str) or not nonce:
        raise InvalidGoogleToken
    return _verify_google_token(credential, nonce=nonce)


def verify_google_mobile_token(credential):
    """Native SDK bearer-token exchange; no browser session or nonce involved."""
    return _verify_google_token(credential)


def _verify_google_token(credential, nonce=None):
    if not settings.GOOGLE_CLIENT_ID:
        raise ImproperlyConfigured('Set GOOGLE_CLIENT_ID before enabling Google sign-in.')
    transport = BoundedRequest()
    try:
        claims = id_token.verify_oauth2_token(credential, transport, audience=settings.GOOGLE_CLIENT_ID)
    except exceptions.TransportError:
        raise GoogleUnavailable from None
    except (ValueError, exceptions.GoogleAuthError):
        raise InvalidGoogleToken from None
    finally:
        transport.session.close()
    if (
        not isinstance(claims, dict)
        or claims.get('aud') != settings.GOOGLE_CLIENT_ID
        or claims.get('iss') not in ('accounts.google.com', 'https://accounts.google.com')
        or claims.get('email_verified') is not True
        or not isinstance(claims.get('sub'), str) or not 1 <= len(claims['sub']) <= 255
        or not isinstance(claims.get('email'), str)
    ):
        raise InvalidGoogleToken
    if nonce is not None and (
        not isinstance(claims.get('nonce'), str)
        or not secrets.compare_digest(claims['nonce'].encode(), nonce.encode())
    ):
        raise InvalidGoogleToken
    email = normalize_email(claims['email'])
    if not email or len(email) > 254:
        raise InvalidGoogleToken
    from django.core.exceptions import ValidationError
    try:
        validate_email(email)
    except ValidationError:
        raise InvalidGoogleToken from None
    # Google is not authoritative for third-party addresses without Workspace.
    domain = email.rsplit('@', 1)[1]
    if domain != 'gmail.com' and claims.get('hd') != domain:
        raise InvalidGoogleToken
    return {'subject': claims['sub'], 'email': email}
