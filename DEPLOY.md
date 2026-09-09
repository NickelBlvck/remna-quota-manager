# Деплой через Docker

Проект — один долгоживущий процесс (Telegram-бот + поток монитора). Веб-порты не
нужны. Состояние — SQLite в `/app/data` (docker volume `quota-data`). Секреты — в
`config.json`, монтируется снаружи, в образ **не попадает**.

---

## TL;DR (сервер)

```bash
mkdir -p /opt/remna-quota && cd /opt/remna-quota
curl -O https://raw.githubusercontent.com/NickelBlvck/remna-quota-manager/master/docker-compose.yml
curl -o config.json https://raw.githubusercontent.com/NickelBlvck/remna-quota-manager/master/config.sample.json
nano config.json                     # токены, admin_user_ids, ноды
docker compose pull
docker compose up -d
docker compose logs -f
```

Обновление потом — всегда:

```bash
docker compose pull && docker compose up -d
```

---

## 1. Подготовка репозитория (один раз)

Локальный репозиторий сейчас только на твоей машине. Чтобы образ собирался сам:

```bash
cd C:/Users/r3dix0r/Desktop/vpn/Repo/remna-quota-manager
git branch -m master main            # опционально: сделать main основной
gh repo create remna-quota-manager --private --source=. --push
# или вручную:
#   git remote add origin git@github.com:NickelBlvck/remna-quota-manager.git
#   git push -u origin master
```

После пуша GitHub Actions (`.github/workflows/docker-publish.yml`):
1. гоняет юнит-тесты;
2. собирает образ;
3. пушит в **GHCR**: `ghcr.io/nickelblvck/remna-quota-manager`
   - `:latest` — с ветки по умолчанию
   - `:1.2.3`, `:1.2` — с git-тега `v1.2.3`
   - `:sha-abc1234` — с каждого коммита

> Логин в `github.com/<repo>` (например `NickelBlvck/remna-quota-manager`) → образ
> будет `ghcr.io/nickelblvck/...` (нижний регистр). Поправь одну строку `image:` в
> `docker-compose.yml`, если логин другой.

### Доступ к образу с сервера

GHCR-пакеты по умолчанию **приватные**. Варианты:

**A. Сделать пакет публичным** (проще всего для одного своего сервиса):
GitHub → твой профиль → Packages → `remna-quota-manager` → Package settings →
Change visibility → Public. Тогда `docker pull` работает без логина.

**B. Оставить приватным**, на сервере логиниться токеном:
GitHub → Settings → Developer settings → Personal access tokens → Fine-grained →
права `read:packages`. Потом на сервере:

```bash
echo <PAT> | docker login ghcr.io -u NickelBlvck --password-stdin
```

Для ресейла (много клиентских серверов) — вариант B + отдельный
read-only PAT на клиента, или свой приватный Registry / Harbor.

---

## 2. Первый запуск на сервере

```bash
mkdir -p /opt/remna-quota && cd /opt/remna-quota
```

**`docker-compose.yml`** — скачай из репо (`curl -O …/docker-compose.yml`) или
скопируй. Проверь строку `image:` и `TZ`.

**`config.json`** — ОБЯЗАТЕЛЬНО создай ДО `up`. Если файла нет, Docker создаст на
его месте каталог, и контейнер упадёт на разборе конфига.

```bash
curl -o config.json https://raw.githubusercontent.com/NickelBlvck/remna-quota-manager/master/config.sample.json
nano config.json
```

Минимум в конфиге: `panel.base_url` + `panel.token`, `telegram.bot_token` +
`telegram.admin_user_ids`, ноды (`nodes.policies` или `monitored_nodes`).
Держи `"dry_run": true` и `"enforcement_mode": "manual"` на старте.

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

В логе должно быть:
`✅ Database initialized` → `✅ API Client initialized` →
`⚠️ DRY-RUN MODE ENABLED` → `Monitor loop started` → `🤖 Bot starting polling`.

---

## 3. Эксплуатация

```bash
docker compose logs -f --tail=100 quota-manager

# read-only проверка "кого залочит по циклу"
docker compose exec quota-manager python db_tool.py check
docker compose exec quota-manager python db_tool.py stats
docker compose exec quota-manager python db_tool.py list

# переключить dry-run без рестарта (монитор читает БД каждый цикл)
docker compose exec quota-manager \
  sqlite3 /app/data/quota.db \
  "INSERT OR REPLACE INTO kv_settings(key,value) VALUES('dry_run','false');"

# режим применения
docker compose exec quota-manager \
  sqlite3 /app/data/quota.db \
  "INSERT OR REPLACE INTO kv_settings(key,value) VALUES('enforcement_mode','auto');"

# статус контейнера (healthcheck = свежесть heartbeat монитора)
docker compose ps
```

После правки `config.json`:

```bash
docker compose restart quota-manager
```

---

## 4. Бэкапы БД

Volume `quota-data`. Разовый бэкап:

```bash
docker compose exec quota-manager \
  sqlite3 /app/data/quota.db ".backup '/app/data/backup.db'"
docker compose cp quota-manager:/app/data/backup.db ./quota-$(date +%F).db
```

Cron на хосте:

```cron
17 4 * * * cd /opt/remna-quota && docker compose exec -T quota-manager sqlite3 /app/data/quota.db ".backup '/app/data/backup.db'" && docker compose cp quota-manager:/app/data/backup.db /backup/quota-$(date +\%F).db
```

---

## 5. Автообновление (опционально)

`docker compose pull && docker compose up -d` в cron, либо Watchtower:

```yaml
  watchtower:
    image: containrrr/watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 3600 --cleanup --scope quota
    labels:
      com.centurylinklabs.watchtower.scope: quota
```

и добавь контейнеру `quota-manager`:
`labels: { com.centurylinklabs.watchtower.scope: quota }`.

---

## 6. Сборка образа локально (без CI)

```bash
docker build -t ghcr.io/nickelblvck/remna-quota-manager:latest .
echo <PAT> | docker login ghcr.io -u NickelBlvck --password-stdin
docker push ghcr.io/nickelblvck/remna-quota-manager:latest
```

Только запустить локально, без пуша:

```bash
docker build -t remna-quota-manager:dev .
docker run --rm -it \
  -v "$PWD/config.json:/app/config.json:ro" \
  -v quota-data:/app/data \
  remna-quota-manager:dev
```

---

## 7. Bedolaga на том же хосте

Если `billing.mode: "bedolaga"` и API Bedolaga доступен только во внутренней сети
его compose:

```bash
docker network ls | grep bedolaga     # узнать имя сети, напр. bedolaga_default
```

В `docker-compose.yml` раскомментируй блок `networks` (и внизу `external: true`),
в `config.json` укажи `"base_url": "http://<имя-сервиса-bedolaga>:8080"`.

Если API Bedolaga проброшен на `127.0.0.1:8080` хоста — проще добавить контейнеру:

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

и `"base_url": "http://host.docker.internal:8080"`.
