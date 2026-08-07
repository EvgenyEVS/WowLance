from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('', views.about, name='home'),      # <-- Пустой путь = главная страница
    path('about/', views.about, name='about')
]