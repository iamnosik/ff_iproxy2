import json
import os
import re
import subprocess
import threading
import time
import traceback
import urllib.parse
import html
from datetime import datetime
from pathlib import Path

from flask import Response, abort, jsonify, redirect, render_template, request
from plugin import F, PluginModuleBase

try:
    import yaml
except Exception:
    yaml = None

from .setup import P
from .epg_service import (
    EPG_HTTP_HEADERS,
    EPG_SOURCE_OPTIONS,
    append_epg_log,
    generate_epg_xml,
    get_current_program_map,
    get_epg_output_path,
    read_epg_log_tail,
    sync_epg_scheduler_job,
)
from .channel_service import (
    get_bundled_channels_raw,
    get_channel_map,
    get_channels,
    get_default_manual_channels_source_path,
    get_manual_channels_raw,
    get_manual_channels_source_path,
    get_operation_mode,
    has_supported_url_type_param,
    import_channels_from_source,
    make_channel_id,
    normalize_url_to_localhost_if_ddns,
    parse_bool,
    parse_manual_channels,
    parse_simple_yaml_scalar,
    replace_url_type,
    update_manual_channel_type,
)
from .stream_service import (
    candidate_h264_encoders,
    encoder_is_usable,
    ensure_cleanup_thread,
    fetch_url,
    get_ffmpeg_bin,
    get_hls_default_options,
    get_hls_dir,
    get_hls_key_from_segment_filename,
    get_hls_playlist_path,
    get_hls_request_options,
    get_hls_session_dir,
    probe_channel_type,
    start_hls,
    stream_via_ffmpeg_copy,
    stream_via_ffmpeg_from_hls,
    touch_hls,
    wait_for_hls_ready,
)

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting
SystemModelSetting = F.SystemModelSetting
blueprint = P.blueprint
_show_yaml_cache = {}
_show_yaml_cache_lock = threading.Lock()
_SHOW_YAML_CACHE_TTL = 10 * 60
SHOW_YAML_DEFAULT_TYPE = 'repack'

def get_public_api_key():
    use_apikey = SystemModelSetting.get_bool('use_apikey')
    if not use_apikey:
        return None
    apikey = SystemModelSetting.get('apikey')
    return apikey or None


def require_api_key(req):
    expected = get_public_api_key()
    if expected is None:
        return
    actual = req.args.get('apikey')
    if actual != expected:
        abort(403)


def get_base_url(req=None):
    ddns = SystemModelSetting.get('ddns')
    if ddns:
        return ddns.rstrip('/') + f'/{package_name}'
    if req is None:
        req = request
    return req.url_root.rstrip('/') + f'/{package_name}'


def with_apikey(url):
    apikey = get_public_api_key()
    if not apikey:
        return url
    separator = '&' if '?' in url else '?'
    return f'{url}{separator}apikey={apikey}'


def with_request_apikey(url, req=None):
    if req is None:
        req = request
    apikey = ''
    try:
        apikey = (req.args.get('apikey') or '').strip()
    except Exception:
        apikey = ''
    if apikey:
        separator = '&' if '?' in url else '?'
        return f'{url}{separator}apikey={apikey}'
    return with_apikey(url)

def get_alive_yaml_path():
    return Path(F.path_data) / 'db' / 'alive.yaml'


def _escape_alive_yaml_scalar(value):
    text = str(value or '')
    return text.replace('\\', '\\\\').replace('"', '\\"')


def build_alive_fix_url_block(channels):
    lines = [
        '  fix_url:',
    ]
    for channel in channels:
        channel_name = str(channel.get('name') or '').strip()
        channel_url = str(channel.get('url') or '').strip()
        if not channel_name or not channel_url:
            continue
        escaped_name = _escape_alive_yaml_scalar(channel_name)
        escaped_url = _escape_alive_yaml_scalar(channel_url)
        lines.extend([
            f'    "{escaped_name}":',
            f'      name: "{escaped_name}"',
            f'      url: "{escaped_url}"',
        ])
    return '\n'.join(lines) + '\n'


def parse_alive_fix_url_block(lines, fix_url_indent):
    if not lines:
        return []
    normalized_lines = []
    for line in lines:
        if line.startswith(' ' * fix_url_indent):
            normalized_lines.append(line[fix_url_indent:])
        else:
            normalized_lines.append(line.lstrip(' '))
    block_text = '\n'.join(normalized_lines) + '\n'

    channels = []
    if yaml is None:
        return parse_alive_fix_url_block_simple(normalized_lines)

    try:
        parsed = yaml.safe_load(block_text) or {}
        fix_url = parsed.get('fix_url')
        if not isinstance(fix_url, dict):
            return parse_alive_fix_url_block_simple(normalized_lines)

        for key, item in fix_url.items():
            if not isinstance(item, dict):
                continue
            channel_name = str(item.get('name') or key or '').strip()
            channel_url = str(item.get('url') or '').strip()
            if not channel_name or not channel_url:
                continue
            channels.append({
                'name': channel_name,
                'url': channel_url,
            })
    except Exception:
        logger.exception('Failed to parse existing fix_url block from alive.yaml')
        return parse_alive_fix_url_block_simple(normalized_lines)
    return channels


