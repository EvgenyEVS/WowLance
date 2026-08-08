from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator


class FreelancerProfile(models.Model):
    """
    Профиль фрилансера. Связь 1:1 с User.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
        verbose_name=_('Пользователь')
    )

    # Основная информация
    first_name = models.CharField(max_length=50, verbose_name=_('Имя'))
    last_name = models.CharField(max_length=50, verbose_name=_('Фамилия'))
    country = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_('Страна проживания')
    )
    experience_years = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Опыт (лет)')
    )
    experience_projects = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Опыт (проектов)')
    )

    # Языки (хранятся в JSON)
    languages = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Владение языками'),
        help_text='Формат: [{"language": "Английский", "level": "B2"}]'
    )

    # Ключевые преимущества (максимум 3)
    key_advantages = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Ключевые преимущества'),
        help_text='Максимум 3 преимущества'
    )

    # Навыки и методы продаж
    skills = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Навыки и методы продаж'),
        help_text='Например: ["Холодные звонки", "SPIN", "Salesforce"]'
    )

    # Социальные сети
    linkedin_url = models.URLField(
        blank=True,
        verbose_name=_('LinkedIn')
    )
    video_url = models.URLField(
        blank=True,
        verbose_name=_('Видеопрезентация'),
        help_text='Ссылка на YouTube или Vimeo (до 40 секунд)'
    )

    # Рейтинг и верификация
    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
        verbose_name=_('Рейтинг')
    )
    is_verified = models.BooleanField(
        default=False,
        verbose_name=_('Верифицирован')
    )
    is_available = models.BooleanField(
        default=True,
        verbose_name=_('Доступен для заказов')
    )

    # Системные поля
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создан'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Обновлён'))

    class Meta:
        verbose_name = _('Профиль фрилансера')
        verbose_name_plural = _('Профили фрилансеров')
        indexes = [
            models.Index(fields=['rating']),
            models.Index(fields=['is_verified']),
            models.Index(fields=['is_available']),
            models.Index(fields=['country']),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def skills_list(self):
        """Возвращает список навыков"""
        return self.skills if isinstance(self.skills, list) else []

    @property
    def advantages_list(self):
        """Возвращает список преимуществ"""
        return self.key_advantages if isinstance(self.key_advantages, list) else []


class PortfolioFile(models.Model):
    """
    Файлы портфолио (резюме, сертификаты, примеры работ)
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='portfolio_files',
        verbose_name=_('Пользователь')
    )
    file = models.FileField(
        upload_to='portfolio/%Y/%m/%d/',
        verbose_name=_('Файл')
    )
    file_name = models.CharField(
        max_length=255,
        verbose_name=_('Название файла')
    )
    file_size = models.PositiveIntegerField(
        verbose_name=_('Размер (байты)')
    )
    uploaded_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_('Дата загрузки')
    )

    class Meta:
        verbose_name = _('Файл портфолио')
        verbose_name_plural = _('Файлы портфолио')
        ordering = ['-uploaded_at']

    def __str__(self):
        return self.file_name

    def delete(self, *args, **kwargs):
        """Удаляем файл из хранилища при удалении записи"""
        self.file.delete(save=False)
        super().delete(*args, **kwargs)