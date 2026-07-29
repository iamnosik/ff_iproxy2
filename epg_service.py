import json
import re
import time
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from plugin import F, Job
import requests
try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

try:
    from bs4 import BeautifulSoup, SoupStrainer
except Exception:
    BeautifulSoup = None
    SoupStrainer = None

from .setup import P

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting


def get_resource_dir():
    return Path(__file__).resolve().parent / 'resources'


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'y', 'yes', 'on')


def normalize_epg_source(value):
    return 'AUTO'


def get_plugin_data_dir():
    path = Path(__file__).resolve().parent / 'data'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_channels():
    from .channel_service import get_channels
    return get_channels()

EPG_SOURCE_OPTIONS = ['AUTO']
EPG_AUTO_PRIORITY = ['SK', 'LG', 'DAUM', 'NAVER']
EPG_HTTP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'Pragma': 'no-cache',
}
EPG_NAVER_HEADERS = {
    'Referer': 'https://search.naver.com/search.naver?where=nexearch',
    'Sec-CH-UA': '"Chromium";v="123", "Not:A-Brand";v="8", "Google Chrome";v="123"',
    'Sec-CH-UA-Mobile': '?0',
    'Sec-CH-UA-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
}
EPG_DAUM_HEADERS = {}
DAUM_CHANNEL_CATEGORIES = ['지상파', '종합편성', '케이블', '스카이라이프', '해외위성', '라디오']
DAUM_SEARCH_URL = 'https://search.daum.net/search?DA=B3T&w=tot&rtmaxcoll=B3T&q={} 편성표'
DAUM_TITLE_REGEX = r'^(?P<title>.*?)\s?([<\(]?(?P<part>\d{1})부[>\)]?)?\s?(<(?P<subname1>.*)>)?\s?((?P<epnum>\d+)회)?\s?(<(?P<subname2>.*)>)?$'
NAVER_CHANNEL_ALIASES = {
    'CJ ONSTYLE': ['CJ 온스타일'],
    'CJ ONSTYLE+': ['CJ온스타일플러스', 'CJ 온스타일 플러스', 'CJ 온스타일+'],
    'SK stoa': ['SK스토아'],
    '신세계쇼핑': ['신세계 쇼핑', '신세계TV쇼핑', '신세계 TV쇼핑'],
    '쇼핑엔티': ['쇼핑NT', '쇼핑 N T', '쇼핑엔티TV', '쇼핑엔티 TV'],
    'W쇼핑': ['더블유쇼핑', 'W 쇼핑'],
    'OCN Movies': ['OCN Movies 편성표', 'OCN무비스', 'OCN 무비스'],
    'OCN': ['오씨엔'],
    'OCN Movies2': ['OCN Movies 2', 'OCN무비즈2', 'OCN 무비즈2', 'OCN Movies II'],
    'Screen': ['스크린'],
    'EBS2': ['EBS 2', 'EBS plus2', 'EBS Plus2'],
    'UHD Dream TV': ['UHD DreamTV', 'UHD드림TV', '유에이치디 드림티비', '드림티비'],
    'SPOTV Golf&Health': ['SPOTV Golf Health', 'SPOTV Golf & Health', '스포티비 골프앤헬스'],
    'JTBC GOLF': ['JTBC Golf', 'JTBC골프'],
    'SBS GOLF': ['SBS Golf', 'SBS골프'],
    'MBC Sports+': ['MBC SPORTS+', 'MBC Sports Plus', 'MBC 스포츠플러스', 'MBC스포츠플러스'],
    'SBS Sports': ['SBS SPORTS', 'SBS 스포츠'],
    'SPOTV': ['스포티비'],
    'GS MY SHOP': ['GS MYSHOP', 'GS 마이샵', 'GS마이샵'],
}
DAUM_CHANNEL_ALIASES = {
    'CJ ONSTYLE': ['CJ온스타일'],
    'CJ ONSTYLE+': ['CJ온스타일플러스', 'CJ온스타일 플러스'],
    '신세계쇼핑': ['(i)신세계 쇼핑', '신세계 TV쇼핑', '신세계TV쇼핑'],
    '쇼핑엔티': ['쇼핑엔티', '쇼핑NT'],
    'W쇼핑': ['W쇼핑'],
    'Screen': ['스크린'],
    'EBS2': ['EBS2', '지상파 EBS2'],
    'UHD Dream TV': ['UHD Dream TV', 'SKYLIFE UHD Dream TV'],
    'SPOTV Golf&Health': ['SPOTV Golf & Health'],
    'JTBC GOLF': ['JTBC GOLF'],
    'SBS GOLF': ['SBS Golf2', 'SBS GOLF'],
    'MBC Sports+': ['MBC Sports+', 'MBC SPORTS+', 'MBC 스포츠플러스'],
    'SBS Sports': ['SBS Sports', 'SBS 스포츠'],
    'SPOTV': ['SPOTV', 'SKYLIFE SPOTV'],
    'GS MY SHOP': ['GS MY SHOP'],
}

