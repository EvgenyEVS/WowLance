"""Заполняет БД выдуманными анкетами фрилансеров."""

from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.profiles.models import FreelancerProfile, Portfolio
from apps.profiles.services import get_or_create_freelancer_profile
from apps.users.models import User

SEED_FREELANCERS = [
    {
        'email': 'anna.sokolova@wowlance.demo',
        'first_name': 'Анна',
        'last_name': 'Соколова',
        'phone': '+79031234567',
        'country': 'Россия',
        'level': FreelancerProfile.Level.SENIOR,
        'experience_years': 8,
        'experience_projects': 42,
        'languages': [
            {'language': 'Русский', 'level': 'Native'},
            {'language': 'Английский', 'level': 'C1'},
        ],
        'key_advantages': [
            'конверсия в демо 28%',
            'B2B SaaS 5+ лет',
            'закрыла 120+ сделок',
        ],
        'skills': ['Холодные звонки', 'SPIN', 'MEDDIC', 'Salesforce', 'Discovery'],
        'portfolio_links': ['https://linkedin.com/in/anna-sokolova-demo'],
        'linkedin_url': 'https://linkedin.com/in/anna-sokolova-demo',
        'video_url': 'https://youtube.com/watch?v=demo-anna',
        'rating': Decimal('4.85'),
        'acceptance_rate': Decimal('96.50'),
        'is_verified': True,
        'is_available': True,
    },
    {
        'email': 'ivan.petrov@wowlance.demo',
        'first_name': 'Иван',
        'last_name': 'Петров',
        'phone': '+79035551234',
        'country': 'Россия',
        'level': FreelancerProfile.Level.MIDDLE,
        'experience_years': 4,
        'experience_projects': 18,
        'languages': [
            {'language': 'Русский', 'level': 'Native'},
            {'language': 'Английский', 'level': 'B2'},
        ],
        'key_advantages': [
            'сильный outbound LinkedIn',
            'среднее 40 касаний/день',
        ],
        'skills': ['LinkedIn Outreach', 'Холодные письма', 'HubSpot', 'AIDA'],
        'portfolio_links': ['https://linkedin.com/in/ivan-petrov-demo'],
        'linkedin_url': 'https://linkedin.com/in/ivan-petrov-demo',
        'video_url': '',
        'rating': Decimal('4.40'),
        'acceptance_rate': Decimal('88.00'),
        'is_verified': True,
        'is_available': True,
    },
    {
        'email': 'maria.kim@wowlance.demo',
        'first_name': 'Мария',
        'last_name': 'Ким',
        'phone': '+77011234567',
        'country': 'Казахстан',
        'level': FreelancerProfile.Level.JUNIOR,
        'experience_years': 1,
        'experience_projects': 5,
        'languages': [
            {'language': 'Русский', 'level': 'Native'},
            {'language': 'Казахский', 'level': 'Native'},
            {'language': 'Английский', 'level': 'B1'},
        ],
        'key_advantages': [
            'быстрый онбординг',
            'высокая дисциплина отчётов',
        ],
        'skills': ['Холодные звонки', 'Скрипты', 'Битрикс24'],
        'portfolio_links': [],
        'linkedin_url': 'https://linkedin.com/in/maria-kim-demo',
        'video_url': '',
        'rating': Decimal('3.90'),
        'acceptance_rate': Decimal('91.00'),
        'is_verified': False,
        'is_available': True,
    },
    {
        'email': 'dmitry.volkov@wowlance.demo',
        'first_name': 'Дмитрий',
        'last_name': 'Волков',
        'phone': '+375291112233',
        'country': 'Беларусь',
        'level': FreelancerProfile.Level.SENIOR,
        'experience_years': 10,
        'experience_projects': 67,
        'languages': [
            {'language': 'Русский', 'level': 'Native'},
            {'language': 'Английский', 'level': 'C1'},
            {'language': 'Немецкий', 'level': 'B1'},
        ],
        'key_advantages': [
            'enterprise продажи',
            'цикл сделки 3–6 мес',
            'средний чек от 2 млн ₽',
        ],
        'skills': ['Complex Sales', 'Challenger Sale', 'Negotiation', 'CRM', 'Account Based'],
        'portfolio_links': [
            'https://linkedin.com/in/dmitry-volkov-demo',
            'https://drive.google.com/demo-cases-volkov',
        ],
        'linkedin_url': 'https://linkedin.com/in/dmitry-volkov-demo',
        'video_url': 'https://vimeo.com/123456789',
        'rating': Decimal('4.95'),
        'acceptance_rate': Decimal('98.00'),
        'is_verified': True,
        'is_available': False,
    },
    {
        'email': 'elena.morozova@wowlance.demo',
        'first_name': 'Елена',
        'last_name': 'Морозова',
        'phone': '+79061112233',
        'country': 'Россия',
        'level': FreelancerProfile.Level.MIDDLE,
        'experience_years': 3,
        'experience_projects': 14,
        'languages': [
            {'language': 'Русский', 'level': 'Native'},
            {'language': 'Английский', 'level': 'B2'},
        ],
        'key_advantages': [
            'работа с возражениями',
            'стабильный pipeline',
        ],
        'skills': ['Холодные звонки', 'База возражений', 'SPIN', 'AmoCRM'],
        'portfolio_links': ['https://linkedin.com/in/elena-morozova-demo'],
        'linkedin_url': 'https://linkedin.com/in/elena-morozova-demo',
        'video_url': '',
        'rating': Decimal('4.20'),
        'acceptance_rate': Decimal('85.50'),
        'is_verified': True,
        'is_available': True,
    },
    {
        'email': 'alex.brown@wowlance.demo',
        'first_name': 'Alex',
        'last_name': 'Brown',
        'phone': '+447700900123',
        'country': 'Великобритания',
        'level': FreelancerProfile.Level.SENIOR,
        'experience_years': 7,
        'experience_projects': 35,
        'languages': [
            {'language': 'English', 'level': 'Native'},
            {'language': 'Russian', 'level': 'B2'},
        ],
        'key_advantages': [
            'international SDR',
            'LinkedIn + email sequences',
            'SaaS PLG experience',
        ],
        'skills': ['LinkedIn Outreach', 'Apollo', 'Outreach.io', 'Cold Email', 'Salesforce'],
        'portfolio_links': ['https://linkedin.com/in/alex-brown-demo'],
        'linkedin_url': 'https://linkedin.com/in/alex-brown-demo',
        'video_url': 'https://youtube.com/watch?v=demo-alex',
        'rating': Decimal('4.70'),
        'acceptance_rate': Decimal('93.00'),
        'is_verified': True,
        'is_available': True,
    },
    {
        'email': 'olga.novikova@wowlance.demo',
        'first_name': 'Ольга',
        'last_name': 'Новикова',
        'phone': '+79020010020',
        'country': 'Россия',
        'level': FreelancerProfile.Level.JUNIOR,
        'experience_years': 2,
        'experience_projects': 8,
        'languages': [
            {'language': 'Русский', 'level': 'Native'},
            {'language': 'Английский', 'level': 'A2'},
        ],
        'key_advantages': [
            'хорошо держит скрипт',
            'много энергии на звонках',
        ],
        'skills': ['Холодные звонки', 'Квалификация BANT', 'Google Sheets'],
        'portfolio_links': [],
        'linkedin_url': '',
        'video_url': '',
        'rating': Decimal('3.60'),
        'acceptance_rate': Decimal('79.00'),
        'is_verified': False,
        'is_available': True,
    },
    {
        'email': 'sergey.ivanov@wowlance.demo',
        'first_name': 'Сергей',
        'last_name': 'Иванов',
        'phone': '+79093334455',
        'country': 'Россия',
        'level': FreelancerProfile.Level.MIDDLE,
        'experience_years': 5,
        'experience_projects': 25,
        'languages': [
            {'language': 'Русский', 'level': 'Native'},
            {'language': 'Английский', 'level': 'B1'},
        ],
        'key_advantages': [
            'продажи IT-услуг',
            'сильный follow-up',
            'конверсия 22%',
        ],
        'skills': ['Холодные звонки', 'LinkedIn Outreach', 'SPIN', 'ZoomInfo', 'Pipedrive'],
        'portfolio_links': [
            'https://linkedin.com/in/sergey-ivanov-demo',
            'https://notion.so/demo-ivanov-cases',
        ],
        'linkedin_url': 'https://linkedin.com/in/sergey-ivanov-demo',
        'video_url': '',
        'rating': Decimal('4.55'),
        'acceptance_rate': Decimal('90.00'),
        'is_verified': True,
        'is_available': True,
    },
    {
        'email': 'natalia.orska@wowlance.demo',
        'first_name': 'Наталья',
        'last_name': 'Орская',
        'phone': '+48500111222',
        'country': 'Польша',
        'level': FreelancerProfile.Level.MIDDLE,
        'experience_years': 4,
        'experience_projects': 16,
        'languages': [
            {'language': 'Польский', 'level': 'Native'},
            {'language': 'Русский', 'level': 'C1'},
            {'language': 'Английский', 'level': 'B2'},
        ],
        'key_advantages': [
            'рынок PL/EU',
            'мультиязычный outreach',
        ],
        'skills': ['LinkedIn Outreach', 'Cold Email', 'HubSpot', 'Objection Handling'],
        'portfolio_links': ['https://linkedin.com/in/natalia-orska-demo'],
        'linkedin_url': 'https://linkedin.com/in/natalia-orska-demo',
        'video_url': 'https://youtube.com/watch?v=demo-natalia',
        'rating': Decimal('4.30'),
        'acceptance_rate': Decimal('87.00'),
        'is_verified': True,
        'is_available': False,
    },
    {
        'email': 'kirill.smirnov@wowlance.demo',
        'first_name': 'Кирилл',
        'last_name': 'Смирнов',
        'phone': '+79024445566',
        'country': 'Россия',
        'level': FreelancerProfile.Level.JUNIOR,
        'experience_years': 1,
        'experience_projects': 3,
        'languages': [
            {'language': 'Русский', 'level': 'Native'},
            {'language': 'Английский', 'level': 'B1'},
        ],
        'key_advantages': [
            'готов к высокой нагрузке',
            'учится быстро',
        ],
        'skills': ['Холодные звонки', 'База клиентов', 'Telegram outreach'],
        'portfolio_links': [],
        'linkedin_url': 'https://linkedin.com/in/kirill-smirnov-demo',
        'video_url': '',
        'rating': Decimal('3.40'),
        'acceptance_rate': Decimal('72.00'),
        'is_verified': False,
        'is_available': True,
    },
]


