from datetime import datetime, timedelta, timezone

from notify import esc


def generate_traffic_report(api, config, *, start=None, end=None, title="Traffic Report", top_n=10):
    """Per-node traffic summary: total bandwidth + top users for a date range.

    ``start``/``end`` are ``YYYY-MM-DD`` strings; when omitted the range is
    "yesterday" (the daily-summary behaviour).
    """
    nodes = config.get("monitored_nodes", [])
    if not start or not end:
        yday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        start = end = yday

    lines = [f"📊 <b>{esc(title)}</b>", f"<i>{esc(start)} → {esc(end)}</i>", ""]

    for node in nodes:
        name = node.get("name", node.get("uuid", "unknown"))
        limit_gb = node.get("limit_gb")
        stats = api.get_node_bandwidth_stats(node["uuid"], start, end, top_limit=top_n)
        total = stats.get("total_bytes")
        top = stats.get("topUsers", [])
        total_gb = total / (1024 ** 3) if total is not None else None

        header = f"🌐 <b>{esc(name)}</b>"
        if limit_gb:
            header += f" · лимит {esc(limit_gb)} GB"
        lines.append(header)
        lines.append(
            f"Всего: <code>{total_gb:.2f} GB</code>" if total_gb is not None
            else "Всего: <code>нет данных</code>"
        )
        if top:
            lines.append("Топ пользователей:")
            for user in top[:top_n]:
                username = user.get("username") or user.get("uuid", "unknown")[:8]
                traffic = int(user.get("total", 0)) / (1024 ** 3)
                flag = " ⚠️" if limit_gb and traffic >= float(limit_gb) else ""
                lines.append(f"- <code>{esc(username)}</code>: <code>{traffic:.2f} GB</code>{flag}")
        lines.append("")

    return "\n".join(lines)


def generate_daily_summary(api, config):
    return generate_traffic_report(api, config, title="Daily Report", top_n=5)
