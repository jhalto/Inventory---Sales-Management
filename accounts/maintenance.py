from datetime import timedelta

from django.db import transaction
from django.db.models import Q

from .models import AccountChallenge, AccountChallengeRecipient, EmailOTP, PhoneOTP, PhoneOTPRecipient
from .recipient_locks import lock_recipient


def delete_expired(model, now, batch_size):
    total = 0
    while True:
        with transaction.atomic():
            ids = list(model.objects.select_for_update(skip_locked=True).filter(
                expires_at__lte=now,
            ).order_by('pk').values_list('pk', flat=True)[:batch_size])
            if not ids:
                return total
            count, _ = model.objects.filter(pk__in=ids).delete()
            total += count


def prune_recipients(now, retention_days, batch_size):
    cutoff = now - timedelta(days=retention_days)
    total = 0
    for model, field in ((PhoneOTPRecipient, 'phone'), (AccountChallengeRecipient, 'key')):
        last_pk = 0
        candidates = model.objects.filter(last_used_at__lt=cutoff).filter(
            Q(last_sent_at__isnull=True) | Q(last_sent_at__lt=cutoff),
        )
        while True:
            batch = list(candidates.filter(pk__gt=last_pk).order_by('pk').values_list('pk', field)[:batch_size])
            if not batch:
                break
            last_pk = batch[-1][0]
            for pk, value in batch:
                with transaction.atomic():
                    if field == 'phone':
                        channel, target = 'phone', value
                    else:
                        channel, separator, target = value.partition(':')
                        if not separator or not target or channel not in ('email', 'phone'):
                            continue
                    if channel == 'phone' and not lock_recipient('phone', target, wait=False):
                        continue
                    if field == 'key' and not lock_recipient('delivery', value, wait=False):
                        continue
                    row = candidates.select_for_update(skip_locked=True).filter(pk=pk).first()
                    if row is None:
                        continue
                    if field == 'key' and row.window_start and row.window_start >= cutoff:
                        continue
                    live = AccountChallenge.objects.filter(channel=channel, target=target, expires_at__gt=now).exists()
                    if channel == 'phone':
                        live = live or PhoneOTP.objects.filter(phone=target, expires_at__gt=now).exists()
                    elif channel == 'email':
                        live = live or EmailOTP.objects.filter(email=target, expires_at__gt=now).exists()
                    else:
                        continue  # Unknown namespace requires explicit review.
                    if not live:
                        row.delete()
                        total += 1
    return total
