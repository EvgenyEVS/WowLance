from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate, logout
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.utils.http import (
    urlsafe_base64_encode,
    urlsafe_base64_decode,
    url_has_allowed_host_and_scheme,
)
from django.utils.encoding import force_bytes, force_str
from django.core.mail import EmailMultiAlternatives
from django.conf import settings

from apps.profiles.services import get_or_create_freelancer_profile
from .forms import RegistrationForm, LoginForm, ALLOWED_REGISTRATION_ROLES
from .tokens import account_activation_token

User = get_user_model()


def register(request):
    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()

            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = account_activation_token.make_token(user)
            activation_path = f'/activate/{uid}/{token}/'
            activation_url = request.build_absolute_uri(activation_path)
            # Для шаблона письма — origin без пути
            domain = activation_url.rsplit(activation_path, 1)[0]

            subject = 'Подтверждение регистрации на WowLance'

            html_message = render_to_string('users/activation_email.html', {
                'user': user,
                'domain': domain,
                'uid': uid,
                'token': token,
            })

            text_message = f"""
Добро пожаловать на WowLance!

Привет, {user.email}!

Для активации аккаунта перейдите по ссылке:
{activation_url}

Ссылка действительна в течение 24 часов.

С уважением,
Команда WowLance
"""

            msg = EmailMultiAlternatives(
                subject,
                text_message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
            )
            msg.attach_alternative(html_message, "text/html")
            msg.send()

            # В DEBUG показываем ссылку на странице — SMTP не нужен
            if settings.DEBUG:
                return render(request, 'users/registration_success.html', {
                    'email': user.email,
                    'activation_url': activation_url,
                    'debug_mode': True,
                })

            messages.success(
                request,
                'Регистрация успешна! На ваш email отправлено письмо с подтверждением.'
            )
            return redirect('users:login')
    else:
        initial = {}
        role = request.GET.get('role', '')
        if role in ALLOWED_REGISTRATION_ROLES:
            initial['role'] = role
        form = RegistrationForm(initial=initial)

    if form.is_bound:
        role = form.data.get('role', '')
    else:
        role = request.GET.get('role', '')

    return render(request, 'users/register.html', {
        'form': form,
        'selected_role': role if role in ALLOWED_REGISTRATION_ROLES else '',
    })


def activate(request, uidb64, token):
    """Активация аккаунта по ссылке из письма."""
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is not None and account_activation_token.check_token(user, token):
        user.status = User.Status.ACTIVE
        user.is_email_verified = True
        user.save()

        if user.role == User.Roles.FREELANCER:
            get_or_create_freelancer_profile(user)

        login(request, user)
        messages.success(request, 'Ваш аккаунт активирован! Добро пожаловать на WowLance.')
        return redirect('core:home')

    messages.error(request, 'Ссылка активации недействительна.')
    return redirect('users:login')


def login_view(request):
    """Вход пользователя."""
    if request.method == 'POST':
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            email = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(request, username=email, password=password)
            if user is not None:
                login(request, user)
                messages.success(request, f'Добро пожаловать, {user.email}!')
                next_url = request.POST.get('next') or request.GET.get('next')
                if next_url and url_has_allowed_host_and_scheme(
                    next_url,
                    allowed_hosts={request.get_host()},
                ):
                    return redirect(next_url)
                return redirect('core:home')
        messages.error(request, 'Неверный email или пароль.')
    else:
        form = LoginForm()

    return render(request, 'users/login.html', {
        'form': form,
        'next': request.GET.get('next', ''),
    })


def logout_view(request):
    """Выход пользователя."""
    logout(request)
    messages.info(request, 'Вы вышли из системы.')
    return redirect('core:home')
