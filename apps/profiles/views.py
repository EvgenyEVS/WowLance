from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from .models import FreelancerProfile, PortfolioFile
from .forms import FreelancerProfileForm, PortfolioFileForm
from apps.users.models import User


@login_required
def profile_detail(request, user_id):
    """
    Публичная карточка фрилансера
    """
    user = get_object_or_404(User, id=user_id, role='freelancer')
    profile = get_object_or_404(FreelancerProfile, user=user)
    portfolio = user.portfolio_files.all()

    is_owner = request.user.id == user.id

    return render(request, 'profiles/detail.html', {
        'profile': profile,
        'portfolio': portfolio,
        'is_owner': is_owner,
    })


@login_required
def profile_edit(request):
    """
    Редактирование своего профиля
    """
    if request.user.role != 'freelancer':
        messages.error(request, 'Только фрилансеры могут редактировать профиль.')
        return redirect('core:home')

    profile, created = FreelancerProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        form = FreelancerProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Профиль успешно обновлён!')
            return redirect('profiles:detail', user_id=request.user.id)
    else:
        form = FreelancerProfileForm(instance=profile)

    return render(request, 'profiles/edit.html', {
        'form': form,
        'profile': profile,
    })


@login_required
def portfolio_upload(request):
    """
    Загрузка файла портфолио (HTMX)
    """
    if request.user.role != 'freelancer':
        return JsonResponse({'error': 'Доступ запрещён'}, status=403)

    if request.method == 'POST':
        form = PortfolioFileForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Файл успешно загружен!')
            return redirect('profiles:detail', user_id=request.user.id)
        else:
            for error in form.errors.values():
                messages.error(request, error)
            return redirect('profiles:edit')

    return redirect('profiles:edit')


@login_required
def portfolio_delete(request, file_id):
    """
    Удаление файла портфолио
    """
    file = get_object_or_404(PortfolioFile, id=file_id)

    if file.user != request.user:
        raise PermissionDenied('Вы не можете удалить этот файл.')

    file.delete()
    messages.success(request, 'Файл удалён!')
    return redirect('profiles:detail', user_id=request.user.id)


@login_required
def add_skill(request):
    """
    Добавление навыка через HTMX (без перезагрузки)
    """
    if request.method == 'POST':
        skill = request.POST.get('skill', '').strip()
        if skill:
            profile, _ = FreelancerProfile.objects.get_or_create(user=request.user)
            skills = profile.skills if isinstance(profile.skills, list) else []
            if skill not in skills:
                skills.append(skill)
                profile.skills = skills
                profile.save()
                messages.success(request, f'Навык "{skill}" добавлен!')
        return redirect('profiles:edit')