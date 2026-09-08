import html
import logging
import requests
from datetime import datetime

logger = logging.getLogger("notify")


def esc(value) -> str:
    """Escape a dynamic value (username, node name, ...) for Telegram HTML mode."""
    return html.escape("" if value is None else str(value), quote=False)

class TelegramNotifier:
    def __init__(self, config):
        self.config = config
        self.reload(config)

    def reload(self, config):
        tg = config.get("telegram", {})
        self.token = tg.get("bot_token")
        self.private_chat_id = tg.get("private_chat_id")
        self.alerts_chat_id = tg.get("alerts_chat_id")
        self.alerts_topic_id = tg.get("alerts_topic_id")
        self.summary_hour = int(tg.get("daily_summary_hour", 9))
        self.summary_window = int(tg.get("daily_summary_window_minutes", 5))

    def _send(self, chat_id, text, topic_id=None):
        if not self.token or not chat_id: return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if topic_id: payload["message_thread_id"] = int(topic_id)

        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                logger.error("Telegram send failed (%s): %s", r.status_code, r.text[:300])
                return False
            return True
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def send_limit_event(self, text):
        """Шлем уведомление о лимите (приват + канал)"""
        results = []
        if self.private_chat_id:
            results.append(self._send(self.private_chat_id, text))
        if self.alerts_chat_id:
            results.append(self._send(self.alerts_chat_id, text, self.alerts_topic_id))
        return any(results)

    def send_daily_summary(self, text):
        """Шлем суточный отчет"""
        if self._send(self.alerts_chat_id, text, self.alerts_topic_id):
            return True
        if self.private_chat_id and self._send(self.private_chat_id, text):
            return True
        return False

    def should_send_daily_summary(self, db):
        """Проверка времени отправки"""
        now = datetime.now()
        # Если уже отправили сегодня — выходим
        if db.get_setting("last_daily_summary_date") == now.strftime("%Y-%m-%d"):
            return False
        
        # Проверяем попадание в окно времени
        if now.hour == self.summary_hour and now.minute < self.summary_window:
            return True
        return False

    def mark_daily_summary_sent(self, db):
        db.set_setting("last_daily_summary_date", datetime.now().strftime("%Y-%m-%d"))
