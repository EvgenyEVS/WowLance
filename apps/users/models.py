from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """
    Расширенная модель пользователя с ролью
    """
    class Roles(models.TextChoices):
        DIRECTOR = 'director', _('Директор')
        FREELANCER = 'freelancer', _('Фрилансер')
        ADMIN = 'admin', _('Администратор')

    email = models.EmailField(_('email address'), unique=True)
    role = models.CharField(
        max_length=20,
        choices=Roles.choices,
        default=Roles.FREELANCER,
        verbose_name=_('Роль')
    )
    phone = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name=_('Телефон')
    )
    is_email_verified = models.BooleanField(
        default=False,
        verbose_name=_('Email подтверждён')
    )

    # Убираем username, используем email для входа
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return self.email

    class Meta:
        verbose_name = _('Пользователь')
        verbose_name_plural = _('Пользователи')