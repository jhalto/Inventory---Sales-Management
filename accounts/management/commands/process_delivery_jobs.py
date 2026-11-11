import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from accounts.outbox import process_pending


class Command(BaseCommand):
    help = 'Dispatch committed delivery jobs and recover expired leases; no OTPs are printed except configured development console SMS.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true')
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--poll-seconds', type=float, default=2)

    def handle(self, *args, **options):
        if options['limit'] < 1 or options['poll_seconds'] < 0.1:
            raise CommandError('Use a positive limit and poll interval of at least 0.1 seconds.')
        while True:
            close_old_connections()
            count = process_pending(options['limit'])
            if not options['watch']:
                self.stdout.write(f'Considered {count} delivery jobs.')
                return
            time.sleep(options['poll_seconds'])
