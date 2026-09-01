"""Context processors модуля PIPELINE."""

from django.utils.functional import SimpleLazyObject

from apps.users.models import User


def _freelancer_earned_total(request):
    user = getattr(request, 'user', None)
    if (
        user is None
        or not getattr(user, 'is_authenticated', False)
        or getattr(user, 'role', None) != User.Roles.FREELANCER
    ):
        return 0
    from .accruals import earned_total_for

    return int(earned_total_for(user))


def freelancer_earnings(request):
    """``freelancer_earned_total`` для шапки сайта — только у фрилансера.

    Лениво: SUM по журналу не выполняется, пока шаблон не обратится к
    переменной (на страницах директора/тимлида запроса нет).
    """
    return {
        'freelancer_earned_total': SimpleLazyObject(
            lambda: _freelancer_earned_total(request)
        ),
    }
