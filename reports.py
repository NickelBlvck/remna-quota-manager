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
    api, config, *, days=30, start=None, end=None, title=None, top_n=10, max_chars=3800
):
    """Per-node traffic summary: total bandwidth + a top-users table per node.

    The window is a rolling ``days``-day range (default 30), NOT any user's
    billing cycle — the monitor enforces limits per personal cycle. ``%`` is
    the user's traffic over this window vs the node's monthly ``limit_gb``,
    ``!`` marks at/over the limit.

    Telegram-HTML output: one plain header line per node + a ``<pre>`` table,
    assembled node-by-node and cut at a node boundary to stay under
    ``max_chars`` without a dangling tag.
    """
    now = datetime.now(timezone.utc)
    start = start or (now - timedelta(days=days)).strftime("%Y-%m-%d")
    end = end or now.strftime("%Y-%m-%d")
    title = title or f"Трафик за {days} дн."
    fetch_n = max(top_n, 200)  # pull enough rows that the node total is meaningful

    nodes = config.get("monitored_nodes", [])
    out = f"📊 <b>{esc(title)}</b>\n<i>{esc(start)} → {esc(end)}</i>\n"
    truncated = False

    for node in nodes:
        name = node.get("name", node.get("uuid", "unknown"))
        limit_gb = node.get("limit_gb")
        stats = api.get_node_bandwidth_stats(node["uuid"], start, end, top_limit=fetch_n)
        top = stats.get("topUsers", []) or []
        total_bytes = stats.get("total_bytes")

        node_total_gb = None
        if total_bytes is not None:
            node_total_gb = total_bytes / (1024 ** 3)
        elif top:
            node_total_gb = sum(int(u.get("total", 0)) for u in top) / (1024 ** 3)

        header = f"\n🌐 <b>{esc(name)}</b>"
        if limit_gb:
            header += f" · лимит {esc(limit_gb)} GB"
        header += (
            f" · всего {node_total_gb:.1f} GB\n" if node_total_gb is not None
            else " · всего: нет данных\n"
        )

        rows = sorted(
            (
                (u.get("username") or (u.get("uuid") or "?")[:8], int(u.get("total", 0)) / (1024 ** 3))
                for u in top
            ),
            key=lambda r: r[1],
            reverse=True,
        )[:top_n]
        chunk = header + (f"<pre>{esc(_fmt_table(rows, limit_gb))}</pre>\n" if rows else "")

        if len(out) + len(chunk) > max_chars:
            truncated = True
            break
        out += chunk

    if truncated:
        out += "\n<i>…список обрезан, часть нод не показана</i>"
    out += (
        f"\n<i>Окно — последние {days} дн., не биллинговый цикл. "
        f"Лимиты монитор считает по личному циклу каждого юзера.</i>"
    )
    return out


def generate_daily_summary(api, config):
    return generate_traffic_report(api, config, days=1, title="Daily Report", top_n=5)
