from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.users.models import User
from .models import Project, RoomDocument


class ProjectCreateForm(forms.ModelForm):
    """Создание проекта по шаблону вводных."""

    offer = forms.CharField(
        label=_('Оффер'),
        widget=forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
        help_text=_('Что продаём и на каких условиях'),
    )
    utp = forms.CharField(
        label=_('УТП'),
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
        help_text=_('Уникальное торговое предложение'),
    )
    audience = forms.CharField(
        label=_('Целевая аудитория'),
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
    )
    hot_criteria = forms.CharField(
        label=_('Критерии горячего лида'),
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
        help_text=_('Например: запросил демо, согласен на встречу, интерес к КП'),
    )

    class Meta:
        model = Project
        fields = [
            'name',
            'project_type',
            'seller_level',
            'tariff_plan',
            'budget',
            'kpi_target',
            'start_date',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'project_type': forms.Select(attrs={'class': 'form-control'}),
            'seller_level': forms.Select(attrs={'class': 'form-control'}),
            'tariff_plan': forms.TextInput(attrs={'class': 'form-control'}),
            'budget': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'kpi_target': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'start_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['budget'].initial = Decimal('0')
        self.fields['tariff_plan'].initial = 'launch'
        self.fields['kpi_target'].required = False
        self.fields['start_date'].required = False

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if len(name) < 3:
            raise forms.ValidationError(_('Название — минимум 3 символа.'))
        return name

    def save(self, commit=True):
        project = super().save(commit=False)
        project.input_data = {
            'offer': self.cleaned_data['offer'].strip(),
            'utp': self.cleaned_data['utp'].strip(),
            'audience': self.cleaned_data['audience'].strip(),
            'hot_criteria': self.cleaned_data['hot_criteria'].strip(),
        }
        if commit:
            project.save()
        return project


class RoomDocumentForm(forms.ModelForm):
    class Meta:
        model = RoomDocument
        fields = ['title', 'file']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'file': forms.ClearableFileInput(attrs={'class': 'form-control'}),
        }


class AssignTeamleadForm(forms.Form):
    teamlead = forms.ModelChoiceField(
        label=_('Тимлид'),
        queryset=User.objects.none(),
        widget=forms.Select(attrs={'class': 'form-control'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['teamlead'].queryset = User.objects.filter(
            role=User.Roles.TEAMLEAD,
            status=User.Status.ACTIVE,
        ).order_by('first_name', 'email')


class AddFreelancerForm(forms.Form):
    freelancer = forms.ModelChoiceField(
        label=_('Фрилансер'),
        queryset=User.objects.none(),
        widget=forms.Select(attrs={'class': 'form-control'}),
    )

    def __init__(self, *args, room=None, **kwargs):
        super().__init__(*args, **kwargs)
        existing_ids = []
        if room is not None:
            existing_ids = list(room.members.values_list('user_id', flat=True))
        self.fields['freelancer'].queryset = User.objects.filter(
            role=User.Roles.FREELANCER,
            status=User.Status.ACTIVE,
        ).exclude(id__in=existing_ids).order_by('first_name', 'email')