def get_epg_dir():
    path = get_plugin_data_dir() / 'epg'
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_epg_output_path():
    return get_epg_dir() / 'epg.xml'


def get_epg_debug_dir():
    path = get_epg_dir() / 'debug'
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_epg_log(message):
    logger.info('[EPG] %s', message)


def append_epg_channel_log(channel_name, message):
    append_epg_log(f'[{channel_name}] {message}')


def save_epg_debug_html(source, channel_name, query, text):
    safe_source = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(source or 'unknown')).strip('_') or 'unknown'
    safe_channel = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(channel_name or 'unknown')).strip('_') or 'unknown'
    safe_query = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(query or 'query')).strip('_') or 'query'
    filename = f'{safe_source}_{safe_channel}_{safe_query}.html'
    path = get_epg_debug_dir() / filename
    try:
        path.write_text(text or '', encoding='utf-8', errors='replace')
        return str(path)
    except Exception:
        logger.exception('Failed to write EPG debug html')
        return ''


def reset_epg_log(message=None):
    if message:
        append_epg_log(message)


def read_epg_log_tail(max_lines=120):
    _ = max_lines
    return 'EPG/진단 로그는 기본 로그 파일에 기록됩니다. 로그 메뉴에서 확인하세요.'


def get_current_program_map():
    path = get_epg_output_path()
    if not path.exists():
        return {}
    try:
        root = ET.fromstring(path.read_text(encoding='utf-8', errors='replace'))
    except Exception:
        logger.exception('Failed to parse epg.xml')
        return {}

    now = datetime.now()
    current = {}
    for program in root.findall('programme'):
        channel_id = program.attrib.get('channel')
        start_text = program.attrib.get('start', '')
        stop_text = program.attrib.get('stop', '')
        if not channel_id or len(start_text) < 14 or len(stop_text) < 14:
            continue
        try:
            start = datetime.strptime(start_text[:14], '%Y%m%d%H%M%S')
            stop = datetime.strptime(stop_text[:14], '%Y%m%d%H%M%S')
        except Exception:
            continue
        if not (start <= now < stop):
            continue
        title_node = program.find('title')
        sub_title_node = program.find('sub-title')
        title = title_node.text.strip() if title_node is not None and title_node.text else ''
        sub_title = sub_title_node.text.strip() if sub_title_node is not None and sub_title_node.text else ''
        if title == '':
            continue
        current[channel_id] = f'{title} {sub_title}'.strip()
    return current


def load_epg_channel_map():
    path = get_resource_dir() / 'epg_channel_map.json'
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        logger.exception('Failed to load epg_channel_map.json')
        return []


def load_epg_channel_map_full():
    path = get_resource_dir() / 'epg_channel_map_full.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        logger.exception('Failed to load epg_channel_map_full.json')
        return {}


def load_epg_service_overrides():
    path = get_resource_dir() / 'epg_service_overrides.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception('Failed to load epg_service_overrides.json')
        return {}


def load_epg_provider_channels(provider):
    data = load_epg_channel_map_full()
    section = data.get(str(provider or '').upper(), {})
    channels = section.get('CHANNELS', []) if isinstance(section, dict) else []
    return channels if isinstance(channels, list) else []


def normalize_channel_name_for_epg(name):
    text = str(name or '').strip().lower()
    text = text.replace('+', ' plus ')
    text = text.replace('&', 'and')
    return re.sub(r'[^0-9a-zA-Z가-힣]+', '', text)


def get_channel_aliases_for_epg(channel_name, source=None):
    aliases = [str(channel_name or '').strip()]
    alias_map = {}
    if source == 'NAVER':
        alias_map = NAVER_CHANNEL_ALIASES
    elif source == 'DAUM':
        alias_map = DAUM_CHANNEL_ALIASES
    for alias in alias_map.get(channel_name, []):
        alias = str(alias or '').strip()
        if alias and alias not in aliases:
            aliases.append(alias)
    return aliases


