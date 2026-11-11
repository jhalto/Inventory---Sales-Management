from django.db import models, router, transaction
from django.contrib.auth.models import AbstractUser
from .managers import CustomUserManager
from .identities import normalize_email, normalize_phone
from django.db.models.functions import Lower, Trim
from django.core.exceptions import ValidationError
import uuid
from django.utils import timezone
# Create your models here.


class _IdentityChangedDuringLock(Exception):
    pass


class User(AbstractUser):
    username = None
    _enforce_identity_bulk_policy = True

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        STAFF = "staff", "Staff"
        CUSTOMER = "customer", "Customer"

    email = models.EmailField(
        unique=True,
        null=True,
        blank=True,
    )

    phone = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
    )

    email_verified = models.BooleanField(default=False)
    phone_verified = models.BooleanField(default=False)

    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.CUSTOMER,
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    class Meta(AbstractUser.Meta):
        abstract = False
        base_manager_name = 'objects'
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(email__isnull=False) & ~models.Q(email=''))
                | (models.Q(phone__isnull=False) & ~models.Q(phone='')),
                name='accounts_user_identity_required',
            ),
            models.UniqueConstraint(Lower(Trim('email')), name='accounts_user_email_ci_unique'),
            models.CheckConstraint(
                condition=models.Q(email__isnull=True) | (
                    models.Q(email=Lower('email')) & ~models.Q(email='') & ~models.Q(email__regex=r'\s')
                ),
                name='accounts_user_email_canonical',
            ),
            models.CheckConstraint(
                condition=models.Q(phone__isnull=True) | models.Q(phone__regex=r'^\+880[0-9]{6,10}$'),
                name='accounts_user_phone_canonical',
            ),
        ]

    def clean(self):
        super().clean()
        self.email = normalize_email(self.email)
        self.phone = normalize_phone(self.phone)
        self.validate_identity(self.email, self.phone)

    @staticmethod
    def validate_identity(email, phone):
        if not email and not phone:
            raise ValidationError('An email address or phone number is required.')

    def save(self, *args, **kwargs):
        email = normalize_email(self.email)
        if email != self.email and kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'email'}
        self.email = email
        phone = normalize_phone(self.phone)
        if phone != self.phone and kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'phone'}
        self.phone = phone
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        kwargs['using'] = using
        fields = kwargs.get('update_fields')
        if fields is not None:
            fields = set(fields)
            if not fields:
                return
        from .otp_service import lock_phone_recipient

        while True:
            previous = None
            if self.pk is not None:
                previous = type(self)._base_manager.using(using).filter(pk=self.pk).values('email', 'phone', 'password').first()
            phone_written = fields is None or 'phone' in fields
            phones = set()
            if phone_written and (previous is None or previous['phone'] != self.phone):
                phones = {value for value in (previous['phone'] if previous else None, self.phone) if value}
            try:
                with transaction.atomic(using=using):
                    # Match OTP issuance/check ordering: recipient -> user -> OTP.
                    for value in sorted(phones):
                        lock_phone_recipient(value, using=using)
                    current = None
                    if previous is not None:
                        current = type(self)._base_manager.using(using).select_for_update().filter(pk=self.pk).values('email', 'phone', 'password').first()
                        if current is not None and phone_written and current['phone'] != previous['phone']:
                            raise _IdentityChangedDuringLock
                    self.validate_identity(
                        current['email'] if current is not None and fields is not None and 'email' not in fields else self.email,
                        current['phone'] if current is not None and fields is not None and 'phone' not in fields else self.phone,
                    )
                    email_changed = current is not None and (fields is None or 'email' in fields) and normalize_email(current['email']) != self.email
                    phone_changed = current is not None and phone_written and current['phone'] != self.phone
                    for name, changed in (('email', email_changed), ('phone', phone_changed)):
                        if changed or (not getattr(self, name) and (fields is None or name in fields or name + '_verified' in fields)):
                            setattr(self, name + '_verified', False)
                            if fields is not None:
                                fields.add(name + '_verified')
                    if fields is not None:
                        kwargs['update_fields'] = fields
                    result = super().save(*args, **kwargs)
                    password_changed = current is not None and (fields is None or 'password' in fields) and current['password'] != self.password
                    if email_changed or phone_changed or password_changed:
                        AccountChallenge.objects.using(using).filter(user_id=self.pk).delete()
                    if email_changed:
                        EmailOTP.objects.using(using).filter(user_id=self.pk).delete()
                    if phone_changed or previous is None:
                        if phones:
                            PhoneOTP.objects.using(using).filter(phone__in=phones).delete()
                    return result
            except _IdentityChangedDuringLock:
                continue

    def __str__(self):
        return self.email or self.phone or str(self.pk)


