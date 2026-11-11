from datetime import timedelta

from django.db import connection
from rest_framework.exceptions import Throttled

from .models import AccountChallengeRecipient
from .otp_service import lock_phone_recipient
from .recipient_locks import database_now, lock_recipient


def reserve_delivery(channel, target):
    """Reserve intent in the challenge transaction; lock phones first, send later."""
    if not connection.in_atomic_block:
        raise RuntimeError('Delivery reservation requires an enclosing transaction.')
    phone_recipient = lock_phone_recipient(target) if channel == 'phone' else None
    key = f'{channel}:{target}'
    lock_recipient('delivery', key)
    recipient, _ = AccountChallengeRecipient.objects.select_for_update().get_or_create(key=key)
    now = database_now()
    last_sent = [value for value in (recipient.last_sent_at, phone_recipient.last_sent_at if phone_recipient else None) if value]
    if last_sent and max(last_sent) > now - timedelta(seconds=60):
        raise Throttled(wait=60, detail='Please wait before requesting another code.')
    if recipient.window_start is None or recipient.window_start <= now - timedelta(hours=1):
        recipient.window_start, recipient.sent_count = now, 0
    if recipient.sent_count >= 10:
        raise Throttled(wait=3600, detail='Too many codes for this contact.')
    recipient.last_sent_at = now
    recipient.last_used_at = now
    recipient.sent_count += 1
    recipient.save(update_fields=['last_sent_at', 'last_used_at', 'sent_count', 'window_start'])
    if phone_recipient:
        phone_recipient.last_sent_at = now
        phone_recipient.save(update_fields=['last_sent_at'])
