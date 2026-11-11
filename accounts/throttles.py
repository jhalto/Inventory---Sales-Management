from collections.abc import Mapping

from django.conf import settings
from django.utils.crypto import salted_hmac
from rest_framework import serializers
from rest_framework.throttling import BaseThrottle
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from datetime import datetime, timezone
import logging
from django.db import DatabaseError
from django.db.models.functions import Now
from rest_framework.exceptions import APIException

from .delivery_limits import database_now
from .models import ThrottleBucket

from .serializers import normalize_phone
from .identities import normalize_email

logger = logging.getLogger(__name__)


class ThrottleUnavailable(APIException):
    status_code = 503
    default_detail = 'Request limit service unavailable. Please try again later.'


DEFAULT_RATES = {
    'google_challenge_ip': '30/hour',
    'management_ip': '60/hour',
    'management_user': '30/hour',
    'token_ip': '60/hour',
    'google_ip': '20/hour',
    'register_ip': '5/hour',
    'login_ip': '20/hour',
    'login_email': '10/hour',
    'email_resend_ip': '5/hour',
    'email_send': '5/hour',
    'email_cooldown': '1/minute',
    'email_verify_ip': '30/hour',
    'email_verify_recipient': '10/hour',
    'phone_send_ip': '10/hour',
    'phone_send': '5/hour',
    'phone_cooldown': '1/minute',
    'phone_verify_ip': '30/hour',
    'phone_verify_recipient': '10/hour',
}


class AccountThrottle(BaseThrottle):
    """Serialize rolling-window reservations in PostgreSQL, never in cache."""

    cache_format = 'throttle_%(scope)s_%(ident)s'
    timer = staticmethod(lambda: database_now().timestamp())

    def __init__(self):
        self.rate = self.get_rate()
        self.retry_after = None
        if self.rate is not None:
            try:
                count, period = self.rate.split('/')
                self.num_requests = int(count)
                self.duration = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[period[0]]
                if self.num_requests <= 0:
                    raise ValueError
            except (ValueError, KeyError, IndexError, AttributeError):
                raise ImproperlyConfigured(f'Invalid account throttle rate for {self.scope}.') from None

    def allow_request(self, request, view):
        if self.rate is None:
            return True
        key = self.get_cache_key(request, view)
        if key is None:
            return True
        if connection.vendor != 'postgresql':
            raise ImproperlyConfigured('Atomic account throttles require PostgreSQL.')
        try:
            with transaction.atomic():
                bucket, _ = ThrottleBucket.objects.select_for_update().get_or_create(
                    key=key, defaults={'expires_at': Now()},
                )
                now = self.timer()  # Sample only after acquiring the lock.
                history = [stamp for stamp in bucket.history if stamp > now - self.duration]
                if len(history) >= self.num_requests:
                    self.retry_after = max(0, history[0] + self.duration - now)
                    return False
                history.append(now)
                bucket.history = history
                bucket.expires_at = datetime.fromtimestamp(now + self.duration, tz=timezone.utc)
                bucket.save(update_fields=['history', 'expires_at'])
        except DatabaseError as exc:
            logger.error('Account limiter unavailable: %s', type(exc).__name__)
            raise ThrottleUnavailable() from None
        return True

    def wait(self):
        return self.retry_after

    def get_rate(self):
        return getattr(settings, 'ACCOUNT_THROTTLE_RATES', {}).get(
            self.scope, DEFAULT_RATES[self.scope]
        )

    def get_cache_key(self, request, view):
        return self.identity_key(self.get_ident(request))

    def identity_key(self, identity):
        digest = salted_hmac('accounts.throttle', identity, algorithm='sha256').hexdigest()
        return self.cache_format % {'scope': self.scope, 'ident': digest}


class RecipientThrottle(AccountThrottle):
    field = 'email'

    def get_cache_key(self, request, view):
        if not isinstance(request.data, Mapping):
            return None
        value = request.data.get(self.field)
        if not isinstance(value, str):
            return None
        try:
            if self.field == 'phone':
                value = normalize_phone(value)
            else:
                value = normalize_email(serializers.EmailField().run_validation(value))
        except serializers.ValidationError:
            return None
        return self.identity_key(value)


class GoogleSignInThrottle(AccountThrottle):
    scope = 'google_ip'


class GoogleChallengeIPThrottle(AccountThrottle):
    scope = 'google_challenge_ip'


class TokenIPThrottle(AccountThrottle):
    scope = 'token_ip'


class ManagementIPThrottle(AccountThrottle):
    scope = 'management_ip'


class ManagementUserThrottle(AccountThrottle):
    scope = 'management_user'

    def get_cache_key(self, request, view):
        return self.identity_key(str(request.user.pk)) if request.user.is_authenticated else None


class RegisterIPThrottle(AccountThrottle):
    scope = 'register_ip'


class LoginIPThrottle(AccountThrottle):
    scope = 'login_ip'


class LoginEmailThrottle(RecipientThrottle):
    scope = 'login_email'


class EmailResendIPThrottle(AccountThrottle):
    scope = 'email_resend_ip'


class EmailSendThrottle(RecipientThrottle):
    scope = 'email_send'


class EmailCooldownThrottle(RecipientThrottle):
    scope = 'email_cooldown'


class EmailVerifyIPThrottle(AccountThrottle):
    scope = 'email_verify_ip'


class EmailVerifyRecipientThrottle(RecipientThrottle):
    scope = 'email_verify_recipient'


class PhoneSendIPThrottle(AccountThrottle):
    scope = 'phone_send_ip'


class PhoneSendThrottle(RecipientThrottle):
    scope = 'phone_send'
    field = 'phone'


class PhoneCooldownThrottle(RecipientThrottle):
    scope = 'phone_cooldown'
    field = 'phone'


class PhoneVerifyIPThrottle(AccountThrottle):
    scope = 'phone_verify_ip'


class PhoneVerifyRecipientThrottle(RecipientThrottle):
    scope = 'phone_verify_recipient'
    field = 'phone'
