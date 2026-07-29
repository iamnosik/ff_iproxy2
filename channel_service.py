import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from plugin import F

try:
    import yaml
except Exception:
    yaml = None

from .setup import P

logger = P.logger
ModelSetting = P.ModelSetting
SystemModelSetting = F.SystemModelSetting
REMOTE_MODE_TYPES = ('direct', 'raw', 'repack', 'mpegts')

DEFAULT_CHANNELS = [
    {'id': 'ch0', 'name': 'YTN', 'url': 'rtp://239.192.47.7:49220'},
    {'id': 'ch2', 'name': 'CJ ONSTYLE+', 'url': 'rtp://239.192.64.7:49220'},
    {'id': 'ch4', 'name': '롯데홈쇼핑', 'url': 'rtp://239.192.56.5:49220'},
    {'id': 'ch5', 'name': 'SBS', 'url': 'udp://239.192.67.98:49220'},
    {'id': 'ch6', 'name': 'CJ ONSTYLE', 'url': 'rtp://239.192.58.11:49220'},
    {'id': 'ch7', 'name': 'KBS2', 'url': 'udp://239.192.67.99:49220'},
    {'id': 'ch8', 'name': '현대홈쇼핑', 'url': 'rtp://239.192.48.4:49220'},
    {'id': 'ch9', 'name': 'KBS1', 'url': 'udp://239.192.67.100:49220'},
    {'id': 'ch10', 'name': 'GS SHOP', 'url': 'rtp://239.192.49.4:49220'},
    {'id': 'ch11', 'name': 'MBC', 'url': 'udp://239.192.67.101:49220'},
    {'id': 'ch12', 'name': '홈&쇼핑', 'url': 'rtp://239.192.81.137:49220'},
    {'id': 'ch14', 'name': 'NS홈쇼핑', 'url': 'rtp://239.192.52.10:49220'},
    {'id': 'ch17', 'name': 'SK stoa', 'url': 'rtp://239.192.64.12:49220'},
    {'id': 'ch19', 'name': '신세계쇼핑', 'url': 'rtp://239.192.64.6:49220'},
    {'id': 'ch21', 'name': '공영쇼핑', 'url': 'rtp://239.192.81.169:49220'},
    {'id': 'ch22', 'name': 'KT알파 쇼핑', 'url': 'rtp://239.192.66.17:49220'},
    {'id': 'ch29', 'name': '쇼핑엔티', 'url': 'rtp://239.192.69.109:49220'},
    {'id': 'ch35', 'name': 'NS Shop+', 'url': 'rtp://239.192.81.143:49220'},
    {'id': 'ch37', 'name': 'W쇼핑', 'url': 'rtp://239.192.81.101:49220'},
    {'id': 'ch72', 'name': 'OCN Movies', 'url': 'udp://239.192.67.227:49220'},
    {'id': 'ch73', 'name': 'OCN', 'url': 'udp://239.192.67.226:49220'},
    {'id': 'ch74', 'name': 'OCN Movies2', 'url': 'udp://239.192.67.228:49220'},
    {'id': 'ch75', 'name': 'Screen', 'url': 'udp://239.192.67.229:49220'},
    {'id': 'ch95', 'name': 'EBS2', 'url': 'rtp://239.192.42.2:49220'},
    {'id': 'ch120', 'name': 'UXN', 'url': 'rtp://239.192.81.38:49220'},
    {'id': 'ch121', 'name': 'UHD Dream TV', 'url': 'rtp://239.192.81.37:49220'},
    {'id': 'ch122', 'name': 'Asia UHD', 'url': 'rtp://239.192.81.36:49220'},
    {'id': 'ch312', 'name': '미드나잇', 'url': 'http://1.214.67.100/vod/62801.m3u8?VOD_RequestID=v2M2-0101-1010-7272-5050-000020180717021633', 'hidden': True},
    {'id': 'ch314', 'name': '허니TV', 'url': 'http://1.214.67.100/vod/74301.m3u8?VOD_RequestID=v2M2-0101-1010-7272-5050-000020180717021633', 'hidden': True},
    {'id': 'ch316', 'name': '플레이보이TV', 'url': 'http://1.214.67.100/vod/62901.m3u8?VOD_RequestID=v2M2-0101-1010-7272-5050-000020180717021633', 'hidden': True},
    {'id': 'ch974', 'name': 'SPOTV Golf&Health', 'url': 'udp://239.192.67.163:49220'},
    {'id': 'ch976', 'name': 'JTBC GOLF', 'url': 'udp://239.192.67.165:49220'},
    {'id': 'ch977', 'name': 'SBS GOLF', 'url': 'udp://239.192.67.164:49220'},
    {'id': 'ch982', 'name': 'SPOTV2', 'url': 'udp://239.192.67.162:49220'},
    {'id': 'ch983', 'name': 'MBC Sports+', 'url': 'udp://239.192.67.131:49220'},
    {'id': 'ch984', 'name': 'SBS Sports', 'url': 'udp://239.192.67.130:49220'},
    {'id': 'ch986', 'name': 'SPOTV', 'url': 'udp://239.192.67.133:49220'},
    {'id': 'ch999', 'name': 'GS MY SHOP', 'url': 'rtp://239.192.66.4:49220'},
]

