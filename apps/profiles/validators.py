from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
import os


def validate_file_extension(value):
    """
    Проверка расширения файла
    """
    ext = os.path.splitext(value.name)[1].lower()
    allowed_extensions = ['.pdf', '.png', '.jpg', '.jpeg', '.doc', '.docx']
    if ext not in allowed_extensions:
        raise ValidationError(
            _('Недопустимый формат файла. Разрешены: PDF, PNG, JPG, JPEG, DOC, DOCX.')
        )


def validate_file_size(value):
    """
    Проверка размера файла (максимум 10 МБ)
    """
    max_size = 10 * 1024 * 1024  # 10 МБ
    if value.size > max_size:
        raise ValidationError(
            _('Размер файла не должен превышать 10 МБ.')
        )


def validate_advantages(value):
    """
    Проверка количества преимуществ (максимум 3)
    """
    if not isinstance(value, list):
        raise ValidationError(_('Преимущества должны быть списком.'))
    if len(value) > 3:
        raise ValidationError(_('Можно указать не более 3 преимуществ.'))