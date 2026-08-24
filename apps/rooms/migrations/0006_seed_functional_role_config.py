"""Начальные бизнес-значения пяти функций каталога (Issue #11).

Цифры утверждены руководителем для MVP. Дальше их правит администратор
через Django admin, без релиза, поэтому миграция создаёт строки, но
**не переписывает** уже существующие: повторный прогон (или прогон на базе,
где значения уже поправили) не должен откатывать решения администратора.
Отсюда `get_or_create`, а не `update_or_create`.

Значения продублированы здесь литералами, а не импортированы из
`apps.rooms.functional_roles`: миграция — снимок состояния на момент
релиза, и она обязана давать один и тот же результат даже после того, как
каталог в коде изменится. Структурные поля (`grade`, `channel`, `is_fixed`)
в БД не попадают вовсе — они живут только в коде.

Ручного SQL нет, `RunPython` работает через ORM и одинаково ведёт себя на
SQLite и PostgreSQL.

Обратная миграция удаляет ровно эти пять `role_key` — и только их, чтобы
откат не унёс строки, заведённые чем-то ещё. Удаление идёт через queryset:
`FunctionalRoleConfig.delete()` в рабочем коде запрещён, но исторические
модели миграций пользовательских методов не имеют, а `QuerySet.delete()`
их и не вызывает.
"""

from django.db import migrations

#: role_key → (monthly_cost, monthly_hours, productivity_text, hot_leads_per_month)
SEED = [
    ('teamlead', '35000.00', 80, 'Стратегия, контроль SLA', 0),
    ('seller_middle', '62000.00', 160, '60 звонков / день', 10),
    ('seller_senior', '85000.00', 160, '80 звонков / день', 15),
    ('linkedin_leadgen', '48000.00', 160, '40 касаний / день', 8),
    ('database_assistant', '28000.00', 80, 'База, CRM, разметка', 0),
]


def seed_functional_roles(apps, schema_editor):
    FunctionalRoleConfig = apps.get_model('rooms', 'FunctionalRoleConfig')
    for role_key, monthly_cost, monthly_hours, productivity_text, hot in SEED:
        FunctionalRoleConfig.objects.get_or_create(
            role_key=role_key,
            defaults={
                'monthly_cost': monthly_cost,
                'monthly_hours': monthly_hours,
                'productivity_text': productivity_text,
                'hot_leads_per_month': hot,
            },
        )


def unseed_functional_roles(apps, schema_editor):
    FunctionalRoleConfig = apps.get_model('rooms', 'FunctionalRoleConfig')
    FunctionalRoleConfig.objects.filter(
        role_key__in=[row[0] for row in SEED]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('rooms', '0005_functionalroleconfig'),
    ]

    operations = [
        migrations.RunPython(seed_functional_roles, unseed_functional_roles),
    ]
