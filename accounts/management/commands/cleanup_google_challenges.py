from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import GoogleBrowserChallenge


class Command(BaseCommand):
    help = 'Remove expired Google browser sign-in challenges without touching active challenges.'

    def handle(self, *args, **options):
        count, _ = GoogleBrowserChallenge.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(f'Deleted {count} expired Google browser challenges.')
