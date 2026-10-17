"""Frankenstein Lab v1.4: password-protected controls for the shared aiohttp host.

Requires LAB_PASSWORD (at least 12 characters). Without it all /lab routes return 404.
Do not run the old local lab_server.py in the cloud.
"""

import hmac
from collections import deque
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
COSTS_FILE = DATA_DIR / 'lab_costs.json'
ACCESS_FILE = DATA_DIR / 'lab_access.json'
TESTERS_FILE = DATA_DIR / 'lab_testers.json'
DIAGNOSTICS_FILE = DATA_DIR / 'lab_diagnostics.json'
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

# v1.2: financial accounting is isolated from bot settings and access lists.
# Never store prompts, images, chat IDs, transcripts, or API credentials.
# Previous v1.1 /data/lab_costs.json records are preserved on migration.
COSTS_RETENTION_DAYS = 90
LEGACY_COST_FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens',
                      'reasoning_output_tokens', 'audio_seconds',
                      'estimated_cost_microusd', 'priced_calls',
                      'unpriced_calls', 'usage_missing_calls')
COST_FIELDS = LEGACY_COST_FIELDS + (
    'cache_write_tokens', 'cache_write_12h_tokens', 'uncached_input_tokens',
    'unclassified_input_tokens', 'estimated_cache_write_tokens',
)
COSTS = {}  # YYYY-MM-DD -> bot -> model -> aggregate

# Price configuration is intentionally opt-in. All prices are USD.
# Netrun environment variable examples (illustrative, NOT official tariffs):
# LAB_LUNA_CACHE_WRITE_USD_PER_M=0.25
# LAB_LUNA_CACHE_READ_USD_PER_M=0.025
# LAB_LUNA_INPUT_USD_PER_M=0.25
# LAB_LUNA_OUTPUT_USD_PER_M=1.19
# LAB_TRANSCRIBE_USD_PER_MIN=...  (only if verified)
# LAB_LUNA_ASSUME_UNCACHED_WRITES=1  (estimate, not an exact measurement)
# Without the opt-in assumption, unreported cache writes remain unclassified.


def _empty_cost_model():
    return {field: 0 for field in COST_FIELDS}


def _load_costs():
    if not COSTS_FILE.exists():
        return {}, False
    try:
        if COSTS_FILE.stat().st_size > 512 * 1024:
            raise ValueError('Cost file too large')
        data = json.loads(COSTS_FILE.read_text(encoding='utf-8'))
        version = data.get('version') if isinstance(data, dict) else None
        if (version not in (1, 2) or not isinstance(data.get('days'), dict)
                or len(data['days']) > COSTS_RETENTION_DAYS):
            raise ValueError('Invalid costs file')
        required_fields = set(LEGACY_COST_FIELDS if version == 1 else COST_FIELDS)
        loaded = {}
        for day, bots in data['days'].items():
            if (not isinstance(day, str) or len(day) != 10
                    or datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day
                    or not isinstance(bots, dict) or set(bots) != set(STATS)):
                raise ValueError('Invalid cost day')
            loaded[day] = {}
            for bot, models in bots.items():
                if not isinstance(models, dict) or len(models) > 24:
                    raise ValueError('Invalid cost models')
                loaded[day][bot] = {}
                for model, values in models.items():
                    if (not isinstance(model, str) or not 1 <= len(model) <= 80
                            or not isinstance(values, dict)
                            or set(values) != required_fields
                            or any(type(v) is not int or v < 0 or v > 10**15
                                   for v in values.values())):
                        raise ValueError('Invalid cost values')
                    # Never reinterpret historical v1.1 tokens as cache writes.
                    loaded[day][bot][model] = _empty_cost_model() | values
        if version == 1:
            print('🧪 Лаборатория: расходы v1.1 загружены без потери истории')
        return loaded, False
    except (OSError, ValueError, TypeError, OverflowError):
        print('⚠️ Лаборатория: файл расходов повреждён; запись расходов заблокирована.')
        return {}, True


