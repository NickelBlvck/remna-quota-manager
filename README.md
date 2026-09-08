# Remnawave Quota Manager — README

> 📋 **Контекст создания**: Проект разрабатывался для VPN-сервиса **POZOR.PW** на базе Remnawave Panel + Bedolaga Bot. Главная задача — автоматическое ограничение пользователей при превышении трафика через переключение **внешних сквадов** (Mihomo конфиги), с персональным циклом разблокировки и уведомлениями в Telegram.

---

## 🎯 Зачем это нужно

| Проблема | Решение |
|----------|---------|
| Пользователь превысил лимит → продолжает тратить трафик | Автоматическое переключение в `limited_external_squad` с ограниченным доступом |
| Все разблокируются 1-го числа → несправедливо для подключившихся в середине месяца | **Персональный цикл**: +30 дней от `lastTrafficResetAt` каждого пользователя |
| Ложные срабатывания из-за скачков трафика | **Hysteresis**: нужно 2 подтверждения подряд перед применением лимита |
| Нельзя тестировать на боевых юзерах | **Dry-run режим**: логирование без реальных действий |
| Нужно видеть, кто и когда был ограничен | **SQLite аудит**: полная история действий + CLI-утилита `db_tool.py` |
| Отчёты приходят в неудобное время | **Гибкое расписание**: окно отправки (например, 09:00–09:05) |

---

## 🏗 Архитектура

```
/opt/remna-quota-manager/
├── main.py                 # Точка входа: инициализация, запуск потоков
├── database.py             # SQLite: limited_users, pending_limits, audit_log, settings
├── remnawave.py            # API-клиент: только рабочие bulk-эндпоинты, external squads only
├── nodes.py                # Резолв monitored_nodes: auto-discover + merge policies (общий для monitor/bot/db_tool)
├── monitor.py              # Ядро: проверка лимитов, верификация, разблокировка
├── bot.py                  # Telegram: команды, inline-кнопки, ручная разблокировка
├── notify.py               # Уведомления: тайминг отчётов, fallback на приват при ошибке топика
├── reports.py              # Генерация текста суточного отчёта
├── billing.py              # Логика периодов: subscription vs calendar mode
├── settings.py             # Загрузка config.json с дефолтами
├── utils_cost.py           # Форматирование стоимости трафика
├── db_tool.py              # CLI: list/unblock/audit/export для БД
├── config.json             # Конфигурация (шаблон в репо)
├── requirements.txt        # Зависимости Python
└── remna-quota.service     # systemd unit для автозапуска
```

---

## 🔑 Ключевые архитектурные решения

### 1. Только внешние сквады (`external-squads`)
**Почему**: Внутренние сквады Remnawave не позволяют гибко управлять Mihomo-маршрутизацией. Внешние сквады — это точка, где подменяется конфиг клиента.

**Реализация**:
- `remnawave.py`: метод `set_user_external_squad()` — единственный способ изменения квот
- `monitor.py`: игнорирует `internal_squad_uuid`, работает только с `limited_external_squad_uuid`
- Конфиг: поля `full_external_squad_uuid` / `limited_external_squad_uuid`

### 2. SQLite вместо `config.json` для состояния
**Почему**: 
- ❌ `config.json`: race conditions при параллельной записи, нет истории, сложно искать
- ✅ SQLite: транзакции, индексы, аудит, CLI-доступ, миграции

**Таблицы**:
```sql
limited_users    -- кто ограничен, когда, на какой период
pending_limits   -- верификация: храним счётчик подтверждений
audit_log        -- полная история действий (кто, когда, почему)
kv_settings      -- флаги типа dry_run, last_daily_summary_date
whitelist        -- UUID, которых никогда не лимитить
```

### 3. Персональный цикл разблокировки (+30 дней)
**Почему**: Глобальный сброс 1-го числа создаёт дисбаланс: подключившийся 25-го числа получает ~35 дней, а подключившийся 5-го — ~25.

**Логика** (`billing.py`):
```python
# Для каждого пользователя:
reset_date = user['lastTrafficResetAt'] or user['createdAt']
unblock_date = reset_date + timedelta(days=30)  # или subscription_cycle_days из конфига
```

