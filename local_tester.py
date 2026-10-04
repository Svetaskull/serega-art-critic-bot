import base64
import os

from openai import OpenAI


# =========================================================
# НАСТРОЙКА
# =========================================================

client = OpenAI()

with open(
    "critic_prompt.txt",
    "r",
    encoding="utf-8"
) as f:
    system_prompt = f.read()


# =========================================================
# ПРЕВРАЩАЕМ КАРТИНКУ В ФОРМАТ ДЛЯ OPENAI
# =========================================================

def image_to_data_url(image_path):

    extension = os.path.splitext(
        image_path
    )[1].lower()

    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp"
    }

    mime_type = mime_types.get(
        extension
    )

    if not mime_type:
        raise ValueError(
            "Серёга такое не жрёт. "
            "Нужен JPG, JPEG, PNG или WEBP."
        )

    with open(
        image_path,
        "rb"
    ) as image_file:

        image_bytes = (
            image_file.read()
        )

    encoded_image = (
        base64.b64encode(
            image_bytes
        ).decode("utf-8")
    )

    return (
        f"data:{mime_type};base64,"
        f"{encoded_image}"
    )


# =========================================================
# СЕРЁГА СМОТРИТ НА РИСУНОК
# =========================================================

def ask_serega(image_path):

    image_data = (
        image_to_data_url(
            image_path
        )
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
                            "Перед тобой рисунок "
                            "из Telegram-канала художника. "
                            "Внимательно рассмотри изображение. "
                            "Дай комментарий Серёги "
                            "по правилам из системного промпта."
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

    return response.output_text


# =========================================================
# ЗАПУСК
# =========================================================

print("")
print("==============================")
print("👨 Серёга проснулся.")
print("==============================")
print("")

image_path = input(
    "Перетащи сюда картинку и нажми Enter: "
).strip()

# Windows иногда добавляет кавычки,
# когда путь содержит пробелы.
image_path = image_path.strip(
    '"'
)

if not os.path.exists(
    image_path
):
    print("")
    print(
        "❌ Серёга картинку не нашёл."
    )

else:

    print("")
    print(
        "👀 Серёга смотрит..."
    )

    try:

        comment = ask_serega(
            image_path
        )

        print("")
        print("Серёга:")
        print(comment)

    except Exception as e:

        print("")
        print("❌ Ошибка:")
        print(type(e).__name__)
        print(e)