def parse_alive_fix_url_block_simple(lines):
    channels = []
    current_key = None
    current_item = None
    entry_indent = None

    for line in lines:
        raw = line.rstrip()
        stripped = raw.strip()
        if not stripped or stripped.startswith('#') or stripped == 'fix_url:':
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        if stripped.endswith(':') and not stripped.startswith(('name:', 'url:')):
            if current_item is not None:
                channel_name = str(current_item.get('name') or current_key or '').strip()
                channel_url = str(current_item.get('url') or '').strip()
                if channel_name and channel_url:
                    channels.append({'name': channel_name, 'url': channel_url})
            current_key = parse_simple_yaml_scalar(stripped[:-1])
            current_item = {}
            entry_indent = indent
            continue
        if current_item is None or ':' not in stripped:
            continue
        if entry_indent is not None and indent <= entry_indent:
            continue
        key, value = stripped.split(':', 1)
        key = key.strip()
        if key in ('name', 'url'):
            current_item[key] = parse_simple_yaml_scalar(value.strip())

    if current_item is not None:
        channel_name = str(current_item.get('name') or current_key or '').strip()
        channel_url = str(current_item.get('url') or '').strip()
        if channel_name and channel_url:
            channels.append({'name': channel_name, 'url': channel_url})
    return channels


def merge_alive_fix_url_channels(existing_channels, new_channels):
    merged = [
        {
            'name': str(channel.get('name') or '').strip(),
            'url': str(channel.get('url') or '').strip(),
        }
        for channel in existing_channels
        if str(channel.get('name') or '').strip() and str(channel.get('url') or '').strip()
    ]

    for channel in new_channels:
        channel_name = str(channel.get('name') or '').strip()
        channel_url = str(channel.get('url') or '').strip()
        if not channel_name or not channel_url:
            continue

        replace_index = None
        for index, existing in enumerate(merged):
            existing_name = str(existing.get('name') or '').strip()
            existing_url = str(existing.get('url') or '').strip()
            if existing_name == channel_name or existing_url == channel_url:
                replace_index = index
                break

        if replace_index is None:
            merged.append({
                'name': channel_name,
                'url': channel_url,
            })
        else:
            merged[replace_index] = {
                'name': channel_name,
                'url': channel_url,
            }

    return merged


def remove_alive_fix_url_channels(existing_channels, removed_channels):
    removed_names = set()
    removed_urls = set()
    for channel in removed_channels:
        channel_name = str(channel.get('name') or '').strip()
        channel_url = str(channel.get('url') or '').strip()
        if channel_name:
            removed_names.add(channel_name)
        if channel_url:
            removed_urls.add(channel_url)

    if not removed_names and not removed_urls:
        return existing_channels

    kept = []
    for channel in existing_channels:
        channel_name = str(channel.get('name') or '').strip()
        channel_url = str(channel.get('url') or '').strip()
        if channel_name in removed_names or channel_url in removed_urls:
            continue
        kept.append(channel)
    return kept


def get_alive_yaml_mode():
    mode = (ModelSetting.get('alive_yaml_mode') or 'mpegts').strip().lower()
    return 'hls' if mode == 'hls' else 'mpegts'


