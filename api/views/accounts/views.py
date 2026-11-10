from rest_framework.response import Response
from accounts.serializers import UserSerializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from accounts.models import User
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.core.exceptions import ImproperlyConfigured
from smtplib import SMTPException
from rest_framework import serializers
from accounts.email_service import EMAIL_OTP_MAX_ATTEMPTS, hash_email_otp, send_verification_email
from accounts.models import EmailOTP
from accounts.serializers import VerifyEmailOTPSerializer


@api_view(["POST"])
@permission_classes([AllowAny])
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
    except (SMTPException, OSError, RuntimeError, ImproperlyConfigured):
        return Response(
            {"status": False, "message": "Unable to send verification email. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        {
            "status": True,
            "message": "Account created. Check your email for the verification code. It expires in 5 minutes.",
            "data": serializer.data,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):

    email = request.data.get("email")
    password = request.data.get("password")

    if not email or not password:
        return Response(
            {
                "status": False,
                "message": "Email and password are required",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = authenticate(
        request,
        email=email.strip().lower(),
        password=password,
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

    return Response(
        {
            "status": True,
            "message": "Login successful",
            "data": {
                "id": user.id,
                "email": user.email,
                "phone": user.phone,
                "role": user.role,
                "access_token": str(refresh.access_token),
                "refresh_token": str(refresh),
            },
        },
        status=status.HTTP_200_OK,
    )


import hmac
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import (
    api_view,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User, PhoneOTP
from accounts.serializers import SendOTPSerializer, VerifyOTPSerializer
from accounts.otp_service import (
    generate_otp,
    hash_otp,
    MAX_ATTEMPTS,
    OTP_EXPIRY_MINUTES,
)
from accounts.sms_service import SMSDeliveryError, send_sms


class OTPThrottle(AnonRateThrottle):
    scope = "phone_otp"
    rate = "10/hour"


class OTPVerifyThrottle(AnonRateThrottle):
    scope = "phone_otp_verify"
    rate = "30/hour"


class EmailVerificationThrottle(AnonRateThrottle):
    rate = "5/hour"


@api_view(["POST"])
@permission_classes([AllowAny])
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
            record is None or record.email != user.email
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
@throttle_classes([EmailVerificationThrottle])
def resend_verification_email(request):
    email = serializers.EmailField().run_validation(request.data.get("email"))
    user = User.objects.filter(email__iexact=email, email_verified=False, is_active=True).first()
    if user:
        try:
            send_verification_email(user)
        except (SMTPException, OSError, RuntimeError, ImproperlyConfigured):
            return Response(
                {"status": False, "message": "Unable to send verification email. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    return Response({"status": True, "message": "If eligible, a verification code has been emailed."})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([OTPThrottle])
def send_otp(request):
    serializer = SendOTPSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    phone = serializer.validated_data["phone"]

    try:
        with transaction.atomic():
            record = PhoneOTP.objects.select_for_update().filter(phone=phone).first()
            cooldown_cutoff = timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES) - timedelta(seconds=60)
            if record and record.expires_at > cooldown_cutoff:
                return Response(
                    {"status": False, "message": "Please wait 60 seconds before requesting another code."},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            otp = generate_otp(phone)
            send_sms(phone, f"Your verification code is {otp}. It expires in 5 minutes.")
    except (SMSDeliveryError, ImproperlyConfigured):
        return Response(
            {"status": False, "message": "Unable to send verification SMS. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        {
            "status": True,
            "message": "If eligible, an OTP has been sent.",
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([OTPVerifyThrottle])
def verify_otp(request):
    serializer = VerifyOTPSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    phone = serializer.validated_data["phone"]
    otp = serializer.validated_data["otp"]

    with transaction.atomic():
        record = (
            PhoneOTP.objects
            .select_for_update()
            .filter(phone=phone)
            .first()
        )

        if (
            record is None
            or record.expires_at <= timezone.now()
            or record.attempts >= MAX_ATTEMPTS
        ):
            return Response(
                {"error": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        record.attempts += 1
        record.save(update_fields=["attempts"])

        expected_hash = hash_otp(phone, otp)

        if not hmac.compare_digest(
            record.code_hash,
            expected_hash
        ):
            return Response(
                {"error": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(phone=phone).first()

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

    return Response(
        {
            "status": True,
            "message": "Authentication successful.",
            "user": {
                "id": user.id,
                "email": user.email,
                "phone": user.phone,
                "role": user.role,
            },
            "access_token": str(refresh.access_token),
            "refresh_token": str(refresh),
        },
        status=status.HTTP_200_OK,
    )
