"""Django admin модуля BIZ.

Модерация фрилансеров здесь намеренно лёгкая: `is_verified` — обычный
булев флаг профиля, а не отдельная модель, статус-машина или роль
модератора. Кто вправе его менять, решает штатный доступ к Django admin
(`is_staff` + права на `profiles.change_freelancerprofile`), поэтому
собственной проверки ролей в действиях нет — второй, расходящейся копии
правил доступа появиться не должно.

Каталог и его бейдж «На модерации» живут в `apps.profiles.views` и
шаблонах и здесь не дублируются: admin меняет только сам флаг, а как
неверифицированный профиль показывается пользователю — вопрос каталога.
"""

from django.contrib import admin
from django.utils import timezone

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
    list_filter = [
        'level', 'is_verified', 'is_available', 'country',
        'does_cold_calling', 'does_linkedin_outreach',
    ]
    search_fields = ['user__email', 'user__first_name', 'user__last_name']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [PortfolioInline]
    fieldsets = (
        ('Пользователь', {'fields': ('user',)}),
        ('Профиль', {'fields': ('country', 'level', 'experience_years', 'experience_projects')}),
        ('Навыки', {'fields': ('skills', 'key_advantages', 'languages', 'portfolio_links')}),
        ('Каналы работы', {'fields': ('does_cold_calling', 'does_linkedin_outreach')}),
        ('Ссылки', {'fields': ('avatar_url', 'linkedin_url', 'video_url')}),
        ('Метрики', {'fields': ('rating', 'acceptance_rate', 'is_verified', 'is_available')}),
        ('Системные', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    #: Массовая модерация из списка профилей. Оба действия — зеркальные
    #: обёртки над `_set_verified`, чтобы правило «что именно пишется в БД»
    #: существовало в одном месте.
    actions = ['verify_profiles', 'unverify_profiles']

    def full_name(self, obj):
        return obj.full_name
    full_name.short_description = 'Имя'

    def _set_verified(self, queryset, value: bool) -> int:
        """Массово выставляет `is_verified` и сообщает, скольких коснулось.

        Один `UPDATE` на всю выборку вместо сохранения каждого объекта:
        модерация сотни профилей не должна превращаться в сотню запросов.
        Модель это допускает — на `FreelancerProfile` нет ни `save()` с
        побочными эффектами, ни signals (их в проекте нет вовсе), а
        `is_verified` ничего не пересчитывает.

        `updated_at` проставляется явно: `auto_now` работает только в
        `Model.save()`, а `queryset.update()` его не трогает — иначе
        «Обновлён» после модерации показывал бы неправду.
        """
        return queryset.update(is_verified=value, updated_at=timezone.now())

    @admin.action(description='Верифицировать')
    def verify_profiles(self, request, queryset):
        updated = self._set_verified(queryset, True)
        self.message_user(request, f'Верифицировано профилей: {updated}.')

    @admin.action(description='Снять верификацию')
    def unverify_profiles(self, request, queryset):
        updated = self._set_verified(queryset, False)
        self.message_user(request, f'Снята верификация с профилей: {updated}.')


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
