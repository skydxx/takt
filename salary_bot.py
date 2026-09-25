import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message, ReactionTypeEmoji
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiosqlite

from database import Database
from config import BOT_TOKEN, ERROR_NOTIFICATION_USER_ID, SUPER_ADMINS, CURATOR_CHAT_ID, VIEW_ONLY_ADMINS, SHIFT_ALERT_USERS
from utils import format_hours, format_money, parse_salary, format_period, format_worker_info
from backup_manager import BackupManager
from web_app import platform_store as workspace_store, run_web_app
from admin_app import run_admin_app
from notification_service import NotificationService

# Московский часовой пояс
MSK = ZoneInfo("Europe/Moscow")

# Настройка понятного логирования
class SimpleFormatter(logging.Formatter):
    """Простой и понятный форматтер логов"""
    
    FORMATS = {
        logging.DEBUG: "🔍 %(message)s",
        logging.INFO: "✅ %(message)s",
        logging.WARNING: "⚠️ %(message)s",
        logging.ERROR: "❌ %(message)s",
        logging.CRITICAL: "🚨 %(message)s"
    }
    
    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)

# Настройка логгера
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(SimpleFormatter())
logger.addHandler(handler)

# Отключаем ненужные логи aiogram
logging.getLogger('aiogram').setLevel(logging.WARNING)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
# Используем абсолютный путь из config
db = Database(db_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'salary_bot.db'))
backup_mgr = None  # Инициализируется после создания бота
scheduler = AsyncIOScheduler()
notification_service = NotificationService(bot, db, workspace_store)


class ConfiguredCommandFilter(BaseFilter):
    """Match the owner's configured text command for a bound Telegram chat."""

    def __init__(self, key: str, fallback: str):
        self.key = key
        self.fallback = fallback

    async def __call__(self, message: Message) -> bool:
        if not message.text:
            return False
        configured = workspace_store.automation_for_chat(message.chat.id)
        command = self.fallback
        if configured:
            command = configured.get('commands', {}).get(self.key, self.fallback)
        return message.text.strip().casefold() == command.strip().casefold()


async def notify_error(error_message: str):
    """Отправка уведомления об ошибке"""
    try:
        await bot.send_message(ERROR_NOTIFICATION_USER_ID, f"⚠️ Ошибка в боте:\n{error_message}")
        logger.error(f"Ошибка отправлена администратору: {error_message[:100]}...")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление об ошибке: {e}")


def is_super_admin(user_id: int) -> bool:
    """Проверка является ли пользователь суперадмином"""
    return user_id in SUPER_ADMINS


def is_view_only_admin(user_id: int) -> bool:
    """Проверка является ли пользователь админом только для просмотра"""
    return user_id in VIEW_ONLY_ADMINS


def can_modify_settings(user_id: int) -> bool:
    """Может ли пользователь изменять настройки (только суперадмины)"""
    return is_super_admin(user_id)


def can_view_stats(user_id: int) -> bool:
    """Может ли пользователь просматривать статистику (суперадмины + view-only админы)"""
    return is_super_admin(user_id) or is_view_only_admin(user_id)


# Словарь для отслеживания уведомлений об отсутствии смены
shift_alerts_sent = {}


