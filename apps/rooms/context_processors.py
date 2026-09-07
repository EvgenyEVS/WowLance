# apps/rooms/context_processors.py

from django.utils import timezone
from .chat_notifications import get_unread_conversations


def chat_context(request):
    context = {}
    if request.user.is_authenticated:
        conversations = get_unread_conversations(request.user)
        context['chat_conversations'] = conversations
        context['chat_new_messages'] = []
    return context


def add_to_room(request):
    """
    Контекстный процессор для добавления данных в шаблоны комнат.
    """
    context = {}

    if request.user.is_authenticated and hasattr(request, 'resolver_match'):
        # Получаем project_id из URL если есть
        project_id = request.resolver_match.kwargs.get('project_id')
        if project_id:
            from .models import Project
            try:
                project = Project.objects.get(id=project_id)
                context['current_project'] = project
            except Project.DoesNotExist:
                pass

    return context