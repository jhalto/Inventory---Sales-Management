from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Clean expired authentication artifacts and safely prune inactive recipient rows.'

    def handle(self, *args, **options):
        for command in ('clearsessions', 'cleanup_account_artifacts', 'cleanup_google_challenges',
                        'cleanup_account_challenges', 'flushexpiredtokens', 'cleanup_throttle_buckets', 'cleanup_delivery_state'):
            call_command(command, stdout=self.stdout, stderr=self.stderr, verbosity=options['verbosity'])
