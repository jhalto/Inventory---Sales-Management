from django.core.management.base import BaseCommand

from accounts.delivery_limits import database_now
from accounts.models import ThrottleBucket


class Command(BaseCommand):
    help = 'Remove expired PostgreSQL throttle buckets without resetting live limits.'

    def handle(self, *args, **options):
        count, _ = ThrottleBucket.objects.filter(expires_at__lte=database_now()).delete()
        self.stdout.write(f'Deleted {count} expired throttle buckets.')
