import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Project(models.Model):
    """Проект директора, в рамках которого запускаются продажи."""

    class Type(models.TextChoices):
        BASE = 'base', _('По базе')
        LINKEDIN = 'linkedin_outreach', _('Аутрич LinkedIn')

    class SellerLevel(models.TextChoices):
        JUNIOR = 'junior', _('Junior')
        MIDDLE = 'middle', _('Middle')
        SENIOR = 'senior', _('Senior')

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Черновик')
        LAUNCHED = 'launched', _('Запущен')
        STAFFING = 'staffing', _('Подбор команды')
        ACTIVE = 'active', _('Активен')
        ON_HOLD = 'on_hold', _('Приостановлен')
        COMPLETED = 'completed', _('Завершён')
        ARCHIVED = 'archived', _('Архив')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='owned_projects',
        verbose_name=_('Директор'),
    )
    name = models.CharField(max_length=255, verbose_name=_('Название'))
    project_type = models.CharField(
        max_length=32,
        choices=Type.choices,
        default=Type.BASE,
        verbose_name=_('Тип продаж'),
    )
    seller_level = models.CharField(
        max_length=20,
        choices=SellerLevel.choices,
        default=SellerLevel.MIDDLE,
        verbose_name=_('Уровень продавцов'),
    )
    tariff_plan = models.CharField(
        max_length=50,
        default='launch',
        verbose_name=_('Тариф'),
    )
    input_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_('Вводные по шаблону'),
        help_text=_('offer, audience, hot_criteria, utp, notes'),
    )
    budget = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        default=0,
        verbose_name=_('Бюджет'),
    )
    start_date = models.DateField(null=True, blank=True, verbose_name=_('Дата старта'))
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        verbose_name=_('Статус'),
    )
    teamlead = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='led_projects',
        verbose_name=_('Тимлид'),
    )
    kpi_target = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        verbose_name=_('Целевой KPI'),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создан'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Обновлён'))

    class Meta:
        verbose_name = _('Проект')
        verbose_name_plural = _('Проекты')
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def offer(self):
        return (self.input_data or {}).get('offer', '')

    @property
    def audience(self):
        return (self.input_data or {}).get('audience', '')

    @property
    def hot_criteria(self):
        return (self.input_data or {}).get('hot_criteria', '')

    @property
    def utp(self):
        return (self.input_data or {}).get('utp', '')


class Room(models.Model):
    """Рабочее пространство проекта."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name='room',
        verbose_name=_('Проект'),
    )
    chat_enabled = models.BooleanField(default=False, verbose_name=_('Чат включён'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создана'))

    class Meta:
        verbose_name = _('Комната')
        verbose_name_plural = _('Комнаты')

    def __str__(self):
        return f'Комната: {self.project.name}'


class RoomMember(models.Model):
    """Участник комнаты проекта."""

    class RoleInRoom(models.TextChoices):
        DIRECTOR = 'director', _('Директор')
        TEAMLEAD = 'teamlead', _('Тимлид')
        FREELANCER = 'freelancer', _('Фрилансер')

    class ReadyStatus(models.TextChoices):
        PENDING = 'pending', _('Ожидает')
        READY = 'ready', _('Готов к работе')
        DECLINED = 'declined', _('Отказался')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        related_name='members',
        verbose_name=_('Комната'),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='room_memberships',
        verbose_name=_('Пользователь'),
    )
    role_in_room = models.CharField(
        max_length=20,
        choices=RoleInRoom.choices,
        verbose_name=_('Роль в комнате'),
    )
    ready_status = models.CharField(
        max_length=20,
        choices=ReadyStatus.choices,
        default=ReadyStatus.PENDING,
        verbose_name=_('Готовность'),
    )
    joined_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Добавлен'))

    class Meta:
        verbose_name = _('Участник комнаты')
        verbose_name_plural = _('Участники комнат')
        constraints = [
            models.UniqueConstraint(
                fields=['room', 'user'],
                name='unique_room_member',
            ),
        ]
        ordering = ['joined_at']

    def __str__(self):
        return f'{self.user} @ {self.room.project.name}'


class RoomDocument(models.Model):
    """Документ / вижен в комнате проекта."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        related_name='documents',
        verbose_name=_('Комната'),
    )
    title = models.CharField(max_length=255, verbose_name=_('Название'))
    file = models.FileField(
        upload_to='rooms/documents/%Y/%m/%d/',
        verbose_name=_('Файл'),
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='uploaded_room_documents',
        verbose_name=_('Загрузил'),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Загружен'))

    class Meta:
        verbose_name = _('Документ комнаты')
        verbose_name_plural = _('Документы комнат')
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    def delete(self, *args, **kwargs):
        if self.file:
            self.file.delete(save=False)
        super().delete(*args, **kwargs)


class RoomActivity(models.Model):
    """Лента событий комнаты."""

    class EventType(models.TextChoices):
        PROJECT_LAUNCHED = 'project_launched', _('Проект запущен')
        MEMBER_ADDED = 'member_added', _('Участник добавлен')
        MEMBER_REMOVED = 'member_removed', _('Участник удалён')
        TEAMLEAD_ASSIGNED = 'teamlead_assigned', _('Тимлид назначен')
        DOCUMENT_UPLOADED = 'document_uploaded', _('Документ загружен')
        TASK_CREATED = 'task_created', _('Задача создана')
        LEAD_CREATED = 'lead_created', _('Лид создан')
        READY = 'ready', _('Готовность')
        OTHER = 'other', _('Другое')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        related_name='activities',
        verbose_name=_('Комната'),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='room_activities',
        verbose_name=_('Автор'),
    )
    event_type = models.CharField(
        max_length=40,
        choices=EventType.choices,
        default=EventType.OTHER,
        verbose_name=_('Тип'),
    )
    message = models.CharField(max_length=500, verbose_name=_('Сообщение'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создано'))

    class Meta:
        verbose_name = _('Событие комнаты')
        verbose_name_plural = _('События комнат')
        ordering = ['-created_at']

    def __str__(self):
        return self.message


class TeamleadInvite(models.Model):
    """Приглашение тимлида по ссылке (без ручной админки)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='teamlead_invites',
        verbose_name=_('Проект'),
    )
    token = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_teamlead_invites',
        verbose_name=_('Создал'),
    )
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='accepted_teamlead_invites',
        verbose_name=_('Принял'),
    )
    is_active = models.BooleanField(default=True, verbose_name=_('Активно'))
    expires_at = models.DateTimeField(verbose_name=_('Истекает'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создано'))
    accepted_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Принято'))

    class Meta:
        verbose_name = _('Приглашение тимлида')
        verbose_name_plural = _('Приглашения тимлидов')
        ordering = ['-created_at']

    def __str__(self):
        return f'Invite → {self.project.name}'

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = secrets.token_urlsafe(32)
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=7)
        super().save(*args, **kwargs)

    @property
    def is_valid(self) -> bool:
        return (
            self.is_active
            and self.accepted_by_id is None
            and self.expires_at > timezone.now()
        )
