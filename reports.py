from datetime import datetime, timedelta, timezone

from notify import esc

_NAME_W = 16  # username column width — keep the table narrow enough for mobile


def _fmt_table(rows, limit_gb) -> str:
    """rows: list of (username, gb). Returns a fixed-width monospace table."""
    head = f"{'user':<{_NAME_W}} {'GB':>8} {'%':>4}"
    out = [head, "-" * len(head)]
    for username, gb in rows:
        uname = (username or "?")[:_NAME_W]
        if limit_gb:
            pct = f"{gb / float(limit_gb) * 100:.0f}"
            mark = " !" if gb >= float(limit_gb) else ""
        else:
            pct, mark = "-", ""
        out.append(f"{uname:<{_NAME_W}} {gb:>8.2f} {pct:>4}{mark}")
    return "\n".join(out)


def generate_traffic_report(
    api, config, *, start=None, end=None, title="Traffic Report", top_n=10, max_chars=3800
):
    """Per-node traffic summary: total bandwidth + a top-users table per node.

    ``start``/``end`` are ``YYYY-MM-DD`` strings; omitted → "yesterday".
    Output is Telegram-HTML: one plain header line per node followed by a
    ``<pre>`` table. Assembled node-by-node and cut at a node boundary so the
    result never exceeds ``max_chars`` with a dangling tag ("!" marks a user
    at or above the node limit).
    """
    nodes = config.get("monitored_nodes", [])
    if not start or not end:
        yday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        start = end = yday

    out = f"📊 <b>{esc(title)}</b>\n<i>{esc(start)} → {esc(end)}</i>\n"
    truncated = False

    for node in nodes:
        name = node.get("name", node.get("uuid", "unknown"))
        limit_gb = node.get("limit_gb")
        stats = api.get_node_bandwidth_stats(node["uuid"], start, end, top_limit=top_n)
        total = stats.get("total_bytes")
        top = stats.get("topUsers", []) or []
        total_gb = total / (1024 ** 3) if total is not None else None

        header = f"\n🌐 <b>{esc(name)}</b>"
        if limit_gb:
            header += f" · лимит {esc(limit_gb)} GB"
        header += (
            f" · всего {total_gb:.1f} GB\n" if total_gb is not None else " · всего: нет данных\n"
        )

        rows = [
            (u.get("username") or (u.get("uuid") or "?")[:8], int(u.get("total", 0)) / (1024 ** 3))
            for u in top[:top_n]
        ]
        rows.sort(key=lambda r: r[1], reverse=True)
        chunk = header + (f"<pre>{esc(_fmt_table(rows, limit_gb))}</pre>\n" if rows else "")

        if len(out) + len(chunk) > max_chars:
            truncated = True
            break
        out += chunk

    if truncated:
        out += "\n<i>…список обрезан, часть нод не показана</i>"
    return out


def generate_daily_summary(api, config):
    return generate_traffic_report(api, config, title="Daily Report", top_n=5)
