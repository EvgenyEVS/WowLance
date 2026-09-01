from django.contrib import admin

from .models import FreelancerAccrual, Lead, LeadStatusHistory, Report, Task


class ReportInline(admin.TabularInline):
    model = Report
    extra = 0
    readonly_fields = ['created_at', 'reviewed_at']


class LeadHistoryInline(admin.TabularInline):
    model = LeadStatusHistory
    extra = 0
    readonly_fields = ['created_at']


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'project', 'assignee', 'status', 'task_type',
        'report_required', 'deadline',
    ]
    list_filter = ['status', 'task_type', 'report_required']
    search_fields = ['title', 'project__name', 'assignee__email']
    autocomplete_fields = ['project', 'assignee', 'created_by', 'lead']
    inlines = [ReportInline]


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ['task', 'author', 'review_status', 'reviewed_by', 'created_at']
    list_filter = ['review_status']
    search_fields = ['task__title', 'author__email']
    autocomplete_fields = ['task', 'author', 'reviewed_by']


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'project', 'creator', 'qualification_status',
        'assigned_manager', 'hot_handoff_at', 'created_at',
    ]
    list_filter = ['qualification_status', 'source']
    search_fields = ['project__name', 'creator__email']
    autocomplete_fields = ['project', 'creator', 'assigned_manager']
    inlines = [LeadHistoryInline]


@admin.register(LeadStatusHistory)
class LeadStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ['lead', 'old_status', 'new_status', 'changed_by', 'created_at']
    list_filter = ['new_status']


@admin.register(FreelancerAccrual)
class FreelancerAccrualAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'freelancer', 'project', 'amount', 'created_at',
    ]
    list_filter = ['created_at']
    search_fields = ['title', 'freelancer__email', 'project__name']
    autocomplete_fields = ['freelancer', 'project', 'report']
    readonly_fields = ['created_at']