**Хранение**: `billing_reset_at` в таблице `limited_users` → точная привязка к циклу.

### 4. Верификация лимита (hysteresis)
**Почему**: Скачки трафика, баги сбора статистики, временные пики — не должны приводить к блокировке.

**Реализация** (`monitor.py`):
```python
# Пользователь превысил лимит → записываем в pending_limits
checks = db.bump_pending(...)  # returns 1, 2, 3...
if checks < required_checks:   # обычно 2
    # Ждём следующего цикла, не блокируем
    notify_verification_pending(...)
else:
    # Подтверждено → применяем лимит
    api.set_user_external_squad(...)
```

### 5. Dry-run режим
**Почему**: Безопасное тестирование на боевых данных без риска заблокировать реальных пользователей.

**Реализация**:
- Флаг в БД: `kv_settings.dry_run = 'true'`
- `monitor.py`: если dry_run → логирование + уведомление, но **нет вызова API**
- Уведомления помечаются: `🧪 dry-run: сквад не менялся`

---

## ⚙️ Конфигурация (`config.json`)

```json
{
  "panel": {
    "base_url": "https://api.pozor.pw",
    "token": "your_token"
  },
  "telegram": {
    "bot_token": "bot_token",
    "private_chat_id": 123456789,
    "alerts_chat_id": -1009876543210,
    "alerts_topic_id": 55,
    "admin_user_ids": [123456789],
    "daily_summary_hour": 9,
    "daily_summary_window_minutes": 5
  },
  "monitored_nodes": [{
    "uuid": "node_uuid",
    "name": "moscow-yandex",
    "limit_gb": 50,
    "full_external_squad_uuid": "uuid_full",
    "limited_external_squad_uuid": "uuid_limited"
  }],
  "billing": {
    "mode": "subscription",
    "subscription_cycle_days": 30
  },
  "dry_run": true,
  "limit_hysteresis_checks": 2,
  "check_interval_minutes": 10
}
```

### Несколько серверов

Каждый сервер указывается отдельным объектом внутри массива `monitored_nodes`.
Для каждой ноды нужны собственные `uuid`, `limit_gb`, `full_external_squad_uuid` и
`limited_external_squad_uuid`:

```json
{
  "monitored_nodes": [
    {
      "uuid": "node-uuid-1",
      "name": "moscow-yandex",
      "limit_gb": 50,
      "full_external_squad_uuid": "full-squad-uuid-1",
      "limited_external_squad_uuid": "limited-squad-uuid-1"
    },
    {
      "uuid": "node-uuid-2",
      "name": "amsterdam-hetzner",
      "limit_gb": 40,
      "full_external_squad_uuid": "full-squad-uuid-2",
      "limited_external_squad_uuid": "limited-squad-uuid-2"
    }
  ]
}
```

Монитор проверяет каждую ноду отдельно. Лимит считается отдельно по паре
`пользователь + нода + период`, поэтому один пользователь может быть ограничен
на одной ноде и продолжать пользоваться другой. При разблокировке full external
squad выбирается из той же ноды, в которой было применено ограничение.

Готовый шаблон с несколькими серверами находится в `config.sample.json`.

### Автоматическое получение нод из панели

Панель возвращает список нод через `GET /api/nodes`. Чтобы не дублировать в
конфиге UUID и названия серверов, включите автодискавери:

```json
{
  "nodes": {
    "auto_discover": true,
    "include_disabled": false,
    "policies": {
      "d83977f2-d88e-4394-821d-4bce64dc7ef9": {
        "limit_gb": 50,
        "full_external_squad_uuid": "full-squad-uuid",
        "limited_external_squad_uuid": "limited-squad-uuid"
      }
    }
  }
}
```

При включенном `auto_discover` проект получает из панели `uuid`, `name` и
статус ноды. В `nodes.policies` нужно оставить только правила quota manager:
лимит и две external squads. Нода без policy пропускается и не получает
ограничения. Отключенная нода пропускается при `include_disabled: false`.