async def check_shift_opened():
    """Проверка открытия смены и отправка уведомлений"""
    try:
        now = datetime.now(MSK)
        current_time = now.time()
        today_key = now.strftime('%Y-%m-%d')
        
        # Получаем все рабочие чаты
        work_chats = await db.get_all_chats('work')
        
        for chat in work_chats:
            chat_id = chat['chat_id']
            chat_name = chat['chat_name']
            
            # Получаем настройки чата
            async with aiosqlite.connect(db.db_path) as database:
                cursor = await database.execute('''
                    SELECT work_time_start FROM chat_settings WHERE chat_id = ?
                ''', (chat_id,))
                settings = await cursor.fetchone()
            
            if not settings or not settings[0]:
                continue
            
            work_start = datetime.strptime(settings[0], '%H:%M').time()
            automation = workspace_store.automation_for_chat(chat_id) or {}
            notification_settings = automation.get('notifications', {})
            alert_minutes = int(notification_settings.get('shift_alert_minutes', 10))
            escalation_minutes = max(alert_minutes + 5, 15)

            # Интервал тревоги задаётся владельцем в кабинете.
            first_alert_time = (datetime.combine(now.date(), work_start) + timedelta(minutes=alert_minutes)).time()
            second_alert_time = (datetime.combine(now.date(), work_start) + timedelta(minutes=escalation_minutes)).time()
            start_command = automation.get('commands', {}).get('start_shift', 'смена')
            
            # Проверяем есть ли активные смены в чате
            active_shifts = await db.get_all_active_shifts()
            chat_has_active_shift = any(s['chat_id'] == chat_id for s in active_shifts)
            
            alert_key = f"{chat_id}_{today_key}"
            
            # Первое уведомление (через 10 минут в чат)
            if first_alert_time <= current_time < second_alert_time:
                if not chat_has_active_shift and alert_key not in shift_alerts_sent:
                    try:
                        await bot.send_message(
                            chat_id,
                            f"⚠️ Внимание!\n\n"
                            f"Прошло 10 минут с начала работы ({settings[0]}), "
                            f"но никто не начал смену.\n\n"
                            f"Пожалуйста, откройте смену командой '{start_command}'."
                        )
                        shift_alerts_sent[alert_key] = 'first'
                        logger.info(f"Отправлено первое уведомление в чат {chat_name}")
                    except Exception as e:
                        logger.error(f"Не удалось отправить первое уведомление в чат {chat_name}: {e}")
            
            # Второе уведомление (через 15 минут ответственным)
            elif current_time >= second_alert_time:
                if not chat_has_active_shift and shift_alerts_sent.get(alert_key) == 'first':
                    # Отправляем уведомления ответственным
                    for user_id in SHIFT_ALERT_USERS:
                        try:
                            await bot.send_message(
                                user_id,
                                f"🚨 КРИТИЧЕСКОЕ УВЕДОМЛЕНИЕ!\n\n"
                                f"🏛 Чат: {chat_name}\n"
                                f"🕐 Время открытия: {settings[0]}\n"
                            f"⏰ Прошло: {escalation_minutes} минут\n\n"
                                f"❌ Смена до сих пор НЕ ОТКРЫТА!\n\n"
                                f"Требуется срочная проверка!"
                            )
                            logger.info(f"Отправлено критическое уведомление пользователю {user_id} о чате {chat_name}")
                        except Exception as e:
                            logger.error(f"Не удалось отправить критическое уведомление пользователю {user_id}: {e}")
                    
                    shift_alerts_sent[alert_key] = 'second'
            
            # Сбрасываем уведомления если смена открыта
            if chat_has_active_shift and alert_key in shift_alerts_sent:
                del shift_alerts_sent[alert_key]
                logger.info(f"Смена открыта в чате {chat_name}, уведомления сброшены")
        
        # Очищаем старые записи (старше 2 дней)
        two_days_ago = (now - timedelta(days=2)).strftime('%Y-%m-%d')
        keys_to_remove = [k for k in shift_alerts_sent.keys() if k.endswith(two_days_ago)]
        for key in keys_to_remove:
            del shift_alerts_sent[key]
        
    except Exception as e:
        logger.error(f"Ошибка в check_shift_opened: {e}")
        await notify_error(f"Ошибка в check_shift_opened: {str(e)}")


async def send_payroll_notifications():
    """Compatibility wrapper; production delivery runs in ``worker.py``."""
    return await notification_service.send_payroll_notifications()


async def send_subscription_notifications():
    """Compatibility wrapper for local/manual runs."""
    return await notification_service.send_subscription_notifications()


# ========== АДМИНСКИЕ КОМАНДЫ ==========

@router.message(Command("setchat"))
async def cmd_set_chat(message: Message):
    """Команда /setchat для добавления чата"""
    try:
        # Только для суперадминов (могут изменять настройки)
        if not can_modify_settings(message.from_user.id):
            return
        
        logger.info(f"Команда /setchat от пользователя {message.from_user.id} в чате {message.chat.id}")
        
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("❌ Использование: /setchat <id_чата> <название>")
            return
        
        chat_id = int(parts[1])
        chat_name = parts[2]
        
        chat_number = await db.add_chat(chat_id, chat_name, 'work', message.chat.id)
        
        logger.info(f"Чат '{chat_name}' (ID: {chat_id}) добавлен под номером {chat_number}")
        
        await message.answer(
            f"✅ Чат добавлен!\n"
            f"📝 Название: {chat_name}\n"
            f"🔢 Номер: {chat_number}\n"
            f"🆔 ID: {chat_id}"
        )
    except ValueError:
        logger.warning(f"Неверный формат ID чата в команде /setchat")
        await message.answer("❌ Неверный формат ID чата")
    except Exception as e:
        logger.error(f"Ошибка при добавлении чата: {e}")
        await notify_error(f"Ошибка в /setchat: {str(e)}")


