"""Локальная Лаборатория Франкенштейна v0.4.

Запуск: python lab_server.py
Открыть: http://127.0.0.1:8000/lab.html

Это учебная локальная панель. Она НЕ меняет настройки облачных Telegram-ботов.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "lab.html"
SETTINGS_FILE = BASE_DIR / "lab_settings.json"
LOCK = threading.Lock()

DEFAULT_SETTINGS = {
    "furia_probability": 35,
    "furia_delay_min": 20,
    "furia_delay_max": 120,
    "mark_probability": 70,
    "mark_delay_min": 20,
    "mark_delay_max": 120,
}


def valid_settings(value):
    if not isinstance(value, dict):
        return False
    for bot in ("furia", "mark"):
        chance = value.get(f"{bot}_probability")
        minimum = value.get(f"{bot}_delay_min")
        maximum = value.get(f"{bot}_delay_max")
        if not all(type(x) is int for x in (chance, minimum, maximum)):
            return False
        if not 0 <= chance <= 100:
            return False
        if not 0 <= minimum <= maximum <= 300:
            return False
    return True


def load_settings():
    if not SETTINGS_FILE.exists():
        print("📂 Настроек пока нет — используем стандартные.")
        return DEFAULT_SETTINGS.copy()
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        # Совместимость с предыдущей версией: в файле могла быть только Фурия.
        settings = DEFAULT_SETTINGS | saved
        if not valid_settings(settings):
            raise ValueError("Некорректные настройки")
        print("💾 Настройки прочитаны из lab_settings.json")
        return settings
    except (OSError, ValueError, TypeError):
        print("⚠️ Не удалось прочитать настройки — используем стандартные.")
        return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    temporary = SETTINGS_FILE.with_suffix(".json.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(settings, file, ensure_ascii=False, indent=4)
        os.replace(temporary, SETTINGS_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


SETTINGS = load_settings()


class LabHandler(BaseHTTPRequestHandler):
    def send_content(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_content(body, "application/json; charset=utf-8", status)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/lab.html"):
            try:
                page = HTML_FILE.read_bytes()
            except OSError:
                self.send_json({"error": "Файл lab.html не найден"}, 500)
                return
            self.send_content(page, "text/html; charset=utf-8")
            return

        if path == "/api/status":
            with LOCK:
                settings = SETTINGS.copy()
            self.send_json({
                "lab": "online",
                "version": "0.4",
                "bots": {
                    "serega": {"name": "Серёга", "status": "не проверялся"},
                    "furia": {
                        "name": "Фурия", "status": "не проверялся",
                        "probability": settings["furia_probability"],
                        "delay_min": settings["furia_delay_min"],
                        "delay_max": settings["furia_delay_max"],
                    },
                    "mark": {
                        "name": "Марк", "status": "не проверялся",
                        "probability": settings["mark_probability"],
                        "delay_min": settings["mark_delay_min"],
                        "delay_max": settings["mark_delay_max"],
                    },
                },
            })
            return
        self.send_json({"error": "Адрес не найден"}, 404)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/settings":
            self.send_json({"error": "Неизвестная команда"}, 404)
            return
        # Не разрешаем стороннему сайту отправлять команды локальному серверу.
        origin = self.headers.get("Origin")
        if origin and origin != "http://127.0.0.1:8000":
            self.send_json({"error": "Недопустимый источник запроса"}, 403)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.send_json({"error": "Нужен JSON"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048:
                raise ValueError("Неверный размер")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or data.get("bot") not in ("furia", "mark"):
                raise ValueError("Неизвестный бот")
            bot = data["bot"]
            new_values = {
                f"{bot}_probability": data["probability"],
                f"{bot}_delay_min": data["delay_min"],
                f"{bot}_delay_max": data["delay_max"],
            }
            with LOCK:
                updated = SETTINGS | new_values
                if not valid_settings(updated):
                    raise ValueError("Неверный диапазон")
                save_settings(updated)
                SETTINGS.update(updated)
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.send_json({"error": f"Не удалось сохранить: {error}"}, 400)
            return

        print(f"💾 {bot}: {data['probability']}%, задержка {data['delay_min']}–{data['delay_max']} сек")
        self.send_json({"success": True, "bot": bot, "settings": new_values})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), LabHandler)
    print("🧪 Frankenstein Lab v0.4")
    print("🌐 http://127.0.0.1:8000/lab.html")
    print("🔒 Только локально. Облачные боты не затронуты.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🧪 Лаборатория закрыта.")
    finally:
        server.server_close()
