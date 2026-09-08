import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def billing_mode(config: Dict[str, Any]) -> str:
    return (config.get("billing") or {}).get("mode", "subscription")


def is_bedolaga_mode(config: Dict[str, Any]) -> bool:
    return billing_mode(config) == "bedolaga"


def is_subscription_mode(config: Dict[str, Any]) -> bool:
    """subscription и bedolaga считают цикл одинаково (катящийся anchor);
    отличается только источник опорной даты и лимита."""
    return billing_mode(config) in ("subscription", "bedolaga")


def _cycle_days(config: Dict[str, Any]) -> int:
    billing = config.get("billing") or {}
    return max(1, int(billing.get("subscription_cycle_days", 30)))


def _cycle_base(user: Dict[str, Any]) -> Optional[datetime]:
    """Опорная дата цикла: lastTrafficResetAt, иначе createdAt."""
    base_str = (
        billing_reset_iso(user)
        or user.get("createdAt")
        or user.get("created_at")
        or user.get("subscription_start_date")
    )
    if not base_str or not isinstance(base_str, str):
        return None
    try:
        return _parse_datetime(base_str)
    except (TypeError, ValueError):
        return None


def cycle_anchor(user: Dict[str, Any], config: Dict[str, Any]) -> Optional[datetime]:
    """Начало ТЕКУЩЕГО квота-цикла юзера (UTC), либо None.

    Катится от опорной даты шагами по ``subscription_cycle_days``: окно всегда
    «сейчас минус остаток цикла», а не застревает на первой дате. Remnawave
    native-сброс (MONTHLY = 1-е число) двигает lastTrafficResetAt сам — тогда
    опорная дата и есть последний сброс, k=0.
    """
    base = _cycle_base(user)
    if base is None:
        return None
    days = _cycle_days(config)
    now = datetime.now(timezone.utc)
    if now <= base:
        return base
    k = (now - base).days // days
    return base + timedelta(days=k * days)


def user_period_key(user: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Стабильный ключ периода: меняется на каждом новом цикле."""
    if is_subscription_mode(config):
        anchor = cycle_anchor(user, config)
        if anchor:
            return anchor.strftime("%Y-%m-%dT%H:%M:%S")
        return datetime.now(timezone.utc).strftime("%Y-%m")
    return datetime.now(timezone.utc).strftime("%Y-%m")


def user_period_dates(user: Dict[str, Any], config: Dict[str, Any]) -> Tuple[str, str]:
    """Начало и конец текущего цикла пользователя (YYYY-MM-DD)."""
    if is_subscription_mode(config):
        anchor = cycle_anchor(user, config) or datetime.now(timezone.utc)
        end_dt = anchor + timedelta(days=_cycle_days(config))
        return anchor.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")

    now = datetime.now(timezone.utc)
    return now.replace(day=1).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


def billing_reset_iso(user: Dict[str, Any]) -> Optional[str]:
    """Возвращает lastTrafficResetAt в ISO формате"""
    reset = user.get("lastTrafficResetAt") or user.get("billing_reset_at")
    if reset and isinstance(reset, str):
        return reset.replace("Z", "+00:00")
    return None


def should_unblock_user(
    limited_info: Dict[str, Any], user: Dict[str, Any], config: Dict[str, Any]
) -> bool:
    """Пора ли разблокировать: прошёл ли цикл, в котором юзера ограничили.

    subscription: разблокировка через ``subscription_cycle_days`` от начала того
    цикла (period_key на момент лимита; фолбэк — billing_reset_at, затем сам
    юзер, затем limited_at). calendar: при смене месяца.
    """
    if is_subscription_mode(config):
        now = datetime.now(timezone.utc)
        limited_anchor: Optional[datetime] = None
        for candidate in (
            limited_info.get("period_key"),
            limited_info.get("billing_reset_at"),
        ):
            if candidate:
                try:
                    limited_anchor = _parse_datetime(candidate)
                    break
                except (TypeError, ValueError):
                    limited_anchor = None
        if limited_anchor is None:
            limited_anchor = _cycle_base(user)
        if limited_anchor is None and limited_info.get("limited_at"):
            try:
                limited_anchor = _parse_datetime(limited_info["limited_at"])
            except (TypeError, ValueError):
                limited_anchor = None
        if limited_anchor is None:
            return False
        return now >= limited_anchor + timedelta(days=_cycle_days(config))

    current_period = datetime.now(timezone.utc).strftime("%Y-%m")
    return current_period != limited_info.get("period_key", "")


def scan_window_dates(config: Dict[str, Any]) -> Tuple[str, str]:
    """Окно предфильтра для проверки лимитов (шире любого цикла)."""
    if is_subscription_mode(config):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(31, _cycle_days(config) + 1))
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    now = datetime.now(timezone.utc)
    return now.replace(day=1).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
