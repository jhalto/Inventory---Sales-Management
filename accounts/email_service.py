import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import EmailOTP, User
from .delivery_limits import reserve_delivery
from .outbox import enqueue


EMAIL_OTP_EXPIRY_MINUTES = 5
EMAIL_OTP_MAX_ATTEMPTS = 5


def hash_email_otp(user_id, email, otp):
    return salted_hmac(
        'accounts.email-otp', f'{user_id}:{email}:{otp}', algorithm='sha256'
    ).hexdigest()


@transaction.atomic
def send_verification_email(user):
    user = User.objects.select_for_update().get(pk=user.pk)
    if user.email_verified or not user.is_active or not user.email:
        return
    reserve_delivery('email', user.email)
    otp = f'{secrets.randbelow(1000000):06d}'
    expires_at = timezone.now() + timedelta(minutes=EMAIL_OTP_EXPIRY_MINUTES)
    job = enqueue('email', {
        'target': user.email, 'subject': 'Your email verification code',
        'message': f'Your email verification code is: {otp}\n\nThis code expires in 5 minutes. If you did not register, ignore this email.',
    }, expires_at)
    EmailOTP.objects.update_or_create(
        user=user,
        defaults={
            'email': user.email,
            'code_hash': hash_email_otp(user.pk, user.email, otp),
            'attempts': 0,
            'expires_at': expires_at,
            'delivery': job,
        },
    )
    return job
