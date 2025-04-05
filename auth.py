import asyncio
import os
import json
import logging
import random
import re
import uuid
import sqlite3
from telethon import TelegramClient, events, types
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    ChatWriteForbiddenError
)
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
TEMPLATE_FILE = 'shablon.txt'
SESSIONS_DIR = 'sessions'
WHITELIST = list(map(int, os.getenv("WHITELIST", "").split(','))) if os.getenv("WHITELIST") else []

os.makedirs(SESSIONS_DIR, exist_ok=True)

USER_SESSIONS = {}
AUTH_STATES = {}
ACCOUNT_TARGETS = {}
AUTORESPONDER_STATUS = {}
RESPONSE_DELAYS = {}
MULTI_RESPONSE = {}
USER_DEFAULT_SESSION = {}

PHONE_REGEX = re.compile(r'^\+?[1-9]\d{7,14}$')
CODE_REGEX = re.compile(r'^\d{5}$|^\d{2}-\d{3}$')
UUID_REGEX = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')

class UserSession:
    def __init__(self, api_id, api_hash, session_str):
        self.client = TelegramClient(
            StringSession(session_str),
            int(api_id),
            api_hash
        )
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_str = session_str
        self.handler = None
        self.is_running = False
        self.queue = asyncio.Queue()
        self.task = None

    async def close(self):
        try:
            if self.handler:
                self.client.remove_event_handler(self.handler)
                self.handler = None
            if self.task:
                self.task.cancel()
            await self.client.disconnect()
            logger.info(f"Соединение {self.api_id} закрыто")
        except Exception as e:
            logger.error(f"Ошибка закрытия: {str(e)}")
        finally:
            if hasattr(self.client.session, '_db'):
                try:
                    self.client.session._db.close()
                except Exception as e:
                    logger.error(f"Ошибка закрытия БД: {str(e)}")

async def message_sender(session: UserSession):
    while True:
        try:
            chat_id, templates, multi, delay = await session.queue.get()
            
            if delay > 0:
                await asyncio.sleep(delay)
                
            for i in range(min(multi, 3)):
                try:
                    await session.client.send_message(
                        entity=chat_id,
                        message=random.choice(templates)
                    )
                    logger.info(f"💬 Ответ #{i+1} отправлен в чат {chat_id}")
                    await asyncio.sleep(random.uniform(0.7, 1.2))
                except FloodWaitError as e:
                    logger.warning(f"⏳ Флуд-контроль: {e.seconds} сек")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    logger.error(f"🚨 Ошибка: {str(e)}", exc_info=True)
                
            session.queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка в обработчике очереди: {str(e)}")

def read_template():
    try:
        if not os.path.exists(TEMPLATE_FILE):
            logger.error("Файл шаблонов не найден")
            return []
        with open(TEMPLATE_FILE, 'r', encoding='utf-8') as f:
            templates = [line.strip() for line in f if line.strip()]
            logger.debug(f"Прочитано шаблонов: {len(templates)}")
            return templates
    except Exception as e:
        logger.error(f"Ошибка чтения шаблонов: {str(e)}")
        return []

def save_session(user_id: int, account_id: str, session_data: dict):
    filename = os.path.join(SESSIONS_DIR, f"{user_id}_{account_id}.json")
    target_data = ACCOUNT_TARGETS.get((user_id, account_id), {'chat_id': None, 'user_ids': set()})
    
    data = {
        **session_data,
        "target_data": {
            'chat_id': target_data['chat_id'],
            'user_ids': list(target_data['user_ids'])
        },
        "autoresponder_status": AUTORESPONDER_STATUS.get((user_id, account_id), False),
        "response_delay": RESPONSE_DELAYS.get((user_id, account_id), 0),
        "multi_response": MULTI_RESPONSE.get((user_id, account_id), 1)
    }
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_sessions():
    global ACCOUNT_TARGETS, AUTORESPONDER_STATUS, RESPONSE_DELAYS, MULTI_RESPONSE, USER_DEFAULT_SESSION
    sessions = {}
    for filename in os.listdir(SESSIONS_DIR):
        if filename.endswith('.json'):
            try:
                parts = filename[:-5].split('_')
                if len(parts) != 2:
                    continue
                
                user_id = int(parts[0])
                account_id = parts[1]
                
                if not UUID_REGEX.match(account_id):
                    continue
                
                filepath = os.path.join(SESSIONS_DIR, filename)
                
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    target_data = data.get('target_data', {})
                    chat_id = target_data.get('chat_id')
                    user_ids = list(map(int, target_data.get('user_ids', [])))
                    
                    chat_id = int(chat_id) if chat_id is not None else None
                    
                    ACCOUNT_TARGETS[(user_id, account_id)] = {
                        'chat_id': chat_id,
                        'user_ids': set(user_ids)
                    }
                    
                    AUTORESPONDER_STATUS[(user_id, account_id)] = data.get('autoresponder_status', False)
                    RESPONSE_DELAYS[(user_id, account_id)] = data.get('response_delay', 0)
                    MULTI_RESPONSE[(user_id, account_id)] = data.get('multi_response', 1)
                    
                    sessions.setdefault(str(user_id), {})[account_id] = UserSession(
                        data.get('api_id', ''),
                        data.get('api_hash', ''),
                        data.get('session_str', '')
                    )
                    if user_id not in USER_DEFAULT_SESSION:
                        USER_DEFAULT_SESSION[user_id] = account_id
            except Exception as e:
                logger.error(f"Ошибка загрузки {filename}: {str(e)}")
    return sessions