def resolve_epg_service(channel, source):
    channel_name = channel.get('name')
    aliases = get_channel_aliases_for_epg(channel.get('name'), source)
    candidates = [normalize_channel_name_for_epg(x) for x in aliases]
    candidates = [x for x in candidates if x]
    if not candidates:
        append_epg_channel_log(channel_name, f'{source} normalized candidate list is empty')
        return None
    if source in ('LG', 'SK', 'NAVER', 'DAUM'):
        overrides = load_epg_service_overrides()
        provider_overrides = overrides.get(source, {}) if isinstance(overrides, dict) else {}
        if isinstance(provider_overrides, dict):
            fixed = provider_overrides.get(channel.get('name'))
            if fixed:
                append_epg_channel_log(channel_name, f'{source} service id resolved from override: {fixed}')
                return fixed
        matches = []
        for row in load_epg_provider_channels(source):
            if not isinstance(row, dict):
                continue
            row_name = normalize_channel_name_for_epg(row.get('Name'))
            row_service = normalize_channel_name_for_epg(row.get('ServiceId'))
            if any(candidate and candidate in candidates for candidate in (row_name, row_service)):
                matches.append(row)
        if matches:
            selected = matches[0]
            if source == 'LG' and len(matches) > 1:
                exact = [row for row in matches if normalize_channel_name_for_epg(row.get('Name')) == candidates[0]]
                pool = exact or matches
                def lg_sort_key(row):
                    service = str(row.get('ServiceId') or '').strip()
                    try:
                        return int(service)
                    except Exception:
                        return -1
                selected = sorted(pool, key=lg_sort_key, reverse=True)[0]
                append_epg_channel_log(
                    channel_name,
                    f'{source} embedded map multiple matches={len(matches)} selected={selected.get("ServiceId")}',
                )
            service = selected.get('ServiceId')
            append_epg_channel_log(channel_name, f'{source} service id resolved from embedded map: {service}')
            return '' if service in (None, '') else str(service).strip()
        if source == 'DAUM':
            append_epg_channel_log(channel_name, f'{source} service id not found in embedded map; aliases={aliases}')
        elif source in ('LG', 'SK'):
            append_epg_channel_log(channel_name, f'{source} embedded map miss; fallback to legacy map')
    alias_keys = ['Name']
    service_key = 'ServiceId'
    if source == 'KT':
        alias_keys = ['KT Name', 'Name']
        service_key = 'KTCh'
    elif source == 'LG':
        alias_keys = ['LG Name', 'Name']
        service_key = 'LGCh'
    elif source == 'SK':
        alias_keys = ['SK Name', 'Name']
        service_key = 'SKCh'
    elif source == 'NAVER':
        alias_keys = ['Name', 'KT Name', 'LG Name', 'SK Name']
    for row in load_epg_channel_map():
        if not isinstance(row, dict):
            continue
        for key in alias_keys:
            candidate = normalize_channel_name_for_epg(row.get(key))
            if candidate and candidate in candidates:
                service = row.get(service_key)
                if service in (None, '') and service_key != 'ServiceId':
                    service = row.get('ServiceId')
                append_epg_channel_log(channel_name, f'{source} service id resolved from legacy map key={key} field={service_key}: {service}')
                return '' if service in (None, '') else str(service).strip()
    append_epg_channel_log(channel_name, f'{source} service id unresolved; aliases={get_channel_aliases_for_epg(channel.get("name"), source)}')
    return None


def make_epg_session():
    user_agent = (ModelSetting.get('epg_user_agent') or '').strip()
    referer = (ModelSetting.get('epg_referer') or '').strip()
    if curl_requests is not None:
        session = curl_requests.Session(impersonate='chrome')
    else:
        session = requests.Session()
    session.headers.update(EPG_HTTP_HEADERS)
    if user_agent:
        session.headers['User-Agent'] = user_agent
    if referer:
        session.headers['Referer'] = referer
    return session


def epg_request(session, method, url, params=None, data=None, headers=None, timeout=15):
    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or '').lower()
    merged_headers = {}
    if 'naver.com' in host:
        merged_headers.update(EPG_NAVER_HEADERS)
    elif 'daum.net' in host:
        merged_headers.update(EPG_DAUM_HEADERS)
    if headers:
        merged_headers.update(headers)
    response = session.request(
        method,
        url,
        params=params,
        data=data,
        timeout=timeout,
        headers=merged_headers or None,
    )
    response.raise_for_status()
    return response


def parse_kt_programs(channel, session, days):
    if BeautifulSoup is None:
        raise RuntimeError('bs4 is required for KT EPG parsing')
    schedule_url = 'https://tv.kt.com/tv/channel/pSchedule.asp'
    service_id = str(channel.get('epg_service_id') or '').strip()
    if service_id == '':
        raise RuntimeError(f'KT service id not set for {channel["name"]}')
    programmes = []
    for offset in range(days):
        target = date.today() + timedelta(days=offset)
        resp = epg_request(
            session,
            'POST',
            schedule_url,
            data={
                'ch_type': '1',
                'view_type': '1',
                'service_ch_no': service_id,
                'seldate': target.strftime('%Y%m%d'),
            },
            headers={'Referer': 'https://tv.kt.com/'},
        )
        body = BeautifulSoup(urllib.parse.unquote(resp.text), 'html.parser', parse_only=SoupStrainer('tbody'))
        if not body:
            continue
        for row in body.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) < 4:
                continue
            hours = cells[0].get_text(strip=True)
            for minute, program, category in zip(*[c.find_all('p') for c in cells[1:]]):
                start_time = datetime.strptime(
                    f'{target.isoformat()} {hours}:{minute.get_text(strip=True)}',
                    '%Y-%m-%d %H:%M',
                )
                title = program.get_text(' ', strip=True).replace('방송중 ', '').strip()
                cat = category.get_text(' ', strip=True)
                rating = ''
                for image in program.find_all('img', alt=True):
                    found = re.match(r'([\d,]+)', image['alt'])
                    if found:
                        rating = found.group(1)
                        break
                programmes.append({
                    'channel_id': channel['id'],
                    'title': title,
                    'sub_title': '',
                    'category': cat,
                    'episode': '',
                    'desc': '',
                    'rebroadcast': False,
                    'rating': rating,
                    'start': start_time,
                })
    return programmes


