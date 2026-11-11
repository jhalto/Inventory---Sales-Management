import hashlib
import hmac
import secrets
import uuid

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import PhoneOTP, PhoneOTPRecipient
from .identities import normalize_phone
from .recipient_locks import database_now, lock_recipient


OTP_EXPIRY_MINUTES = 5
MAX_ATTEMPTS = 5


def lock_phone_recipient(phone, using='default'):
    """Lock the stable key before accessing a row that maintenance may prune."""
    phone = normalize_phone(phone)
    lock_recipient('phone', phone, using=using)
    recipients = PhoneOTPRecipient.objects.using(using)
    recipient, _ = recipients.select_for_update().get_or_create(phone=phone)
    recipient.last_used_at = database_now(using)
    recipient.save(using=using, update_fields=['last_used_at'])
    return recipient


def hash_otp(phone, otp):
    phone = normalize_phone(phone)
    message = f"{phone}:{otp}".encode()

    return hmac.new(
        settings.PHONE_OTP_SECRET.encode(),
        message,
        hashlib.sha256
    ).hexdigest()


def generate_otp(phone):
    phone = normalize_phone(phone)
    otp = f"{secrets.randbelow(1000000):06d}"

    PhoneOTP.objects.update_or_create(
        phone=phone,
        defaults={
            "code_hash": hash_otp(phone, otp),
            "backend": "local",
            "version": uuid.uuid4(),
            "delivery": None,
            "verification_sid": "",
            "service_sid": "",
            "attempts": 0,
            "expires_at": timezone.now()
            + timedelta(minutes=OTP_EXPIRY_MINUTES),
        }
    )

    return otp
