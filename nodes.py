import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def resolve_monitored_nodes(api, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the list of monitored nodes.

    Without ``nodes.auto_discover`` this is just ``config["monitored_nodes"]``.
    With auto-discovery the panel node list is merged with the local quota
    policies (``nodes.policies`` keyed by node uuid or name, legacy
    ``monitored_nodes`` entries as a fallback). Nodes without a policy — and
    disabled nodes unless ``include_disabled`` is set — are skipped.

    Shared by the monitor loop, the Telegram bot and ``db_tool.py`` so manual
    unblock resolves squads the same way the monitor does.
    """
    nodes_cfg = config.get("nodes") or {}
    configured = config.get("monitored_nodes", []) or []

    if not nodes_cfg.get("auto_discover"):
        return configured

    discovered = api.get_nodes() if api is not None else []
    if not discovered:
        logger.error("Node discovery returned no nodes; keeping configured nodes")
        return configured

    policies = nodes_cfg.get("policies") or {}
    legacy = {str(n.get("uuid")): n for n in configured}
    legacy.update({str(n.get("name")): n for n in configured})

    result: List[Dict[str, Any]] = []
    for remote in discovered:
        uuid = str(remote.get("uuid") or "")
        name = str(remote.get("name") or uuid)
        if not uuid:
            continue
        if remote.get("isDisabled") and not nodes_cfg.get("include_disabled", False):
            continue
        policy = (
            policies.get(uuid)
            or policies.get(name)
            or legacy.get(uuid)
            or legacy.get(name)
        )
        if not policy:
            logger.warning("Discovered node %s (%s) has no local policy; skipping", name, uuid)
            continue
        merged = dict(remote)
        merged.update(policy)
        merged["uuid"] = uuid
        merged["name"] = name
        result.append(merged)

    logger.info("Resolved %s monitored nodes", len(result))
    return result
