"""Frankenstein Lab v1.0: password-protected controls for the shared aiohttp host.

Requires LAB_PASSWORD (at least 12 characters). Without it all /lab routes return 404.
Do not run the old local lab_server.py in the cloud.
"""

import hmac
from collections import deque
from datetime import datetime, timezone, timedelta
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
ACTIVITY_FILE = DATA_DIR / 'lab_activity.json'
ANALYTICS_FILE = DATA_DIR / 'lab_analytics.json'
ACCESS_FILE = DATA_DIR / 'lab_access.json'
TESTERS_FILE = DATA_DIR / 'lab_testers.json'
TESTER_REQUEST_LIMIT_MAX = 10000
ACCESS_LIMIT = 30
# IDs from Netrun remain allowed and cannot be revoked from the dashboard.
ENV_PRIVATE_ALLOWED_IDS = {
    int(value.strip()) for value in os.getenv('SEREGA_PRIVATE_ALLOWED_IDS', '').split(',')
    if value.strip().isdigit() and int(value.strip()) > 0
}
PASSWORD = os.getenv('LAB_PASSWORD', '')
LAB_ENABLED = len(PASSWORD) >= 12
COOKIE_NAME = 'frankenstein_session'
SESSION_LIFETIME = 8 * 60 * 60

DEFAULT_SETTINGS = {
    'serega_enabled': True,
    'furia_enabled': True,
    'mark_enabled': True,
    'furia_probability': 70,
    'furia_delay_min': 20,
    'furia_delay_max': 120,
    'mark_probability': 70,
    'mark_delay_min': 20,
    'mark_delay_max': 120,
}

LOCK = threading.RLock()
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec='seconds')
EVENTS = deque(maxlen=120)
STATS = {name: {'received': 0, 'sent': 0, 'errors': 0, 'skipped': 0,
                'last_received': None, 'last_sent': None, 'last_error': None}
         for name in ('serega', 'furia', 'mark')}
# Store only aggregate counts and recent event metadata, never user content.
def _load_activity():
    if not ACTIVITY_FILE.exists():
        return
    try:
        if ACTIVITY_FILE.stat().st_size > 256 * 1024:
            raise ValueError('Activity file is too large')
        data = json.loads(ACTIVITY_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError('Unknown activity format')
        stats = data.get('stats')
        events = data.get('events')
        if not isinstance(stats, dict) or not isinstance(events, list):
            raise ValueError('Invalid activity structure')
        loaded = {}
        for bot, defaults in STATS.items():
            values = stats.get(bot)
            if not isinstance(values, dict):
                raise ValueError('Missing bot stats')
            for field in ('received', 'sent', 'errors', 'skipped'):
                if type(values.get(field)) is not int or values[field] < 0:
                    raise ValueError('Invalid counter')
            for field in ('last_received', 'last_sent', 'last_error'):
                value = values.get(field)
                if value is not None and (not isinstance(value, str) or len(value) > 64):
                    raise ValueError('Invalid timestamp')
            loaded[bot] = {field: values[field] for field in defaults}
        allowed_kinds = {'received', 'sent', 'errors', 'skipped', 'power_on', 'power_off'}
        recent = []
        for event in events[:120]:
            if (not isinstance(event, dict) or event.get('bot') not in STATS
                    or event.get('kind') not in allowed_kinds
                    or not isinstance(event.get('time'), str)
                    or len(event['time']) > 64
                    or not isinstance(event.get('text'), str)
                    or len(event['text']) > 160):
                raise ValueError('Invalid event')
            recent.append({key: event[key] for key in ('time', 'bot', 'kind', 'text')})
        for bot, values in loaded.items():
            STATS[bot].update(values)
        EVENTS.extend(recent)
        print(f'🧪 Лаборатория: статистика восстановлена из {ACTIVITY_FILE}')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'⚠️ Лаборатория: статистика не восстановлена ({type(exc).__name__}); начинаем с нуля.')


