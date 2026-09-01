from django import forms
from django.utils.translation import gettext_lazy as _

from apps.rooms.models import Project
from apps.users.models import User
from .models import Lead, Report, Task
from .services import parse_checklist_text
from .teamlead_report import default_report_period


class TeamleadPeriodReportForm(forms.Form):
    """Параметры отчёта тимлида за период: «с», «по» и проект.

    Форма только разбирает и валидирует запрос — агрегация живёт в
    `apps.pipeline.teamlead_report.build_teamlead_period_report`.

    Обе даты `required=False` намеренно: по контракту и пустой запрос
    (`/teamlead/report/`), и явно пустые параметры
    (`?date_from=&date_to=`) означают период по умолчанию — последние семь
    календарных дней, включая сегодня. Подстановка дефолта происходит в
    `clean()`, поэтому связанная форма с пустыми данными остаётся валидной.

    Пустой `project` означает «Все проекты»: сервис в этом случае берёт все
    проекты, где пользователь — тимлид.
    """

    date_from = forms.DateField(
        label=_('С'),
        required=False,
        widget=forms.DateInput(
            attrs={'class': 'form-control', 'type': 'date'},
            format='%Y-%m-%d',
        ),
        input_formats=['%Y-%m-%d'],
    )
    date_to = forms.DateField(
        label=_('По'),
        required=False,
        widget=forms.DateInput(
            attrs={'class': 'form-control', 'type': 'date'},
            format='%Y-%m-%d',
        ),
        input_formats=['%Y-%m-%d'],
    )
    project = forms.ModelChoiceField(
        label=_('Проект'),
        queryset=Project.objects.none(),
        required=False,
        empty_label=_('Все проекты'),
        widget=forms.Select(attrs={'class': 'form-control'}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        default_from, default_to = default_report_period()
        self.fields['date_from'].initial = default_from
        self.fields['date_to'].initial = default_to
        if user is not None and getattr(user, 'is_authenticated', False):
            # Без фильтра по статусу: отчёт за прошедший период нужен и по
            # завершённому проекту. Сужение queryset'ом закрывает выбор
            # чужого проекта на уровне формы; отдельный 403 за чужой
            # `project_id` выдаёт view.
            self.fields['project'].queryset = (
                Project.objects.filter(teamlead=user).order_by('created_at')
            )

    def clean(self):
        cleaned = super().clean()
        default_from, default_to = default_report_period()
        date_from = cleaned.get('date_from') or default_from
        date_to = cleaned.get('date_to') or default_to

        if date_from > date_to:
            raise forms.ValidationError(
                _('Дата «С» не может быть позже даты «По».')
            )

        cleaned['date_from'] = date_from
        cleaned['date_to'] = date_to
        return cleaned


class TaskCreateForm(forms.Form):
    title = forms.CharField(
        label=_('Название'),
        min_length=5,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    description = forms.CharField(
        label=_('Описание'),
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
    )
    assignee = forms.ModelChoiceField(
        label=_('Исполнитель'),
        queryset=User.objects.none(),
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    deadline = forms.DateTimeField(
        label=_('Дедлайн'),
        required=False,
        widget=forms.DateTimeInput(
            attrs={'class': 'form-control', 'type': 'datetime-local'},
            format='%Y-%m-%dT%H:%M',
        ),
        input_formats=['%Y-%m-%dT%H:%M'],
    )
    checklist_text = forms.CharField(
        label=_('Чеклист'),
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'Пункт 1\nПункт 2',
        }),
        help_text=_('По одному пункту на строку'),
    )
    report_required = forms.BooleanField(
        label=_('Обязателен отчёт'),
        required=False,
        initial=True,
    )

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        if project is not None:
            member_ids = project.room.members.values_list('user_id', flat=True)
            self.fields['assignee'].queryset = User.objects.filter(
                id__in=member_ids,
                status=User.Status.ACTIVE,
            ).order_by('first_name', 'email')

    def cleaned_checklist(self):
        return parse_checklist_text(self.cleaned_data.get('checklist_text', ''))


class ReportSubmitForm(forms.ModelForm):
    class Meta:
        model = Report
        fields = ['content_text', 'attachment']
        widgets = {
            'content_text': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 4,
                'placeholder': 'Что сделано, результат звонка/письма…',
            }),
            'attachment': forms.ClearableFileInput(attrs={'class': 'form-control'}),
        }

    def clean_content_text(self):
        text = (self.cleaned_data.get('content_text') or '').strip()
        if len(text) < 10:
            raise forms.ValidationError(_('Минимум 10 символов.'))
        return text

    def clean_attachment(self):
        file = self.cleaned_data.get('attachment')
        if not file:
            raise forms.ValidationError(_('Прикрепите скриншот или файл.'))
        return file


class ReportReviewForm(forms.Form):
    comment = forms.CharField(
        label=_('Комментарий'),
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
    )
    action = forms.ChoiceField(
        choices=[('approve', 'Утвердить'), ('reject', 'Отклонить')],
        widget=forms.HiddenInput(),
    )


class LeadCreateForm(forms.Form):
    name = forms.CharField(
        label=_('Имя контакта'),
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    phone = forms.CharField(
        label=_('Телефон'),
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    email = forms.EmailField(
        label=_('Email'),
        required=False,
        widget=forms.EmailInput(attrs={'class': 'form-control'}),
    )
    linkedin = forms.URLField(
        label=_('LinkedIn'),
        required=False,
        widget=forms.URLInput(attrs={'class': 'form-control'}),
    )
    source = forms.ChoiceField(
        label=_('Источник'),
        choices=Lead.Source.choices,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    notes = forms.CharField(
        label=_('Комментарий'),
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
    )
    qualification_status = forms.ChoiceField(
        label=_('Статус'),
        choices=[
            (Lead.Qualification.COLD, _('Холодный')),
            (Lead.Qualification.WARM, _('Тёплый')),
        ],
        initial=Lead.Qualification.WARM,
        widget=forms.Select(attrs={'class': 'form-control'}),
        help_text=_('«Горячий» выставляет только тимлид после проверки.'),
    )

    def clean(self):
        cleaned = super().clean()
        if not any([
            cleaned.get('name'),
            cleaned.get('phone'),
            cleaned.get('email'),
            cleaned.get('linkedin'),
        ]):
            raise forms.ValidationError(_('Укажите хотя бы один контакт.'))
        return cleaned

    def contact_info(self) -> dict:
        return {
            'name': self.cleaned_data.get('name', ''),
            'phone': self.cleaned_data.get('phone', ''),
            'email': self.cleaned_data.get('email', ''),
            'linkedin': self.cleaned_data.get('linkedin', ''),
        }


class LeadQualifyForm(forms.Form):
    qualification_status = forms.ChoiceField(
        label=_('Квалификация'),
        choices=Lead.Qualification.choices,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    matched_hot_criteria = forms.CharField(
        label=_('Совпавшие критерии Hot'),
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 2,
            'placeholder': 'Запросил демо\nСогласен на встречу',
        }),
        help_text=_('Для Hot — по одному критерию на строку'),
    )
    comment = forms.CharField(
        label=_('Комментарий'),
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
    )

    def cleaned_criteria_list(self):
        raw = self.cleaned_data.get('matched_hot_criteria', '')
        return [line.strip() for line in raw.splitlines() if line.strip()]
