"""Frankenstein Lab v0.5: password-protected controls for the shared aiohttp host.

Requires LAB_PASSWORD (at least 12 characters). Without it all /lab routes return 404.
Do not run the old local lab_server.py in the cloud.
"""

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from aiohttp import web

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path('/data') if Path('/data').is_dir() else BASE_DIR
SETTINGS_FILE = DATA_DIR / 'lab_settings.json'
PASSWORD = os.getenv('LAB_PASSWORD', '')
LAB_ENABLED = len(PASSWORD) >= 12
COOKIE_NAME = 'frankenstein_session'
SESSION_LIFETIME = 8 * 60 * 60

DEFAULT_SETTINGS = {
    'furia_probability': 70,
    'furia_delay_min': 20,
    'furia_delay_max': 120,
    'mark_probability': 70,
    'mark_delay_min': 20,
    'mark_delay_max': 120,
}

LOCK = threading.Lock()
SESSIONS = {}  # Token -> (expiration, CSRF token); cleared on restart.
FAILED_LOGINS = {}  # Remote IP -> recent failure timestamps.


def valid_settings(value):
    if not isinstance(value, dict):
        return False
    for bot in ('furia', 'mark'):
        chance = value.get(f'{bot}_probability')
        minimum = value.get(f'{bot}_delay_min')
        maximum = value.get(f'{bot}_delay_max')
        if not all(type(x) is int for x in (chance, minimum, maximum)):
            return False
        if not (0 <= chance <= 100 and 0 <= minimum <= maximum <= 300):
            return False
    return True


def load_settings():
    if not SETTINGS_FILE.exists():
        return DEFAULT_SETTINGS.copy()
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        if not isinstance(saved, dict):
            raise ValueError('Not an object')
        values = DEFAULT_SETTINGS | saved
        if not valid_settings(values):
            raise ValueError('Invalid ranges')
        return values
    except (OSError, ValueError, TypeError) as exc:
        print(f'⚠️ Лаборатория: настройки не прочитаны ({type(exc).__name__}), использованы стандартные.')
        return DEFAULT_SETTINGS.copy()


SETTINGS = load_settings()


def get_bot_settings(bot):
    """Read current values when a new channel post is handled."""
    if bot not in ('furia', 'mark'):
        raise ValueError('Unknown bot')
    with LOCK:
        return (
            SETTINGS[f'{bot}_probability'] / 100.0,
            SETTINGS[f'{bot}_delay_min'],
            SETTINGS[f'{bot}_delay_max'],
        )


