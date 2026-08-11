"""Хелперы для абсолютных URL с учётом PUBLIC_SCHEME=https."""

from django.conf import settings


def absolute_uri(request, path: str) -> str:
    """Как build_absolute_uri, но для прод-хоста предпочитает https://."""
    url = request.build_absolute_uri(path)
    if settings.PUBLIC_SCHEME == 'https' and url.startswith('http://'):
        host = settings.PUBLIC_HOST
        if host and host in url:
            return 'https://' + url[len('http://'):]
    return url
