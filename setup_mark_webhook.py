import hashlib
import os
import urllib.parse
import urllib.request
import json


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

WEBHOOK_URL = "https://serega-art-critic.de2.netrun.io/mark-webhook"
WEBHOOK_SECRET = hashlib.sha256(
    TELEGRAM_TOKEN.encode("utf-8")
).hexdigest()


def telegram_api(method: str, data: dict | None = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"

    encoded_data = None

    if data is not None:
        encoded_data = urllib.parse.urlencode(data).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=encoded_data,
        method="POST" if encoded_data is not None else "GET",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


print("🧠 Подключаем Марка к облачному webhook...")
print(f"🌐 Адрес: {WEBHOOK_URL}")

result = telegram_api(
    "setWebhook",
    {
        "url": WEBHOOK_URL,
        "secret_token": WEBHOOK_SECRET,
        "drop_pending_updates": "false",
    },
)

if not result.get("ok"):
    raise RuntimeError(
        f"Telegram не смог установить webhook: {result}"
    )

print("✅ Telegram ответил:")
print(result.get("description", "Webhook установлен."))

info = telegram_api("getWebhookInfo")

if not info.get("ok"):
    raise RuntimeError(
        f"Не удалось проверить webhook: {info}"
    )

webhook_info = info.get("result", {})

print("")
print("📡 Текущий webhook Марка:")
print(webhook_info.get("url") or "не установлен")
print(
    "📨 Ожидают доставки:",
    webhook_info.get("pending_update_count", 0),
)

last_error = webhook_info.get("last_error_message")

if last_error:
    print("⚠️ Последняя ошибка Telegram:")
    print(last_error)

print("")
print("Готово.")
