import asyncio
import base64
import hashlib
import os
import time

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, Update
from openai import OpenAI

import furia_bot
import mark_bot
import lab_control


# =========================================================
# НАСТРОЙКА
# =========================================================

TELEGRAM_TOKEN = os.getenv(
    "SEREGA_TELEGRAM_BOT_TOKEN"
)

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "Не найдена переменная "
        "SEREGA_TELEGRAM_BOT_TOKEN"
    )


bot = Bot(
    token=TELEGRAM_TOKEN
)

dp = Dispatcher()

client = OpenAI()


# =========================================================
# ЗАГРУЖАЕМ МОЗГИ СЕРЁГИ
# =========================================================

with open(
    "critic_prompt.txt",
    "r",
    encoding="utf-8"
) as f:
    system_prompt = f.read()


# =========================================================
# НАСТРОЙКИ СЕРЁГИ
# =========================================================

# Сколько раз повторять запрос к OpenAI,
# если произошла временная ошибка.
MAX_AI_ATTEMPTS = 3

# Пауза между повторными попытками.
RETRY_DELAY_SECONDS = 5


# =========================================================
# ПРЕВРАЩАЕМ БАЙТЫ КАРТИНКИ В DATA URL
# =========================================================

def image_bytes_to_data_url(
    image_bytes: bytes,
    mime_type: str
):

    encoded_image = base64.b64encode(
        image_bytes
    ).decode("utf-8")

    return (
        f"data:{mime_type};base64,"
        f"{encoded_image}"
    )


# =========================================================
# ОПРЕДЕЛЯЕМ ТИП ИЗОБРАЖЕНИЯ
# =========================================================

def get_image_mime_type(
    file_name: str | None,
    telegram_mime_type: str | None
):

    # Если Telegram уже сообщил нормальный MIME —
    # используем его.
    if telegram_mime_type in {
        "image/jpeg",
        "image/png",
        "image/webp"
    }:
        return telegram_mime_type

    if not file_name:
        return "image/jpeg"

    extension = os.path.splitext(
        file_name
    )[1].lower()

    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp"
    }

    return mime_types.get(
        extension
    )


# =========================================================
# СКАЧИВАЕМ ИЗОБРАЖЕНИЕ ИЗ TELEGRAM
# =========================================================

async def download_image_from_message(
    message: Message
):

    # -----------------------------------------------------
    # ОБЫЧНАЯ TELEGRAM-ФОТОГРАФИЯ
    # -----------------------------------------------------

    if message.photo:

        # Telegram присылает несколько размеров
        # одной фотографии.
        #
        # [-1] — самый большой доступный вариант.

        photo = message.photo[-1]

        file = await bot.get_file(
            photo.file_id
        )

        buffer = await bot.download_file(
            file.file_path
        )

        image_bytes = buffer.read()

        return (
            image_bytes,
            "image/jpeg"
        )


    # -----------------------------------------------------
    # ИЗОБРАЖЕНИЕ, ОТПРАВЛЕННОЕ КАК ФАЙЛ
    # -----------------------------------------------------

    if message.document:

        document = message.document

        mime_type = get_image_mime_type(
            document.file_name,
            document.mime_type
        )

        # Это документ, но не изображение.
        if not mime_type:
            return None

        file = await bot.get_file(
            document.file_id
        )

        buffer = await bot.download_file(
            file.file_path
        )

        image_bytes = buffer.read()

        return (
            image_bytes,
            mime_type
        )


    return None


# =========================================================
# СЕРЁГА АНАЛИЗИРУЕТ РИСУНОК
# =========================================================

