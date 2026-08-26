"""Демо-аккаунты ролей для локального стенда и VPS.

Идемпотентно по email. Пароль по умолчанию: DemoPass123!
Не трогает UI. Фрилансеров каталога создаёт seed_freelancers.
"""

from django.core.management.base import BaseCommand

from apps.profiles.services import get_or_create_freelancer_profile
from apps.users.models import User

DEFAULT_PASSWORD = 'DemoPass123!'

SEED_ACCOUNTS = [
    {
        'email': 'director@wowlance.demo',
        'role': User.Roles.DIRECTOR,
        'first_name': 'Дмитрий',
        'last_name': 'Директоров',
    },
    {
        'email': 'teamlead@wowlance.demo',
        'role': User.Roles.TEAMLEAD,
        'first_name': 'Татьяна',
        'last_name': 'Тимлидова',
    },
    {
        'email': 'artem.nesterov@wowlance.demo',
        'role': User.Roles.TEAMLEAD,
        'first_name': 'Артём',
        'last_name': 'Нестеров',
    },
    {
        'email': 'manager@wowlance.demo',
        'role': User.Roles.MANAGER,
        'first_name': 'Марина',
        'last_name': 'Менеджерова',
    },
]


class Command(BaseCommand):
    help = 'Создаёт демо директора / тимлидов / менеджера (идемпотентно).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--password',
            default=DEFAULT_PASSWORD,
            help=f'Пароль для всех демо-аккаунтов (по умолчанию {DEFAULT_PASSWORD})',
        )

    def handle(self, *args, **options):
        password = options['password']
        created = updated = 0

        for data in SEED_ACCOUNTS:
            email = data['email']
            user, was_created = User.objects.get_or_create(
                email=email,
                defaults={
                    'username': email,
                    'first_name': data['first_name'],
                    'last_name': data['last_name'],
                    'role': data['role'],
                    'status': User.Status.ACTIVE,
                    'is_email_verified': True,
                },
            )
            user.first_name = data['first_name']
            user.last_name = data['last_name']
            user.role = data['role']
            user.status = User.Status.ACTIVE
            user.is_email_verified = True
            user.set_password(password)
            user.save()

            if user.role == User.Roles.TEAMLEAD:
                get_or_create_freelancer_profile(user)

            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f'+ {email} ({data["role"]})'))
            else:
                updated += 1
                self.stdout.write(f'~ обновлён {email} ({data["role"]})')

        self.stdout.write(self.style.SUCCESS(
            f'Готово: создано {created}, обновлено {updated}. Пароль: {password}'
        ))
        self.stdout.write(
            'Каталог фрилансеров: python manage.py seed_freelancers'
        )