def _save_costs(days):
    temporary = COSTS_FILE.with_suffix('.json.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as file:
            json.dump({'version': 2, 'days': days}, file, ensure_ascii=False,
                      separators=(',', ':'))
        os.replace(temporary, COSTS_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


COSTS, COSTS_LOAD_ERROR = _load_costs()


def _usage_value(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _safe_count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else 0


def _safe_rate(value):
    # Reject NaN, infinity, booleans, negative numbers and malformed strings.
    if value is None or type(value) is bool:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and 0 <= result <= 100000 else None


def _env_rate(name):
    raw = os.getenv(name)
    return _safe_rate(raw) if raw is not None and raw.strip() else None


def _luna_model(model):
    normalized = model.lower().replace('.', '_').replace('-', '_')
    return normalized.startswith('gpt_5_6_luna')


def _reported_cache_write_tokens(usage, details, available):
    """Read write tokens only when an API response explicitly reports them."""
    for obj, key in (
        (details, 'cache_write_tokens'),
        (details, 'cache_creation_tokens'),
        (usage, 'cache_creation_input_tokens'),
        (usage, 'cache_write_input_tokens'),
    ):
        value = _usage_value(obj, key)
        if type(value) is int and 0 <= value <= available:
            return value
    return None


def record_ai_usage(bot, kind, response=None, *, model=None, audio_seconds=None,
                    input_price_per_million=None, output_price_per_million=None,
                    cached_input_price_per_million=None,
                    transcription_price_per_minute=None,
                    cache_write_price_per_million=None,
                    cache_write_12h_price_per_million=None):
    """Record a successful OpenAI call without interrupting Telegram handling.

    The three bots already call this after each API response. Prices are read
    from opt-in environment variables (or explicit keyword overrides), never
    guessed. Token counts for cache reads and writes are kept separately.
    When a write count is unavailable, it stays unclassified unless the owner
    explicitly enables LAB_LUNA_ASSUME_UNCACHED_WRITES=1. That estimate may
    overcount writes by including truly uncached input.
    """
    if bot not in STATS or kind not in ('generation', 'transcription'):
        return
    if model is None:
        model = _usage_value(response, 'model')
    if not isinstance(model, str) or not 1 <= len(model) <= 80:
        model = 'unknown'
    usage = _usage_value(response, 'usage')
    inp = _safe_count(_usage_value(usage, 'input_tokens'))
    out = _safe_count(_usage_value(usage, 'output_tokens'))
    details = _usage_value(usage, 'input_tokens_details')
    out_details = _usage_value(usage, 'output_tokens_details')
    cached = min(inp, _safe_count(_usage_value(details, 'cached_tokens')))
    reasoning = min(out, _safe_count(_usage_value(out_details, 'reasoning_tokens')))
    seconds = audio_seconds if audio_seconds is not None else _usage_value(usage, 'seconds')
    if type(seconds) not in (int, float) or not 0 <= seconds <= 10**9:
        seconds = 0
    import math
    seconds = math.ceil(seconds)
    has_usage = usage is not None or seconds > 0
    is_luna = _luna_model(model)
    is_transcription = kind == 'transcription'

    noncached = inp - cached
    writes = 0
    writes_12h = 0
    estimated_writes = 0
    unclassified = 0
    uncached = 0
    if not is_transcription and noncached:
        reported = _reported_cache_write_tokens(usage, details, noncached)
        reported_12h = _usage_value(details, 'cache_write_12h_tokens')
        if reported_12h is None:
            reported_12h = _usage_value(usage, 'cache_write_12h_tokens')
        if type(reported_12h) is not int or not 0 <= reported_12h <= noncached:
            reported_12h = 0
        if reported is not None:
            writes_12h = min(reported_12h, reported)
            writes = reported - writes_12h
            uncached = noncached - reported
        elif is_luna and os.getenv('LAB_LUNA_ASSUME_UNCACHED_WRITES', '').strip() == '1':
            writes_12h = reported_12h
            writes = noncached - writes_12h
            estimated_writes = writes
        else:
            # Unknown split between write and uncached: don't price it blindly.
            unclassified = noncached

    input_rate = _safe_rate(input_price_per_million)
    output_rate = _safe_rate(output_price_per_million)
    cached_rate = _safe_rate(cached_input_price_per_million)
    write_rate = _safe_rate(cache_write_price_per_million)
    write_12h_rate = _safe_rate(cache_write_12h_price_per_million)
    audio_rate = _safe_rate(transcription_price_per_minute)
    if is_luna:
        if input_rate is None:
            input_rate = _env_rate('LAB_LUNA_INPUT_USD_PER_M')
        if output_rate is None:
            output_rate = _env_rate('LAB_LUNA_OUTPUT_USD_PER_M')
        if cached_rate is None:
            cached_rate = _env_rate('LAB_LUNA_CACHE_READ_USD_PER_M')
        if write_rate is None:
            write_rate = _env_rate('LAB_LUNA_CACHE_WRITE_USD_PER_M')
        if write_12h_rate is None:
            write_12h_rate = _env_rate('LAB_LUNA_CACHE_WRITE_12H_USD_PER_M')
    if is_transcription and audio_rate is None and model.lower().startswith('gpt-transcribe'):
        audio_rate = _env_rate('LAB_TRANSCRIBE_USD_PER_MIN')

    cost_micro = 0
    if is_transcription:
        # A transcription is billed by EITHER time OR token usage, not both.
        # Prefer the verified minute tariff if duration is available.
        if seconds > 0 and audio_rate is not None:
            priced = True
            dollars = Decimal(seconds) * audio_rate / Decimal(60)
        elif seconds == 0 and (inp or out) and input_rate is not None and output_rate is not None:
            priced = True
            dollars = (Decimal(inp) * input_rate + Decimal(out) * output_rate) / Decimal(1_000_000)
        else:
            priced = False
            dollars = Decimal(0)
    else:
        priced = (has_usage and (inp > 0 or out > 0)
                  and unclassified == 0
                  and (uncached == 0 or input_rate is not None)
                  and (cached == 0 or cached_rate is not None)
                  and (writes == 0 or write_rate is not None)
                  and (writes_12h == 0 or write_12h_rate is not None)
                  and (out == 0 or output_rate is not None))
        dollars = Decimal(0)
        if priced:
            dollars = (Decimal(uncached) * (input_rate or 0)
                       + Decimal(cached) * (cached_rate or 0)
                       + Decimal(writes) * (write_rate or 0)
                       + Decimal(writes_12h) * (write_12h_rate or 0)
                       + Decimal(out) * (output_rate or 0)) / Decimal(1_000_000)
    if priced:
        cost_micro = int((dollars * Decimal(1_000_000)).quantize(
            Decimal('1'), rounding=ROUND_HALF_UP))
    now = datetime.now(timezone.utc).date()
    cutoff = (now - timedelta(days=COSTS_RETENTION_DAYS - 1)).isoformat()
    day = now.isoformat()
    with LOCK:
        if COSTS_LOAD_ERROR:
            return
        updated = {d: bots for d, bots in COSTS.items() if d >= cutoff}
        daily = {name: models.copy() for name, models in updated.get(day, {
            name: {} for name in STATS
        }).items()}
        if model not in daily[bot] and len(daily[bot]) >= 24:
            model = 'other'
        previous = daily[bot].get(model, _empty_cost_model())
        values = previous.copy()
        increments = {
            'input_tokens': inp, 'output_tokens': out,
            'cached_input_tokens': cached, 'reasoning_output_tokens': reasoning,
            'audio_seconds': seconds, 'estimated_cost_microusd': cost_micro,
            'priced_calls': int(priced),
            'unpriced_calls': int(has_usage and not priced),
            'usage_missing_calls': int(not has_usage),
            'cache_write_tokens': writes,
            'cache_write_12h_tokens': writes_12h,
            'uncached_input_tokens': uncached,
            'unclassified_input_tokens': unclassified,
            'estimated_cache_write_tokens': estimated_writes,
        }
        if any(values[key] + inc > 10**15 for key, inc in increments.items()):
            return
        for key, inc in increments.items():
            values[key] += inc
        daily[bot][model] = values
        updated[day] = daily
        try:
            _save_costs(updated)
        except OSError as exc:
            print(f'⚠️ Лаборатория: расходы не сохранены ({type(exc).__name__}).')
            return
        COSTS.clear()
        COSTS.update(updated)


def _costs_snapshot():
    """Under LOCK. 30 UTC days; prices in micro-USD, never float money."""
    today = datetime.now(timezone.utc).date()
    result = []
    for offset in range(29, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        bots = {}
        for bot in STATS:
            models = COSTS.get(day, {}).get(bot, {})
            totals = _empty_cost_model()
            for values in models.values():
                for field in COST_FIELDS:
                    totals[field] += values[field]
            bots[bot] = {'totals': totals,
                         'models': {name: values.copy() for name, values in models.items()}}
        result.append({'date': day, 'bots': bots})
    return {'timezone': 'UTC', 'days': result, 'read_error': COSTS_LOAD_ERROR,
            'currency': 'USD', 'unit': 'micro_usd', 'estimated': True,
            'pricing_version': 2,
            'pricing_mode': 'configured_rates_only',
            'cache_write_inference_enabled':
                os.getenv('LAB_LUNA_ASSUME_UNCACHED_WRITES', '').strip() == '1'}



# v1.4: independent, privacy-safe diagnostic journal. Bot integrations call
# record_diagnostic() when an actual failure occurs. Never persist raw error
# messages, tracebacks, Telegram content, prompts, user IDs, or API keys.
DIAGNOSTICS_MAX_EVENTS = 200
DIAGNOSTICS_SOURCES = frozenset(('openai', 'telegram', 'webhook', 'system'))
DIAGNOSTICS_OPERATIONS = frozenset((
    'generation', 'transcription', 'send_message', 'send_photo',
    'get_updates', 'handle_update', 'webhook', 'startup', 'background', 'other',
))
DIAGNOSTICS = []  # Newest first; last 200 only.
DIAGNOSTICS_COUNTS = {bot: 0 for bot in STATS}


def _load_diagnostics():
    if not DIAGNOSTICS_FILE.exists():
        return [], {bot: 0 for bot in STATS}, False
    try:
        if DIAGNOSTICS_FILE.stat().st_size > 128 * 1024:
            raise ValueError('Diagnostic file too large')
        data = json.loads(DIAGNOSTICS_FILE.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('events'), list)
                or len(data['events']) > DIAGNOSTICS_MAX_EVENTS
                or not isinstance(data.get('counts'), dict)
                or set(data['counts']) != set(STATS)):
            raise ValueError('Invalid diagnostics structure')
        counts = data['counts']
        if any(type(value) is not int or not 0 <= value <= 1_000_000_000
               for value in counts.values()):
            raise ValueError('Invalid diagnostics counts')
        events = []
        for item in data['events']:
            if not isinstance(item, dict) or set(item) != {
                    'time', 'bot', 'source', 'operation', 'error_type'}:
                raise ValueError('Invalid diagnostic event')
            stamp = item['time']
            if (not isinstance(stamp, str) or len(stamp) > 40
                    or item['bot'] not in STATS
                    or item['source'] not in DIAGNOSTICS_SOURCES
                    or item['operation'] not in DIAGNOSTICS_OPERATIONS
                    or not isinstance(item['error_type'], str)
                    or not 1 <= len(item['error_type']) <= 64
                    or not item['error_type'].isascii()
                    or not all(c.isalnum() or c == '_' for c in item['error_type'])):
                raise ValueError('Invalid diagnostic values')
            datetime.fromisoformat(stamp)
            events.append(item.copy())
        if any(counts[bot] < sum(e['bot'] == bot for e in events) for bot in STATS):
            raise ValueError('Invalid diagnostic totals')
        print(f'🧪 Лаборатория: журнал диагностики восстановлен ({len(events)} записей)')
        return events, counts.copy(), False
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        print('⚠️ Лаборатория: журнал диагностики повреждён; запись заблокирована.')
        return [], {bot: 0 for bot in STATS}, True


def _save_diagnostics(events, counts):
    """Called under LOCK; a failed write leaves the old file untouched."""
    temporary = DIAGNOSTICS_FILE.with_suffix('.json.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as file:
            json.dump({'version': 1, 'events': events, 'counts': counts},
                      file, ensure_ascii=False, separators=(',', ':'))
        os.replace(temporary, DIAGNOSTICS_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


DIAGNOSTICS, DIAGNOSTICS_COUNTS, DIAGNOSTICS_LOAD_ERROR = _load_diagnostics()


def record_diagnostic(bot, source, operation='other', error=None):
    """Persist safe metadata for a real bot failure; never log exception text.

    Example: record_diagnostic('furia', 'openai', 'generation', exc)
    Keep existing record_event(bot, 'errors') calls for legacy counters.
    """
    if (bot not in STATS or source not in DIAGNOSTICS_SOURCES
            or operation not in DIAGNOSTICS_OPERATIONS):
        return
    # Exception class only. str(error) could leak messages, tokens or IDs.
    if isinstance(error, BaseException):
        error_type = type(error).__name__
    elif isinstance(error, type) and issubclass(error, BaseException):
        error_type = error.__name__
    else:
        error_type = 'UnknownError'
    if (not error_type.isascii() or len(error_type) > 64
            or not all(c.isalnum() or c == '_' for c in error_type)):
        error_type = 'UnknownError'
    entry = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'),
             'bot': bot, 'source': source, 'operation': operation,
             'error_type': error_type}
    with LOCK:
        if DIAGNOSTICS_LOAD_ERROR or DIAGNOSTICS_COUNTS[bot] >= 1_000_000_000:
            return
        updated_events = [entry] + DIAGNOSTICS[:DIAGNOSTICS_MAX_EVENTS - 1]
        updated_counts = DIAGNOSTICS_COUNTS | {bot: DIAGNOSTICS_COUNTS[bot] + 1}
        try:
            _save_diagnostics(updated_events, updated_counts)
        except OSError:
            print('⚠️ Лаборатория: ошибка сохранения журнала диагностики.')
            return
        DIAGNOSTICS[:] = updated_events
        DIAGNOSTICS_COUNTS.update(updated_counts)


def _diagnostics_snapshot():
    """Under LOCK. Counters are since the last journal clear."""
    return {
        'events': [item.copy() for item in DIAGNOSTICS],
        'counts': DIAGNOSTICS_COUNTS.copy(),
        'last_error': {bot: next((item['time'] for item in DIAGNOSTICS
                                 if item['bot'] == bot), None) for bot in STATS},
        'read_error': DIAGNOSTICS_LOAD_ERROR,
        'limit': DIAGNOSTICS_MAX_EVENTS,
        'timezone': 'UTC',
    }


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
    result = {'lab': 'online', 'version': '1.4', 'csrf': session[1],
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
        result['costs'] = _costs_snapshot()
        result['diagnostics'] = _diagnostics_snapshot()
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



async def diagnostics_clear_handler(request):
    """Clear only the diagnostic journal, not legacy stats or costs."""
    session = _require_session(request)
    _require_csrf(request, session)
    if request.content_length is not None and request.content_length > 256:
        raise web.HTTPBadRequest(text='Неверный размер запроса')
    with LOCK:
        if DIAGNOSTICS_LOAD_ERROR:
            raise web.HTTPServiceUnavailable(text='Журнал повреждён. Очистка заблокирована.')
        empty_counts = {bot: 0 for bot in STATS}
        try:
            _save_diagnostics([], empty_counts)
        except OSError:
            raise web.HTTPInternalServerError(text='Не удалось очистить журнал')
        DIAGNOSTICS.clear()
        DIAGNOSTICS_COUNTS.update(empty_counts)
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
    app.router.add_post('/lab/api/diagnostics/clear', diagnostics_clear_handler)
    if LAB_ENABLED:
        print(f'🧪 Frankenstein Lab v1.4: включена; настройки в {SETTINGS_FILE}')
    else:
        print('🔒 Frankenstein Lab отключена: задай LAB_PASSWORD длиной от 12 символов.')
