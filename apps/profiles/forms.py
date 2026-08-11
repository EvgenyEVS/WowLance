from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import FreelancerProfile, PortfolioItem
from .validators import validate_file_extension, validate_file_size


def _lines_to_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _list_to_lines(value):
    if isinstance(value, list):
        return '\n'.join(str(item) for item in value if item)
    return ''


def _parse_languages(value):
    """Строки вида «Английский: B2» или «Английский — B2» → список dict."""
    if isinstance(value, list):
        return value
    result = []
    for line in _lines_to_list(value):
        if '—' in line:
            language, level = line.split('—', 1)
        elif ':' in line:
            language, level = line.split(':', 1)
        else:
            language, level = line, ''
        result.append({
            'language': language.strip(),
            'level': level.strip(),
        })
    return result


def _languages_to_lines(value):
    if not isinstance(value, list):
        return ''
    lines = []
    for item in value:
        if isinstance(item, dict):
            language = item.get('language', '')
            level = item.get('level', '')
            lines.append(f'{language}: {level}'.strip(': ').strip() if level else language)
        else:
            lines.append(str(item))
    return '\n'.join(lines)


class UserProfileForm(forms.ModelForm):
    """Имя и фамилия редактируются в User, не в FreelancerProfile."""

    first_name = forms.CharField(label=_('Имя'), max_length=150)
    last_name = forms.CharField(label=_('Фамилия'), max_length=150)
    skills = forms.CharField(
        label=_('Навыки и методы продаж'),
        required=False,
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': 'Холодные звонки\nSPIN\nSalesforce',
        }),
        help_text=_('По одному навыку на строку'),
    )
    key_advantages = forms.CharField(
        label=_('Ключевые преимущества'),
        required=False,
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': 'закрыл 100 сделок\nконверсия 35%',
        }),
        help_text=_('Максимум 3 преимущества, по одному на строку'),
    )
    languages = forms.CharField(
        label=_('Владение языками'),
        required=False,
        widget=forms.Textarea(attrs={
            'rows': 2,
            'placeholder': 'Английский: B2\nРусский: Native',
        }),
        help_text=_('По одному языку на строку: «Язык: уровень»'),
    )
    portfolio_links = forms.CharField(
        label=_('Ссылки на портфолио'),
        required=False,
        widget=forms.Textarea(attrs={
            'rows': 2,
            'placeholder': 'https://linkedin.com/in/...\nhttps://drive.google.com/...',
        }),
        help_text=_('По одной ссылке на строку'),
    )

    class Meta:
        model = FreelancerProfile
        fields = [
            'country',
            'level',
            'experience_years',
            'experience_projects',
            'languages',
            'key_advantages',
            'skills',
            'portfolio_links',
            'linkedin_url',
            'avatar_url',
            'video_url',
            'is_available',
        ]
        widgets = {
            'avatar_url': forms.URLInput(attrs={
                'placeholder': 'https://…/photo.jpg',
            }),
            'video_url': forms.URLInput(attrs={
                'placeholder': 'https://youtube.com/watch?v=...',
            }),
            'linkedin_url': forms.URLInput(attrs={
                'placeholder': 'https://linkedin.com/in/...',
            }),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user')
        super().__init__(*args, **kwargs)
        self.fields['first_name'].initial = self.user.first_name
        self.fields['last_name'].initial = self.user.last_name
        if self.instance and self.instance.pk:
            self.fields['skills'].initial = _list_to_lines(self.instance.skills)
            self.fields['key_advantages'].initial = _list_to_lines(self.instance.key_advantages)
            self.fields['portfolio_links'].initial = _list_to_lines(self.instance.portfolio_links)
            self.fields['languages'].initial = _languages_to_lines(self.instance.languages)

    def clean_skills(self):
        return _lines_to_list(self.cleaned_data.get('skills'))

    def clean_key_advantages(self):
        advantages = _lines_to_list(self.cleaned_data.get('key_advantages'))
        if len(advantages) > 3:
            raise ValidationError('Не более 3 преимуществ.')
        return advantages

    def clean_portfolio_links(self):
        links = _lines_to_list(self.cleaned_data.get('portfolio_links'))
        for link in links:
            if not (link.startswith('http://') or link.startswith('https://')):
                raise ValidationError(
                    f'Ссылка должна начинаться с http:// или https://: {link}'
                )
        return links

    def clean_languages(self):
        return _parse_languages(self.cleaned_data.get('languages'))

    def save(self, commit=True):
        profile = super().save(commit=False)
        self.user.first_name = self.cleaned_data['first_name']
        self.user.last_name = self.cleaned_data['last_name']
        if commit:
            self.user.save(update_fields=['first_name', 'last_name'])
            profile.save()
        return profile


FreelancerProfileForm = UserProfileForm


class PortfolioItemFileForm(forms.ModelForm):
    """Загрузка файла в портфолио."""

    class Meta:
        model = PortfolioItem
        fields = ['title', 'file']

    def __init__(self, *args, **kwargs):
        self.portfolio = kwargs.pop('portfolio')
        super().__init__(*args, **kwargs)
        self.fields['title'].widget.attrs.update({'class': 'form-control'})

    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file:
            validate_file_extension(file)
            validate_file_size(file)
        return file

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.portfolio = self.portfolio
        instance.item_type = PortfolioItem.ItemType.FILE
        if instance.file:
            instance.file_size = instance.file.size
            if not instance.title:
                instance.title = instance.file.name
        if commit:
            instance.save()
        return instance


class PortfolioItemLinkForm(forms.ModelForm):
    """Добавление внешней ссылки в портфолио."""

    class Meta:
        model = PortfolioItem
        fields = ['title', 'url']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'url': forms.URLInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        self.portfolio = kwargs.pop('portfolio')
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.portfolio = self.portfolio
        instance.item_type = PortfolioItem.ItemType.LINK
        if commit:
            instance.save()
        return instance
