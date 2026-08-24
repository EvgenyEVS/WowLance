from django.contrib import admin

from .models import (
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
