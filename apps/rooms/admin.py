from django.contrib import admin, messages

from .termination import (
    TerminationError,
    complete_termination,
    revoke_termination,
)
from .models import (
    FreelancerTermination,
    FunctionalRoleConfig,
    Project,
    Room,
    RoomActivity,
    RoomChatMessage,
    RoomDocument,
    RoomFunctionSlot,
    RoomMember,
    RoomSlotCandidate,
    TeamleadInvite,
)


class RoomMemberInline(admin.TabularInline):
    model = RoomMember
    extra = 0
    autocomplete_fields = ['user']


class RoomDocumentInline(admin.TabularInline):
    model = RoomDocument
    extra = 0
    readonly_fields = ['created_at']


class RoomInline(admin.StackedInline):
    model = Room
    extra = 0
    show_change_link = True


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'owner', 'project_type', 'seller_level',
        'status', 'teamlead', 'created_at',
    ]
    list_filter = ['status', 'project_type', 'seller_level']
    search_fields = ['name', 'owner__email']
    autocomplete_fields = ['owner', 'teamlead']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [RoomInline]


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ['project', 'chat_enabled', 'created_at']
    search_fields = ['project__name']
    inlines = [RoomMemberInline, RoomDocumentInline]


@admin.register(RoomMember)
class RoomMemberAdmin(admin.ModelAdmin):
    list_display = [
        'user', 'room', 'role_in_room', 'role_key',
        'function_slot', 'ready_status', 'joined_at',
    ]
    list_filter = ['role_in_room', 'ready_status', 'role_key']
    search_fields = ['user__email', 'room__project__name']
    autocomplete_fields = ['user', 'room']


@admin.register(RoomFunctionSlot)
class RoomFunctionSlotAdmin(admin.ModelAdmin):
    list_display = [
        'room', 'role_key', 'slot_index', 'required_level',
        'required_channel', 'is_active', 'assigned_member',
    ]
    list_filter = ['role_key', 'required_level', 'required_channel', 'is_active']
    search_fields = ['role_key', 'room__project__name']
    autocomplete_fields = ['room']
    readonly_fields = ['created_at', 'updated_at']

    @admin.display(description='Занят участником')
    def assigned_member(self, obj):
        return obj.assigned_member or '—'


@admin.register(RoomSlotCandidate)
class RoomSlotCandidateAdmin(admin.ModelAdmin):
    list_display = ['slot', 'candidate', 'outcome', 'actor', 'created_at', 'updated_at']
    list_filter = ['outcome']
    search_fields = ['candidate__email', 'slot__role_key', 'slot__room__project__name']
    autocomplete_fields = ['candidate', 'actor']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(RoomDocument)
class RoomDocumentAdmin(admin.ModelAdmin):
    list_display = ['title', 'room', 'uploaded_by', 'created_at']
    search_fields = ['title', 'room__project__name']
    autocomplete_fields = ['room', 'uploaded_by']


@admin.register(RoomActivity)
class RoomActivityAdmin(admin.ModelAdmin):
    list_display = ['message', 'event_type', 'room', 'actor', 'created_at']
    list_filter = ['event_type']
    search_fields = ['message', 'room__project__name']


@admin.register(TeamleadInvite)
class TeamleadInviteAdmin(admin.ModelAdmin):
    list_display = ['project', 'token', 'is_active', 'expires_at', 'accepted_by', 'created_at']
    list_filter = ['is_active']
    search_fields = ['project__name', 'token']


@admin.register(FunctionalRoleConfig)
class FunctionalRoleConfigAdmin(admin.ModelAdmin):
    """Каталог функций: администратор правит только бизнес-значения.

    Что разрешено: стоимость, часы, текст продуктивности, Hot-лиды.

    Что запрещено и почему:

    * **добавление** — состав каталога структурный, шестая функция без
      грейда, канала и правил проекции сломала бы будущий подбор;
    * **удаление** — на роль ссылаются сохранённые составы проектов;
    * **смена `role_key` у существующей записи** — она бы переписала
      экономику проектов, ссылающихся на этот ключ.

    Структурные поля (`label`, `grade`, `channel`, `is_fixed`) показываются
    рядом read-only: администратору видно, что он правит, но не через что
    он это меняет — их источник истины в коде.

    Изменения применяются к проектам не сразу: экономика проекта живёт
    снапшотом и обновится при следующем явном сохранении состава
    (см. `apps.rooms.unit_economics`).
    """

    list_display = [
        'label', 'role_key', 'grade_display', 'channel_display', 'is_fixed',
        'monthly_cost', 'monthly_hours', 'hot_leads_per_month', 'updated_at',
    ]
    list_display_links = ['label', 'role_key']
    search_fields = ['role_key']
    ordering = ['role_key']
    fields = [
        'role_key',
        'label',
        'grade_display',
        'channel_display',
        'is_fixed',
        'monthly_cost',
        'monthly_hours',
        'productivity_text',
        'hot_leads_per_month',
        'updated_at',
    ]
    readonly_fields = [
        'label', 'grade_display', 'channel_display', 'is_fixed', 'updated_at',
    ]

    @admin.display(description='Название')
    def label(self, obj):
        return obj.label

    @admin.display(description='Грейд')
    def grade_display(self, obj):
        return obj.grade or 'N/A'

    @admin.display(description='Канал')
    def channel_display(self, obj):
        return obj.channel or '—'

    @admin.display(description='Обязательная', boolean=True)
    def is_fixed(self, obj):
        return obj.is_fixed

    def get_readonly_fields(self, request, obj=None):
        """`role_key` фиксируется сразу после создания записи."""
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            readonly.append('role_key')
        return readonly

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        """Убирает массовое удаление: оно не проходит через has_delete_permission
        объекта и обошло бы запрет выше."""
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(RoomChatMessage)
class RoomChatMessageAdmin(admin.ModelAdmin):
    """Минимальный просмотр переписки комнаты.

    Поиска по полному тексту сообщений нет намеренно: продуктовой модерации
    чата сейчас не существует, а полнотекстовый поиск по всей переписке — это
    отдельное решение о доступе к личным данным, а не деталь админки.
    Найти нужную комнату можно по проекту, автора — по email.
    """

    list_display = ['room', 'author', 'channel', 'short_text', 'created_at']
    list_filter = ['channel', 'created_at']
    search_fields = ['room__project__name', 'author__email']
    autocomplete_fields = ['room', 'author']
    readonly_fields = ['created_at']

    @admin.display(description='Текст')
    def short_text(self, obj):
        return obj.text[:80] + ('…' if len(obj.text) > 80 else '')


