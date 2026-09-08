#!/usr/bin/env python3
import logging
import sys
import os
import threading
import time

from database import QuotaDatabase
from remnawave import RemnawaveAPI
from monitor import TrafficMonitor
from bot import QuotaBot
from settings import load_config

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("main")

def main():
    logger.info("🚀 Starting Remnawave Quota Manager...")
    
    try:
        # 1. Загрузка конфига
        config = load_config()
        
        # 2. Инициализация БД
        db = QuotaDatabase()
        logger.info("✅ Database initialized at %s", db.db_path)
        
        # 3. Миграция из старого config.json (если есть).
        # Идемпотентно (INSERT OR IGNORE), поэтому безопасно на каждом старте;
        # исходные поля в config.json не трогаются — можно удалить вручную после
        # первого успешного запуска.
        if config.get("whitelist") or config.get("limited_users"):
            logger.info("Migrating legacy whitelist/limited_users from config.json...")
            db.migrate_from_config(config)

        # 4. API Клиент
        panel_cfg = config.get("panel", {})
        if not panel_cfg.get("token"):
            logger.error("❌ No API token configured!")
            sys.exit(1)
            
        api = RemnawaveAPI(
            base_url=panel_cfg["base_url"],
            token=panel_cfg["token"]
        )
        logger.info("✅ API Client initialized")

        # 5. Проверка Dry-Run
        if db.is_dry_run(config.get("dry_run", True)):
            logger.warning("⚠️ DRY-RUN MODE ENABLED: No actions will be performed on squads.")

        # 6. Инициализация компонентов
        bot = QuotaBot(config.get("telegram", {}).get("bot_token"), api_client=api, db=db, config=config)
        monitor = TrafficMonitor(api, db, config)
        
        # 7. Запуск монитора в отдельном потоке
        monitor_thread = threading.Thread(target=monitor.run_loop, daemon=True, name="MonitorLoop")
        monitor_thread.start()
        logger.info("🔄 Monitor thread started (interval %s min)", config.get("check_interval_minutes", 10))
        
        # 8. Запуск бота (блокирующий вызов)
        bot.run()
        
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    main()