USER_SESSIONS = load_sessions()

async def setup_autoresponder(session: UserSession, user_id: int, account_id: str):
    try:
        if session.handler:
            session.client.remove_event_handler(session.handler)
            session.handler = None

        key = (user_id, account_id)
        target_data = ACCOUNT_TARGETS.get(key)
        
        if not target_data or target_data.get('chat_id') is None:
            logger.error(f"❌ Для сессии {account_id} не настроен чат!")
            return

        chat_id = target_data['chat_id']
        user_ids = target_data['user_ids']
        
        logger.info(f"🔄 Инициализация автоответчика для чата {chat_id}")

        async def message_handler(event):
            try:
                if not AUTORESPONDER_STATUS.get(key, False):
                    return

                if event.message.out:
                    return

                sender = await event.get_sender()
                if not sender or sender.id not in user_ids:
                    return

                templates = read_template()
                if not templates:
                    return

                delay = RESPONSE_DELAYS.get(key, 0)
                multi = MULTI_RESPONSE.get(key, 1)
                
                await session.queue.put((
                    chat_id,
                    templates,
                    multi,
                    delay
                ))

            except Exception as e:
                logger.error(f"🚨 Ошибка: {str(e)}", exc_info=True)

        session.handler = session.client.add_event_handler(
            message_handler,
            events.NewMessage(chats=chat_id)
        )
        
        if not session.client.is_connected():
            await session.client.start()
            logger.info(f"🔄 Клиент {account_id} подключен")

        if not session.task or session.task.done():
            session.task = asyncio.create_task(message_sender(session))

        session.is_running = True
        logger.info(f"✅ Автоответчик для чата {chat_id} активирован")

    except Exception as e:
        logger.error(f"❌ Ошибка инициализации: {str(e)}", exc_info=True)
        session.is_running = False