def _find_block_end(lines, start_index, parent_indent):
    for index in range(start_index + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped == '':
            continue
        current_indent = len(lines[index]) - len(lines[index].lstrip(' '))
        if current_indent <= parent_indent:
            return index
    return len(lines)


def update_alive_fix_url(req):
    path = get_alive_yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        original_text = path.read_text(encoding='utf-8')
    else:
        original_text = ''
    lines = original_text.splitlines()

    alive_channels = []
    hidden_alive_channels = []
    alive_mode = get_alive_yaml_mode()
    include_hidden = ModelSetting.get_bool('include_hidden_channels')
    for channel in get_channels():
        payload = make_channel_payload(channel, req)
        channel_name = str(payload.get('name') or '').strip()
        if not channel_name:
            continue
        alive_channel = {
            'name': channel_name,
            'url': payload['hls_url'] if alive_mode == 'hls' else payload['mpegts_url'],
        }
        if channel.get('hidden') and not include_hidden:
            hidden_alive_channels.append(alive_channel)
            continue
        alive_channels.append(alive_channel)

    channel_source_index = None
    channel_source_indent = 0
    for index, line in enumerate(lines):
        if line.strip() == 'channel_source:':
            channel_source_index = index
            channel_source_indent = len(line) - len(line.lstrip(' '))
            break

    existing_fix_url_channels = []
    fix_url_index = None
    fix_url_end = None
    if channel_source_index is not None:
        channel_source_end = _find_block_end(lines, channel_source_index, channel_source_indent)
        fix_url_indent = channel_source_indent + 2
        for index in range(channel_source_index + 1, channel_source_end):
            if lines[index].strip() == 'fix_url:':
                current_indent = len(lines[index]) - len(lines[index].lstrip(' '))
                if current_indent == fix_url_indent:
                    fix_url_index = index
                    break
        if fix_url_index is not None:
            fix_url_end = _find_block_end(lines, fix_url_index, fix_url_indent)
            existing_fix_url_channels = parse_alive_fix_url_block(lines[fix_url_index:fix_url_end], fix_url_indent)

    existing_fix_url_channels = remove_alive_fix_url_channels(existing_fix_url_channels, hidden_alive_channels)
    merged_alive_channels = merge_alive_fix_url_channels(existing_fix_url_channels, alive_channels)
    block_text = build_alive_fix_url_block(merged_alive_channels).rstrip('\n')

    if channel_source_index is None:
        if original_text and not original_text.endswith('\n'):
            original_text += '\n'
        prefix = original_text
        if prefix and not prefix.endswith('\n\n'):
            prefix += '\n'
        new_text = prefix + 'channel_source:\n' + block_text + '\n'
    else:
        if fix_url_index is None:
            new_lines = lines[:channel_source_index + 1] + block_text.splitlines() + lines[channel_source_index + 1:]
        else:
            new_lines = lines[:fix_url_index] + block_text.splitlines() + lines[fix_url_end:]
        new_text = '\n'.join(new_lines)
        if original_text.endswith('\n') or not new_text.endswith('\n'):
            new_text += '\n'

    path.write_text(new_text, encoding='utf-8')
    return {
        'path': str(path),
        'count': len(merged_alive_channels),
    }


def make_channel_payload(channel, req):
    base = get_base_url(req)
    # 웹 Play는 채널마다 원본 코덱이 달라도 브라우저에서 재생하도록
    # H.264/AAC 호환 HLS를 사용합니다. 외부 M3U의 MPEG-TS 경로는 유지됩니다.
    web_codec_mode = 'browser_reencode' if channel.get('web_force_transcode') else 'browser'
    web_player_url = with_request_apikey(f'/{package_name}/api/channel/{channel["id"]}?type=repack&codec_mode={web_codec_mode}', req)
    hls_url = with_request_apikey(f'{base}/api/channel/{channel["id"]}?type=repack', req)
    mpegts_url = with_request_apikey(f'{base}/api/channel/{channel["id"]}?type=mpegts', req)
    player_url = with_request_apikey(f'{base}/player/{channel["id"]}', req)
    if get_operation_mode() != 'shared' and has_supported_url_type_param(channel.get('url')):
        hls_url = replace_url_type(channel['url'], 'repack')
        mpegts_url = replace_url_type(channel['url'], 'mpegts')
        web_player_url = hls_url
        player_url = channel['url']
    return {
        'id': channel['id'],
        'name': channel['name'],
        'url': channel['url'],
        'type': channel['type'],
        'epg_source': channel.get('epg_source', 'AUTO'),
        'epg_enabled': channel.get('epg_enabled', True),
        'hidden': channel['hidden'],
        'web_force_transcode': channel.get('web_force_transcode', False),
        'rtp': channel['rtp'],
        'source': channel['source'],
        'mpegts_url': mpegts_url,
        'hls_url': hls_url,
        'web_player_url': web_player_url,
        'player_url': player_url,
    }


def build_playlist(req, playlist_type='mpegts', include_all=True):
    lines = ['#EXTM3U']
    for channel in get_channels():
        if channel.get('hidden') and not include_all:
            continue
        payload = make_channel_payload(channel, req)
        url = payload['hls_url'] if playlist_type in ('raw', 'repack', 'hls') else payload['mpegts_url']
        lines.append(f'#EXTINF:-1 tvg-name="{channel["name"]}",{channel["name"]}')
        lines.append(url)
    return '\n'.join(lines) + '\n'


def _escape_tvh_attr(value):
    return str(value or '').replace('&', '&amp;').replace('"', '&quot;')


def _escape_tvh_pipe_value(value):
    return str(value or '').replace('\\', '\\\\').replace('"', '\\"')


def build_playlist_tvh(req, include_all=True):
    lines = ['#EXTM3U']
    user_agent = ModelSetting.get('user_agent') or 'Mozilla/5.0'
    for idx, channel in enumerate(get_channels(), start=1):
        if channel.get('hidden') and not include_all:
            continue
        payload = make_channel_payload(channel, req)
        display_name = str(channel.get('name') or '').strip()
        if not display_name:
            continue
        url = payload['mpegts_url']
        lines.append(
            '#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}" tvg-logo="" '
            'group-title="I-Proxy2" tvg-chno="{chno}" tvh-chnum="{chno}",{display}'.format(
                tvg_id=_escape_tvh_attr(display_name),
                tvg_name=_escape_tvh_attr(display_name),
                chno=idx,
                display=display_name,
            )
        )
        lines.append(
            'pipe://ffmpeg -user_agent "{ua}" -loglevel quiet -i "{url}" '
            '-c copy -metadata service_provider=ff_iproxy2 -metadata service_name="{name}" '
            '-c:v copy -c:a aac -b:a 128k -f mpegts -tune zerolatency pipe:1'.format(
                ua=_escape_tvh_pipe_value(user_agent),
                url=_escape_tvh_pipe_value(url),
                name=_escape_tvh_pipe_value(display_name),
            )
        )
    return '\n'.join(lines) + '\n'


def normalize_show_yaml_type(value):
    mode = str(value or SHOW_YAML_DEFAULT_TYPE).strip().lower()
    return mode if mode in ('raw', 'repack', 'mpegts') else SHOW_YAML_DEFAULT_TYPE


def get_show_yaml_path():
    return Path(__file__).resolve().parent / 'show.yaml'


def get_show_yaml_poster_url(req=None):
    base = get_base_url(req or request)
    return with_request_apikey(f'{base}/api/poster', req)


def build_show_yaml_payload(req, play_type=SHOW_YAML_DEFAULT_TYPE):
    show_type = normalize_show_yaml_type(play_type)
    extras = []
    include_hidden = ModelSetting.get_bool('include_hidden_channels')
    current_programs = get_current_program_map()
    for channel in get_channels():
        if channel.get('hidden') and not include_hidden:
            continue
        payload = make_channel_payload(channel, req)
        channel_name = str(channel.get('name') or channel.get('id') or '').strip()
        title = str(current_programs.get(channel.get('id')) or 'LIVE').strip() or 'LIVE'
        extras.append({
            'mode': 'm3u8' if show_type in ('raw', 'repack', 'hls') else 'mpegts',
            'type': 'featurette',
            'param': payload['hls_url'] if show_type in ('raw', 'repack') else payload['mpegts_url'],
            'channel': channel_name,
            'title': title,
            'thumb': get_show_yaml_poster_url(req),
        })
    return {
        'primary': True,
        'code': 'iproxy',
        'title': 'I-Proxy2',
        'posters': get_show_yaml_poster_url(req),
        'summary': f'I-Proxy2 channels\n{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        'extras': extras,
    }


def dump_yaml_payload(payload):
    if yaml is not None:
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def write_show_yaml_file(req=None, play_type=SHOW_YAML_DEFAULT_TYPE):
    req = req or request
    show_type = normalize_show_yaml_type(play_type)
    payload = build_show_yaml_payload(req, show_type)
    body = dump_yaml_payload(payload)
    path = get_show_yaml_path()
    path.write_text(body, encoding='utf-8')
    logger.info('[I-Proxy2] show.yaml file written type=%s path=%s', show_type, path)
    return {'path': str(path), 'type': show_type, 'count': len(payload.get('extras') or [])}


def refresh_show_yaml_cache(req=None, play_type=SHOW_YAML_DEFAULT_TYPE):
    now_ts = time.time()
    req = req or request
    show_type = normalize_show_yaml_type(play_type)
    cache_key = f'{show_type}|{get_base_url(req)}|{(req.args.get("apikey") or "").strip()}'
    body = dump_yaml_payload(build_show_yaml_payload(req, show_type))
    with _show_yaml_cache_lock:
        _show_yaml_cache[cache_key] = {'body': body, 'fetched_at': now_ts}
    logger.info('[I-Proxy2] show.yaml cache refreshed type=%s', show_type)
    return {'ret': 'success', 'updated_at': datetime.now().isoformat()}


def api_show_yaml_response(req, play_type=SHOW_YAML_DEFAULT_TYPE):
    now_ts = time.time()
    show_type = normalize_show_yaml_type(play_type)
    cache_key = f'{show_type}|{get_base_url(req)}|{(req.args.get("apikey") or "").strip()}'
    body = ''
    with _show_yaml_cache_lock:
        cached = dict(_show_yaml_cache.get(cache_key) or {})
        cached_body = str(cached.get('body') or '')
        cached_at = float(cached.get('fetched_at') or 0.0)
        if cached_body and now_ts - cached_at < _SHOW_YAML_CACHE_TTL:
            body = cached_body
    if not body:
        try:
            refresh_show_yaml_cache(req, show_type)
        except Exception:
            logger.exception('[I-Proxy2] show.yaml refresh failed')
        with _show_yaml_cache_lock:
            body = str((_show_yaml_cache.get(cache_key) or {}).get('body') or '')
    return Response(
        body,
        content_type='application/x-yaml; charset=utf-8',
        headers={'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0'},
    )


def render_markdown_text(text):
    try:
        import markdown
        return markdown.markdown(text, extensions=['extra', 'tables', 'fenced_code'])
    except Exception:
        pass

    def inline(value):
        escaped = html.escape(value)
        return re.sub(r'`([^`]+)`', r'<code>\1</code>', escaped)

    lines = text.splitlines()
    html_lines = []
    in_list = False
    in_code = False
    code_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('```'):
            if in_code:
                html_lines.append('<pre><code>{}</code></pre>'.format(html.escape('\n'.join(code_lines))))
                code_lines = []
                in_code = False
            else:
                if in_list:
                    html_lines.append('</ul>')
                    in_list = False
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            continue
        if stripped.startswith('#'):
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            level = min(6, len(stripped) - len(stripped.lstrip('#')))
            title = stripped[level:].strip()
            html_lines.append(f'<h{level}>{inline(title)}</h{level}>')
            continue
        if stripped.startswith('- '):
            if not in_list:
                html_lines.append('<ul>')
                in_list = True
            html_lines.append(f'<li>{inline(stripped[2:].strip())}</li>')
            continue
        if in_list:
            html_lines.append('</ul>')
            in_list = False
        html_lines.append(f'<p>{inline(stripped)}</p>')
    if in_code:
        html_lines.append('<pre><code>{}</code></pre>'.format(html.escape('\n'.join(code_lines))))
    if in_list:
        html_lines.append('</ul>')
    return '\n'.join(html_lines)


def get_manual_body():
    path = Path(__file__).resolve().parent / 'manual.md'
    if not path.exists():
        abort(404)
    text = path.read_text(encoding='utf-8')
    return render_markdown_text(text)


@blueprint.route('/manual', methods=['GET'])
@blueprint.route('/manual/', methods=['GET'])
@blueprint.route('/manual/manual.md', methods=['GET'])
def page_manual():
    return render_template(f'{package_name}_manual.html', arg={'manual_body': get_manual_body()})




class Logic(PluginModuleBase):
    db_default = {
        'ffmpeg_path': 'ffmpeg',
        'operation_mode': 'personal',
        'idle_timeout': '30',
        'hls_time': '1',
        'hls_list_size': '6',
        'hls_codec_mode': 'original',
        'hls_target_resolution': 'original',
        'hls_preset': 'normal',
        'hls_h264_encoder': 'libx264',
        'hls_vaapi_device': '',
        'hls_vaapi_rate_control': 'auto',
        'alive_yaml_mode': 'mpegts',
        'include_hidden_channels': 'True',
        'manual_channels': '[]',
        'manual_channels_source_path': get_default_manual_channels_source_path(),
        'line_channels': '',
        'user_agent': 'Mozilla/5.0',
        'epg_auto_start': 'False',
        'epg_schedule': '360',
        'epg_days': '2',
        'epg_user_agent': EPG_HTTP_HEADERS['User-Agent'],
        'epg_referer': '',
        'epg_last_generated': '',
    }

    def __init__(self, PM):
        super().__init__(PM, None)
        self.name = 'setting'
        ensure_cleanup_thread()
        self.sync_epg_scheduler()

    def epg_scheduler_job_id(self):
        return f'{package_name}_epg_generate'

    def sync_epg_scheduler(self, enabled_override=None, schedule_override=None):
        return sync_epg_scheduler_job(enabled_override, schedule_override)

    def scheduler_function(self):
        try:
            generate_epg_xml()
        except Exception as exception:
            append_epg_log(f'Scheduler failed: {str(exception)}')
            logger.error(traceback.format_exc())

    def process_menu(self, sub, req):
        arg = ModelSetting.to_dict()
        base = get_base_url(req)
        arg['package_name'] = package_name
        arg['mpegts_playlist_url'] = with_apikey(f'{base}/api/m3u?type=mpegts')
        arg['hls_playlist_url'] = with_apikey(f'{base}/api/m3u?type=repack')
        arg['tvh_playlist_url'] = with_apikey(f'{base}/api/m3u?type=tvh')
        arg['show_yaml_url'] = with_apikey(f'{base}/show.yaml?type={SHOW_YAML_DEFAULT_TYPE}')
        arg['show_yaml_mpegts_url'] = with_apikey(f'{base}/show.yaml?type=mpegts')
        arg['epg_xml_url'] = with_apikey(f'{base}/api/epg.xml')
        arg['channel_count'] = len(get_channels())
        arg['epg_source_options'] = EPG_SOURCE_OPTIONS
        arg['epg_schedule_active'] = str(F.scheduler.is_include(self.epg_scheduler_job_id()))
        arg['epg_log_tail'] = read_epg_log_tail()
        arg['epg_last_generated'] = ModelSetting.get('epg_last_generated')
        arg['default_channels_json'] = '[]'
        arg['bundled_channels_raw'] = get_bundled_channels_raw()
        arg['manual_channels_source_path_default'] = get_default_manual_channels_source_path()
        arg['manual_channels_source_path'] = get_manual_channels_source_path()
        arg['system_ddns'] = (SystemModelSetting.get('ddns') or '').strip()
        try:
            arg['manual_channels'] = get_manual_channels_raw()
            arg['manual_channels_json'] = json.dumps(parse_manual_channels(arg['manual_channels']), ensure_ascii=False)
        except Exception:
            arg['manual_channels'] = '[]'
            arg['manual_channels_json'] = '[]'
        if sub == 'list':
            current_programs = get_current_program_map()
            arg['channels'] = []
            for item in get_channels():
                payload = make_channel_payload(item, req)
                payload['current_program'] = current_programs.get(item['id'], '')
                arg['channels'].append(payload)
        if sub == 'manual':
            arg['manual_body'] = get_manual_body()
        return render_template(f'{package_name}_{sub}.html', arg=arg)

    def process_command(self, command, arg1, arg2, arg3, req):
        _ = (arg3, req)
        try:
            if command == 'ffmpeg_version':
                return jsonify(self._ffmpeg_version())
            if command == 'transcode_capabilities':
                return jsonify(self._transcode_capabilities())
            if command == 'test_transcode_encoder':
                return jsonify(self._test_transcode_encoder())
            if command == 'sync_alive_yaml':
                result = update_alive_fix_url(req)
                return jsonify({'ret': 'success', 'msg': f'alive.yaml에 {result["count"]}개 채널을 반영했습니다.'})
            if command == 'generate_epg':
                output = generate_epg_xml()
                return jsonify({'ret': 'success', 'msg': 'EPG generated', 'title': 'EPG 생성 완료', 'modal': str(output)})
            if command == 'sync_epg_schedule':
                return jsonify(self.sync_epg_scheduler(arg1, arg2))
            return jsonify({'ret': 'warning', 'msg': f'Unknown command: {command}'})
        except Exception as e:
            logger.error('Exception:%s', e)
            logger.error(traceback.format_exc())
            return jsonify({'ret': 'danger', 'msg': str(e)})

    def process_ajax(self, sub, req):
        try:
            if sub == 'ffmpeg_version':
                return jsonify(self._ffmpeg_version())
            if sub == 'transcode_capabilities':
                return jsonify(self._transcode_capabilities())
            if sub == 'test_transcode_encoder':
                return jsonify(self._test_transcode_encoder())
            if sub == 'save_settings':
                return jsonify(self._save_settings(req))
            if sub == 'import_manual_channels':
                source_value = req.form.get('source_path', req.form.get('raw', ''))
                imported = import_channels_from_source(source_value)
                return jsonify({
                    'ret': 'success',
                    'msg': f'{len(imported["channels"])}개 채널을 가져왔습니다.',
                    'format': imported['format'],
                    'source': imported.get('source', ''),
                    'channels': imported['channels'],
                })
            if sub == 'channel_list':
                return jsonify({'list': [make_channel_payload(item, req) for item in get_channels()]})
            if sub == 'player_event':
                mode = (req.form.get('mode') or '').strip()
                channel_name = (req.form.get('name') or '').strip()
                channel_url = (req.form.get('url') or '').strip()
                action = (req.form.get('action') or 'click').strip()
                logger.info('[PLAYER] action=%s mode=%s channel=%s url=%s', action, mode, channel_name, channel_url)
                return jsonify({'ret': 'success'})
            if sub == 'diagnose_manual_channel':
                channel_id = req.form.get('id', '').strip()
                channel_url = req.form.get('url', '').strip()
                if not channel_url:
                    return jsonify({'ret': 'warning', 'msg': '채널을 찾을 수 없습니다.'})
                result = probe_channel_type(channel_url)
                detected_type = ((result.get('data') or {}).get('detected_type') or '').strip()
                if channel_id and detected_type:
                    update_manual_channel_type(channel_id, detected_type, channel_url)
                return jsonify(result)
            if sub == 'epg_status':
                return jsonify({
                    'ret': 'success',
                    'data': {
                        'last_generated': ModelSetting.get('epg_last_generated'),
                        'log_tail': read_epg_log_tail(),
                        'schedule_active': F.scheduler.is_include(self.epg_scheduler_job_id()),
                    },
                })
            return jsonify({'ret': 'warning', 'msg': f'Unknown ajax: {sub}'})
        except Exception as e:
            logger.error('Exception:%s', e)
            logger.error(traceback.format_exc())
            return jsonify({'ret': 'danger', 'msg': str(e)})

    def _ffmpeg_version(self):
        ffmpeg_path = ModelSetting.get('ffmpeg_path')
        if not ffmpeg_path:
            return {'ret': 'danger', 'msg': 'ffmpeg_path is empty'}
        proc = subprocess.run([ffmpeg_path, '-version'], capture_output=True, text=True, timeout=10, check=False)
        if proc.returncode != 0:
            return {'ret': 'danger', 'msg': proc.stderr.strip() or 'ffmpeg check failed'}
        first_lines = '\n'.join(proc.stdout.strip().splitlines()[:12])
        return {'ret': 'success', 'title': 'ffmpeg version', 'modal': first_lines}

    def _transcode_capabilities(self):
        ffmpeg_path = ModelSetting.get('ffmpeg_path')
        if not ffmpeg_path:
            return {'ret': 'danger', 'msg': 'ffmpeg_path is empty'}
        encoders = subprocess.run([ffmpeg_path, '-hide_banner', '-encoders'], capture_output=True, text=True, timeout=20, check=False)
        hwaccels = subprocess.run([ffmpeg_path, '-hide_banner', '-hwaccels'], capture_output=True, text=True, timeout=20, check=False)
        if encoders.returncode != 0:
            return {'ret': 'danger', 'msg': encoders.stderr.strip() or 'encoder check failed'}

        encoder_text = encoders.stdout
        hwaccel_text = hwaccels.stdout if hwaccels.returncode == 0 else (hwaccels.stderr or '')
        known = [
            ('libx264', 'H.264 CPU'),
            ('libx265', 'H.265 CPU'),
            ('h264_nvenc', 'H.264 NVIDIA NVENC'),
            ('hevc_nvenc', 'H.265 NVIDIA NVENC'),
            ('h264_qsv', 'H.264 Intel QSV'),
            ('hevc_qsv', 'H.265 Intel QSV'),
            ('h264_vaapi', 'H.264 VAAPI'),
            ('hevc_vaapi', 'H.265 VAAPI'),
        ]
        lines = ['[Detected encoders]']
        found = False
        for token, label in known:
            if token in encoder_text:
                lines.append(f'- {label}: available')
                found = True
        if not found:
            lines.append('- No known video encoder detected')
        lines.extend(['', '[hwaccels]', hwaccel_text.strip() or 'No hwaccels output', '', '[matching encoder lines]'])
        excerpt = [line for line in encoder_text.splitlines() if any(token in line for token, _ in known)]
        lines.append('\n'.join(excerpt) if excerpt else 'No matching encoder lines')
        return {'ret': 'success', 'title': 'Available encoders', 'modal': '\n'.join(lines)}

    def _test_transcode_encoder(self):
        ffmpeg_path = ModelSetting.get('ffmpeg_path')
        if not ffmpeg_path:
            return {'ret': 'danger', 'msg': 'ffmpeg_path is empty'}

        tested = []
        selected = None
        selected_device = ''
        for encoder in candidate_h264_encoders():
            ok, device = encoder_is_usable(ffmpeg_path, encoder, force=True, with_device=True)
            suffix = f' ({device})' if device else ''
            tested.append(f'- {encoder}: {"OK" if ok else "FAIL"}{suffix}')
            if ok and selected is None:
                selected = encoder
                selected_device = device or ''

        if selected is None:
            return {
                'ret': 'danger',
                'title': 'Encoder test',
                'modal': '\n'.join(['[codec]', 'h264', '', '[results]', *tested]),
                'msg': '사용 가능한 h264 인코더를 찾지 못했습니다',
            }

        ModelSetting.set('hls_h264_encoder', selected)
        ModelSetting.set('hls_vaapi_device', selected_device)
        return {
            'ret': 'success',
            'title': 'Encoder test',
            'modal': '\n'.join(['[codec]', 'h264', '', '[saved]', selected, selected_device or '', '', '[results]', *tested]),
            'data': {'selected_encoder': selected, 'selected_device': selected_device},
            'msg': '테스트 결과가 저장되었습니다',
        }

    def _save_settings(self, req):
        keys = [
            'ffmpeg_path',
            'operation_mode',
            'idle_timeout',
            'hls_time',
            'hls_list_size',
            'hls_codec_mode',
            'hls_target_resolution',
            'hls_preset',
            'hls_vaapi_rate_control',
            'user_agent',
            'alive_yaml_mode',
            'include_hidden_channels',
            'manual_channels',
            'manual_channels_source_path',
        ]
        for key in keys:
            if key not in req.form:
                continue
            value = req.form.get(key, '')
            if key == 'include_hidden_channels':
                value = 'True' if parse_bool(value) else 'False'
            ModelSetting.set(key, value)
        try:
            alive_result = update_alive_fix_url(req)
            return {
                'ret': 'success',
                'msg': f'설정을 저장했습니다. alive.yaml에 {alive_result["count"]}개 채널을 반영했습니다.',
            }
        except Exception as exception:
            logger.exception('Failed to update alive.yaml')
            return {
                'ret': 'warning',
                'msg': f'설정을 저장했지만 alive.yaml 갱신은 실패했습니다: {str(exception)}',
            }


@blueprint.route('/api/m3u', methods=['GET'])
def api_playlist():
    require_api_key(request)
    playlist_type = (request.args.get('type') or 'mpegts').strip().lower()
    if playlist_type not in ('raw', 'repack', 'hls', 'mpegts', 'tvh'):
        abort(400)
    include_param = request.args.get('include')
    if include_param is None:
        include_all = ModelSetting.get_bool('include_hidden_channels')
    else:
        include_all = include_param == 'all'
    if playlist_type == 'tvh':
        return Response(build_playlist_tvh(request, include_all=include_all), content_type='audio/mpegurl')
    return Response(build_playlist(request, playlist_type=playlist_type, include_all=include_all), content_type='audio/mpegurl')


@blueprint.route('/api/channels', methods=['GET'])
def api_channels():
    require_api_key(request)
    include_param = request.args.get('include')
    if include_param is None:
        include_all = ModelSetting.get_bool('include_hidden_channels')
    else:
        include_all = include_param == 'all'

    program_map = get_current_program_map()
    channels = []
    for channel in get_channels():
        if channel.get('hidden') and not include_all:
            continue
        payload = make_channel_payload(channel, request)
        payload['current_program'] = program_map.get(channel['id'], '')
        channels.append(payload)

    return jsonify({'updated_at': int(time.time()), 'channels': channels})


@blueprint.route('/api/epg.xml', methods=['GET'])
def api_epg():
    require_api_key(request)
    path = get_epg_output_path()
    if not path.exists():
        try:
            generate_epg_xml()
        except Exception:
            logger.error(traceback.format_exc())
            abort(502)
    if not path.exists():
        abort(404)
    return Response(path.read_text(encoding='utf-8', errors='replace'), content_type='application/xml; charset=utf-8')


@blueprint.route('/api/poster', methods=['GET'])
def api_poster():
    data = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">'
        '<rect width="640" height="360" fill="#111827"/>'
        '<rect x="36" y="36" width="568" height="288" rx="24" fill="#1f2937"/>'
        '<text x="320" y="170" text-anchor="middle" font-family="Arial, sans-serif" '
        'font-size="54" font-weight="700" fill="#f9fafb">I-Proxy2</text>'
        '<text x="320" y="220" text-anchor="middle" font-family="Arial, sans-serif" '
        'font-size="24" fill="#93c5fd">Live Channels</text>'
        '</svg>'
    )
    return Response(data, content_type='image/svg+xml; charset=utf-8')


