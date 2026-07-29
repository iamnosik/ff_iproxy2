import glob
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from plugin import F

from .setup import P
from .channel_service import (
    detect_iproxy_api_type,
    get_channel_map,
    is_forward_channel_url,
    normalize_url_to_localhost_if_ddns,
)

logger = P.logger
ModelSetting = P.ModelSetting
path_app_root = F.path_app_root

STATE_LOCK = threading.Lock()
HLS_START_LOCK = threading.Lock()
processes_hls = {}
udp_clients = {}
last_access_hls = {}
last_access_udp = {}
cleanup_started = False
encoder_probe_cache = {}
hls_ready_logged = set()

def ensure_cleanup_thread():
    global cleanup_started
    with STATE_LOCK:
        if cleanup_started:
            return
        cleanup_started = True
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()


def get_plugin_data_dir():
    path = Path(__file__).resolve().parent / 'data'
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_hls_dir():
    path = get_plugin_data_dir() / 'hls'
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_ffmpeg_bin():
    if platform.system() == 'Windows':
        candidate = os.path.join(path_app_root, 'bin', 'Windows', 'ffmpeg.exe')
        if os.path.exists(candidate):
            return candidate
    return ModelSetting.get('ffmpeg_path') or 'ffmpeg'


def get_idle_timeout():
    try:
        return max(5, int(ModelSetting.get('idle_timeout')))
    except Exception:
        return 30