def generate_critique_sync(
    image_bytes: bytes,
    mime_type: str
):

    image_data = image_bytes_to_data_url(
        image_bytes,
        mime_type
    )

    response = client.responses.create(
        model="gpt-5.6-luna",
        instructions=system_prompt,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Перед тобой одно изображение "
                            "из публикации Telegram-канала "
                            "художника. "
                            "Игнорируй подпись публикации. "
                            "Внимательно рассмотри именно "
                            "это изображение. "
                            "Дай комментарий Серёги "
                            "по правилам системного промпта. "
                            "Если обоснованных замечаний нет, "
                            "ответь ровно одним словом: норм"
                        )
                    },
                    {
                        "type": "input_image",
                        "image_url": image_data
                    }
                ]
            }
        ]
    )

    comment = response.output_text.strip()

    if not comment:
        raise RuntimeError(
            "OpenAI вернул пустой ответ."
        )

    return comment


# =========================================================
# ПОВТОРНЫЕ ПОПЫТКИ
# =========================================================

async def generate_critique(
    image_bytes: bytes,
    mime_type: str
):

    last_error = None

    for attempt in range(
        1,
        MAX_AI_ATTEMPTS + 1
    ):

        try:

            print(
                f"🧠 Попытка анализа "
                f"{attempt}/{MAX_AI_ATTEMPTS}"
            )

            # OpenAI-клиент здесь синхронный.
            # Уводим его в отдельный поток,
            # чтобы Telegram-бот не зависал
            # целиком на время анализа.
            comment = await asyncio.to_thread(
                generate_critique_sync,
                image_bytes,
                mime_type
            )

            return comment

        except Exception as e:

            last_error = e

            print("")
            print(
                "⚠️ Ошибка анализа изображения:"
            )
            print(
                type(e).__name__,
                e
            )

            if attempt < MAX_AI_ATTEMPTS:

                print(
                    f"🔄 Повтор через "
                    f"{RETRY_DELAY_SECONDS} сек."
                )

                await asyncio.sleep(
                    RETRY_DELAY_SECONDS
                )


    raise last_error


# =========================================================
# ПРОВЕРЯЕМ, ЕСТЬ ЛИ В СООБЩЕНИИ ИЗОБРАЖЕНИЕ
# =========================================================

def message_has_image(
    message: Message
):

    if message.photo:
        return True

    if message.document:

        mime_type = get_image_mime_type(
            message.document.file_name,
            message.document.mime_type
        )

        if mime_type:
            return True

    return False


# =========================================================
# /START
# =========================================================

@dp.message(CommandStart())
async def start_handler(
    message: Message
):

    if not lab_control.get_bot_enabled('serega'):
        return

    if message.chat.type == "private":
        if private_access_allowed(message):
            await message.answer(
                "Серёга на месте. 🎨 Присылай изображение и напиши, "
                "что именно проверить. Можешь задавать уточняющие вопросы. "
                "/reset — начать новый разбор."
            )
        else:
            await message.answer(
                "Личный режим Серёги пока не открыт для этого аккаунта."
            )
    else:
        await message.answer("Серёга на месте.")
    lab_control.record_event('serega', 'sent')


# =========================================================
# ПОСТ ИЗ КАНАЛА → ОБЯЗАТЕЛЬНЫЙ АНАЛИЗ
# =========================================================

@dp.message(
    F.is_automatic_forward == True
)
async def channel_post_handler(
    message: Message
):

    if not lab_control.get_bot_enabled('serega'):
        lab_control.record_event('serega', 'skipped')
        return

    # Нас интересуют только изображения.
    #
    # Текстовые посты, видео и прочее
    # Серёга просто игнорирует.

    if not message_has_image(
        message
    ):
        return


    print("")
    print("=" * 50)
    print(
        "🖼 Получено изображение из канала."
    )

    if message.media_group_id:

        print(
            "📚 Изображение является "
            "частью альбома."
        )

    print(
        f"💬 Message ID: {message.message_id}"
    )

    print(
        "👀 Серёга начал доёбываться..."
    )


    try:

        image_data = (
            await download_image_from_message(
                message
            )
        )

        if not image_data:

            print(
                "⚠️ Не удалось получить "
                "изображение."
            )

            return


        image_bytes, mime_type = (
            image_data
        )


        comment = (
            await generate_critique(
                image_bytes,
                mime_type
            )
        )


        # -------------------------------------------------
        # ОТПРАВЛЯЕМ КОММЕНТАРИЙ
        # -------------------------------------------------
        #
        # Отвечаем именно на автоматически
        # пересланное сообщение из канала.
        #
        # Поэтому комментарий оказывается
        # в связанной группе обсуждения.

        if not lab_control.get_bot_enabled('serega'):
            lab_control.record_event('serega', 'skipped')
            return

        await message.reply(
            comment
        )
        lab_control.record_event('serega', 'sent')


        print("")
        print("✅ Серёга ответил:")
        print(comment)
        print("=" * 50)


    except Exception as e:

        lab_control.record_event('serega', 'errors')
        print("")
        print(
            "❌ СЕРЁГА НЕ СМОГ "
            "ОБРАБОТАТЬ ИЗОБРАЖЕНИЕ"
        )

        print(
            type(e).__name__,
            e
        )

        print("=" * 50)


