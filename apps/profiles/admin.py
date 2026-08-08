from django.contrib import admin
from .models import FreelancerProfile, PortfolioFile


@admin.register(FreelancerProfile)
class FreelancerProfileAdmin(admin.ModelAdmin):
    list_display = [
        'full_name', 'user', 'country', 'rating',
        'is_verified', 'is_available', 'experience_years'
    ]
    list_filter = ['is_verified', 'is_available', 'country']
    search_fields = ['first_name', 'last_name', 'user__email']
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = (
        ('Основная информация', {
            'fields': ('user', 'first_name', 'last_name', 'country')
        }),
        ('Опыт', {
            'fields': ('experience_years', 'experience_projects')
        }),
        ('Навыки и преимущества', {
            'fields': ('skills', 'key_advantages', 'languages')
        }),
        ('Ссылки', {
            'fields': ('linkedin_url', 'video_url')
        }),
        ('Рейтинг и верификация', {
            'fields': ('rating', 'is_verified', 'is_available')
        }),
        ('Системные поля', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def full_name(self, obj):
        return obj.full_name
    full_name.short_description = 'Имя'


@admin.register(PortfolioFile)
class PortfolioFileAdmin(admin.ModelAdmin):
    list_display = ['file_name', 'user', 'uploaded_at', 'file_size']
    list_filter = ['uploaded_at']
    search_fields = ['file_name', 'user__email']