DEFAULT_CHANNELS = []


def get_bundled_channels_path():
    return Path(__file__).resolve().parent / 'resources' / 'channels.json'


def get_bundled_channels_raw():
    path = get_bundled_channels_path()
    if not path.exists():
        return ''
    try:
        return path.read_text(encoding='utf-8')
    except Exception:
        logger.exception('Failed to load bundled channels.json')
        return ''


def get_default_manual_channels_source_path():
    return str(get_bundled_channels_path())


def get_manual_channels_source_path():
    value = (ModelSetting.get('manual_channels_source_path') or '').strip()
    return value or get_default_manual_channels_source_path()


if yaml is not None:
    class YamlIncludeLoader(yaml.SafeLoader):
        def __init__(self, stream):
            stream_name = getattr(stream, 'name', '')
            self._root = Path(stream_name).resolve().parent if stream_name else Path.cwd()
            super().__init__(stream)


    def _yaml_include(loader, node):
        filename = loader._root / loader.construct_scalar(node)
        with open(filename, 'r', encoding='utf-8') as handle:
            return yaml.load(handle, Loader=YamlIncludeLoader)


    YamlIncludeLoader.add_constructor('!include', _yaml_include)
else:
    YamlIncludeLoader = None

def normalize_scheme_type(url, explicit_type=None):
    if explicit_type:
        explicit = explicit_type.lower()
        if explicit in ('rtp', 'udp', 'http', 'https', 'raw', 'repack', 'mpegts', 'http_hls', 'http_stream'):
            if explicit == 'rtp':
                return 'rtp'
            if explicit in ('raw', 'repack'):
                return 'http_hls'
            if explicit == 'mpegts':
                return 'http_stream'
            if explicit in ('http', 'https'):
                return 'http_hls'
            if explicit in ('http_hls', 'http_stream'):
                return explicit
            return 'udp'
        return explicit
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == 'rtp':
        return 'rtp'
    if parsed.scheme == 'udp':
        return 'udp'
    if parsed.scheme in ('http', 'https'):
        return 'http_hls'
    return 'udp'


def detect_iproxy_api_type(url):
    parsed = urllib.parse.urlparse(url or '')
    if parsed.scheme not in ('http', 'https'):
        return ''
    api_type = get_url_type_param(url)
    if api_type in ('direct', 'raw', 'repack'):
        return 'http_hls'
    if api_type == 'mpegts':
        return 'http_stream'
    return ''


def get_url_type_param(url):
    parsed = urllib.parse.urlparse(url or '')
    if parsed.scheme not in ('http', 'https'):
        return ''
    query = urllib.parse.parse_qs(parsed.query or '', keep_blank_values=True)
    return ((query.get('type') or [''])[0] or '').strip().lower()


