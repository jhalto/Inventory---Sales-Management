import base64
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .sms_service import SMSDeliveryError

logger = logging.getLogger(__name__)


class VerifyRateLimited(SMSDeliveryError):
    pass


def verify_request(resource, payload, checking=False):
    account = settings.TWILIO_ACCOUNT_SID
    service = settings.TWILIO_VERIFY_SERVICE_SID
    token = settings.TWILIO_AUTH_TOKEN
    if not re.fullmatch(r'AC[0-9a-fA-F]{32}', account) or not re.fullmatch(r'VA[0-9a-fA-F]{32}', service) or not token:
        logger.error('Twilio Verify requires a valid account SID, Verify Service SID, and Auth Token. Check environment settings and restart Django.')
        raise ImproperlyConfigured('Configure valid Twilio account credentials and a Verify Service SID.')
    authorization = base64.b64encode(f'{account}:{token}'.encode()).decode()
    request = Request(
        f'https://verify.twilio.com/v2/Services/{service}/{resource}',
        data=urlencode(payload).encode(),
        headers={'Authorization': f'Basic {authorization}', 'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=settings.SMS_TIMEOUT) as response:
            result = json.load(response)
    except HTTPError as exc:
        status = exc.code
        try:
            error = json.loads(exc.read(65536))
            code = error.get('code') if isinstance(error, dict) else None
            code = code if type(code) is int else None
        except (ValueError, OSError):
            code = None
        finally:
            exc.close()
        if checking and status == 404:
            return {'status': 'expired'}
        # Never log provider messages, response bodies, phone numbers, or codes.
        logger.warning('Twilio Verify request rejected: HTTP %s, provider error code %s, operation %s', status, code, resource)
        if status == 429:
            raise VerifyRateLimited('Twilio Verify rate limit reached.') from None
        raise SMSDeliveryError('Twilio Verify rejected the request.') from None
    except (URLError, OSError) as exc:
        logger.error('Twilio Verify connection failed: %s', type(getattr(exc, 'reason', exc)).__name__)
        raise SMSDeliveryError('Twilio Verify is unavailable.') from None
    except ValueError:
        logger.error('Twilio Verify returned invalid JSON.')
        raise SMSDeliveryError('Twilio Verify is unavailable.') from None
    if not isinstance(result, dict):
        logger.error('Twilio Verify returned an invalid response type.')
        raise SMSDeliveryError('Invalid Twilio Verify response.')
    return result


def start_verification(phone):
    result = verify_request('Verifications', {'To': phone, 'Channel': 'sms'})
    sid = result.get('sid', '')
    if (
        not isinstance(sid, str) or not re.fullmatch(r'VE[0-9a-fA-F]{32}', sid)
        or result.get('status') != 'pending'
        or result.get('to') != phone
        or result.get('service_sid') != settings.TWILIO_VERIFY_SERVICE_SID
    ):
        logger.error('Twilio Verify returned an unexpected verification reference, status, service, or recipient.')
        raise SMSDeliveryError('Twilio Verify did not accept verification.')
    return sid


def check_verification(phone, otp, verification_sid):
    result = verify_request('VerificationCheck', {'VerificationSid': verification_sid, 'Code': otp}, checking=True)
    return (
        result.get('status') == 'approved'
        and result.get('sid') == verification_sid
        and result.get('to') == phone
        and result.get('service_sid') == settings.TWILIO_VERIFY_SERVICE_SID
    )
