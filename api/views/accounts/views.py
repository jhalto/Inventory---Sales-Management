from rest_framework.response import Response
from accounts.serializers import LoginSerializer, ResendVerificationSerializer, UserSerializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from accounts.models import User
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.core.exceptions import ImproperlyConfigured
from smtplib import SMTPException
import logging
from rest_framework import serializers
from accounts.email_service import EMAIL_OTP_MAX_ATTEMPTS, hash_email_otp, send_verification_email
from accounts.models import EmailOTP
import hmac
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import (
    api_view,
    permission_classes,
    throttle_classes,
)

from accounts.throttles import (
    RegisterIPThrottle, LoginIPThrottle, LoginEmailThrottle,
    EmailResendIPThrottle, EmailSendThrottle, EmailCooldownThrottle,
    EmailVerifyIPThrottle, EmailVerifyRecipientThrottle,
    PhoneSendIPThrottle, PhoneSendThrottle, PhoneCooldownThrottle,
    PhoneVerifyIPThrottle, PhoneVerifyRecipientThrottle,
)

from accounts.models import User, PhoneOTP, AccountChallenge
from accounts.serializers import SendOTPSerializer, VerifyOTPSerializer,VerifyEmailOTPSerializer
from accounts.otp_service import (
    generate_otp,
    hash_otp,
    MAX_ATTEMPTS,
    OTP_EXPIRY_MINUTES,
    lock_phone_recipient,
)
from accounts.sms_service import SMSDeliveryError
from accounts.twilio_verify import VerifyRateLimited
from .responses import authentication_response
from accounts.outbox import enqueue, delivery_ready
from accounts.remote_checks import prepare_phone_check, approved_receipt
import uuid

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([RegisterIPThrottle, EmailSendThrottle, EmailCooldownThrottle])
def register(request):

    serializer = UserSerializers(data=request.data)

    if not serializer.is_valid():
        return Response(
            {
                "status": False,
                "message": "Invalid data",
                "errors": serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with transaction.atomic():
            user = serializer.save()
            send_verification_email(user)
    except IntegrityError:
        # The atomic block has rolled back, so this lookup uses a valid transaction.
        if not User.objects.filter(email__iexact=serializer.validated_data['email']).exists():
            raise
        return Response(
            {
                'status': False,
                'message': 'Invalid data',
                'errors': {'email': ['This email is already registered.']},
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    except (SMTPException, OSError, RuntimeError, ImproperlyConfigured) as exc:
        logger.error('Verification email failed: %s (SMTP status: %s)', type(exc).__name__, getattr(exc, 'smtp_code', None))
        return Response(
            {"status": False, "message": "Unable to send verification email. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        {
            "status": True,
            "message": "Account created. Verification delivery has been requested. Check your email; delivery may be pending.",
            "data": serializer.data,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([LoginIPThrottle, LoginEmailThrottle])
def login(request):

    serializer = LoginSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                "status": False,
                "message": "Invalid data",
                "errors": serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = authenticate(
        request,
        email=serializer.validated_data['email'],
        password=serializer.validated_data['password'],
    )

    if user is None:
        return Response(
            {
                "status": False,
                "message": "Invalid credentials",
            },
            status=status.HTTP_401_UNAUTHORIZED,
        )

    if not user.email_verified:
        return Response(
            {"status": False, "message": "Please verify your email address before logging in."},
            status=status.HTTP_403_FORBIDDEN,
        )

    refresh = RefreshToken.for_user(user)

    return authentication_response(user, refresh.access_token, refresh, 'Login successful')




@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([EmailVerifyIPThrottle, EmailVerifyRecipientThrottle])
def verify_email(request):
    serializer = VerifyEmailOTPSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"]
    otp = serializer.validated_data["otp"]
    invalid = {"error": "Invalid or expired verification code."}
    with transaction.atomic():
        user = User.objects.select_for_update().filter(
            email__iexact=email, email_verified=False, is_active=True,
        ).first()
        record = EmailOTP.objects.select_for_update().filter(user=user).first() if user else None
        if (
            record is None or record.email != user.email or not delivery_ready(record)
            or record.expires_at <= timezone.now()
            or record.attempts >= EMAIL_OTP_MAX_ATTEMPTS
        ):
            return Response(invalid, status=status.HTTP_400_BAD_REQUEST)
        record.attempts += 1
        record.save(update_fields=["attempts"])
        if not hmac.compare_digest(record.code_hash, hash_email_otp(user.pk, user.email, otp)):
            return Response(invalid, status=status.HTTP_400_BAD_REQUEST)
        user.email_verified = True
        user.save(update_fields=["email_verified"])
        record.delete()
    return Response({"status": True, "message": "Email verified. You can now log in."})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([EmailResendIPThrottle, EmailSendThrottle, EmailCooldownThrottle])
def resend_verification_email(request):
    serializer = ResendVerificationSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data['email']
    user = User.objects.filter(email__iexact=email, email_verified=False, is_active=True).first()
    if user:
        try:
            send_verification_email(user)
        except (SMTPException, OSError, RuntimeError, ImproperlyConfigured) as exc:
            logger.error('Verification email resend failed: %s (SMTP status: %s)', type(exc).__name__, getattr(exc, 'smtp_code', None))
            return Response(
                {"status": False, "message": "Unable to send verification email. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    return Response({"status": True, "message": "If eligible, verification delivery has been requested."})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PhoneSendIPThrottle, PhoneSendThrottle, PhoneCooldownThrottle])
def send_otp(request):
    serializer = SendOTPSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    phone = serializer.validated_data["phone"]

    try:
        with transaction.atomic():
            recipient = lock_phone_recipient(phone)
            record = PhoneOTP.objects.select_for_update().filter(phone=phone).first()
            now = timezone.now()
            cooldown_cutoff = now + timedelta(minutes=OTP_EXPIRY_MINUTES) - timedelta(seconds=60)
            if (
                (recipient.last_sent_at and recipient.last_sent_at > now - timedelta(seconds=60))
                or (record and record.expires_at > cooldown_cutoff)
            ):
                return Response(
                    {"status": False, "message": "Please wait 60 seconds before requesting another code."},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                    headers={'Retry-After': '60'},
                )
            if settings.PHONE_OTP_BACKEND == 'twilio_verify':
                AccountChallenge.objects.filter(channel='phone', target=phone).delete()
                job = enqueue('twilio_start', {'target': phone, 'service_sid': settings.TWILIO_VERIFY_SERVICE_SID}, now + timedelta(minutes=OTP_EXPIRY_MINUTES))
                PhoneOTP.objects.update_or_create(phone=phone, defaults={
                    'backend': 'twilio_verify', 'code_hash': '',
                    'version': uuid.uuid4(), 'verification_sid': '', 'delivery': job,
                    'service_sid': settings.TWILIO_VERIFY_SERVICE_SID,
                    'attempts': 0,
                    'expires_at': timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES),
                })
            else:
                AccountChallenge.objects.filter(channel='phone', target=phone).delete()
                otp = generate_otp(phone)
                record = PhoneOTP.objects.get(phone=phone)
                job = enqueue('sms', {'target': phone, 'message': f'Your verification code is {otp}. It expires in 5 minutes.'}, record.expires_at)
                record.delivery = job
                record.save(update_fields=['delivery'])
            recipient.last_sent_at = timezone.now()
            recipient.save(update_fields=['last_sent_at'])
    except VerifyRateLimited:
        return Response(
            {'status': False, 'message': 'Please wait before requesting another code.'},
            status=status.HTTP_429_TOO_MANY_REQUESTS, headers={'Retry-After': '60'},
        )
    except (SMSDeliveryError, ImproperlyConfigured) as exc:
        logger.error('Phone OTP send failed: %s; phone backend=%s', type(exc).__name__, settings.PHONE_OTP_BACKEND)
        return Response(
            {"status": False, "message": "Unable to send verification SMS. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    job.refresh_from_db()
    return Response(
        {
            "status": True,
            "message": "OTP delivery requested; check your phone. Delivery may be pending.",
            "delivery_status": job.state,
        },
        status=status.HTTP_200_OK if job.state == 'sent' else status.HTTP_202_ACCEPTED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PhoneVerifyIPThrottle, PhoneVerifyRecipientThrottle])
def verify_otp(request):
    serializer = VerifyOTPSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    phone = serializer.validated_data["phone"]
    otp = serializer.validated_data["otp"]

    try:
        prepare_phone_check(phone, otp)
    except VerifyRateLimited:
        return Response({'error': 'Please wait before checking another code.'}, status=429, headers={'Retry-After': '60'})
    except (SMSDeliveryError, ImproperlyConfigured, OSError):
        return Response({'error': 'Unable to check verification code. Please request a new code if this persists.'}, status=503)

    with transaction.atomic():
        lock_phone_recipient(phone)
        user = User.objects.select_for_update().filter(phone=phone).first()
        record = (
            PhoneOTP.objects
            .select_for_update()
            .filter(phone=phone)
            .first()
        )

        if (
            record is None
            or record.expires_at <= timezone.now()
            or (record.backend != 'twilio_verify' and record.attempts >= MAX_ATTEMPTS)
            or record.backend != settings.PHONE_OTP_BACKEND
            or not delivery_ready(record)
        ):
            return Response(
                {"error": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if record.backend == 'twilio_verify':
            if not record.verification_sid or record.service_sid != settings.TWILIO_VERIFY_SERVICE_SID:
                return Response({'error': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)
            approved = approved_receipt('phone', record, otp)
        else:
            approved = hmac.compare_digest(record.code_hash, hash_otp(phone, otp))
            record.attempts += 1
            record.save(update_fields=['attempts'])
        if not approved:
            return Response(
                {"error": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user is None:
            user = User.objects.create_user(
                phone=phone,
                role=User.Role.CUSTOMER,
                phone_verified=True,
            )
        else:
            if not user.is_active:
                return Response(
                    {"error": "Account unavailable."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if not user.phone_verified:
                user.phone_verified = True
                user.save(update_fields=["phone_verified"])

        record.delete()

        refresh = RefreshToken.for_user(user)
        response = authentication_response(user, refresh.access_token, refresh, 'Authentication successful.')

    return response
