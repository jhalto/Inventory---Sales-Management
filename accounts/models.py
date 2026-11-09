from django.db import models
from django.contrib.auth.models import AbstractUser
# Create your models here.

class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        STAFF = "staff", "Staff"
        CUSTOMER = 'customer', "Customer"

    role = models.CharField(
        max_length= 10,
        choices= Role.choices,
        default= Role.CUSTOMER
    )
    phone = models.CharField(
        max_length= 20,
        blank= True
    )

    def __str__(self):
        return self.username