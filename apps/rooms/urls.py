from django.urls import path

from . import views

app_name = 'rooms'

urlpatterns = [
    path('projects/', views.project_list, name='project_list'),
    path('projects/create/', views.project_create, name='project_create'),
    path('projects/<uuid:project_id>/', views.project_detail, name='project_detail'),
    path('projects/<uuid:project_id>/launch/', views.project_launch, name='project_launch'),
    path('projects/<uuid:project_id>/room/', views.room_overview, name='room_overview'),
    path('projects/<uuid:project_id>/room/documents/', views.room_documents, name='room_documents'),
    path(
        'projects/<uuid:project_id>/room/documents/upload/',
        views.room_document_upload,
        name='room_document_upload',
    ),
    path(
        'projects/<uuid:project_id>/room/documents/<uuid:document_id>/delete/',
        views.room_document_delete,
        name='room_document_delete',
    ),
    path('projects/<uuid:project_id>/room/team/', views.room_team, name='room_team'),
    path(
        'projects/<uuid:project_id>/room/team/assign-teamlead/',
        views.room_assign_teamlead,
        name='room_assign_teamlead',
    ),
    path(
        'projects/<uuid:project_id>/room/team/add-freelancer/',
        views.room_add_freelancer,
        name='room_add_freelancer',
    ),
    path(
        'projects/<uuid:project_id>/room/team/<uuid:member_id>/remove/',
        views.room_remove_member,
        name='room_remove_member',
    ),
    path(
        'projects/<uuid:project_id>/room/ready/',
        views.room_confirm_ready,
        name='room_confirm_ready',
    ),
]
