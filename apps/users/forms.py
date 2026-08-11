from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _

from .models import User as UserModel

User = get_user_model()

REGISTRATION_ROLE_CHOICES = [
    (UserModel.Roles.DIRECTOR, _('Директор')),
    (UserModel.Roles.FREELANCER, _('Фрилансер')),
]
ALLOWED_REGISTRATION_ROLES = {choice[0] for choice in REGISTRATION_ROLE_CHOICES}


class RegistrationForm(UserCreationForm):
    """Форма регистрации. Роль только director|freelancer."""

    first_name = forms.CharField(
        label=_('Имя'),
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    last_name = forms.CharField(
        label=_('Фамилия'),
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    email = forms.EmailField(
        label=_('Email'),
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'autocomplete': 'email',
        }),
    )
    password1 = forms.CharField(
        label=_('Пароль'),
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
    )
    password2 = forms.CharField(
        label=_('Подтверждение пароля'),
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
    )
    role = forms.ChoiceField(
        label=_('Роль'),
        choices=REGISTRATION_ROLE_CHOICES,
        widget=forms.RadioSelect(attrs={'class': 'role-radio'}),
    )

    class Meta:
        model = User
        fields = [
            'first_name',
            'last_name',
            'email',
            'password1',
            'password2',
            'role',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_user = None

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if not email:
            raise forms.ValidationError(_('Укажите email.'))

        existing = User.objects.filter(email__iexact=email).first()
        if existing is None:
            existing = User.objects.filter(username__iexact=email).first()

        if existing is not None:
            if existing.status == UserModel.Status.PENDING:
                # Неподтверждённый аккаунт можно «перезарегистрировать»
                self.pending_user = existing
                return email
            raise forms.ValidationError(
                _(
                    'Этот email уже зарегистрирован. Войдите или '
                    'восстановите доступ, если аккаунт уже подтверждён.'
                ),
            )
        return email

    def clean_role(self):
        role = self.cleaned_data.get('role')
        if role not in ALLOWED_REGISTRATION_ROLES:
            raise forms.ValidationError(
                _('Выберите роль: директор или фрилансер.'),
            )
        return role

    def validate_unique(self):
        """Pending-аккаунт с тем же email переиспользуем — не ругаемся на unique."""
        if self.pending_user is not None:
            return
        super().validate_unique()

    def save(self, commit=True):
        if self.pending_user is not None:
            user = self.pending_user
            user.email = self.cleaned_data['email']
            user.username = self.cleaned_data['email']
            user.first_name = self.cleaned_data['first_name']
            user.last_name = self.cleaned_data['last_name']
            user.role = self.cleaned_data['role']
            user.status = UserModel.Status.PENDING
            user.is_email_verified = False
            user.set_password(self.cleaned_data['password1'])
            if commit:
                user.save()
            return user

        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        user.username = self.cleaned_data['email']
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.role = self.cleaned_data['role']
        user.status = UserModel.Status.PENDING
        user.is_active = False
        if commit:
            user.save()
        return user


class ResendActivationForm(forms.Form):
    email = forms.EmailField(
        label=_('Email'),
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'autocomplete': 'email',
        }),
    )

    def clean_email(self):
        return (self.cleaned_data['email'] or '').strip().lower()


class LoginForm(AuthenticationForm):
    """Форма входа."""

    username = forms.EmailField(
        label=_('Email'),
        widget=forms.EmailInput(attrs={'class': 'form-control'}),
    )
    password = forms.CharField(
        label=_('Пароль'),
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
    )

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if user.status == UserModel.Status.BLOCKED:
            raise forms.ValidationError(
                _('Аккаунт заблокирован. Обратитесь в поддержку.'),
                code='blocked',
            )
        if user.status == UserModel.Status.PENDING:
            raise forms.ValidationError(
                _('Подтвердите email перед входом.'),
                code='pending',
            )
