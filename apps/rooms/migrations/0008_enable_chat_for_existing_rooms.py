"""Включает чат у комнат, созданных до появления чата.

Схему миграция не трогает: `Room.chat_enabled` уже существует, nullable/default
у поля не меняются, backfill сообщений не нужен (таблица чата создаётся пустой
предыдущей миграцией 0007). Меняются только значения флага у существующих строк.

Почему data migration, а не новый default у поля: смена `default=False` на
`default=True` потребовала бы schema migration ради значения, которое всё равно
задаётся при создании комнаты в `services.ensure_room_for_project`. Так у поля
остаётся одна история изменений, а не две.

Обратная миграция намеренно ничего не делает: выключить чат обратно всем
комнатам означало бы затереть решения, принятые администратором в админке.
"""

from django.db import migrations


def enable_chat_for_existing_rooms(apps, schema_editor):
    Room = apps.get_model('rooms', 'Room')
    Room.objects.filter(chat_enabled=False).update(chat_enabled=True)


class Migration(migrations.Migration):

    dependencies = [
        ('rooms', '0007_roomchatmessage'),
    ]

    operations = [
        migrations.RunPython(
            enable_chat_for_existing_rooms,
            migrations.RunPython.noop,
        ),
    ]
