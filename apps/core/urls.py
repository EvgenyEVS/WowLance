from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('', views.home, name='home'),          # <-- ГЛАВНАЯ СТРАНИЦА
    path('about/', views.about, name='about'),  # <-- Страница "О проекте"
]