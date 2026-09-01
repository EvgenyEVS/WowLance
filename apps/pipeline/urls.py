from django.urls import path

from . import views

app_name = 'pipeline'

urlpatterns = [
    path(
        'projects/<uuid:project_id>/room/tasks/',
        views.room_tasks,
        name='room_tasks',
    ),
    path(
        'projects/<uuid:project_id>/room/tasks/create/',
        views.task_create,
        name='task_create',
    ),
    path(
        'projects/<uuid:project_id>/room/tasks/<uuid:task_id>/',
        views.task_detail,
        name='task_detail',
    ),
    path(
        'projects/<uuid:project_id>/room/tasks/<uuid:task_id>/start/',
        views.task_start,
        name='task_start',
    ),
    path(
        'projects/<uuid:project_id>/room/tasks/<uuid:task_id>/report/',
        views.task_submit_report,
        name='task_submit_report',
    ),
    path(
        'projects/<uuid:project_id>/room/tasks/<uuid:task_id>/reports/<uuid:report_id>/review/',
        views.task_review_report,
        name='task_review_report',
    ),
    path(
        'projects/<uuid:project_id>/room/tasks/<uuid:task_id>/close/',
        views.task_close,
        name='task_close',
    ),
    path(
        'projects/<uuid:project_id>/room/leads/',
        views.room_leads,
        name='room_leads',
    ),
    path(
        'projects/<uuid:project_id>/room/leads/create/',
        views.lead_create,
        name='lead_create',
    ),
    path(
        'projects/<uuid:project_id>/room/leads/<uuid:lead_id>/',
        views.lead_detail,
        name='lead_detail',
    ),
    path(
        'projects/<uuid:project_id>/room/leads/<uuid:lead_id>/qualify/',
        views.lead_qualify,
        name='lead_qualify',
    ),
    path('manager/inbox/', views.manager_inbox, name='manager_inbox'),
    path(
        'teamlead/report/',
        views.teamlead_report,
        name='teamlead_report',
    ),
]