@router.message(Command("delchat"))
async def cmd_del_chat(message: Message):
    """Команда /delchat для удаления чата из системы"""
    try:
        # Только для суперадминов (могут изменять настройки)
        if not can_modify_settings(message.from_user.id):
            return
        
        logger.info(f"Команда /delchat от пользователя {message.from_user.id} в чате {message.chat.id}")
        
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("❌ Использование: /delchat <номер_чата>")
            return
        
        chat_number = int(parts[1])
        
        # Удаляем чат
        result = await db.delete_chat(chat_number)
        
        logger.info(f"Чат #{chat_number} '{result['chat_name']}' удален. Удалено смен: {result['shifts_deleted']}")
        
        await message.answer(
            f"✅ Чат удален!\n\n"
            f"📝 Название: {result['chat_name']}\n"
            f"🔢 Номер: {chat_number}\n"
            f"🆔 ID: {result['chat_id']}\n"
            f"📊 Удалено смен: {result['shifts_deleted']}"
        )
    except ValueError as e:
        logger.warning(f"Неверный номер чата в команде /delchat: {e}")
        await message.answer(f"❌ {str(e)}")
    except Exception as e:
        logger.error(f"Ошибка при удалении чата: {e}")
        await notify_error(f"Ошибка в /delchat: {str(e)}")


@router.message(Command("setjob"))
async def cmd_set_job(message: Message):
    """Команда /setjob для установки рабочих условий"""
    try:
        # Только для суперадминов (могут изменять настройки)
        if not can_modify_settings(message.from_user.id):
            return
        
        logger.info(f"Команда /setjob от пользователя {message.from_user.id}")
        
        parts = message.text.split()
        if len(parts) < 4:
            await message.answer(
                "❌ Использование: /setjob <номер> <время> <ставка>\n"
                "Пример: /setjob 1 9:00-22:00 3000"
            )
            return
        
        chat_number = int(parts[1])
        work_time = parts[2]
        salary = parse_salary(parts[3])
        
        chat_id = await db.get_chat_id_by_number(chat_number)
        if not chat_id:
            logger.warning(f"Чат #{chat_number} не найден")
            await message.answer(f"⚠️ Чат #{chat_number} не найден. Используйте сначала /setchat")
            return
        
        await db.set_chat_settings(chat_number, chat_id, work_time, salary)
        logger.info(f"Настройки для чата #{chat_number}: {work_time}, ставка {salary}₽")
        
        await message.answer(
            f"✅ Настройки для чата #{chat_number}:\n"
            f"⏰ Время: {work_time}\n"
            f"💰 Ставка: {format_money(salary)}"
        )
    except ValueError as e:
        logger.warning(f"Неверный формат в /setjob: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Ошибка при настройке рабочих условий: {e}")
        await notify_error(f"Ошибка в /setjob: {str(e)}")


@router.message(Command("setjm"))
async def cmd_set_job_money(message: Message):
    """Команда /setjm для изменения ставки"""
    try:
        # Только для суперадминов (могут изменять настройки)
        if not can_modify_settings(message.from_user.id):
            return
        
        parts = message.text.split()
        if len(parts) < 3:
            await message.answer("❌ Использование: /setjm <номер> <ставка>")
            return
        
        chat_number = int(parts[1])
        salary = parse_salary(parts[2])
        
        await db.update_chat_salary(chat_number, salary)
        await message.answer(f"✅ Ставка для чата #{chat_number}: {format_money(salary)}")
    except Exception as e:
        logger.error(f"Error in cmd_set_job_money: {e}")
        await notify_error(f"Ошибка в /setjm: {str(e)}")


@router.message(Command("setjt"))
async def cmd_set_job_time(message: Message):
    """Команда /setjt для изменения времени работы"""
    try:
        # Только для суперадминов (могут изменять настройки)
        if not can_modify_settings(message.from_user.id):
            return
        
        parts = message.text.split()
        if len(parts) < 3:
            await message.answer("❌ Использование: /setjt <номер> <время>")
            return
        
        chat_number = int(parts[1])
        work_time = parts[2]
        
        await db.update_chat_time(chat_number, work_time)
        await message.answer(f"✅ Время для чата #{chat_number}: {work_time}")
    except Exception as e:
        logger.error(f"Error in cmd_set_job_time: {e}")
        await notify_error(f"Ошибка в /setjt: {str(e)}")


