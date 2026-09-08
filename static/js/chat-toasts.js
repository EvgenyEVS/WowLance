// chat-toasts.js - единый механизм обновления

// Функция закрытия тоста
function closeToast(toast) {
    if (!toast || !toast.parentNode) return;
    toast.style.transition = 'opacity 0.4s ease, transform 0.4s ease';
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(30px)';
    setTimeout(() => {
        if (toast.parentNode) {
            toast.remove();
        }
    }, 400);
}

// Настройка авто-закрытия
function setupAutoClose(toast) {
    if (toast._closeTimer) {
        clearTimeout(toast._closeTimer);
    }
    toast._closeTimer = setTimeout(() => {
        closeToast(toast);
    }, 5000);
}

// Обработчик кликов (делегирование)
document.addEventListener('click', function(e) {
    const closeBtn = e.target.closest('.chat-toast-close');
    if (closeBtn) {
        e.stopPropagation();
        const toast = closeBtn.closest('.chat-toast');
        if (toast) {
            closeToast(toast);
            return;
        }
    }

    const link = e.target.closest('.chat-toast-link');
    if (link) {
        e.preventDefault();
        const toast = link.closest('.chat-toast');
        if (toast) {
            closeToast(toast);
            setTimeout(() => {
                window.location.href = link.href;
            }, 350);
        }
    }
});

// Единый механизм обновления
function updateChatAlerts() {
    if (document.visibilityState !== 'visible') return;

    const container = document.getElementById('chat-toasts-container');
    if (!container) {
        return;
    }

    const roomMatch = window.location.pathname.match(/\/projects\/([^\/]+)\//);
    const roomId = roomMatch ? roomMatch[1] : '';

    fetch('/chat-alerts/?room_id=' + roomId)
        .then(r => r.text())
        .then(html => {
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');

            // 1. Обновляем колокольчик
            const newBell = doc.querySelector('#chat-bell-container');
            const wrapper = document.getElementById('chat-bell-wrapper');
            if (newBell && wrapper) {
                const oldBell = wrapper.querySelector('#chat-bell-container');
                if (oldBell) {
                    oldBell.replaceWith(newBell.cloneNode(true));
                }
            }

            // 2. Добавляем новые тосты
            const toasts = doc.querySelectorAll('.chat-toast');
            if (toasts.length === 0) return;

            const existingIds = new Set();
            container.querySelectorAll('.chat-toast').forEach(t => {
                if (t.id) existingIds.add(t.id);
            });

            toasts.forEach(toast => {
                if (toast.id && !existingIds.has(toast.id)) {
                    container.prepend(toast.cloneNode(true));
                    const newToast = container.firstChild;
                    setupAutoClose(newToast);
                    newToast.dataset.setupDone = 'true';
                }
            });
        })
        .catch(err => console.log('Fetch error:', err));
}

// Запускаем обновление каждые 3 секунды
setInterval(updateChatAlerts, 3000);

// Инициализация
document.addEventListener('DOMContentLoaded', function() {
    // Настраиваем существующие тосты
    const container = document.getElementById('chat-toasts-container');
    if (container) {
        container.querySelectorAll('.chat-toast:not([data-setup-done])').forEach(toast => {
            setupAutoClose(toast);
            toast.dataset.setupDone = 'true';
        });
    }
    // Первое обновление через 1 секунду
    setTimeout(updateChatAlerts, 1000);
});