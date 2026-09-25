"""
Конфигурация VPN магазина
"""

# Ценообразование VPN
VPN_BASE_PRICE = 100  # Базовая цена за 1 устройство (₽/месяц)
VPN_ADDITIONAL_DEVICE_PRICE = 40  # Цена за каждое дополнительное устройство (₽/месяц)

# Ограничения
VPN_MIN_DEVICES = 1
VPN_MAX_DEVICES = 15
VPN_MIN_DAYS = 14
VPN_MAX_DAYS = 120

# Время ограничения между заказами (секунды)
ORDER_COOLDOWN_SECONDS = 120  # 2 минуты

def calculate_vpn_price(device_count: int, period_days: int) -> float:
    """
    Рассчитать цену VPN подписки
    
    Args:
        device_count: количество устройств (1-15)
        period_days: период в днях (14-120)
    
    Returns:
        Итоговая цена в рублях
    """
    if device_count < VPN_MIN_DEVICES or device_count > VPN_MAX_DEVICES:
        raise ValueError(f"Количество устройств должно быть от {VPN_MIN_DEVICES} до {VPN_MAX_DEVICES}")
    
    if period_days < VPN_MIN_DAYS or period_days > VPN_MAX_DAYS:
        raise ValueError(f"Период должен быть от {VPN_MIN_DAYS} до {VPN_MAX_DAYS} дней")
    
    # Базовая цена за месяц (30 дней)
    base_monthly = VPN_BASE_PRICE
    if device_count > 1:
        base_monthly += (device_count - 1) * VPN_ADDITIONAL_DEVICE_PRICE
    
    # Пропорциональный расчет по дням
    total_price = (base_monthly / 30) * period_days
    
    return round(total_price, 2)

# Предустановленные вопросы безопасности
SECURITY_QUESTIONS = [
    "Название вашей первой школы",
    "Девичья фамилия матери",
    "Кличка первого домашнего животного",
    "Город рождения",
    "Любимое блюдо в детстве",
    "Имя лучшего друга детства"
]
