import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Загружает конфиг с дефолтами"""
    defaults = {
        "panel": {"base_url": "", "token": ""},
        "telegram": {
            "daily_summary_hour": 9,
            "daily_summary_window_minutes": 5
        },
        "actions_enabled": True,
        "dry_run": True,
        "limit_buffer_percent": 0,
        "limit_hysteresis_checks": 2,
        "check_interval_minutes": 10,
        "billing": {
            "mode": "subscription",
            "reset_day": 1,
            "subscription_cycle_days": 30
        },
        "bedolaga": {
            "base_url": "",
            "token": "",
            "timeout": 15,
            "username_template": "user_{telegram_id}",
            "fallback_unlinked": True
        },
        "traffic_cost": {"currency": "₽", "price_per_gb": 0}
        ,"nodes": {"auto_discover": False, "include_disabled": False, "policies": {}}
    }
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        # Merge с дефолтами
        for key, value in defaults.items():
            if key not in config:
                config[key] = value
            elif isinstance(value, dict):
                for k, v in value.items():
                    config[key].setdefault(k, v)
        _validate_config(config)
        logger.info("Config loaded from %s", path)
        return config
    except FileNotFoundError:
        logger.warning("Config not found at %s, using defaults", path)
        return defaults
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in config: %s", e)
        return defaults


def _validate_config(config: Dict[str, Any]) -> None:
    panel = config.get("panel") or {}
    if not panel.get("base_url") or not panel.get("token") or str(panel.get("token")).startswith("your_"):
        raise ValueError("panel.base_url and panel.token are required")
    telegram = config.get("telegram") or {}
    if not telegram.get("bot_token") or str(telegram.get("bot_token")).startswith("your_"):
        raise ValueError("telegram.bot_token is required")
    admin_ids = telegram.get("admin_user_ids") or []
    if not admin_ids:
        raise ValueError("telegram.admin_user_ids must contain at least one administrator")
    if config.get("dry_run") is False and not config.get("actions_enabled", True):
        logger.warning("actions_enabled=false overrides dry_run=false")
    billing = config.get("billing") or {}
    if int(billing.get("subscription_cycle_days", 30)) <= 0:
        raise ValueError("billing.subscription_cycle_days must be positive")
    mode = billing.get("mode", "subscription")
    if mode not in ("subscription", "calendar", "bedolaga"):
        raise ValueError("billing.mode must be one of: subscription, calendar, bedolaga")
    if mode == "bedolaga":
        bed = config.get("bedolaga") or {}
        if not bed.get("base_url") or not bed.get("token") or str(bed.get("token")).startswith("your_"):
            raise ValueError("billing.mode=bedolaga requires bedolaga.base_url and bedolaga.token")
    if int(config.get("check_interval_minutes", 10)) <= 0:
        raise ValueError("check_interval_minutes must be positive")
    nodes_cfg = config.get("nodes") or {}
    configured_nodes = config.get("monitored_nodes", [])
    policies = nodes_cfg.get("policies") or {}
    if nodes_cfg.get("auto_discover") and not configured_nodes and not policies:
        raise ValueError("nodes.policies must contain at least one node policy when auto_discover is enabled")
    for node in configured_nodes:
        if not node.get("uuid") or float(node.get("limit_gb", 0)) <= 0:
            raise ValueError("each monitored node requires uuid and positive limit_gb")
    for key, policy in policies.items():
        if not isinstance(policy, dict) or float(policy.get("limit_gb", 0)) <= 0:
            raise ValueError(f"node policy {key} requires positive limit_gb")