def has_supported_url_type_param(url):
    return get_url_type_param(url) in REMOTE_MODE_TYPES


def replace_url_type(url, play_type):
    url = str(url or '').strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return url
    query = urllib.parse.parse_qs(parsed.query or '', keep_blank_values=True)
    if 'type' not in query:
        return url
    query['type'] = [str(play_type or '').strip().lower()]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment,
    ))


def is_forward_channel_url(url):
    text = str(url or '').strip()
    normalized = normalize_url_to_localhost_if_ddns(text)
    if normalized != text:
        return True
    parsed = urllib.parse.urlparse(text)
    host = (parsed.hostname or '').strip().lower()
    if host not in ('localhost', '127.0.0.1'):
        return False
    return bool(detect_iproxy_api_type(url))


def apply_known_channel_types(items):
    result = []
    for item in items or []:
        normalized = dict(item)
        detected_type = detect_iproxy_api_type(normalized.get('url'))
        if detected_type:
            normalized['type'] = detected_type
            normalized['type_diagnosed'] = True
            normalized['rtp'] = False
        result.append(normalized)
    return result


def normalize_epg_source(value):
    return 'AUTO'


def make_channel_id(index):
    return f'ch{index:03d}'


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'y', 'yes', 'on')


def parse_line_channels(raw):
    channels = []
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith('#'):
            continue
        if '|' in text:
            parts = [x.strip() for x in text.split('|')]
        elif '\t' in text:
            parts = [x.strip() for x in text.split('\t')]
        else:
            parts = [x.strip() for x in text.split(',', 1)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        name, url = parts[0], parts[1]
        channels.append({
            'name': name,
            'url': url,
            'mpegts_url': '',
            'type': '',
            'epg_enabled': True,
            'hidden': False,
            'rtp': False,
            'source': 'line',
        })
    return channels


def parse_manual_channels(raw):
    text = (raw or '').strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError('수동 채널 형식은 배열이어야 합니다.')

    items = []
    for index, value in enumerate(data, start=1):
        if not isinstance(value, dict):
            continue
        name = (value.get('name') or '').strip()
        url = (value.get('url') or '').strip()
        if not name or not url:
            continue
        explicit_type = (value.get('type') or '').strip().lower()
        type_diagnosed = parse_bool(value.get('type_diagnosed'))
        detected_type = normalize_scheme_type(url, explicit_type) if (explicit_type and type_diagnosed) else ''
        items.append({
            'id_hint': value.get('id') or make_channel_id(index),
            'name': name,
            'url': url,
            'mpegts_url': (value.get('mpegts_url') or '').strip(),
            'type': detected_type,
            'type_diagnosed': type_diagnosed,
            'epg_source': normalize_epg_source(value.get('epg_source')),
            'epg_enabled': parse_bool(value.get('epg_enabled')) if value.get('epg_enabled') is not None else True,
            'hidden': parse_bool(value.get('hidden')),
            'web_force_transcode': parse_bool(value.get('web_force_transcode')),
            'rtp': detected_type == 'rtp',
            'source': 'manual',
        })
    return items


def get_manual_channels_raw():
    raw = (ModelSetting.get('manual_channels') or '').strip()
    if raw in ('', '[]'):
        return '[]'
    return raw


def update_manual_channel_type(channel_id, detected_type, url=None):
    raw = get_manual_channels_raw()
    try:
        data = json.loads(raw)
    except Exception:
        return False
    if not isinstance(data, list):
        return False

    updated = False
    for index, value in enumerate(data, start=1):
        if not isinstance(value, dict):
            continue
        current_id = str(value.get('id') or make_channel_id(index))
        current_url = (value.get('url') or '').strip()
        if current_id != str(channel_id):
            continue
        if url and current_url and current_url != url:
            continue
        value['id'] = current_id
        value['type'] = detected_type or ''
        value['type_diagnosed'] = True if detected_type else False
        value['rtp'] = True if detected_type == 'rtp' else False
        updated = True
        break

    if updated:
        ModelSetting.set('manual_channels', json.dumps(data, ensure_ascii=False))
    return updated


def parse_json_channels(raw):
    text = raw.strip()
    if not text:
        return []
    data = json.loads(text)
    return parse_structured_channels(data, source='json')


def _parse_structured_channels_legacy(data, source='json'):
    items = []
    if isinstance(data, dict):
        iterable = list(data.items())
    elif isinstance(data, list):
        iterable = list(enumerate(data, start=1))
    else:
        raise ValueError('JSON 채널 형식은 객체 또는 배열이어야 합니다.')

    for key, value in iterable:
        if not isinstance(value, dict):
            continue
        url = (value.get('url') or '').strip()
        name = (value.get('name') or str(key)).strip()
        if not url or not name:
            continue
        items.append({
            'id_hint': str(key),
            'name': name,
            'url': url,
            'mpegts_url': (value.get('mpegts_url') or '').strip(),
            'type': '',
            'type_diagnosed': False,
            'epg_enabled': parse_bool(value.get('epg_enabled')) if value.get('epg_enabled') is not None else True,
            'hidden': parse_bool(value.get('hidden')),
            'rtp': False,
            'source': 'json',
        })
    return items


def parse_structured_channels(data, source='json'):
    items = []
    if isinstance(data, dict):
        if len(data) == 1 and isinstance(next(iter(data.values())), (list, dict)):
            data = next(iter(data.values()))
        if isinstance(data, dict):
            iterable = list(data.items())
        else:
            iterable = list(enumerate(data, start=1))
    elif isinstance(data, list):
        iterable = list(enumerate(data, start=1))
    else:
        raise ValueError(f'{str(source).upper()} 채널 형식은 객체 또는 배열이어야 합니다.')

    for key, value in iterable:
        if isinstance(value, str):
            value = {'name': str(key), 'url': value}
        if not isinstance(value, dict):
            continue
        url = (value.get('url') or value.get('source') or value.get('link') or '').strip()
        name = (value.get('name') or value.get('title') or value.get('channel') or str(key)).strip()
        if not url or not name:
            continue
        explicit_type = (value.get('type') or value.get('scheme') or '').strip().lower()
        type_diagnosed = parse_bool(value.get('type_diagnosed')) and bool(explicit_type)
        detected_type = normalize_scheme_type(url, explicit_type) if type_diagnosed else ''
        items.append({
            'id_hint': str(value.get('id') or value.get('id_hint') or key),
            'name': name,
            'url': url,
            'mpegts_url': (value.get('mpegts_url') or '').strip(),
            'type': detected_type,
            'type_diagnosed': type_diagnosed,
            'epg_enabled': parse_bool(value.get('epg_enabled')) if value.get('epg_enabled') is not None else True,
            'hidden': parse_bool(value.get('hidden')),
            'rtp': detected_type == 'rtp',
            'source': source,
        })
    return items


def parse_simple_yaml_scalar(value):
    text = str(value or '').strip()
    if text == '':
        return ''
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    lowered = text.lower()
    if lowered in ('true', 'yes', 'on'):
        return True
    if lowered in ('false', 'no', 'off'):
        return False
    if lowered in ('null', 'none', '~'):
        return None
    if re.fullmatch(r'-?\d+', text):
        try:
            return int(text)
        except Exception:
            return text
    return text


def parse_simple_yaml(raw):
    lines = raw.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        cleaned.append(line.rstrip())
    if not cleaned:
        return []

    first = cleaned[0].lstrip()
    if first.startswith('- '):
        items = []
        current = None
        for line in cleaned:
            stripped = line.lstrip()
            if stripped.startswith('- '):
                if current is not None:
                    items.append(current)
                current = {}
                remainder = stripped[2:].strip()
                if remainder and ':' in remainder:
                    key, value = remainder.split(':', 1)
                    current[key.strip()] = parse_simple_yaml_scalar(value)
                elif remainder:
                    current['url'] = parse_simple_yaml_scalar(remainder)
                continue
            if current is None or ':' not in stripped:
                continue
            key, value = stripped.split(':', 1)
            current[key.strip()] = parse_simple_yaml_scalar(value)
        if current is not None:
            items.append(current)
        return items

    root = {}
    current_key = None
    current_value = None
    for line in cleaned:
        indent = len(line) - len(line.lstrip(' '))
        stripped = line.strip()
        if indent == 0:
            if current_key is not None:
                root[current_key] = current_value
            if ':' not in stripped:
                continue
            key, value = stripped.split(':', 1)
            current_key = key.strip()
            value = value.strip()
            if value == '':
                current_value = {}
            else:
                current_value = parse_simple_yaml_scalar(value)
        elif isinstance(current_value, dict) and ':' in stripped:
            key, value = stripped.split(':', 1)
            current_value[key.strip()] = parse_simple_yaml_scalar(value)
    if current_key is not None:
        root[current_key] = current_value
    return root


def parse_yaml_channels(raw):
    text = (raw or '').strip()
    if not text:
        return []
    data = yaml.safe_load(text) if yaml is not None else parse_simple_yaml(text)
    return parse_structured_channels(data, source='yaml')


def parse_m3u_channels(raw):
    text = (raw or '').strip()
    if not text:
        return []
    items = []
    current = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('#EXTM3U'):
            continue
        if stripped.startswith('#EXTINF'):
            current = {}
            match = re.match(r'#EXTINF:(?P<duration>[^,]*),(?P<name>.*)$', stripped)
            if match:
                current['name'] = match.group('name').strip()
            tvg_id = re.search(r'tvg-id="([^"]+)"', stripped)
            tvg_name = re.search(r'tvg-name="([^"]+)"', stripped)
            if tvg_id:
                current['id_hint'] = tvg_id.group(1).strip()
            if tvg_name and not current.get('name'):
                current['name'] = tvg_name.group(1).strip()
            continue
        if stripped.startswith('#'):
            continue
        name = (current.get('name') or '').strip() or f'Channel {len(items) + 1}'
        items.append({
            'id_hint': current.get('id_hint') or make_channel_id(len(items) + 1),
            'name': name,
            'url': stripped,
            'mpegts_url': '',
            'type': '',
            'type_diagnosed': False,
            'epg_enabled': True,
            'hidden': False,
            'rtp': False,
            'source': 'm3u',
        })
        current = {}
    return items


def parse_import_channels(raw):
    text = (raw or '').strip()
    if not text:
        raise ValueError('가져올 채널 목록을 입력하세요.')

    if text.startswith('#EXTM3U') or '#EXTINF:' in text:
        items = parse_m3u_channels(text)
        detected = 'm3u8'
    else:
        for fmt, parser in (('json', parse_json_channels), ('yaml', parse_yaml_channels)):
            try:
                items = parser(text)
                detected = fmt
                break
            except Exception:
                continue
        else:
            raise ValueError('지원하지 않는 형식입니다. json, yaml, m3u8 형식을 사용하세요.')

    if not items:
        raise ValueError(f'입력한 {detected} 형식에서 채널을 찾지 못했습니다.')
    return {
        'format': detected,
        'channels': apply_known_channel_types(items),
    }


def load_import_source(raw):
    text = (raw or '').strip()
    if not text:
        raise ValueError('가져올 채널 목록을 입력하세요.')

    if any(token in text for token in ('\n', '\r', '#EXTINF', '{', '[')):
        return {
            'content': text,
            'source': 'inline',
        }

    if re.match(r'^https?://', text, re.I):
        target_url = normalize_url_to_localhost_if_ddns(text)
        req = urllib.request.Request(target_url, headers={'User-Agent': ModelSetting.get('user_agent') or 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode('utf-8', errors='replace')
        return {
            'content': content,
            'source': text,
        }

    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (Path(__file__).resolve().parent / path).resolve()
    if not path.exists() or not path.is_file():
        raise ValueError(f'파일을 찾을 수 없습니다: {path}')
    try:
        content = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        content = path.read_text(encoding='utf-8', errors='replace')
    return {
        'content': content,
        'source': str(path),
    }


def import_channels_from_source(raw):
    loaded = load_import_source(raw)
    parsed = parse_import_channels(loaded['content'])
    parsed['source'] = loaded['source']
    parsed['channels'] = restore_localhost_urls_to_source_host(parsed.get('channels', []), loaded['source'])
    parsed['channels'] = mark_forward_channels(parsed.get('channels', []))
    return parsed


def normalize_url_to_localhost_if_ddns(url):
    ddns = (SystemModelSetting.get('ddns') or '').strip()
    if not ddns:
        return url

    ddns_parsed = urllib.parse.urlparse(ddns)
    ddns_host = (ddns_parsed.hostname or '').strip().lower()
    if not ddns_host:
        return url
    ddns_port = ddns_parsed.port

    parsed = urllib.parse.urlparse(str(url or '').strip())
    host = (parsed.hostname or '').strip().lower()
    port = parsed.port
    if parsed.scheme not in ('http', 'https'):
        return url
    if host != ddns_host:
        return url
    if not (ddns_port in (None, port) or port in (None, ddns_port)):
        return url

    netloc = 'localhost'
    if port is not None:
        netloc = f'{netloc}:{port}'
    elif ddns_port is not None:
        netloc = f'{netloc}:{ddns_port}'
    return urllib.parse.urlunparse((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))


def restore_localhost_urls_to_source_host(channels, source_url):
    source_text = str(source_url or '').strip()
    if not re.match(r'^https?://', source_text, re.I):
        return channels

    source_parsed = urllib.parse.urlparse(source_text)
    source_host = (source_parsed.hostname or '').strip()
    if not source_host:
        return channels

    normalized = []
    for channel in channels or []:
        item = dict(channel)
        url = str(item.get('url') or '').strip()
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or '').strip().lower()
        if parsed.scheme in ('http', 'https') and host in ('localhost', '127.0.0.1'):
            netloc = source_host
            if parsed.port is not None:
                netloc = f'{netloc}:{parsed.port}'
            elif source_parsed.port is not None:
                netloc = f'{netloc}:{source_parsed.port}'
            item['url'] = urllib.parse.urlunparse((
                source_parsed.scheme or parsed.scheme,
                netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            ))
        normalized.append(item)
    return normalized


def mark_forward_channels(channels):
    normalized = []
    for channel in channels or []:
        item = dict(channel)
        if is_forward_channel_url(item.get('url') or ''):
            item['type'] = 'forward'
            item['type_diagnosed'] = True
            item['rtp'] = False
        normalized.append(item)
    return normalized


def get_channels():
    manual_channels = parse_manual_channels(get_manual_channels_raw())
    legacy_line_channels = []
    if not manual_channels:
        legacy_line_channels = parse_line_channels(ModelSetting.get('line_channels') or '')
    merged = []
    index = 1
    for item in manual_channels + legacy_line_channels:
        channel_id = item.get('id_hint') or make_channel_id(index)
        normalized = {
            'id': re.sub(r'[^a-zA-Z0-9_-]', '_', str(channel_id)) or make_channel_id(index),
            'name': item['name'],
            'url': item['url'],
            'mpegts_url': item.get('mpegts_url', ''),
            'type': item['type'],
            'type_diagnosed': item.get('type_diagnosed', False),
            'epg_source': normalize_epg_source(item.get('epg_source')),
            'epg_enabled': item.get('epg_enabled', True),
            'hidden': item.get('hidden', False),
            'rtp': item.get('rtp', False),
            'source': item.get('source', 'line'),
        }
        merged.append(normalized)
        index += 1
    return apply_known_channel_types(merged)


def get_channel_map():
    return {item['id']: item for item in get_channels()}


def get_operation_mode():
    mode = str(ModelSetting.get('operation_mode') or 'personal').strip().lower()
    return mode if mode in ('personal', 'shared') else 'personal'
