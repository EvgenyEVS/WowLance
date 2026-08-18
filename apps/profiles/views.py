from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.views.decorators.http import require_POST

from apps.users.models import User
from .models import FreelancerProfile, Portfolio, PortfolioItem
from .forms import UserProfileForm, PortfolioItemFileForm, PortfolioItemLinkForm
from .services import get_or_create_freelancer_profile
from .card import (
    highlights_for_profile,
    rating_stars,
    seller_title_for_level,
    video_embed_url,
)

def _require_freelancer(user):
    if user.role != User.Roles.FREELANCER:
        raise PermissionDenied('Только фрилансеры могут выполнять это действие.')


@login_required
def freelancer_catalog(request):
    """Каталог фрилансеров с фильтрами по уровню, доступности и поиску."""
    profiles = (
        FreelancerProfile.objects
        .select_related('user')
        .filter(
            user__role=User.Roles.FREELANCER,
            user__status=User.Status.ACTIVE,
        )
    )

    level = request.GET.get('level', '').strip()
    available = request.GET.get('available', '').strip()
    q = request.GET.get('q', '').strip()

    if level in FreelancerProfile.Level.values:
        profiles = profiles.filter(level=level)

    if available == '1':
        profiles = profiles.filter(is_available=True)
    elif available == '0':
        profiles = profiles.filter(is_available=False)

    if q:
        profiles = profiles.filter(
            Q(user__first_name__icontains=q)
            | Q(user__last_name__icontains=q)
            | Q(user__email__icontains=q)
            | Q(country__icontains=q)
            | Q(skills__icontains=q)
        )

    profiles = profiles.order_by('-is_verified', '-rating', 'user__first_name')
    ctx = {
        'profiles': profiles,
        'levels': FreelancerProfile.Level.choices,
        'selected_level': level,
        'selected_available': available,
        'search_query': q,
    }
    return render(request, 'profiles/catalog.html', ctx)


@login_required
def profile_detail(request, user_id):
    """Карточка фрилансера по макету (фото, highlights, video, проекты/навыки)."""
    user = get_object_or_404(User, id=user_id, role=User.Roles.FREELANCER)
    profile = get_object_or_404(FreelancerProfile, user=user)
    portfolio = getattr(profile, 'portfolio', None)
    portfolio_items = list(portfolio.items.filter(is_public=True)[:6]) if portfolio else []
    is_owner = request.user.id == user.id
    ctx = {
        'profile_user': user,
        'profile': profile,
        'portfolio': portfolio,
        'portfolio_items': portfolio_items,
        'is_owner': is_owner,
        'avatar_initials': ''.join(
            part[0] for part in user.full_name.split()[:2]
        ).upper() or user.email[:1].upper(),
        'seller_title': seller_title_for_level(profile.level),
        'rating_stars': rating_stars(profile.rating),
        'highlights': highlights_for_profile(profile),
        'video_embed_url': video_embed_url(profile.video_url),
    }
    return render(request, 'profiles/detail.html', ctx)


@login_required
def portfolio_detail(request, user_id):
    """Полная страница портфолио фрилансера."""
    user = get_object_or_404(User, id=user_id, role=User.Roles.FREELANCER)
    profile = get_object_or_404(FreelancerProfile, user=user)
    portfolio = get_object_or_404(Portfolio, profile=profile)
    is_owner = request.user.id == user.id

    items = portfolio.items.all()
    if not is_owner:
        items = items.filter(is_public=True)

    return render(request, 'profiles/portfolio.html', {
        'profile_user': user,
        'profile': profile,
        'portfolio': portfolio,
        'portfolio_items': items,
        'is_owner': is_owner,
    })


@login_required
def profile_edit(request):
    """Редактирование профиля фрилансера."""
    if request.user.role != User.Roles.FREELANCER:
        messages.error(request, 'Только фрилансеры могут редактировать профиль.')
        return redirect('core:home')

    profile = get_or_create_freelancer_profile(request.user)

    if request.method == 'POST':
        form = UserProfileForm(request.POST, instance=profile, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Профиль успешно обновлён!')
            return redirect('profiles:detail', user_id=request.user.id)
    else:
        form = UserProfileForm(instance=profile, user=request.user)

    return render(request, 'profiles/edit.html', {
        'form': form,
        'profile': profile,
    })


@login_required
@require_POST
def portfolio_upload(request):
    """Загрузка файла в портфолио."""
    _require_freelancer(request.user)

    profile = get_or_create_freelancer_profile(request.user)
    portfolio = profile.portfolio

    form = PortfolioItemFileForm(request.POST, request.FILES, portfolio=portfolio)
    if form.is_valid():
        form.save()
        messages.success(request, 'Файл добавлен в портфолио!')
    else:
        for error in form.errors.values():
            messages.error(request, error)

    return redirect('profiles:portfolio', user_id=request.user.id)


@login_required
@require_POST
def portfolio_add_link(request):
    """Добавление ссылки в портфолио."""
    _require_freelancer(request.user)

    profile = get_or_create_freelancer_profile(request.user)
    portfolio = profile.portfolio

    form = PortfolioItemLinkForm(request.POST, portfolio=portfolio)
    if form.is_valid():
        form.save()
        messages.success(request, 'Ссылка добавлена в портфолио!')
    else:
        for error in form.errors.values():
            messages.error(request, error)

    return redirect('profiles:portfolio', user_id=request.user.id)


@login_required
@require_POST
def portfolio_delete(request, item_id):
    """Удаление элемента портфолио."""
    item = get_object_or_404(PortfolioItem, id=item_id)

    if item.portfolio.profile.user != request.user:
        raise PermissionDenied('Вы не можете удалить этот элемент.')

    item.delete()
    messages.success(request, 'Элемент удалён из портфолио!')
    return redirect('profiles:portfolio', user_id=request.user.id)


@login_required
@require_POST
def add_skill(request):
    """Добавление навыка."""
    _require_freelancer(request.user)

    skill = request.POST.get('skill', '').strip()
    if skill:
        profile = get_or_create_freelancer_profile(request.user)
        skills = profile.skills if isinstance(profile.skills, list) else []
        if skill not in skills:
            skills.append(skill)
            profile.skills = skills
            profile.save(update_fields=['skills'])
            messages.success(request, f'Навык «{skill}» добавлен!')
    return redirect('profiles:edit')
