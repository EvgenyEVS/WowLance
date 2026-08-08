from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate, logout
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.sites.shortcuts import get_current_site
from django.template.loader import render_to_string
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.core.mail import send_mail
from django.conf import settings
from .forms import RegistrationForm, LoginForm
from .tokens import account_activation_token

User = get_user_model()


def register(request):
    """
    Регистрация нового пользователя
    """
    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False  # Ждём подтверждения email
            user.save()

            # Отправляем письмо с подтверждением
            current_site = get_current_site(request)
            subject = 'Подтверждение регистрации на WowLance'
            message = render_to_string('users/activation_email.html', {
                'user': user,
                'domain': f"http://{current_site.domain}",
                'uid': urlsafe_base64_encode(force_bytes(user.pk)),
                'token': account_activation_token.make_token(user),
            })
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=False,
            )

            messages.success(request,
                             'Регистрация успешна! На ваш email отправлено письмо с подтверждением.')
            return redirect('users:login')
    else:
        form = RegistrationForm()

    return render(request, 'users/register.html', {'form': form})


def activate(request, uidb64, token):
    """
    Активация аккаунта по ссылке из письма
    """
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is not None and account_activation_token.check_token(user, token):
        user.is_active = True
        user.is_email_verified = True
        user.save()
        login(request, user)
        messages.success(request, 'Ваш аккаунт активирован! Добро пожаловать на WowLance.')
        return redirect('core:home')
    else:
        messages.error(request, 'Ссылка активации недействительна.')
        return redirect('users:login')


def login_view(request):
    """
    Вход пользователя
    """
    if request.method == 'POST':
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            email = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(request, username=email, password=password)
            if user is not None:
                login(request, user)
                messages.success(request, f'Добро пожаловать, {user.email}!')
                return redirect('core:home')
        messages.error(request, 'Неверный email или пароль.')
    else:
        form = LoginForm()

    return render(request, 'users/login.html', {'form': form})


def logout_view(request):
    """
    Выход пользователя
    """
    logout(request)
    messages.info(request, 'Вы вышли из системы.')
    return redirect('core:home')
