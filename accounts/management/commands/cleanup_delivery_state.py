from datetime import timedelta

from django.core.management.base import BaseCommand

from accounts.delivery_limits import database_now
from accounts.models import DeliveryJob, VerificationReceipt


class Command(BaseCommand):
    help = 'Erase expired encrypted delivery payloads and prune delivery metadata older than one day.'

    def handle(self, *args, **options):
        now = database_now()
        DeliveryJob.objects.filter(expires_at__lte=now).update(payload='')
        DeliveryJob.objects.filter(expires_at__lte=now).exclude(state='sent').update(state='cancelled')
        VerificationReceipt.objects.filter(expires_at__lte=now).delete()
        count, _ = DeliveryJob.objects.filter(expires_at__lte=now - timedelta(days=1)).delete()
        self.stdout.write(f'Pruned {count} expired delivery records; expired payloads erased.')
