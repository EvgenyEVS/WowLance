"""Absolute URL helper respecting PUBLIC_SCHEME."""

from urllib.parse import urlparse, urlunparse

from django.conf import settings


def absolute_uri(request, path: str) -> str:
    """Build absolute URL; force PUBLIC_SCHEME for PUBLIC_HOST (demo has no TLS)."""
    if not path.startswith('/'):
        path = f'/{path}'

    host = (settings.PUBLIC_HOST or '').strip()
    scheme = (settings.PUBLIC_SCHEME or 'http').strip() or 'http'

    if host:
        return f'{scheme}://{host}{path}'

    url = request.build_absolute_uri(path)
    parsed = urlparse(url)
    return urlunparse(
        (scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
