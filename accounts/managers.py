from django.contrib.auth.models import UserManager
from django.db import models
from asgiref.sync import sync_to_async
from .identities import normalize_email, normalize_phone


class UserQuerySet(models.QuerySet):
    identity_fields = frozenset({'email', 'phone', 'email_verified', 'phone_verified'})

    def _guard_identity_writes(self, fields):
        # Historical migration models intentionally lack this runtime marker;
        # their reviewed data migrations must still be able to repair identities.
        if getattr(self.model, '_enforce_identity_bulk_policy', False) and self.identity_fields.intersection(fields):
            raise ValueError('Bulk identity/verification writes are unsupported. Load each user and call save() so normalization, verification resets and OTP invalidation run.')

    def update(self, **kwargs):
        self._guard_identity_writes(kwargs)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        fields = tuple(fields)
        self._guard_identity_writes(fields)
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        if getattr(self.model, '_enforce_identity_bulk_policy', False):
            raise ValueError('Bulk user creation is unsupported. Use create_user() or save() for each account.')
        return super().bulk_create(objs, *args, **kwargs)


class CustomUserManager(UserManager.from_queryset(UserQuerySet)):

    @classmethod
    def normalize_email(cls, email):
        return normalize_email(email)

    def get_by_natural_key(self, username):
        email = self.normalize_email(username)
        if email is None:
            raise self.model.DoesNotExist
        return self.get(email__iexact=email)

    async def aget_by_natural_key(self, username):
        email = self.normalize_email(username)
        if email is None:
            raise self.model.DoesNotExist
        return await self.aget(email__iexact=email)

    def create_user(
        self,
        email=None,
        password=None,
        **extra_fields
    ):
        phone = normalize_phone(extra_fields.get("phone"))
        extra_fields['phone'] = phone

        email = self.normalize_email(email) if email else None

        if not email and not phone:
            raise ValueError("Email or phone is required")

        user = self.model(
            email=email,
            **extra_fields
        )

        if password is not None:
            user.set_password(password)
        else:
            user.set_unusable_password()

        user.save(using=self._db)

        return user

    async def acreate_user(self, email=None, password=None, **extra_fields):
        # Keep normalization, hashing and the transactional save in one worker.
        return await sync_to_async(self.create_user, thread_sensitive=True)(
            email=email, password=password, **extra_fields
        )

    acreate_user.alters_data = True

    def create_superuser(
        self,
        email,
        password,
        **extra_fields
    ):
        email = self.normalize_email(email)
        if not email or not password:
            raise ValueError(
                "Superuser requires email and password"
            )

        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", "admin")

        if extra_fields["is_staff"] is not True:
            raise ValueError("Superuser must have is_staff=True")

        if extra_fields["is_superuser"] is not True:
            raise ValueError("Superuser must have is_superuser=True")

        return self.create_user(
            email=email,
            password=password,
            **extra_fields
        )

    async def acreate_superuser(self, email, password, **extra_fields):
        return await sync_to_async(self.create_superuser, thread_sensitive=True)(
            email=email, password=password, **extra_fields
        )

    acreate_superuser.alters_data = True
