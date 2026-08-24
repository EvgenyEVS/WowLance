import uuid

from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator


class FreelancerProfile(models.Model):
    """
    Профиль фрилансера. Связь 1:1 с User.
    Имя/фамилия — только в User (без дублирования).
    """

    class Level(models.TextChoices):
        JUNIOR = 'junior', _('Junior')
        MIDDLE = 'middle', _('Middle')
        SENIOR = 'senior', _('Senior')

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='freelancer_profile',
        verbose_name=_('Пользователь'),
    )

    country = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_('Страна проживания'),
    )
    level = models.CharField(
        max_length=20,
        choices=Level.choices,
        default=Level.MIDDLE,
        verbose_name=_('Уровень'),
    )
    experience_years = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Опыт (лет)'),
    )
    experience_projects = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Опыт (проектов)'),
    )

    languages = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Владение языками'),
        help_text='Формат: [{"language": "Английский", "level": "B2"}]',
    )
    key_advantages = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Ключевые преимущества'),
        help_text='Максимум 3 преимущества',
    )
    skills = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Навыки и методы продаж'),
        help_text='Например: ["Холодные звонки", "SPIN", "Salesforce"]',
    )
    portfolio_links = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Ссылки на портфолио'),
        help_text='Например: ["https://linkedin.com/in/...", "https://drive.google.com/..."]',
    )

    # Структурированные каналы работы для будущего подбора (staffing/matching).
    # В отличие от свободного текста `skills`, эти признаки пригодны
    # для жёсткого ORM-фильтра. Оба признака независимы и могут быть True вместе.
    does_cold_calling = models.BooleanField(
        default=False,
        verbose_name=_('Работает с холодными звонками'),
        help_text=_('Cold Calling — структурированный признак для подбора'),
    )
    does_linkedin_outreach = models.BooleanField(
        default=False,
        verbose_name=_('Работает с LinkedIn-аутричем'),
        help_text=_('LinkedIn — структурированный признак для подбора'),
    )

    linkedin_url = models.URLField(
        blank=True,
        verbose_name=_('LinkedIn'),
    )
    avatar_url = models.URLField(
        blank=True,
        verbose_name=_('Аватар (URL)'),
        help_text=_('Ссылка на деловое фото профиля'),
    )
    video_url = models.URLField(
        blank=True,
        verbose_name=_('Видеопрезентация'),
        help_text='Ссылка на YouTube или Vimeo (до 40 секунд)',
    )

    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
        verbose_name=_('Рейтинг'),
    )
    acceptance_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name=_('Процент принятых отчётов'),
        help_text='0–100%',
    )
    is_verified = models.BooleanField(
        default=False,
        verbose_name=_('Верифицирован'),
    )
    is_available = models.BooleanField(
        default=True,
        verbose_name=_('Доступен для заказов'),
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создан'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Обновлён'))

    class Meta:
        verbose_name = _('Профиль фрилансера')
        verbose_name_plural = _('Профили фрилансеров')
        indexes = [
            models.Index(fields=['rating']),
            models.Index(fields=['level']),
            models.Index(fields=['is_verified']),
            models.Index(fields=['is_available']),
            models.Index(fields=['country']),
            models.Index(fields=['does_cold_calling']),
            models.Index(fields=['does_linkedin_outreach']),
        ]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        return self.user.full_name

    @property
    def skills_list(self):
        return self.skills if isinstance(self.skills, list) else []

    @property
    def advantages_list(self):
        return self.key_advantages if isinstance(self.key_advantages, list) else []

    @property
    def portfolio_links_list(self):
        return self.portfolio_links if isinstance(self.portfolio_links, list) else []


class Portfolio(models.Model):
    """
    Портфолио фрилансера — контейнер для работ, файлов и ссылок.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    profile = models.OneToOneField(
        FreelancerProfile,
        on_delete=models.CASCADE,
        related_name='portfolio',
        verbose_name=_('Профиль'),
    )
    title = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_('Заголовок'),
        help_text='Например: «Кейсы B2B-продаж»',
    )
    description = models.TextField(
        blank=True,
        verbose_name=_('Описание'),
    )
    is_public = models.BooleanField(
        default=True,
        verbose_name=_('Публичное'),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создан'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Обновлён'))

    class Meta:
        verbose_name = _('Портфолио')
        verbose_name_plural = _('Портфолио')

    def __str__(self):
        return self.title or f'Портфолио {self.profile.full_name}'


class PortfolioItem(models.Model):
    """
    Элемент портфолио: файл, внешняя ссылка или кейс.
    """

    class ItemType(models.TextChoices):
        FILE = 'file', _('Файл')
        LINK = 'link', _('Ссылка')
        CASE = 'case', _('Кейс')

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name=_('Портфолио'),
    )
    item_type = models.CharField(
        max_length=10,
        choices=ItemType.choices,
        default=ItemType.FILE,
        verbose_name=_('Тип'),
    )
    title = models.CharField(max_length=255, verbose_name=_('Название'))
    description = models.TextField(blank=True, verbose_name=_('Описание'))
    file = models.FileField(
        upload_to='portfolio/%Y/%m/%d/',
        blank=True,
        null=True,
        verbose_name=_('Файл'),
    )
    file_size = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Размер (байты)'),
    )
    url = models.URLField(
        blank=True,
        verbose_name=_('URL'),
    )
    sort_order = models.PositiveSmallIntegerField(
        default=0,
        verbose_name=_('Порядок'),
    )
    is_public = models.BooleanField(
        default=True,
        verbose_name=_('Публичный'),
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_('Дата добавления'),
    )

    class Meta:
        verbose_name = _('Элемент портфолио')
        verbose_name_plural = _('Элементы портфолио')
        ordering = ['sort_order', '-created_at']

    def __str__(self):
        return self.title

    def delete(self, *args, **kwargs):
        if self.file:
            self.file.delete(save=False)
        super().delete(*args, **kwargs)
