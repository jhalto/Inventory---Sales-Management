"""Purpose-bound proofs for account recovery and contact management."""
from contextlib import contextmanager
from datetime import timedelta
import hmac
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from .models import AccountChallenge, PhoneOTP, User
from .delivery_limits import reserve_delivery
from .outbox import enqueue, delivery_ready
from .otp_service import lock_phone_recipient


class IdentityConflict(APIException):
    status_code = 409
    default_detail = 'This identity is already registered to another account.'


def password_digest(user):
    return salted_hmac('accounts.challenge-password', user.password, algorithm='sha256').hexdigest()


def code_digest(record, code):
    return salted_hmac(
        'accounts.self-service',
        f'{record.pk}:{record.user_id}:{record.purpose}:{record.channel}:{record.target}:{code}',
        algorithm='sha256',
    ).hexdigest()


@contextmanager
def locked_user(user_id, extra_phones=()):
    snapshot = User.objects.filter(pk=user_id).values('phone').first()
    if snapshot is None:
        raise PermissionDenied('Account unavailable.')
    with transaction.atomic():
        for phone in sorted({value for value in (snapshot['phone'], *extra_phones) if value}):
            lock_phone_recipient(phone)
        user = User.objects.select_for_update().filter(pk=user_id, is_active=True).first()
        if user is None:
            raise PermissionDenied('Account unavailable.')
        if user.phone != snapshot['phone']:
            raise ValidationError('Account changed. Start again.')
        yield user


def current_verified_target(user, channel):
    target = getattr(user, channel)
    if not target or not getattr(user, channel + '_verified'):
        raise ValidationError('A verified contact is required for this operation.')
    return target


def issue_challenge(user, purpose, channel, target):
    now = timezone.now()
    reserve_delivery(channel, target)

    AccountChallenge.objects.filter(channel=channel, target=target).delete()
    if channel == 'phone':
        PhoneOTP.objects.filter(phone=target).delete()
    record = AccountChallenge(
        user=user, purpose=purpose, channel=channel, target=target,
        email_snapshot=user.email or '', phone_snapshot=user.phone or '',
        password_digest=password_digest(user), expires_at=now + timedelta(minutes=5),
    )
    code = f'{secrets.randbelow(1000000):06d}'
    record.code_hash = code_digest(record, code)
    payload = {'target': target}
    if channel == 'phone' and settings.PHONE_OTP_BACKEND == 'twilio_verify':
        record.backend = 'twilio_verify'
        record.service_sid = settings.TWILIO_VERIFY_SERVICE_SID
        kind = 'twilio_start'
        payload['service_sid'] = record.service_sid
    elif channel == 'phone':
        kind = 'sms'
        payload['message'] = f'Your account {purpose} code is {code}. It expires in 5 minutes.'
    else:
        kind = 'email'
        payload.update(subject=f'Your account {purpose} code', message=f'Your account {purpose} code is {code}. It expires in 5 minutes. Do not share this code.')
    record.delivery = enqueue(kind, payload, record.expires_at)
    record.save()
    return record


def check_code(user, challenge_id, purpose, code):
    record = AccountChallenge.objects.select_for_update().filter(
        pk=challenge_id, user=user, purpose=purpose,
    ).first()
    if (record is None or record.expires_at <= timezone.now() or not delivery_ready(record)
            or record.email_snapshot != (user.email or '')
            or record.phone_snapshot != (user.phone or '')
            or not hmac.compare_digest(record.password_digest, password_digest(user))):
        return None
    if purpose in ('reset', 'reauth') and (
        getattr(user, record.channel) != record.target
        or not getattr(user, record.channel + '_verified')
    ):
        return None
    if record.channel == 'phone':
        if record.backend != settings.PHONE_OTP_BACKEND:
            return None
        if record.backend == 'twilio_verify':
            if not record.verification_sid or record.service_sid != settings.TWILIO_VERIFY_SERVICE_SID:
                return None
            from .remote_checks import approved_receipt
            return record if approved_receipt('account', record, code) else None
    if record.attempts >= 5:
        return None
    record.attempts += 1
    record.save(update_fields=['attempts'])
    return record if hmac.compare_digest(record.code_hash, code_digest(record, code)) else None


def check_step_up(user, data):
    if 'current_password' in data:
        return user.has_usable_password() and user.check_password(data['current_password'])
    record = check_code(user, data['reauth_challenge_id'], 'reauth', data['reauth_otp'])
    if record:
        record.delete()
        return True
    return False


def check_available(user, channel, target):
    lookup = {'email__iexact': target} if channel == 'email' else {'phone': target}
    if User.objects.filter(**lookup).exclude(pk=user.pk).exists():
        raise IdentityConflict()
