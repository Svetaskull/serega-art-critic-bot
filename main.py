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

    lab_control.record_ai_call('serega', 'generation')
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
            lab_control.touch_private_user(message.from_user.id)
            await message.answer(
                "Серёга на месте. 🎨 Присылай изображение и напиши, "
                "что именно проверить. Можешь задавать уточняющие вопросы. "
                "Можно отправить альбом до 5 картинок для сравнения. "
                "/images — список по количеству, /reset — новый разбор."
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
# СЕРЁГА 2.2 — ЛИЧНЫЙ АРТ-КОНСУЛЬТАНТ
# =========================================================
# Этот режим независим от комментариев к постам канала.
# Доступ: секрет Netrun + динамический список Лаборатории.
# Без обоих списков доступ закрыт, чтобы посторонние не тратили API-бюджет.

PRIVATE_ALLOWED_IDS = {
    int(value.strip())
    for value in os.getenv("SEREGA_PRIVATE_ALLOWED_IDS", "").split(",")
    if value.strip().isdigit()
}

PRIVATE_SESSION_TTL = 45 * 60  # 45 минут без сообщений
PRIVATE_MAX_SESSIONS = 25
PRIVATE_MAX_IMAGE_BYTES = 6 * 1024 * 1024  # на одно изображение
PRIVATE_MAX_IMAGES = 5  # не больше пяти изображений в одном разборе
PRIVATE_MAX_TOTAL_IMAGE_BYTES = 18 * 1024 * 1024
PRIVATE_MAX_TEXT_CHARS = 3000
PRIVATE_MAX_HISTORY_ITEMS = 12  # 6 последних пар вопрос/ответ
PRIVATE_ALBUM_WAIT_SECONDS = 2.0  # ждём все части Telegram-альбома
PRIVATE_RESPONSE_TOKENS = 3200  # запас для полного ответа, не целевая длина
PRIVATE_CONTINUATION_TOKENS = 2600
PRIVATE_TELEGRAM_CHUNK_CHARS = 3900

# Память живёт только в RAM. Перезапуск Netrun очищает её.
# Снимки хранятся отдельно для каждого личного чата.
private_sessions = {}
private_chat_locks = {}
private_albums = {}  # (chat_id, media_group_id) -> ожидающие сообщения

PRIVATE_INSTRUCTIONS = """Ты — Серёга, опытный арт-критик и арт-директор.
В личных сообщениях ты помогаешь дизайнеру или художнику улучшить работу.
Говори по-русски, прямо, живо, с лёгкой иронией, но без унижений.
Всегда учитывай подписи к изображениям, пояснения, идею и критерии проверки.
Если просят проверить конкретный аспект, сначала разбери именно его.
Замечания делай предметными: что не так, где именно, почему и как исправить.
Отделяй объективные ошибки от вкусовых предпочтений.
Если изображений нет, не притворяйся, будто видел их.
Если изображения есть, рассматривай их вместе с историей разговора.
Если прислано несколько изображений, сравнивай их как отдельные работы:
чётко называй «изображение 1», «изображение 2» и так далее.
Не путай изображения, не приписывай одному детали другого.
При сравнении версий отмечай, что стало лучше, что хуже и какой вариант
предпочтительнее по заданному критерию. Не выдумывай отличия.
Новые изображения не отменяют старые, пока пользователь не вызвал /reset.
Не утверждай, что изменил изображение: сейчас ты умеешь только анализировать.
Отвечай понятно, без длинных вступлений и лишнего пафоса.
В обычном режиме отвечай компактно: ориентир 1500–2500 символов.
Сначала короткий вывод, затем только существенные наблюдения и правки.
Не повторяй одну мысль в нескольких разделах и не пересказывай всю теорию.
Если просят подробный разбор, можно отвечать длиннее, но без воды.
Не обрывай предложения: заканчивай ответ законченным выводом.
"""


def private_access_allowed(message: Message) -> bool:
    return (
        message.from_user is not None
        and (
            message.from_user.id in PRIVATE_ALLOWED_IDS
            or lab_control.is_private_user_allowed(message.from_user.id)
        )
    )


def private_session(chat_id: int) -> dict:
    now = time.monotonic()

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
            "images": [],  # [(bytes, mime_type), ...]
            "updated": now,
        }

    state = private_sessions[chat_id]
    state["updated"] = now
    return state