def _save_activity():
    """Called under LOCK; atomic replacement keeps the previous valid snapshot."""
    temporary = ACTIVITY_FILE.with_suffix('.json.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as file:
            json.dump({'version': 1, 'stats': STATS, 'events': list(EVENTS)},
                      file, ensure_ascii=False, separators=(',', ':'))
        os.replace(temporary, ACTIVITY_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


_load_activity()

# v1.0: daily aggregate history is independent of the v0.9 activity file.
# This avoids altering or resetting the old cumulative counters and event log.
ANALYTICS_KINDS = ('received', 'sent', 'errors', 'skipped', 'ai_generations', 'ai_transcriptions')
ANALYTICS_RETENTION_DAYS = 90
ANALYTICS = {}  # YYYY-MM-DD -> bot -> counter -> nonnegative integer


def _empty_daily_bot():
    return {kind: 0 for kind in ANALYTICS_KINDS}


def _load_analytics():
    if not ANALYTICS_FILE.exists():
        return {}, False
    try:
        if ANALYTICS_FILE.stat().st_size > 128 * 1024:
            raise ValueError('Analytics file is too large')
        data = json.loads(ANALYTICS_FILE.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('days'), dict)
                or len(data['days']) > ANALYTICS_RETENTION_DAYS):
            raise ValueError('Invalid analytics format')
        loaded = {}
        for day, bots in data['days'].items():
            if not isinstance(day, str) or len(day) != 10:
                raise ValueError('Invalid analytics day')
            if datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day:
                raise ValueError('Invalid analytics date')
            if not isinstance(bots, dict) or set(bots) != set(STATS):
                raise ValueError('Invalid analytics bots')
            loaded[day] = {}
            for bot, counts in bots.items():
                if not isinstance(counts, dict) or set(counts) != set(ANALYTICS_KINDS):
                    raise ValueError('Invalid analytics counters')
                if any(type(value) is not int or value < 0 or value > 1_000_000_000
                       for value in counts.values()):
                    raise ValueError('Invalid analytics values')
                loaded[day][bot] = counts.copy()
        print(f'🧪 Лаборатория: аналитика восстановлена ({len(loaded)} дней)')
        return loaded, False
    except (OSError, ValueError, TypeError) as exc:
        print(f'⚠️ Лаборатория: аналитика повреждена ({type(exc).__name__}); запись заблокирована.')
        return {}, True


def _save_analytics(days):
    temporary = ANALYTICS_FILE.with_suffix('.json.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as file:
            json.dump({'version': 1, 'days': days}, file, ensure_ascii=False,
                      separators=(',', ':'))
        os.replace(temporary, ANALYTICS_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


ANALYTICS, ANALYTICS_LOAD_ERROR = _load_analytics()


def _record_daily(bot, kind, now):
    """Under LOCK. Analytics failure never prevents a Telegram response."""
    if ANALYTICS_LOAD_ERROR:
        return
    day = now[:10]
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=ANALYTICS_RETENTION_DAYS - 1)).isoformat()
    updated = {date: counts for date, counts in ANALYTICS.items() if date >= cutoff}
    daily = {name: counts.copy() for name, counts in updated.get(day, {
        name: _empty_daily_bot() for name in STATS
    }).items()}
    if daily[bot][kind] >= 1_000_000_000:
        return
    daily[bot][kind] += 1
    updated[day] = daily
    try:
        _save_analytics(updated)
    except OSError as exc:
        print(f'⚠️ Лаборатория: аналитика не сохранена ({type(exc).__name__}).')
        return
    ANALYTICS.clear()
    ANALYTICS.update(updated)


def record_ai_call(bot, kind):
    """Count an attempted OpenAI call, immediately before making that call.

    kind: 'generation' (text/image reasoning) or 'transcription' (speech).
    Cached transcripts must NOT call this function.
    """
    if bot not in STATS or kind not in ('generation', 'transcription'):
        return
    counter = 'ai_generations' if kind == 'generation' else 'ai_transcriptions'
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with LOCK:
        _record_daily(bot, counter, now)


def _analytics_snapshot():
    """Under LOCK. 30 calendar days, including today, UTC."""
    today = datetime.now(timezone.utc).date()
    result = []
    for offset in range(29, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        counts = ANALYTICS.get(day, {})
        result.append({'date': day, 'bots': {
            name: counts.get(name, _empty_daily_bot()).copy() for name in STATS
        }})
    return {'timezone': 'UTC', 'days': result, 'read_error': ANALYTICS_LOAD_ERROR}

# Separately persisted private-chat allowlist; independent of bot settings.
# Invalid or damaged data fails closed; do not overwrite it automatically.
def _valid_user_id(value):
    if type(value) is not str or not value.isascii() or not value.isdecimal():
        return None
    if not 1 <= len(value) <= 19 or value[0] == '0':
        return None
    number = int(value)
    return number if 0 < number <= 9223372036854775807 else None


def _valid_label(value):
    return (type(value) is str and len(value) <= 60
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _load_access():
    if not ACCESS_FILE.exists():
        return {}, False
    try:
        if ACCESS_FILE.stat().st_size > 16 * 1024:
            raise ValueError('Access file too large')
        data = json.loads(ACCESS_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('users'), list):
            raise ValueError('Unknown access file format')
        users = data['users']
        if len(users) > ACCESS_LIMIT:
            raise ValueError('Too many users')
        result = {}
        for item in users:
            if not isinstance(item, dict):
                raise ValueError('Invalid user')
            user_id = _valid_user_id(item.get('id'))
            label = item.get('label')
            if user_id is None or not _valid_label(label) or user_id in result:
                raise ValueError('Invalid or duplicate user')
            result[user_id] = label.strip()
        print(f'🧪 Лаборатория: список доступа восстановлен ({len(result)} пользователей)')
        return result, False
    except (OSError, ValueError, TypeError) as exc:
        print(f'⚠️ Лаборатория: список доступа повреждён ({type(exc).__name__}); управление заблокировано.')
        return {}, True


def _save_access(users):
    temporary = ACCESS_FILE.with_suffix('.json.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as file:
            json.dump({'version': 1, 'users': [
                {'id': str(user_id), 'label': label}
                for user_id, label in sorted(users.items())
            ]}, file, ensure_ascii=False, indent=2)
        os.replace(temporary, ACCESS_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


MANAGED_USERS, ACCESS_LOAD_ERROR = _load_access()


# Private tester counters/limits live in their own file. Existing v0.8 access
# lists remain unchanged. No messages, pictures or chat history are stored.
def _load_testers():
    if not TESTERS_FILE.exists():
        return {}, False
    try:
        if TESTERS_FILE.stat().st_size > 64 * 1024:
            raise ValueError('Tester file too large')
        data = json.loads(TESTERS_FILE.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('users'), dict)
                or len(data['users']) > 128):
            raise ValueError('Invalid tester data')
        result = {}
        for raw_id, item in data['users'].items():
            uid = _valid_user_id(raw_id)
            if uid is None or not isinstance(item, dict):
                raise ValueError('Invalid tester ID')
            requests = item.get('requests')
            limit = item.get('limit')
            last_active = item.get('last_active')
            if (type(requests) is not int or requests < 0 or requests > 1_000_000_000
                    or type(limit) is not int or not 0 <= limit <= TESTER_REQUEST_LIMIT_MAX
                    or (last_active is not None and
                        (not isinstance(last_active, str) or len(last_active) > 64))):
                raise ValueError('Invalid tester values')
            result[uid] = {'requests': requests, 'limit': limit, 'last_active': last_active}
        print(f'🧪 Лаборатория: статистика тестировщиков восстановлена ({len(result)})')
        return result, False
    except (OSError, ValueError, TypeError) as exc:
        print(f'⚠️ Лаборатория: файл тестировщиков повреждён ({type(exc).__name__}); лимиты заблокированы.')
        return {}, True


def _save_testers(users):
    temporary = TESTERS_FILE.with_suffix('.json.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as file:
            json.dump({'version': 1, 'users': {
                str(uid): data for uid, data in sorted(users.items())
            }}, file, ensure_ascii=False, indent=2)
        os.replace(temporary, TESTERS_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


TESTERS, TESTERS_LOAD_ERROR = _load_testers()


def _tester_data(user_id):
    return TESTERS.get(user_id, {'requests': 0, 'limit': 0, 'last_active': None}).copy()


def _private_allowed_unlocked(user_id):
    return user_id in ENV_PRIVATE_ALLOWED_IDS or user_id in MANAGED_USERS


def touch_private_user(user_id):
    """Record an authorized tester's command activity, not message contents."""
    if type(user_id) is not int or user_id <= 0:
        return
    with LOCK:
        if TESTERS_LOAD_ERROR or not _private_allowed_unlocked(user_id):
            return
        updated = TESTERS.copy()
        data = _tester_data(user_id)
        data['last_active'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        updated[user_id] = data
        try:
            _save_testers(updated)
        except OSError as exc:
            print(f'⚠️ Лаборатория: активность не сохранена ({type(exc).__name__}).')
            return
        TESTERS.clear()
        TESTERS.update(updated)


def reserve_private_request(user_id):
    """Atomically charge ONE AI request (including a Telegram album).

    Returns ok / limit / denied / unavailable. Fail closed if the file cannot
    be read or written, so a disk failure cannot bypass paid-request limits.
    """
    if type(user_id) is not int or user_id <= 0:
        return 'denied'
    with LOCK:
        if not _private_allowed_unlocked(user_id):
            return 'denied'
        if TESTERS_LOAD_ERROR:
            return 'unavailable'
        data = _tester_data(user_id)
        if data['limit'] and data['requests'] >= data['limit']:
            return 'limit'
        if data['requests'] >= 1_000_000_000:
            return 'limit'
        data['requests'] += 1
        data['last_active'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        updated = TESTERS | {user_id: data}
        try:
            _save_testers(updated)
        except OSError as exc:
            print(f'⚠️ Лаборатория: запрос не учтён ({type(exc).__name__}).')
            return 'unavailable'
        TESTERS.clear()
        TESTERS.update(updated)
        return 'ok'


def is_private_user_allowed(user_id):
    """Live access check. A removed user is denied without a restart."""
    if type(user_id) is not int or user_id <= 0:
        return False
    with LOCK:
        return _private_allowed_unlocked(user_id)


SESSIONS = {}  # Token -> (expiration, CSRF token); cleared on restart.
FAILED_LOGINS = {}  # Remote IP -> recent failure timestamps.


def valid_settings(value):
    if not isinstance(value, dict):
        return False
    if any(type(value.get(f'{bot}_enabled')) is not bool
           for bot in ('serega', 'furia', 'mark')):
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


def get_bot_enabled(bot):
    """One source of truth for all three bot power switches."""
    if bot not in STATS:
        raise ValueError('Unknown bot')
    with LOCK:
        return SETTINGS[f'{bot}_enabled']


def record_event(bot, kind):
    """Metadata only: no chat IDs, usernames, messages or prompts."""
    if bot not in STATS or kind not in ('received', 'sent', 'errors', 'skipped', 'power_on', 'power_off'):
        return
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    labels = {
        'received': 'Получено обновление Telegram',
        'sent': 'Отправлен ответ',
        'errors': 'Ошибка обработки',
        'skipped': 'Ответ пропущен',
        'power_on': 'Ответы включены администратором',
        'power_off': 'Ответы отключены администратором',
    }
    with LOCK:
        if kind in ('received', 'sent', 'errors', 'skipped'):
            STATS[bot][kind] += 1
        if kind in ('received', 'sent', 'errors'):
            STATS[bot]['last_' + ('error' if kind == 'errors' else kind)] = now
        EVENTS.appendleft({'time': now, 'bot': bot, 'kind': kind,
                           'text': labels[kind]})
        try:
            _save_activity()
        except OSError as exc:
            # A disk issue must never interrupt a Telegram webhook.
            print(f'⚠️ Лаборатория: статистика не сохранена ({type(exc).__name__}).')
        if kind in ('received', 'sent', 'errors', 'skipped'):
            _record_daily(bot, kind, now)


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
    with LOCK:
        stats = {name: values.copy() for name, values in STATS.items()}
        events = list(EVENTS)[:50]
        managed_users = sorted(MANAGED_USERS.items())
        access_load_error = ACCESS_LOAD_ERROR
    result = {'lab': 'online', 'version': '1.0', 'csrf': session[1],
              'started_at': STARTED_AT, 'events': events, 'bots': {},
              'access': {'managed': [{'id': str(uid), 'label': label}
                                     for uid, label in managed_users],
                         'netrun_ids': [str(uid) for uid in sorted(ENV_PRIVATE_ALLOWED_IDS)],
                         'limit': ACCESS_LIMIT, 'read_error': access_load_error,
                          'testers_read_error': TESTERS_LOAD_ERROR}}
    with LOCK:
        for entry in result['access']['managed']:
            entry.update(_tester_data(int(entry['id'])))
        result['access']['netrun'] = [
            {'id': str(uid), **_tester_data(uid)}
            for uid in sorted(ENV_PRIVATE_ALLOWED_IDS)
        ]
    for bot, name in (('serega', 'Серёга'), ('furia', 'Фурия'), ('mark', 'Марк')):
        data = {'name': name, 'enabled': settings[f'{bot}_enabled'],
                'stats': stats[bot]}
        if bot != 'serega':
            data.update({
                'probability': settings[f'{bot}_probability'],
                'delay_min': settings[f'{bot}_delay_min'],
                'delay_max': settings[f'{bot}_delay_max'],
            })
        result['bots'][bot] = data
    with LOCK:
        result['analytics'] = _analytics_snapshot()
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


async def power_handler(request):
    session = _require_session(request)
    _require_csrf(request, session)
    if request.content_length is None or request.content_length > 512:
        raise web.HTTPBadRequest(text='Неверный запрос')
    try:
        data = await request.json()
        if not isinstance(data, dict) or data.get('bot') not in STATS:
            raise ValueError('Неизвестный бот')
        if type(data.get('enabled')) is not bool:
            raise ValueError('enabled должен быть true или false')
        bot = data['bot']
        enabled = data['enabled']
        with LOCK:
            updated = SETTINGS | {f'{bot}_enabled': enabled}
            if not valid_settings(updated):
                raise ValueError('Некорректные настройки')
            _save_settings(updated)
            SETTINGS.update(updated)
    except (ValueError, TypeError, KeyError) as exc:
        raise web.HTTPBadRequest(text=f'Неверные настройки: {exc}')
    except OSError:
        raise web.HTTPInternalServerError(text='Не удалось сохранить переключатель')
    record_event(bot, 'power_on' if enabled else 'power_off')
    print(f'🧪 Лаборатория: {bot} → {"ВКЛ" if enabled else "ВЫКЛ"}')
    return _security_headers(web.json_response({'success': True, 'bot': bot, 'enabled': enabled}))


async def access_add_handler(request):
    session = _require_session(request)
    _require_csrf(request, session)
    if request.content_length is None or request.content_length > 512:
        raise web.HTTPBadRequest(text='Неверный размер запроса')
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('Ожидается объект')
        user_id = _valid_user_id(data.get('id'))
        label = data.get('label', '')
        if user_id is None or not _valid_label(label):
            raise ValueError('Неверный Telegram ID или подпись')
        label = label.strip()
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    with LOCK:
        if ACCESS_LOAD_ERROR:
            raise web.HTTPServiceUnavailable(text='Файл доступа повреждён. Исправь его вручную.')
        if user_id in ENV_PRIVATE_ALLOWED_IDS:
            raise web.HTTPConflict(text='Этот ID уже разрешён через секрет Netrun')
        if user_id in MANAGED_USERS:
            raise web.HTTPConflict(text='Этот Telegram ID уже добавлен')
        if len(MANAGED_USERS) >= ACCESS_LIMIT:
            raise web.HTTPConflict(text='Достигнут лимит пользователей')
        updated = MANAGED_USERS | {user_id: label}
        try:
            _save_access(updated)
        except OSError:
            raise web.HTTPInternalServerError(text='Не удалось сохранить список доступа')
        MANAGED_USERS.clear()
        MANAGED_USERS.update(updated)
    print('🧪 Лаборатория: пользователь добавлен в список доступа')
    return _security_headers(web.json_response({'success': True}))


async def access_remove_handler(request):
    session = _require_session(request)
    _require_csrf(request, session)
    if request.content_length is None or request.content_length > 256:
        raise web.HTTPBadRequest(text='Неверный размер запроса')
    try:
        data = await request.json()
        user_id = _valid_user_id(data.get('id')) if isinstance(data, dict) else None
        if user_id is None:
            raise ValueError('Неверный Telegram ID')
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    with LOCK:
        if ACCESS_LOAD_ERROR:
            raise web.HTTPServiceUnavailable(text='Файл доступа повреждён. Исправь его вручную.')
        if user_id not in MANAGED_USERS:
            raise web.HTTPNotFound(text='Пользователь не найден в списке Лаборатории')
        updated = MANAGED_USERS.copy()
        del updated[user_id]
        try:
            _save_access(updated)
        except OSError:
            raise web.HTTPInternalServerError(text='Не удалось сохранить список доступа')
        MANAGED_USERS.clear()
        MANAGED_USERS.update(updated)
        if not TESTERS_LOAD_ERROR and user_id not in ENV_PRIVATE_ALLOWED_IDS and user_id in TESTERS:
            updated_testers = TESTERS.copy()
            updated_testers.pop(user_id, None)
            try:
                _save_testers(updated_testers)
                TESTERS.clear()
                TESTERS.update(updated_testers)
            except OSError:
                print('⚠️ Лаборатория: не удалось очистить статистику удалённого участника')
    print('🧪 Лаборатория: пользователь удалён из списка доступа')
    return _security_headers(web.json_response({'success': True}))


async def tester_limit_handler(request):
    session = _require_session(request)
    _require_csrf(request, session)
    if request.content_length is None or request.content_length > 256:
        raise web.HTTPBadRequest(text='Неверный размер запроса')
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('Неверный запрос')
        user_id = _valid_user_id(data.get('id'))
        limit = data.get('limit')
        if user_id is None or type(limit) is not int or not 0 <= limit <= TESTER_REQUEST_LIMIT_MAX:
            raise ValueError('Лимит должен быть целым числом от 0 до 10000')
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    with LOCK:
        if TESTERS_LOAD_ERROR:
            raise web.HTTPServiceUnavailable(text='Файл статистики повреждён. Изменения заблокированы.')
        if not _private_allowed_unlocked(user_id):
            raise web.HTTPNotFound(text='Участник не найден')
        item = _tester_data(user_id)
        item['limit'] = limit
        updated = TESTERS | {user_id: item}
        try:
            _save_testers(updated)
        except OSError:
            raise web.HTTPInternalServerError(text='Не удалось сохранить лимит')
        TESTERS.clear()
        TESTERS.update(updated)
    return _security_headers(web.json_response({'success': True}))


async def tester_reset_handler(request):
    session = _require_session(request)
    _require_csrf(request, session)
    if request.content_length is None or request.content_length > 256:
        raise web.HTTPBadRequest(text='Неверный размер запроса')
    try:
        data = await request.json()
        user_id = _valid_user_id(data.get('id')) if isinstance(data, dict) else None
        if user_id is None:
            raise ValueError('Неверный Telegram ID')
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    with LOCK:
        if TESTERS_LOAD_ERROR:
            raise web.HTTPServiceUnavailable(text='Файл статистики повреждён. Изменения заблокированы.')
        if not _private_allowed_unlocked(user_id):
            raise web.HTTPNotFound(text='Участник не найден')
        item = _tester_data(user_id)
        item['requests'] = 0
        updated = TESTERS | {user_id: item}
        try:
            _save_testers(updated)
        except OSError:
            raise web.HTTPInternalServerError(text='Не удалось сбросить счётчик')
        TESTERS.clear()
        TESTERS.update(updated)
    return _security_headers(web.json_response({'success': True}))


async def redirect_handler(request):
    raise web.HTTPFound('/lab/')


def register_routes(app):
    app.router.add_get('/lab', redirect_handler)
    app.router.add_get('/lab/', page_handler)
    app.router.add_post('/lab/login', login_handler)
    app.router.add_post('/lab/logout', logout_handler)
    app.router.add_get('/lab/api/status', status_handler)
    app.router.add_post('/lab/api/settings', settings_handler)
    app.router.add_post('/lab/api/power', power_handler)
    app.router.add_post('/lab/api/access/add', access_add_handler)
    app.router.add_post('/lab/api/access/remove', access_remove_handler)
    app.router.add_post('/lab/api/testers/limit', tester_limit_handler)
    app.router.add_post('/lab/api/testers/reset', tester_reset_handler)
    if LAB_ENABLED:
        print(f'🧪 Frankenstein Lab v1.0: включена; настройки в {SETTINGS_FILE}')
    else:
        print('🔒 Frankenstein Lab отключена: задай LAB_PASSWORD длиной от 12 символов.')
