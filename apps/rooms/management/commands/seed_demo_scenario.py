"""Идемпотентный сквозной демо-сценарий для стейкхолдеров.

Создаёт (при необходимости) директора/тимлида/менеджера и фрилансеров,
затем проект в STAFFING с пакетом quick_start и назначенным тимлидом.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand

from apps.rooms.models import Project
from apps.rooms.services import (
    apply_package_and_sync_slots,
    assign_teamlead,
    handle_project_paid,
)
from apps.users.models import User

DEMO_PROJECT_NAME = 'Демо для стейкхолдеров'
DIRECTOR_EMAIL = 'director@wowlance.demo'
TEAMLEAD_EMAIL = 'teamlead@wowlance.demo'


class Command(BaseCommand):
    help = 'Готовит демо-проект со staffing и автоподбором слотов.'

    def handle(self, *args, **options):
        call_command('seed_demo_accounts')
        call_command('seed_freelancers')

        director = User.objects.filter(
            email=DIRECTOR_EMAIL, role=User.Roles.DIRECTOR,
        ).first()
        teamlead = User.objects.filter(
            email=TEAMLEAD_EMAIL, role=User.Roles.TEAMLEAD,
        ).first()
        if not director or not teamlead:
            self.stderr.write(self.style.ERROR(
                'Нет director@ / teamlead@ после сидов — прервано.'
            ))
            return

        project, created = Project.objects.get_or_create(
            owner=director,
            name=DEMO_PROJECT_NAME,
            defaults={
                'project_type': Project.Type.BASE,
                'seller_level': Project.SellerLevel.MIDDLE,
                'status': Project.Status.DRAFT,
                'input_data': {
                    'offer': 'Демо-оффер WowLance',
                    'utp': 'Быстрый запуск продаж',
                    'audience': 'B2B SMB',
                    'hot_criteria': 'Запросил демо',
                },
                'budget': 97000,
            },
        )

        if project.status == Project.Status.DRAFT:
            handle_project_paid(project, actor=director)
            project.refresh_from_db()

        assign_teamlead(project, teamlead, actor=director)
        result = apply_package_and_sync_slots(project, 'quick_start', director)

        self.stdout.write(self.style.SUCCESS(
            f'{"+ создан" if created else "~ обновлён"} проект «{project.name}» '
            f'({project.status}), id={project.id}'
        ))
        if result.unfilled_opened_slots:
            self.stdout.write(self.style.WARNING(
                f'Пустых слотов после автоподбора: {result.unfilled_opened_slots}. '
                'Проверьте verified/video у фрилансеров.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                'Слоты пакета заполнены автоподбором (или уже были заняты).'
            ))
        self.stdout.write(
            f'Логин директора: {DIRECTOR_EMAIL} / DemoPass123!\n'
            f'Логин тимлида: {TEAMLEAD_EMAIL} / DemoPass123!\n'
            f'Логин менеджера: manager@wowlance.demo / DemoPass123!'
        )
