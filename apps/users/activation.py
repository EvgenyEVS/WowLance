"""Отправка и переиспользование активации аккаунта."""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .tokens import account_activation_token


def build_activation_url(request, user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = account_activation_token.make_token(user)
    path = f'/activate/{uid}/{token}/'
    return request.build_absolute_uri(path), uid, token


def send_activation_email(request, user) -> str:
    """Шлёт письмо и возвращает абсолютный URL активации."""
    activation_url, uid, token = build_activation_url(request, user)
    activation_path = f'/activate/{uid}/{token}/'
    domain = activation_url.rsplit(activation_path, 1)[0]

    subject = 'Подтверждение регистрации на WowLance'
    html_message = render_to_string('users/activation_email.html', {
        'user': user,
        'domain': domain,
        'uid': uid,
        'token': token,
    })
    text_message = (
        f'Добро пожаловать на WowLance!\n\n'
        f'Привет, {user.email}!\n\n'
        f'Для активации аккаунта перейдите по ссылке:\n{activation_url}\n\n'
        f'С уважением,\nКоманда WowLance\n'
    )
    msg = EmailMultiAlternatives(
        subject,
        text_message,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )
    msg.attach_alternative(html_message, 'text/html')
    msg.send()
    return activation_url
