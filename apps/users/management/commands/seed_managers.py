"""Создаёт (или обновляет) демо-менеджера платформы."""
from django.core.management.base import BaseCommand
from apps.users.models import User


DEFAULT_EMAIL = 'manager@wowlance.demo'
DEFAULT_PASSWORD = 'DemoPass123!'


class Command(BaseCommand):
    help = 'Создаёт демо-менеджера платформы (идемпотентно по email).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            default=DEFAULT_EMAIL,
            help=f'Email менеджера (по умолчанию {DEFAULT_EMAIL})',
        )
        parser.add_argument(
            '--password',
            default=DEFAULT_PASSWORD,
            help=f'Пароль менеджера (по умолчанию {DEFAULT_PASSWORD})',
        )

    def handle(self, *args, **options):
        email = options['email']
        password = options['password']

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                'username': email,
                'first_name': 'Мария',
                'last_name': 'Менеджерова',
                'role': User.Roles.MANAGER,
                'status': User.Status.ACTIVE,
                'is_email_verified': True,
            },
        )

        # Обновляем поля в любом случае, чтобы демо было консистентным
        user.first_name = 'Мария'
        user.last_name = 'Менеджерова'
        user.role = User.Roles.MANAGER
        user.status = User.Status.ACTIVE
        user.is_email_verified = True
        user.set_password(password)
        user.save()

        if created:
            self.stdout.write(self.style.SUCCESS(f'+ менеджер создан: {email}'))
        else:
            self.stdout.write(self.style.SUCCESS(f'~ менеджер обновлён: {email}'))

        self.stdout.write(self.style.SUCCESS(
            f'Готово. Логин: {email}, пароль: {password}'
        ))