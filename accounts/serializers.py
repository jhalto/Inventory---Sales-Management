from collections.abc import Mapping

from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from accounts.models import User
from accounts.identities import normalize_email, normalize_phone as normalize_identity_phone


class NormalizedEmailField(serializers.EmailField):
    def to_internal_value(self, data):
        return normalize_email(super().to_internal_value(data))


class LoginSerializer(serializers.Serializer):
    email = NormalizedEmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            raise serializers.ValidationError({'non_field_errors': ['Expected an object with email and password.']})
        errors = {
            field: ['Must be a string.']
            for field in ('email', 'password')
            if field in data and data[field] is not None and not isinstance(data[field], str)
        }
        if errors:
            raise serializers.ValidationError(errors)
        return super().to_internal_value(data)

    def validate_email(self, value):
        return normalize_email(value)


class UserSerializers(serializers.ModelSerializer):

    email = NormalizedEmailField(required=True)

    password = serializers.CharField(
        write_only=True,
        required=True,
        trim_whitespace=False,
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "password",
        ]
        read_only_fields = ["id"]

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            email_field = self.fields["email"]
            try:
                email = email_field.run_validation(email_field.get_value(data))
            except serializers.ValidationError as exc:
                raise serializers.ValidationError({"email": exc.detail})

            if User.objects.filter(email__iexact=normalize_email(email)).exists():
                raise serializers.ValidationError(
                    {"email": ["This email is already registered."]}
                )

        return super().to_internal_value(data)

    def validate_email(self, value):
        return normalize_email(value)

    def validate_password(self, value):
        if not value.strip():
            raise serializers.ValidationError('Password cannot contain only whitespace.')
        return value

    def validate(self, attrs):
        user = User(email=attrs['email'])
        try:
            validate_password(attrs['password'], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': exc.messages})
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(
            **validated_data
        )



def normalize_phone(value):
    try:
        phone = normalize_identity_phone(value)
        if phone is None:
            raise DjangoValidationError('Enter a valid Bangladeshi phone number.')
        return phone
    except DjangoValidationError as exc:
        raise serializers.ValidationError(exc.messages)


class ResendVerificationSerializer(serializers.Serializer):
    email = NormalizedEmailField()

    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            raise serializers.ValidationError({'non_field_errors': ['Expected an object with an email address.']})
        if 'email' in data and data['email'] is not None and not isinstance(data['email'], str):
            raise serializers.ValidationError({'email': ['Must be a string.']})
        return super().to_internal_value(data)


class VerifyEmailOTPSerializer(serializers.Serializer):
    email = NormalizedEmailField()
    otp = serializers.RegexField(r"^[0-9]{6}$")


class SendOTPSerializer(serializers.Serializer):
    phone = serializers.CharField()

    def validate_phone(self, value):
        return normalize_phone(value)


class VerifyOTPSerializer(serializers.Serializer):
    phone = serializers.CharField()
    otp = serializers.RegexField(r"^[0-9]{6}$")

    def validate_phone(self, value):
        return normalize_phone(value)
