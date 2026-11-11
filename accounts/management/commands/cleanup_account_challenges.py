from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import AccountChallenge


class Command(BaseCommand):
    help = 'Delete expired self-service challenges; retain recipient cooldown/limit rows.'

    def handle(self, *args, **options):
        count, _ = AccountChallenge.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(f'Deleted {count} expired account challenges.')