# Команды для вечерних смен
@router.message(Command("nsetjob"))
async def cmd_nset_job(message: Message):
    """Команда /nsetjob для настройки вечерних смен"""
    try:
        # Только для суперадминов (могут изменять настройки)
        if not can_modify_settings(message.from_user.id):
            return
        
        parts = message.text.split()
        if len(parts) < 4:
            await message.answer(
                "❌ Использование: /nsetjob <номер> <время> <ставка>\n"
                "Пример: /nsetjob 1 17:00-21:00 1500"
            )
            return
        
        chat_number = int(parts[1])
        work_time = parts[2]
        salary = parse_salary(parts[3])
        
        chat_id = await db.get_chat_id_by_number(chat_number)
        if not chat_id:
            await message.answer(f"⚠️ Чат #{chat_number} не найден")
            return
        
        await db.set_chat_settings(chat_number, chat_id, work_time, salary, is_evening=True)
        
        await message.answer(
            f"✅ Вечерняя смена для чата #{chat_number}:\n"
            f"🌙 Время: {work_time}\n"
            f"💰 Ставка: {format_money(salary)}"
        )
    except Exception as e:
        logger.error(f"Error in cmd_nset_job: {e}")
        await notify_error(f"Ошибка в /nsetjob: {str(e)}")


@router.message(Command("nsetjm"))
async def cmd_nset_job_money(message: Message):
    """Команда /nsetjm для изменения ставки вечерней смены"""
    try:
        # Только для суперадминов (могут изменять настройки)
        if not can_modify_settings(message.from_user.id):
            return
        
        parts = message.text.split()
        if len(parts) < 3:
            await message.answer("❌ Использование: /nsetjm <номер> <ставка>")
            return
        
        chat_number = int(parts[1])
        salary = parse_salary(parts[2])
        
        await db.update_chat_salary(chat_number, salary, is_evening=True)
        await message.answer(f"✅ Вечерняя ставка для чата #{chat_number}: {format_money(salary)}")
    except Exception as e:
        logger.error(f"Error in cmd_nset_job_money: {e}")
        await notify_error(f"Ошибка в /nsetjm: {str(e)}")


@router.message(Command("nsetjt"))
async def cmd_nset_job_time(message: Message):
    """Команда /nsetjt для изменения времени вечерней смены"""
    try:
        # Только для суперадминов (могут изменять настройки)
        if not can_modify_settings(message.from_user.id):
            return
        
        parts = message.text.split()
        if len(parts) < 3:
            await message.answer("❌ Использование: /nsetjt <номер> <время>")
            return
        
        chat_number = int(parts[1])
        work_time = parts[2]
        
        await db.update_chat_time(chat_number, work_time, is_evening=True)
        await message.answer(f"✅ Вечернее время для чата #{chat_number}: {work_time}")
    except Exception as e:
        logger.error(f"Error in cmd_nset_job_time: {e}")
        await notify_error(f"Ошибка в /nsetjt: {str(e)}")


@router.message(Command("ssetjm"))
async def cmd_sset_job_money(message: Message):
    """Команда /ssetjm для установки персональной ставки"""
    try:
        # Только для суперадминов (могут изменять настройки)
        if not can_modify_settings(message.from_user.id):
            return
        
        parts = message.text.split()
        if len(parts) < 3:
            await message.answer("❌ Использование: /ssetjm <id_пользователя> <ставка>")
            return
        
        user_id = int(parts[1])
        salary = parse_salary(parts[2])
        
        await db.set_custom_rate(user_id, salary)
        
        await message.answer(
            f"✅ Персональная ставка установлена!\n"
            f"👤 ID: {user_id}\n"
            f"💰 Ставка: {format_money(salary)}"
        )
    except Exception as e:
        logger.error(f"Error in cmd_super_set_job_money: {e}")
        await notify_error(f"Ошибка в /ssetjm: {str(e)}")


# ========== ОБРАБОТКА СМЕН ==========

