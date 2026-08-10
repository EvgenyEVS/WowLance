from django.urls import path

from . import views

app_name = 'profiles'

urlpatterns = [
    path('freelancers/', views.freelancer_catalog, name='catalog'),
    path('freelancer/<uuid:user_id>/', views.profile_detail, name='detail'),
    path('freelancer/<uuid:user_id>/portfolio/', views.portfolio_detail, name='portfolio'),
    path('profile/edit/', views.profile_edit, name='edit'),
    path('profile/portfolio/upload/', views.portfolio_upload, name='portfolio_upload'),
    path('profile/portfolio/add-link/', views.portfolio_add_link, name='portfolio_add_link'),
    path('profile/portfolio/delete/<uuid:item_id>/', views.portfolio_delete, name='portfolio_delete'),
    path('profile/add-skill/', views.add_skill, name='add_skill'),
]