def parse_lg_programs(channel, session, days):
    service_id = str(channel.get('epg_service_id') or '').strip()
    if service_id == '':
        raise RuntimeError(f'LG service id not set for {channel["name"]}')
    fetch_days = min(max(1, int(days)), 5)
    title_regex = re.compile(r'\s?(?:\[.*?\])?(.*?)(?:\[(.*)\])?\s?(?:\(([\d,]+)회\))?\s?(<재>)?$')
    rating_map = {'0': 0, '1': 7, '2': 12, '3': 15, '4': 19}
    category_map = {
        '00': '영화',
        '02': '만화',
        '03': '드라마',
        '05': '스포츠',
        '06': '교육',
        '08': '연예/오락',
        '09': '공연/음악',
        '11': '다큐',
        '12': '뉴스/정보',
        '13': '라이프',
        '31': '기타',
    }
    programmes = []
    for offset in range(fetch_days):
        target = date.today() + timedelta(days=offset)
        resp = epg_request(
            session,
            'GET',
            'https://www.lguplus.com/uhdc/fo/prdv/chnlgid/v1/tv-schedule-list',
            params={
                'urcBrdCntrTvChnlId': service_id,
                'brdCntrTvChnlBrdDt': target.strftime('%Y%m%d'),
            },
        )
        payload = resp.json() or {}
        entries = payload.get('brdCntTvSchIDtoList', []) or []
        if not entries:
            continue
        for item in entries:
            title = str(item.get('brdPgmTitNm') or '').strip()
            if title == '':
                continue
            match = title_regex.match(title)
            parsed_title = match.group(1).strip() if match and match.group(1) else title
            sub_title = match.group(2).strip() if match and match.group(2) else ''
            episode = match.group(3).strip() if match and match.group(3) else ''
            rebroadcast = bool(match and match.group(4))
            start_raw = f'{item.get("brdCntrTvChnlBrdDt", "")}{item.get("epgStrtTme", "")}'
            if len(start_raw) != 16:
                continue
            category = category_map.get(str(item.get('urcBrdCntrTvSchdGnreCd') or '').strip(), '')
            rating = rating_map.get(str(item.get('brdWtchAgeGrdCd') or '').strip(), 0)
            programmes.append({
                'channel_id': channel['id'],
                'title': parsed_title,
                'sub_title': sub_title,
                'category': category,
                'episode': episode,
                'desc': str(item.get('brdPgmDscr') or '').strip(),
                'rebroadcast': rebroadcast,
                'rating': rating,
                'start': datetime.strptime(start_raw, '%Y%m%d%H:%M:%S'),
            })
    return programmes


def parse_skb_programs(channel, session, days):
    service_id = str(channel.get('epg_service_id') or '').strip()
    if service_id == '':
        raise RuntimeError(f'SK service id not set for {channel["name"]}')
    fetch_days = min(max(1, int(days)), 3)
    append_epg_channel_log(channel['name'], f'SK service id in use: {service_id}')
    resp = epg_request(
        session,
        'GET',
        'https://www.bworld.co.kr/myb/core-prod/product/btv-channel/week-frmt-list',
        params={'idSvc': service_id, 'stdDt': date.today().strftime('%Y%m%d'), 'gubun': 'week'},
        headers={'Referer': 'https://www.bworld.co.kr/'},
    )
    info_list = (((resp.json() or {}).get('result') or {}).get('chnlFrmtInfoList') or [])
    if not isinstance(info_list, list):
        raise RuntimeError(f'SK unexpected response for {channel["name"]}')
    genre_code = {
        '1': '드라마',
        '2': '영화',
        '4': '만화',
        '8': '스포츠',
        '9': '교육',
        '11': '홈쇼핑',
        '13': '예능',
        '14': '시사/다큐',
        '15': '음악',
        '16': '라이프',
        '17': '교양',
        '18': '뉴스',
    }
    title_regex = re.compile(r'^(.*?)(\(([\d,]+)회\))?(<(.*)>)?(\((재)\))?$')
    programmes = []
    for offset in range(fetch_days):
        target = date.today() + timedelta(days=offset)
        target_ymd = target.strftime('%Y%m%d')
        day_count = 0
        for info in info_list:
            if str(info.get('eventDt') or '') != target_ymd:
                continue
            title = str(info.get('nmTitle') or '').strip()
            sub_title = ''
            episode = ''
            rebroadcast = False
            match = title_regex.match(title)
            if match:
                title = (match.group(1) or '').strip()
                sub_title = (match.group(5) or '').strip()
                episode = (match.group(3) or '').strip()
                rebroadcast = bool(match.group(7))
            start_raw = str(info.get('dtEventStart') or '').strip()
            end_raw = str(info.get('dtEventEnd') or '').strip()
            if len(start_raw) != 14:
                continue
            programmes.append({
                'channel_id': channel['id'],
                'title': title,
                'sub_title': sub_title,
                'category': genre_code.get(str(info.get('cdGenre') or '').strip(), ''),
                'episode': episode,
                'desc': str(info.get('nmSynop') or '').strip(),
                'rebroadcast': rebroadcast,
                'rating': str(info.get('cdRating') or '').strip(),
                'start': datetime.strptime(start_raw, '%Y%m%d%H%M%S'),
                'end': datetime.strptime(end_raw, '%Y%m%d%H%M%S') if len(end_raw) == 14 else None,
            })
            day_count += 1
        append_epg_channel_log(channel['name'], f'SK programmes for {target.isoformat()}: {day_count}')
    return programmes