@router.message(ConfiguredCommandFilter('start_shift', 'смена'))
async def handle_shift_regular(message: Message):
    """Обработка 'смена'"""
    try:
        chat_id = message.chat.id
        user_id = message.from_user.id
        username = message.from_user.username
        full_name = message.from_user.full_name
        
        # Проверяем что это не админский чат
        async with aiosqlite.connect(db.db_path) as database:
            cursor = await database.execute(
                'SELECT chat_id FROM chats WHERE chat_id = ? AND chat_type = ?',
                (chat_id, 'work')
            )
            if not await cursor.fetchone():
                # Это не рабочий чат - игнорируем команду
                logger.info(f"Команда 'смена' в нерабочем чате {chat_id} - игнорируем")
                return
        
        await db.add_or_update_worker(user_id, username, full_name)
        
        active_shift = await db.get_active_shift(chat_id, 'regular')
        
        if active_shift:
            # Есть активная смена
            logger.info(f"Закрытие смены пользователя {active_shift['user_id']}")
            shift_result = await db.end_shift(active_shift['id'])
            period_stats = await db.get_period_stats(shift_result['user_id'], chat_id)
            
            prev_worker = await db.get_worker_info(shift_result['user_id'])
            worker_info = format_worker_info(
                shift_result['user_id'],
                prev_worker.get('username') if prev_worker else None,
                prev_worker.get('full_name') if prev_worker else None
            )
            
            # В рабочем чате показываем стандартную зарплату, реальная только в личке/админских
            display_earned = shift_result.get('standard_earned', shift_result['earned_amount'])
            
            logger.info(f"Смена закрыта. Реально заработано: {shift_result['earned_amount']:.2f}₽, показываем в чате: {display_earned:.2f}₽")
            
            msg = await message.answer(
                f"⏹ Смена закрыта!\n\n"
                f"👤 {worker_info}\n"
                f"⏱ Отработано: {format_hours(shift_result['hours_worked'])}\n"
                f"💰 За смену: {format_money(display_earned)}\n"
                f"📊 За период: {format_hours(period_stats['hours_worked'])}"
            )
            
            await db.schedule_message_deletion(chat_id, msg.message_id, 300)
            
            # Если это была смена ТОГО ЖЕ пользователя - просто закрываем, не открывая новую
            if active_shift['user_id'] == user_id:
                logger.info(f"Пользователь {user_id} закрыл свою смену")
                return
            
            # Если это была смена ДРУГОГО - пытаемся открыть новую
            logger.info(f"Открытие новой смены для пользователя {user_id}")
        else:
            # Нет активной смены - пытаемся открыть новую
            logger.info(f"Открытие новой смены для пользователя {user_id}")
        
        # Пытаемся открыть смену
        try:
            await db.start_shift(user_id, chat_id, 'regular')
            logger.info(f"Смена открыта для пользователя {user_id}")
            
            # Ставим простой лайк
            try:
                await message.react([ReactionTypeEmoji(emoji="👍")])
            except:
                pass  # Игнорируем ошибки реакций
        except ValueError as e:
            # Рабочий день закончен - смена не открывается
            logger.info(f"Смена не открыта: {e}")
            # Ничего не делаем - смена уже была закрыта выше (если была активна)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке смены: {e}")
        await notify_error(f"Ошибка при обработке смены: {str(e)}")


@router.message(F.text.lower() == "смена-вечер")
async def handle_shift_evening(message: Message):
    """Обработка 'смена-вечер'"""
    try:
        chat_id = message.chat.id
        user_id = message.from_user.id
        username = message.from_user.username
        full_name = message.from_user.full_name
        
        # Проверяем что это не админский чат
        async with aiosqlite.connect(db.db_path) as database:
            cursor = await database.execute(
                'SELECT chat_id FROM chats WHERE chat_id = ? AND chat_type = ?',
                (chat_id, 'work')
            )
            if not await cursor.fetchone():
                # Это не рабочий чат - игнорируем команду
                logger.info(f"Команда 'смена-вечер' в нерабочем чате {chat_id} - игнорируем")
                return
        
        await db.add_or_update_worker(user_id, username, full_name)
        
        active_shift = await db.get_active_shift(chat_id, 'evening')
        
        if active_shift:
            # Есть активная смена
            shift_result = await db.end_shift(active_shift['id'])
            period_stats = await db.get_period_stats(shift_result['user_id'], chat_id)
            
            prev_worker = await db.get_worker_info(shift_result['user_id'])
            worker_info = format_worker_info(
                shift_result['user_id'],
                prev_worker.get('username') if prev_worker else None,
                prev_worker.get('full_name') if prev_worker else None
            )
            
            # В рабочем чате показываем стандартную зарплату, реальная только в личке/админских
            display_earned = shift_result.get('standard_earned', shift_result['earned_amount'])
            
            msg = await message.answer(
                f"🌙 Вечерняя смена закрыта!\n\n"
                f"👤 {worker_info}\n"
                f"⏱ Отработано: {format_hours(shift_result['hours_worked'])}\n"
                f"💰 За смену: {format_money(display_earned)}\n"
                f"📊 За период: {format_hours(period_stats['hours_worked'])}"
            )
            
            await db.schedule_message_deletion(chat_id, msg.message_id, 300)
            
            # Если это была смена ТОГО ЖЕ пользователя - просто закрываем, не открывая новую
            if active_shift['user_id'] == user_id:
                logger.info(f"Пользователь {user_id} закрыл свою вечернюю смену")
                return
            
            # Если это была смена ДРУГОГО - пытаемся открыть новую
            logger.info(f"Открытие новой вечерней смены для пользователя {user_id}")
        else:
            # Нет активной смены - пытаемся открыть новую
            logger.info(f"Открытие новой вечерней смены для пользователя {user_id}")
        
        # Пытаемся открыть смену
        try:
            await db.start_shift(user_id, chat_id, 'evening')
            logger.info(f"Вечерняя смена открыта для пользователя {user_id}")
            
            # Ставим простой лайк
            try:
                await message.react([ReactionTypeEmoji(emoji="👍")])
            except:
                pass  # Игнорируем ошибки реакций
        except ValueError as e:
            # Рабочий день закончен - смена не открывается
            logger.info(f"Вечерняя смена не открыта: {e}")
            # Ничего не делаем - смена уже была закрыта выше (если была активна)
        
    except Exception as e:
        logger.error(f"Error in handle_shift_evening: {e}")
        await notify_error(f"Ошибка при обработке вечерней смены: {str(e)}")


