import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_subscription_mode(config: Dict[str, Any]) -> bool:
    """Проверяет режим биллинга"""
    billing = config.get("billing") or {}
    return billing.get("mode", "subscription") == "subscription"


def user_period_key(user: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Stable key for a user billing period."""
    if is_subscription_mode(config):
        # В режиме подписки период привязан к дате сброса
        reset = billing_reset_iso(user)
        if reset:
            return reset[:19]
        return datetime.now().strftime("%Y-%m")
    return datetime.now().strftime("%Y-%m")


def user_period_dates(user: Dict[str, Any], config: Dict[str, Any]) -> Tuple[str, str]:
    """Даты начала и конца периода пользователя"""
    billing = config.get("billing") or {}
    
    if is_subscription_mode(config):
        # Подписка: +30 дней от lastTrafficResetAt или createdAt
        reset = billing_reset_iso(user) or user.get("createdAt")
        if reset:
            try:
                start_dt = _parse_datetime(reset)
            except (TypeError, ValueError):
                start_dt = datetime.now(timezone.utc)
        else:
            start_dt = datetime.now(timezone.utc)
        
        cycle_days = int(billing.get("subscription_cycle_days", 30))
        end_dt = start_dt + timedelta(days=cycle_days)
        return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")
    
    # Calendar mode: с 1-го числа до сегодня
    now = datetime.now()
    start = now.replace(day=1)
    return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


def billing_reset_iso(user: Dict[str, Any]) -> Optional[str]:
    """Возвращает lastTrafficResetAt в ISO формате"""
    reset = user.get("lastTrafficResetAt") or user.get("billing_reset_at")
    if reset and isinstance(reset, str):
        return reset.replace("Z", "+00:00")
    return None


def should_unblock_user(limited_info: Dict[str, Any], user: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """
    Проверяет, пора ли разблокировать пользователя.
    В режиме subscription: если прошёл цикл (30 дней от billing_reset_at).
    В режиме calendar: если период изменился.
    """
    if is_subscription_mode(config):
        billing = config.get("billing") or {}
        cycle_days = int(billing.get("subscription_cycle_days", 30))
        
        reset_str = billing_reset_iso(user) or limited_info.get("billing_reset_at")
        if not reset_str:
            return False
        
        try:
            reset_dt = _parse_datetime(reset_str)
            unblock_dt = reset_dt + timedelta(days=cycle_days)
            return datetime.now(timezone.utc) >= unblock_dt
        except (TypeError, ValueError):
            return False
    else:
        # Calendar mode: разблокировка при смене месяца
        current_period = datetime.now(timezone.utc).strftime("%Y-%m")
        limited_period = limited_info.get("period_key", "")
        return current_period != limited_period


def scan_window_dates(config: Dict[str, Any]) -> Tuple[str, str]:
    """Окно сканирования для проверки лимитов"""
    billing = config.get("billing") or {}
    
    if is_subscription_mode(config):
        # Для subscription сканируем последние 31 день (покрывает любой цикл)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=31)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    else:
        # Calendar: с 1-го числа текущего месяца
        now = datetime.now(timezone.utc)
        start = now.replace(day=1)
        return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