@blueprint.route('/show.yaml', methods=['GET'])
def api_show_yaml():
    require_api_key(request)
    return api_show_yaml_response(request, request.args.get('type') or request.args.get('mode') or SHOW_YAML_DEFAULT_TYPE)


@blueprint.route('/player/<channel_id>', methods=['GET'])
def page_player(channel_id):
    require_api_key(request)
    channels = get_channel_map()
    channel = channels.get(channel_id)
    if channel is None:
        abort(404)
    payload = make_channel_payload(channel, request)
    payload['current_program'] = get_current_program_map().get(channel_id, '')
    logger.info('[PLAYER] popup_open channel=%s type=%s remote=%s ua=%s', channel.get('name'), channel.get('type'), request.remote_addr, request.headers.get('User-Agent', ''))
    return render_template(f'{package_name}_player.html', arg={
        'package_name': package_name,
        'channel': payload,
        'play_url': payload['web_player_url'],
    })


@blueprint.route('/ajax/epg_generate', methods=['POST'])
def ajax_epg_generate():
    try:
        output = generate_epg_xml()
        return jsonify({'ret': 'success', 'msg': 'EPG generated', 'path': str(output)})
    except Exception as exception:
        append_epg_log(f'EPG generate failed: {str(exception)}')
        logger.error(traceback.format_exc())
        return jsonify({'ret': 'danger', 'msg': str(exception)})