def parse_naver_programs(channel, session, days):
    if BeautifulSoup is None:
        raise RuntimeError('bs4 is required for NAVER EPG parsing')
    channel_name = channel.get('name')

    def parse_search_date(text, fallback_year):
        match = re.search(r'(\d{2})\.(\d{2})\.', str(text or ''))
        if match:
            return date(fallback_year, int(match.group(1)), int(match.group(2)))
        match = re.search(r'(\d{2})월\s*(\d{2})일', str(text or ''))
        if match:
            return date(fallback_year, int(match.group(1)), int(match.group(2)))
        return None

    def parse_search_programmes_from_html(html, target):
        section = BeautifulSoup(html, 'html.parser', parse_only=SoupStrainer('section', {'class': re.compile(r'cs_tvtime')}))
        root = section.find('section') if hasattr(section, 'find') else None
        if root is None and getattr(section, 'name', None) == 'section':
            root = section
        if root is None:
            return []

        date_links = root.select('ul._date_list a')
        target_col = None
        for idx, link in enumerate(date_links, start=1):
            label_date = parse_search_date(link.get_text(' ', strip=True), target.year)
            if label_date == target:
                target_col = idx
                break
            href = link.get('href', '')
            query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get('query', [''])[0]
            query_date = parse_search_date(urllib.parse.unquote_plus(query), target.year)
            if query_date == target:
                target_col = idx
                break

        if target_col is None:
            current = root.select_one('ul._date_list em.today')
            if current is not None:
                parent = current.find_parent('a')
                if parent is not None:
                    for idx, link in enumerate(date_links, start=1):
                        if link == parent:
                            target_col = idx
                            break

        if target_col is None:
            return []

        programmes = []
        for row in root.select('ul.program_list > li.item'):
            hour_node = row.select_one('.time_box span')
            if hour_node is None:
                continue
            hour_match = re.search(r'(\d{1,2})', hour_node.get_text(' ', strip=True))
            if hour_match is None:
                continue
            hour_value = int(hour_match.group(1))
            column = row.select_one(f'.ind_program.col{target_col}')
            if column is None:
                continue
            for inner in column.select('.inner'):
                minute_node = inner.select_one('.time_min')
                title_node = inner.select_one('.pr_title')
                if minute_node is None or title_node is None:
                    continue
                minute_match = re.search(r'(\d{1,2})', minute_node.get_text(' ', strip=True))
                if minute_match is None:
                    continue
                minute_value = int(minute_match.group(1))
                start_time = datetime.combine(target, datetime.min.time()).replace(hour=hour_value, minute=minute_value)
                sub_title_node = inner.select_one('.pr_sub_title')
                desc_text = ''
                if sub_title_node is not None:
                    desc_text = sub_title_node.get_text(' ', strip=True)
                programmes.append({
                    'channel_id': channel['id'],
                    'title': title_node.get_text(' ', strip=True),
                    'sub_title': '',
                    'category': '',
                    'episode': '',
                    'desc': desc_text,
                    'rebroadcast': inner.select_one('.s_label.re') is not None or '재' in inner.get_text(' ', strip=True),
                    'rating': '',
                    'start': start_time,
                })
        return programmes

    def fetch_search_programmes_for_date(target):
        base_names = [channel['name']]
        for alias in NAVER_CHANNEL_ALIASES.get(channel['name'], []):
            if alias not in base_names:
                base_names.append(alias)

        queries = []
        for base_name in base_names:
            queries.extend([
                f'{base_name} 편성표',
                f'{base_name} 오늘 편성표',
                f'{base_name} {target.strftime("%m월 %d일")} 편성표',
                f'{base_name}{target.strftime("%m월 %d일")} 편성표',
            ])

        seen = set()
        for query in queries:
            if query in seen:
                continue
            seen.add(query)
            try:
                append_epg_channel_log(channel_name, f'NAVER search query: {query}')
                resp = epg_request(
                    session,
                    'GET',
                    'https://search.naver.com/search.naver',
                    params={
                        'where': 'nexearch',
                        'sm': 'tab_etc',
                        'query': query,
                    },
                )
            except Exception as exception:
                append_epg_channel_log(channel_name, f'NAVER search query failed: {query} / {str(exception)}')
                continue
            parsed = parse_search_programmes_from_html(resp.text, target)
            if parsed:
                append_epg_channel_log(channel_name, f'NAVER search query matched {len(parsed)} programmes for {target.isoformat()}: {query}')
                return parsed
            append_epg_channel_log(channel_name, f'NAVER search query returned 0 programmes for {target.isoformat()}: {query}')
        return []

    service_id = str(channel.get('epg_service_id') or '').strip()
    append_epg_channel_log(channel_name, f'NAVER service id in use: {service_id or "(none)"}')
    programmes = []
    for offset in range(days):
        target = date.today() + timedelta(days=offset)
        day_programmes = []
        if service_id != '':
            try:
                append_epg_channel_log(channel_name, f'NAVER API request date={target.isoformat()} service_id={service_id}')
                resp = epg_request(
                    session,
                    'GET',
                    'https://m.search.naver.com/p/csearch/content/nqapirender.nhn',
                    params={
                        'key': 'SingleChannelDailySchedule',
                        'where': 'm',
                        'pkid': '66',
                        'u1': service_id,
                        'u2': target.strftime('%Y%m%d'),
                    },
                )
                data = resp.json()
                append_epg_channel_log(channel_name, f'NAVER API status for {target.isoformat()}: {data.get("statusCode")}')
                if str(data.get('statusCode', '')).lower() == 'success':
                    soup = BeautifulSoup(''.join(data.get('dataHtml', [])), 'html.parser')
                    for row in soup.find_all('li', {'class': 'list'}):
                        cells = row.find_all('div')
                        if len(cells) < 5:
                            continue
                        start_text = cells[1].get_text(strip=True)
                        if ':' not in start_text:
                            continue
                        start_time = datetime.strptime(f'{target.isoformat()} {start_text}', '%Y-%m-%d %H:%M')
                        sub_title = cells[5].get_text(strip=True) if len(cells) > 5 else ''
                        day_programmes.append({
                            'channel_id': channel['id'],
                            'title': cells[4].get_text(' ', strip=True),
                            'sub_title': sub_title,
                            'category': '',
                            'episode': '',
                            'desc': '',
                            'rebroadcast': row.find('span', {'class': 're'}) is not None,
                            'rating': '',
                            'start': start_time,
                        })
                append_epg_channel_log(channel_name, f'NAVER API programmes for {target.isoformat()}: {len(day_programmes)}')
            except Exception:
                append_epg_channel_log(channel_name, f'NAVER API fallback to search page for {target.isoformat()}')
        if not day_programmes:
            day_programmes = fetch_search_programmes_for_date(target)
        append_epg_channel_log(channel_name, f'NAVER final programmes for {target.isoformat()}: {len(day_programmes)}')
        programmes.extend(day_programmes)
    return programmes