# ========== КОМАНДЫ ДЛЯ РАБОТНИКОВ ==========

@router.message(Command("current"))
async def cmd_current(message: Message):
    """Команда /current - показывает кто сейчас работает"""
    try:
        chat_id = message.chat.id
        user_id = message.from_user.id
        is_private = message.chat.type == 'private'
        is_super_admin = user_id in SUPER_ADMINS
        
        logger.info(f"Команда /current от пользователя {user_id} в чате {chat_id}")
        
        # Проверяем является ли чат рабочим или админским
        async with aiosqlite.connect(db.db_path) as database:
            # Проверяем есть ли этот чат в списке рабочих
            cursor = await database.execute(
                'SELECT chat_id FROM chats WHERE chat_id = ? AND chat_type = ?',
                (chat_id, 'work')
            )
            is_work_chat = bool(await cursor.fetchone())
            
            # Проверяем админский ли это чат
            is_admin_chat = False
            if not is_work_chat:
                cursor = await database.execute(
                    'SELECT chat_id FROM chats WHERE chat_id = ? AND chat_type = ?',
                    (chat_id, 'admin')
                )
                is_admin_chat = bool(await cursor.fetchone())
        
        # Если это личка и не админ - нельзя
        if is_private and not is_super_admin:
            await message.answer("❌ Эту команду можно использовать только в рабочих чатах")
            return
            
        # Если это какой-то левый чат
        if not is_work_chat and not is_admin_chat and not is_private:
            return
            
        active_shifts = await db.get_all_active_shifts()
        
        # Если это рабочий чат - показываем только смены ЭТОГО чата
        if is_work_chat:
            shifts_to_show = [s for s in active_shifts if s['chat_id'] == chat_id]
            title = "👨‍💻 Сейчас работают в этом чате:"
        else:
            # Админам показываем всё
            shifts_to_show = active_shifts
            title = "👨‍💻 Сейчас работают:"
            
        if not shifts_to_show:
            await message.answer("💤 Сейчас никто не работает")
            return
            
        text = [title]
        for shift in shifts_to_show:
            worker = await db.get_worker_info(shift['user_id'])
            if not worker:
                continue
                
            start_time = datetime.strptime(shift['start_time'], '%Y-%m-%d %H:%M:%S.%f')
            duration = datetime.now() - start_time
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)
            
            chat_info = f" ({shift['chat_name']})" if not is_work_chat else ""
            type_info = " (🌙)" if shift['shift_type'] == 'evening' else ""
            
            # Формируем имя
            name = worker.get('full_name') or worker.get('username') or f"ID: {worker['user_id']}"
            
            text.append(f"• {name}{chat_info}{type_info} — {hours}ч {minutes}мин")
            
        await message.answer("\n".join(text))
        
    except Exception as e:
        logger.error(f"Error in cmd_current: {e}")
        await notify_error(f"Ошибка в /current: {str(e)}")


