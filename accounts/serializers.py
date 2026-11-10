from collections.abc import Mapping

import phonenumbers
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password

from accounts.models import User


class UserSerializers(serializers.ModelSerializer):

    email = serializers.EmailField(required=True)

    password = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password],
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

            if User.objects.filter(email__iexact=email.strip().lower()).exists():
                raise serializers.ValidationError(
                    {"email": ["This email is already registered."]}
                )

        return super().to_internal_value(data)

    def validate_email(self, value):
        return value.strip().lower()

    def create(self, validated_data):
        return User.objects.create_user(
            **validated_data
        )



def normalize_phone(value):
    try:
        number = phonenumbers.parse(value, "BD")

        if not phonenumbers.is_valid_number(number):
            raise ValueError()

        if number.country_code != 880:
            raise ValueError()

        return phonenumbers.format_number(
            number,
            phonenumbers.PhoneNumberFormat.E164
        )

    except (phonenumbers.NumberParseException, ValueError):
        raise serializers.ValidationError(
            "Enter a valid Bangladeshi phone number."
        )


class VerifyEmailOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()
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
