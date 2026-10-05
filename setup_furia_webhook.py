import hashlib
import json
import os
import urllib.parse
import urllib.request


TELEGRAM_TOKEN = os.getenv(
    "FURIA_TELEGRAM_BOT_TOKEN"
)

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "Не найдена переменная "
        "FURIA_TELEGRAM_BOT_TOKEN"
    )


WEBHOOK_URL = (
    "https://serega-art-critic.de2.netrun.io"
    "/furia-webhook"
)

WEBHOOK_SECRET = hashlib.sha256(
    TELEGRAM_TOKEN.encode("utf-8")
).hexdigest()


def telegram_api(
    method: str,
    data: dict | None = None
):

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/{method}"
    )

    encoded_data = None

    if data is not None:
        encoded_data = urllib.parse.urlencode(
            data
        ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=encoded_data,
        method="POST"
    )

    with urllib.request.urlopen(
        request,
        timeout=30
    ) as response:
        payload = json.loads(
            response.read().decode("utf-8")
        )

    if not payload.get("ok"):
        raise RuntimeError(
            "Telegram API вернул ошибку: "
            f"{payload}"
        )

    return payload


def main():

    print("")
    print(
        "💅 Подключаем Фурию к облачному webhook..."
    )
    print(
        f"🌐 Адрес: {WEBHOOK_URL}"
    )

    result = telegram_api(
        "setWebhook",
        {
            "url": WEBHOOK_URL,
            "secret_token": WEBHOOK_SECRET
        }
    )

    print("✅ Telegram ответил:")
    print(
        result.get(
            "description",
            "Webhook установлен."
        )
    )

    info = telegram_api(
        "getWebhookInfo"
    ).get(
        "result",
        {}
    )

    print("")
    print("📡 Текущий webhook Фурии:")
    print(
        info.get(
            "url",
            "не указан"
        )
    )

    print(
        "📨 Ожидают доставки:",
        info.get(
            "pending_update_count",
            0
        )
    )

    last_error = info.get(
        "last_error_message"
    )

    if last_error:
        print(
            "⚠️ Последняя ошибка Telegram:"
        )
        print(last_error)

    print("")
    print("Готово.")


if __name__ == "__main__":
    main()