# =========================================================
# СЕРЁГА 2.0 — ЛИЧНЫЙ АРТ-КОНСУЛЬТАНТ
# =========================================================
# ВАЖНО: этот режим независим от анализа постов в канале.
# Доступ разрешён только Telegram ID из переменной окружения
# SEREGA_PRIVATE_ALLOWED_IDS, например: 123456789,987654321
# Без списка доступ закрыт, чтобы посторонние не тратили API-бюджет.

PRIVATE_ALLOWED_IDS = {
    int(value.strip())
    for value in os.getenv("SEREGA_PRIVATE_ALLOWED_IDS", "").split(",")
    if value.strip().isdigit()
}

PRIVATE_SESSION_TTL = 45 * 60  # 45 минут без сообщений
PRIVATE_MAX_SESSIONS = 25
PRIVATE_MAX_IMAGE_BYTES = 6 * 1024 * 1024
PRIVATE_MAX_TEXT_CHARS = 3000
PRIVATE_MAX_HISTORY_ITEMS = 12  # 6 последних пар вопрос/ответ

# Память хранится только в RAM, отдельно для каждого личного чата.
# После перезапуска Netrun она обнуляется — это ожидаемо для v2.0.
private_sessions = {}
private_chat_locks = {}

PRIVATE_INSTRUCTIONS = """Ты — Серёга, опытный арт-критик и арт-директор.
В личных сообщениях ты помогаешь дизайнеру или художнику улучшить работу.
Говори по-русски, прямо, живо, с лёгкой иронией, но без унижений.
Всегда учитывай подпись к изображению, пояснения, идею и критерии проверки.
Если просят проверить конкретный аспект, сначала разбери именно его.
Замечания делай предметными: что не так, где именно, почему и как исправить.
Отделяй объективные ошибки от вкусовых предпочтений.
Если изображение отсутствует, не притворяйся, будто видел его.
Если изображение есть, рассматривай его вместе с историей разговора.
Не утверждай, что изменил изображение: сейчас ты умеешь только анализировать.
Отвечай понятно, без длинных вступлений и лишнего пафоса.
"""


def private_access_allowed(message: Message) -> bool:
    return (
        message.from_user is not None
        and message.from_user.id in PRIVATE_ALLOWED_IDS
    )


def private_session(chat_id: int) -> dict:
    now = time.monotonic()

    # Не держим в памяти старые изображения бесконечно.
    for key, state in list(private_sessions.items()):
        if now - state["updated"] > PRIVATE_SESSION_TTL:
            private_sessions.pop(key, None)

    if chat_id not in private_sessions:
        if len(private_sessions) >= PRIVATE_MAX_SESSIONS:
            oldest_id = min(
                private_sessions,
                key=lambda key: private_sessions[key]["updated"]
            )
            private_sessions.pop(oldest_id, None)
        private_sessions[chat_id] = {
            "history": [],
            "image": None,
            "mime_type": None,
            "updated": now,
        }

    state = private_sessions[chat_id]
    state["updated"] = now
    return state