@router.message(Command("link"))
async def cmd_link(message: Message):
    """Команда /link - получение ключа для связывания с веб-аккаунтом"""
    try:
        # Только в личных сообщениях
        if message.chat.type != 'private':
            await message.answer("⚠️ Эта команда доступна только в личных сообщениях!")
            return
        
        user_id = message.from_user.id
        
        # Проверяем что аккаунт еще не привязан
        existing = await db.check_telegram_already_linked(user_id)
        if existing:
            await message.answer(
                f"✅ Ваш Telegram уже привязан к аккаунту: <b>{existing['username']}</b>\n\n"
                f"Если хотите отвязать и привязать другой аккаунт, используйте страницу "
                f"'Сбросить Telegram' на сайте.",
                parse_mode='HTML'
            )
            return
        
        # Проверяем есть ли уже активный ключ
        has_active = await db.check_active_link_key_exists(user_id)
        if has_active:
            await message.answer(
                "⚠️ У вас уже есть активный ключ связывания!\n\n"
                "Используйте его на странице 'Присоединиться' на сайте.\n"
                "Если потеряли ключ, подождите 1 час и создайте новый."
            )
            return
        
        # Удаляем старые истекшие ключи
        await db.delete_old_link_keys(user_id)
        
        # Создаем новый одноразовый ключ
        key_code = await db.create_link_key(user_id)
        
        await message.answer(
            f"🔑 <b>Ключ для связывания аккаунта</b>\n\n"
            f"Ваш одноразовый ключ:\n"
            f"<code>{key_code}</code>\n\n"
            f"📋 Скопируйте этот ключ и введите его на странице 'Присоединиться' на сайте.\n\n"
            f"⏰ Ключ действителен 1 час.\n"
            f"🔒 После использования ключ станет недействительным.\n\n"
            f"После связывания аккаунтов вам откроется доступ к дополнительным функциям сайта.",
            parse_mode='HTML'
        )
        logger.info("Создан одноразовый ключ связывания для пользователя %s", user_id)
        
    except Exception as e:
        logger.error(f"Error in cmd_link: {e}")
        await message.answer("❌ Ошибка при создании ключа")
        await notify_error(f"Ошибка в /link: {str(e)}")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Команда /stats - статистика (личная или общая)"""
    try:
        user_id = message.from_user.id
        chat_id = message.chat.id
        is_private = message.chat.type == 'private'
        
        # Админы могут смотреть чужую стату
        if can_view_stats(user_id):
            # Парсим аргументы: /stats [период] [user_id]
            parts = message.text.split()
            target_user_id = user_id
            
            if len(parts) > 1:
                # Если указан ID или username
                try:
                    if parts[1].isdigit():
                        target_user_id = int(parts[1])
                    # TODO: Добавить поиск по username
                except:
                    pass
            
            # Логика показа полной статистики...
            # (упрощено для примера, здесь должна быть полная логика)
            await show_personal_stats(message, target_user_id)
            return

        # Обычные юзеры - только свою и только в личке
        if not is_private:
            # В публичном чате удаляем сообщение чтобы не спамить
            await message.delete()
            # И отправляем в личку
            try:
                await bot.send_message(
                    user_id, 
                    "📊 Статистику можно смотреть только в личных сообщениях с ботом!"
                )
            except:
                pass
            return
            
        await show_personal_stats(message, user_id)
        
    except Exception as e:
        logger.error(f"Error in cmd_stats: {e}")
        await notify_error(f"Ошибка в /stats: {str(e)}")


async def show_personal_stats(message: Message, user_id: int):
    """Показать личную статистику пользователя"""
    try:
        period_start, period_end = db.get_current_period()
        stats = await db.get_stats_for_period_by_dates(user_id, period_start, period_end)
        
        period_str = format_period(period_start)
        
        await message.answer(
            f"📊 Ваша статистика за {period_str}:\n\n"
            f"⏱ Отработано: {format_hours(stats['hours_worked'])}\n"
            f"💰 Заработано: {format_money(stats['earned_amount'])}"
        )
    except Exception as e:
        logger.error(f"Error showing personal stats: {e}")
        await message.answer("❌ Ошибка при получении статистики")


# ========== ЗАПУСК ==========

async def on_startup():
    """Действия при запуске бота"""
    try:
        await db.init_db()
        logger.info("✅ База данных успешно инициализирована")
        
        # Инициализация менеджера бэкапов
        global backup_mgr
        backup_mgr = BackupManager(bot, db)
        logger.info("✅ Менеджер бэкапов инициализирован")
        
        # Планировщик задач
        # 1. Удаление старых сообщений (каждую минуту)
        scheduler.add_job(db.delete_expired_messages, 'interval', minutes=1, args=[bot])
        
        # 2. Проверка открытия смены (в рабочее время)
        scheduler.add_job(check_shift_opened, 'interval', minutes=1)
        
        # 3. Автозакрытие смен в 23:59 (проверка каждые 5 минут с 22:00)
        async def auto_close_shifts():
            try:
                now = datetime.now()
                # Если время близко к полуночи
                if now.hour == 23 and now.minute >= 55:
                    work_chats = await db.get_all_chats('work')
                    for chat in work_chats:
                        # Закрываем все активные смены
                        active = await db.get_all_active_shifts()
                        for shift in active:
                            if shift['chat_id'] == chat['chat_id']:
                                await db.end_shift(shift['id'])
                                logger.info(f"Автозакрытие смены {shift['id']} в полночь")
            except Exception as e:
                logger.error(f"Error in auto_close_shifts: {e}")

        scheduler.add_job(auto_close_shifts, 'cron', hour='22-23', minute='*/5')
        
        # 4. Повторная отправка неудачных legacy-отчетов (каждые 30 мин)
        async def retry_reports():
            reports = await db.get_pending_reports()
            for report in reports:
                try:
                    await bot.send_message(report['target_id'], report['report_text'])
                except Exception as exc:
                    logger.warning("Не удалось повторить отчёт %s: %s", report['id'], exc)
                    await db.update_report_attempt(report['id'], success=False)
                else:
                    await db.update_report_attempt(report['id'], success=True)
            await db.clear_old_pending_reports()
            
        scheduler.add_job(retry_reports, 'interval', minutes=30)
        
        # 5. Ежедневный бэкап (в 04:00 утра)
        scheduler.add_job(backup_mgr.create_daily_backup, 'cron', hour=4, minute=0)
        
        scheduler.start()
        logger.info("✅ Планировщик задач запущен:\n"
                   "  - Удаление сообщений: каждую минуту\n"
                   "  - Проверка открытия смены: каждую минуту\n"
                   "  - Автозакрытие смен: каждые 5 мин с 22:00 до 23:59\n"
                   "  - Зарплатные и подписочные уведомления: отдельный worker.py\n"
                   "  - Повтор отчетов: каждые 30 минут\n"
                   "  - Бэкап чатов: каждый день в 4:00")
        
        # Запуск публичного и админ веб-интерфейсов в отдельных потоках/процессах.
        # Это dev/single-host удобство: в проде это два отдельных контейнера
        # (см. docker-compose.example.yml), но интерфейс run_web_app()/run_admin_app()
        # одинаковый в обоих случаях.
        try:
            web_thread = threading.Thread(target=run_web_app, daemon=True)
            web_thread.start()
            logger.info("✅ 🌐 Публичный веб-интерфейс запущен")
        except Exception as e:
            logger.error(f"❌ Не удалось запустить публичный веб-интерфейс: {e}")

        try:
            admin_thread = threading.Thread(target=run_admin_app, args=(backup_mgr,), daemon=True)
            admin_thread.start()
            logger.info("✅ 🔐 Админ веб-интерфейс запущен")
        except Exception as e:
            logger.error(f"❌ Не удалось запустить админ веб-интерфейс: {e}")

        # Уведомляем админа о запуске
        await bot.send_message(
            ERROR_NOTIFICATION_USER_ID, 
            "✅ 🚀 Бот перезапущен и готов к работе!\n"
            "Все системы (БД, Веб, Бэкапы) в норме."
        )
        
    except Exception as e:
        logger.error(f"Ошибка при запуске: {e}")
        # Пробуем отправить уведомление, если бот инициализирован
        try:
            await bot.send_message(ERROR_NOTIFICATION_USER_ID, f"🚨 Критическая ошибка при запуске: {e}")
        except:
            pass

async def main():
    """Главная функция"""
    # Регистрируем обработчики
    dp.include_router(router)
    
    # Запускаем startup
    await on_startup()
    
    logger.info("✅ 🚀 Бот для расчета зарплаты запущен!")
    logger.info("✅ 📊 Отслеживаю смены и команды...")
    
    # Запускаем поллинг
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("✅ Бот остановлен")
    except Exception as e:
        logger.error(f"🚨 Критическая ошибка: {e}")
