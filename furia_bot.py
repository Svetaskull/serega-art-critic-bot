import asyncio
import random
import base64
import json
import os
import tempfile
import subprocess

import cv2
import imageio_ffmpeg

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from openai import OpenAI


# =========================================================
# НАСТРОЙКА
# =========================================================

telegram_token = os.getenv("FURIA_TELEGRAM_BOT_TOKEN")

if not telegram_token:
    raise RuntimeError(
        "Не найдена переменная FURIA_TELEGRAM_BOT_TOKEN"
    )

bot = Bot(token=telegram_token)
dp = Dispatcher()

client = OpenAI()

with open("furia_prompt.txt", "r", encoding="utf-8") as f:
    system_prompt = f.read()


MEMORY_FILE = "furia_memory.json"
VIDEO_CACHE_DIR = "video_cache"

MAX_HISTORY_MESSAGES = 20
MAX_SAVED_MEMORIES = 500

VIDEO_FRAMES_COUNT = 6

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024


os.makedirs(VIDEO_CACHE_DIR, exist_ok=True)


# =========================================================
# ПАМЯТЬ
# =========================================================

def load_memory():

    if not os.path.exists(MEMORY_FILE):
        print("🆕 Файла памяти пока нет. Начинаем с чистого листа.")
        return {}

    try:

        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory = json.load(f)

        print(f"💾 Память загружена: {len(memory)} записей.")

        return memory

    except Exception as e:

        print("⚠️ Не удалось загрузить память:")
        print(type(e).__name__)
        print(e)

        return {}


def save_memory():

    global furia_messages

    if len(furia_messages) > MAX_SAVED_MEMORIES:

        items = list(furia_messages.items())
        items = items[-MAX_SAVED_MEMORIES:]

        furia_messages = dict(items)

    try:

        temp_file = MEMORY_FILE + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as f:

            json.dump(
                furia_messages,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temp_file, MEMORY_FILE)

    except Exception as e:

        print("⚠️ Не удалось сохранить память:")
        print(type(e).__name__)
        print(e)


def make_memory_key(chat_id, message_id):

    return f"{chat_id}:{message_id}"


furia_messages = load_memory()


# =========================================================
# ИСТОРИЯ РАЗГОВОРА
# =========================================================

def build_conversation_history(history):

    if not history:
        return ""

    recent_history = history[-MAX_HISTORY_MESSAGES:]

    lines = []

    for item in recent_history:

        speaker = item.get("speaker", "")
        text = item.get("text", "")

        if speaker == "furia":
            lines.append(f"Фурия: {text}")

        elif speaker == "user":
            lines.append(f"Пользователь: {text}")

    return "\n".join(lines)


# =========================================================
# ФОТО
# =========================================================

async def get_image_data(photo_file_id):

    if not photo_file_id:
        return None

    try:

        file = await bot.get_file(photo_file_id)

        photo_file = await bot.download_file(
            file.file_path
        )

        image_bytes = photo_file.read()

        encoded_image = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        return (
            f"data:image/jpeg;base64,"
            f"{encoded_image}"
        )

    except Exception as e:

        print("⚠️ Не удалось загрузить фотографию:")
        print(type(e).__name__)
        print(e)

        return None


# =========================================================
# ВИДЕО — КЭШ
# =========================================================

def safe_cache_name(value):

    return "".join(
        char
        for char in value
        if char.isalnum() or char in "-_"
    )


def get_video_cache_folder(cache_key):

    return os.path.join(
        VIDEO_CACHE_DIR,
        safe_cache_name(cache_key)
    )


def frame_file_to_data_url(frame_path):

    try:

        with open(frame_path, "rb") as f:
            image_bytes = f.read()

        encoded = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        return (
            f"data:image/jpeg;base64,"
            f"{encoded}"
        )

    except Exception:
        return None


def load_cached_frames(cache_key):

    """
    Загружает только сохранённые кадры видео.
    """

    if not cache_key:
        return []

    folder = get_video_cache_folder(cache_key)

    if not os.path.isdir(folder):
        return []

    frame_paths = []

    for filename in sorted(os.listdir(folder)):

        if (
            filename.startswith("frame_")
            and filename.lower().endswith(".jpg")
        ):

            frame_paths.append(
                os.path.join(
                    folder,
                    filename
                )
            )

    frames = []

    for frame_path in frame_paths:

        image_data = frame_file_to_data_url(
            frame_path
        )

        if image_data:
            frames.append(image_data)

    return frames


