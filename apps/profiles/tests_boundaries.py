"""Границы модулей: BIZ (`apps.profiles`) не зависит от ROOM.

См. docs/ADR-001-monolith-modules.md — `profiles`/`users` не импортируют
`rooms`/`pipeline`. Добавление фрилансера в комнату инициируется формой,
action которой ведёт на URL в `apps.rooms`, а форму выбора проекта шаблонам
отдаёт context processor модуля ROOM.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase

PROFILES_DIR = Path(__file__).resolve().parent
FORBIDDEN_MODULES = ('apps.rooms', 'apps.pipeline')


def _imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


class ProfilesModuleBoundaryTests(SimpleTestCase):
    def test_profiles_does_not_import_room_module(self):
        offenders = []
        for path in sorted(PROFILES_DIR.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            for module in _imported_modules(path):
                if any(
                    module == forbidden or module.startswith(forbidden + '.')
                    for forbidden in FORBIDDEN_MODULES
                ):
                    offenders.append(f'{path.relative_to(PROFILES_DIR)}: {module}')

        self.assertEqual(
            offenders,
            [],
            'BIZ-модуль profiles не должен импортировать ROOM-модули: '
            + ', '.join(offenders),
        )