class Command(BaseCommand):
    help = 'Создаёт 10 демо-анкет фрилансеров (идемпотентно по email).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--password',
            default='DemoPass123!',
            help='Пароль для всех демо-аккаунтов (по умолчанию DemoPass123!)',
        )

    def handle(self, *args, **options):
        password = options['password']
        created_count = 0
        updated_count = 0

        for data in SEED_FREELANCERS:
            email = data['email']
            user, user_created = User.objects.get_or_create(
                email=email,
                defaults={
                    'username': email,
                    'first_name': data['first_name'],
                    'last_name': data['last_name'],
                    'phone': data.get('phone', ''),
                    'role': User.Roles.FREELANCER,
                    'status': User.Status.ACTIVE,
                    'is_email_verified': True,
                },
            )
            if user_created:
                user.set_password(password)
                user.save()
            else:
                user.first_name = data['first_name']
                user.last_name = data['last_name']
                user.phone = data.get('phone', '')
                user.role = User.Roles.FREELANCER
                user.status = User.Status.ACTIVE
                user.is_email_verified = True
                user.set_password(password)
                user.save()

            profile = get_or_create_freelancer_profile(user)
            for field in (
                'country', 'level', 'experience_years', 'experience_projects',
                'languages', 'key_advantages', 'skills', 'portfolio_links',
                'linkedin_url', 'video_url', 'rating', 'acceptance_rate',
                'is_verified', 'is_available',
            ):
                setattr(profile, field, data[field])
            profile.save()

            Portfolio.objects.update_or_create(
                profile=profile,
                defaults={
                    'title': f'Кейсы {data["first_name"]} {data["last_name"]}',
                    'description': 'Демо-портфолио для каталога WowLance',
                    'is_public': True,
                },
            )

            if user_created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f'+ {email}'))
            else:
                updated_count += 1
                self.stdout.write(f'~ обновлён {email}')

        self.stdout.write(self.style.SUCCESS(
            f'Готово: создано {created_count}, обновлено {updated_count}. '
            f'Пароль: {password}'
        ))