@blueprint.route('/ajax/epg_schedule', methods=['POST'])
def ajax_epg_schedule():
    try:
        enabled = request.form.get('enabled', '')
        schedule = request.form.get('schedule', '')
        return jsonify(sync_epg_scheduler_job(enabled, schedule))
    except Exception as exception:
        logger.error(traceback.format_exc())
        return jsonify({'ret': 'danger', 'msg': str(exception)})


@blueprint.route('/ajax/ffmpeg_version', methods=['POST'])
def ajax_ffmpeg_version():
    try:
        return jsonify(P.logic.get_module('setting')._ffmpeg_version())
    except Exception as exception:
        logger.error(traceback.format_exc())
        return jsonify({'ret': 'danger', 'msg': str(exception)})


@blueprint.route('/ajax/transcode_capabilities', methods=['POST'])
def ajax_transcode_capabilities():
    try:
        return jsonify(P.logic.get_module('setting')._transcode_capabilities())
    except Exception as exception:
        logger.error(traceback.format_exc())
        return jsonify({'ret': 'danger', 'msg': str(exception)})


def build_mpegts_channel_response(channel_id, channel):
    if get_operation_mode() != 'shared':
        if has_supported_url_type_param(channel.get('url')):
            return redirect(replace_url_type(channel['url'], 'mpegts'), code=302)
        response = Response(stream_via_ffmpeg_copy(channel), mimetype='video/mp2t')
        if str(channel.get('url') or '').startswith(('http://', 'https://')):
            response.headers['Cache-Control'] = 'no-cache, no-store'
            response.headers['X-Accel-Buffering'] = 'no'
        return response

    hls_key = start_hls(channel_id, get_hls_default_options())
    if not hls_key:
        abort(400)
    if not wait_for_hls_ready(hls_key):
        abort(404)

    touch_hls(hls_key)
    return Response(stream_via_ffmpeg_from_hls(hls_key), mimetype='video/mp2t')


