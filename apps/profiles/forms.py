from django import forms
from django.utils.translation import gettext_lazy as _
from .models import FreelancerProfile, PortfolioFile
from .validators import validate_file_extension, validate_file_size


class FreelancerProfileForm(forms.ModelForm):
    """
    Форма для редактирования профиля фрилансера
    """
    class Meta:
        model = FreelancerProfile
        fields = [
            'first_name',
            'last_name',
            'country',
            'experience_years',
            'experience_projects',
            'languages',
            'key_advantages',
            'skills',
            'linkedin_url',
            'video_url',
            'is_available',
        ]
        widgets = {
            'languages': forms.Textarea(attrs={
                'rows': 2,
                'placeholder': '[{"language": "Английский", "level": "B2"}]'
            }),
            'key_advantages': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': '["закрыл 100 сделок", "конверсия 35%"]'
            }),
            'skills': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': '["Холодные звонки", "SPIN", "Salesforce"]'
            }),
            'video_url': forms.URLInput(attrs={
                'placeholder': 'https://youtube.com/...'
            }),
            'linkedin_url': forms.URLInput(attrs={
                'placeholder': 'https://linkedin.com/in/...'
            }),
        }

    def clean_key_advantages(self):
        """
        Проверяем, что преимуществ не больше 3
        """
        advantages = self.cleaned_data.get('key_advantages', [])
        if not isinstance(advantages, list):
            advantages = []
        if len(advantages) > 3:
            raise forms.ValidationError('Не более 3 преимуществ.')
        return advantages


class PortfolioFileForm(forms.ModelForm):
    """
    Форма для загрузки файлов портфолио
    """
    class Meta:
        model = PortfolioFile
        fields = ['file']

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file:
            validate_file_extension(file)
            validate_file_size(file)
        return file

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.user:
            instance.user = self.user
        instance.file_name = self.cleaned_data['file'].name
        instance.file_size = self.cleaned_data['file'].size
        if commit:
            instance.save()
        return instance