def parse_daum_programs(channel, session, days):
    if BeautifulSoup is None:
        raise RuntimeError('bs4 is required for DAUM EPG parsing')
    channel_name = channel.get('name')
    service_id = str(channel.get('epg_service_id') or '').strip()
    if service_id == '':
        raise RuntimeError(f'DAUM service id not set for {channel["name"]}')
    append_epg_channel_log(channel_name, f'DAUM service id in use: {service_id}')

    query_candidates = [service_id]
    for name in get_channel_aliases_for_epg(channel.get('name'), 'DAUM'):
        if name and name not in query_candidates:
            query_candidates.append(name)
    append_epg_channel_log(channel_name, f'DAUM query candidates: {query_candidates}')

    soup = None
    matched_query = None
    title_regex = re.compile(DAUM_TITLE_REGEX)
    for query in query_candidates:
        try:
            append_epg_channel_log(channel_name, f'DAUM request query: {query}')
            resp = epg_request(session, 'GET', DAUM_SEARCH_URL.format(query), timeout=20)
            append_epg_channel_log(
                channel_name,
                'DAUM response '
                f'status={resp.status_code} bytes={len(resp.text)} '
                f'content_type={resp.headers.get("Content-Type", "")} '
                f'content_encoding={resp.headers.get("Content-Encoding", "")} '
                f'final_url={resp.url}',
            )
        except Exception as exception:
            append_epg_channel_log(channel_name, f'DAUM request failed: {query} / {str(exception)}')
            continue
        candidate_soup = BeautifulSoup(resp.text, 'html.parser')
        marker_count = len(candidate_soup.find_all(attrs={'disp-attr': 'B3T'}))
        append_epg_channel_log(channel_name, f'DAUM B3T marker count for {query}: {marker_count}')
        if marker_count > 0:
            soup = candidate_soup
            matched_query = query
            append_epg_channel_log(channel_name, f'DAUM request matched query: {query}')
            break
        title_node = candidate_soup.select_one('title')
        title_text = title_node.get_text(' ', strip=True) if title_node is not None else ''
        preview = re.sub(r'\s+', ' ', (candidate_soup.get_text(' ', strip=True) or ''))[:200]
        debug_path = save_epg_debug_html('DAUM', channel_name, query, resp.text)
        append_epg_channel_log(
            channel_name,
            f'DAUM request had no B3T marker: {query} / title={title_text} / preview={preview} / debug={debug_path}',
        )
    if soup is None:
        raise RuntimeError(f'DAUM EPG data not found for {channel["name"]}')

    day_nodes = soup.select('div[class="tbl_head head_type2"] > span > span[class="date"]')
    if not day_nodes:
        raise RuntimeError(f'DAUM date headers not found for {channel["name"]}')
    append_epg_channel_log(channel_name, f'DAUM date headers found: {len(day_nodes)} using query={matched_query}')

    current = datetime.now()
    base_date = datetime.strptime(day_nodes[0].get_text(strip=True), '%m.%d').replace(year=current.year)
    if (base_date - current).days > 0:
        base_date = base_date.replace(year=base_date.year - 1)

    programmes = []
    max_days = min(days, len(day_nodes), 7)
    for day_index in range(max_days):
        target_date = (base_date + timedelta(days=day_index)).date().isoformat()
        hours = soup.select(f'[id="tvProgramListWrap"] > table > tbody > tr > td:nth-of-type({day_index + 1})')
        append_epg_channel_log(channel_name, f'DAUM hour cell count for {target_date}: {len(hours)}')
        if len(hours) != 24:
            append_epg_channel_log(channel_name, f'DAUM day {target_date} expected 24 hour cells but got {len(hours)}')
            continue
        day_count = 0
        for hour_index, hour in enumerate(hours):
            for dl in hour.select('dl'):
                minute_nodes = dl.select('dt')
                if not minute_nodes:
                    continue
                minute_text = minute_nodes[0].get_text(strip=True)
                if minute_text == '' or minute_text.isdigit() is False:
                    continue
                minute_value = int(minute_text)
                start_time = base_date + timedelta(days=day_index, hours=hour_index, minutes=minute_value)
                title = ''
                rebroadcast = False
                rating = ''
                extras = []
                for atag in dl.select('dd > a'):
                    title = atag.get_text(' ', strip=True)
                for span in dl.select('dd > span'):
                    class_val = ' '.join(span.get('class', []))
                    if class_val == '':
                        title = span.get_text(' ', strip=True)
                    elif 'ico_re' in class_val:
                        rebroadcast = True
                    elif 'ico_rate' in class_val:
                        rating_match = re.search(r'(\d+)', class_val)
                        if rating_match:
                            rating = rating_match.group(1)
                    else:
                        extras.append(span.get_text(' ', strip=True))
                if title == '':
                    continue
                sub_title = ''
                episode = ''
                matched = title_regex.search(title)
                if matched:
                    title = (matched.group('title') or '').strip()
                    if matched.group('part'):
                        title = f'{title} {matched.group("part")}부'.strip()
                    episode = matched.group('epnum') or ''
                    sub_title = (matched.group('subname2') or matched.group('subname1') or '').strip()
                programmes.append({
                    'channel_id': channel['id'],
                    'title': title,
                    'sub_title': sub_title,
                    'category': '',
                    'episode': episode,
                    'desc': ' '.join([x for x in extras if x]),
                    'rebroadcast': rebroadcast,
                    'rating': rating,
                    'start': start_time,
                })
                day_count += 1
        append_epg_channel_log(channel_name, f'DAUM programmes for {target_date}: {day_count}')
    if matched_query and matched_query != service_id:
        append_epg_channel_log(channel_name, f'DAUM matched alternate query: {matched_query}')
    return programmes