def _save_settings(settings):
    temporary = SETTINGS_FILE.with_suffix('.json.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as file:
            json.dump(settings, file, ensure_ascii=False, indent=2)
        os.replace(temporary, SETTINGS_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


def _session(request):
    token = request.cookies.get(COOKIE_NAME, '')
    if not token:
        return None
    with LOCK:
        item = SESSIONS.get(token)
        if item is None:
            return None
        if item[0] <= time.time():
            SESSIONS.pop(token, None)
            return None
        return item


def _require_session(request):
    if not LAB_ENABLED:
        raise web.HTTPNotFound()
    session = _session(request)
    if session is None:
        raise web.HTTPUnauthorized(text='Требуется вход в лабораторию')
    return session


def _security_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


LOGIN_HTML = '''<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Frankenstein Lab · Вход</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;background:#111318;color:#f2f2f2;font:16px Arial,sans-serif}
.card{width:min(100%,420px);padding:34px;background:#1b1e25;border:1px solid #30343d;border-radius:22px}h1{font-size:26px;margin:0 0 10px}p{color:#aab3c3;line-height:1.5}
label{display:block;margin:24px 0 9px;font-size:14px}input{width:100%;padding:13px;background:#111318;color:white;border:1px solid #586171;border-radius:10px;font-size:16px}
button{width:100%;margin-top:16px;padding:13px;background:#71d89a;color:#122015;font-weight:700;border:0;border-radius:10px;cursor:pointer;font-size:16px}
#error{color:#ff9e9e;min-height:22px;font-size:14px}
</style></head><body><main class="card"><h1>🧪 Лаборатория Франкенштейна</h1>
<p>Вход в центр управления электронными долбоёбами.</p><form id="login"><label for="password">Пароль</label>
<input id="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Войти в лабораторию</button></form><p id="error" role="alert"></p></main>
<script>document.getElementById('login').addEventListener('submit',async e=>{
e.preventDefault();const btn=e.target.querySelector('button');btn.disabled=true;document.getElementById('error').textContent='';
try{const r=await fetch('/lab/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});
if(!r.ok){document.getElementById('error').textContent=r.status===429?'Слишком много попыток. Попробуй позже.':'Пароль не подошёл.';return;}
location.assign('/lab/');}catch(err){document.getElementById('error').textContent='Нет связи с сервером.';}finally{btn.disabled=false;}
});</script></body></html>'''


async def page_handler(request):
    if not LAB_ENABLED:
        raise web.HTTPNotFound()
    if _session(request) is None:
        return _security_headers(web.Response(text=LOGIN_HTML, content_type='text/html', charset='utf-8'))
    try:
        page = (BASE_DIR / 'lab.html').read_text(encoding='utf-8')
    except OSError:
        raise web.HTTPInternalServerError(text='Не найден lab.html')
    return _security_headers(web.Response(text=page, content_type='text/html', charset='utf-8'))


async def login_handler(request):
    if not LAB_ENABLED:
        raise web.HTTPNotFound()
    ip = request.remote or 'unknown'
    now = time.time()
    with LOCK:
        failures = [t for t in FAILED_LOGINS.get(ip, []) if now - t < 300]
        FAILED_LOGINS[ip] = failures
        if len(failures) >= 10:
            raise web.HTTPTooManyRequests(text='Повтори через пять минут')
    if request.content_length is None or request.content_length > 1024:
        raise web.HTTPBadRequest(text='Неверный запрос')
    try:
        body = await request.json()
        password = body.get('password', '') if isinstance(body, dict) else ''
    except (ValueError, TypeError):
        password = ''
    if not isinstance(password, str) or not hmac.compare_digest(password, PASSWORD):
        with LOCK:
            FAILED_LOGINS.setdefault(ip, []).append(now)
        raise web.HTTPUnauthorized(text='Неверный пароль')
    with LOCK:
        FAILED_LOGINS.pop(ip, None)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        SESSIONS[token] = (now + SESSION_LIFETIME, csrf)
    response = _security_headers(web.json_response({'success': True}))
    is_https = request.secure or request.headers.get('X-Forwarded-Proto', '').lower() == 'https'
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_LIFETIME,
        httponly=True, secure=is_https, samesite='Strict', path='/lab',
    )
    return response


async def logout_handler(request):
    session = _require_session(request)
    token = request.cookies.get(COOKIE_NAME, '')
    _require_csrf(request, session)
    with LOCK:
        SESSIONS.pop(token, None)
    response = _security_headers(web.json_response({'success': True}))
    response.del_cookie(COOKIE_NAME, path='/lab')
    return response


def _require_csrf(request, session):
    received = request.headers.get('X-CSRF-Token', '')
    if not received or not hmac.compare_digest(received, session[1]):
        raise web.HTTPForbidden(text='Неверный защитный токен')


async def status_handler(request):
    session = _require_session(request)
    with LOCK:
        settings = SETTINGS.copy()
    result = {'lab': 'online', 'version': '0.5', 'csrf': session[1], 'bots': {
        'serega': {'name': 'Серёга', 'status': 'сервер запущен'},
    }}
    for bot, name in (('furia', 'Фурия'), ('mark', 'Марк')):
        result['bots'][bot] = {
            'name': name, 'status': 'сервер запущен',
            'probability': settings[f'{bot}_probability'],
            'delay_min': settings[f'{bot}_delay_min'],
            'delay_max': settings[f'{bot}_delay_max'],
        }
    return _security_headers(web.json_response(result))


async def settings_handler(request):
    session = _require_session(request)
    _require_csrf(request, session)
    if request.content_length is None or request.content_length > 2048:
        raise web.HTTPRequestEntityTooLarge(max_size=2048, actual_size=request.content_length or 0)
    try:
        data = await request.json()
        if not isinstance(data, dict) or data.get('bot') not in ('furia', 'mark'):
            raise ValueError('Неизвестный бот')
        bot = data['bot']
        values = {
            f'{bot}_probability': data['probability'],
            f'{bot}_delay_min': data['delay_min'],
            f'{bot}_delay_max': data['delay_max'],
        }
        with LOCK:
            updated = SETTINGS | values
            if not valid_settings(updated):
                raise ValueError('Некорректные значения')
            _save_settings(updated)
            SETTINGS.update(updated)
    except (ValueError, TypeError, KeyError) as exc:
        raise web.HTTPBadRequest(text=f'Неверные настройки: {exc}')
    except OSError:
        raise web.HTTPInternalServerError(text='Не удалось сохранить настройки на диск')
    print(f'🧪 Лаборатория: {bot} → {data["probability"]}%, {data["delay_min"]}–{data["delay_max"]} сек')
    return _security_headers(web.json_response({'success': True, 'bot': bot}))


async def redirect_handler(request):
    raise web.HTTPFound('/lab/')


def register_routes(app):
    app.router.add_get('/lab', redirect_handler)
    app.router.add_get('/lab/', page_handler)
    app.router.add_post('/lab/login', login_handler)
    app.router.add_post('/lab/logout', logout_handler)
    app.router.add_get('/lab/api/status', status_handler)
    app.router.add_post('/lab/api/settings', settings_handler)
    if LAB_ENABLED:
        print(f'🧪 Frankenstein Lab v0.5: включена; настройки в {SETTINGS_FILE}')
    else:
        print('🔒 Frankenstein Lab отключена: задай LAB_PASSWORD длиной от 12 символов.')