def fetch_url(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': ModelSetting.get('user_agent')})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.error('FETCH ERROR %s : %s', url, e)
        return None


def get_hls_default_options():
    return {
        'codec_mode': normalize_hls_codec_mode(ModelSetting.get('hls_codec_mode')),
        'resolution': normalize_hls_resolution_value(ModelSetting.get('hls_target_resolution')),
        'preset': normalize_hls_preset_value(ModelSetting.get('hls_preset')),
    }


def normalize_hls_codec_mode(value):
    text = (value or 'original').strip().lower()
    return text if text in ('original', 'h264', 'browser') else 'original'


def normalize_hls_resolution_value(value):
    text = (value or 'original').strip().lower()
    return text if text in ('original', '1080p', '720p', '480p') else 'original'


def normalize_hls_preset_value(value):
    text = (value or 'normal').strip().lower()
    return text if text in ('fast', 'normal', 'quality') else 'normal'


def get_hls_request_options(req=None):
    options = get_hls_default_options()
    if req is None:
        return options
    options['codec_mode'] = normalize_hls_codec_mode(req.args.get('codec_mode') or options['codec_mode'])
    options['resolution'] = normalize_hls_resolution_value(req.args.get('resolution') or options['resolution'])
    options['preset'] = normalize_hls_preset_value(req.args.get('preset') or options['preset'])
    return options


def get_hls_session_key(channel_id, options=None):
    if options is None:
        effective = get_hls_default_options()
    else:
        effective = {
            'codec_mode': normalize_hls_codec_mode(options.get('codec_mode')),
            'resolution': normalize_hls_resolution_value(options.get('resolution')),
            'preset': normalize_hls_preset_value(options.get('preset')),
        }
    digest = hashlib.md5(json.dumps(effective, sort_keys=True).encode('utf-8')).hexdigest()[:12]
    return f'{channel_id}__{digest}'


def get_hls_session_dir(hls_key):
    path = get_hls_dir() / hls_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_hls_playlist_path(hls_key):
    return get_hls_session_dir(hls_key) / 'live.m3u8'


def get_hls_segment_pattern(hls_key):
    return 'segment_%05d.ts'


def get_hls_key_from_segment_filename(filename):
    normalized = str(filename or '').replace('\\', '/').strip('/')
    if '/' in normalized:
        return normalized.split('/', 1)[0]
    stem = os.path.splitext(os.path.basename(normalized))[0]
    if '_' in stem:
        return stem.rsplit('_', 1)[0]
    return stem


def stop_hls(hls_key, reason='stop'):
    with STATE_LOCK:
        proc = processes_hls.pop(hls_key, None)
        last_access_hls.pop(hls_key, None)
        hls_ready_logged.discard(hls_key)
    if proc is not None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    session_dir = get_hls_dir() / hls_key
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


def stop_hls_by_channel(channel_id):
    """채널 ID에 속한 모든 hls_key 프로세스 및 파일을 정리합니다."""
    with STATE_LOCK:
        keys_to_stop = [k for k in list(processes_hls.keys()) if k.startswith(f'{channel_id}__')]
    for key in keys_to_stop:
        stop_hls(key, reason='replace_channel_session')
    hls_dir = get_hls_dir()
    prefix = f'{channel_id}__'
    for path in hls_dir.glob(f'{prefix}*'):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            try:
                path.unlink()
            except Exception:
                pass


def _log_process_output(proc, prefix):
    if proc.stderr is None:
        return
    try:
        for _ in proc.stderr:
            pass
    except Exception:
        pass


def _log_hls_process_output(proc, hls_key, channel_id):
    _log_process_output(proc, '')
    try:
        returncode = proc.wait(timeout=1)
    except Exception:
        returncode = proc.poll()
    logger.info('[LIVE:hls] end channel=%s session=%s returncode=%s', channel_id, hls_key, returncode)


def start_hls(channel_id, options=None):
    with HLS_START_LOCK:
        return _start_hls_locked(channel_id, options)


def _start_hls_locked(channel_id, options=None):
    channels = get_channel_map()
    channel = channels.get(channel_id)
    if channel is None or channel['type'] not in ('udp', 'rtp', 'http_stream', 'http_hls', 'forward'):
        return ''
    hls_options = get_hls_default_options() if options is None else dict(options)
    hls_key = get_hls_session_key(channel_id, hls_options)
    session_dir = get_hls_session_dir(hls_key)

    with STATE_LOCK:
        proc = processes_hls.get(hls_key)
        if proc is not None and proc.poll() is None:
            last_access_hls[hls_key] = time.time()
            return hls_key

    ffmpeg_bin = get_ffmpeg_bin()
    source = channel['url']
    input_url = source
    if source.startswith('udp://'):
        input_url = f'{source}?fifo_size=5000000&overrun_nonfatal=1'
    codec_mode = normalize_hls_codec_mode(hls_options.get('codec_mode'))
    video_codec = ''
    source_height = 0
    should_transcode_h264 = False
    target_height = get_hls_target_height(hls_options)
    target_width, _ = get_hls_target_dimensions(hls_options)
    probe = ffmpeg_probe_input(input_url, timeout=8)
    video_codec = normalize_video_codec_name(probe.get('video_codec'))
    source_height = int(probe.get('height') or 0)
    # browser 모드는 원본 코덱·오디오 구성과 상관없이 데스크톱 브라우저가
    # 재생할 수 있는 H.264/AAC HLS를 만듭니다. 일반 HLS/API 요청은 기존
    # 설정을 유지하므로 MPEG-TS 패스스루에는 영향을 주지 않습니다.
    if codec_mode == 'browser':
        should_transcode_h264 = True
    elif codec_mode == 'h264':
        should_transcode_h264 = video_codec in ('hevc', 'h265')
    should_scale = bool(target_height and source_height and target_height < source_height)
    should_transcode = should_transcode_h264 or should_scale
    selected_encoder = (ModelSetting.get('hls_h264_encoder') or 'libx264').strip()

    def build_hls_command(encoder_name):
        cmd = [
            ffmpeg_bin,
            '-loglevel', 'info',
            '-fflags', '+genpts+discardcorrupt',
        ]
        if input_url.startswith('http://') or input_url.startswith('https://'):
            user_agent = (ModelSetting.get('user_agent') or '').strip()
            if user_agent:
                cmd.extend(['-user_agent', user_agent])
        cmd.extend(['-i', input_url])
        if should_transcode:
            encoder = encoder_name or 'libx264'
            vaapi_device = (ModelSetting.get('hls_vaapi_device') or '').strip()
            preset = map_encoder_preset(encoder, get_hls_preset_value(hls_options))
            cmd.extend([
                '-map', '0:v:0',
                '-map', '0:a?',
            ])
            if encoder in ('h264_vaapi',):
                cmd[1:1] = ['-vaapi_device', vaapi_device or '/dev/dri/renderD128']
                vf = 'format=nv12,hwupload'
                if should_scale and target_width and target_height:
                    vf += f',scale_vaapi={target_width}:{target_height}'
                cmd.extend([
                    '-vf', vf,
                    '-c:v', encoder,
                    '-b:v', '1500k',
                    '-maxrate', '1500k',
                    '-bufsize', '3000k',
                    '-g', '60',
                ])
            else:
                if should_scale and target_width and target_height:
                    cmd.extend(['-vf', f'scale={target_width}:{target_height}'])
                cmd.extend(['-c:v', encoder])
                if encoder == 'libx264':
                    if preset:
                        cmd.extend(['-preset', preset])
                    cmd.extend(['-crf', '23'])
                elif encoder == 'h264_nvenc':
                    if preset:
                        cmd.extend(['-preset', preset])
                    cmd.extend(['-pix_fmt', 'nv12'])
                elif encoder == 'h264_qsv' and preset:
                    cmd.extend(['-preset', preset])
                if encoder == 'libx264':
                    cmd.extend(['-pix_fmt', 'yuv420p'])
                cmd.extend([
                    '-b:v', '1500k',
                    '-maxrate', '1500k',
                    '-bufsize', '3000k',
                    '-g', '60',
                    '-force_key_frames', 'expr:gte(t,n_forced*2)',
                ])
            cmd.extend([
                '-c:a', 'aac',
                '-b:a', '192k',
                '-ar', '48000',
            ])
        else:
            cmd.extend([
                '-ignore_unknown',
                '-map', '0:v:0?',
                '-map', '0:a:0?',
                '-sn',
                '-dn',
                '-c', 'copy',
            ])
        cmd.extend([
            '-f', 'hls',
            '-hls_time', str(ModelSetting.get('hls_time')),
            '-hls_list_size', str(ModelSetting.get('hls_list_size')),
            '-hls_flags', 'delete_segments+append_list+independent_segments+temp_file+omit_endlist',
            '-hls_segment_filename', get_hls_segment_pattern(hls_key),
            str(get_hls_playlist_path(hls_key)),
        ])
        return cmd

    cmd = build_hls_command(selected_encoder)
    if should_transcode:
        vaapi_device = (ModelSetting.get('hls_vaapi_device') or '').strip()
    stop_hls_by_channel(channel_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    logger.info('[LIVE:hls] start channel=%s session=%s dir=%s source=%s', channel_id, hls_key, session_dir, input_url)
    proc = subprocess.Popen(
        cmd,
        cwd=str(session_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    threading.Thread(target=_log_hls_process_output, args=(proc, hls_key, channel_id), daemon=True).start()
    if should_transcode and selected_encoder != 'libx264':
        time.sleep(1)
        if proc.poll() is not None:
            cmd = build_hls_command('libx264')
            proc = subprocess.Popen(
                cmd,
                cwd=str(session_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
            )
            threading.Thread(target=_log_hls_process_output, args=(proc, hls_key, channel_id), daemon=True).start()
    with STATE_LOCK:
        processes_hls[hls_key] = proc
        last_access_hls[hls_key] = time.time()
    return hls_key


TS_PACKET_SIZE = 188


def strip_rtp_header(data):
    if len(data) < 12:
        return data
    version = (data[0] >> 6) & 0x3
    if version != 2:
        return data
    cc = data[0] & 0x0F
    has_ext = (data[0] >> 4) & 0x1
    header_len = 12 + cc * 4
    if has_ext:
        if len(data) < header_len + 4:
            return data
        ext_len = ((data[header_len + 2] << 8) | data[header_len + 3]) * 4
        header_len += 4 + ext_len
    if header_len > len(data):
        return data
    return data[header_len:]


def align_ts_packets(data):
    start = -1
    for i in range(min(len(data), TS_PACKET_SIZE)):
        if data[i] == 0x47:
            next_pos = i + TS_PACKET_SIZE
            if next_pos >= len(data) or data[next_pos] == 0x47:
                start = i
                break
    if start < 0:
        return b''
    payload = data[start:]
    aligned_len = (len(payload) // TS_PACKET_SIZE) * TS_PACKET_SIZE
    return payload[:aligned_len]


def ffmpeg_probe_input(input_url, timeout=8):
    ffmpeg_bin = get_ffmpeg_bin()
    cmd = [
        ffmpeg_bin,
        '-hide_banner',
        '-nostdin',
        '-loglevel', 'info',
    ]
    if input_url.startswith('http://') or input_url.startswith('https://'):
        user_agent = (ModelSetting.get('user_agent') or '').strip()
        if user_agent:
            cmd.extend(['-user_agent', user_agent])
    cmd.extend([
        '-analyzeduration', '1000000',
        '-probesize', '1000000',
        '-i', input_url,
        '-map', '0:0',
        '-t', '1',
        '-c', 'copy',
        '-f', 'null',
        '-',
    ])
    started_at = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    timed_out = False
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        _, stderr = proc.communicate()
    success = ('Input #0' in stderr and 'Stream #0' in stderr) or ('Output #0' in stderr)
    width, height = extract_ffmpeg_video_resolution(stderr)
    result = {
        'success': success,
        'timed_out': timed_out,
        'stderr': stderr,
        'returncode': proc.returncode,
        'command': cmd,
        'video_codec': extract_ffmpeg_video_codec(stderr),
        'width': width,
        'height': height,
    }
    return result


def extract_ffmpeg_video_codec(stderr):
    match = re.search(r'Video:\s*([a-zA-Z0-9_]+)', stderr or '')
    if not match:
        return ''
    return match.group(1).strip().lower()


def extract_ffmpeg_video_resolution(stderr):
    match = re.search(r'Video:\s*[a-zA-Z0-9_]+.*?,\s*(\d{2,5})x(\d{2,5})', stderr or '')
    if not match:
        return 0, 0
    try:
        return int(match.group(1)), int(match.group(2))
    except Exception:
        return 0, 0


def normalize_video_codec_name(codec_name):
    text = (codec_name or '').strip().lower()
    if text in ('hevc', 'h265', 'libx265'):
        return 'hevc'
    if text in ('h264', 'avc1', 'libx264'):
        return 'h264'
    return text


def get_hls_target_height(options=None):
    if options is None:
        options = get_hls_default_options()
    value = normalize_hls_resolution_value(options.get('resolution'))
    return {
        '1080p': 1080,
        '720p': 720,
        '480p': 480,
    }.get(value, 0)


def get_hls_target_dimensions(options=None):
    if options is None:
        options = get_hls_default_options()
    value = normalize_hls_resolution_value(options.get('resolution'))
    return {
        '1080p': (1920, 1080),
        '720p': (1280, 720),
        '480p': (854, 480),
    }.get(value, (0, 0))


def get_hls_preset_value(options=None):
    if options is None:
        options = get_hls_default_options()
    return normalize_hls_preset_value(options.get('preset'))


def map_encoder_preset(encoder, preset):
    if encoder == 'libx264':
        return {'fast': 'veryfast', 'normal': 'medium', 'quality': 'slow'}.get(preset, 'medium')
    if encoder == 'h264_nvenc':
        return {'fast': 'fast', 'normal': 'medium', 'quality': 'slow'}.get(preset, 'medium')
    if encoder == 'h264_qsv':
        return {'fast': 'faster', 'normal': 'medium', 'quality': 'slow'}.get(preset, 'medium')
    return ''


def candidate_h264_encoders():
    return ['h264_nvenc', 'h264_qsv', 'h264_vaapi', 'libx264']


def list_vaapi_devices():
    dri_root = '/dev/dri'
    if not os.path.isdir(dri_root):
        return []
    devices = []
    for name in sorted(os.listdir(dri_root)):
        if name.startswith('renderD'):
            devices.append(os.path.join(dri_root, name))
    return devices


def build_encoder_probe_command(ffmpeg_path, encoder, vaapi_device=None):
    base = [ffmpeg_path, '-hide_banner', '-loglevel', 'error']
    if encoder == 'h264_vaapi':
        return base + [
            '-vaapi_device', vaapi_device,
            '-f', 'lavfi',
            '-i', 'testsrc2=size=1280x720:rate=30',
            '-vf', 'format=nv12,hwupload',
            '-frames:v', '30',
            '-an',
            '-c:v', encoder,
            '-f', 'null', '-',
        ]
    cmd = base + [
        '-f', 'lavfi',
        '-i', 'testsrc2=size=1280x720:rate=30',
        '-frames:v', '30',
        '-an',
        '-c:v', encoder,
    ]
    if encoder == 'h264_nvenc':
        cmd.extend(['-preset', 'medium', '-pix_fmt', 'nv12'])
    cmd.extend(['-f', 'null', '-'])
    return cmd


def encoder_is_usable(ffmpeg_path, encoder, force=False, with_device=False):
    cache_key = encoder
    if force:
        encoder_probe_cache.pop(cache_key, None)
    cached = encoder_probe_cache.get(cache_key)
    if cached is not None:
        ok, device = cached
        return (ok, device) if with_device else ok
    if encoder == 'h264_vaapi':
        for device in list_vaapi_devices():
            cmd = build_encoder_probe_command(ffmpeg_path, encoder, vaapi_device=device)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
            if proc.returncode == 0:
                encoder_probe_cache[cache_key] = (True, device)
                return (True, device) if with_device else True
        encoder_probe_cache[cache_key] = (False, '')
        return (False, '') if with_device else False
    cmd = build_encoder_probe_command(ffmpeg_path, encoder)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    ok = proc.returncode == 0
    device = ''
    encoder_probe_cache[cache_key] = (ok, device)
    return (ok, device) if with_device else ok


def probe_channel_type(url, timeout=3):
    parsed = urllib.parse.urlparse(url)
    if is_forward_channel_url(url):
        normalized_url = normalize_url_to_localhost_if_ddns(url)
        return {
            'ret': 'success',
            'msg': '포워드 채널로 처리합니다.',
            'data': {
                'detected_type': 'forward',
                'suggested_url': normalized_url,
            },
        }
    if parsed.scheme in ('http', 'https'):
        iproxy_type = detect_iproxy_api_type(url)
        if iproxy_type:
            return {
                'ret': 'success',
                'msg': 'I-Proxy API URL 형식을 자동 인식했습니다.',
                'data': {
                    'detected_type': iproxy_type,
                    'diagnosed_by': 'url_pattern',
                    'ffmpeg_returncode': None,
                    'ffmpeg_timeout': False,
                },
            }
        result = ffmpeg_probe_input(url, timeout=max(5, timeout + 2))
        stderr = (result.get('stderr') or '').lower()
        detected_type = ''
        if result['success']:
            if ('format: hls' in stderr) or ('input #0, hls' in stderr) or ('input #0, applehttp' in stderr):
                detected_type = 'http_hls'
            else:
                detected_type = 'http_stream'
        return {
            'ret': 'success' if detected_type else 'warning',
            'msg': '채널 진단 완료' if detected_type else 'ffmpeg로 HTTP 채널 타입을 확인하지 못했습니다.',
            'data': {
                'detected_type': detected_type,
                'diagnosed_by': 'ffmpeg',
                'ffmpeg_returncode': result['returncode'],
                'ffmpeg_timeout': result['timed_out'],
            },
        }
    if parsed.scheme not in ('udp', 'rtp'):
        return {
            'ret': 'warning',
            'msg': 'UDP/RTP 채널만 진단할 수 있습니다.',
            'data': {'detected_type': ''},
        }

    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return {
            'ret': 'danger',
            'msg': '주소에서 호스트 또는 포트를 확인할 수 없습니다.',
        }

    rtp_url = f'rtp://{host}:{port}'
    udp_url = f'udp://{host}:{port}?fifo_size=5000000&overrun_nonfatal=1'
    tests = [
        ('rtp', rtp_url),
        ('udp', udp_url),
    ]
    attempts = []
    for detected_type, candidate in tests:
        result = ffmpeg_probe_input(candidate, timeout=max(5, timeout + 2))
        attempts.append({
            'type': detected_type,
            'returncode': result['returncode'],
            'timed_out': result['timed_out'],
            'success': result['success'],
        })
        if result['success']:
            suggested_url = urllib.parse.urlunparse((detected_type, f'{host}:{port}', '', '', '', ''))
            return {
                'ret': 'success',
                'msg': '채널 진단 완료',
                'data': {
                    'detected_type': detected_type,
                    'suggested_url': suggested_url,
                    'ffmpeg_attempts': attempts,
                },
            }
    return {
        'ret': 'warning',
        'msg': 'ffmpeg로 채널 타입을 확인하지 못했습니다.',
        'data': {
            'detected_type': '',
            'ffmpeg_attempts': attempts,
        },
    }


def stream_via_ffmpeg_fallback(channel, chunk_size=188 * 32):
    ffmpeg_bin = get_ffmpeg_bin()
    source = channel['url']
    input_url = source
    channel_id = channel.get('id') or channel.get('name') or 'unknown'
    if source.startswith('udp://'):
        input_url = f'{source}?fifo_size=5000000&overrun_nonfatal=1'

    cmd = [
        ffmpeg_bin,
        '-loglevel', 'info',
        '-fflags', '+genpts+discardcorrupt',
    ]
    if input_url.startswith('http://') or input_url.startswith('https://'):
        user_agent = (ModelSetting.get('user_agent') or '').strip()
        if user_agent:
            cmd.extend(['-user_agent', user_agent])
    cmd.extend([
        '-analyzeduration', '3000000',
        '-probesize', '3000000',
        '-i', input_url,
        '-ignore_unknown',
        '-map', '0:v?',
        '-map', '0:a?',
        '-c', 'copy',
        '-f', 'mpegts',
        '-',
    ])

    logger.info('[LIVE:mpegts] start channel=%s source=%s', channel_id, input_url)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    threading.Thread(target=_log_process_output, args=(proc, ''), daemon=True).start()

    def generate():
        close_reason = 'eof'
        try:
            while True:
                chunk = proc.stdout.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            close_reason = 'client_closed'
            raise
        except Exception:
            close_reason = 'error'
            logger.info('[LIVE:mpegts] error channel=%s source=%s', channel_id, input_url)
            raise
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            logger.info('[LIVE:mpegts] end channel=%s reason=%s returncode=%s', channel_id, close_reason, proc.poll())

    return generate()


def stream_via_ffmpeg_copy(channel, chunk_size=8192):
    """HTTP MPEG-TS는 즉시 전달하고 UDP/RTP는 기존 FFmpeg 경로를 사용합니다."""
    source = str(channel.get('url') or '').strip()
    channel_id = channel.get('id') or channel.get('name') or 'unknown'
    if not source.startswith(('http://', 'https://')):
        logger.info('[LIVE:mpegts] 패스스루 미지원 channel=%s source=%s; FFmpeg 사용', channel_id, source)
        return stream_via_ffmpeg_fallback(channel, chunk_size)

    user_agent = (ModelSetting.get('user_agent') or 'Mozilla/5.0').strip()
    upstream_request = urllib.request.Request(source, headers={'User-Agent': user_agent})
    try:
        upstream = urllib.request.urlopen(upstream_request, timeout=10)
    except Exception:
        logger.exception('[LIVE:mpegts] 패스스루 연결 실패 channel=%s source=%s; FFmpeg 사용', channel_id, source)
        return stream_via_ffmpeg_fallback(channel, chunk_size)

    logger.info('[LIVE:mpegts] 패스스루 시작 channel=%s source=%s', channel_id, source)

    def generate():
        close_reason = 'eof'
        try:
            while True:
                chunk = upstream.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            close_reason = 'client_closed'
            raise
        except Exception:
            close_reason = 'error'
            logger.exception('[LIVE:mpegts] 패스스루 오류 channel=%s source=%s', channel_id, source)
            raise
        finally:
            try:
                upstream.close()
            except Exception:
                pass
            logger.info('[LIVE:mpegts] 패스스루 종료 channel=%s reason=%s', channel_id, close_reason)

    return generate()


def wait_for_hls_ready(hls_key, min_segments=2, playlist_wait_count=60, segment_wait_count=60):
    playlist = get_hls_playlist_path(hls_key)
    for _ in range(playlist_wait_count):
        if playlist.exists():
            break
        time.sleep(0.5)
    if not playlist.exists():
        return False

    for _ in range(segment_wait_count):
        ts_files = list(get_hls_session_dir(hls_key).glob('*.ts'))
        if len(ts_files) >= min_segments:
            break
        time.sleep(0.5)
    segment_count = len(list(get_hls_session_dir(hls_key).glob('*.ts')))
    ready = segment_count >= min_segments
    if ready:
        with STATE_LOCK:
            should_log = hls_key not in hls_ready_logged
            hls_ready_logged.add(hls_key)
        if should_log:
            logger.info('[LIVE:hls] ready session=%s segments=%s playlist=%s', hls_key, segment_count, playlist)
    else:
        logger.warning('[LIVE:hls] not_ready session=%s segments=%s playlist=%s', hls_key, segment_count, playlist)
    return ready


def stream_via_ffmpeg_from_hls(hls_key, chunk_size=188 * 32):
    ffmpeg_bin = get_ffmpeg_bin()
    playlist = get_hls_playlist_path(hls_key)
    cmd = [
        ffmpeg_bin,
        '-hide_banner',
        '-nostdin',
        '-loglevel', 'warning',
        '-fflags', '+genpts+discardcorrupt',
        '-i', str(playlist),
        '-ignore_unknown',
        '-map', '0:v?',
        '-map', '0:a?',
        '-c', 'copy',
        '-f', 'mpegts',
        '-',
    ]

    logger.info('[LIVE:mpegts] start session=%s playlist=%s', hls_key, playlist)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    threading.Thread(target=_log_process_output, args=(proc, ''), daemon=True).start()

    def generate():
        close_reason = 'eof'
        try:
            while True:
                chunk = proc.stdout.read(chunk_size)
                if not chunk:
                    break
                with STATE_LOCK:
                    last_access_hls[hls_key] = time.time()
                yield chunk
        except GeneratorExit:
            close_reason = 'client_closed'
            raise
        except Exception:
            close_reason = 'error'
            logger.info('[LIVE:mpegts] error session=%s', hls_key)
            raise
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            logger.info('[LIVE:mpegts] end session=%s reason=%s returncode=%s', hls_key, close_reason, proc.poll())

    return generate()


def udp_reader_loop(channel_id):
    channels = get_channel_map()
    channel = channels[channel_id]
    parsed = urllib.parse.urlparse(channel['url'])
    mcast_group = parsed.hostname
    mcast_port = parsed.port

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((mcast_group, mcast_port))
    mreq = struct.pack('4sL', socket.inet_aton(mcast_group), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(10)
    is_rtp = None

    try:
        while True:
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                with STATE_LOCK:
                    if channel_id not in udp_clients:
                        break
                    if len(udp_clients[channel_id]) == 0 and time.time() - last_access_udp.get(channel_id, 0) > get_idle_timeout():
                        break
                continue

            with STATE_LOCK:
                if channel_id not in udp_clients:
                    break

            if is_rtp is None:
                is_rtp = channel.get('rtp', False) or (len(data) >= 12 and (data[0] >> 6) == 2 and data[0] != 0x47)

            if is_rtp:
                data = align_ts_packets(strip_rtp_header(data))
            else:
                data = align_ts_packets(data)
            if not data:
                continue

            with STATE_LOCK:
                queues = list(udp_clients.get(channel_id, []))
            for item in queues:
                try:
                    item.put_nowait(data)
                except queue.Full:
                    try:
                        item.get_nowait()
                        item.put_nowait(data)
                    except Exception:
                        pass
    finally:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
            sock.close()
        except Exception:
            pass
        with STATE_LOCK:
            udp_clients.pop(channel_id, None)


def start_udp(channel_id):
    with STATE_LOCK:
        if channel_id in udp_clients:
            last_access_udp[channel_id] = time.time()
            return True
        udp_clients[channel_id] = []
        last_access_udp[channel_id] = time.time()
    thread = threading.Thread(target=udp_reader_loop, args=(channel_id,), daemon=True)
    thread.start()
    return True


def cleanup_loop():
    while True:
        now = time.time()
        timeout = get_idle_timeout()
        with STATE_LOCK:
            stale = [key for key in list(processes_hls.keys()) if now - last_access_hls.get(key, 0) > timeout]
        for key in stale:
            stop_hls(key)
        time.sleep(10)


def touch_hls(hls_key):
    with STATE_LOCK:
        last_access_hls[hls_key] = time.time()
