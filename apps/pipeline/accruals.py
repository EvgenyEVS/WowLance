"""Журнал начислений фрилансеру (демо-заглушка до кошелька).

Не путать с бюджетом комнаты и `earned_total` директора: здесь только
суммы из `FreelancerAccrual`, без юнит-экономики и Stripe.
"""

from decimal import Decimal

from django.db.models import Sum

from .models import FreelancerAccrual

#: Сумма демо-начисления за один принятый отчёт. Не ставка из юнит-экономики
#: (там рубли директора). Когда появится боевая выплата — заменят константу.
DEMO_ACCRUAL_USD_PER_APPROVED_REPORT = Decimal('10')


def earned_total_for(user) -> Decimal:
    """SUM всех начислений пользователя; пустой журнал → 0."""
    total = (
        FreelancerAccrual.objects.filter(freelancer=user)
        .aggregate(s=Sum('amount'))['s']
    )
    return total if total is not None else Decimal('0')


def earned_on_project(user, project) -> Decimal:
    """SUM начислений пользователя на одном проекте; пустой журнал → 0."""
    total = (
        FreelancerAccrual.objects.filter(freelancer=user, project=project)
        .aggregate(s=Sum('amount'))['s']
    )
    return total if total is not None else Decimal('0')


def ensure_accrual_for_approved_report(report) -> FreelancerAccrual:
    """Идемпотентно создаёт строку журнала за принятый отчёт."""
    accrual, _created = FreelancerAccrual.objects.get_or_create(
        report=report,
        defaults={
            'freelancer': report.author,
            'project': report.task.project,
            'amount': DEMO_ACCRUAL_USD_PER_APPROVED_REPORT,
            'title': f'Отчёт принят: {report.task.title}',
        },
    )
    return accrual