def private_critique_sync(history: list, image: bytes | None,
                          mime_type: str | None, question: str) -> str:
    # В историю кладём только текст; изображение передаём отдельно.
    content = [{"type": "input_text", "text": question}]
    if image is not None and mime_type is not None:
        content.append({
            "type": "input_image",
            "image_url": image_bytes_to_data_url(image, mime_type),
        })

    response = client.responses.create(
        model="gpt-5.6-luna",
        instructions=PRIVATE_INSTRUCTIONS,
        input=history + [{"role": "user", "content": content}],
        max_output_tokens=1800,
    )
    result = (response.output_text or "").strip()
    if not result:
        raise RuntimeError("OpenAI вернул пустой ответ в личном режиме")
    return result


@dp.message(Command("reset"), F.chat.type == "private")
async def private_reset_handler(message: Message):
    if not lab_control.get_bot_enabled("serega"):
        lab_control.record_event("serega", "skipped")
        return
    if not private_access_allowed(message):
        await message.answer("Личный режим Серёги пока не открыт для этого аккаунта.")
        return

    chat_id = message.chat.id
    lock = private_chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        private_sessions.pop(chat_id, None)
    await message.answer("Контекст очищен. Присылай новую работу и задание. 🎨")
    lab_control.record_event("serega", "sent")