def get_transcript_status(cache_key):

    """
    Возвращает:

    success   — речь успешно распознана
    no_speech — аудио обработано, речи нет
    None      — транскрипция ещё не завершалась успешно

    ВАЖНО:
    ошибка API не сохраняется как готовый результат.
    Поэтому после ошибки бот попробует ещё раз.
    """

    if not cache_key:
        return None

    folder = get_video_cache_folder(cache_key)

    status_path = os.path.join(
        folder,
        "transcript_status.txt"
    )

    if not os.path.exists(status_path):
        return None

    try:

        with open(
            status_path,
            "r",
            encoding="utf-8"
        ) as f:

            status = f.read().strip()

        if status in ("success", "no_speech"):
            return status

    except Exception:
        pass

    return None


def load_cached_transcript(cache_key):

    if not cache_key:
        return ""

    folder = get_video_cache_folder(cache_key)

    transcript_path = os.path.join(
        folder,
        "transcript.txt"
    )

    if not os.path.exists(transcript_path):
        return ""

    try:

        with open(
            transcript_path,
            "r",
            encoding="utf-8"
        ) as f:

            return f.read().strip()

    except Exception:
        return ""


def save_transcript_cache(
    cache_key,
    transcript,
    status
):

    folder = get_video_cache_folder(
        cache_key
    )

    os.makedirs(
        folder,
        exist_ok=True
    )

    transcript_path = os.path.join(
        folder,
        "transcript.txt"
    )

    status_path = os.path.join(
        folder,
        "transcript_status.txt"
    )

    with open(
        transcript_path,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(transcript or "")

    with open(
        status_path,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(status)


# =========================================================
# ВИДЕО — КАДРЫ
# =========================================================

def extract_and_cache_video_frames(
    video_path,
    cache_key
):

    folder = get_video_cache_folder(
        cache_key
    )

    os.makedirs(
        folder,
        exist_ok=True
    )

    capture = cv2.VideoCapture(
        video_path
    )

    if not capture.isOpened():

        print(
            "⚠️ OpenCV не смог открыть видео."
        )

        return []

    total_frames = int(
        capture.get(
            cv2.CAP_PROP_FRAME_COUNT
        )
    )

    fps = capture.get(
        cv2.CAP_PROP_FPS
    )

    if total_frames <= 0:

        capture.release()
        return []

    if fps and fps > 0:

        duration = total_frames / fps

        print(
            f"🎬 Длина видео: "
            f"{duration:.1f} сек."
        )

    positions = []

    for i in range(VIDEO_FRAMES_COUNT):

        fraction = (
            0.10
            + 0.80
            * i
            / max(
                VIDEO_FRAMES_COUNT - 1,
                1
            )
        )

        frame_number = int(
            total_frames * fraction
        )

        positions.append(frame_number)

    frame_paths = []

    for index, frame_number in enumerate(
        positions,
        start=1
    ):

        capture.set(
            cv2.CAP_PROP_POS_FRAMES,
            frame_number
        )

        success, frame = capture.read()

        if not success:
            continue

        frame_path = os.path.join(
            folder,
            f"frame_{index:02d}.jpg"
        )

        saved = cv2.imwrite(
            frame_path,
            frame,
            [
                cv2.IMWRITE_JPEG_QUALITY,
                80
            ]
        )

        if saved:
            frame_paths.append(frame_path)

    capture.release()

    return frame_paths


# =========================================================
# ВИДЕО — ДОСТАЁМ ЗВУК
# =========================================================

def extract_audio_to_wav(
    video_path,
    wav_path
):

    """
    Достаёт звук из MOV/MP4/другого видео
    и превращает его в обычный WAV.

    Возвращает:
    success  — звук получен
    no_audio — в видео нет аудиодорожки
    error    — произошла техническая ошибка
    """

    try:

        ffmpeg_exe = (
            imageio_ffmpeg.get_ffmpeg_exe()
        )

        command = [
            ffmpeg_exe,

            # Перезаписывать временный файл
            "-y",

            # Входной видеофайл
            "-i",
            video_path,

            # Видео нам здесь не нужно
            "-vn",

            # Берём одну аудиодорожку,
            # если она существует
            "-map",
            "0:a:0?",

            # Один канал достаточно для речи
            "-ac",
            "1",

            # 16 кГц достаточно для речи
            "-ar",
            "16000",

            # Несжатый PCM WAV
            "-c:a",
            "pcm_s16le",

            wav_path
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

        # Если WAV реально появился и не пустой —
        # всё хорошо.
        if (
            result.returncode == 0
            and os.path.exists(wav_path)
            and os.path.getsize(wav_path) > 1000
        ):

            return "success"

        stderr = (
            result.stderr or ""
        ).lower()

        no_audio_phrases = [
            "does not contain any stream",
            "output file does not contain any stream",
            "stream map '0:a:0?' matches no streams",
            "matches no streams"
        ]

        if any(
            phrase in stderr
            for phrase in no_audio_phrases
        ):

            return "no_audio"

        print(
            "⚠️ FFmpeg не смог достать звук."
        )

        # Показываем только конец сообщения,
        # чтобы терминал не завалило простынёй.
        if result.stderr:

            print(
                result.stderr[-1000:]
            )

        return "error"

    except Exception as e:

        print(
            "⚠️ Ошибка при извлечении аудио:"
        )

        print(type(e).__name__)
        print(e)

        return "error"


# =========================================================
# ВИДЕО — РАСПОЗНАВАНИЕ РЕЧИ
# =========================================================

def transcribe_wav_sync(wav_path):

    """
    Отправляет уже нормальный WAV
    в OpenAI.

    Возвращает:
    (status, transcript)

    status:
    success
    no_speech
    error
    """

    try:

        print(
            "👂 Фурия слушает, "
            "что говорят в видео..."
        )

        with open(
            wav_path,
            "rb"
        ) as audio_file:

            transcription = (
                client.audio.transcriptions.create(
                    model="gpt-transcribe",
                    file=audio_file,
                    prompt=(
                        "Транскрибируй реально "
                        "слышимую человеческую речь. "
                        "Сохраняй язык оригинала. "
                        "Не придумывай слова, "
                        "которых не слышно."
                    )
                )
            )

        transcript = (
            transcription.text or ""
        ).strip()

        if transcript:

            print(
                "🗣 Фурия распознал речь:"
            )

            print(transcript)

            return (
                "success",
                transcript
            )

        print(
            "🔇 Разборчивой речи "
            "в видео не найдено."
        )

        return (
            "no_speech",
            ""
        )

    except Exception as e:

        print(
            "⚠️ Не удалось распознать "
            "речь в видео:"
        )

        print(type(e).__name__)
        print(e)

        # ВАЖНО:
        # error НЕ считаем готовой транскрипцией.
        return (
            "error",
            ""
        )


# =========================================================
# ВИДЕО — ПОЛНАЯ ОБРАБОТКА
# =========================================================

async def process_video(video):

    """
    Умная обработка видео.

    Если кадры уже есть — используем их.

    Если транскрипция уже успешно делалась —
    используем её.

    Если прошлый раз транскрипция УПАЛА —
    пробуем снова.
    """

    if not video:
        return [], "", None

    cache_key = (
        video.file_unique_id
        or video.file_id
    )

    # -----------------------------------------------------
    # СМОТРИМ, ЧТО УЖЕ ЕСТЬ В КЭШЕ
    # -----------------------------------------------------

    cached_frames = load_cached_frames(
        cache_key
    )

    transcript_status = (
        get_transcript_status(
            cache_key
        )
    )

    cached_transcript = (
        load_cached_transcript(
            cache_key
        )
    )

    frames_ready = bool(
        cached_frames
    )

    transcript_ready = (
        transcript_status
        in (
            "success",
            "no_speech"
        )
    )

    # -----------------------------------------------------
    # ЕСЛИ ВСЁ УЖЕ ГОТОВО
    # -----------------------------------------------------

    if (
        frames_ready
        and transcript_ready
    ):

        print(
            "⚡ Видео полностью "
            "загружено из кэша."
        )

        print(
            f"👀 Кадров из кэша: "
            f"{len(cached_frames)}"
        )

        if transcript_status == "success":

            print(
                "👂 Расшифровка речи "
                "взята из кэша."
            )

        else:

            print(
                "🔇 В кэше отмечено: "
                "разборчивой речи нет."
            )

        return (
            cached_frames,
            cached_transcript,
            cache_key
        )

    # -----------------------------------------------------
    # НАМ НУЖЕН ИСХОДНЫЙ ВИДЕОФАЙЛ
    # -----------------------------------------------------

    file_size = video.file_size or 0

    if (
        file_size
        and file_size
        > TELEGRAM_DOWNLOAD_LIMIT
    ):

        size_mb = (
            file_size
            / 1024
            / 1024
        )

        print(
            f"⚠️ Видео весит "
            f"{size_mb:.1f} МБ."
        )

        print(
            "Telegram Bot API "
            "не даст его скачать."
        )

        return (
            cached_frames,
            cached_transcript,
            cache_key
        )

    temp_video_path = None
    temp_wav_path = None

    try:

        if frames_ready:

            print(
                "⚡ Кадры уже есть в кэше."
            )

            print(
                "👂 Но слух прошлый раз "
                "не отработал — пробуем снова."
            )

        print(
            "⬇️ Фурия скачивает видео..."
        )

        telegram_file = (
            await bot.get_file(
                video.file_id
            )
        )

        suffix = ".mp4"

        if telegram_file.file_path:

            extension = os.path.splitext(
                telegram_file.file_path
            )[1]

            if extension:
                suffix = extension

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=suffix
        ) as temp_file:

            temp_video_path = (
                temp_file.name
            )

        await bot.download_file(
            telegram_file.file_path,
            destination=temp_video_path
        )

        # -------------------------------------------------
        # КАДРЫ
        # -------------------------------------------------

        if not frames_ready:

            print(
                "🎞 Фурия ковыряется "
                "в кадрах..."
            )

            frame_paths = (
                await asyncio.to_thread(
                    extract_and_cache_video_frames,
                    temp_video_path,
                    cache_key
                )
            )

            video_frames = []

            for frame_path in frame_paths:

                image_data = (
                    frame_file_to_data_url(
                        frame_path
                    )
                )

                if image_data:

                    video_frames.append(
                        image_data
                    )

            print(
                f"👀 Фурия увидел кадров "
                f"из видео: "
                f"{len(video_frames)}"
            )

        else:

            video_frames = (
                cached_frames
            )

            print(
                f"👀 Используем старые "
                f"кадры: "
                f"{len(video_frames)}"
            )

        # -------------------------------------------------
        # РЕЧЬ
        # -------------------------------------------------

        video_transcript = (
            cached_transcript
        )

        if not transcript_ready:

            print(
                "🎧 Фурия достаёт "
                "аудиодорожку..."
            )

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".wav"
            ) as temp_wav:

                temp_wav_path = (
                    temp_wav.name
                )

            # NamedTemporaryFile уже создал пустой файл.
            # FFmpeg удобнее писать поверх него,
            # поэтому удаляем пустышку.
            try:
                os.remove(
                    temp_wav_path
                )
            except OSError:
                pass

            audio_status = (
                await asyncio.to_thread(
                    extract_audio_to_wav,
                    temp_video_path,
                    temp_wav_path
                )
            )

            # ---------------------------------------------
            # АУДИО ЕСТЬ
            # ---------------------------------------------

            if audio_status == "success":

                (
                    new_status,
                    transcript
                ) = await asyncio.to_thread(
                    transcribe_wav_sync,
                    temp_wav_path
                )

                if new_status == "success":

                    video_transcript = (
                        transcript
                    )

                    save_transcript_cache(
                        cache_key,
                        transcript,
                        "success"
                    )

                    print(
                        "💾 Расшифровка "
                        "сохранена в кэш."
                    )

                elif new_status == "no_speech":

                    video_transcript = ""

                    save_transcript_cache(
                        cache_key,
                        "",
                        "no_speech"
                    )

                    print(
                        "💾 В кэше сохранено: "
                        "разборчивой речи нет."
                    )

                else:

                    print(
                        "♻️ Ошибку транскрипции "
                        "НЕ сохраняем."
                    )

                    print(
                        "При следующей попытке "
                        "Фурия попробует снова."
                    )

            # ---------------------------------------------
            # АУДИОДОРОЖКИ НЕТ
            # ---------------------------------------------

            elif audio_status == "no_audio":

                print(
                    "🔇 В видео вообще "
                    "нет аудиодорожки."
                )

                video_transcript = ""

                save_transcript_cache(
                    cache_key,
                    "",
                    "no_speech"
                )

            # ---------------------------------------------
            # FFmpeg УПАЛ
            # ---------------------------------------------

            else:

                print(
                    "♻️ Ошибку извлечения "
                    "звука НЕ сохраняем."
                )

                print(
                    "Фурия попробует "
                    "ещё раз позже."
                )

        return (
            video_frames,
            video_transcript,
            cache_key
        )

    except Exception as e:

        print(
            "⚠️ Не удалось обработать видео:"
        )

        print(type(e).__name__)
        print(e)

        return (
            cached_frames,
            cached_transcript,
            cache_key
        )

    finally:

        if (
            temp_video_path
            and os.path.exists(
                temp_video_path
            )
        ):

            try:
                os.remove(
                    temp_video_path
                )
            except OSError:
                pass

        if (
            temp_wav_path
            and os.path.exists(
                temp_wav_path
            )
        ):

            try:
                os.remove(
                    temp_wav_path
                )
            except OSError:
                pass


# =========================================================
# ГЕНЕРАЦИЯ КОММЕНТАРИЯ МАРКА
# =========================================================

async def generate_furia_comment(
    post,
    history=None,
    image_data=None,
    video_frames=None,
    video_transcript=""
):

    history = history or []
    video_frames = video_frames or []

    conversation = (
        build_conversation_history(
            history
        )
    )

    transcript_block = ""

    if video_transcript:

        transcript_block = f"""
Расшифровка реально слышимой речи из видео:
{video_transcript}
"""

    # -----------------------------------------------------
    # ПРОДОЛЖЕНИЕ РАЗГОВОРА
    # -----------------------------------------------------

    if conversation:

        user_input = f"""
Исходный пост канала:
{post}

{transcript_block}

История разговора:
{conversation}

Продолжи этот разговор от лица Фурияа.

Учитывай всю историю разговора,
а не только последнюю реплику.

Не начинай разговор заново.

Не повторяй мысль,
которую Фурия уже высказал.

Отвечай именно на последнюю
реплику пользователя
с учётом предыдущего разговора.

Если приложена фотография,
это исходная фотография поста.

Если приложены кадры видео,
они идут в хронологическом порядке
от начала ролика к концу.

Расшифровка речи относится
к этому же исходному видео.

Используй вместе:
текст поста,
изображение или кадры,
распознанную речь
и историю разговора.

Не придумывай реплики,
звуки или события,
которых нет в доступном контексте.

Напиши только ответ Фурияа.
"""

    # -----------------------------------------------------
    # ПЕРВЫЙ КОММЕНТАРИЙ
    # -----------------------------------------------------

    else:

        user_input = f"""
Исходный пост канала:
{post}

{transcript_block}

Если приложена фотография,
внимательно учитывай её содержание.

Если приложены несколько кадров видео,
они идут в хронологическом порядке
от начала ролика к концу.

Расшифровка речи,
если она приведена выше,
получена из этого же видео.

Используй вместе:
визуальное содержание,
речь
и подпись поста.

Не придумывай того,
чего нельзя определить
по посту,
кадрам
или расшифровке.

Напиши один комментарий Фурияа.
"""

    content = [
        {
            "type": "input_text",
            "text": user_input
        }
    ]

    # -----------------------------------------------------
    # ФОТО
    # -----------------------------------------------------

    if image_data:

        content.append(
            {
                "type": "input_image",
                "image_url": image_data
            }
        )

    # -----------------------------------------------------
    # КАДРЫ ВИДЕО
    # -----------------------------------------------------

    for index, frame in enumerate(
        video_frames,
        start=1
    ):

        content.append(
            {
                "type": "input_text",
                "text": (
                    f"Кадр видео "
                    f"{index}:"
                )
            }
        )

        content.append(
            {
                "type": "input_image",
                "image_url": frame
            }
        )

    response = client.responses.create(
        model="gpt-5.6-luna",
        instructions=system_prompt,
        input=[
            {
                "role": "user",
                "content": content
            }
        ]
    )

    return response.output_text


# =========================================================
# /START
# =========================================================

@dp.message(CommandStart())
async def start_handler(message: Message):

    await message.answer(
        "Фурия на месте. Кто тут опять хуйнёй занимается?"
    )


# =========================================================
# НОВЫЙ ПОСТ КАНАЛА
# =========================================================

@dp.message(
    lambda message:
    message.is_automatic_forward
)
async def channel_post_handler(
    message: Message
):

    post = (
        message.text
        or message.caption
        or ""
    )

    has_photo = bool(
        message.photo
    )

    has_video = bool(
        message.video
    )

    if (
        not post
        and not has_photo
        and not has_video
    ):
        return

    # -----------------------------------------------------
    # 70% ВЕРОЯТНОСТЬ
    # -----------------------------------------------------

    if random.random() > 0.7:

        print(
            "😴 Фурия решил промолчать."
        )

        return

    photo_file_id = None

    video_file_id = None
    video_file_unique_id = None
    video_cache_key = None

    image_data = None
    video_frames = []
    video_transcript = ""

    # -----------------------------------------------------
    # ФОТО
    # -----------------------------------------------------

    if message.photo:

        print(
            "📸 Фурия увидел фотографию!"
        )

        photo = message.photo[-1]

        photo_file_id = (
            photo.file_id
        )

        image_data = (
            await get_image_data(
                photo_file_id
            )
        )

    # -----------------------------------------------------
    # ВИДЕО
    # -----------------------------------------------------

    if message.video:

        print(
            "🎥 Фурия увидел видео!"
        )

        video_file_id = (
            message.video.file_id
        )

        video_file_unique_id = (
            message.video.file_unique_id
        )

        (
            video_frames,
            video_transcript,
            video_cache_key
        ) = await process_video(
            message.video
        )

        if not video_frames:

            if not post:

                print(
                    "🙈 Видео посмотреть "
                    "не получилось, "
                    "а подписи нет."
                )

                return

            print(
                "⚠️ Фурия реагирует "
                "только на подпись."
            )

    # -----------------------------------------------------
    # ГЕНЕРАЦИЯ
    # -----------------------------------------------------

    try:

        comment = (
            await generate_furia_comment(
                post=post,
                history=[],
                image_data=image_data,
                video_frames=video_frames,
                video_transcript=(
                    video_transcript
                )
            )
        )

    except Exception as e:

        print("❌ Ошибка OpenAI:")
        print(type(e).__name__)
        print(e)

        return

    # -----------------------------------------------------
    # ЗАДЕРЖКА
    # -----------------------------------------------------

    delay = random.randint(
        20,
        120
    )

    print(
        f"⏳ Фурия ответит "
        f"через {delay} сек."
    )

    await asyncio.sleep(
        delay
    )

    # -----------------------------------------------------
    # ОТПРАВКА
    # -----------------------------------------------------

    try:

        sent_message = (
            await message.reply(
                comment
            )
        )

    except Exception as e:

        print(
            "❌ Не удалось "
            "отправить комментарий:"
        )

        print(type(e).__name__)
        print(e)

        return

    history = [
        {
            "speaker": "furia",
            "text": comment
        }
    ]

    memory_key = make_memory_key(
        sent_message.chat.id,
        sent_message.message_id
    )

    furia_messages[memory_key] = {
        "post": post,

        "photo_file_id": (
            photo_file_id
        ),

        "video_file_id": (
            video_file_id
        ),

        "video_file_unique_id": (
            video_file_unique_id
        ),

        "video_cache_key": (
            video_cache_key
        ),

        "video_transcript": (
            video_transcript
        ),

        "history": history
    }

    save_memory()

    print(
        "💬 Фурия прокомментировал пост."
    )

    print(
        "💾 Разговор сохранён."
    )


# =========================================================
# ОТВЕТ ПОЛЬЗОВАТЕЛЯ МАРКУ
# =========================================================

@dp.message(
    lambda message:
    message.reply_to_message
    is not None
)
async def reply_to_furia_handler(
    message: Message
):

    replied_message = (
        message.reply_to_message
    )

    memory_key = make_memory_key(
        replied_message.chat.id,
        replied_message.message_id
    )

    if memory_key not in furia_messages:
        return

    memory = furia_messages[
        memory_key
    ]

    post = memory.get(
        "post",
        ""
    )

    photo_file_id = memory.get(
        "photo_file_id"
    )

    video_cache_key = memory.get(
        "video_cache_key"
    )

    video_transcript = memory.get(
        "video_transcript",
        ""
    )

    history = memory.get(
        "history",
        []
    ).copy()

    user_reply = (
        message.text
        or message.caption
        or ""
    )

    if not user_reply:
        return

    history.append(
        {
            "speaker": "user",
            "text": user_reply
        }
    )

    print(
        f"👤 Пользователь: "
        f"{user_reply}"
    )

    # -----------------------------------------------------
    # ФОТО
    # -----------------------------------------------------

    image_data = None

    if photo_file_id:

        print(
            "🖼 Фурия снова смотрит "
            "на исходную фотографию."
        )

        image_data = (
            await get_image_data(
                photo_file_id
            )
        )

    # -----------------------------------------------------
    # ВИДЕО ИЗ КЭША
    # -----------------------------------------------------

    video_frames = []

    if video_cache_key:

        print(
            "⚡ Фурия открывает видео "
            "из локального кэша."
        )

        video_frames = (
            load_cached_frames(
                video_cache_key
            )
        )

        cached_transcript = (
            load_cached_transcript(
                video_cache_key
            )
        )

        transcript_status = (
            get_transcript_status(
                video_cache_key
            )
        )

        if transcript_status == "success":

            video_transcript = (
                cached_transcript
            )

            print(
                "👂 Речь взята "
                "из кэша."
            )

        elif transcript_status == "no_speech":

            video_transcript = ""

            print(
                "🔇 Для этого видео "
                "в кэше нет речи."
            )

        print(
            f"👀 Кадров из кэша: "
            f"{len(video_frames)}"
        )

    # -----------------------------------------------------
    # ГЕНЕРАЦИЯ ОТВЕТА
    # -----------------------------------------------------

    try:

        answer = (
            await generate_furia_comment(
                post=post,
                history=history,
                image_data=image_data,
                video_frames=video_frames,
                video_transcript=(
                    video_transcript
                )
            )
        )

    except Exception as e:

        print("❌ Ошибка OpenAI:")
        print(type(e).__name__)
        print(e)

        return

    history.append(
        {
            "speaker": "furia",
            "text": answer
        }
    )

    # -----------------------------------------------------
    # ОТПРАВКА
    # -----------------------------------------------------

    try:

        sent_message = (
            await message.reply(
                answer
            )
        )

    except Exception as e:

        print(
            "❌ Не удалось "
            "отправить ответ:"
        )

        print(type(e).__name__)
        print(e)

        return

    # -----------------------------------------------------
    # СОХРАНЕНИЕ
    # -----------------------------------------------------

    new_memory_key = (
        make_memory_key(
            sent_message.chat.id,
            sent_message.message_id
        )
    )

    furia_messages[
        new_memory_key
    ] = {
        "post": post,

        "photo_file_id": (
            photo_file_id
        ),

        "video_file_id": memory.get(
            "video_file_id"
        ),

        "video_file_unique_id": memory.get(
            "video_file_unique_id"
        ),

        "video_cache_key": (
            video_cache_key
        ),

        "video_transcript": (
            video_transcript
        ),

        "history": history
    }

    save_memory()

    print(
        f"🧠 Реплик в текущем "
        f"разговоре: "
        f"{len(history)}"
    )

    print(
        "💾 Память обновлена."
    )

    print(
        "↩️ Фурия ответил пользователю."
    )


# =========================================================
# ЗАПУСК
# =========================================================

async def main():

    print("")
    print(
        "===================================="
    )
    print(
        "💅 Фурия 1.0 запущена."
    )
    print(
        f"💾 Записей в памяти: "
        f"{len(furia_messages)}"
    )
    print(
        "📸 Зрение: включено."
    )
    print(
        "🎥 Видео: включено."
    )
    print(
        "👂 Слух: включён."
    )
    print(
        "⚡ Кэш видео: включён."
    )
    print(
        "===================================="
    )
    print("")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())