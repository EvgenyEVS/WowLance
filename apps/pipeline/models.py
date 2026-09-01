import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class Task(models.Model):
    """Задача в проекте (фрилансер или менеджер)."""

    class Status(models.TextChoices):
        NEW = 'new', _('Новая')
        IN_PROGRESS = 'in_progress', _('В работе')
        READY_FOR_REVIEW = 'ready_for_review', _('На проверке')
        APPROVED = 'approved', _('Утверждена')
        REJECTED = 'rejected', _('Отклонена')
        CLOSED = 'closed', _('Закрыта')

    class TaskType(models.TextChoices):
        WORK = 'work', _('Рабочая')
        MANAGER_HANDOFF = 'manager_handoff', _('Горячий лид → менеджер')
        ONBOARDING = 'onboarding', _('Онбординг')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        'rooms.Project',
        on_delete=models.CASCADE,
        related_name='tasks',
        verbose_name=_('Проект'),
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='assigned_tasks',
        verbose_name=_('Исполнитель'),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_tasks',
        verbose_name=_('Создал'),
    )
    lead = models.ForeignKey(
        'pipeline.Lead',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='manager_tasks',
        verbose_name=_('Связанный лид'),
    )
    title = models.CharField(max_length=255, verbose_name=_('Название'))
    description = models.TextField(blank=True, verbose_name=_('Описание'))
    deadline = models.DateTimeField(null=True, blank=True, verbose_name=_('Дедлайн'))
    checklist = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Чеклист'),
        help_text=_('[{"text": "...", "done": false}]'),
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.NEW,
        verbose_name=_('Статус'),
    )
    task_type = models.CharField(
        max_length=32,
        choices=TaskType.choices,
        default=TaskType.WORK,
        verbose_name=_('Тип задачи'),
    )
    report_required = models.BooleanField(
        default=True,
        verbose_name=_('Обязателен отчёт'),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создана'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Обновлена'))
    #: Фактический момент закрытия задачи. Ставится один раз в
    #: `apps.pipeline.services.close_task` и больше не сдвигается.
    #: Нужен SLA, статистике исполнителей и будущему рейтингу, потому что
    #: `updated_at` — `auto_now` и переписывается при любом сохранении.
    #: `null=True` обязателен: задачам, закрытым до появления поля, честное
    #: время закрытия задним числом не восстановить — у них остаётся NULL.
    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_('Закрыта'),
    )

    class Meta:
        verbose_name = _('Задача')
        verbose_name_plural = _('Задачи')
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    def clean(self):
        if len(self.title or '') < 5:
            raise ValidationError({'title': _('Название — минимум 5 символов.')})

    @property
    def latest_report(self):
        return self.reports.order_by('-created_at').first()

    def can_be_closed(self) -> bool:
        if not self.report_required:
            return self.status in {
                self.Status.APPROVED,
                self.Status.IN_PROGRESS,
                self.Status.NEW,
                self.Status.READY_FOR_REVIEW,
            }
        report = self.latest_report
        return bool(
            report
            and report.review_status == Report.ReviewStatus.APPROVED
            and self.status == self.Status.APPROVED
        )


class Report(models.Model):
    """Отчёт по задаче (текст + вложение)."""

    class ReviewStatus(models.TextChoices):
        PENDING = 'pending', _('На проверке')
        APPROVED = 'approved', _('Принят')
        REJECTED = 'rejected', _('Отклонён')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='reports',
        verbose_name=_('Задача'),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reports',
        verbose_name=_('Автор'),
    )
    content_text = models.TextField(verbose_name=_('Текст отчёта'))
    attachment = models.FileField(
        upload_to='reports/%Y/%m/%d/',
        verbose_name=_('Вложение (скриншот)'),
    )
    reviewer_comment = models.TextField(
        blank=True,
        verbose_name=_('Комментарий проверяющего'),
    )
    review_status = models.CharField(
        max_length=20,
        choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING,
        verbose_name=_('Статус проверки'),
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_reports',
        verbose_name=_('Проверил'),
    )
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Проверен'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создан'))

    class Meta:
        verbose_name = _('Отчёт')
        verbose_name_plural = _('Отчёты')
        ordering = ['-created_at']

    def __str__(self):
        return f'Отчёт по «{self.task.title}»'

    def clean(self):
        if len((self.content_text or '').strip()) < 10:
            raise ValidationError({'content_text': _('Текст отчёта — минимум 10 символов.')})
        if not self.attachment:
            raise ValidationError({'attachment': _('Вложение обязательно.')})


