from django.contrib import admin

from .models import (
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

    list_display = ['room', 'author', 'short_text', 'created_at']
    list_filter = ['created_at']
    search_fields = ['room__project__name', 'author__email']
    autocomplete_fields = ['room', 'author']
    readonly_fields = ['created_at']

    @admin.display(description='Текст')
    def short_text(self, obj):
        return obj.text[:80] + ('…' if len(obj.text) > 80 else '')
