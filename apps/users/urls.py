from django.urls import path
from . import views

app_name = 'users'

urlpatterns = [
    path('register/', views.register, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('resend-activation/', views.resend_activation, name='resend_activation'),
    path('activate/<str:uidb64>/<str:token>/', views.activate, name='activate'),
]