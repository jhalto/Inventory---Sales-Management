from collections.abc import Mapping

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .serializers import NormalizedEmailField, normalize_phone


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            raise serializers.ValidationError({'non_field_errors': ['Expected a JSON object.']})
        errors = {key: ['Must be a string.'] for key in self.fields if key in data and not isinstance(data[key], str)}
        if errors:
            raise serializers.ValidationError(errors)
        return super().to_internal_value(data)


class ChannelSerializer(StrictSerializer):
    channel = serializers.ChoiceField(choices=['email', 'phone'])


class ContactSerializer(StrictSerializer):
    email = NormalizedEmailField(required=False)
    phone = serializers.CharField(required=False)

    def validate_phone(self, value):
        return normalize_phone(value)


class ResetRequestSerializer(ContactSerializer, ChannelSerializer):
    def validate(self, attrs):
        if not attrs.get(attrs['channel']):
            raise serializers.ValidationError({attrs['channel']: ['This field is required.']})
        return attrs


class CodeSerializer(StrictSerializer):
    challenge_id = serializers.UUIDField()
    otp = serializers.RegexField(r'^[0-9]{6}$')


class StepUpSerializer(StrictSerializer):
    current_password = serializers.CharField(required=False, trim_whitespace=False, max_length=4096)
    reauth_challenge_id = serializers.UUIDField(required=False)
    reauth_otp = serializers.RegexField(r'^[0-9]{6}$', required=False)

    def validate(self, attrs):
        password = 'current_password' in attrs
        otp = 'reauth_challenge_id' in attrs and 'reauth_otp' in attrs
        if password == otp or (password and ('reauth_challenge_id' in attrs or 'reauth_otp' in attrs)):
            raise serializers.ValidationError('Provide current_password or both reauth_challenge_id and reauth_otp.')
        return attrs


class ContactConfirmSerializer(StepUpSerializer, CodeSerializer):
    pass


class NewPasswordSerializer(StrictSerializer):
    new_password = serializers.CharField(trim_whitespace=False, max_length=4096)


class PasswordChangeSerializer(StepUpSerializer, NewPasswordSerializer):
    pass


class ResetConfirmSerializer(CodeSerializer, NewPasswordSerializer):
    pass


def validate_new_password(value, user):
    if not value.strip():
        raise serializers.ValidationError({'new_password': ['Password cannot contain only whitespace.']})
    try:
        validate_password(value, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({'new_password': exc.messages})