> ⚠️ `telegram.admin_user_ids` — **обязательное** поле (список числовых Telegram ID).
> Без него `settings._validate_config` не пропустит запуск. Бот отвечает только
> этим пользователям.

| Параметр | Зачем | Значение по умолчанию |
|----------|-------|----------------------|
| `dry_run` | Тестовый режим: без реальных действий | `true` |
| `limit_hysteresis_checks` | Сколько раз подряд должно превысить лимит | `2` |
| `daily_summary_window_minutes` | Окно отправки отчёта (мин) | `5` |
| `subscription_cycle_days` | Длина цикла подписки (дней) | `30` |

---

## 🚀 Быстрый старт

```bash
# 1. Клонировать/создать структуру
cd /opt && mkdir -p remna-quota-manager/data && cd remna-quota-manager

# 2. Создать файлы (скопируй из репо)
# main.py, database.py, remnawave.py, monitor.py, bot.py, notify.py,
# nodes.py, reports.py, billing.py, settings.py, utils_cost.py, db_tool.py,
# config.json, requirements.txt, remna-quota.service

# 3. Установить зависимости в venv (Debian 12+/PEP 668 — системный pip заблокирован)
sudo apt install -y python3-venv python3-full
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 4. Настроить конфиг
nano config.json  # вставить токены, UUID сквадов, chat_id

# 5. Отдельный системный пользователь + права
sudo useradd --system --no-create-home --shell /usr/sbin/nologin remna-quota
sudo mkdir -p /opt/remna-quota-manager/data   # обязан существовать до старта (ReadWritePaths= в юните)
sudo chown -R remna-quota:remna-quota /opt/remna-quota-manager
sudo chmod 750 /opt/remna-quota-manager/data

# 6. Установить systemd
sudo cp remna-quota.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable remna-quota.service

# 7. Запустить
sudo systemctl start remna-quota.service

# 8. Проверить
sudo journalctl -u remna-quota.service -f --no-pager | grep -E "✅|🚨|Monitor"
```

---

## 🛠 Управление

### CLI-утилита `db_tool.py`
```bash
cd /opt/remna-quota-manager   # рядом должна быть data/quota.db и config.json

.venv/bin/python db_tool.py list          # показать ограниченных
.venv/bin/python db_tool.py stats         # статистика
.venv/bin/python db_tool.py check         # кто превышает лимит по своему циклу (read-only прогон enforcement)
.venv/bin/python db_tool.py unblock <uuid># разблокировать вручную
.venv/bin/python db_tool.py audit 20      # последние действия
.venv/bin/python db_tool.py export        # экспорт в JSON
```

### Telegram-бот
```
/start            — главное меню (только для telegram.admin_user_ids)
📊 Статус         — dry-run флаг, кол-во limited/pending
📈 Отчёт          — трафик по нодам за последние N дн. + топ-10 юзеров (моноширинная таблица, `!` = на лимите/выше)
🎯 Проверка циклов — кто превышает лимит ПО СВОЕМУ циклу (то, что реально решает монитор): verdict over/`N/M`/WL/LIMITED
👥 Ограниченные   — список + кнопки разблокировки
🔓 Разблокировать — тот же список ограниченных
🛡 Whitelist      — просмотр + добавление UUID-исключений
📋 Аудит          — последние 10 действий
```

Кнопка `📈 Отчёт` строит статистику по требованию за **скользящее окно**
`subscription_cycle_days` (по умолчанию 30 дн.) — это НЕ личный биллинговый цикл
юзера. Лимиты монитор применяет по циклу каждого (`billing.user_period_dates`),
поэтому `%` в отчёте — оценочный. Тот же отчёт за прошедшие сутки монитор шлёт
автоматически в окно `daily_summary_hour`.

Данные берутся из `POST /api/bandwidth-stats/nodes/users` (тело `{"nodesUuids":[uuid]}`,
`start`/`end` в формате `YYYY-MM-DD`). Ответ: `topUsers[].total` в байтах, поля с
общим итогом нет — «всего» считается суммой `sparklineData` либо топа.

