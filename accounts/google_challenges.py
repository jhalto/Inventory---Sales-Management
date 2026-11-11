import secrets
from datetime import timedelta

from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.db import transaction

from .models import GoogleBrowserChallenge


def digest(value, purpose):
    return salted_hmac('accounts.google.' + purpose, value, algorithm='sha256').hexdigest()


@transaction.atomic
def issue_browser_challenge(session):
    binding = session.get('google_sign_in_binding')
    if not isinstance(binding, str) or not binding:
        binding = secrets.token_urlsafe(32)
        session['google_sign_in_binding'] = binding
    nonce = session.get('google_sign_in_nonce')
    record = GoogleBrowserChallenge.objects.select_for_update().filter(
        session_digest=digest(binding, 'session'),
    ).first()
    if (record is not None and record.consumed_at is None
            and record.expires_at > timezone.now()
            and isinstance(nonce, str) and nonce
            and record.nonce_digest == digest(nonce, 'nonce')):
        return nonce
    nonce = secrets.token_urlsafe(32)
    GoogleBrowserChallenge.objects.update_or_create(
        session_digest=digest(binding, 'session'),
        defaults={'nonce_digest': digest(nonce, 'nonce'),
                  'expires_at': timezone.now() + timedelta(minutes=10), 'consumed_at': None},
    )
    session['google_sign_in_nonce'] = nonce
    if not session.get('_auth_user_id'):
        # Anonymous Google handshakes should not retain Django's two-week cookie.
        session.set_expiry(10 * 60)
    return nonce


def browser_challenge_key(session):
    nonce = session.get('google_sign_in_nonce')
    binding = session.get('google_sign_in_binding')
    if not isinstance(nonce, str) or not nonce or not isinstance(binding, str) or not binding:
        return None
    return {'session_digest': digest(binding, 'session'), 'nonce_digest': digest(nonce, 'nonce')}
