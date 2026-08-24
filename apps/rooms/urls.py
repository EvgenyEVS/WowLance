from django.urls import path

from . import views

app_name = 'rooms'

urlpatterns = [
    path('projects/', views.project_list, name='project_list'),
    path('projects/create/', views.project_create, name='project_create'),
    path('setup/', views.setup_wizard, name='setup_wizard'),
    path('apply-architecture/', views.apply_architecture, name='apply_architecture'),
    path('projects/<uuid:project_id>/', views.project_detail, name='project_detail'),
    path('projects/<uuid:project_id>/launch/', views.project_launch, name='project_launch'),
    path('projects/<uuid:project_id>/pay/', views.project_pay, name='project_pay'),
    path('projects/<uuid:project_id>/room/', views.room_overview, name='room_overview'),
    path(
        'projects/<uuid:project_id>/room/functional-roles/update/',
        views.room_functional_roles_update,
        name='room_functional_roles_update',
    ),
    path(
        'projects/<uuid:project_id>/room/functional-roles/apply-package/',
        views.room_functional_roles_apply_package,
        name='room_functional_roles_apply_package',
    ),
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
    path('projects/<uuid:project_id>/room/comms/', views.room_comms, name='room_comms'),
    path('projects/<uuid:project_id>/room/team/', views.room_team, name='room_team'),
    path(
        'projects/<uuid:project_id>/room/team/assign-teamlead/',
        views.room_assign_teamlead,
        name='room_assign_teamlead',
    ),
    path(
        'projects/<uuid:project_id>/room/team/invite-teamlead/',
        views.room_create_teamlead_invite,
        name='room_create_teamlead_invite',
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
        'projects/<uuid:project_id>/room/team/slots/<uuid:slot_id>/auto-assign/',
        views.room_slot_auto_assign,
        name='room_slot_auto_assign',
    ),
    path(
        'projects/<uuid:project_id>/room/team/slots/<uuid:slot_id>/replace/',
        views.room_slot_replace,
        name='room_slot_replace',
    ),
    path(
        'projects/<uuid:project_id>/room/team/slots/<uuid:slot_id>/candidates/',
        views.room_slot_candidates,
        name='room_slot_candidates',
    ),
    path(
        'projects/<uuid:project_id>/room/team/slots/<uuid:slot_id>/candidates/'
        '<uuid:candidate_id>/assign/',
        views.room_slot_assign_candidate,
        name='room_slot_assign_candidate',
    ),
    path(
        'projects/<uuid:project_id>/room/ready/',
        views.room_confirm_ready,
        name='room_confirm_ready',
    ),
    path(
        'catalog/<uuid:user_id>/add-to-room/',
        views.catalog_add_to_room,
        name='catalog_add_to_room',
    ),
    path(
        'invite/teamlead/<str:token>/',
        views.teamlead_invite_accept,
        name='teamlead_invite_accept',
    ),
]
