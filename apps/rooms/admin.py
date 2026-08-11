from django.contrib import admin

from .models import Project, Room, RoomActivity, RoomDocument, RoomMember, TeamleadInvite


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
    list_display = ['user', 'room', 'role_in_room', 'ready_status', 'joined_at']
    list_filter = ['role_in_room', 'ready_status']
    search_fields = ['user__email', 'room__project__name']
    autocomplete_fields = ['user', 'room']


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