class Lead(models.Model):
    """Карточка лида."""

    class Source(models.TextChoices):
        LINKEDIN = 'linkedin', _('LinkedIn')
        BASE = 'base', _('База')
        OTHER = 'other', _('Другое')

    class Qualification(models.TextChoices):
        COLD = 'cold', _('Холодный')
        WARM = 'warm', _('Тёплый')
        HOT = 'hot', _('Горячий')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        'rooms.Project',
        on_delete=models.CASCADE,
        related_name='leads',
        verbose_name=_('Проект'),
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_leads',
        verbose_name=_('Создал'),
    )
    contact_info = models.JSONField(
        default=dict,
        verbose_name=_('Контакты'),
        help_text=_('name, phone, email, linkedin'),
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.OTHER,
        verbose_name=_('Источник'),
    )
    qualification_status = models.CharField(
        max_length=20,
        choices=Qualification.choices,
        default=Qualification.COLD,
        verbose_name=_('Квалификация'),
    )
    notes = models.TextField(blank=True, verbose_name=_('Комментарий'))
    matched_hot_criteria = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Совпавшие критерии горячего лида'),
    )
    assigned_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_leads',
        verbose_name=_('Менеджер'),
    )
    hot_handoff_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Передан менеджеру'),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Создан'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Обновлён'))

    class Meta:
        verbose_name = _('Лид')
        verbose_name_plural = _('Лиды')
        ordering = ['-created_at']

    def __str__(self):
        name = (self.contact_info or {}).get('name') or (self.contact_info or {}).get('email')
        return name or f'Лид {self.id}'

    @property
    def contact_name(self):
        return (self.contact_info or {}).get('name', '')

    @property
    def contact_phone(self):
        return (self.contact_info or {}).get('phone', '')

    @property
    def contact_email(self):
        return (self.contact_info or {}).get('email', '')

    @property
    def contact_linkedin(self):
        return (self.contact_info or {}).get('linkedin', '')


class LeadStatusHistory(models.Model):
    """История изменений квалификации лида."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name='status_history',
        verbose_name=_('Лид'),
    )
    old_status = models.CharField(max_length=20, blank=True, verbose_name=_('Было'))
    new_status = models.CharField(max_length=20, verbose_name=_('Стало'))
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='lead_status_changes',
        verbose_name=_('Кто изменил'),
    )
    comment = models.TextField(blank=True, verbose_name=_('Комментарий'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Когда'))

    class Meta:
        verbose_name = _('История статуса лида')
        verbose_name_plural = _('История статусов лидов')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.old_status} → {self.new_status}'


class FreelancerAccrual(models.Model):
    """Журнал начислений фрилансеру (демо-заглушка, не кошелёк)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    freelancer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='accruals',
        verbose_name=_('Фрилансер'),
    )
    project = models.ForeignKey(
        'rooms.Project',
        on_delete=models.CASCADE,
        related_name='freelancer_accruals',
        verbose_name=_('Проект'),
    )
    report = models.OneToOneField(
        Report,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='accrual',
        verbose_name=_('Отчёт'),
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name=_('Сумма (USD)'),
    )
    title = models.CharField(max_length=255, verbose_name=_('Основание'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Начислено'))

    class Meta:
        verbose_name = _('Начисление фрилансеру')
        verbose_name_plural = _('Начисления фрилансерам')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} — {self.amount}'

    def clean(self):
        if self.amount is not None and self.amount < 0:
            raise ValidationError({'amount': _('Сумма не может быть отрицательной.')})
