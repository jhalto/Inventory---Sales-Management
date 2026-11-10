import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string


class SMSDeliveryError(Exception):
    pass


def console_send_sms(phone, message):
    if not settings.DEBUG:
        raise ImproperlyConfigured('Console SMS is available only with DEBUG=True.')
    print(f'[Development SMS] To: {phone}\n{message}', flush=True)


def twilio_send_sms(phone, message):
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    sender = settings.TWILIO_FROM_NUMBER
    service_sid = settings.TWILIO_MESSAGING_SERVICE_SID
    if not account_sid or not auth_token or not (sender or service_sid):
        raise ImproperlyConfigured('Configure Twilio credentials and a sender or messaging service.')

    payload = {'To': phone, 'Body': message}
    payload.update({'MessagingServiceSid': service_sid} if service_sid else {'From': sender})
    authorization = base64.b64encode(f'{account_sid}:{auth_token}'.encode()).decode()
    request = Request(
        f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json',
        data=urlencode(payload).encode(),
        headers={
            'Authorization': f'Basic {authorization}',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        method='POST',
    )
    try:
        with urlopen(request, timeout=settings.SMS_TIMEOUT) as response:
            result = json.load(response)
        if not isinstance(result, dict) or not result.get('sid') or result.get('status') in ('failed', 'undelivered'):
            raise SMSDeliveryError('SMS provider did not accept the message.')
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise SMSDeliveryError('SMS provider is unavailable or rejected the message.') from None


def send_sms(phone, message):
    backend = import_string(settings.SMS_BACKEND)
    backend(phone, message)
