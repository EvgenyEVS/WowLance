import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
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


class RoomFunctionSlot(models.Model):
    """
    Функциональный слот команды проекта.

    Директор покупает функции, а не конкретных людей: если проекту нужно
    два `seller_middle`, создаются два отдельных стабильных слота
    (slot_index 1 и 2). Каждый слот независимо пустой или занятый
    и имеет собственную историю кандидатов (`RoomSlotCandidate`).

    Кто занимает слот — хранится ТОЛЬКО в `RoomMember.function_slot`
    (см. свойство `assigned_member` ниже). Второго поля назначения здесь нет,
    чтобы источник истины не мог рассинхронизироваться.
    """

    class Grade(models.TextChoices):
        """
        Требуемый грейд исполнителя.

        Значения совпадают с `apps.profiles.FreelancerProfile.Level`,
        но enum объявлен в ROOM: BIZ и ROOM не импортируют друг друга (ADR-001).
        """

        JUNIOR = 'junior', _('Junior')
        MIDDLE = 'middle', _('Middle')
        SENIOR = 'senior', _('Senior')

    class Channel(models.TextChoices):
        """Требуемый канал работы. `ANY` — канал слоту не важен."""

        ANY = 'any', _('Любой канал')
        COLD_CALLING = 'cold_calling', _('Холодные звонки')
        LINKEDIN = 'linkedin', _('LinkedIn')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        related_name='function_slots',
        verbose_name=_('Комната'),
    )
    role_key = models.CharField(
        max_length=64,
        verbose_name=_('Ключ функции'),
        help_text=_(
            'Стабильный ключ бизнес-функции, например seller. '
            'Каталог ролей — отдельный этап, поэтому справочник в БД не заводим.'
        ),
    )
    slot_index = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name=_('Номер слота'),
        help_text=_('Различает одинаковые роли внутри комнаты: 1, 2, 3…'),
    )
    required_level = models.CharField(
        max_length=20,
        choices=Grade.choices,
        default=Grade.MIDDLE,
        verbose_name=_('Требуемый грейд'),
    )
    required_channel = models.CharField(
        max_length=20,
        choices=Channel.choices,
        default=Channel.ANY,
        verbose_name=_('Требуемый канал'),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_('Слот активен'),
        help_text=_('False — слот закрыт (отменён) и в подборе не участвует'),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создан'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Обновлён'))

    class Meta:
        verbose_name = _('Функциональный слот')
        verbose_name_plural = _('Функциональные слоты')
        ordering = ['role_key', 'slot_index']
        constraints = [
            models.UniqueConstraint(
                fields=['room', 'role_key', 'slot_index'],
                name='unique_room_function_slot',
            ),
            models.CheckConstraint(
                condition=models.Q(slot_index__gte=1),
                name='room_function_slot_index_min_1',
            ),
        ]

    def __str__(self):
        return f'{self.role_key} #{self.slot_index} @ {self.room.project.name}'

    @property
    def assigned_member(self):
        """Участник, занимающий слот, или None. Единственный источник истины."""
        return getattr(self, 'member', None)

    @property
    def is_filled(self) -> bool:
        return self.assigned_member is not None


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
    role_key = models.CharField(
        max_length=64,
        blank=True,
        default='',
        verbose_name=_('Ключ закрываемой функции'),
        help_text=_(
            'Какую бизнес-функцию закрывает участник (например seller). '
            'Не заменяет role_in_room: тот отвечает за права в комнате.'
        ),
    )
    function_slot = models.OneToOneField(
        'rooms.RoomFunctionSlot',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='member',
        verbose_name=_('Функциональный слот'),
        help_text=_('Единственный источник истины «кто занимает слот»'),
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

    def save(self, *args, update_fields=None, **kwargs):
        """
        Держит `role_key` в согласии со слотом.

        Слот остаётся единственным источником истины о функции; `role_key`
        участника — денормализованная копия, которую модель сама
        приводит к значению слота при каждом `save()` / `objects.create()`.
        Участник без слота сохраняется как раньше: его `role_key` не трогаем.
        """
        if self.function_slot_id and self.role_key != self.function_slot.role_key:
            self.role_key = self.function_slot.role_key
            if update_fields is not None:
                update_fields = {*update_fields, 'role_key'}
        super().save(*args, update_fields=update_fields, **kwargs)

    def clean(self):
        super().clean()
        if not self.function_slot_id:
            return
        if self.function_slot.room_id != self.room_id:
            raise ValidationError(
                {'function_slot': _('Слот принадлежит другой комнате.')}
            )
        # role_key участника не дублирует слот, а лишь не должен ему противоречить.
        # Пустое значение означает «не указан» и остаётся допустимым:
        # источник истины о функции слота — сам слот.
        if self.role_key and self.role_key != self.function_slot.role_key:
            raise ValidationError(
                {
                    'role_key': _(
                        'role_key участника не совпадает с role_key слота: %(slot_role_key)s.'
                    )
                    % {'slot_role_key': self.function_slot.role_key},
                }
            )


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


class RoomSlotCandidate(models.Model):
    """
    Состояние кандидата по конкретному функциональному слоту.

    Нужна будущему подбору, чтобы понимать, кого уже показывали, кого
    назначили, кого пропустили и кто отказался. Живёт в ROOM (это данные
    комнаты), а не в профиле фрилансера.

    На пару (слот, кандидат) хранится одна строка: `outcome` обновляется,
    `created_at` фиксирует первый показ, `updated_at` — последнее изменение.
    Это исключает неконтролируемые дубли, из-за которых будущая логика
    «Другой сейлер» могла бы предложить одного и того же человека повторно.
    """

    class Outcome(models.TextChoices):
        SHOWN = 'shown', _('Показан')
        ASSIGNED = 'assigned', _('Назначен')
        SKIPPED = 'skipped', _('Пропущен')
        DECLINED = 'declined', _('Отказался')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slot = models.ForeignKey(
        RoomFunctionSlot,
        on_delete=models.CASCADE,
        related_name='candidates',
        verbose_name=_('Слот'),
    )
    candidate = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='slot_candidacies',
        verbose_name=_('Кандидат'),
    )
    outcome = models.CharField(
        max_length=20,
        choices=Outcome.choices,
        default=Outcome.SHOWN,
        verbose_name=_('Результат'),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='slot_candidate_actions',
        verbose_name=_('Кто изменил'),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создано'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Обновлено'))

    class Meta:
        verbose_name = _('Кандидат на слот')
        verbose_name_plural = _('Кандидаты на слоты')
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['slot', 'candidate'],
                name='unique_slot_candidate',
            ),
        ]

    def __str__(self):
        return f'{self.candidate} → {self.slot} ({self.get_outcome_display()})'

