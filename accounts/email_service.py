import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import EmailOTP, User


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
    otp = f'{secrets.randbelow(1000000):06d}'
    EmailOTP.objects.update_or_create(
        user=user,
        defaults={
            'email': user.email,
            'code_hash': hash_email_otp(user.pk, user.email, otp),
            'attempts': 0,
            'expires_at': timezone.now() + timedelta(minutes=EMAIL_OTP_EXPIRY_MINUTES),
        },
    )
    sent = send_mail(
        subject='Your email verification code',
        message=(
            f'Your email verification code is: {otp}\n\n'
            f'This code expires in {EMAIL_OTP_EXPIRY_MINUTES} minutes. '
            'If you did not register, you can ignore this email.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        using='default',
    )
    if sent != 1:
        raise RuntimeError('Verification email was not accepted by the mail backend.')