@dp.message(F.chat.type == "private")
async def private_art_director_handler(message: Message):
    if not lab_control.get_bot_enabled("serega"):
        lab_control.record_event("serega", "skipped")
        return

    if not private_access_allowed(message):
        await message.answer("Личный режим Серёги пока не открыт для этого аккаунта.")
        return

    # Команды, не поддерживаемые личным режимом, не отправляем модели.
    if (message.text or "").startswith("/"):
        await message.answer("Пришли изображение с заданием или напиши вопрос. /reset — начать заново.")
        lab_control.record_event("serega", "sent")
        return

    question = (message.caption or message.text or "").strip()
    if len(question) > PRIVATE_MAX_TEXT_CHARS:
        await message.answer("Слишком длинное задание. Сократи текст до 3000 символов.")
        lab_control.record_event("serega", "sent")
        return

    # Документы PDF, ZIP и другие файлы не считаем изображениями.
    document = message.document
    is_image_document = bool(document and (
        document.mime_type in {"image/jpeg", "image/png", "image/webp"}
        or (document.file_name or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ))
    has_image = bool(message.photo or is_image_document)

    if not question and not has_image:
        await message.answer("Пришли картинку, описание идеи или вопрос по дизайну. 🎨")
        lab_control.record_event("serega", "sent")
        return

    chat_id = message.chat.id
    lock = private_chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        try:
            state = private_session(chat_id)
            if has_image:
                if document and document.file_size and document.file_size > PRIVATE_MAX_IMAGE_BYTES:
                    await message.answer("Изображение слишком большое. Пришли файл до 6 МБ.")
                    lab_control.record_event("serega", "sent")
                    return

                image_data = await download_image_from_message(message)
                if image_data is None:
                    await message.answer("Не получилось прочитать изображение. Попробуй JPG или PNG.")
                    lab_control.record_event("serega", "sent")
                    return

                image_bytes, mime_type = image_data
                if len(image_bytes) > PRIVATE_MAX_IMAGE_BYTES:
                    await message.answer("Изображение слишком большое. Пришли файл до 6 МБ.")
                    lab_control.record_event("serega", "sent")
                    return

                # Если пользователь сначала прислал текстовые заметки,
                # сохраняем их. Если картинка уже была, начинаем новый разбор.
                if state["image"] is not None:
                    state["history"] = []
                state["image"] = image_bytes
                state["mime_type"] = mime_type

            if not question:
                question = "Разбери эту работу: сильные стороны, проблемы и конкретные улучшения."

            answer = await asyncio.to_thread(
                private_critique_sync,
                list(state["history"]),
                state["image"],
                state["mime_type"],
                question,
            )

            # Администратор мог выключить бота, пока нейросеть думала.
            if not lab_control.get_bot_enabled("serega"):
                lab_control.record_event("serega", "skipped")
                return

            # У Telegram лимит на длину одного сообщения.
            for offset in range(0, len(answer), 3900):
                await message.answer(answer[offset:offset + 3900])
                lab_control.record_event("serega", "sent")

            state["history"].extend([
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ])
            state["history"] = state["history"][-PRIVATE_MAX_HISTORY_ITEMS:]
            state["updated"] = time.monotonic()

        except Exception as exc:
            lab_control.record_event("serega", "errors")
            print("❌ Серёга 2.0: ошибка личного режима:", type(exc).__name__, str(exc)[:300])
            if lab_control.get_bot_enabled("serega"):
                await message.answer("Не смог обработать запрос. Попробуй ещё раз чуть позже.")


# =========================================================
# WEBHOOK / ОБЩИЙ ВЕБ-СЕРВЕР
# =========================================================

PORT = int(
    os.getenv(
        "PORT",
        "8080"
    )
)


# =========================================================
# МАРШРУТЫ И СЕКРЕТЫ
# =========================================================

# Старый адрес Серёги сохраняем без изменений.
SEREGA_WEBHOOK_PATH = "/telegram-webhook"

# Фурия получает отдельный адрес.
FURIA_WEBHOOK_PATH = "/furia-webhook"

# Марк получает третий отдельный адрес.
MARK_WEBHOOK_PATH = "/mark-webhook"


SEREGA_WEBHOOK_SECRET = hashlib.sha256(
    TELEGRAM_TOKEN.encode("utf-8")
).hexdigest()

FURIA_WEBHOOK_SECRET = hashlib.sha256(
    furia_bot.telegram_token.encode("utf-8")
).hexdigest()

MARK_WEBHOOK_SECRET = hashlib.sha256(
    mark_bot.telegram_token.encode("utf-8")
).hexdigest()


# =========================================================
# ФОНОВАЯ ОБРАБОТКА
# =========================================================

# Telegram должен быстро получить OK.
# Сам анализ изображения, видео, задержки и ответы
# продолжаются отдельными asyncio-задачами.
background_tasks = set()


def background_task_finished(
    task: asyncio.Task
):

    background_tasks.discard(
        task
    )

    try:
        task.result()

    except asyncio.CancelledError:
        pass

    except Exception as e:

        print("")
        print(
            "❌ ОШИБКА ФОНОВОЙ "
            "ОБРАБОТКИ UPDATE"
        )
        print(
            type(e).__name__,
            e
        )
        print("")


def run_in_background(
    coroutine
):

    task = asyncio.create_task(
        coroutine
    )

    background_tasks.add(
        task
    )

    task.add_done_callback(
        background_task_finished
    )


# =========================================================
# HEALTH CHECK
# =========================================================

async def health_handler(
    request: web.Request
):

    return web.Response(
        text="Серёга, Фурия и Марк живы."
    )


# =========================================================
# WEBHOOK СЕРЁГИ
# =========================================================

async def serega_webhook_handler(
    request: web.Request
):

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if (
        received_secret
        != SEREGA_WEBHOOK_SECRET
    ):

        print(
            "⛔ Отклонён запрос Серёги "
            "с неправильным webhook-секретом."
        )

        return web.Response(
            status=403,
            text="Forbidden"
        )

    try:

        data = await request.json()

        update = Update.model_validate(
            data,
            context={
                "bot": bot
            }
        )

        lab_control.record_event('serega', 'received')

        run_in_background(
            dp.feed_update(
                bot,
                update
            )
        )

        return web.Response(
            text="OK"
        )

    except Exception as e:
        lab_control.record_event('serega', 'errors')

        print("")
        print(
            "❌ ОШИБКА ПРИ ПРИЁМЕ "
            "WEBHOOK СЕРЁГИ"
        )
        print(
            type(e).__name__,
            e
        )
        print("")

        return web.Response(
            status=500,
            text="Error"
        )


# =========================================================
# WEBHOOK ФУРИИ
# =========================================================

async def furia_webhook_handler(
    request: web.Request
):

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if (
        received_secret
        != FURIA_WEBHOOK_SECRET
    ):

        print(
            "⛔ Отклонён запрос Фурии "
            "с неправильным webhook-секретом."
        )

        return web.Response(
            status=403,
            text="Forbidden"
        )

    try:

        data = await request.json()

        update = Update.model_validate(
            data,
            context={
                "bot": furia_bot.bot
            }
        )

        lab_control.record_event('furia', 'received')

        run_in_background(
            furia_bot.dp.feed_update(
                furia_bot.bot,
                update
            )
        )

        return web.Response(
            text="OK"
        )

    except Exception as e:
        lab_control.record_event('furia', 'errors')

        print("")
        print(
            "❌ ОШИБКА ПРИ ПРИЁМЕ "
            "WEBHOOK ФУРИИ"
        )
        print(
            type(e).__name__,
            e
        )
        print("")

        return web.Response(
            status=500,
            text="Error"
        )


# =========================================================
# WEBHOOK МАРКА
# =========================================================

async def mark_webhook_handler(
    request: web.Request
):

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if received_secret != MARK_WEBHOOK_SECRET:
        print(
            "⛔ Отклонён запрос Марка "
            "с неправильным webhook-секретом."
        )
        return web.Response(status=403, text="Forbidden")

    try:
        data = await request.json()
        update = Update.model_validate(
            data,
            context={"bot": mark_bot.bot}
        )

        lab_control.record_event('mark', 'received')

        run_in_background(
            mark_bot.dp.feed_update(
                mark_bot.bot,
                update
            )
        )

        return web.Response(text="OK")

    except Exception as e:
        lab_control.record_event('mark', 'errors')
        print("")
        print("❌ ОШИБКА ПРИ ПРИЁМЕ WEBHOOK МАРКА")
        print(type(e).__name__, e)
        print("")
        return web.Response(status=500, text="Error")


# =========================================================
# ЗАПУСК / ОСТАНОВКА ОБЩЕГО ПРИЛОЖЕНИЯ
# =========================================================

async def on_startup(
    app: web.Application
):

    print("")
    print("=" * 50)
    print(
        "🏠 Общий хост ботов запущен."
    )
    print(
        "🌐 Режим: webhook."
    )
    print(
        f"🚪 Порт: {PORT}"
    )
    print("")
    print(
        f"👨 Серёга: "
        f"{SEREGA_WEBHOOK_PATH}"
    )
    print(
        f"💅 Фурия: "
        f"{FURIA_WEBHOOK_PATH}"
    )
    print(
        f"🧠 Марк: "
        f"{MARK_WEBHOOK_PATH}"
    )
    print("=" * 50)
    print("")

    await furia_bot.startup()
    await mark_bot.startup()


async def on_cleanup(
    app: web.Application
):

    tasks = list(
        background_tasks
    )

    for task in tasks:
        task.cancel()

    if tasks:

        await asyncio.gather(
            *tasks,
            return_exceptions=True
        )

    await bot.session.close()

    await furia_bot.cleanup()
    await mark_bot.cleanup()


def create_app():

    app = web.Application()

    app.router.add_get(
        "/",
        health_handler
    )

    app.router.add_post(
        SEREGA_WEBHOOK_PATH,
        serega_webhook_handler
    )

    app.router.add_post(
        FURIA_WEBHOOK_PATH,
        furia_webhook_handler
    )

    app.router.add_post(
        MARK_WEBHOOK_PATH,
        mark_webhook_handler
    )

    # Парольная лаборатория подключается к тому же серверу.
    lab_control.register_routes(app)

    app.on_startup.append(
        on_startup
    )

    app.on_cleanup.append(
        on_cleanup
    )

    return app


# =========================================================
# ЗАПУСК
# =========================================================

if __name__ == "__main__":

    web.run_app(
        create_app(),
        host="0.0.0.0",
        port=PORT
    )