def private_critique_images_sync(history: list, images: list,
                                 question: str) -> str:
    # На каждом ходе повторно передаём все картинки текущего разбора.
    # История содержит только текст, поэтому не раздувается base64-данными.
    content = [{"type": "input_text", "text": question}]
    for index, (image_bytes, mime_type) in enumerate(images, start=1):
        content.append({
            "type": "input_text",
            "text": f"Изображение {index} из {len(images)}:",
        })
        content.append({
            "type": "input_image",
            "image_url": image_bytes_to_data_url(image_bytes, mime_type),
        })

    lab_control.record_ai_call('serega', 'generation')
    response = client.responses.create(
        model="gpt-5.6-luna",
        instructions=PRIVATE_INSTRUCTIONS,
        input=history + [{"role": "user", "content": content}],
        max_output_tokens=PRIVATE_RESPONSE_TOKENS,
    )
    parts = [(response.output_text or "").strip()]

    # Responses API может вернуть неполный текст при исчерпании токенов.
    # Один раз просим продолжить, чтобы не выдавать обрыв как готовый ответ.
    if getattr(response, "status", None) == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
        if reason == "max_output_tokens" and getattr(response, "id", None):
            print("⚠️ Серёга: лимит ответа, запрашиваем продолжение")
            lab_control.record_ai_call('serega', 'generation')
            continuation = client.responses.create(
                model="gpt-5.6-luna",
                instructions=PRIVATE_INSTRUCTIONS,
                previous_response_id=response.id,
                input=[{
                    "role": "user",
                    "content": (
                        "Продолжи ровно с того места, где ответ оборвался. "
                        "Не повторяй уже сказанное. Закончи мысль и дай краткий итог."
                    ),
                }],
                max_output_tokens=PRIVATE_CONTINUATION_TOKENS,
            )
            next_part = (continuation.output_text or "").strip()
            if next_part:
                parts.append(next_part)
            else:
                parts.append("[Продолжение не получено. Попроси закончить разбор.]")
            if getattr(continuation, "status", None) != "completed":
                print("⚠️ Серёга: продолжение не завершилось штатно")
                parts.append("[Ответ мог оборваться. Попроси продолжить разбор.]")
        else:
            print(f"⚠️ Серёга: ответ неполный, причина: {reason}")
            parts.append("[Ответ не завершён. Попроси продолжить разбор.]")

    # Продолжение может начинаться посреди предложения. В таком случае
    # склеиваем его через пробел, а не создаём новый абзац.
    result = ""
    for part in parts:
        if not part:
            continue
        if not result:
            result = part
        elif result[-1] in ".!?…:;\n" or part.startswith("["):
            result += "\n\n" + part
        else:
            result += " " + part
    result = result.strip()
    if not result:
        raise RuntimeError("OpenAI вернул пустой ответ в личном режиме")
    return result


def private_critique_sync(history: list, image: bytes | None,
                          mime_type: str | None, question: str) -> str:
    # Сохраняем совместимость с тестами и вызовами Серёги 2.0.
    images = [(image, mime_type)] if image is not None and mime_type else []
    return private_critique_images_sync(history, images, question)


def private_is_image(message: Message) -> bool:
    document = message.document
    return bool(message.photo or (document and (
        document.mime_type in {"image/jpeg", "image/png", "image/webp"}
        or (document.file_name or "").lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        )
    )))


def private_split_text(text: str, limit: int = PRIVATE_TELEGRAM_CHUNK_CHARS) -> list[str]:
    """Делим по абзацам/предложениям/словам, не посреди слова."""
    if not text:
        return []
    chunks = []
    remaining = text.strip()
    while len(remaining) > limit:
        window = remaining[:limit]
        # Стараемся сохранить структуру ответа.
        minimum = limit // 2
        cut = window.rfind("\n\n", minimum)
        if cut < 0:
            cut = window.rfind("\n", minimum)
        if cut < 0:
            # Для обычного предложения предпочтительнее граница фразы.
            candidates = [window.rfind(mark, minimum) for mark in (". ", "! ", "? ")]
            cut = max(candidates)
            if cut >= 0:
                cut += 1
        if cut < 0:
            cut = window.rfind(" ", minimum)
        if cut < 0:
            # Единственное исключение — сплошная строка > лимита Telegram.
            cut = limit
        piece = remaining[:cut].strip()
        if piece:
            chunks.append(piece)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def private_answer(message: Message, text: str):
    for part in private_split_text(text):
        # Access can be revoked while an AI response is being generated.
        if not private_access_allowed(message):
            return
        await message.answer(part)
        lab_control.record_event("serega", "sent")