def build_hls_channel_response(channel_id, channel):
    logger.debug('[PLAYER] hls_request channel=%s type=%s remote=%s ua=%s', channel.get('name'), channel.get('type'), request.remote_addr, request.headers.get('User-Agent', ''))
    request_options = get_hls_request_options(request)

    if get_operation_mode() != 'shared' and has_supported_url_type_param(channel.get('url')):
        return redirect(replace_url_type(channel['url'], 'repack'), code=302)

    hls_key = start_hls(channel_id, request_options)
    if not hls_key:
        abort(400)
    if not wait_for_hls_ready(hls_key, min_segments=1):
        abort(404)

    playlist = get_hls_playlist_path(hls_key)
    content = playlist.read_text(encoding='utf-8')
    base = get_base_url(request)
    lines = []
    for line in content.splitlines():
        if line.endswith('.ts'):
            lines.append(with_apikey(f'{base}/api/ts/{hls_key}/{os.path.basename(line)}'))
        else:
            lines.append(line)

    touch_hls(hls_key)
    return Response('\n'.join(lines) + '\n', content_type='application/vnd.apple.mpegurl')


@blueprint.route('/api/channel/<channel_id>', methods=['GET'])
def api_channel(channel_id):
    require_api_key(request)
    channels = get_channel_map()
    channel = channels.get(channel_id)
    if channel is None:
        abort(404)
    channel_type = (request.args.get('type') or 'mpegts').strip().lower()
    if channel_type in ('raw', 'repack', 'hls'):
        return build_hls_channel_response(channel_id, channel)
    if channel_type == 'mpegts':
        return build_mpegts_channel_response(channel_id, channel)
    abort(400)


@blueprint.route('/api/seg/<path:encoded_url>', methods=['GET'])
def api_seg(encoded_url):
    require_api_key(request)
    segment_url = urllib.parse.unquote(encoded_url)
    data = fetch_url(segment_url)
    if not data:
        abort(502)
    return Response(data, mimetype='video/MP2T')


@blueprint.route('/api/ts/<path:filename>', methods=['GET'])
def api_ts(filename):
    require_api_key(request)
    hls_key = get_hls_key_from_segment_filename(filename)
    touch_hls(hls_key)
    safe_name = os.path.basename(filename)
    path = get_hls_session_dir(hls_key) / safe_name
    if not path.exists():
        abort(404)
    return Response(path.read_bytes(), mimetype='video/mp2t')
