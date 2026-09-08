"""Inclusion tag колокольчика: SSR-бейдж без отдельного context processor."""

from django import template

from apps.rooms.chat_notifications import build_bell_context, user_can_see_chat_bell

register = template.Library()


_EMPTY_BELL = {
    'show_chat_bell': False,
    'conversations': [],
    'conversation_count': 0,
    'toasts': [],
    'fallback_href': None,
    'poll_query': '',
}


@register.inclusion_tag('rooms/_header_chat_bell.html', takes_context=True)
def header_chat_bell(context):
    # handler500 и часть тестовых render() идут без RequestContext —
    # колокольчик не должен ронять страницу поверх исходной ошибки.
    request = context.get('request')
    if request is None:
        return dict(_EMPTY_BELL)
    user = getattr(request, 'user', None)
    if not user_can_see_chat_bell(user):
        return dict(_EMPTY_BELL)
    project = context.get('project')
    viewing_project_id = str(project.id) if project is not None else None
    viewing_comms = context.get('active_tab') == 'comms'
    return build_bell_context(
        request,
        emit_toasts=False,
        viewing_project_id=viewing_project_id,
        viewing_comms=viewing_comms,
    )
