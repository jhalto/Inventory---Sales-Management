import hashlib
import hmac
import secrets

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import PhoneOTP


OTP_EXPIRY_MINUTES = 5
MAX_ATTEMPTS = 5


def hash_otp(phone, otp):
    message = f"{phone}:{otp}".encode()

    return hmac.new(
        settings.PHONE_OTP_SECRET.encode(),
        message,
        hashlib.sha256
    ).hexdigest()


def generate_otp(phone):
    otp = f"{secrets.randbelow(1000000):06d}"

    PhoneOTP.objects.update_or_create(
        phone=phone,
        defaults={
            "code_hash": hash_otp(phone, otp),
            "attempts": 0,
            "expires_at": timezone.now()
            + timedelta(minutes=OTP_EXPIRY_MINUTES),
        }
    )

    return otp