async def private_process_messages(messages: list[Message]):
    """Один запрос к ИИ на одно сообщение или целый Telegram-альбом."""
    messages = sorted(messages, key=lambda item: item.message_id)
    message = messages[-1]
    chat_id = message.chat.id
    lock = private_chat_locks.setdefault(chat_id, asyncio.Lock())

    async with lock:
        try:
            if not lab_control.get_bot_enabled("serega"):
                lab_control.record_event("serega", "skipped")
                return
            if not private_access_allowed(message):
                lab_control.record_event("serega", "skipped")
                return

            state = private_session(chat_id)
            new_image_messages = [m for m in messages if private_is_image(m)]
            captions = [(m.caption or m.text or "").strip() for m in messages]
            captions = [caption for caption in captions if caption]

            if sum(len(caption) for caption in captions) > PRIVATE_MAX_TEXT_CHARS:
                await private_answer(message, "Слишком длинное задание. Сократи текст до 3000 символов.")
                return

            if not new_image_messages and not captions:
                await private_answer(message, "Пришли картинку, описание идеи или вопрос по дизайну. 🎨")
                return

            if len(state["images"]) + len(new_image_messages) > PRIVATE_MAX_IMAGES:
                await private_answer(
                    message,
                    f"В одном разборе можно хранить до {PRIVATE_MAX_IMAGES} изображений. "
                    "Отправь /reset, чтобы начать новый набор картинок."
                )
                return

            # Сначала скачиваем и проверяем весь набор; при ошибке
            # не добавляем в память ни одной картинки из альбома.
            new_images = []
            for image_message in new_image_messages:
                document = image_message.document
                if document and document.file_size and document.file_size > PRIVATE_MAX_IMAGE_BYTES:
                    await private_answer(message, "Изображение слишком большое. Пришли файл до 6 МБ.")
                    return

                image_data = await download_image_from_message(image_message)
                if image_data is None:
                    await private_answer(message, "Не получилось прочитать изображение. Попробуй JPG или PNG.")
                    return
                image_bytes, mime_type = image_data
                if len(image_bytes) > PRIVATE_MAX_IMAGE_BYTES:
                    await private_answer(message, "Изображение слишком большое. Пришли файл до 6 МБ.")
                    return
                new_images.append((image_bytes, mime_type))

            images = state["images"] + new_images
            if sum(len(data) for data, _ in images) > PRIVATE_MAX_TOTAL_IMAGE_BYTES:
                await private_answer(
                    message,
                    "Суммарный размер изображений превышает 18 МБ. "
                    "Пришли картинки поменьше или начни новый разбор через /reset."
                )
                return

            first_new_index = len(state["images"]) + 1
            if len(captions) == 1:
                question = captions[0]
            elif len(captions) > 1:
                # Для альбома подписи привязываем к номеру конкретной картинки.
                question = "\n".join(
                    f"Подпись к изображению {first_new_index + index}: "
                    f"{(m.caption or m.text).strip()}"
                    for index, m in enumerate(messages)
                    if (m.caption or m.text or "").strip()
                )
            elif len(images) > 1 and new_images:
                question = (
                    "Сравни присланные изображения по композиции, "
                    "сильным и слабым сторонам. Что стало лучше или хуже? "
                    "Называй изображения по номерам."
                )
            elif new_images:
                question = "Разбери эту работу: сильные стороны, проблемы и конкретные улучшения."
            else:
                question = "Разбери текущие работы и ответь на мой вопрос."

            # Один альбом = один платный запрос. Резервируем до вызова ИИ,
            # атомарно и с сохранением в /data; ошибки ИИ тоже расходуют квоту.
            quota = lab_control.reserve_private_request(message.from_user.id)
            if quota != 'ok':
                if quota == 'limit':
                    await private_answer(message, "Лимит запросов исчерпан. Попроси администратора Лаборатории увеличить его или сбросить счётчик.")
                elif quota == 'unavailable':
                    await private_answer(message, "Учёт запросов временно недоступен. Попробуй позже.")
                else:
                    lab_control.record_event('serega', 'skipped')
                return

            # Сохраняем набор только после проверки всех лимитов.
            state["images"] = images
            history_question = question
            if new_images:
                numbers = list(range(first_new_index, first_new_index + len(new_images)))
                history_question = (
                    f"[Добавлены изображения: {', '.join(map(str, numbers))}. "
                    f"Всего изображений: {len(images)}.]\n{question}"
                )

            answer = await asyncio.to_thread(
                private_critique_images_sync,
                list(state["history"]),
                list(state["images"]),
                history_question,
            )

            if not lab_control.get_bot_enabled("serega") or not private_access_allowed(message):
                lab_control.record_event("serega", "skipped")
                return

            await private_answer(message, answer)
            state["history"].extend([
                {"role": "user", "content": history_question},
                {"role": "assistant", "content": answer},
            ])
            state["history"] = state["history"][-PRIVATE_MAX_HISTORY_ITEMS:]
            state["updated"] = time.monotonic()

        except Exception as exc:
            lab_control.record_event("serega", "errors")
            print("❌ Серёга 2.2: ошибка личного режима:", type(exc).__name__, str(exc)[:300])
            if lab_control.get_bot_enabled("serega") and private_access_allowed(message):
                await private_answer(message, "Не смог обработать запрос. Попробуй ещё раз чуть позже.")


