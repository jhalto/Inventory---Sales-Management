import phonenumbers
from django.core.exceptions import ValidationError


def normalize_email(email):
    if not email:
        return None
    return email.strip().lower() or None


def normalize_phone(phone):
    if phone is None or isinstance(phone, str) and not phone.strip():
        return None
    if not isinstance(phone, str):
        raise ValidationError('Enter a valid Bangladeshi phone number.')
    try:
        number = phonenumbers.parse(phone.strip(), 'BD')
        if number.country_code != 880 or not phonenumbers.is_valid_number(number):
            raise ValueError
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    except (phonenumbers.NumberParseException, ValueError):
        raise ValidationError('Enter a valid Bangladeshi phone number.') from None