class GoogleBrowserChallenge(models.Model):
    session_digest = models.CharField(max_length=64, primary_key=True)
    nonce_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)


class AccountChallengeRecipient(models.Model):
    key = models.CharField(max_length=300, unique=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    window_start = models.DateTimeField(null=True, blank=True)
    sent_count = models.PositiveSmallIntegerField(default=0)
    last_used_at = models.DateTimeField(default=timezone.now, db_index=True)


class ThrottleBucket(models.Model):
    key = models.CharField(max_length=255, primary_key=True)
    history = models.JSONField(default=list)
    expires_at = models.DateTimeField(db_index=True)


class DeliveryJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20)
    state = models.CharField(max_length=12, default='pending', db_index=True)
    payload = models.TextField()
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    lease_id = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True)


class VerificationReceipt(models.Model):
    kind = models.CharField(max_length=10)
    record_key = models.CharField(max_length=40)
    version = models.UUIDField()
    verification_sid = models.CharField(max_length=34)
    service_sid = models.CharField(max_length=34)
    code_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=12, default='checking')
    claim_id = models.UUIDField(default=uuid.uuid4)
    lease_until = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['kind', 'record_key', 'version'], name='accounts_receipt_generation_unique')]


class AccountChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    purpose = models.CharField(max_length=20, choices=[
        ('reset', 'Password reset'), ('reauth', 'Reauthentication'),
        ('email', 'Email change'), ('phone', 'Phone change'),
    ])
    channel = models.CharField(max_length=5, choices=[('email', 'Email'), ('phone', 'Phone')])
    target = models.CharField(max_length=254)
    email_snapshot = models.CharField(max_length=254, blank=True)
    phone_snapshot = models.CharField(max_length=20, blank=True)
    password_digest = models.CharField(max_length=64)
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)
    backend = models.CharField(max_length=20, default='local')
    verification_sid = models.CharField(max_length=34, blank=True)
    service_sid = models.CharField(max_length=34, blank=True)
    delivery = models.OneToOneField(DeliveryJob, null=True, blank=True, on_delete=models.SET_NULL)


class GoogleIdentity(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='google_identity')
    subject = models.CharField(max_length=255, unique=True)


class EmailOTP(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='email_otp')
    email = models.EmailField()
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)
    delivery = models.OneToOneField(DeliveryJob, null=True, blank=True, on_delete=models.SET_NULL)


class PhoneOTPRecipient(models.Model):
    phone = models.CharField(max_length=20, unique=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(default=timezone.now, db_index=True)


class PhoneOTP(models.Model):
    phone = models.CharField(max_length=20, unique=True)
    backend = models.CharField(max_length=20, default='local', choices=[('local', 'Local'), ('twilio_verify', 'Twilio Verify')])
    verification_sid = models.CharField(max_length=34, blank=True)
    service_sid = models.CharField(max_length=34, blank=True)
    version = models.UUIDField(default=uuid.uuid4)
    delivery = models.OneToOneField(DeliveryJob, null=True, blank=True, on_delete=models.SET_NULL)

    code_hash = models.CharField(max_length=128)

    attempts = models.PositiveSmallIntegerField(default=0)

    expires_at = models.DateTimeField(db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.phone


