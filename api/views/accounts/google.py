from django.conf import settings
from functools import wraps
from math import ceil
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET
from rest_framework import serializers, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, permission_classes, throttle_classes, authentication_classes, parser_classes
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.authentication import JWTAuthentication

from accounts.google_service import GoogleUnavailable, InvalidGoogleToken, verify_google_token, verify_google_mobile_token
from accounts.models import GoogleBrowserChallenge, GoogleIdentity, User
from accounts.google_challenges import issue_browser_challenge, browser_challenge_key
from accounts.throttles import GoogleSignInThrottle, GoogleChallengeIPThrottle, ThrottleUnavailable
from .responses import authentication_response


class GoogleCredentialField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail('invalid')
        return super().to_internal_value(data)


class GoogleCredentialSerializer(serializers.Serializer):
    credential = GoogleCredentialField(max_length=16384, trim_whitespace=False)


def challenge(request):
    nonce = issue_browser_challenge(request.session)
    return {'client_id': settings.GOOGLE_CLIENT_ID, 'nonce': nonce, 'csrf_token': get_token(request)}


def limit_challenge_get(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        throttle = GoogleChallengeIPThrottle()
        try:
            allowed = throttle.allow_request(request, None)
        except ThrottleUnavailable as exc:
            return JsonResponse({'error': str(exc.detail)}, status=503)
        if not allowed:
            response = JsonResponse({'error': 'Too many Google sign-in requests. Please try again later.'}, status=429)
            response['Retry-After'] = str(max(1, ceil(throttle.wait() or 1)))
            return response
        return view(request, *args, **kwargs)
    return wrapped


@require_GET
@never_cache
@limit_challenge_get
@ensure_csrf_cookie
def google_page(request):
    if not settings.GOOGLE_CLIENT_ID:
        return JsonResponse({'error': 'Google sign-in is not configured. Set GOOGLE_CLIENT_ID and restart Django.'}, status=503)
    response = render(request, 'accounts/google_sign_in.html', {'google_config': challenge(request)})
    response['Cross-Origin-Opener-Policy'] = 'same-origin-allow-popups'
    return response


@require_GET
@never_cache
@limit_challenge_get
@ensure_csrf_cookie
def google_config(request):
    if not settings.GOOGLE_CLIENT_ID:
        return JsonResponse({'error': 'Google sign-in is not configured.'}, status=503)
    return JsonResponse(challenge(request))


def process_google_login(request, linking=False, native=False):
    if native:
        # Native endpoints never use ambient browser authentication. Flutter Web
        # must retain the browser handshake instead of bypassing CSRF/nonce checks.
        if 'HTTP_ORIGIN' in request.META or request.COOKIES:
            return Response({'error': 'Use the browser Google sign-in flow for requests with origins or cookies.'}, status=403)
    else:
        SessionAuthentication().enforce_csrf(request)
    serializer = GoogleCredentialSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not native:
        nonce = request.session.get('google_sign_in_nonce')
        challenge_key = browser_challenge_key(request.session)
        if not challenge_key or not GoogleBrowserChallenge.objects.filter(
            **challenge_key, consumed_at__isnull=True, expires_at__gt=timezone.now(),
        ).exists():
            return Response({'error': 'Start a new Google sign-in session.'}, status=400)
    try:
        credential = serializer.validated_data['credential']
        identity = verify_google_mobile_token(credential) if native else verify_google_token(credential, nonce)
    except InvalidGoogleToken:
        return Response({'error': 'Invalid Google sign-in. Start again and try another account if needed.'}, status=401)
    except (GoogleUnavailable, ImproperlyConfigured):
        return Response({'error': 'Google sign-in is temporarily unavailable.'}, status=503)

    try:
        with transaction.atomic():
            if not native:
                # Re-check after acquiring the row lock. A competing exchange or
                # new challenge may have changed it during Google verification.
                browser_challenge = GoogleBrowserChallenge.objects.select_for_update().filter(
                    session_digest=challenge_key['session_digest'],
                ).first()
                if (
                    browser_challenge is None or browser_challenge.consumed_at is not None
                    or browser_challenge.expires_at <= timezone.now()
                    or browser_challenge.nonce_digest != challenge_key['nonce_digest']
                ):
                    return Response({'error': 'Google sign-in challenge expired or already used. Start again.'}, status=400)
            linked = GoogleIdentity.objects.select_related('user').filter(subject=identity['subject']).first()
            if linking:
                user = User.objects.select_for_update().get(pk=request.user.pk)
                if not user.is_active or not user.email_verified or user.email != identity['email']:
                    return Response({'error': 'Verify the matching account email before linking Google.'}, status=403)
                if linked and linked.user_id != user.pk:
                    return Response({'error': 'This Google account is already linked.'}, status=409)
                if GoogleIdentity.objects.filter(user=user).exclude(subject=identity['subject']).exists():
                    return Response({'error': 'This account already has a different Google login.'}, status=409)
                if not linked:
                    GoogleIdentity.objects.create(user=user, subject=identity['subject'])
            elif linked:
                user = User.objects.select_for_update().get(pk=linked.user_id)
            else:
                if User.objects.filter(email__iexact=identity['email']).exists():
                    return Response({'error': 'An account with this email exists. Sign in with your existing method, verify your email, then link Google.', 'code': 'google_link_required'}, status=409)
                user = User.objects.create_user(email=identity['email'], email_verified=True, role=User.Role.CUSTOMER)
                GoogleIdentity.objects.create(user=user, subject=identity['subject'])
            if not user.is_active:
                return Response({'error': 'Account unavailable.'}, status=403)
            if not native:
                browser_challenge.consumed_at = timezone.now()
                browser_challenge.save(update_fields=['consumed_at'])
            refresh = RefreshToken.for_user(user)
            response = authentication_response(
                user, refresh.access_token, refresh,
                'Google account linked.' if linking else 'Google sign-in successful.',
            )
    except User.DoesNotExist:
        return Response({'error': 'Account unavailable. Start a new sign-in session.', 'code': 'account_unavailable'}, status=403)
    except IntegrityError:
        return Response({'error': 'Account linking changed. Start a new Google sign-in session and try again.'}, status=409)

    if not native:
        request.session.pop('google_sign_in_nonce', None)
        request.session.cycle_key()
    return response


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([GoogleSignInThrottle])
@parser_classes([JSONParser])
def google_login(request):
    return process_google_login(request)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([GoogleSignInThrottle])
@parser_classes([JSONParser])
def google_link(request):
    return process_google_login(request, linking=True)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([GoogleSignInThrottle])
@parser_classes([JSONParser])
def google_mobile_login(request):
    return process_google_login(request, native=True)


@api_view(['POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
@throttle_classes([GoogleSignInThrottle])
@parser_classes([JSONParser])
def google_mobile_link(request):
    return process_google_login(request, linking=True, native=True)
