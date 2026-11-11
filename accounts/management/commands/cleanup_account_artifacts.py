from django.core.management.base import BaseCommand, CommandError

from accounts.maintenance import delete_expired, prune_recipients
from accounts.models import EmailOTP, PhoneOTP
from accounts.recipient_locks import database_now


class Command(BaseCommand):
    help = 'Remove expired legacy OTPs and safely prune inactive recipient rows (30 days by default).'

    def add_arguments(self, parser):
        parser.add_argument('--recipient-retention-days', type=int, default=30)
        parser.add_argument('--batch-size', type=int, default=500)

    def handle(self, *args, **options):
        if options['recipient_retention_days'] < 1 or options['batch_size'] < 1:
            raise CommandError('Retention must be at least one day and batch size must be positive.')
        now = database_now()
        email_count = delete_expired(EmailOTP, now, options['batch_size'])
        phone_count = delete_expired(PhoneOTP, now, options['batch_size'])
        recipient_count = prune_recipients(now, options['recipient_retention_days'], options['batch_size'])
        self.stdout.write(f'Deleted {email_count} expired email OTPs, {phone_count} expired phone OTPs and {recipient_count} inactive recipient rows.')
