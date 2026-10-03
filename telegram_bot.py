import asyncio
import base64
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from openai import OpenAI


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

    await message.answer(
        "Серёга на месте."
    )


# =========================================================
# ПОСТ ИЗ КАНАЛА → ОБЯЗАТЕЛЬНЫЙ АНАЛИЗ
# =========================================================

@dp.message(
    F.is_automatic_forward == True
)
async def channel_post_handler(
    message: Message
):

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

        await message.reply(
            comment
        )


        print("")
        print("✅ Серёга ответил:")
        print(comment)
        print("=" * 50)


    except Exception as e:

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
# ЗАПУСК
# =========================================================

async def main():

    print("")
    print("=" * 50)
    print("👨 Серёга 1.0 запущен.")
    print("🖼 Анализ изображений: включён.")
    print("📚 Альбомы: каждое изображение.")
    print("🎯 Вероятность реакции: 100%.")
    print("📝 Подписи постов: игнорируются.")
    print("=" * 50)
    print("")

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":
    asyncio.run(
        main()
    )