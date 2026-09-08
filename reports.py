from datetime import datetime, timedelta, timezone

from notify import esc


def generate_daily_summary(api, config):
    nodes = config.get("monitored_nodes", [])
    lines = [
        "📊 <b>Daily Report</b>",
        f"<i>Period: {datetime.now().strftime('%Y-%m-%d')}</i>",
        ""
    ]

    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    day = yesterday.strftime("%Y-%m-%d")
    for node in nodes:
        name = node.get("name", node.get("uuid", "unknown"))
        stats = api.get_node_bandwidth_stats(node["uuid"], day, day, top_limit=10)
        total = stats.get("total_bytes")
        top = stats.get("topUsers", [])
        total_gb = total / (1024 ** 3) if total is not None else None
        lines.append(f"🌐 <b>{esc(name)}</b>")
        lines.append(f"Трафик: <code>{total_gb:.2f} GB</code>" if total_gb is not None else "Трафик: <code>нет данных</code>")
        if top:
            lines.append("Топ пользователей:")
            for user in top[:5]:
                username = user.get("username") or user.get("uuid", "unknown")[:8]
                traffic = int(user.get("total", 0)) / (1024 ** 3)
                lines.append(f"- <code>{esc(username)}</code>: <code>{traffic:.2f} GB</code>")
        lines.append("")
        
    return "\n".join(lines)
