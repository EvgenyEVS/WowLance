from django.contrib import admin

from .models import FreelancerProfile, Portfolio, PortfolioItem


class PortfolioItemInline(admin.TabularInline):
    model = PortfolioItem
    extra = 0
    fields = ['item_type', 'title', 'file', 'url', 'sort_order', 'is_public']


class PortfolioInline(admin.StackedInline):
    model = Portfolio
    extra = 0
    fields = ['title', 'description', 'is_public']


@admin.register(FreelancerProfile)
class FreelancerProfileAdmin(admin.ModelAdmin):
    list_display = [
        'full_name', 'user', 'level', 'country', 'rating',
        'acceptance_rate', 'is_verified', 'is_available',
    ]
    list_filter = ['level', 'is_verified', 'is_available', 'country']
    search_fields = ['user__email', 'user__first_name', 'user__last_name']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [PortfolioInline]
    fieldsets = (
        ('Пользователь', {'fields': ('user',)}),
        ('Профиль', {'fields': ('country', 'level', 'experience_years', 'experience_projects')}),
        ('Навыки', {'fields': ('skills', 'key_advantages', 'languages', 'portfolio_links')}),
        ('Ссылки', {'fields': ('avatar_url', 'linkedin_url', 'video_url')}),
        ('Метрики', {'fields': ('rating', 'acceptance_rate', 'is_verified', 'is_available')}),
        ('Системные', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    def full_name(self, obj):
        return obj.full_name
    full_name.short_description = 'Имя'


@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = ['profile', 'title', 'is_public', 'updated_at']
    list_filter = ['is_public']
    search_fields = ['profile__user__email', 'title']
    inlines = [PortfolioItemInline]


@admin.register(PortfolioItem)
class PortfolioItemAdmin(admin.ModelAdmin):
    list_display = ['title', 'portfolio', 'item_type', 'is_public', 'created_at']
    list_filter = ['item_type', 'is_public']
    search_fields = ['title', 'portfolio__profile__user__email']