def sort_and_fill_programmes(programmes, days):
    grouped = {}
    for item in programmes:
        grouped.setdefault(item['channel_id'], []).append(item)
    end_cap = datetime.combine(date.today() + timedelta(days=days + 1), datetime.min.time())
    for channel_id, items in grouped.items():
        items.sort(key=lambda x: x['start'])
        for index, item in enumerate(items):
            next_start = items[index + 1]['start'] if index + 1 < len(items) else end_cap
            item['stop'] = next_start
    return grouped


def build_xmltv(channels, programmes_by_channel):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE tv SYSTEM "xmltv.dtd">', '<tv generator-info-name="ff_iproxy2">']
    for channel in channels:
        lines.append(f'  <channel id="{xml_escape(channel["id"])}">')
        lines.append(f'    <display-name>{xml_escape(channel["name"])}</display-name>')
        lines.append('  </channel>')
    for channel in channels:
        for program in programmes_by_channel.get(channel['id'], []):
            start = program['start'].strftime('%Y%m%d%H%M%S +0900')
            stop = program['stop'].strftime('%Y%m%d%H%M%S +0900')
            lines.append(f'  <programme start="{start}" stop="{stop}" channel="{xml_escape(channel["id"])}">')
            lines.append(f'    <title>{xml_escape(program["title"] or "")}</title>')
            if program.get('sub_title'):
                lines.append(f'    <sub-title>{xml_escape(program["sub_title"])}</sub-title>')
            if program.get('desc'):
                lines.append(f'    <desc>{xml_escape(program["desc"])}</desc>')
            if program.get('category'):
                lines.append(f'    <category>{xml_escape(program["category"])}</category>')
            if program.get('episode'):
                lines.append(f'    <episode-num system="onscreen">{xml_escape(program["episode"])}</episode-num>')
            if program.get('rebroadcast'):
                lines.append('    <previously-shown />')
            if program.get('rating'):
                lines.append('    <rating system="KMRB">')
                lines.append(f'      <value>{xml_escape(str(program["rating"]))}</value>')
                lines.append('    </rating>')
            lines.append('  </programme>')
    lines.append('</tv>')
    return '\n'.join(lines) + '\n'


