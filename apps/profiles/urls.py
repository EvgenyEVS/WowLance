from django.urls import path
from . import views

app_name = 'profiles'

urlpatterns = [
    # Публичная карточка фрилансера
    path('freelancer/<int:user_id>/', views.profile_detail, name='detail'),

    # Редактирование своего профиля
    path('profile/edit/', views.profile_edit, name='edit'),

    # Загрузка и удаление портфолио
    path('profile/portfolio/upload/', views.portfolio_upload, name='portfolio_upload'),
    path('profile/portfolio/delete/<int:file_id>/', views.portfolio_delete, name='portfolio_delete'),

    # HTMX-эндпоинты
    path('profile/add-skill/', views.add_skill, name='add_skill'),
]