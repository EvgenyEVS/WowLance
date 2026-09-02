"""Границы модулей (ADR-001): два стража графа импортов.

* BIZ не зависит от ROOM — `apps.profiles` не импортирует `apps.rooms` и
  `apps.pipeline`. Добавление фрилансера в комнату инициируется формой,
  action которой ведёт на URL в `apps.rooms`, а форму выбора проекта
  шаблонам отдаёт context processor модуля ROOM.
* Подбор не зависит от pipeline — `apps.rooms.staffing` не импортирует
  `apps.pipeline`: задачи и лиды живут за фасадом ROOM.

Анализируется ТОЛЬКО список импортов (`ast.Import` / `ast.ImportFrom`).
Тела функций, исходники и docstring'и не разбираются: границы — это граф
зависимостей, а не текст модуля.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase

APPS_DIR = Path(__file__).resolve().parent.parent
PROFILES_DIR = APPS_DIR / 'profiles'
STAFFING_DIR = APPS_DIR / 'rooms' / 'staffing'


def _imported_modules(path: Path):
    """Абсолютные модули, импортируемые файлом. Только верхний уровень AST."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def _offenders(package_dir: Path, forbidden):
    result = []
    for path in sorted(package_dir.rglob('*.py')):
        if '__pycache__' in path.parts:
            continue
        for module in _imported_modules(path):
            if any(
                module == name or module.startswith(name + '.')
                for name in forbidden
            ):
                result.append(f'{path.relative_to(APPS_DIR)}: {module}')
    return result


class ModuleBoundaryTests(SimpleTestCase):
    def test_profiles_does_not_import_room_modules(self):
        offenders = _offenders(PROFILES_DIR, ('apps.rooms', 'apps.pipeline'))

        self.assertEqual(
            offenders,
            [],
            'BIZ-модуль profiles не должен импортировать ROOM-модули: '
            + ', '.join(offenders),
        )

    def test_staffing_does_not_import_pipeline(self):
        offenders = _offenders(STAFFING_DIR, ('apps.pipeline',))

        self.assertEqual(
            offenders,
            [],
            'Подбор не должен зависеть от pipeline: ' + ', '.join(offenders),
        )
