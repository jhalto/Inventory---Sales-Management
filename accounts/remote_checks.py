"""Reserve -> remote I/O -> durable approval receipt -> local consumption."""
from datetime import timedelta
import uuid

from django.conf import settings
from django.db import connection, transaction, DatabaseError
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import APIException

from .delivery_limits import database_now
from .models import AccountChallenge, PhoneOTP, VerificationReceipt
from .otp_service import lock_phone_recipient
from .outbox import delivery_ready
from .twilio_verify import check_verification, VerifyRateLimited


class RemoteCheckUnavailable(APIException):
    status_code = 503
    default_detail = {'detail': 'Verification result is uncertain. Request a new code.', 'code': 'verification_uncertain'}


class RemoteCheckBusy(RemoteCheckUnavailable):
    wait = 2
    default_detail = {'detail': 'Verification or delivery is in progress. Retry shortly.', 'code': 'verification_busy'}


def version(record):
    return record.pk if isinstance(record, AccountChallenge) else record.version


def code_hash(kind, record, code):
    return salted_hmac('accounts.remote-proof', f'{kind}:{record.pk}:{version(record)}:{code}', algorithm='sha256').hexdigest()


def approved_receipt(kind, record, code):
    return VerificationReceipt.objects.filter(
        kind=kind, record_key=str(record.pk), version=version(record), state='approved',
        code_hash=code_hash(kind, record, code), verification_sid=record.verification_sid,
        service_sid=record.service_sid,
    ).exists()


def reserve(record, kind, code):
    now = database_now()
    if (record is not None and record.backend == 'twilio_verify' and record.delivery_id
            and not record.verification_sid and record.expires_at > now):
        return 'busy', None
    if (record is None or record.expires_at <= now or record.backend != 'twilio_verify'
            or settings.PHONE_OTP_BACKEND != 'twilio_verify' or not record.verification_sid
            or record.service_sid != settings.TWILIO_VERIFY_SERVICE_SID or not delivery_ready(record)):
        return 'invalid', None
    receipt = VerificationReceipt.objects.select_for_update().filter(
        kind=kind, record_key=str(record.pk), version=version(record),
    ).first()
    if receipt and receipt.state == 'approved':
        return ('approved' if approved_receipt(kind, record, code) else 'invalid'), receipt
    if receipt and receipt.state == 'checking':
        if receipt.lease_until > now:
            return 'busy', receipt
        receipt.state = 'unknown'
        receipt.save(update_fields=['state'])
    if receipt and receipt.state == 'unknown':
        return 'unknown', receipt
    if record.attempts >= 5:
        return 'invalid', receipt
    record.attempts += 1
    record.save(update_fields=['attempts'])
    values = dict(
        verification_sid=record.verification_sid, service_sid=record.service_sid,
        code_hash=code_hash(kind, record, code), state='checking', claim_id=uuid.uuid4(),
        lease_until=now + timedelta(seconds=90), expires_at=record.expires_at,
    )
    if receipt:
        for name, value in values.items():
            setattr(receipt, name, value)
        receipt.save()
    else:
        receipt = VerificationReceipt.objects.create(kind=kind, record_key=str(record.pk), version=version(record), **values)
    return 'call', receipt


def run_check(state, receipt, target, code):
    if state == 'busy':
        raise RemoteCheckBusy()
    if state == 'unknown':
        raise RemoteCheckUnavailable()
    if state != 'call':
        return state == 'approved'
    if connection.in_atomic_block:
        raise RuntimeError('Remote verification must run outside all transactions.')
    try:
        approved = check_verification(target, code, receipt.verification_sid)
    except Exception as exc:
        try:
            VerificationReceipt.objects.filter(pk=receipt.pk, claim_id=receipt.claim_id, state='checking').update(
                state='rejected' if isinstance(exc, VerifyRateLimited) else 'unknown',
            )
        except DatabaseError:
            raise RemoteCheckUnavailable() from None
        if isinstance(exc, VerifyRateLimited):
            raise
        raise RemoteCheckUnavailable() from None
    # Approval is committed independently of the subsequent local login/change.
    try:
        updated = VerificationReceipt.objects.filter(
            pk=receipt.pk, claim_id=receipt.claim_id, state='checking', lease_until__gt=database_now(),
        ).update(state='approved' if approved else 'rejected')
        if updated != 1:
            raise RemoteCheckUnavailable()
    except DatabaseError:
        # Never infer success from a subsequent provider 404. An expired checking
        # lease becomes unknown; the user must request another challenge.
        raise RemoteCheckUnavailable() from None
    return approved


def prepare_phone_check(phone, code):
    if settings.PHONE_OTP_BACKEND != 'twilio_verify':
        return
    with transaction.atomic():
        lock_phone_recipient(phone)
        record = PhoneOTP.objects.select_for_update().filter(phone=phone).first()
        state, receipt = reserve(record, 'phone', code)
    run_check(state, receipt, phone, code)


def prepare_account_check(challenge_id, purpose, code, user_id=None):
    snapshot = AccountChallenge.objects.filter(pk=challenge_id, purpose=purpose).first()
    if (snapshot is None or snapshot.channel != 'phone' or snapshot.backend != 'twilio_verify'
            or (user_id is not None and snapshot.user_id != user_id)):
        return
    from .self_service import locked_user, password_digest
    with locked_user(snapshot.user_id, [snapshot.target]) as user:
        record = AccountChallenge.objects.select_for_update().filter(pk=snapshot.pk, user=user, purpose=purpose).first()
        if (record is None or record.email_snapshot != (user.email or '') or record.phone_snapshot != (user.phone or '')
                or record.password_digest != password_digest(user)):
            return
        if purpose in ('reset', 'reauth') and (user.phone != record.target or not user.phone_verified):
            return
        state, receipt = reserve(record, 'account', code)
    run_check(state, receipt, snapshot.target, code)


def prepare_request_proofs(data, *, user_id=None, purpose=None):
    if purpose and 'challenge_id' in data:
        prepare_account_check(data['challenge_id'], purpose, data['otp'], user_id)
    if 'reauth_challenge_id' in data:
        prepare_account_check(data['reauth_challenge_id'], 'reauth', data['reauth_otp'], user_id)
