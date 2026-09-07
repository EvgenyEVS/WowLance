from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def get_current_room_id(context):
    """Возвращает project_id текущей комнаты или пустую строку."""
    request = context.get('request')
    if not request or not request.resolver_match:
        return ''

    # Пробуем разные варианты ключей
    project_id = (
            request.resolver_match.kwargs.get('project_id')
            or request.resolver_match.kwargs.get('pk')
            or request.GET.get('project_id', '')
    )

    return str(project_id) if project_id else ''