def generate_epg_xml():
    channels = [channel for channel in _get_channels() if not channel.get('hidden') and channel.get('epg_enabled', True)]
    days = max(1, min(7, int(ModelSetting.get('epg_days') or '2')))
    session = make_epg_session()
    reset_epg_log(f'EPG generation started for {len(channels)} channels')
    programmes = []
    failed_channels = []
    fetchers = {
        'KT': parse_kt_programs,
        'LG': parse_lg_programs,
        'SK': parse_skb_programs,
        'NAVER': parse_naver_programs,
        'DAUM': parse_daum_programs,
    }
    for channel in channels:
        selected = normalize_epg_source(channel.get('epg_source'))
        tried = EPG_AUTO_PRIORITY if selected == 'AUTO' else [selected]
        success = False
        last_reason = ''
        append_epg_channel_log(channel['name'], f'EPG start selected={selected} tried={tried}')
        for source in tried:
            fetcher = fetchers.get(source)
            if fetcher is None:
                continue
            try:
                append_epg_log(f'[{channel["name"]}] trying {source}')
                channel['epg_service_id'] = resolve_epg_service(channel, source)
                result = fetcher(channel, session, days)
                if result:
                    programmes.extend(result)
                    append_epg_log(f'[{channel["name"]}] {source} success ({len(result)} programmes)')
                    success = True
                    break
                append_epg_log(f'[{channel["name"]}] {source} returned no programmes')
                last_reason = f'{source}: returned no programmes'
            except Exception as exception:
                append_epg_log(f'[{channel["name"]}] {source} failed: {str(exception)}')
                last_reason = f'{source}: {str(exception)}'
        if not success:
            append_epg_log(f'[{channel["name"]}] no EPG data')
            failed_channels.append({'name': channel['name'], 'reason': last_reason or 'no EPG data'})
    xml_text = build_xmltv(channels, sort_and_fill_programmes(programmes, days))
    output = get_epg_output_path()
    output.write_text(xml_text, encoding='utf-8')
    ModelSetting.set('epg_last_generated', time.strftime('%Y-%m-%d %H:%M:%S'))
    if failed_channels:
        append_epg_log(f'Failed channels ({len(failed_channels)})')
        for item in failed_channels:
            if item.get('reason'):
                append_epg_log(f'- {item["name"]}: {item["reason"]}')
            else:
                append_epg_log(f'- {item["name"]}')
    else:
        append_epg_log('Failed channels (0)')
    append_epg_log(f'EPG generation finished: {output}')
    return output


def sync_epg_scheduler_job(enabled_override=None, schedule_override=None):
    if enabled_override is not None:
        ModelSetting.set('epg_auto_start', 'True' if parse_bool(enabled_override) else 'False')
    if schedule_override is not None:
        ModelSetting.set('epg_schedule', str(schedule_override or '').strip())
    job_id = f'{package_name}_epg_generate'
    enabled = ModelSetting.get_bool('epg_auto_start')
    schedule = (ModelSetting.get('epg_schedule') or '').strip()
    if F.scheduler.is_include(job_id):
        F.scheduler.remove_job(job_id)
    if not enabled or schedule == '':
        return {'ret': 'success', 'msg': 'EPG scheduler disabled'}
    job = Job(package_name, job_id, schedule, generate_epg_xml, 'IPTV proxy EPG generate')
    F.scheduler.add_job_instance(job)
    return {'ret': 'success', 'msg': 'EPG scheduler updated'}
