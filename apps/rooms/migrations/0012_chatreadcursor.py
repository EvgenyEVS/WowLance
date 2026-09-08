# Generated manually for ChatReadCursor (header chat bell).

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rooms', '0011_freelancertermination_appeal_reason'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ChatReadCursor',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    'channel',
                    models.CharField(
                        choices=[
                            ('team', 'Команда'),
                            ('director_teamlead', 'Директор — тимлид'),
                        ],
                        max_length=32,
                        verbose_name='Канал',
                    ),
                ),
                (
                    'last_read_at',
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        verbose_name='Прочитано до',
                    ),
                ),
                (
                    'room',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='chat_read_cursors',
                        to='rooms.room',
                        verbose_name='Комната',
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='chat_read_cursors',
                        to=settings.AUTH_USER_MODEL,
                        verbose_name='Пользователь',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Курсор прочтения чата',
                'verbose_name_plural': 'Курсоры прочтения чата',
                'constraints': [
                    models.UniqueConstraint(
                        fields=('user', 'room', 'channel'),
                        name='unique_chat_read_cursor',
                    ),
                ],
            },
        ),
    ]
