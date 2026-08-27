from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate, logout
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.utils.http import (
    urlsafe_base64_decode,
    url_has_allowed_host_and_scheme,
)
from django.utils.encoding import force_str
from django.conf import settings
from django.views.decorators.http import require_POST

from apps.profiles.services import get_or_create_freelancer_profile
from .activation import send_activation_email
from .forms import (
    RegistrationForm,
    LoginForm,
    ResendActivationForm,
    ALLOWED_REGISTRATION_ROLES,
)
from .tokens import account_activation_token
from .wowtalent_client import get_wowtalent_user_data

User = get_user_model()


def _is_console_email_backend() -> bool:
    """True, если письма пишутся в консоль сервера, а не уходят по SMTP."""
    return 'console' in (settings.EMAIL_BACKEND or '').lower()


def _registration_success_response(request, user, activation_url):
    """
    После регистрации / повторной отправки письма:
    - DEBUG или DEMO_MODE: страница успеха со ссылкой при console;
    - иначе: сообщение и редирект на логин (ожидаем реальное письмо).
    """
    show_link = settings.DEBUG or getattr(settings, 'DEMO_MODE', False)
    if show_link:
        return render(request, 'users/registration_success.html', {
            'email': user.email,
            'activation_url': activation_url,
            'debug_mode': True,
            'is_console_backend': _is_console_email_backend(),
            'demo_mode': getattr(settings, 'DEMO_MODE', False),
        })
    messages.success(
        request,
        'Регистрация успешна! На ваш email отправлено письмо с подтверждением. '
        'После активации аккаунта вы сможете войти в WowLance.',
    )
    return redirect('users:login')


def register(request):
    # Общая часть для GET и POST: определяем, пришёл ли пользователь из WOW Talent
    ref_code = request.GET.get('ref', '').strip()
    is_wowtalent_ref = False
    wt_data = get_wowtalent_user_data(ref_code) if ref_code else None
    if wt_data:
        is_wowtalent_ref = True

    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            reused = form.pending_user is not None
            user = form.save()
            activation_url = send_activation_email(request, user)
            if reused:
                messages.info(
                    request,
                    'Этот email уже ждал подтверждения. Мы обновили данные '
                    'и отправили новую ссылку активации.',
                )
            return _registration_success_response(request, user, activation_url)
    else:
        # GET-запрос: заполняем initial данными из stub и ролью
        initial = {}
        role = request.GET.get('role', '')
        if role in ALLOWED_REGISTRATION_ROLES:
            initial['role'] = role
        if wt_data:
            initial.update(wt_data)  # first_name, last_name, email
        form = RegistrationForm(initial=initial)

    arch = request.GET.get('arch', '').strip()
    if arch:
        request.session['architecture_preset'] = arch

    if form.is_bound:
        role = form.data.get('role', '')
    else:
        role = request.GET.get('role', '')

    return render(request, 'users/register.html', {
        'form': form,
        'selected_role': role if role in ALLOWED_REGISTRATION_ROLES else '',
        'arch': arch or request.session.get('architecture_preset', ''),
        'is_wowtalent_ref': is_wowtalent_ref,
    })


def resend_activation(request):
    """Повторная отправка ссылки для pending-аккаунта."""
    form = ResendActivationForm(
        request.POST or None,
        initial={'email': request.GET.get('email', '')},
    )
    if request.method == 'POST' and form.is_valid():
        email = form.cleaned_data['email']
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            messages.error(request, 'Аккаунт с таким email не найден.')
        elif user.status == User.Status.ACTIVE:
            messages.info(request, 'Этот аккаунт уже активирован. Можно войти.')
            return redirect('users:login')
        elif user.status == User.Status.BLOCKED:
            messages.error(request, 'Аккаунт заблокирован.')
        elif user.status == User.Status.PENDING:
            activation_url = send_activation_email(request, user)
            return _registration_success_response(request, user, activation_url)
        else:
            messages.error(request, 'Нельзя отправить ссылку для этого аккаунта.')

    return render(request, 'users/resend_activation.html', {'form': form})


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
        if (
            user.role == User.Roles.DIRECTOR
            and request.session.get('architecture_preset')
        ):
            from django.urls import reverse
            return redirect(
                f"{reverse('rooms:setup_wizard')}?step=2"
                f"&arch={request.session['architecture_preset']}"
            )
        return redirect('core:home')

    messages.error(
        request,
        'Ссылка активации недействительна или устарела. '
        'Запросите новую на странице повторной отправки.',
    )
    return redirect('users:resend_activation')


def login_view(request):
    """Вход пользователя."""
    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST':
        email = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''
        existing = User.objects.filter(email__iexact=email).first()

        if existing and existing.status == User.Status.PENDING:
            # ModelBackend не пускает is_active=False — объясняем причину
            if existing.check_password(password):
                messages.warning(
                    request,
                    'Аккаунт ещё не подтверждён. Откройте ссылку из письма '
                    'или запросите новую.',
                )
                return redirect(f"/resend-activation/?email={existing.email}")
            messages.error(request, 'Неверный email или пароль.')
        elif form.is_valid():
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
            # pending/blocked из confirm_login_allowed
            if form.non_field_errors():
                for err in form.non_field_errors():
                    messages.error(request, err)
            else:
                messages.error(request, 'Неверный email или пароль.')

    return render(request, 'users/login.html', {
        'form': form if request.method == 'POST' else LoginForm(),
        'next': request.GET.get('next', ''),
    })


@require_POST
def logout_view(request):
    """Выход пользователя (только POST + CSRF)."""
    logout(request)
    messages.info(request, 'Вы вышли из системы.')
    return redirect('core:home')
