import uuid

from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """
    Базовая модель пользователя для всех ролей платформы.
    Имя и фамилия хранятся только здесь (единый источник правды).
    """

    class Roles(models.TextChoices):
        DIRECTOR = 'director', _('Директор')
        TEAMLEAD = 'teamlead', _('Тимлид')
        MANAGER = 'manager', _('Менеджер')
        FREELANCER = 'freelancer', _('Фрилансер')
        ADMIN = 'admin', _('Администратор')

    class Status(models.TextChoices):
        PENDING = 'pending', _('Ожидает подтверждения')
        ACTIVE = 'active', _('Активен')
        BLOCKED = 'blocked', _('Заблокирован')

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    email = models.EmailField(_('email address'), unique=True)
    role = models.CharField(
        max_length=20,
        choices=Roles.choices,
        default=Roles.FREELANCER,
        verbose_name=_('Роль'),
    )
    phone = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name=_('Телефон'),
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name=_('Статус аккаунта'),
    )
    is_email_verified = models.BooleanField(
        default=False,
        verbose_name=_('Email подтверждён'),
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return self.email

    @property
    def full_name(self):
        name = f'{self.first_name} {self.last_name}'.strip()
        return name or self.email

    def save(self, *args, **kwargs):
        self._sync_active_flag()
        super().save(*args, **kwargs)

    def _sync_active_flag(self):
        """Django is_active синхронизируется со статусом аккаунта."""
        self.is_active = self.status == self.Status.ACTIVE

    class Meta:
        verbose_name = _('Пользователь')
        verbose_name_plural = _('Пользователи')