async def private_flush_album(key: tuple):
    """Telegram присылает альбом несколькими обновлениями: ждём их все."""
    try:
        while True:
            album = private_albums.get(key)
            if album is None:
                return
            wait = PRIVATE_ALBUM_WAIT_SECONDS - (time.monotonic() - album["updated"])
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            messages = album["messages"]
            private_albums.pop(key, None)
            await private_process_messages(messages)
            return
    except asyncio.CancelledError:
        return


@dp.message(Command("reset"), F.chat.type == "private")
async def private_reset_handler(message: Message):
    if not lab_control.get_bot_enabled("serega"):
        lab_control.record_event("serega", "skipped")
        return
    if not private_access_allowed(message):
        await message.answer("Личный режим Серёги пока не открыт для этого аккаунта.")
        return

    lab_control.touch_private_user(message.from_user.id)
    chat_id = message.chat.id
    # Отменяем ещё не собранные альбомы, чтобы они не вернулись после /reset.
    for key, album in list(private_albums.items()):
        if key[0] == chat_id:
            private_albums.pop(key, None)
            album["task"].cancel()

    lock = private_chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        private_sessions.pop(chat_id, None)
    await private_answer(message, "Контекст очищен. Присылай новую работу и задание. 🎨")


@dp.message(Command("images"), F.chat.type == "private")
async def private_images_handler(message: Message):
    if not lab_control.get_bot_enabled("serega"):
        lab_control.record_event("serega", "skipped")
        return
    if not private_access_allowed(message):
        await message.answer("Личный режим Серёги пока не открыт для этого аккаунта.")
        return
    lab_control.touch_private_user(message.from_user.id)
    lock = private_chat_locks.setdefault(message.chat.id, asyncio.Lock())
    async with lock:
        count = len(private_session(message.chat.id)["images"])
    await private_answer(
        message,
        f"В текущем разборе {count} из {PRIVATE_MAX_IMAGES} изображений. "
        "Для нового набора используй /reset."
    )


@dp.message(F.chat.type == "private")
async def private_art_director_handler(message: Message):
    if not lab_control.get_bot_enabled("serega"):
        lab_control.record_event("serega", "skipped")
        return

    if not private_access_allowed(message):
        await message.answer("Личный режим Серёги пока не открыт для этого аккаунта.")
        return

    if (message.text or "").startswith("/"):
        await private_answer(
            message,
            "Пришли изображения с заданием или напиши вопрос. "
            "/images — сколько картинок в памяти; /reset — начать заново."
        )
        return

    if message.media_group_id and private_is_image(message):
        # Один Telegram-альбом = один анализ и один ответ, а не 5 ответов.
        key = (message.chat.id, message.media_group_id)
        album = private_albums.get(key)
        if album is None:
            album = {"messages": [], "updated": time.monotonic(), "task": None}
            private_albums[key] = album
            album["task"] = run_in_background(private_flush_album(key))
        album["messages"].append(message)
        album["updated"] = time.monotonic()
        return

    # Если текст пришёл сразу после альбома, дожидаемся его обработки,
    # чтобы вопрос не обогнал ещё загружающиеся изображения.
    pending = [
        album["task"] for key, album in list(private_albums.items())
        if key[0] == message.chat.id
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    await private_process_messages([message])


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

    return task


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
