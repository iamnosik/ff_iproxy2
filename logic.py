"""Low-latency HTTP MPEG-TS pass-through for I-Proxy channels.

This intentionally does not repack or transcode.  A client receives the bytes
produced by udpxy (or another HTTP MPEG-TS source) as soon as the upstream
connection is open.  It is therefore only suitable for sources that players
can already decode as MPEG-TS.
"""

import html
import json
import sqlite3
import urllib.request
from contextlib import closing
from pathlib import Path

from flask import Response, abort, request, stream_with_context
from plugin import F, PluginModuleBase

from .setup import P

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting
SystemModelSetting = F.SystemModelSetting
blueprint = P.blueprint


def _public_api_key():
    if not SystemModelSetting.get_bool('use_apikey'):
        return ''
    return SystemModelSetting.get('apikey') or ''


def _require_api_key(req):
    expected = _public_api_key()
    if expected and req.args.get('apikey') != expected:
        abort(403)


def _with_api_key(url):
    api_key = _public_api_key()
    return f'{url}?apikey={api_key}' if api_key else url


def _base_url(req=None):
    ddns = (SystemModelSetting.get('ddns') or '').strip()
    if ddns:
        return f'{ddns.rstrip("/")}/{package_name}'
    req = req or request
    return f'{req.url_root.rstrip("/")}/{package_name}'


def _source_db_path():
    return Path(F.path_data) / 'db' / 'ff_iproxy.db'


def load_channels():
    """Load the currently saved I-Proxy channel list without modifying it."""
    db_path = _source_db_path()
    if not db_path.exists():
        logger.warning('[PASSTHROUGH] I-Proxy DB not found: %s', db_path)
        return []

    try:
        with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)) as conn:
            row = conn.execute(
                'SELECT value FROM ff_iproxy_setting WHERE key = ?', ('manual_channels',)
            ).fetchone()
    except sqlite3.Error:
        logger.exception('[PASSTHROUGH] Failed to read I-Proxy channel DB')
        return []

    try:
        raw_channels = json.loads((row or ['[]'])[0] or '[]')
    except (TypeError, ValueError):
        logger.exception('[PASSTHROUGH] I-Proxy manual_channels is invalid JSON')
        return []

    channels = []
    for item in raw_channels if isinstance(raw_channels, list) else []:
        if not isinstance(item, dict) or item.get('hidden'):
            continue
        channel_id = str(item.get('id') or '').strip()
        name = str(item.get('name') or channel_id).strip()
        url = str(item.get('url') or '').strip()
        if not channel_id or not name or not url.startswith(('http://', 'https://')):
            continue
        channels.append({'id': channel_id, 'name': name, 'url': url})
    return channels


def get_channel(channel_id):
    return next((channel for channel in load_channels() if channel['id'] == channel_id), None)


@stream_with_context
def stream_response(upstream, channel):
    with closing(upstream):
        try:
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            logger.info('[PASSTHROUGH] client_closed channel=%s', channel['id'])
            raise
        except Exception:
            logger.exception('[PASSTHROUGH] stream_error channel=%s source=%s', channel['id'], channel['url'])
            raise
        finally:
            logger.info('[PASSTHROUGH] end channel=%s', channel['id'])


def open_source(channel):
    user_agent = (ModelSetting.get('user_agent') or 'Mozilla/5.0').strip()
    try:
        timeout = max(3, int(ModelSetting.get('connect_timeout') or 10))
    except (TypeError, ValueError):
        timeout = 10
        logger.warning('[PASSTHROUGH] Invalid connect_timeout; using %s seconds', timeout)
    req = urllib.request.Request(channel['url'], headers={'User-Agent': user_agent})
    try:
        upstream = urllib.request.urlopen(req, timeout=timeout)
    except Exception as error:
        logger.warning('[PASSTHROUGH] open_failed channel=%s source=%s error=%s', channel['id'], channel['url'], error)
        abort(502)
    logger.info('[PASSTHROUGH] start channel=%s source=%s', channel['id'], channel['url'])
    return upstream


class Logic(PluginModuleBase):
    db_default = {
        'user_agent': 'Mozilla/5.0',
        'connect_timeout': '10',
    }

    def __init__(self, PM):
        super().__init__(PM, None)
        self.name = 'setting'

    def process_menu(self, sub, req):
        _ = sub
        playlist_url = _with_api_key(f'{_base_url(req)}/api/m3u')
        channels = load_channels()
        body = [
            '<h3>I-Proxy2: FFmpeg 없는 MPEG-TS 패스스루</h3>',
            '<p>현재 I-Proxy의 HTTP 채널을 읽어 그대로 전달합니다.</p>',
            f'<p>M3U: <code>{html.escape(playlist_url)}</code></p>',
            f'<p>활성 채널: {len(channels)}</p>',
        ]
        return Response('\n'.join(body), content_type='text/html; charset=utf-8')


@blueprint.route('/api/m3u', methods=['GET'])
def api_m3u():
    _require_api_key(request)
    base = _base_url(request)
    lines = ['#EXTM3U']
    for channel in load_channels():
        lines.append(f'#EXTINF:-1 tvg-name="{channel["name"]}",{channel["name"]}')
        lines.append(_with_api_key(f'{base}/api/channel/{channel["id"]}'))
    return Response('\n'.join(lines) + '\n', content_type='audio/mpegurl')


@blueprint.route('/api/channel/<channel_id>', methods=['GET'])
def api_channel(channel_id):
    _require_api_key(request)
    channel = get_channel(channel_id)
    if channel is None:
        abort(404)
    upstream = open_source(channel)
    response = Response(stream_response(upstream, channel), mimetype='video/mp2t', direct_passthrough=True)
    response.headers['Cache-Control'] = 'no-cache, no-store'
    response.headers['X-Accel-Buffering'] = 'no'
    return response