async def main():
    try:
        async with TelegramClient(
            StringSession(),
            int(API_ID),
            API_HASH
        ) as client:
            await client.start()
            logger.info("🤖 Главный бот запущен")

            for user_key, sessions in USER_SESSIONS.items():
                user_id = int(user_key)
                for account_id, session in sessions.items():
                    key = (user_id, account_id)
                    if AUTORESPONDER_STATUS.get(key, False):
                        await setup_autoresponder(session, user_id, account_id)

            @client.on(events.NewMessage(pattern=r'^/help$', func=lambda e: e.is_private))
            async def help_handler(event):
                if event.sender_id not in WHITELIST:
                    return
                
                help_text = """
📜 Доступные команды:
/auth - Создать новую сессию
/addid [chat_id] [user_id] - Добавить цель
/run - Запустить автоответчик
/stop - Остановить автоответчик
/setdelay [секунды] - Установить задержку
/multi [1-3] - Установить количество ответов
/listids - Показать цели
/sessions - Список сессий
/setsession [UUID] - Выбрать сессию
/delsession [UUID] - Удалить сессию
/help - Справка по командам
                """
                await event.reply(help_text.strip())

            def get_current_session(user_id: int):
                if user_id not in USER_DEFAULT_SESSION:
                    return None, None
                account_id = USER_DEFAULT_SESSION[user_id]
                return USER_SESSIONS.get(str(user_id), {}), account_id

            @client.on(events.NewMessage(pattern=r'^/auth$', func=lambda e: e.is_private))
            async def auth_handler(event):
                user_id = event.sender_id
                account_id = str(uuid.uuid4())
                
                if any((user_id, aid) in AUTH_STATES for aid in USER_SESSIONS.get(str(user_id), {})):
                    await event.reply("⏳ Уже в процессе авторизации")
                    return
                
                AUTH_STATES[(user_id, account_id)] = {
                    'stage': 'api_id',
                    'message_ids': set()
                }
                USER_DEFAULT_SESSION[user_id] = account_id
                msg = await event.reply(f"🔢 Введите API ID для новой сессии:\nID сессии: `{account_id}`")
                AUTH_STATES[(user_id, account_id)]['message_ids'].add(msg.id)

            @client.on(events.NewMessage(pattern=r'^/multi\s+([1-3])$', func=lambda e: e.is_private))
            async def multi_handler(event):
                user_id = event.sender_id
                sessions, account_id = get_current_session(user_id)
                if not account_id or not sessions:
                    await event.reply("❌ Сначала создайте сессию через /auth")
                    return
                
                try:
                    count = int(event.pattern_match.group(1))
                except ValueError:
                    await event.reply("❌ Неверный формат. Используйте число от 1 до 3")
                    return
                
                key = (user_id, account_id)
                MULTI_RESPONSE[key] = count
                
                if session := sessions.get(account_id):
                    save_session(user_id, account_id, {
                        'api_id': session.api_id,
                        'api_hash': session.api_hash,
                        'session_str': session.session_str
                    })
                    await event.reply(f"🔢 Количество ответов установлено: {count}")
                else:
                    await event.reply("❌ Сессия не найдена")

            @client.on(events.NewMessage(pattern=r'^/addid\s+(-?\d+)\s+(-?\d+)$', func=lambda e: e.is_private))
            async def addid_handler(event):
                user_id = event.sender_id
                sessions, account_id = get_current_session(user_id)
                if not account_id or not sessions:
                    await event.reply("❌ Сначала создайте сессию через /auth")
                    return
                
                try:
                    chat_id = int(event.pattern_match.group(1))
                    target_id = int(event.pattern_match.group(2))
                except ValueError:
                    await event.reply("❌ Неверный формат ID. Используйте целые числа")
                    return
                
                key = (user_id, account_id)
                ACCOUNT_TARGETS[key] = {
                    'chat_id': chat_id,
                    'user_ids': set([target_id])
                }
                
                if session := sessions.get(account_id):
                    save_session(user_id, account_id, {
                        'api_id': session.api_id,
                        'api_hash': session.api_hash,
                        'session_str': session.session_str
                    })
                    await setup_autoresponder(session, user_id, account_id)
                    await event.reply(f"✅ Цель {target_id} добавлена в чат {chat_id}")
                else:
                    await event.reply("❌ Сессия не найдена")

            @client.on(events.NewMessage(pattern=r'^/run$', func=lambda e: e.is_private))
            async def run_handler(event):
                user_id = event.sender_id
                sessions, account_id = get_current_session(user_id)
                if not account_id or not sessions:
                    await event.reply("❌ Сначала создайте сессию через /auth")
                    return
                
                key = (user_id, account_id)
                if session := sessions.get(account_id):
                    try:
                        await setup_autoresponder(session, user_id, account_id)
                        AUTORESPONDER_STATUS[key] = True
                        save_session(user_id, account_id, {
                            'api_id': session.api_id,
                            'api_hash': session.api_hash,
                            'session_str': session.session_str
                        })
                        await event.reply("✅ Автоответчик запущен")
                    except Exception as e:
                        await event.reply(f"🚨 Ошибка: {str(e)}")
                else:
                    await event.reply("❌ Сессия не найдена")

            @client.on(events.NewMessage(pattern=r'^/stop$', func=lambda e: e.is_private))
            async def stop_handler(event):
                user_id = event.sender_id
                sessions, account_id = get_current_session(user_id)
                if not account_id or not sessions:
                    await event.reply("❌ Сначала создайте сессию через /auth")
                    return
                
                key = (user_id, account_id)
                if session := sessions.get(account_id):
                    AUTORESPONDER_STATUS[key] = False
                    if session.handler:
                        session.client.remove_event_handler(session.handler)
                        session.handler = None
                    save_session(user_id, account_id, {
                        'api_id': session.api_id,
                        'api_hash': session.api_hash,
                        'session_str': session.session_str
                    })
                    await event.reply("⛔ Автоответчик остановлен")
                else:
                    await event.reply("❌ Сессия не найдена")

            @client.on(events.NewMessage(pattern=r'^/sessions$', func=lambda e: e.is_private))
            async def sessions_handler(event):
                user_id = event.sender_id
                sessions = USER_SESSIONS.get(str(user_id), {})
                if not sessions:
                    await event.reply("❌ У вас нет активных сессий")
                    return
                
                response = "📂 Ваши сессии:\n" + "\n".join(
                    f"{'★' if uuid == USER_DEFAULT_SESSION.get(user_id) else ' '} {uuid}" 
                    for uuid in sessions.keys()
                )
                await event.reply(response)

            @client.on(events.NewMessage(pattern=r'^/setsession\s+([0-9a-f-]{36})$', func=lambda e: e.is_private))
            async def setsession_handler(event):
                user_id = event.sender_id
                new_uuid = event.pattern_match.group(1).lower()
                if USER_SESSIONS.get(str(user_id), {}).get(new_uuid):
                    USER_DEFAULT_SESSION[user_id] = new_uuid
                    await event.reply(f"✅ Основная сессия изменена на:\n`{new_uuid}`")
                else:
                    await event.reply("❌ Сессия не найдена")

            @client.on(events.NewMessage(pattern=r'^/delsession\s+([0-9a-f-]{36})$', func=lambda e: e.is_private))
            async def delsession_handler(event):
                user_id = event.sender_id
                del_uuid = event.pattern_match.group(1).lower()
                filename = os.path.join(SESSIONS_DIR, f"{user_id}_{del_uuid}.json")
                if os.path.exists(filename):
                    os.remove(filename)
                    if del_uuid == USER_DEFAULT_SESSION.get(user_id):
                        del USER_DEFAULT_SESSION[user_id]
                    if USER_SESSIONS.get(str(user_id), {}).get(del_uuid):
                        await USER_SESSIONS[str(user_id)][del_uuid].close()
                        del USER_SESSIONS[str(user_id)][del_uuid]
                    await event.reply(f"🗑 Сессия {del_uuid} удалена")
                else:
                    await event.reply("❌ Файл сессии не найден")

            @client.on(events.NewMessage(pattern=r'^/listids$', func=lambda e: e.is_private))
            async def listids_handler(event):
                user_id = event.sender_id
                sessions, account_id = get_current_session(user_id)
                if not account_id or not sessions:
                    await event.reply("❌ Сначала создайте сессию через /auth")
                    return
                
                target_data = ACCOUNT_TARGETS.get((user_id, account_id), {'chat_id': None, 'user_ids': set()})
                response = (f"🔍 Текущая сессия {account_id}:\n"
                           f"Чат: {target_data['chat_id']}\n"
                           f"Цели: {', '.join(map(str, target_data['user_ids']))}")
                await event.reply(response)

            @client.on(events.NewMessage(pattern=r'^/setdelay\s+(\d+)$', func=lambda e: e.is_private))
            async def setdelay_handler(event):
                user_id = event.sender_id
                sessions, account_id = get_current_session(user_id)
                if not account_id or not sessions:
                    await event.reply("❌ Сначала создайте сессию через /auth")
                    return
                
                try:
                    delay = int(event.pattern_match.group(1))
                except ValueError:
                    await event.reply("❌ Неверный формат задержки")
                    return
                
                key = (user_id, account_id)
                if delay < 0:
                    await event.reply("❌ Задержка не может быть отрицательной")
                    return
                
                if session := sessions.get(account_id):
                    RESPONSE_DELAYS[key] = delay
                    save_session(user_id, account_id, {
                        'api_id': session.api_id,
                        'api_hash': session.api_hash,
                        'session_str': session.session_str
                    })
                    await event.reply(f"⏱ Задержка установлена: {delay} сек")
                else:
                    await event.reply("❌ Сессия не найдена")

            @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
            async def auth_processor(event):
                if event.sender_id not in WHITELIST:
                    return

                user_id = event.sender_id
                text = event.raw_text.strip()
                
                active_auth = next(((k, v) for k, v in AUTH_STATES.items() 
                                  if k[0] == user_id and event.id not in v['message_ids']), None)
                if not active_auth:
                    return
                
                (auth_key, state) = active_auth
                state['message_ids'].add(event.id)
                
                try:
                    if state['stage'] == 'api_id':
                        if not text.isdigit():
                            raise ValueError("Получите на my.telegram.org/auth")
                        state['api_id'] = int(text)
                        state['stage'] = 'api_hash'
                        msg = await event.reply("🔑 Введите API HASH:")
                        state['message_ids'].add(msg.id)
                    
                    elif state['stage'] == 'api_hash':
                        state['api_hash'] = text
                        state['stage'] = 'phone'
                        msg = await event.reply("📱 Введите номер телефона:")
                        state['message_ids'].add(msg.id)
                    
                    elif state['stage'] == 'phone':
                        if not PHONE_REGEX.match(text):
                            raise ValueError("Неверный формат номера")
                        state['phone'] = text
                        state['client'] = TelegramClient(
                            StringSession(),
                            state['api_id'],
                            state['api_hash']
                        )
                        await state['client'].connect()
                        state['code_hash'] = await state['client'].send_code_request(state['phone'])
                        state['stage'] = 'code'
                        msg = await event.reply("📨 Введите код из SMS:")
                        state['message_ids'].add(msg.id)
                    
                    elif state['stage'] == 'code':
                        code = text.replace('-', '')
                        try:
                            await state['client'].sign_in(
                                phone=state['phone'],
                                code=code,
                                phone_code_hash=state['code_hash'].phone_code_hash
                            )
                        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                            if isinstance(e, PhoneCodeExpiredError):
                                await event.reply("⌛️ Код устарел! Запросите новый код через /auth")
                                del AUTH_STATES[auth_key]
                                return
                            raise ValueError("Неверный код")
                        except SessionPasswordNeededError:
                            state['stage'] = '2fa'
                            msg = await event.reply("🔐 Введите пароль 2FA:")
                            state['message_ids'].add(msg.id)
                            return
                            
                        session_str = state['client'].session.save()
                        save_session(
                            user_id,
                            auth_key[1],
                            {
                                'api_id': state['api_id'],
                                'api_hash': state['api_hash'],
                                'session_str': session_str
                            }
                        )
                        USER_SESSIONS.setdefault(str(user_id), {})[auth_key[1]] = UserSession(
                            state['api_id'],
                            state['api_hash'],
                            session_str
                        )
                        await event.reply(f"✅ Аккаунт {auth_key[1]} авторизован!")
                        del AUTH_STATES[auth_key]
                    
                    elif state['stage'] == '2fa':
                        await state['client'].sign_in(password=text)
                        session_str = state['client'].session.save()
                        save_session(
                            user_id,
                            auth_key[1],
                            {
                                'api_id': state['api_id'],
                                'api_hash': state['api_hash'],
                                'session_str': session_str
                            }
                        )
                        await event.reply(f"✅ 2FA пройдена для {auth_key[1]}!")
                        del AUTH_STATES[auth_key]

                except Exception as e:
                    error_msg = str(e)
                    if "Только числа!" in error_msg:
                        await event.reply("❌ Неверный формат API ID! Введите число:")
                    elif "Неверный формат номера" in error_msg:
                        await event.reply("❌ Неверный номер! Формат: +79991234567")
                    elif "Неверный код" in error_msg:
                        await event.reply("❌ Неверный код! Введите заново:")
                    else:
                        await event.reply(f"❌ Ошибка: {error_msg}\nПовторите ввод:")
                    
                    state['stage'] = AUTH_STATES[auth_key]['stage']

            await client.run_until_disconnected()

    except sqlite3.OperationalError as e:
        logger.error(f"Ошибка блокировки БД: {str(e)}")
        logger.info("Попытка переподключения через 5 секунд...")
        await asyncio.sleep(5)
        await main()
    except Exception as e:
        logger.error(f"Критическая ошибка: {str(e)}")
    finally:
        logger.info("⏳ Выключение...")
        for user_sessions in USER_SESSIONS.values():
            for session in user_sessions.values():
                await session.close()

if __name__ == "__main__":
    asyncio.run(main())