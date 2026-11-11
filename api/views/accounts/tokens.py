from collections.abc import Mapping

from django.db import transaction
from rest_framework import serializers
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from accounts.throttles import TokenIPThrottle
from .responses import authentication_response


class TokenEndpointAuthentication(JWTAuthentication):
    def authenticate(self, request):
        # Only the refresh token in the body authenticates this operation.
        return None


class RefreshInputSerializer(serializers.Serializer):
    refresh = serializers.CharField(trim_whitespace=False, max_length=4096)

    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            raise serializers.ValidationError({'non_field_errors': ['Expected an object with a refresh token.']})
        if 'refresh' in data and not isinstance(data['refresh'], str):
            raise serializers.ValidationError({'refresh': ['Must be a string.']})
        return super().to_internal_value(data)


def exchange_refresh(request, *, logout=False):
    serializer = RefreshInputSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    raw_token = serializer.validated_data['refresh']
    try:
        token = RefreshToken(raw_token)
        user_id = token.get(api_settings.USER_ID_CLAIM)
        if not isinstance(user_id, (str, int)) or isinstance(user_id, bool):
            raise InvalidToken('Invalid account identifier.')
        with transaction.atomic():
            # A stable row serializes refresh/logout even for pre-blacklist tokens.
            user = User.objects.select_for_update().get(**{api_settings.USER_ID_FIELD: user_id})
            token = RefreshToken(raw_token)  # Recheck blacklist after waiting.
            # Includes active-account and password-change revocation checks.
            JWTAuthentication().get_user(token)
            if logout:
                token.blacklist()
                return Response(status=204)
            result = TokenRefreshSerializer().validate({'refresh': raw_token})
            return authentication_response(user, result['access'], result['refresh'], 'Tokens refreshed.')
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        raise InvalidToken('Account unavailable.')
    except TokenError as exc:
        raise InvalidToken(str(exc)) from exc


@api_view(['POST'])
@authentication_classes([TokenEndpointAuthentication])
@permission_classes([AllowAny])
@throttle_classes([TokenIPThrottle])
def token_refresh(request):
    return exchange_refresh(request)


@api_view(['POST'])
@authentication_classes([TokenEndpointAuthentication])
@permission_classes([AllowAny])
@throttle_classes([TokenIPThrottle])
def logout(request):
    return exchange_refresh(request, logout=True)
