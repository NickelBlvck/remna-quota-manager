from typing import Union


def format_money(amount: Union[int, float], currency: str = "₽") -> str:
    """Форматирует сумму с валютой"""
    if amount == 0:
        return f"0 {currency}"
    if amount < 1:
        return f"{amount:.2f} {currency}"
    if amount < 1000:
        return f"{amount:.1f} {currency}"
    return f"{int(amount):,} {currency}".replace(",", " ")


def node_cost_per_gb(node: dict, config: dict) -> float:
    """Возвращает стоимость 1 ГБ для ноды"""
    cost_cfg = config.get("traffic_cost") or {}
    return float(cost_cfg.get("price_per_gb", 0))