@admin.register(FreelancerTermination)
class FreelancerTerminationAdmin(admin.ModelAdmin):
    """Кейсы расторжения: карточка, на которую ведёт письмо о протесте.

    Регистрация нужна самому продукту, а не удобству: письмо поддержке
    содержит ссылку `admin:rooms_freelancertermination_change`, и без
    зарегистрированной модели этот адрес не существует — протест некуда
    было бы адресовать.

    Решение поддержки по протесту принимается здесь, двумя действиями списка,
    и **только** ими: отдельного support-интерфейса, публичных адресов и
    кнопок в комнате у этого сценария нет. Оба действия — обёртки над
    доменными операциями (`complete_termination` / `revoke_termination`);
    ни статус, ни членство админка руками не правит, иначе архивация
    участника и автомат кейса разъехались бы.

    `queryset.update()` в действиях не используется принципиально: он
    поменял бы статус мимо автомата, мимо блокировки строки и мимо
    архивации `RoomMember`.
    """

    list_display = [
        'freelancer', 'project', 'status', 'initiated_by',
        'initiated_at', 'deadline_at', 'appealed_at',
    ]
    list_filter = ['status']
    search_fields = ['freelancer__email', 'room__project__name']
    raw_id_fields = ['room', 'freelancer', 'member', 'initiated_by']
    readonly_fields = [
        'initiated_at', 'deadline_at', 'appealed_at', 'completed_at',
        'revoked_at',
    ]
    actions = ['uphold_termination', 'reject_termination']

    @admin.display(description='Проект', ordering='room__project__name')
    def project(self, obj):
        return obj.room.project.name

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'room__project', 'freelancer', 'initiated_by',
        )

    @admin.action(description='Оставить расторжение в силе (протест отклонён)')
    def uphold_termination(self, request, queryset):
        """Поддержка подтверждает расторжение после протеста.

        Обрабатываются только `appeal_pending`: у `notice_sent` срок ответа
        ещё идёт и решает его сам фрилансер, а терминальные статусы решены
        окончательно. Поддержка отвечает на протест, а не завершает
        расторжения вместо участников процесса.
        """
        self._decide(
            request,
            queryset,
            operation=lambda case: complete_termination(case, actor=request.user),
            done_label='Расторжение оставлено в силе',
        )

    @admin.action(description='Отклонить расторжение (протест удовлетворён)')
    def reject_termination(self, request, queryset):
        """Поддержка удовлетворяет протест: уведомление снимается.

        Кейс уходит в `revoked`, членство и слот не трогаются — до
        завершения они не архивировались, и человек просто продолжает
        работать. Завершённое расторжение этим действием не отменяется:
        реактивация архивного участника — отдельный сценарий повторного
        найма, а не решение по протесту.
        """
        self._decide(
            request,
            queryset,
            operation=revoke_termination,
            done_label='Расторжение отклонено',
        )

    def _decide(self, request, queryset, *, operation, done_label):
        """Общий прогон решения поддержки по выбранным кейсам.

        Строки обрабатываются по одной, а не пакетным `update()`: каждая
        проходит через доменную операцию, которая перечитывает кейс под
        блокировкой и проверяет автомат. Между отрисовкой списка и нажатием
        кнопки статус мог измениться — фрилансер ушёл сам, тимлид отозвал
        уведомление, — и такая строка обязана быть пропущена, а не уронить
        весь batch.

        `TerminationError` (в том числе `InvalidTerminationTransition`) —
        ожидаемый исход гонки и считается пропуском. Всё остальное не
        перехватывается: ошибка программы или базы должна быть видна, а не
        превращаться в «пропущено».
        """
        decided = 0
        skipped = 0
        for case in queryset:
            if case.status != FreelancerTermination.Status.APPEAL_PENDING:
                skipped += 1
                continue
            try:
                operation(case)
            except TerminationError:
                skipped += 1
                continue
            decided += 1

        if decided:
            self.message_user(
                request,
                f'{done_label}: {decided}.',
                messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request,
                f'Пропущено кейсов без действующего протеста: {skipped}. '
                'Решение поддержки применяется только к оспоренным '
                'расторжениям.',
                messages.WARNING,
            )
