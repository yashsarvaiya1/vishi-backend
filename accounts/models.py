# accounts/models.py

from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin


class UserManager(BaseUserManager):
    def create_user(self, mobile_number, password=None, **extra_fields):
        if not mobile_number:
            raise ValueError('Mobile number is required')
        user = self.model(mobile_number=mobile_number, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.password = None
        user.save(using=self._db)
        return user

    def create_superuser(self, mobile_number, password=None, **extra_fields):
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_active', True)
        return self.create_user(mobile_number, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    mobile_number       = models.CharField(max_length=15, unique=True)
    password            = models.CharField(max_length=128, null=True, blank=True)
    username            = models.CharField(max_length=150, blank=True)
    address             = models.TextField(blank=True)
    additional_contacts = models.JSONField(default=list, blank=True)
    is_active           = models.BooleanField(default=True)
    is_staff            = models.BooleanField(default=False)
    is_superuser        = models.BooleanField(default=False)
    date_joined         = models.DateTimeField(auto_now_add=True)
    last_login          = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD  = 'mobile_number'
    REQUIRED_FIELDS = []
    objects         = UserManager()

    def __str__(self):
        return self.username or self.mobile_number

    class Meta:
        ordering = ['date_joined']
