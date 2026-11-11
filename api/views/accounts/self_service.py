from functools import wraps
import logging
from smtplib import SMTPException
import uuid

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from accounts.models import AccountChallenge, GoogleIdentity, User
from accounts.self_service import (
    check_available, check_code, check_step_up, current_verified_target,
    issue_challenge, locked_user,
)
from accounts.self_service_serializers import (
    ChannelSerializer, ContactConfirmSerializer, ContactSerializer,
    PasswordChangeSerializer, ResetConfirmSerializer, ResetRequestSerializer,
    StepUpSerializer, validate_new_password,
)
from accounts.sms_service import SMSDeliveryError
from accounts.throttles import ManagementIPThrottle, ManagementUserThrottle
from accounts.twilio_verify import VerifyRateLimited
from .tokens import TokenEndpointAuthentication
from accounts.remote_checks import prepare_request_proofs

logger = logging.getLogger(__name__)
DELIVERY_ERRORS = (SMSDeliveryError, SMTPException, OSError, RuntimeError, ImproperlyConfigured)


def delivery_errors(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except VerifyRateLimited:
            return Response({'detail': 'Please wait before requesting another code.'}, status=429, headers={'Retry-After': '60'})
        except DELIVERY_ERRORS as exc:
            logger.error('Account challenge delivery/check failed: %s', type(exc).__name__)
            return Response({'detail': 'Verification service unavailable. Please try again later.'}, status=503)
    return wrapped


def validated(serializer_class, request):
    serializer = serializer_class(data=request.data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def recheck_auth(request):
    # A password might have changed while this request waited for the user lock.
    if request.auth is not None:
        JWTAuthentication().get_user(request.auth)


def invalid_proof():
    # Returning (rather than raising) commits failed-code attempt increments.
    return Response({'detail': 'Invalid or expired proof.'}, status=400)


@api_view(['POST'])
@authentication_classes([TokenEndpointAuthentication])
@permission_classes([AllowAny])
@throttle_classes([ManagementIPThrottle])
def password_reset_request(request):
    data = validated(ResetRequestSerializer, request)
    channel, target = data['channel'], data[data['channel']]
    challenge_id = uuid.uuid4()
    user = User.objects.filter(**{channel: target, channel + '_verified': True}, is_active=True).first()
    if user:
        try:
            with locked_user(user.pk, [target] if channel == 'phone' else []) as current:
                if current_verified_target(current, channel) == target:
                    challenge_id = issue_challenge(current, 'reset', channel, target).pk
        except (Throttled, ValidationError, PermissionDenied, *DELIVERY_ERRORS) as exc:
            # Keep eligibility, recipient cooldown and delivery failures private.
            logger.warning('Password recovery request not delivered: %s', type(exc).__name__)
    return Response({'challenge_id': str(challenge_id), 'message': 'If eligible, recovery delivery has been requested.'})


@api_view(['POST'])
@authentication_classes([TokenEndpointAuthentication])
@permission_classes([AllowAny])
@throttle_classes([ManagementIPThrottle])
@delivery_errors
def password_reset_confirm(request):
    data = validated(ResetConfirmSerializer, request)
    prepare_request_proofs(data, purpose='reset')
    record = AccountChallenge.objects.filter(pk=data['challenge_id'], purpose='reset').first()
    if record is None:
        return invalid_proof()
    with locked_user(record.user_id, [record.target] if record.channel == 'phone' else []) as user:
        validate_new_password(data['new_password'], user)
        if not check_code(user, record.pk, 'reset', data['otp']):
            return invalid_proof()
        user.set_password(data['new_password'])
        user.save(update_fields=['password'])
        AccountChallenge.objects.filter(user=user).delete()
    return Response({'message': 'Password reset. Sign in again.'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ManagementIPThrottle, ManagementUserThrottle])
@delivery_errors
def reauth_request(request):
    data = validated(ChannelSerializer, request)
    with locked_user(request.user.pk) as user:
        recheck_auth(request)
        target = current_verified_target(user, data['channel'])
        record = issue_challenge(user, 'reauth', data['channel'], target)
    return Response({'challenge_id': str(record.pk), 'message': 'Reauthentication delivery requested; delivery may be pending.'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ManagementIPThrottle, ManagementUserThrottle])
@delivery_errors
def password_change(request):
    data = validated(PasswordChangeSerializer, request)
    prepare_request_proofs(data, user_id=request.user.pk)
    with locked_user(request.user.pk) as user:
        recheck_auth(request)
        validate_new_password(data['new_password'], user)
        if not check_step_up(user, data):
            return invalid_proof()
        user.set_password(data['new_password'])
        user.save(update_fields=['password'])
        AccountChallenge.objects.filter(user=user).delete()
    return Response({'message': 'Password changed. Sign in again.'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ManagementIPThrottle, ManagementUserThrottle])
@delivery_errors
def contact_request(request, channel):
    data = validated(ContactSerializer, request)
    if channel not in data:
        raise ValidationError({channel: ['This field is required.']})
    target = data[channel]
    with locked_user(request.user.pk, [target] if channel == 'phone' else []) as user:
        recheck_auth(request)
        check_available(user, channel, target)
        record = issue_challenge(user, channel, channel, target)
    return Response({'challenge_id': str(record.pk), 'message': 'Verification delivery requested for the new contact; delivery may be pending.'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ManagementIPThrottle, ManagementUserThrottle])
@delivery_errors
def contact_confirm(request, channel):
    data = validated(ContactConfirmSerializer, request)
    prepare_request_proofs(data, user_id=request.user.pk, purpose=channel)
    record = AccountChallenge.objects.filter(pk=data['challenge_id'], user=request.user, purpose=channel).first()
    if record is None:
        return invalid_proof()
    try:
        with locked_user(request.user.pk, [record.target] if channel == 'phone' else []) as user:
            recheck_auth(request)
            if not check_code(user, record.pk, channel, data['otp']):
                return invalid_proof()
            if not check_step_up(user, data):
                return invalid_proof()
            check_available(user, channel, record.target)
            setattr(user, channel, record.target)
            user.save(update_fields=[channel])
            # User.save resets changed-contact flags; this handler supplies proof.
            setattr(user, channel + '_verified', True)
            user.save(update_fields=[channel + '_verified'])
            AccountChallenge.objects.filter(user=user).delete()
    except IntegrityError:
        return Response({'detail': 'This identity is already registered to another account.'}, status=409)
    return Response({'message': 'Contact verified and updated.', channel: record.target})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ManagementIPThrottle, ManagementUserThrottle])
@delivery_errors
def google_unlink(request):
    data = validated(StepUpSerializer, request)
    prepare_request_proofs(data, user_id=request.user.pk)
    with locked_user(request.user.pk) as user:
        recheck_auth(request)
        if not check_step_up(user, data):
            return invalid_proof()
        if not ((user.email_verified and user.email and user.has_usable_password())
                or (user.phone_verified and user.phone)):
            return Response({'detail': 'Set a password for a verified email or verify a phone before unlinking Google.'}, status=400)
        GoogleIdentity.objects.filter(user=user).delete()
    return Response(status=204)
