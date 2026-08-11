"""Хелперы для карточки профиля фрилансера."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


SELLER_TITLES = {
    'junior': 'Junior seller',
    'middle': 'Middle seller',
    'senior': 'Senior seller',
}

_YT_WATCH = re.compile(
    r'(?:youtube\.com/watch\?.*?v=|youtu\.be/|youtube\.com/embed/)([A-Za-z0-9_-]{6,})',
    re.I,
)
_VIMEO = re.compile(r'vimeo\.com/(?:video/)?(\d+)', re.I)


def seller_title_for_level(level: str) -> str:
    return SELLER_TITLES.get(level, 'Seller')


def rating_stars(rating) -> list[bool]:
    """5 булевых флагов: залитая звезда / пустая."""
    try:
        value = float(rating or 0)
    except (TypeError, ValueError):
        value = 0.0
    filled = max(0, min(5, int(round(value))))
    return [i < filled for i in range(5)]


def video_embed_url(url: str) -> str | None:
    """Превращает YouTube/Vimeo URL в embed, иначе None."""
    if not url:
        return None
    raw = url.strip()
    yt = _YT_WATCH.search(raw)
    if yt:
        return f'https://www.youtube-nocookie.com/embed/{yt.group(1)}'
    vim = _VIMEO.search(raw)
    if vim:
        return f'https://player.vimeo.com/video/{vim.group(1)}'
    parsed = urlparse(raw)
    if 'youtube-nocookie.com' in parsed.netloc and '/embed/' in parsed.path:
        return raw
    if 'youtube.com' in parsed.netloc and parsed.path.startswith('/embed/'):
        video_id = parsed.path.rstrip('/').split('/')[-1]
        return f'https://www.youtube-nocookie.com/embed/{video_id}'
    if 'youtube.com' in parsed.netloc:
        vid = parse_qs(parsed.query).get('v', [None])[0]
        if vid:
            return f'https://www.youtube-nocookie.com/embed/{vid}'
    return None


def highlights_for_profile(profile) -> list[str]:
    """До 3 ключевых преимуществ; если пусто — запасные факты из профиля."""
    advantages = [
        str(item).strip()
        for item in (profile.advantages_list or [])
        if str(item).strip()
    ][:3]
    if advantages:
        return advantages
    fallback = []
    if profile.experience_projects:
        fallback.append(f'{profile.experience_projects} проектов')
    if profile.acceptance_rate and float(profile.acceptance_rate) > 0:
        fallback.append(f'{profile.acceptance_rate}% принятых отчётов')
    if profile.country:
        fallback.append(profile.country)
    return fallback[:3]
