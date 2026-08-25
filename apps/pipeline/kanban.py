"""Колонки канбанов ROOM: задачи и лиды. Только presentation.

Единственный источник определения колонок. Раньше `_kanban_columns`
существовал двумя одинаковыми копиями — в `apps.pipeline.views` и в
`apps.rooms.views`, — и «Обзор» с вкладкой «Задачи» могли разойтись при
первой же правке. Теперь обе страницы раскладывают задачи одной функцией.

Границы модуля
--------------

* Статусы (`Task.Status`, `Lead.Qualification`) здесь **не заводятся и не
  меняются**: модуль только группирует объекты по уже существующим
  значениям enum. Никакой миграции за этим файлом не стоит.
* Бизнес-правил перехода статусов тут нет: их держат
  `apps.pipeline.services` (`start_task`, `submit_report`, `review_report`,
  `close_task`, `set_lead_qualification`). Раскладка по колонкам ничего не
  разрешает и не запрещает.
* Записей в БД нет вообще — на вход приходит уже готовый список.

Почему `REJECTED` попадает в «К работе»
---------------------------------------

`start_task` берёт в работу задачу из `NEW` **и** `REJECTED`: отклонённый
отчёт возвращает задачу исполнителю. Значит, отклонённая задача — это
работа, которую предстоит сделать, а не результат. Прежняя раскладка
относила её туда же (всё, что не «на проверке» и не «готово», уходило в
«К работе»), и семантика здесь не меняется.
"""

from .models import Lead, Task

__all__ = [
    'LEAD_COLUMNS',
    'TASK_COLUMNS',
    'lead_columns',
    'task_columns',
]


#: Четыре колонки доски задач в продуктовом порядке (Issue #11).
#:
#: `IN_PROGRESS` получил собственную колонку «В работе»: раньше он схлопывался
#: с «К работе», и доска не отличала невзятую задачу от уже начатой. Остальные
#: статусы сохраняют прежний смысл: `READY_FOR_REVIEW` — «На проверке»,
#: `APPROVED` и `CLOSED` — «Готово».
#:
#: Каждый статус упомянут ровно один раз: раскладка обязана быть разбиением,
#: иначе одна задача показалась бы в двух колонках.
TASK_COLUMNS: tuple[dict, ...] = (
    {
        'key': 'todo',
        'title': 'К работе',
        'statuses': frozenset({Task.Status.NEW, Task.Status.REJECTED}),
    },
    {
        'key': 'in_progress',
        'title': 'В работе',
        'statuses': frozenset({Task.Status.IN_PROGRESS}),
    },
    {
        'key': 'review',
        'title': 'На проверке',
        'statuses': frozenset({Task.Status.READY_FOR_REVIEW}),
    },
    {
        'key': 'done',
        'title': 'Готово',
        'statuses': frozenset({Task.Status.APPROVED, Task.Status.CLOSED}),
    },
)

#: Колонки доски лидов: существующий `Lead.Qualification`, без новых статусов.
LEAD_COLUMNS: tuple[dict, ...] = (
    {
        'key': 'cold',
        'title': 'Cold',
        'statuses': frozenset({Lead.Qualification.COLD}),
    },
    {
        'key': 'warm',
        'title': 'Warm',
        'statuses': frozenset({Lead.Qualification.WARM}),
    },
    {
        'key': 'hot',
        'title': 'Hot',
        'statuses': frozenset({Lead.Qualification.HOT}),
    },
)


def _split(items, columns, status_of, fallback_key):
    """Раскладывает объекты по колонкам их статусом. Порядок входа сохраняется.

    Статус, которого нет ни в одной колонке (историческая запись, будущий
    вариант enum), уходит в `fallback_key`, а не пропадает: потерять карточку
    хуже, чем показать её не в той колонке.
    """
    buckets = {column['key']: [] for column in columns}
    routes = {
        status: column['key']
        for column in columns
        for status in column['statuses']
    }
    for item in items:
        buckets[routes.get(status_of(item), fallback_key)].append(item)
    return [
        {'key': column['key'], 'title': column['title'], 'items': buckets[column['key']]}
        for column in columns
    ]


def task_columns(tasks) -> list[dict]:
    """Задачи, разложенные по четырём колонкам доски.

    Ключ `tasks` в результате оставлен для обратной совместимости с уже
    написанной разметкой доски; `items` — то же самое под общим именем.
    """
    columns = _split(tasks, TASK_COLUMNS, lambda task: task.status, 'todo')
    for column in columns:
        column['tasks'] = column['items']
    return columns


def lead_columns(leads) -> list[dict]:
    """Лиды, разложенные по Cold / Warm / Hot."""
    columns = _split(
        leads, LEAD_COLUMNS, lambda lead: lead.qualification_status, 'cold',
    )
    for column in columns:
        column['leads'] = column['items']
    return columns
