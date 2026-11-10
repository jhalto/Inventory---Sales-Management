from django.contrib.auth.models import UserManager


class CustomUserManager(UserManager):

    def create_user(
        self,
        email=None,
        password=None,
        **extra_fields
    ):
        phone = extra_fields.get("phone")

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

    def create_superuser(
        self,
        email,
        password,
        **extra_fields
    ):
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