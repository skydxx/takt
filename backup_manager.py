"""
Менеджер бэкапов чатов
Автоматическое сохранение истории чатов без уведомлений
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from utils import safe_path_within

logger = logging.getLogger(__name__)


class BackupManager:
    def __init__(self, bot, database, backup_dir="/opt/salarybot/backups"):
        self.bot = bot
        self.database = database
        self.backup_dir = backup_dir
        
    async def create_daily_backup(self):
        """Создание ежедневного бэкапа всех чатов"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            backup_path = Path(self.backup_dir) / today
            backup_path.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"📁 Начинаю бэкап чатов в {backup_path}")
            
            # Получаем список всех чатов из БД
            chats = await self.database.get_all_chats()
            
            backed_up = 0
            failed = 0
            
            for chat in chats:
                chat_id = chat['chat_id']
                chat_name = chat.get('chat_name', f'chat_{chat_id}')
                
                try:
                    # Получаем всю статистику чата
                    stats = await self.database.get_chat_full_stats(chat_id)
                    
                    # Сохраняем в JSON
                    backup_data = {
                        'chat_id': chat_id,
                        'chat_name': chat_name,
                        'backup_date': today,
                        'backup_timestamp': datetime.now().isoformat(),
                        'statistics': stats
                    }
                    
                    # Безопасное имя файла
                    safe_name = "".join(c for c in chat_name if c.isalnum() or c in (' ', '-', '_')).strip()
                    file_name = f"{safe_name}_{chat_id}.json"
                    file_path = backup_path / file_name
                    
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(backup_data, f, ensure_ascii=False, indent=2)
                    
                    backed_up += 1
                    logger.info(f"✅ Бэкап чата '{chat_name}' создан")
                    
                except Exception as e:
                    failed += 1
                    logger.error(f"❌ Ошибка бэкапа чата {chat_id}: {e}")
                    continue
            
            # Сохраняем мета-информацию о бэкапе
            meta_data = {
                'date': today,
                'timestamp': datetime.now().isoformat(),
                'total_chats': len(chats),
                'backed_up': backed_up,
                'failed': failed
            }
            
            with open(backup_path / '_meta.json', 'w', encoding='utf-8') as f:
                json.dump(meta_data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"🎉 Бэкап завершен: {backed_up} успешно, {failed} ошибок")
            
            # Удаляем старые бэкапы
            await self.cleanup_old_backups()
            
            return backed_up, failed
            
        except Exception as e:
            logger.error(f"❌ Критическая ошибка при создании бэкапа: {e}")
            raise
    
    async def cleanup_old_backups(self, days_to_keep=7):
        """Удаление бэкапов старше N дней"""
        try:
            backup_base = Path(self.backup_dir)
            if not backup_base.exists():
                return
            
            cutoff_date = datetime.now() - timedelta(days=days_to_keep)
            deleted = 0
            
            for item in backup_base.iterdir():
                if not item.is_dir():
                    continue
                
                try:
                    # Проверяем дату по имени папки (YYYY-MM-DD)
                    folder_date = datetime.strptime(item.name, "%Y-%m-%d")
                    
                    if folder_date < cutoff_date:
                        # Удаляем всю папку с бэкапом
                        import shutil
                        shutil.rmtree(item)
                        deleted += 1
                        logger.info(f"🗑️ Удален старый бэкап: {item.name}")
                        
                except ValueError:
                    # Неправильный формат имени папки, пропускаем
                    continue
            
            if deleted > 0:
                logger.info(f"🧹 Очистка завершена: удалено {deleted} старых бэкапов")
                
        except Exception as e:
            logger.error(f"❌ Ошибка при очистке старых бэкапов: {e}")
    
    async def get_backup_list(self):
        """Получить список доступных бэкапов"""
        try:
            backup_base = Path(self.backup_dir)
            if not backup_base.exists():
                return []
            
            backups = []
            for item in backup_base.iterdir():
                if not item.is_dir():
                    continue
                
                meta_file = item / '_meta.json'
                if meta_file.exists():
                    with open(meta_file, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                        meta['folder'] = item.name
                        backups.append(meta)
            
            # Сортируем по дате (новые первые)
            backups.sort(key=lambda x: x['date'], reverse=True)
            return backups
            
        except Exception as e:
            logger.error(f"❌ Ошибка при получении списка бэкапов: {e}")
            return []
    
    async def get_backup_chats(self, backup_date):
        """Получить список чатов в бэкапе"""
        try:
            backup_path = safe_path_within(self.backup_dir, backup_date)
            if backup_path is None or not backup_path.exists():
                return []
            
            chats = []
            for file in backup_path.iterdir():
                if file.name.startswith('_') or not file.name.endswith('.json'):
                    continue

                # backup_path itself was validated above, but each entry is
                # re-checked individually: a symlink placed inside the date
                # folder (file.name) could otherwise resolve outside
                # backup_dir even though the containing directory is safe.
                safe_file = safe_path_within(self.backup_dir, backup_date, file.name)
                if safe_file is None or not safe_file.is_file():
                    continue

                with open(safe_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    chats.append({
                        'chat_id': data['chat_id'],
                        'chat_name': data['chat_name'],
                        'file_name': file.name
                    })
            
            return chats
            
        except Exception as e:
            logger.error(f"❌ Ошибка при получении списка чатов бэкапа: {e}")
            return []
    
    async def get_backup_chat_data(self, backup_date, file_name):
        """Получить данные конкретного чата из бэкапа"""
        try:
            file_path = safe_path_within(self.backup_dir, backup_date, file_name)
            if file_path is None or not file_path.exists():
                return None
            
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
                
        except Exception as e:
            logger.error(f"❌ Ошибка при чтении данных бэкапа: {e}")
            return None
