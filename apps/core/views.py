from django.shortcuts import render

def about(request):
    return render(request, 'core/about.html')


def home(request):
    """
    Главная страница. Показывает разный контент в зависимости от роли.
    """
    if not request.user.is_authenticated:
        return render(request, 'core/landing.html')

    if request.user.role == 'director':
        return render(request, 'core/director_dashboard.html')
    elif request.user.role == 'freelancer':
        return render(request, 'core/freelancer_dashboard.html')

    return render(request, 'core/landing.html')