### Переключение dry-run
```bash
# Через БД (без перезапуска)
sqlite3 data/quota.db "INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('dry_run', 'false');"

# Или через конфиг + перезапуск
sudo nano config.json  # dry_run: false
sudo systemctl restart remna-quota.service
```

---

## 🐛 Типовые проблемы

| Симптом | Причина | Решение |
|---------|---------|---------|
| `error: externally-managed-environment` при `pip install` | Debian 12+/PEP 668 | Ставить в venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| `sqlite3.OperationalError: attempt to write a readonly database` | Права на папку `data/` | `sudo chown -R remna-quota:remna-quota data/` |
| `status=203/EXEC` у сервиса | В `ExecStart` неверный путь к python venv | Проверь, что `/opt/remna-quota-manager/.venv/bin/python` существует |
| `status=226/NAMESPACE`, `Failed to set up mount namespacing: .../data` | Каталог `data/` не существует, а он указан в `ReadWritePaths=` | `sudo mkdir -p /opt/remna-quota-manager/data && sudo chown remna-quota: /opt/remna-quota-manager/data` |
| `API 404` на `/api/bandwidth-stats/...` | Per-user эндпоинта нет; актуальный — `POST /api/bandwidth-stats/nodes/users` с телом `{"nodesUuids":[...]}` | `get_node_bandwidth_stats()` уже шлёт POST + fallback на legacy GET |
| В отчёте «всего: нет данных», но топ есть | Панель не отдаёт итог в этом ответе | Норма — «всего» досчитывается из `sparklineData`/топа; на суть не влияет |
| Числа в `📈 Отчёт` кратно больше, чем в панели | Окно отчёта = `subscription_cycle_days` (напр. 30 дн.), а в панели выбран 1 день | Сравнивать за одинаковый период |
| Отчёт не приходит | Неправильное окно времени | Проверить `daily_summary_hour` и `window_minutes` |
| Пользователь не разблокируется | `billing_reset_at` не сохранён | Убедиться, что `add_limited()` передаёт `billing_reset_at` |
| Дубли уведомлений | `should_send_daily_summary` без окна | Использовать проверку `minute < window` |

---

## 🗓 Roadmap (не реализовано, но обсуждалось)

| Фича | Статус | Зачем |
|------|--------|-------|
| ✅ Внешние сквады только | Готово | Управление Mihomo конфигами |
| ✅ Персональный цикл разблокировки | Готово | Справедливый billing |
| ✅ SQLite + аудит | Готово | Надёжность + отладка |
| ✅ Dry-run режим | Готово | Безопасное тестирование |
| ✅ Верификация лимита (hysteresis) | Готово | Защита от ложных срабатываний |
| ⏳ Подтверждение лимита через бота | В плане | Inline-кнопки: `[🔒 Залочить] [✅ Пропустить]` |
| ⏳ Предупреждения 50%/90% | В плане | Уведомлять ДО достижения лимита |
| ⏳ Отдельный скрипт отчётов | В плане | Вынести из основного цикла, гибкое расписание |
| ⏳ Bedolaga Bot API интеграция | В плане | Отправка предупреждений через основной бот проекта |

---

## 💡 Советы по развитию

1. **Добавь метрики**: Экспортируй в Prometheus ключевые события (`quota_limited_total`, `quota_unblocked_total`) — удобно для мониторинга.
2. **Тестовые юзеры**: Создай аккаунты с префиксом `test_` и фильтруй их в `monitor.py` — безопасно для отладки.
3. **Ротация логов**: Настрой `logrotate` для `/var/log/remna-quota/`, чтобы логи не раздувались.
4. **Бэкапы БД**: Добавь cron-задачу: `sqlite3 quota.db ".backup '/backup/quota-$(date +%F).db'"`.

---

> 📬 **Поддержку и вопросы** — в чат разработки POZOR.PW.  
> 🔄 **Обновления** — следите за тегами в репозитории.  
> 🔐 **Безопасность**: Никогда не коммитьте `config.json` с токенами в публичный репо!

*Документ создан на основе диалога разработки, май 2026. Контекст сохранён для будущих участников команды.* 🛠✨
