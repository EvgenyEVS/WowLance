# apps/users/wowtalent_client.py

def get_wowtalent_user_data(ref_code: str) -> dict | None:
    """
    Возвращает данные пользователя из WOW Talent по реферальному коду.
    В текущей версии MVP используется stub (заглушка) вместо реального HTTP-запроса.

    Args:
        ref_code: Реферальный код из URL (например, 'wowtalent_demo').

    Returns:
        Словарь с данными пользователя (first_name, last_name, email) или None,
        если код не найден.
    """
    # Словарь с демо-данными. В будущем здесь может быть реальный запрос к API.
    DEMO_DATA = {
        "wowtalent_demo": {
            "first_name": "Иван",
            "last_name": "Иванов",
            "email": "ivan.ivanov@wowtalent.demo",
        },
        # Можно добавить другие тестовые коды при необходимости
    }

    return DEMO_DATA.get(ref_code)