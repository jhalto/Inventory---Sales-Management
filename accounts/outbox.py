"""Commit delivery intent before I/O; recover leases and retry the same payload."""
import base64
from datetime import timedelta
import json
import logging
import uuid

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.mail import send_mail
from django.db import connection, transaction
from django.db.models import Q
from django.utils.crypto import salted_hmac

from .delivery_limits import database_now
from .models import AccountChallenge, DeliveryJob, EmailOTP, PhoneOTP
from .sms_service import send_sms
from .twilio_verify import start_verification

logger = logging.getLogger(__name__)


def cipher():
    key = salted_hmac('accounts.outbox-encryption', 'v1', secret=settings.PHONE_OTP_SECRET, algorithm='sha256').digest()
    return Fernet(base64.urlsafe_b64encode(key))


def enqueue(kind, payload, expires_at):
    if not connection.in_atomic_block:
        raise RuntimeError('Enqueue must share the challenge transaction.')
    job = DeliveryJob.objects.create(
        kind=kind, payload=cipher().encrypt(json.dumps(payload).encode()).decode(),
        available_at=database_now(), expires_at=expires_at,
    )
    if getattr(settings, 'OUTBOX_INLINE_DISPATCH', True):
        transaction.on_commit(lambda: dispatch_committed(job.pk))
    return job


def dispatch_committed(job_id):
    try:
        process_job(job_id)
    except Exception as exc:
        # A failed claim/ack remains durable; never turn committed work into a
        # misleading rollback response or log a provider body/secret.
        logger.error('Outbox dispatch interrupted: %s', type(exc).__name__)


def linked_record(job):
    record = EmailOTP.objects.select_related('user').filter(delivery=job).first()
    if record:
        return record if record.user.is_active and not record.user.email_verified and record.email == record.user.email else None
    record = AccountChallenge.objects.select_related('user').filter(delivery=job).first()
    if record:
        from .self_service import password_digest
        if (record.user.is_active and (record.channel != 'phone' or record.backend == settings.PHONE_OTP_BACKEND)
                and record.email_snapshot == (record.user.email or '')
                and record.phone_snapshot == (record.user.phone or '')
                and record.password_digest == password_digest(record.user)):
            return record
        return None
    return PhoneOTP.objects.filter(delivery=job, backend=settings.PHONE_OTP_BACKEND).first()


def delivery_ready(record):
    # A delivered code remains usable if the post-send acknowledgement failed.
    return not record.delivery_id or DeliveryJob.objects.filter(pk=record.delivery_id, attempts__gt=0).exclude(state='cancelled').exists()


def process_job(job_id):
    if connection.in_atomic_block:
        raise RuntimeError('Provider dispatch must run outside all transactions.')
    with transaction.atomic():
        job = DeliveryJob.objects.select_for_update().filter(pk=job_id).first()
        now = database_now()
        if job is None or job.state in ('sent', 'failed', 'cancelled'):
            return False
        if job.state == 'sending' and job.lease_until and job.lease_until > now:
            return False
        if job.available_at > now:
            return False
        if job.expires_at <= now or linked_record(job) is None:
            job.state, job.payload = 'cancelled', ''
            job.save(update_fields=['state', 'payload'])
            return False
        if job.attempts >= 3:
            job.state = 'failed'
            job.save(update_fields=['state'])
            return False
        job.state, job.lease_id = 'sending', uuid.uuid4()
        job.lease_until = now + timedelta(seconds=90)
        job.attempts += 1
        job.save(update_fields=['state', 'lease_id', 'lease_until', 'attempts'])
        claim = job.lease_id

    try:
        payload = json.loads(cipher().decrypt(job.payload.encode()))
        if job.kind == 'email':
            accepted = send_mail(payload['subject'], payload['message'], settings.DEFAULT_FROM_EMAIL, [payload['target']], using='default')
            if accepted != 1:
                raise RuntimeError('Mail submission not accepted.')
            reference = ''
        elif job.kind == 'sms':
            send_sms(payload['target'], payload['message'])
            reference = ''
        elif job.kind == 'twilio_start':
            if payload['service_sid'] != settings.TWILIO_VERIFY_SERVICE_SID or settings.PHONE_OTP_BACKEND != 'twilio_verify':
                raise RuntimeError('Verify configuration changed.')
            reference = start_verification(payload['target'])
        else:
            raise RuntimeError('Unsupported delivery kind.')
    except Exception as exc:
        # Submission may have succeeded before a timeout. Preserve ciphertext
        # and the existing challenge; retries never generate a replacement code.
        DeliveryJob.objects.filter(pk=job.pk, lease_id=claim, state='sending').update(
            state='unknown', last_error=type(exc).__name__[:80],
            available_at=database_now() + timedelta(seconds=5 * 2 ** (job.attempts - 1)),
            lease_until=None,
        )
        logger.warning('Outbox provider attempt uncertain: %s', type(exc).__name__)
        return False

    # If this commit fails, the sending lease survives and a worker retries.
    with transaction.atomic():
        job = DeliveryJob.objects.select_for_update().get(pk=job.pk)
        if job.lease_id != claim or job.state != 'sending':
            return False
        if job.expires_at <= database_now() or linked_record(job) is None:
            job.state = 'cancelled'
        else:
            if reference:
                for model in (PhoneOTP, AccountChallenge):
                    model.objects.filter(delivery=job).update(verification_sid=reference)
            job.state, job.accepted_at = 'sent', database_now()
        job.payload, job.last_error, job.lease_until = '', '', None
        job.save(update_fields=['state', 'accepted_at', 'payload', 'last_error', 'lease_until'])
    return job.state == 'sent'


def process_pending(limit=100):
    now = database_now()
    ids = list(DeliveryJob.objects.filter(available_at__lte=now).filter(
        Q(state__in=['pending', 'unknown']) | Q(state='sending', lease_until__lte=now),
    ).order_by('available_at').values_list('pk', flat=True)[:limit])
    for job_id in ids:
        dispatch_committed(job_id)
    return len(ids)
