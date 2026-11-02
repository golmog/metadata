# -*- coding: utf-8 -*-
import os
import copy
import uuid
import shlex
import re
import time
import json
import base64
import threading
import subprocess
import tempfile
import configparser
import traceback
import requests
import shutil
from io import BytesIO
from datetime import datetime

from .setup import *
from .util_runtime import storage_logger, redact_diagnostic

logger = storage_logger(logger)


class MetaPreviewUtil:
    _token_cache = {}
    _impersonate_indices = {}
    _rotation_lock = threading.Lock()
    _remote_dir_cache = {}
    _verified_remote_dirs = set()
    _local_dir_cache = {}
    MAX_DIR_CACHE_SIZE = 300

    @staticmethod
    def remote_missing(result, directory=False):
        detail = str(result.stderr or '').lower()
        phrases = ('directory not found',) if directory else ('object not found', 'file not found', 'directory not found')
        return result.returncode in (3, 4) and any(phrase in detail for phrase in phrases)

    @classmethod
    def remote_stat(cls, command, lookup=None, category='', purpose='storage'):
        from .util_runtime import StorageLookupError
        try:
            result = lookup(command) if lookup else cls.run_rclone_lookup(command, category=category, purpose=purpose)
            if cls.remote_missing(result):
                return None
            if result.returncode != 0 or not result.stdout.strip():
                raise StorageLookupError(redact_diagnostic('원격 파일 상태 조회 실패 (rc=%s): %s' % (
                    result.returncode, str(result.stderr or '').strip())))
            item = json.loads(result.stdout)
            if isinstance(item, list) and len(item) == 1:
                item = item[0]
            if not isinstance(item, dict) or not (item.get('ID') or item.get('id')):
                raise StorageLookupError('원격 파일 상태 형식 오류')
            return item
        except StorageLookupError:
            raise
        except Exception as exc:
            raise StorageLookupError('원격 파일 상태를 확인할 수 없습니다.') from exc

    @staticmethod
    def validate_remote_media(item):
        from .util_runtime import IncompleteStorageError, StorageLookupError
        if type(item.get('IsDir')) is not bool or type(item.get('Size')) is not int:
            raise StorageLookupError('원격 파일 종류 또는 크기 확인 실패')
        if item['IsDir'] or item['Size'] <= 0:
            raise IncompleteStorageError('원격 미디어가 비어 있거나 정규 파일이 아닙니다.')

    @staticmethod
    def validate_directory_media(files, name):
        from .util_runtime import IncompleteStorageError, StorageLookupError
        error = getattr(files, 'unusable', {}).get(name.lower())
        if error:
            kind = IncompleteStorageError if error == 'storage_asset_incomplete' else StorageLookupError
            raise kind('원격 미디어의 종류 또는 크기를 확인하세요.')

    @staticmethod
    def log_storage_event(event, target='', category='', purpose='storage', attempt=1, returncode=0, elapsed_ms=0, reused=False, reason=''):
        try:
            category = category if category in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'JAV', 'PERSON') else 'storage'
            name = str(target).replace('\\', '/').split('?', 1)[0].split('#', 1)[0].rsplit('/', 1)[-1]
            name = '저장소 루트' if not name or ':' in name or '@' in name else re.sub(r'[^\w.() -]', '_', name)[:100]
            kind = '트레일러' if '_trailer.mp4' in name.lower() else '프리뷰' if '_preview.mp4' in name.lower() else {
                'video': '영상', 'image_batch': '이미지', 'image': '이미지', 'actor': '인물 이미지',
                'replacement': '미디어 교체', 'cleanup': '임시 파일 정리'}.get(purpose, '저장소')
            text = {'lookup_success': '조회 완료', 'lookup_not_found': '파일 없음',
                'lookup_transient_retry': '일시 오류로 재시도', 'lookup_error': '조회 실패',
                'upload_start': '업로드 시작', 'upload_confirmed': '업로드 완료',
                'cleanup_done': '정리 완료'}.get(event, '처리 상태')
            detail = (' (' + reason + ')') if event == 'upload_confirmed' and reason else ''
            if event=='lookup_success' and reason=='조회 성공 · 일치 0건':text='폴더 조회 완료 — 요청한 파일 없음'
            elif event=='lookup_success':text='저장 폴더 조회 완료'
            elif event=='lookup_not_found':text='요청한 파일 없음'
            elif event=='lookup_transient_retry':text='조회 시도 실패 — 재시도 예정'
            elif event=='lookup_error':text='조회 최종 실패 — 저장소 응답 확인 필요'
            if attempt>1:detail+=' (%d번째 시도)'%attempt
            if elapsed_ms:detail+=' (%.1f초)'%(elapsed_ms/1000)
            if reused:detail+=' (캐시 재사용)'
            label = kind if purpose == 'image_batch' else kind + ' ' + name
            emit = (logger.warning if event == 'lookup_transient_retry' else logger.error if event == 'lookup_error'
                    else logger.info if event == 'upload_confirmed' else logger.debug)
            emit('[GDrive] [%s] %s %s%s',category,label,text,detail)
            if event in ('lookup_success','lookup_not_found','lookup_transient_retry','lookup_error'):
                logger.debug('[GDrive] 조회 진단: 분류=%s, 대상=%s, 반환코드=%s, 시도=%s, 사유=%s',
                    category,label,returncode,attempt,reason)
        except Exception:
            pass

    @classmethod
    def run_rclone_lookup(cls, command, category='', purpose='storage'):
        """Classify absence from rc AND stderr; retry only transient read failures."""
        args=iter(command[command.index('lsjson')+1:])
        target=''
        for arg in args:
            if arg in ('--include','--exclude','--max-depth','--files-from-raw','--config'):
                next(args,None)
            elif not arg.startswith('-'):
                target=arg;break
        for attempt in range(1, 4):
            started = time.monotonic()
            try:
                result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
            except subprocess.TimeoutExpired:
                cls.log_storage_event('lookup_transient_retry' if attempt < 3 else 'lookup_error', target,
                    category, purpose, attempt, 'timeout', int((time.monotonic() - started) * 1000), reason='조회 시간 초과')
                if attempt == 3:
                    raise
            else:
                detail = str(result.stderr or '').lower()
                missing = cls.remote_missing(result)
                transient = not missing and (result.returncode in (5, 10) or (result.returncode != 0 and any(
                    marker in detail for marker in ('timeout', 'timed out', 'connection reset', 'connection refused',
                        'connection closed', 'temporary failure', 'temporarily unavailable', 'unexpected eof',
                        'tls handshake', '429', 'rate limit', 'ratelimit', 'backenderror', 'http 500', 'http 502',
                        'http 503', 'http 504', 'error 500:', 'error 502:', 'error 503:', 'error 504:',
                        'status code 500', 'status code 502', 'status code 503', 'status code 504'))))
                event = 'lookup_success' if result.returncode == 0 else 'lookup_not_found' if missing else (
                    'lookup_transient_retry' if transient and attempt < 3 else 'lookup_error')
                reason = ''
                if result.returncode != 0 and not missing:
                    reason = str(result.stderr or '').strip()
                if result.returncode==0:
                    try:
                        if json.loads(result.stdout)==[]:reason='조회 성공 · 일치 0건'
                    except (ValueError,TypeError):pass
                quiet_absence=missing
                if purpose=='backup_probe' and result.returncode==0:
                    try:quiet_absence=json.loads(result.stdout)==[]
                    except (ValueError,TypeError):pass
                if not (quiet_absence and purpose == 'backup_probe'):
                    cls.log_storage_event(event, target, category, purpose, attempt, result.returncode,
                        int((time.monotonic() - started) * 1000), reason=reason)
                if not transient or attempt == 3:
                    return result
            time.sleep(attempt * 2)

    @classmethod
    def complete_media(cls, data, category='JAV_CEN', media_path=None, options=None):
        """Apply the cached-read or explicit video-completion policy, then format playback."""
        completed = (data.get('extra_info') or {}).pop('_parallel_video_report', None)
        try:
            report = completed if completed is not None else cls._complete_media(data, category, media_path, options)
            if not (options or {}).get('_defer_extras'):
                cls.finalize_media_extras(data, category)
                if media_path:
                    from .util_metadata import MetaUtil
                    MetaUtil.note_media_completion(options, report, media_path)
            return report
        except Exception as exc:
            logger.exception('[MetaPreview] [%s] 미디어 처리 예외, 메타데이터 유지 (%s)', category, data.get('code'))
            extra = data.setdefault('extra_info', {})
            extra.pop('source_video_path', None)
            errors = dict(extra.get('media_errors') or {})
            errors['video'] = {'message': str(exc), 'time': datetime.now().isoformat(timespec='seconds')}
            extra['media_errors'] = errors
            report = {'changed': True, 'failures': {'video': str(exc)}, 'error_code': getattr(exc, 'error_code', 'media_processing_failed')}
            if media_path and not (options or {}).get('_defer_extras'):
                from .util_metadata import MetaUtil
                MetaUtil.note_media_completion(options, report, media_path)
            return report

    @classmethod
    def _complete_media(cls, data, category='JAV_CEN', media_path=None, options=None):
        opts = options or {}
        extra = data.setdefault('extra_info', {})
        if opts.get('_cached_metadata'):
            # Cached reads trust the registration, including an absent video. Hydrate
            # normalized rows only in this output copy; never discover or repair here.
            for kind, key in (('trailer', 'trailer_video'), ('preview', 'preview_clip')):
                if not extra.get(key):
                    for storage in ('gdrive', 'local'):
                        stored = cls.get_stored_video(data, kind, storage, category)
                        if stored:
                            extra[key] = copy.deepcopy(stored)
                            break
            return {'changed': False, 'failures': {},
                    'trailer': bool(extra.get('trailer_video')), 'preview': bool(extra.get('preview_clip'))}
        extra.pop('source_video_path', None)
        before = copy.deepcopy(extra)
        original_before = copy.deepcopy(data.get('original') or {})
        existing_item = opts.get('_existing_media_item') or {}
        if opts.get('preserve_preview') and isinstance(existing_item, dict):
            existing_preview = (existing_item.get('extra_info') or {}).get('preview_clip')
            if existing_preview:
                extra['preview_clip'] = copy.deepcopy(existing_preview)
        video_observations = {}
        cls.check_and_adopt_existing_video(data, category=category,
            reuse_gdrive=opts.get('reuse_existing_media', bool(media_path)), existing_item=opts.get('_existing_media_item'),
            reprocess=opts.get('reuse_existing_media') is False, observations=video_observations)
        extra = data.setdefault('extra_info', {})
        errors = dict(extra.get('media_errors') or {})
        errors.pop('video', None)
        failures = {}
        valid_source = bool(media_path and os.path.isfile(media_path))
        code = data.get('code') or ''
        raw_trailer = next((entry.get('content_url') for entry in (data.get('original') or {}).get('extras') or []
                            if isinstance(entry, dict) and entry.get('content_type') == 'trailer' and entry.get('content_url')), None)
        if not raw_trailer:
            raw_trailer = next((entry.get('content_url') for entry in data.get('extras') or []
                                if isinstance(entry, dict) and entry.get('content_type') == 'trailer'
                                and entry.get('content_url') and 'mode=preview_' not in entry['content_url']), None)
        if raw_trailer and cls.is_trailer_download_enabled(category) and not extra.get('trailer_video'):
            logger.debug('[MetaPreview] [%s] 공식 트레일러 처리 시작: %s', category, code)
            success, result = cls.process_trailer_download_workflow(code, raw_trailer, category,
                force=not opts.get('reuse_existing_media', False), item_dict=data,
                video_observations=video_observations,
                previous_video=cls.get_stored_video(dict(existing_item, extra_info={}), 'trailer',
                    cls.get_setting('preview_storage_type', category, default='local'), category) or
                    cls.get_stored_video(existing_item, 'trailer',
                    cls.get_setting('preview_storage_type', category, default='local'), category))
            if not success:
                failures['trailer'] = str(result)
        extra = data.setdefault('extra_info', {})
        if (valid_source and cls.is_auto_create_enabled(category)
                and not extra.get('preview_clip') and not extra.get('trailer_video')):
            logger.info('[MetaPreview] [%s] %s: 저장된 트레일러·프리뷰가 없어 원본 영상에서 프리뷰 생성 시작 (%s)', category, code, media_path)
            success, result = cls.process_preview_workflow(code, str(media_path), category, force=False, item_dict=data)
            if not success:
                failures['preview'] = str(result)
        extra = data.setdefault('extra_info', {})
        for kind, key in (('trailer', 'trailer_video'), ('preview', 'preview_clip')):
            if kind in failures:
                errors[kind] = {'message': failures[kind], 'time': datetime.now().isoformat(timespec='seconds')}
                logger.warning('[MetaPreview] [%s] %s: %s 처리 실패 — 메타데이터 유지 (%s)',category,code,
                    '트레일러' if kind=='trailer' else '프리뷰',failures[kind])
            elif extra.get(key):
                errors.pop(kind, None)
        if errors:
            extra['media_errors'] = errors
        else:
            extra.pop('media_errors', None)
        logger.debug('[MetaPreview] [%s] %s: 원본 영상 %s, 저장된 트레일러 %s, 프리뷰 %s%s',
            category,code,'있음' if valid_source else '없음','있음' if extra.get('trailer_video') else '없음',
            '있음' if extra.get('preview_clip') else '없음',' (처리 실패: '+', '.join(failures)+')' if failures else '')
        return {'changed': before != extra or original_before != (data.get('original') or {}), 'failures': failures}

    @classmethod
    def video_extra(cls, url, title, entries=None):
        """Keep all EntityExtra fields when changing a trailer's playback URL."""
        template = next((entry for entry in entries or [] if isinstance(entry, dict)
            and str(entry.get('content_type', 'trailer')).lower() == 'trailer'), {})
        result = copy.deepcopy(template)
        result.update(content_url=url, title=title, content_type='trailer')
        result.setdefault('mode', 'mp4')
        return result

    @classmethod
    def finalize_media_extras(cls, data, category='JAV_CEN'):
        """Assemble playback only after translation and the caller's filtering finish."""
        extra = data.get('extra_info') or {}
        entries = data.get('extras') or []
        original = (data.get('original') or {}).get('extras') or []
        for kind, key in (('preview', 'preview_clip'), ('trailer', 'trailer_video')):
            if not isinstance(extra.get(key), dict) or not extra[key]:
                continue
            stream = cls.get_video_stream_url(extra[key], data, video_type=kind, category=category)
            if stream:
                clip = extra[key]
                templates = [clip['extra']] if isinstance(clip.get('extra'), dict) else []
                if not templates:
                    templates = [entry for entry in entries if isinstance(entry, dict)
                        and str(entry.get('content_type', 'trailer')).lower() == 'trailer'
                        and (entry.get('content_url') == stream or
                             (kind == 'trailer' and 'mode=preview_' not in str(entry.get('content_url', ''))))]
                if not templates and kind == 'trailer':
                    templates = [entry for entry in original if isinstance(entry, dict)
                        and str(entry.get('content_type', 'trailer')).lower() == 'trailer']
                title = ('[Preview] ' if kind == 'preview' else '') + str(
                    data.get('title') or data.get('tagline') or data.get('originaltitle') or data.get('code') or '')
                selected = cls.video_extra(stream, title, templates)
                if templates:
                    clip['extra'] = copy.deepcopy(templates[0])
                data['extras'] = [selected] + [copy.deepcopy(entry) for entry in entries
                    if isinstance(entry, dict) and str(entry.get('content_type', 'trailer')).lower() != 'trailer']
                break

    @classmethod
    def get_setting(cls, key, category='JAV_CEN', default=''):
        cat_upper = str(category or 'JAV_CEN').upper()
        if cat_upper in ['JAV_UNCEN', 'UNCENSORED']:
            module_pfx = 'jav_uncensored'
        elif cat_upper in ['WESTERN', 'WEST']:
            module_pfx = 'western'
        else:
            module_pfx = 'jav_censored'

        # Category switches are read by is_enabled/is_auto_create_enabled.
        # Storage roots, playback, encoders and rclone are shared UI settings.
        key = {'preview_rclone_conf': 'rclone_conf', 'preview_rclone_remote': 'preview_gdrive_remote_path'}.get(key, key)
        val = P.ModelSetting.get(f'jav_censored_{key}')
        return val if val is not None else default

    @classmethod
    def is_enabled(cls, category='JAV_CEN'):
        """프리뷰 클립 기능 자체의 활성화 여부"""
        cat_upper = str(category or 'JAV_CEN').upper()
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')
        return P.ModelSetting.get_bool(f"{module_pfx}_use_preview_clip")

    @classmethod
    def is_auto_create_enabled(cls, category='JAV_CEN'):
        """예고편 부재 시 백그라운드 자동 생성 활성화 여부"""
        if not cls.is_enabled(category):
            return False
        cat_upper = str(category or 'JAV_CEN').upper()
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')
        return P.ModelSetting.get_bool(f"{module_pfx}_preview_auto_create")

    @classmethod
    def is_trailer_download_enabled(cls, category='JAV_CEN'):
        """트레일러 영상 자동 다운로드 활성화 여부"""
        if not cls.is_enabled(category):
            return False
        cat_upper = str(category or 'JAV_CEN').upper()
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')
        return P.ModelSetting.get_bool(f"{module_pfx}_video_auto_download_trailer")

    @classmethod
    def is_delete_video_on_meta_delete_enabled(cls, category='JAV_CEN'):
        """메타데이터 삭제 시 영상 파일도 함께 삭제할지 여부"""
        if not cls.is_enabled(category):
            return False
        cat_upper = str(category or 'JAV_CEN').upper()
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')
        return P.ModelSetting.get_bool(f"{module_pfx}_video_delete_on_metadata_delete")

    @classmethod
    def get_ffprobe_path(cls, category='JAV_CEN'):
        """FFmpeg 경로와 동일 디렉토리에서 ffprobe 바이너리를 우선 자동 탐색"""
        ffmpeg_bin = cls.get_setting("preview_ffmpeg_path", category, default="ffmpeg")
        if os.path.isabs(ffmpeg_bin) or os.path.dirname(ffmpeg_bin):
            bin_dir = os.path.dirname(ffmpeg_bin)
            probe_name = "ffprobe.exe" if os.name == 'nt' else "ffprobe"
            candidate = os.path.join(bin_dir, probe_name)
            if os.path.exists(candidate):
                return candidate
        return "ffprobe"

    @classmethod
    def get_video_duration(cls, filepath, ffprobe_path='ffprobe'):
        if not filepath or not os.path.exists(filepath):
            return 0.0
        try:
            cmd = [
                ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", filepath
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip())
        except Exception as e:
            logger.debug(f"[MetaPreview] ffprobe duration 측정 실패: {e}")
        return 0.0

    @classmethod
    def get_stream_codecs(cls, filepath, ffprobe_bin='ffprobe'):
        """소스 영상의 비디오 및 오디오 코덱 확인"""
        if not filepath or not os.path.exists(filepath):
            return None, None
        try:
            cmd = [
                ffprobe_bin, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath
            ]
            p_v = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            v_codec = p_v.stdout.strip().lower() if p_v.returncode == 0 else ""

            cmd_a = [
                ffprobe_bin, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath
            ]
            p_a = subprocess.run(cmd_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            a_codec = p_a.stdout.strip().lower() if p_a.returncode == 0 else ""

            return v_codec, a_codec
        except Exception as e:
            logger.debug(f"[MetaPreview] 코덱 스트림 확인 중 예외: {e}")
            return None, None

    @classmethod
    def generate_preview_clip(cls, source_video_path, output_filepath, category='JAV_CEN'):
        if not source_video_path or not os.path.exists(source_video_path):
            return False, "원본 영상 파일이 존재하지 않습니다."

        ffmpeg_bin = cls.get_setting("preview_ffmpeg_path", category, default="ffmpeg")
        ffprobe_bin = cls.get_ffprobe_path(category)
        total_duration = int(cls.get_setting("preview_duration", category, default="60"))
        include_audio = cls.get_setting("preview_include_audio", category, default="False") == "True"

        v_codec, a_codec = cls.get_stream_codecs(source_video_path, ffprobe_bin)
        if not v_codec:
            return False, "비디오 스트림 정보를 읽을 수 없습니다."

        allowed_v_codecs = ['h264', 'avc', 'avc1', 'hevc', 'h265', 'av01', 'av1', 'vp9']
        if v_codec not in allowed_v_codecs:
            logger.info(f"[MetaPreview] 소스 비디오 코덱({v_codec})이 MP4 패키징 비권장 코덱이므로 생성을 건너뜁니다: {os.path.basename(source_video_path)}")
            return False, f"무인코딩 변환 불가 코덱 ({v_codec}). 생성을 건너뜁니다."

        duration = cls.get_video_duration(source_video_path, ffprobe_bin)
        if duration <= 60.0:
            return False, f"영상 길이가 너무 짧아 프리뷰 클립을 생성할 수 없습니다 ({duration:.1f}초)."

        target_unit = 3.5 if total_duration <= 60 else 4.0
        num_segments = max(4, int(total_duration // target_unit))
        
        last_start = max(0.0, duration - target_unit)
        segment_interval = last_start / float(num_segments - 1)
        timestamps = [i * segment_interval for i in range(num_segments)]
        timestamps[-1] = last_start

        logger.info(f"[MetaPreview] 원본 영상의 여러 구간에서 프리뷰 생성 시작 (재인코딩 없음): {os.path.basename(source_video_path)} (총 {duration:.1f}초, 목표: {total_duration}초, {num_segments}구간 추출, 코덱: {v_codec})")

        tmp_dir = os.path.join(path_data, 'tmp', f"preview_seg_{int(time.time())}_{os.urandom(3).hex()}")
        os.makedirs(tmp_dir, exist_ok=True)
        os.makedirs(os.path.dirname(output_filepath), exist_ok=True)

        segment_files = []
        concat_list_file = os.path.join(tmp_dir, "concat_list.txt")

        allowed_a_codecs = ['aac', 'mp4a', 'mp3', 'opus', 'ac3', 'eac3']
        can_copy_audio = include_audio and (a_codec in allowed_a_codecs)

        t_start = time.time()
        try:
            for idx, ts in enumerate(timestamps):
                seg_path = os.path.join(tmp_dir, f"seg_{idx:03d}.mp4")

                cmd_cut = [
                    ffmpeg_bin, "-y",
                    "-ss", f"{ts:.2f}"
                ]
                if idx < num_segments - 1:
                    cmd_cut.extend(["-t", f"{target_unit:.2f}"])
                cmd_cut.extend([
                    "-i", source_video_path,
                    "-map", "0:v:0",
                    "-c:v", "copy"
                ])

                if v_codec in ['hevc', 'h265']:
                    cmd_cut.extend(["-tag:v", "hvc1"])

                if can_copy_audio:
                    cmd_cut.extend(["-map", "0:a:0", "-c:a", "copy"])
                else:
                    cmd_cut.append("-an")

                cmd_cut.extend(["-avoid_negative_ts", "make_zero", seg_path])

                res_cut = subprocess.run(cmd_cut, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
                if res_cut.returncode == 0 and os.path.exists(seg_path) and os.path.getsize(seg_path) > 1024:
                    segment_files.append(seg_path)
                else:
                    logger.debug('[MetaPreview] 구간 컷팅 실패: index=%s position=%s exit=%s', idx, ts, res_cut.returncode)

            if len(segment_files) < 3:
                return False, f"유효한 비디오 구간을 추출하지 못했습니다 (성공: {len(segment_files)}구간)."

            with open(concat_list_file, 'w', encoding='utf-8') as f_list:
                for seg in segment_files:
                    f_list.write(f"file '{os.path.abspath(seg)}'\n")

            cmd_concat = [
                ffmpeg_bin, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list_file,
                "-c", "copy"
            ]

            if v_codec in ['hevc', 'h265']:
                cmd_concat.extend(["-tag:v", "hvc1"])

            cmd_concat.extend([
                "-movflags", "+faststart",
                output_filepath
            ])

            res_concat = subprocess.run(cmd_concat, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=40)
            if res_concat.returncode != 0:
                logger.error('[MetaPreview] FFmpeg 무인코딩 병합 실패: exit=%s', res_concat.returncode)
                return False, '프리뷰 병합에 실패했습니다 (exit=%s).' % res_concat.returncode

            if os.path.exists(output_filepath) and os.path.getsize(output_filepath) > 0:
                elapsed = time.time() - t_start
                fsize_mb = os.path.getsize(output_filepath) / (1024 * 1024)
                actual_dur = cls.get_video_duration(output_filepath, ffprobe_bin)
                logger.info(f"[MetaPreview] 무인코딩 프리뷰 클립 생성 완료: {output_filepath} ({fsize_mb:.2f}MB, 길이: {actual_dur:.1f}초, {elapsed:.1f}초 소요, 총 {len(segment_files)}구간)")
                return True, output_filepath
            else:
                return False, "최종 결합된 프리뷰 파일이 비어있습니다."

        except Exception as e:
            logger.exception('[MetaPreview] 프리뷰 생성 예외')
            return False, '프리뷰 생성 중 오류가 발생했습니다.'
        finally:
            if os.path.exists(tmp_dir):
                try:
                    shutil.rmtree(tmp_dir)
                except Exception as e_clean:
                    logger.debug(f"[MetaPreview] 임시 컷팅 폴더 정리 예외: {e_clean}")

    @classmethod
    def get_video_relative_path(cls, item_or_dict, video_type='preview', category='JAV_CEN'):
        """카테고리별 표준 계층 경로 및 비디오 파일명 반환 ({카테고리}/{label_1|studio_1}/{label|studio}/{code}_{type}.mp4)"""
        cat_upper = str(category or 'JAV_CEN').upper()
        if isinstance(item_or_dict, dict):
            code_clean = (item_or_dict.get('code') or item_or_dict.get('ui_code') or 'unknown').strip()
            ui_code = (item_or_dict.get('ui_code') or item_or_dict.get('originaltitle') or item_or_dict.get('code') or '').strip()
            studio_raw = (item_or_dict.get('studio') or 'Unknown').strip()
            label_raw = (item_or_dict.get('label') or '').strip().upper()
        else:
            code_clean = (getattr(item_or_dict, 'code', '') or getattr(item_or_dict, 'ui_code', '') or 'unknown').strip()
            ui_code = (getattr(item_or_dict, 'ui_code', '') or getattr(item_or_dict, 'originaltitle', '') or getattr(item_or_dict, 'code', '') or '').strip()
            studio_raw = (getattr(item_or_dict, 'studio', '') or 'Unknown').strip()
            label_raw = (getattr(item_or_dict, 'label', '') or '').strip().upper()

        if cat_upper != 'WESTERN':
            code_clean = (ui_code or code_clean).lower()
        suffix = "trailer" if video_type == 'trailer' else "preview"
        filename = f"{code_clean}_{suffix}.mp4"

        from support_site import SiteAvBase

        if cat_upper == 'WESTERN':
            safe_studio = re.sub(r'[^A-Za-z0-9가-힣]', '_', studio_raw).strip('_') or 'Unknown'
            studio_1 = safe_studio[0].upper() if safe_studio else 'ETC'
            if studio_1.isdigit():
                studio_1 = '09'
            rel_dir = f"western/{studio_1}/{safe_studio}"

        elif cat_upper == 'JAV_UNCEN':
            clean_label = label_raw
            if not clean_label:
                if '-' in ui_code:
                    clean_label = ui_code.split('-')[0].strip().upper()
                else:
                    parsed_ui, _, _ = SiteAvBase._parse_ui_code(ui_code, category='JAV_UNCEN')
                    clean_label = parsed_ui.split('-')[0].strip().upper() if '-' in parsed_ui else 'ETC'
            rel_dir = f"jav_uncen/{clean_label}"

        else:
            # JAV_CEN
            clean_label = label_raw
            if not clean_label:
                if '-' in ui_code:
                    clean_label = ui_code.split('-')[0].strip().upper()
                else:
                    parsed_ui, _, _ = SiteAvBase._parse_ui_code(ui_code, category='JAV_CEN')
                    clean_label = parsed_ui.split('-')[0].strip().upper() if '-' in parsed_ui else 'ETC'
            label_1 = clean_label[0] if clean_label else 'ETC'
            if clean_label.startswith('741') or label_1.isdigit():
                label_1 = '09'
            rel_dir = f"jav_cen/{label_1}/{clean_label}"

        return rel_dir, filename

    @classmethod
    def get_preview_relative_path(cls, item, category='JAV_CEN'):
        """기존 프리뷰 전용 상대 경로 헬퍼 호환 유지"""
        return cls.get_video_relative_path(item, video_type='preview', category=category)


    @classmethod
    def get_stored_video(cls, item_or_dict, video_type, storage_type, category='JAV_CEN'):
        """Read the selected storage's recorded asset without rewriting other storage."""
        from urllib.parse import parse_qs, urlparse
        extra = item_or_dict.get('extra_info', {}) if isinstance(item_or_dict, dict) else item_or_dict.extra_info or {}
        key = 'trailer_video' if video_type == 'trailer' else 'preview_clip'
        info = extra.get(key) or {}
        if info.get('storage_type', 'local') == storage_type and info:
            return dict(info)
        rows = (item_or_dict.get('media_files') or item_or_dict.get('media') or []) if isinstance(item_or_dict, dict) else item_or_dict.media_files
        for row in rows:
            def value(key):
                return row.get(key) if isinstance(row, dict) else getattr(row, key, None)
            if value('media_type') != video_type or not value('relative_path'):
                continue
            fid = value('gdrive_fileid')
            if bool(fid) != (storage_type == 'gdrive'):
                continue
            relative = value('relative_path')
            result = dict(storage_type=storage_type, relative_path=relative,
                          filename=relative.replace('\\', '/').rsplit('/', 1)[-1],
                          google_fileid=fid or '', local_path='')
            if storage_type == 'local':
                query = parse_qs(urlparse(value('url') or '').query)
                if query.get('mode') != ['preview_local'] or not query.get('path'):
                    continue
                result['local_path'] = query['path'][0]
            return result
        return None

    @classmethod
    def get_video_stream_url(cls, clip_info, item_or_dict, video_type='preview', category='JAV_CEN'):
        """저장된 영상 클립(프리뷰 또는 트레일러)의 최종 서빙/스트리밍 웹 URL 생성"""
        if not clip_info or not isinstance(clip_info, dict):
            return ''

        storage_type = clip_info.get('storage_type', 'local')
        file_id = clip_info.get('google_fileid')
        local_path = clip_info.get('local_path')

        ddns_host = (F.SystemModelSetting.get('ddns') or '').rstrip('/')
        cat_upper = str(category or 'JAV_CEN').upper()
        is_uncen = (cat_upper == 'JAV_UNCEN')
        video_endpoint = 'jav_video_un' if is_uncen else 'jav_video'

        # 구글 드라이브 저장 방식
        if storage_type == 'gdrive' and file_id:
            return f"{ddns_host}/metadata/normal/{video_endpoint}?mode=preview_gdrive&fileid={file_id}&cat={cat_upper}"

        # 로컬 디렉토리 저장 방식
        if storage_type == 'local' and local_path:
            # Local videos always use the FF streaming proxy.
            from urllib.parse import quote_plus
            return f"{ddns_host}/metadata/normal/{video_endpoint}?mode=preview_local&path={quote_plus(local_path)}"

        return ''


    @classmethod
    def normalize_rclone_remote_name(cls, remote_str):
        """리모트 문자열 끝에 콜론(:)이 없으면 자동으로 붙여 Rclone 표준 리모트명으로 변환"""
        if not remote_str or not isinstance(remote_str, str):
            return ''
        clean = remote_str.strip()
        if not clean:
            return ''
        if ':' not in clean:
            return f"{clean}:"
        return clean


    _remote_file_paths = {}
    MAX_REMOTE_PATH_CACHE_SIZE = 16384

    @classmethod
    def _remember_remote_path(cls, file_id, relative_path, category, is_image):
        key = (str(category).upper(), bool(is_image), str(file_id))
        if key not in cls._remote_file_paths and len(cls._remote_file_paths) >= cls.MAX_REMOTE_PATH_CACHE_SIZE:
            cls._remote_file_paths.pop(next(iter(cls._remote_file_paths)), None)
        cls._remote_file_paths[key] = relative_path

    @classmethod
    def get_recorded_remote_path(cls, file_id, default='', category='JAV_CEN', is_image=False):
        return cls._remote_file_paths.get((str(category).upper(), bool(is_image), str(file_id)), default)

    @classmethod
    def clear_dir_cache(cls):
        """배치 작업 시작/종료 시 메모리 디렉터리 캐시 초기화"""
        cls._remote_file_paths.clear()
        cls._remote_dir_cache.clear()
        cls._verified_remote_dirs.clear()
        cls._local_dir_cache.clear()

    _image_parent_ids = {}

    @classmethod
    def resolve_image_parent(cls, remote_dir, binary, config, category, refresh=False):
        """Bounded folder lookup, independent of file-only remote_stat validation."""
        from .util_runtime import StorageLookupError
        root=P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or ''
        expected=cls.build_rclone_target_remote_path(root,'').rstrip('/')
        if not expected or not (remote_dir==expected or remote_dir.startswith(expected+('' if expected.endswith(':') else '/'))):
            raise StorageLookupError('이미지 폴더가 설정 리모트 루트를 벗어납니다.')
        try:settings_stat=(os.stat(config).st_mtime_ns,os.stat(config).st_size)
        except OSError:settings_stat=None
        key=(config,settings_stat,root,remote_dir)
        cached=cls._image_parent_ids.get(key)
        if not refresh and cached and time.monotonic()-cached[0]<30:return cached[1]
        result=cls.run_rclone_lookup([binary,'--config',config,'lsjson','--stat',remote_dir],category=category,purpose='image_parent')
        if cls.remote_missing(result,directory=True):
            made=subprocess.run([binary,'--config',config,'mkdir',remote_dir],capture_output=True,text=True,timeout=45)
            if made.returncode:raise StorageLookupError('이미지 목적 폴더 준비에 실패했습니다.')
            result=cls.run_rclone_lookup([binary,'--config',config,'lsjson','--stat',remote_dir],category=category,purpose='image_parent')
        if result.returncode or not result.stdout.strip():raise StorageLookupError('이미지 부모 디렉터리 조회 실패')
        value=json.loads(result.stdout)
        if isinstance(value,list):value=value[0] if len(value)==1 else None
        expected_name=remote_dir.rsplit('/',1)[-1].split(':',1)[-1]
        root_hint=re.fullmatch(r'\{([A-Za-z0-9_-]+)\}',remote_dir.split(':',1)[-1]) if remote_dir==expected else None
        virtual_root=remote_dir==expected and (remote_dir.endswith(':') or root_hint is not None)
        if not virtual_root and isinstance(value,dict) and value.get('Name') not in (None,'',expected_name):
            raise StorageLookupError('조회된 부모 폴더 이름이 목적 경로와 다릅니다.')
        fid=(value.get('ID') or value.get('id')) if isinstance(value,dict) and value.get('IsDir') is True else None
        if root_hint and fid and fid!=root_hint.group(1):raise StorageLookupError('명시한 Drive 루트 ID와 조회 결과가 다릅니다.')
        if virtual_root and not fid:
            remote_name=remote_dir.split(':',1)[0]
            if root_hint:root_id=root_hint.group(1)
            else:
                settings=configparser.RawConfigParser();settings.read(config,encoding='utf-8')
                if not settings.has_section(remote_name) or settings.get(remote_name,'type',fallback='')!='drive':
                    raise StorageLookupError('가상 리모트 루트의 Drive 신원을 확인할 수 없습니다.')
                root_id=settings.get(remote_name,'root_folder_id',fallback='') or settings.get(remote_name,'team_drive',fallback='') or 'root'
            if not re.fullmatch(r'[A-Za-z0-9_-]+',root_id):raise StorageLookupError('Drive 루트 ID 형식 오류')
            token=cls.get_gdrive_access_token(config,remote_name,file_id=root_id)
            if not token:raise StorageLookupError('Drive 루트 조회 인증 실패')
            response=requests.get('https://www.googleapis.com/drive/v3/files/'+root_id,
                params={'fields':'id,mimeType,trashed','supportsAllDrives':'true'},headers={'Authorization':'Bearer '+token},timeout=20)
            try:
                identity=response.json() if response.status_code==200 else {}
                if (identity.get('mimeType')!='application/vnd.google-apps.folder' or identity.get('trashed') is not False
                        or root_id!='root' and identity.get('id')!=root_id):raise StorageLookupError('Drive 루트 폴더 ID 확인 실패')
                fid=identity.get('id')
            finally:response.close()
        if not fid:
            # An ID-less directory stat is valid on Drive; list only its exact parent's children.
            if '/' in remote_dir.split(':',1)[-1].rstrip('/'):
                parent,name=remote_dir.rstrip('/').rsplit('/',1)
            else:
                remote,tail=remote_dir.split(':',1);parent=remote+':';name=tail.strip('/')
            if not name:raise StorageLookupError('리모트 루트의 폴더 ID를 확인할 수 없습니다.')
            listing=cls.run_rclone_lookup([binary,'--config',config,'lsjson',parent,'--dirs-only','--max-depth','1'],category=category,purpose='image_parent')
            if listing.returncode or not listing.stdout.strip():raise StorageLookupError('부모의 하위 폴더 조회 실패')
            rows=json.loads(listing.stdout)
            if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):raise StorageLookupError('하위 폴더 응답 형식 오류')
            matches=[r for r in rows if str(r.get('Name') or '').casefold()==name.casefold()]
            if len(matches)!=1 or matches[0].get('Name')!=name or matches[0].get('IsDir') is not True:raise StorageLookupError('부모 폴더 이름이 없거나 중복됩니다.')
            fid=matches[0].get('ID') or matches[0].get('id')
        if not isinstance(fid,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',fid):raise StorageLookupError('이미지 부모 폴더 ID가 없습니다.')
        if len(cls._image_parent_ids)>512:cls._image_parent_ids.clear()
        cls._image_parent_ids[key]=(time.monotonic(),fid)
        return fid

    @classmethod
    def get_remote_file_id(cls, dest_rel_path, category='JAV_CEN', is_image=False, use_dir_cache=False):
        """원격 구글 드라이브 경로의 파일 존재 여부를 사전 확인하고 File ID를 반환 (부재 시 None)."""
        if not dest_rel_path:
            return None

        clean_path = dest_rel_path.replace('\\', '/').strip('/')
        cat_upper = str(category or 'JAV_CEN').upper()

        # Share folder observations with user-image discovery in storage synchronization.
        if use_dir_cache:
            files = cls.get_remote_directory_files(os.path.dirname(clean_path), category=cat_upper,
                                                   is_image=is_image, use_cache=True)
            cls.validate_directory_media(files, os.path.basename(clean_path))
            return files.get(os.path.basename(clean_path).lower())

        # 단일 파일 stat 조회 모드
        rclone_bin = cls.get_setting("rclone_path", cat_upper, default="rclone")
        rclone_conf = cls.get_setting("rclone_conf", cat_upper, default="/data/db/rclone.conf")
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')

        if is_image:
            raw_upload_remote = (
                P.ModelSetting.get("jav_censored_image_gdrive_remote_path") or ""
            ).strip()
        else:
            raw_upload_remote = (
                cls.get_setting("preview_gdrive_remote_path", cat_upper, default="") or
                cls.get_setting("preview_video_folder_id", cat_upper, default="")
            ).strip()

        if not raw_upload_remote:
            from .util_runtime import StorageLookupError
            raise StorageLookupError('구글 드라이브 리모트 설정이 없습니다.')

        dest_remote_file = cls.build_rclone_target_remote_path(raw_upload_remote, dest_rel_path=dest_rel_path)
        if not dest_remote_file:
            from .util_runtime import StorageLookupError
            raise StorageLookupError('구글 드라이브 대상 경로 오류')

        cmd = [rclone_bin, "--config", rclone_conf, "lsjson", "--stat", dest_remote_file]
        item_obj = cls.remote_stat(cmd, category=cat_upper, purpose='actor' if '/actors/' in '/' + clean_path else 'image' if is_image else 'video')
        if item_obj is None:
            return None
        cls.validate_remote_media(item_obj)
        fid = item_obj.get('ID') or item_obj.get('id')
        actual_name = item_obj.get('Name') or item_obj.get('Path') or os.path.basename(clean_path)
        cls._remember_remote_path(fid, '/'.join(
            part for part in (os.path.dirname(clean_path), os.path.basename(actual_name)) if part), cat_upper, is_image)
        return fid

    @classmethod
    def get_remote_directory_files(cls, rel_dir, category='JAV_CEN', is_image=False, use_cache=False):
        key = (str(category).upper(), bool(is_image), rel_dir)
        if use_cache and key in cls._remote_dir_cache and key in cls._verified_remote_dirs:
            return cls._remote_dir_cache[key]
        files = cls._load_remote_directory_file_map(rel_dir, category=category, is_image=is_image)
        if use_cache:
            if len(cls._remote_dir_cache) >= cls.MAX_DIR_CACHE_SIZE:
                oldest = next(iter(cls._remote_dir_cache))
                cls._remote_dir_cache.pop(oldest, None)
                cls._verified_remote_dirs.discard(oldest)
            cls._remote_dir_cache[key] = files
            cls._verified_remote_dirs.add(key)
        return files

    @classmethod
    def register_remote_file(cls, dest_rel_path, file_id, category='JAV_CEN', is_image=False):
        """새로 업로드되거나 갱신된 파일 ID를 메모리 캐시에 즉시 반영"""
        if not dest_rel_path or not file_id:
            return

        cls._remember_remote_path(file_id, dest_rel_path.replace('\\', '/').strip('/'), category, is_image)
        clean_path = dest_rel_path.replace('\\', '/').strip('/')
        dir_name = os.path.dirname(clean_path)
        file_name = os.path.basename(clean_path).lower()
        cat_upper = str(category or 'JAV_CEN').upper()
        cache_key = (cat_upper, bool(is_image), dir_name)
        cls._verified_remote_dirs.discard(cache_key)

        if cache_key not in cls._remote_dir_cache:
            cls._remote_dir_cache[cache_key] = {}

        cls._remote_dir_cache[cache_key][file_name] = file_id

    @classmethod
    def has_local_file(cls, full_filepath, use_cache=False):
        """로컬 파일 경로의 존재 여부 확인 (캐시 옵션 지원)"""
        if not full_filepath:
            return False
        if not use_cache:
            if not os.path.exists(full_filepath):
                return False
            if not os.path.isfile(full_filepath) or os.path.getsize(full_filepath) <= 0:
                from .util_runtime import IncompleteStorageError
                raise IncompleteStorageError('로컬 미디어가 비어 있거나 정규 파일이 아닙니다.')
            return True

        norm_path = os.path.abspath(full_filepath)
        dir_path = os.path.dirname(norm_path)
        file_name = os.path.basename(norm_path).lower()

        if dir_path not in cls._local_dir_cache:
            if len(cls._local_dir_cache) >= cls.MAX_DIR_CACHE_SIZE:
                oldest_local_key = next(iter(cls._local_dir_cache))
                del cls._local_dir_cache[oldest_local_key]

            if os.path.isdir(dir_path):
                try:
                    self_files = {f.lower() for f in os.listdir(dir_path)}
                    cls._local_dir_cache[dir_path] = self_files
                except Exception:
                    cls._local_dir_cache[dir_path] = set()
            else:
                cls._local_dir_cache[dir_path] = set()

        # A filename-only cache cannot certify media type or size.
        return cls.has_local_file(full_filepath, use_cache=False)

    @classmethod
    def register_local_file(cls, full_filepath):
        """새로 생성된 로컬 파일을 디렉터리 캐시에 등록"""
        if not full_filepath:
            return

        norm_path = os.path.abspath(full_filepath)
        dir_path = os.path.dirname(norm_path)
        file_name = os.path.basename(norm_path).lower()

        if dir_path not in cls._local_dir_cache:
            cls._local_dir_cache[dir_path] = set()

        cls._local_dir_cache[dir_path].add(file_name)

    @classmethod
    def _load_remote_directory_file_map(cls, rel_dir, category='JAV_CEN', is_image=False):
        """rclone lsjson 명령으로 해당 디렉터리의 전체 파일명과 File ID를 1회 단일 쿼리로 수집"""
        cat_upper = str(category or 'JAV_CEN').upper()
        rclone_bin = cls.get_setting("rclone_path", cat_upper, default="rclone")
        rclone_conf = cls.get_setting("rclone_conf", cat_upper, default="/data/db/rclone.conf")
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')

        if is_image:
            raw_upload_remote = (
                P.ModelSetting.get("jav_censored_image_gdrive_remote_path") or ""
            ).strip()
        else:
            raw_upload_remote = (
                cls.get_setting("preview_gdrive_remote_path", cat_upper, default="") or
                cls.get_setting("preview_video_folder_id", cat_upper, default="")
            ).strip()

        from .util_runtime import StorageLookupError
        if not raw_upload_remote:
            raise StorageLookupError('구글 드라이브 리모트 설정이 없습니다.')

        remote_target_dir = cls.build_rclone_target_remote_path(raw_upload_remote, dest_rel_path=rel_dir)
        if not remote_target_dir:
            raise StorageLookupError('구글 드라이브 대상 경로 오류')

        cmd = [rclone_bin, "--config", rclone_conf, "lsjson", remote_target_dir]
        logger.debug(f"[MetaPreview] 저장소 이미지·영상 폴더 목록 조회 시작: {remote_target_dir}")

        try:
            res = cls.run_rclone_lookup(cmd, category=category, purpose='image_batch' if is_image else 'video')
            if cls.remote_missing(res, directory=True):
                return {}
            if res.returncode != 0 or not res.stdout.strip():
                raise StorageLookupError('원격 폴더 조회 실패')
            if res.returncode == 0 and res.stdout.strip():
                items = json.loads(res.stdout.strip())
                if not isinstance(items, list):
                    raise StorageLookupError('원격 폴더 목록 형식 오류')
                class FileMap(dict):
                    pass
                file_map = FileMap()
                file_map.unusable = {}
                for it in items:
                    if not isinstance(it, dict) or not (it.get('Name') or it.get('Path')):
                        raise StorageLookupError('원격 폴더 파일명 확인 실패')
                    name = str(it.get('Name') or it.get('Path'))
                    if name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif') if is_image else ('.mp4',)):
                        try:
                            cls.validate_remote_media(it)
                        except StorageLookupError as exc:
                            # Preserve a negative observation for this name, not a folder-wide outage.
                            file_map.unusable[name.lower()] = exc.error_code
                        if not (it.get('ID') or it.get('id')):
                            raise StorageLookupError('원격 미디어 File ID 확인 실패')
                    if isinstance(it, dict) and not it.get('IsDir'):
                        fname = it.get('Name') or it.get('Path')
                        fid = it.get('ID') or it.get('id')
                        if fname and fid:
                            file_map[fname.lower()] = fid
                            cls._remember_remote_path(fid, f'{rel_dir}/{fname}'.strip('/'), cat_upper, is_image)
                logger.debug(f"[MetaPreview] 저장소 이미지·영상 폴더 목록 조회 완료 ({len(file_map):,}개 파일): {remote_target_dir}")
                return file_map
        except StorageLookupError:
            raise
        except Exception as exc:
            raise StorageLookupError('원격 폴더를 확인할 수 없습니다.') from exc


    @classmethod
    def build_rclone_target_remote_path(cls, remote_str, dest_rel_path=""):
        """리모트 경로 루트와 상대 경로를 문자열로 단순 결합 (포맷 앞의 슬래시 정제)"""
        if not remote_str or not isinstance(remote_str, str):
            return None
        clean_remote = remote_str.strip()
        if not clean_remote:
            return None

        # 콜론이 없으면 기본 리모트명으로 간주하여 콜론 추가
        if ':' not in clean_remote:
            clean_remote = f"{clean_remote}:"

        clean_rel = str(dest_rel_path or '').replace('\\', '/').strip('/')
        if '..' in clean_rel.split('/'):
            raise ValueError('Remote asset path escapes its root')
        if not clean_rel:
            return clean_remote

        # 리모트가 콜론으로 끝나면 슬래시 없이 바로 연결 (예: mydrive: + jav/cen -> mydrive:jav/cen)
        if clean_remote.endswith(':'):
            return f"{clean_remote}{clean_rel}"
        # 리모트 뒤에 서브 경로가 있는 경우 슬래시로 연결 (예: mydrive:images + jav/cen -> mydrive:images/jav/cen)
        return f"{clean_remote.rstrip('/')}/{clean_rel}"


    @classmethod
    def sync_file_to_gdrive(cls, local_path, dest_rel_path, category='JAV_CEN', overwrite=True, is_image=False, prechecked=False, replacement=None):
        """
        정식명 전송 어댑터. 백업/복구/정리는 영수증이 소유한다.
        prechecked는 호환 인자이며 기존 파일 교체 권한을 부여하지 않는다.
        """
        if not local_path or not os.path.exists(local_path):
            return None, "로컬 파일이 존재하지 않습니다."

        cat_upper = str(category or 'JAV_CEN').upper()
        rclone_bin = cls.get_setting("rclone_path", cat_upper, default="rclone")
        rclone_conf = cls.get_setting("rclone_conf", cat_upper, default="/data/db/rclone.conf")

        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')

        if is_image:
            raw_upload_remote = (
                P.ModelSetting.get("jav_censored_image_gdrive_remote_path") or ""
            ).strip()
        else:
            raw_upload_remote = (
                cls.get_setting("preview_gdrive_remote_path", cat_upper, default="") or
                cls.get_setting("preview_video_folder_id", cat_upper, default="")
            ).strip()

        extra_options = cls.get_setting("rclone_options", cat_upper, default="")

        if not raw_upload_remote:
            return None, "구글 드라이브 리모트 경로 루트가 설정되지 않았습니다."

        dest_remote_file = cls.build_rclone_target_remote_path(raw_upload_remote, dest_rel_path=dest_rel_path)
        if not dest_remote_file:
            return None, "유효하지 않은 Rclone 원격 대상 경로입니다."

        def get_remote_file_stat(remote_target):
            cmd = [rclone_bin, "--config", rclone_conf, "lsjson", "--stat", remote_target]
            item = cls.remote_stat(cmd, category=cat_upper, purpose='actor' if '/actors/' in '/' + dest_rel_path else 'image' if is_image else 'video')
            if item is None:
                return None
            cls.validate_remote_media(item)
            return item

        # 원격 파일 존재 여부 사전 검사
        if replacement and replacement.get('image_contract'):
            if replacement['target']!=dest_remote_file or replacement['relative_path']!=dest_rel_path:raise ValueError('이미지 영수증 대상 불일치')
            MediaReplacement.image_write_start(replacement,local_path)
            existing_fid=None
        elif replacement:
            if (replacement.get('storage')!='gdrive' or replacement.get('target')!=dest_remote_file or
                    replacement.get('relative_path')!=dest_rel_path or replacement.get('category')!=cat_upper or
                    replacement.get('config')!=rclone_conf or replacement.get('rclone')!=rclone_bin or
                    replacement.get('status')!='pending' or
                    replacement.get('had_original') and not replacement.get('staged')):
                raise ValueError('전송 영수증 준비/대상 불일치')
            existing_fid=None
        else:
            existing_item=get_remote_file_stat(dest_remote_file)
            existing_fid=(existing_item.get('ID') or existing_item.get('id')) if existing_item else None

        # 이미 원격에 파일이 있고 덮어쓰기/재처리가 불필요한 경우: 다운로드/업로드 없이 File ID만 즉시 반환
        if existing_fid and not overwrite:
            cls.log_storage_event('lookup_success', dest_rel_path, cat_upper, 'image' if is_image else 'video', reused=True)
            return existing_fid, "already_exists"

        if existing_fid:
            return None, '기존 파일 교체는 공통 영수증의 사전 백업이 필요합니다.'

        # 정식 파일명으로 직접 업로드 실행
        cmd = [rclone_bin, "--config", rclone_conf, "moveto", local_path, dest_remote_file]
        if extra_options:
            cmd.extend(extra_options.split())
        if replacement:
            cmd.append('--immutable')

        if replacement and replacement.get('initial_observation') and replacement.get('media_type') in ('trailer', 'preview'):
            replacement.update(status='pending', **{'video_write_started': True})
        upload_size=os.path.getsize(local_path)
        cls.log_storage_event('upload_start', dest_rel_path, cat_upper, 'image' if is_image else 'video')
        try:
            p_up = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=240)
            if p_up.returncode != 0:
                logger.error('[MetaPreview] %s: Google Drive 업로드 실패 — 기존 파일의 복원 여부를 확인하세요.',os.path.basename(dest_rel_path))
                logger.debug('[MetaPreview] 업로드 실패 진단: 반환코드=%s, 응답=%s',p_up.returncode,p_up.stderr)
                # 업로드 실패 시 백업해둔 기존 파일 원상 복구
                return None, 'Rclone 업로드에 실패했습니다 (exit=%s).' % p_up.returncode

            # 업로드 완료 후 신규 File ID 조회 및 검증
            new_fid = None
            for attempt in range(1, 6):
                time.sleep(min(2.0, 0.5 * attempt))
                observation = get_remote_file_stat(dest_remote_file)
                new_fid=(observation.get('ID') or observation.get('id')) if observation else None
                if new_fid:
                    if observation.get('Name')!=os.path.basename(dest_rel_path) or observation.get('Size')!=upload_size:
                        raise ValueError('업로드 이름/크기 확인 실패')
                    break

            if not new_fid:
                return None, "업로드 후 신규 Google File ID 획득 실패"

            if replacement:
                MediaReplacement.generated(replacement,new_fid,observation=observation)

            cls.register_remote_file(dest_rel_path, new_fid, category=cat_upper, is_image=is_image)
            cls.log_storage_event('upload_confirmed', dest_rel_path, cat_upper, 'actor' if '/actors/' in '/' + dest_rel_path else 'image' if is_image else 'video')
            return new_fid, None

        except Exception as exc:
            logger.exception('[MetaPreview] 구글 드라이브 업로드 예외')
            return None, '구글 드라이브 업로드 중 오류가 발생했습니다.'

    @classmethod
    def sync_images_to_gdrive(cls, images, category='JAV_CEN', replacements=None):
        """Upload prepared images together; callers own per-file backups/rollback."""
        if not images:
            return {}, None
        parents = {os.path.dirname(relative) for relative in images}
        if len(parents) != 1:
            return {}, '일괄 업로드 이미지의 대상 폴더가 다릅니다.'
        relative_dir = parents.pop()
        remote = cls.build_rclone_target_remote_path(
            P.ModelSetting.get('jav_censored_image_gdrive_remote_path'), relative_dir)
        if not remote:
            return {}, '구글 드라이브 리모트 경로 루트가 설정되지 않았습니다.'
        binary = cls.get_setting('rclone_path', category, default='rclone')
        config = cls.get_setting('rclone_conf', category, default='/data/db/rclone.conf')
        options = shlex.split(cls.get_setting('rclone_options', category, default='') or '')
        temporary_root = os.path.join(path_data, 'tmp')
        os.makedirs(temporary_root, exist_ok=True)
        confirmed = {}
        error = None
        with tempfile.TemporaryDirectory(prefix='gdrive_batch_', dir=temporary_root) as work:
            staged = os.path.join(work, 'images')
            os.makedirs(staged)
            names = {os.path.basename(relative): relative for relative in images}
            sizes = {}
            hashes = {}
            for name, relative in names.items():
                entry=next((e for e in replacements or [] if e['relative_path']==relative and e.get('image_contract')),None)
                if not entry or not name or '\n' in name or '\r' in name:
                    raise ValueError('Invalid batch image filename')
                MediaReplacement.image_write_start(entry,images[relative])
                shutil.copyfile(images[relative], os.path.join(staged, name))
                sizes[name] = os.path.getsize(os.path.join(staged, name))
                import hashlib
                with open(images[relative], 'rb') as source:
                    hashes[name] = hashlib.md5(source.read()).hexdigest()
            file_list = os.path.join(work, 'files.txt')
            with open(file_list, 'w', encoding='utf-8') as fp:
                fp.write('\n'.join(names) + '\n')
            command = [binary, '--config', config, 'move', staged, remote, '--no-traverse'] + options + ['--immutable']
            cls.log_storage_event('upload_start', remote, str(category).upper(), 'image_batch')
            # A failed move can still have completed some files. Certify those
            # separately, using one directory query for all requested File IDs.
            for attempt in range(3):
                if any(os.path.exists(os.path.join(staged, name)) for name in names):
                    try:
                        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=240)
                        if result.returncode:
                            error = str(result.stderr or '').strip() or 'rclone move failed'
                            logger.warning('[MetaPreview] 이미지 업로드 %s번째 시도 실패 — %s',attempt+1,
                                '재시도 예정' if attempt<2 else '최종 업로드 결과 확인 필요')
                            logger.debug('[MetaPreview] 업로드 실패 진단: 반환코드=%s, 응답=%s',result.returncode,error)
                    except Exception as exc:
                        error = str(exc)
                try:
                    result = subprocess.run([binary, '--config', config, 'lsjson', remote,
                        '--files-only', '--max-depth', '1', '--hash', '--files-from-raw', file_list],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
                    cls.log_storage_event('lookup_success' if result.returncode == 0 else
                        'lookup_not_found' if cls.remote_missing(result) else 'lookup_error',
                        remote, str(category).upper(), 'image_batch', attempt + 1, result.returncode,
                        reason='' if result.returncode == 0 or cls.remote_missing(result) else str(result.stderr or '').strip())
                    if result.returncode == 0:
                        rows=json.loads(result.stdout)
                        if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):raise ValueError('이미지 전송 결과 형식 오류')
                        for name in names:
                            receipt=next(e for e in replacements if e['relative_path']==names[name])
                            initial_ids={row['ID'] for row in receipt['initial_observation']['files']}
                            matches={}
                            for row in rows:
                                fid=row.get('ID') or row.get('id')
                                if (row.get('Name') == name and fid and fid not in initial_ids and
                                        row.get('IsDir') is False and row.get('Size') == sizes[name] and sizes[name] > 0):
                                    md5=(row.get('Hashes') or {}).get('md5') or (row.get('Hashes') or {}).get('MD5')
                                    if md5 and str(md5).lower() == hashes[name]:
                                        matches[fid]=row
                            if len(matches)==1 and not os.path.exists(os.path.join(staged,name)):
                                file_id, observation=next(iter(matches.items()))
                                try:
                                    MediaReplacement.generated(receipt,file_id,observation=observation)
                                    confirmed[names[name]]=file_id
                                except Exception as exc:
                                    error=str(exc)
                            elif len(matches)>1:
                                error='동시 업로드의 신규 ID 소유권을 구분하지 못해 해당 슬롯을 보존합니다.'
                except Exception as exc:
                    error = str(exc)
                if len(confirmed) == len(images):
                    break
                if attempt < 2:
                    logger.debug('[MetaPreview] 이미지 업로드 재시도 예정: %s번째 시도, 파일 ID 미확인 %s개',
                        attempt + 2, len(images) - len(confirmed))
                    time.sleep(attempt + 1)
        if len(confirmed) != len(images):
            error = error or '일부 이미지의 업로드 또는 File ID 확인 실패'
            logger.warning('[MetaPreview] 이미지 업로드 최종 확인 보류: %s개 확인 / 전체 %s개 — %s', len(confirmed), len(images), error)
        else:
            error = None
        if len(confirmed) == len(images):
            cls.log_storage_event('upload_confirmed', remote, str(category).upper(), 'image_batch',
                reason='%s/%s개 확인' % (len(confirmed), len(images)))
        return confirmed, error

    @classmethod
    def upload_to_gdrive(cls, local_clip_path, dest_rel_path, category='JAV_CEN', prechecked=False, replacement=None):
        """기존 트레일러 업로드 호출을 통합 엔진으로 연결."""
        fid, err = cls.sync_file_to_gdrive(local_clip_path, dest_rel_path, category=category, overwrite=True, is_image=False, prechecked=prechecked, replacement=replacement)
        return fid, err


    @classmethod
    def delete_preview_clip(cls, clip_info, category='JAV_CEN', owner=None, delete_context=None, video_type='preview'):
        if not clip_info or not isinstance(clip_info, dict):
            return True, "삭제할 클립 정보가 없습니다."
        return False, 'D02_RECEIPT_REQUIRED: DB commit 전 영상의 직접 삭제를 보류했습니다.'

    @classmethod
    def delete_video_assets(cls, extra_info, category='JAV_CEN', video_type=None, owner=None, delete_context=None):
        if video_type not in (None, '', 'trailer', 'preview'):
            return False, "유효하지 않은 영상 종류입니다."
        if not extra_info or not isinstance(extra_info, dict):
            return True, "삭제할 비디오 정보가 없습니다."
        if not any(extra_info.get(key) for kind, key in
                   (('preview', 'preview_clip'), ('trailer', 'trailer_video'))
                   if not video_type or video_type == kind):
            return True, "삭제할 비디오 정보가 없습니다."
        return False, 'D02_RECEIPT_REQUIRED: DB commit 전 영상의 직접 삭제를 보류했습니다.'

    @classmethod
    def check_and_adopt_existing_video(cls, item_or_dict, category='JAV_CEN', reuse_gdrive=False, existing_item=None, reprocess=False, observations=None):
        """
        저장소(로컬 디렉토리)에 이미 프리뷰 클립 또는 트레일러 파일이 존재하면
        신규 다운로드나 인코딩 없이 메타데이터 extra_info로 자동 흡수
        """
        if reprocess:
            return False

        if not cls.is_enabled(category):
            return False

        storage_type = cls.get_setting("preview_storage_type", category, default="local")
        if storage_type == 'gdrive' and reuse_gdrive:
            from .util_metadata import MetaUtil
            from .util_runtime import StorageLookupError
            extra = dict((item_or_dict.get('extra_info') if isinstance(item_or_dict, dict) else item_or_dict.extra_info) or {})
            changed = False
            pending = {}
            for kind, key in (('trailer', 'trailer_video'), ('preview', 'preview_clip')):
                previous = (cls.get_stored_video(item_or_dict, kind, 'gdrive', category) or
                            cls.get_stored_video(existing_item or {}, kind, 'gdrive', category) or {})
                if previous.get('google_fileid') and previous.get('relative_path'):
                    if extra.get(key) != previous:
                        extra[key] = copy.deepcopy(previous)
                        changed = True
                    continue
                rel_dir, filename = cls.get_video_relative_path(item_or_dict, video_type=kind, category=category)
                relative_path = previous.get('relative_path') or '/'.join(filter(None, (rel_dir, filename)))
                # A literal work prefix is required; never broaden into a whole folder.
                prefix = filename[:-len('_' + kind + '.mp4')]
                if (not prefix or prefix in ('.', '..', 'unknown') or
                        re.search(r'[\x00-\x1f/\\:*?"<>|\[\]{}]', prefix)):
                    raise StorageLookupError('영상 작품 접두어를 확인할 수 없습니다.')
                relative_path = str(relative_path).replace('\\', '/')
                if (relative_path.startswith('/') or ':' in relative_path or
                        any(part in ('', '.', '..') for part in relative_path.split('/')) or '/' not in relative_path):
                    raise StorageLookupError('영상 상대 경로를 확인할 수 없습니다.')
                directory, name = relative_path.rsplit('/', 1)
                pending.setdefault((directory, prefix), []).append((kind, key, name, previous))
            observed = observations if observations is not None else {}
            for (directory, prefix), candidates in pending.items():
                lookup_key = (str(category).upper(), directory, prefix.lower())
                if lookup_key not in observed:
                    try:
                        observed[lookup_key] = MetaUtil.query_gdrive_remote_dir_files(
                            directory, category=category, code_lower=prefix, strict=True, is_image=False)
                    except StorageLookupError:
                        raise
                    except Exception as exc:
                        raise StorageLookupError('영상 폴더 조회를 완료하지 못했습니다.') from exc
                files = observed[lookup_key]
                actual_names = getattr(files, 'actual_names', {})
                names = [str(name).lower() for name in actual_names.values()]
                if len(names) != len(set(names)):
                    raise StorageLookupError('영상 폴더에 중복 파일명이 있습니다.')
                for kind, key, name, previous in candidates:
                    fid = files.get(name.lower())
                    actual_name = actual_names.get(str(fid), '') if fid else ''
                    if (not actual_name or '/' in actual_name or '\\' in actual_name or
                            actual_name.lower() != name.lower() or
                            not actual_name.lower().startswith(prefix.lower() + '_') or
                            not actual_name.lower().endswith('_' + kind + '.mp4')):
                        continue
                    actual_path = directory + '/' + actual_name
                    extra[key] = dict(previous, storage_type='gdrive', google_fileid=fid, local_path='',
                        relative_path=actual_path, filename=actual_name,
                        duration=previous.get('duration') or 0,
                        created_time=previous.get('created_time') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    changed = True
                    logger.info('[MetaPreview] 기존 GDrive %s 재사용: %s', kind, actual_path)
            if changed:
                if isinstance(item_or_dict, dict):
                    item_or_dict['extra_info'] = extra
                else:
                    item_or_dict.extra_info = extra
            return changed
        if storage_type != 'local':
            return False

        local_root = cls.get_setting("preview_local_path", category, default="/data/previews")
        if not os.path.exists(local_root):
            return False

        if isinstance(item_or_dict, dict):
            extra = item_or_dict.setdefault('extra_info', {})
        else:
            if not getattr(item_or_dict, 'extra_info', None):
                item_or_dict.extra_info = {}
            extra = item_or_dict.extra_info

        changed = False

        # 프리뷰 클립 자동 감지 및 흡수
        if not extra.get('preview_clip'):
            rel_dir, preview_filename = cls.get_video_relative_path(item_or_dict, video_type='preview', category=category)
            full_preview_path = os.path.join(local_root, rel_dir.replace('/', os.path.sep), preview_filename)
            if os.path.exists(full_preview_path) and os.path.getsize(full_preview_path) > 1024:
                ffprobe_bin = cls.get_ffprobe_path(category)
                dur = cls.get_video_duration(full_preview_path, ffprobe_bin)
                extra['preview_clip'] = {
                    'storage_type': 'local',
                    'filename': preview_filename,
                    'relative_path': f'{rel_dir}/{preview_filename}',
                    'duration': int(round(dur)) if dur else 60,
                    'created_time': datetime.fromtimestamp(os.path.getmtime(full_preview_path)).strftime('%Y-%m-%d %H:%M:%S'),
                    'local_path': full_preview_path,
                    'google_fileid': ''
                }
                changed = True
                logger.info(f"[MetaPreview] 기존 로컬 프리뷰 클립 자동 감지 및 흡수 완료: {full_preview_path}")

        # 트레일러 영상 자동 감지 및 흡수
        if not extra.get('trailer_video'):
            rel_dir, trailer_filename = cls.get_video_relative_path(item_or_dict, video_type='trailer', category=category)
            full_trailer_path = os.path.join(local_root, rel_dir.replace('/', os.path.sep), trailer_filename)
            if os.path.exists(full_trailer_path) and os.path.getsize(full_trailer_path) > 1024:
                ffprobe_bin = cls.get_ffprobe_path(category)
                dur = cls.get_video_duration(full_trailer_path, ffprobe_bin)
                extra['trailer_video'] = {
                    'storage_type': 'local',
                    'filename': trailer_filename,
                    'relative_path': f'{rel_dir}/{trailer_filename}',
                    'duration': int(round(dur)) if dur else 0,
                    'created_time': datetime.fromtimestamp(os.path.getmtime(full_trailer_path)).strftime('%Y-%m-%d %H:%M:%S'),
                    'local_path': full_trailer_path,
                    'google_fileid': ''
                }
                changed = True
                logger.info(f"[MetaPreview] 기존 로컬 트레일러 영상 자동 감지 및 흡수 완료: {full_trailer_path}")

        return changed

    @staticmethod
    def trailer_source_url(url):
        from urllib.parse import urlparse, parse_qs
        value = str(url or '').strip()
        for _ in range(3):
            parsed = urlparse(value)
            if '/metadata/normal/jav_video' not in parsed.path:
                break
            inner = (parse_qs(parsed.query).get('url') or [''])[0]
            if not inner or inner == value:
                break
            value = inner
        return value

    @classmethod
    def remove_official_trailer(cls, target, url):
        """Remove the failed download URL without recording or blocking it."""
        is_dict = isinstance(target, dict)
        extra = dict((target.get('extra_info') if is_dict else target.extra_info) or {})
        rejected = cls.trailer_source_url(url)
        if not rejected:
            return False
        extra.pop('unavailable_trailer', None)
        def matches(url):
            return bool(url and cls.trailer_source_url(url) == rejected)
        def strip(data):
            result = dict(data or {})
            for key in ('trailer', 'trailer_url'):
                if matches(result.get(key)):
                    result.pop(key, None)
            if isinstance(result.get('extras'), list):
                result['extras'] = [entry for entry in result['extras'] if not (
                    isinstance(entry, dict) and str(entry.get('content_type') or '').lower() == 'trailer'
                    and matches(entry.get('content_url')))]
            return result
        extra = strip(extra)
        original = strip(target.get('original') if is_dict else target.original)
        if is_dict:
            target.update(strip(target))
            for key in ('trailer', 'trailer_url'):
                if matches(target.get(key)):
                    target.pop(key, None)
            target.update(original=original, extra_info=extra)
        else:
            target.original, target.extra_info = original, extra
            for media in list(target.media_files):
                if media.media_type == 'trailer' and not media.gdrive_fileid and matches(media.url):
                    target.media_files.remove(media)
        return True

    @staticmethod
    def publish_local_video(source, destination):
        # Prepared source -> final name. Receipt owners have already staged old files.
        import uuid
        parent = os.path.dirname(os.path.abspath(destination))
        os.makedirs(parent, exist_ok=True)
        backup = None
        written = False
        if os.path.lexists(destination):
            backup = str(destination) + '.old_' + uuid.uuid4().hex
            os.rename(destination, backup)
        try:
            with open(source, 'rb') as src:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                written = True
                with os.fdopen(fd, 'wb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
        except Exception:
            if written and os.path.exists(destination):
                os.remove(destination)
            if backup:
                os.rename(backup, destination)
            raise
        if backup:
            os.remove(backup)

    @classmethod
    def process_trailer_download_workflow(cls, code, trailer_url, category='JAV_CEN', force=False, item_dict=None, use_dir_cache=False, video_observations=None, previous_video=None):
        """원격 트레일러 영상 다운로드 ➔ Rclone 업로드 또는 로컬 이동 ➔ 메타데이터 갱신 (디렉터리 캐시 지원)"""
        from .mod_meta_db import ModuleMetaDb, MetaItem

        if not force and not cls.is_trailer_download_enabled(category):
            return False, "트레일러 자동 다운로드 설정이 꺼져 있습니다."

        if not trailer_url or not isinstance(trailer_url, str) or not trailer_url.startswith(('http://', 'https://')):
            return False, "유효한 원격 트레일러 URL이 아닙니다."

        std_cat = str(category or 'JAV_CEN').upper()
        sess = None
        item = None

        # 메모리 딕셔너리가 전달되지 않은 경우(배치 작업 등)에만 DB 세션 조회
        if item_dict is None:
            sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
            if not sess:
                return False, "DB 세션 획득 실패"
            for _ in range(3):
                item = sess.query(MetaItem).filter_by(code=code).first()
                if item:
                    break
                time.sleep(0.3)

            if not item:
                sess.remove()
                logger.warning(f"[MetaPreview] DB에 해당 작품 메타데이터가 존재하지 않아 작업을 중단합니다: {code}")
                return False, f"작품 메타데이터를 찾을 수 없습니다: {code}"

        replacement_entry = None
        temp_output = None
        workflow_success = False
        media_commit = {'state': 'not_attempted'}

        try:
            target_obj = item_dict if item_dict is not None else item
            extra = target_obj.get('extra_info', {}) if isinstance(target_obj, dict) else dict(target_obj.extra_info or {})
            storage_type = cls.get_setting("preview_storage_type", std_cat, default="local")
            existing_trailer = cls.get_stored_video(target_obj, 'trailer', storage_type, std_cat)
            rel_dir, trailer_filename = cls.get_video_relative_path(target_obj, video_type='trailer', category=std_cat)
            dest_rel_full_path = f"{rel_dir}/{trailer_filename}"
            previous = previous_video or existing_trailer or {}
            if not force and previous.get('storage_type') == storage_type and previous.get('relative_path'):
                recorded = previous['relative_path'].replace('\\', '/')
                if recorded.startswith('/') or ':' in recorded or '..' in recorded.split('/'):
                    raise ValueError('Invalid recorded video path')
                dest_rel_full_path = recorded
                trailer_filename = recorded.rsplit('/', 1)[-1]

            # 로컬 저장 모드일 때 이미 해당 파일이 있으면 다운로드 생략 및 흡수
            if storage_type == 'local':
                local_root = cls.get_setting("preview_local_path", std_cat, default="/data/previews")
                final_local_file = os.path.join(local_root, dest_rel_full_path.replace('/', os.path.sep))
                has_file = cls.has_local_file(final_local_file, use_cache=use_dir_cache)
                if not force and has_file and os.path.exists(final_local_file) and os.path.getsize(final_local_file) > 1024:
                    cls.register_local_file(final_local_file)
                    duration = cls.get_video_duration(final_local_file, cls.get_ffprobe_path(std_cat))
                    extra['trailer_video'] = dict(storage_type='local', local_path=final_local_file,
                        relative_path=dest_rel_full_path, filename=trailer_filename, google_fileid='',
                        duration=int(round(duration or 0)), created_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    if isinstance(target_obj, dict):
                        target_obj['extra_info'] = extra
                    else:
                        target_obj.extra_info = extra
                    if sess:
                        ModuleMetaDb.sync_item_video_assets(target_obj)
                        sess.commit()
                    return True, extra.get('trailer_video')

            observation = None
            if storage_type == 'gdrive':
                if not force and existing_trailer and existing_trailer.get('google_fileid') and existing_trailer.get('relative_path'):
                    return True,existing_trailer
                from .util_metadata import MetaUtil
                prepared=next((e for e in extra.get('_media_replacements',[]) if e.get('media_type')=='trailer'
                    and e.get('relative_path')==dest_rel_full_path and e.get('storage')=='gdrive'),None)
                if prepared is None:
                    directory,name=dest_rel_full_path.rsplit('/',1)
                    files=None
                    for key,value in (video_observations or {}).items():
                        if (key[0]==std_cat and key[1]==directory and name.lower().startswith(key[2].lower()+'_')
                                and getattr(value,'listing_complete',False)):
                            files=value;break
                    if files is None:files=MetaUtil.query_gdrive_remote_dir_files(directory,std_cat,strict=True,is_image=False)
                    initial=dict(directory=directory,files=[dict(Name=getattr(files,'actual_names',{}).get(str(fid),n),ID=fid,IsDir=False) for n,fid in files.items()])
                    fid=files.get(name.lower())
                    if not force and fid:
                        extra['trailer_video']=dict(storage_type='gdrive',relative_path=dest_rel_full_path,filename=name,google_fileid=fid,local_path='')
                        if isinstance(target_obj,dict):target_obj['extra_info']=extra
                        else:target_obj.extra_info=extra
                        if sess:
                            ModuleMetaDb.sync_item_video_assets(target_obj);sess.commit()
                        return True,extra['trailer_video']
                    replacement_entry=MetaUtil.prepare_video_replacement(target_obj,std_cat,dest_rel_full_path,
                        observation={'initial_observation':initial},previous=previous,prebackup=True)
                else:replacement_entry=prepared
                MediaReplacement.stage(replacement_entry)
                if not isinstance(target_obj,dict):
                    pending_extra=dict(target_obj.extra_info or {});pending_extra.pop('_media_replacements',None);target_obj.extra_info=pending_extra

            tmp_dir = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            fd, temp_output = tempfile.mkstemp(prefix='trailer_', suffix='.mp4', dir=tmp_dir)
            os.close(fd)

            # 출처 사이트 식별 및 프록시 엔드포인트 결정
            site_raw = target_obj.get('site') if isinstance(target_obj, dict) else getattr(target_obj, 'site', '')
            site_key = (site_raw or '').strip().lower()
            from urllib.parse import quote_plus
            endpoint = 'jav_video_un' if std_cat == 'JAV_UNCEN' else 'jav_video'
            proxy_route_path = '/%s/normal/%s?site=%s&url=%s' % (
                P.package_name, endpoint, quote_plus(site_key), quote_plus(trailer_url))
            logger.info('[MetaPreview] FF 영상 프록시 다운로드 시작: %s [%s/%s]', code, std_cat, site_key or '미지정')
            download_success = False
            last_status_code = None
            source_failure = None
            def read_proxy_response(response, internal):
                nonlocal last_status_code, source_failure
                last_status_code = response.status_code
                source_failure = response.headers.get('X-Metadata-Trailer-Error')
                content_type = str(response.headers.get('Content-Type') or '').lower()
                if last_status_code not in (200, 206):
                    return False
                if any(t in content_type for t in ('text/html', 'text/plain', 'application/json')):
                    source_failure = 'invalid_content'
                    return False
                if internal:
                    chunks = response.iter_encoded() if hasattr(response, 'iter_encoded') else (
                        response.response if hasattr(response, 'response') else [response.data])
                else:
                    chunks = response.iter_content(chunk_size=65536)
                with open(temp_output, 'wb') as fp:
                    for chunk in chunks:
                        if chunk:
                            fp.write(chunk)
                return os.path.getsize(temp_output) > 1024
            try:
                with F.app.test_client() as client:
                    response = client.get(proxy_route_path, follow_redirects=True)
                    try:
                        download_success = read_proxy_response(response, True)
                    finally:
                        response.close()
            except Exception:
                source_failure = None
                logger.debug('[MetaPreview] FF 내부 영상 프록시 호출 실패: %s [%s]', code, std_cat, exc_info=True)

            if not download_success and source_failure not in ('not_found', 'invalid_content', 'tls_error'):
                ddns = (F.SystemModelSetting.get('ddns') or '').rstrip('/')
                if ddns:
                    response = None
                    try:
                        response = requests.get(ddns + proxy_route_path, stream=True, timeout=(10, 90), verify=False)
                        download_success = read_proxy_response(response, False)
                    except Exception:
                        source_failure = None
                        logger.exception('[MetaPreview] DDNS FF 영상 프록시 다운로드 실패: %s [%s]', code, std_cat)
                    finally:
                        if response is not None:
                            response.close()

            if not download_success or not os.path.exists(temp_output) or os.path.getsize(temp_output) < 1024:
                cls.remove_official_trailer(target_obj, trailer_url)
                if sess:
                    sess.commit()
                status = 'HTTP %s' % last_status_code if last_status_code is not None else 'HTTP 응답 확인 불가'
                logger.warning('[MetaPreview] FF 트레일러 다운로드 실패: %s [%s/%s] %s %s; 공식 URL %s',
                    code, std_cat, site_key or '미지정', status, source_failure or '영상 응답 미확인', '제거')
                return False, '트레일러 FF 프록시 다운로드 실패 (%s, %s)' % (status, source_failure or '영상 응답 미확인')

            ffprobe_bin = cls.get_ffprobe_path(std_cat)
            dur = cls.get_video_duration(temp_output, ffprobe_bin)
            if replacement_entry is not None and replacement_entry.get('retired_prebackup'):
                import math
                if not isinstance(dur, (int, float)) or not math.isfinite(dur) or dur <= 0:
                    raise ValueError('트레일러 ffprobe 유효성 확인 실패')

            from .util_metadata import MetaUtil
            from .util_runtime import StorageConflict
            prepared = replacement_entry or next((e for e in extra.get('_media_replacements', [])
                if e.get('media_type') == 'trailer' and e.get('relative_path') == dest_rel_full_path
                and e.get('storage') == storage_type), None)
            if prepared is not None:
                if prepared['status'] != 'pending':
                    raise StorageConflict('이미 진행된 현재 트레일러 교체 정보입니다.')
                replacement_entry = prepared
                MediaReplacement.stage(replacement_entry)
            else:
                replacement_entry = MetaUtil.prepare_video_replacement(target_obj, std_cat, dest_rel_full_path,
                    observation=observation, previous=previous if force else None)

            trailer_meta = {
                'storage_type': storage_type,
                'filename': trailer_filename,
                'relative_path': dest_rel_full_path,
                'duration': int(round(dur)) if dur else 0,
                'created_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            if storage_type == 'gdrive':
                file_id, err = cls.upload_to_gdrive(temp_output, dest_rel_full_path, category=std_cat, prechecked=replacement_entry is not None, replacement=replacement_entry)
                if not file_id:
                    return False, f"구글 드라이브 업로드 실패: {err}"

                cls.register_remote_file(dest_rel_full_path, file_id, category=std_cat, is_image=False)
                trailer_meta['google_fileid'] = file_id
                trailer_meta['local_path'] = ''
            else:
                local_root = cls.get_setting("preview_local_path", std_cat, default="/data/previews")
                final_local_file = os.path.join(local_root, dest_rel_full_path.replace('/', os.path.sep))
                os.makedirs(os.path.dirname(final_local_file), exist_ok=True)
                cls.publish_local_video(temp_output, final_local_file)
                cls.register_local_file(final_local_file)
                trailer_meta['local_path'] = final_local_file
                trailer_meta['google_fileid'] = ''

            # 인메모리 객체 또는 DB 레코드에 반영
            if replacement_entry is not None and storage_type != 'gdrive':
                MediaReplacement.generated(replacement_entry, trailer_meta.get('google_fileid'))
            extra['trailer_video'] = trailer_meta
            extra.pop('unavailable_trailer', None)
            if isinstance(target_obj, dict):
                target_obj['extra_info'] = extra
            else:
                extra.pop('_media_replacements', None)
                target_obj.extra_info = extra
                if sess:
                    ModuleMetaDb.sync_item_video_assets(target_obj)
                    if replacement_entry is not None and replacement_entry.get('retired_prebackup'):
                        from sqlalchemy import event
                        actual_session = sess() if hasattr(sess, 'registry') else sess
                        def committed(session):
                            media_commit['state'] = 'committed'
                        event.listen(actual_session, 'after_commit', committed, once=True)
                        media_commit['state'] = 'unknown'
                    sess.commit()
                    if replacement_entry is not None and replacement_entry.get('retired_prebackup'):
                        media_commit['state'] = 'committed'
                    MetaUtil.finish_media_replacements([replacement_entry], True)

            workflow_success = True
            emit = logger.debug if storage_type == 'gdrive' else logger.info
            emit(f"[MetaPreview] [{std_cat}] {code} 트레일러 영상 저장 완료 ({storage_type}) -> {trailer_filename}")
            return True, trailer_meta

        except Exception as e:
            if sess:
                sess.rollback()
            logger.exception('[MetaPreview] 트레일러 준비/저장 처리 실패: %s [%s]', code, std_cat)
            return False, '트레일러 다운로드 중 오류가 발생했습니다.'
        finally:
            if replacement_entry is not None and not workflow_success:
                from .util_metadata import MetaUtil
                MetaUtil.finish_media_replacements([replacement_entry],
                    None if media_commit['state'] == 'unknown' else media_commit['state'] == 'committed')
            if sess:
                sess.remove()
            if temp_output and os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except Exception:
                    pass

    @classmethod
    def process_preview_workflow(cls, code, source_video_path, category='JAV_CEN', force=False, item_dict=None):
        """기존 파일 삭제 ➔ FFmpeg 몽타주 추출 ➔ Rclone 업로드/로컬 이동 ➔ 메타데이터(메모리 또는 DB) 갱신"""
        from .mod_meta_db import ModuleMetaDb, MetaItem

        std_cat = str(category or 'JAV_CEN').upper()
        sess = None
        item = None

        if item_dict is None:
            sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
            if not sess:
                return False, "DB 세션 획득 실패"
            for _ in range(3):
                item = sess.query(MetaItem).filter_by(code=code).first()
                if item:
                    break
                time.sleep(0.3)

            if not item:
                sess.remove()
                return False, f"작품 메타데이터를 찾을 수 없습니다: {code}"

        replacement_entry = None
        temp_output = None
        workflow_success = False
        media_commit = {'state': 'not_attempted'}

        try:
            target_obj = item_dict if item_dict is not None else item
            extra = target_obj.get('extra_info', {}) if isinstance(target_obj, dict) else dict(target_obj.extra_info or {})
            storage_type = cls.get_setting("preview_storage_type", std_cat, default="local")
            existing_clip = cls.get_stored_video(target_obj, 'preview', storage_type, std_cat)
            rel_dir, clip_filename = cls.get_video_relative_path(target_obj, video_type='preview', category=std_cat)
            dest_rel_full_path = f"{rel_dir}/{clip_filename}"
            previous = existing_clip or {}
            if not force and previous.get('storage_type') == storage_type and previous.get('relative_path'):
                recorded = previous['relative_path'].replace('\\', '/')
                if recorded.startswith('/') or ':' in recorded or '..' in recorded.split('/'):
                    raise ValueError('Invalid recorded video path')
                dest_rel_full_path = recorded
                clip_filename = recorded.rsplit('/', 1)[-1]

            if existing_clip and not force:
                return True, existing_clip

            tmp_dir = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            temp_output = os.path.join(tmp_dir, clip_filename)

            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except Exception:
                    pass

            success, msg = cls.generate_preview_clip(source_video_path, temp_output, category=std_cat)
            if not success:
                return False, msg

            from .util_metadata import MetaUtil
            replacement_entry = MetaUtil.prepare_video_replacement(target_obj, std_cat, dest_rel_full_path,
                video_type='preview', replace_existing_preview=bool(force))

            clip_meta = {
                'storage_type': storage_type,
                'filename': clip_filename,
                'relative_path': dest_rel_full_path,
                'duration': int(cls.get_setting("preview_duration", std_cat, default="60")),
                'created_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            if storage_type == 'gdrive':
                file_id, err = cls.upload_to_gdrive(temp_output, dest_rel_full_path, category=std_cat, prechecked=replacement_entry is not None, replacement=replacement_entry)
                if not file_id:
                    return False, f"구글 드라이브 업로드 실패: {err}"
                clip_meta['google_fileid'] = file_id
                clip_meta['local_path'] = ''
            else:
                local_root = cls.get_setting("preview_local_path", std_cat, default="/data/previews")
                final_local_file = os.path.join(local_root, dest_rel_full_path.replace('/', os.path.sep))
                os.makedirs(os.path.dirname(final_local_file), exist_ok=True)
                cls.publish_local_video(temp_output, final_local_file)
                clip_meta['local_path'] = final_local_file
                clip_meta['google_fileid'] = ''

            extra['preview_clip'] = clip_meta
            extra.pop('source_video_path', None)
            if replacement_entry is not None and storage_type != 'gdrive':
                MediaReplacement.generated(replacement_entry, clip_meta.get('google_fileid'))

            if isinstance(target_obj, dict):
                target_obj['extra_info'] = extra
            else:
                extra.pop('_media_replacements', None)
                target_obj.extra_info = extra
                if sess:
                    ModuleMetaDb.sync_item_video_assets(target_obj)
                    if replacement_entry is not None:
                        from sqlalchemy import event
                        actual_session = sess() if hasattr(sess, 'registry') else sess
                        def committed(session):
                            media_commit['state'] = 'committed'
                        event.listen(actual_session, 'after_commit', committed, once=True)
                        media_commit['state'] = 'unknown'
                    sess.commit()
                    media_commit['state'] = 'committed'
                    if replacement_entry is not None:
                        MetaUtil.finish_media_replacements([replacement_entry], True)

            workflow_success = True
            logger.info(f"[MetaPreview] [{std_cat}] {code} 프리뷰 클립 등록 완료 ({storage_type}) -> {clip_filename}")
            return True, clip_meta

        except Exception as e:
            if sess:
                sess.rollback()
            logger.exception('[MetaPreview] 프리뷰 워크플로우 예외: code=%s', code)
            return False, '프리뷰 처리 중 오류가 발생했습니다.'
        finally:
            if replacement_entry is not None and not workflow_success:
                from .util_metadata import MetaUtil
                MetaUtil.finish_media_replacements([replacement_entry],
                    None if media_commit['state'] == 'unknown' else media_commit['state'] == 'committed')
            if sess:
                sess.remove()
            if temp_output and os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except Exception:
                    pass


    @classmethod
    def _b64url(cls, b_data):
        if isinstance(b_data, str):
            b_data = b_data.encode('utf-8')
        return base64.urlsafe_b64encode(b_data).decode('utf-8').rstrip('=')

    @classmethod
    def _generate_sa_jwt(cls, sa_info, subject_email=None):
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": sa_info.get("client_email"),
            "scope": "https://www.googleapis.com/auth/drive",
            "aud": sa_info.get("token_uri", "https://oauth2.googleapis.com/token"),
            "iat": now,
            "exp": now + 3600
        }
        if subject_email:
            claims["sub"] = subject_email

        header_b64 = cls._b64url(json.dumps(header))
        claims_b64 = cls._b64url(json.dumps(claims))
        payload = f"{header_b64}.{claims_b64}".encode('utf-8')

        signature_bytes = None
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            key = load_pem_private_key(sa_info["private_key"].encode('utf-8'), password=None)
            signature_bytes = key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        except ImportError:
            with tempfile.NamedTemporaryFile('w', delete=False) as tf:
                tf.write(sa_info["private_key"])
                temp_key_path = tf.name
            try:
                p = subprocess.run(
                    ['openssl', 'dgst', '-sha256', '-sign', temp_key_path],
                    input=payload,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True
                )
                signature_bytes = p.stdout
            finally:
                if os.path.exists(temp_key_path):
                    os.remove(temp_key_path)

        if not signature_bytes:
            raise Exception("RSA 서명 생성 실패 (cryptography 및 openssl 모두 불가)")

        sig_b64 = cls._b64url(signature_bytes)
        return f"{header_b64}.{claims_b64}.{sig_b64}"

    @classmethod
    def get_gdrive_access_token(cls, rclone_conf_path, remote_name, file_id=None):
        now = time.time()
        if not rclone_conf_path or not os.path.exists(rclone_conf_path):
            logger.error(f"[MetaPreview] rclone.conf 파일이 없습니다: {rclone_conf_path}")
            return None

        try:
            config = configparser.ConfigParser()
            config.read(rclone_conf_path, encoding='utf-8')
            if remote_name not in config:
                logger.error(f"[MetaPreview] rclone.conf 내에 리모트 '{remote_name}' 섹션이 없습니다.")
                return None

            section = config[remote_name]

            # 서비스 계정(Service Account) 및 권한 위임(Impersonate) 처리
            sa_file = section.get('service_account_file', '').strip()
            if sa_file:
                if not os.path.isabs(sa_file):
                    sa_file = os.path.normpath(os.path.join(os.path.dirname(rclone_conf_path), sa_file))

                if not os.path.exists(sa_file):
                    logger.error(f"[MetaPreview] 서비스 어카운트 키 파일을 찾을 수 없습니다: '{sa_file}'")
                    return None

                with open(sa_file, 'r', encoding='utf-8') as f:
                    sa_info = json.load(f)

                # impersonate_list 또는 impersonate 단일 계정 파싱
                raw_list = section.get('impersonate_list', '').strip()
                impersonate_users = []
                if raw_list:
                    try:
                        parsed = json.loads(raw_list)
                        if isinstance(parsed, list):
                            impersonate_users = [str(x).strip() for x in parsed if str(x).strip()]
                    except Exception:
                        impersonate_users = re.findall(r'[\w\.-]+@[\w\.-]+', raw_list)

                if not impersonate_users and section.get('impersonate'):
                    impersonate_users = [section.get('impersonate').strip()]

                # 위임 대상 사용자 선택 (동일 영상/파일은 해시 바인딩하여 단일 세션 유지, 그 외 순차 로테이션)
                target_user = None
                total_users = len(impersonate_users)
                user_seq = 0
                if impersonate_users:
                    if file_id:
                        import zlib
                        user_idx = zlib.crc32(str(file_id).encode('utf-8')) % total_users
                        target_user = impersonate_users[user_idx]
                        user_seq = user_idx + 1
                    else:
                        with cls._rotation_lock:
                            curr_idx = cls._impersonate_indices.get(remote_name, 0)
                            target_user = impersonate_users[curr_idx % total_users]
                            cls._impersonate_indices[remote_name] = (curr_idx + 1) % total_users
                            user_seq = (curr_idx % total_users) + 1

                cache_key = f"{remote_name}:{target_user}" if target_user else f"{remote_name}:sa"

                # 위임 계정별 토큰 캐시 확인 (만료 5분 전까지 재사용)
                if cache_key in cls._token_cache:
                    cached_token, expire_at = cls._token_cache[cache_key]
                    if now < (expire_at - 300):
                        if target_user:
                            logger.debug(f"[MetaPreview] 서비스 계정 인증 토큰 재사용: {remote_name}")
                        return cached_token

                # JWT 어서션 생성 및 OAuth2 access_token 발급 요청
                jwt_assertion = cls._generate_sa_jwt(sa_info, subject_email=target_user)
                token_uri = sa_info.get('token_uri', 'https://oauth2.googleapis.com/token')
                payload = {
                    'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                    'assertion': jwt_assertion
                }

                res = requests.post(token_uri, data=payload, timeout=10)
                if res.status_code == 200:
                    token_data = res.json()
                    access_token = token_data.get('access_token')
                    expires_in = int(token_data.get('expires_in', 3600))
                    cls._token_cache[cache_key] = (access_token, now + expires_in)

                    if target_user:
                        logger.debug(f"[MetaPreview] 위임 서비스 계정 인증 토큰 갱신 완료: {remote_name} (유효기간 {expires_in}초)")
                    else:
                        logger.debug(f"[MetaPreview] 서비스 계정 인증 토큰 갱신 완료: '{remote_name}' (유효: {expires_in}초)")
                    return access_token
                else:
                    logger.error(f"[MetaPreview] 서비스 계정 인증 토큰 요청 실패 (HTTP {res.status_code})")
                    return None

            # 일반 OAuth2 리프레시 토큰(refresh_token) 방식 처리
            client_id = section.get('client_id', '')
            client_secret = section.get('client_secret', '')
            token_json_raw = section.get('token', '')

            refresh_token = ''
            if token_json_raw:
                try:
                    token_dict = json.loads(token_json_raw)
                    refresh_token = token_dict.get('refresh_token', '')
                except Exception:
                    pass

            if not refresh_token:
                logger.error(f"[MetaPreview] 저장소 {remote_name}: 서비스 계정 또는 갱신용 인증 정보를 찾지 못했습니다.")
                return None

            if remote_name in cls._token_cache:
                token, expire_at = cls._token_cache[remote_name]
                if now < (expire_at - 300):
                    return token

            token_url = "https://oauth2.googleapis.com/token"
            payload = {
                'client_id': client_id,
                'client_secret': client_secret,
                'refresh_token': refresh_token,
                'grant_type': 'refresh_token'
            }

            res = requests.post(token_url, data=payload, timeout=10)
            if res.status_code == 200:
                res_data = res.json()
                access_token = res_data.get('access_token')
                expires_in = int(res_data.get('expires_in', 3600))
                cls._token_cache[remote_name] = (access_token, now + expires_in)
                logger.debug(f"[MetaPreview] Google OAuth2 access_token 갱신 성공 (리모트: {remote_name}, 유효: {expires_in}초)")
                return access_token
            else:
                logger.error(f"[MetaPreview] Google OAuth2 토큰 갱신 실패 (HTTP {res.status_code}): {res.text[:200]}")
                return None

        except Exception as e:
            logger.error(f"[MetaPreview] OAuth 토큰 요청 예외: {e}")
            logger.error(traceback.format_exc())
            return None


class MediaReplacement:


    @staticmethod
    def command(entry, operation, *paths):
        result = subprocess.run([entry['rclone'], '--config', entry['config'], operation, *paths],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=40)
        if result.returncode:
            raise RuntimeError('rclone %s failed (rc=%s): %s' % (operation, result.returncode, result.stderr))
        return result.stdout

    @classmethod
    def exists(cls, entry, path):
        if entry['storage'] == 'local':
            return os.path.isfile(path)
        item = MetaPreviewUtil.remote_stat([entry['rclone'], '--config', entry['config'], 'lsjson', '--stat', path],
            category=entry.get('category', ''), purpose='backup_probe' if path == entry.get('backup') else 'replacement')
        if item is not None and 'status' not in entry and path == entry.get('target'):
            MetaPreviewUtil.validate_remote_media(item)
            entry['old_id'] = item.get('ID') or item.get('id')
        return item is not None

    @classmethod
    def move(cls, entry, source, destination):
        if entry['storage'] == 'local':
            os.replace(source, destination)
        else:
            cls.command(entry, 'moveto', source, destination)

    @classmethod
    def delete(cls, entry, path):
        if entry['storage'] == 'local':
            if os.path.exists(path):
                os.remove(path)
                return True
            return False
        else:
            try:
                cls.command(entry, 'deletefile', path)
                return True
            except RuntimeError as exc:
                if 'not found' not in str(exc).lower() and 'does not exist' not in str(exc).lower():
                    raise
                return False

    @classmethod
    def begin(cls, spec, exists=False):
        basename = spec['relative_path'].replace('\\', '/').rsplit('/', 1)[-1].lower()
        manual_image = (spec.get('manual_user_image') is True and isinstance(spec.get('user_plan'), dict)
                        and spec.get('subject_type', 'item') == 'item'
                        and spec['media_type'] in ('poster', 'landscape') and not exists)
        manual_image = manual_image or (spec.get('image_contract')==1 and spec.get('explicit_user_image') is True
            and isinstance(spec.get('user_plan'),dict) and spec.get('image_plan',{}).get('relative_path')==spec['relative_path']
            and spec['user_plan'].get('code')==spec.get('code') and spec['user_plan'].get('category')==spec.get('category')
            and spec['user_plan'].get('canonical_rel')==spec['relative_path']
            and spec['media_type'] in ('poster','landscape'))
        manual_image = manual_image or (spec.get('image_contract')==1 and spec.get('subject_type')=='person'
            and spec.get('image_plan',{}).get('person_custom') and spec.get('person_id') is not None
            and spec.get('person_idx') and spec.get('person_domain') in ('JAV','WESTERN'))
        if ('_user.' in basename and not manual_image) or (
                spec['media_type'] == 'preview' and exists and not spec.get('replace_existing_preview')):
            raise ValueError('Protected media cannot be staged for replacement')
        identifier = uuid.uuid4().hex
        entry = dict(spec, identifier=identifier,
                     backup=spec.get('original_target', spec['target']) + '.old_' + identifier if exists else '',
                     had_original=bool(exists), status='pending', staged=False, new_id='')
        if entry.get('retired_prebackup'):
            name = entry['retired_video']['relative_path'].rsplit('/', 1)[-1]
            entry.update(retired_original_name=name, retired_backup_name=name + '.old_' + identifier)
        return entry

    @classmethod
    def stage(cls, entry):
        if entry.get('staged'):
            return
        if entry.get('image_contract'):
            return cls.image_stage(entry)
        if entry.get('initial_observation') and entry.get('media_type') in ('trailer', 'preview'):
            from .util_metadata import MetaUtil
            if entry.get('retired_prebackup'):
                MetaUtil.prebackup_registered_video(entry)
        elif entry['backup']:
            if cls.exists(entry, entry['backup']):
                from .util_runtime import StorageConflict
                raise StorageConflict('백업 이름에 이미 파일이 있습니다.')
            if entry['storage'] == 'gdrive':
                entry['stage_started'] = True
            cls.move(entry, entry['target'], entry['backup'])
            entry['stage_started'] = True
            logger.debug('[MediaReplacement] 원본 백업 이동 완료: %s', entry['relative_path'])
        entry['staged'] = True

    @classmethod
    def generated(cls, entry, file_id='', *, expected_new_id=None, observation=None):
        if entry.get('image_contract'):
            return cls.image_generated(entry, file_id, observation)
        if entry.get('initial_observation'):
            cls.validate_upload_observation(entry, file_id, observation)
        entry.update(status='generated', new_id=file_id or '')
        if entry['storage'] == 'local':
            entry['new_file'] = os.path.isfile(entry['target'])

    @classmethod
    def removed(cls, entry):
        entry['status'] = 'removed'

    @classmethod
    def image_settings(cls, entry):
        from .util_runtime import StorageConflict
        if not entry.get('image_contract'):
            plan=entry['user_plan']
            current=P.ModelSetting.get(plan['module']+'_image_mode') or 'image_server'
            if current!=plan.get('configured_mode',plan['mode']) or P.ModelSetting.get('jav_censored_image_gdrive_remote_path')!=plan['remote_root']:
                raise StorageConflict('구 이미지 저장 방식/루트가 변경되어 보존합니다.')
            if MetaPreviewUtil.get_setting('rclone_conf',plan['category'],default='/data/db/rclone.conf')!=plan['config']:
                raise StorageConflict('구 이미지 리모트 설정 변경으로 보존합니다.')
            return
        plan=entry['image_plan'];module={'WESTERN':'western','JAV_UNCEN':'jav_uncensored','JAV_CEN':'jav_censored'}[entry['category']]
        current=P.ModelSetting.get(module+'_image_mode') or ('image_server' if entry.get('explicit_user_image') else P.ModelSetting.get('jav_censored_image_mode') or 'ff_proxy')
        if plan.get('person_custom'):current=P.ModelSetting.get(module+'_actor_image_mode') or 'gds'
        if current!=plan['configured_mode']:raise StorageConflict('이미지 저장 방식이 변경되어 작업을 보류합니다.')
        root=(P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or '') if entry['storage']=='gdrive' else os.path.abspath(P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data,'images'))
        if root!=plan['root']:raise StorageConflict('이미지 저장 루트가 변경되어 작업을 보류합니다.')
        if entry['storage']=='gdrive' and MetaPreviewUtil.get_setting('rclone_conf',entry['category'],default='/data/db/rclone.conf')!=entry['config']:
            raise StorageConflict('이미지 리모트 설정이 변경되어 작업을 보류합니다.')

    @classmethod
    def image_identity(cls, entry, file_id, expected_name=None, parent=None):
        """Read-only metadata. Mutation callers must additionally prove name, parent and DB state."""
        import requests
        from .util_runtime import StorageConflict
        plan=entry.get('image_plan') or {'root':entry['user_plan']['remote_root']}
        config=entry.get('config') or entry['user_plan']['config']
        if not re.fullmatch(r'[A-Za-z0-9_-]+',file_id or ''):raise StorageConflict('이미지 File ID 형식 오류')
        token=MetaPreviewUtil.get_gdrive_access_token(config,plan['root'].split(':',1)[0],file_id=file_id)
        if not token:raise StorageConflict('이미지 저장소 인증 확인 실패')
        response=requests.get('https://www.googleapis.com/drive/v3/files/'+file_id,
            params={'fields':'id,name,parents,trashed','supportsAllDrives':'true'},headers={'Authorization':'Bearer '+token},timeout=20)
        try:
            value=response.json() if response.status_code==200 else {}
            if (value.get('id')!=file_id or value.get('trashed') is not False or not isinstance(value.get('name'),str)
                    or not isinstance(value.get('parents'),list) or len(value['parents'])!=1):raise StorageConflict('이미지 ID/이름/부모 상태 확인 실패')
            if expected_name is not None and value['name']!=expected_name:raise StorageConflict('이미지 실제 파일명이 다릅니다.')
            if parent is not None and value['parents']!=[parent]:raise StorageConflict('이미지 실제 부모가 다릅니다.')
            return value
        finally:response.close()

    @classmethod
    def validate_upload_observation(cls, entry, file_id, observation):
        from .util_runtime import StorageConflict
        old_id=(entry.get('image_plan',{}).get('old') or {}).get('fid') or (entry.get('retired_video') or {}).get('file_id') or entry.get('original_id')
        if (not re.fullmatch(r'[A-Za-z0-9_-]+',file_id or '') or file_id==old_id or
                not isinstance(observation,dict) or observation.get('Name')!=os.path.basename(entry['relative_path']) or
                (observation.get('ID') or observation.get('id'))!=file_id):
            raise StorageConflict('업로드 신규 이름/ID 확인 실패')
        MetaPreviewUtil.validate_remote_media(observation)
        if any(row['ID']==file_id for row in entry['initial_observation']['files']):
            raise StorageConflict('업로드 결과가 최초 관측의 기존 ID를 재사용합니다.')

    @classmethod
    def mutate_observed_file(cls, entry, file_id, current_name, root, new_name=None):
        """ID-only mutation; the operation's first observation authorizes normal work."""
        import requests
        from .util_runtime import StorageConflict
        if not re.fullmatch(r'[A-Za-z0-9_-]+',file_id or ''):raise StorageConflict('파일 ID 형식 오류')
        token=MetaPreviewUtil.get_gdrive_access_token(entry['config'],root.split(':',1)[0],file_id=file_id)
        if not token:raise StorageConflict('저장소 변경 인증 확인 실패')
        url='https://www.googleapis.com/drive/v3/files/'+file_id
        options=dict(params={'supportsAllDrives':'true'},headers={'Authorization':'Bearer '+token},timeout=20)
        if new_name is not None:
            options.update(json={'name':new_name});options['params']['fields']='id,name,trashed'
        response=(requests.patch if new_name is not None else requests.delete)(url,**options)
        try:
            if new_name is None:
                if response.status_code not in (200,204):raise StorageConflict('ID 삭제 결과 불명')
            else:
                value=response.json() if response.status_code==200 else {}
                if value.get('id')!=file_id or value.get('name')!=new_name or value.get('trashed') is not False:
                    raise StorageConflict('ID 이름 변경 결과 불명')
                return value
        finally:response.close()

    @classmethod
    def image_mutate(cls, entry, file_id, current_name, parent, new_name=None):
        import requests
        from .util_runtime import StorageConflict
        cls.image_settings(entry)
        if entry.get('initial_observation'):
            return cls.mutate_observed_file(entry,file_id,current_name,entry['image_plan']['root'],new_name)
        cls.image_identity(entry,file_id,current_name,parent)
        root=entry['image_plan']['root'] if entry.get('image_contract') else entry['user_plan']['remote_root']
        config=entry.get('config') or entry['user_plan']['config']
        token=MetaPreviewUtil.get_gdrive_access_token(config,root.split(':',1)[0],file_id=file_id)
        if not token:raise StorageConflict('이미지 변경 인증 확인 실패')
        kwargs=dict(params={'supportsAllDrives':'true'},headers={'Authorization':'Bearer '+token},timeout=20)
        if new_name is not None:kwargs.update(json={'name':new_name});kwargs['params']['fields']='id,name,parents,trashed'
        response=(requests.patch if new_name is not None else requests.delete)('https://www.googleapis.com/drive/v3/files/'+file_id,**kwargs)
        try:
            if new_name is None:
                if response.status_code not in (200,204):raise StorageConflict('이미지 삭제 결과 불명')
            else:
                value=response.json() if response.status_code==200 else {}
                if value.get('id')!=file_id or value.get('name')!=new_name or value.get('parents')!=[parent] or value.get('trashed') is not False:raise StorageConflict('이미지 이름 변경 결과 불명')
                return value
        finally:response.close()

    @classmethod
    def image_stage(cls, entry):
        from .util_metadata import MetaUtil
        from .util_runtime import StorageConflict
        plan = entry['image_plan']; old = plan['old']
        cls.image_settings(entry)
        if entry.get('staged'):
            return
        if entry['storage'] == 'gdrive' and 'system_candidates' in plan:
            for index, row in enumerate(plan['system_candidates']):
                row['backup_name'] = row['Name'] + '.old_' + entry['identifier'] + '_' + str(index)
                if any(value['Name'].casefold() == row['backup_name'].casefold() for value in entry['initial_observation']['files']):
                    raise StorageConflict('이미지 백업 이름이 이미 존재하여 보존합니다.')
                entry['stage_started'] = True
                cls.image_mutate(entry, row['ID'], row['Name'], None, row['backup_name'])
                row['staged'] = True
            entry['staged'] = True
            return
        if entry['had_original']:
            if not entry.get('initial_observation') and MetaUtil._manual_image_references(dict(category=entry['category'], old=old)):
                raise StorageConflict('공유된 이전 이미지를 보존합니다.')
            if entry['storage'] == 'gdrive':
                backup = old['name'] + '.old_' + entry['identifier']
                if entry.get('initial_observation'):
                    if any(row['Name'].casefold() == backup.casefold() for row in entry['initial_observation']['files']):
                        raise StorageConflict('이미지 백업 이름 충돌')
                else:
                    lookup = dict(rclone=entry['rclone'], config=entry['config'], remote_dir=old['target'].rsplit('/', 1)[0], category=entry['category'])
                    if MetaUtil._manual_drive_rows(lookup, backup):
                        raise StorageConflict('이미지 백업 이름 충돌')
                entry['stage_started'] = True
                cls.image_mutate(entry, old['fid'], old['name'], old.get('parent_id'), backup)
            else:
                MetaUtil.validate_image_local_target(entry['target'], plan['root'])
                if os.path.lexists(entry['backup']):
                    raise StorageConflict('로컬 이미지 백업 이름 충돌')
                os.replace(entry['original_target'], entry['backup'])
                entry['stage_started'] = True
            logger.debug('[MediaReplacement] 이미지 백업 완료: %s', entry['relative_path'])
        entry['staged'] = True

    @classmethod
    def image_write_start(cls, entry, source=None):
        from .util_metadata import MetaUtil
        from .util_runtime import StorageConflict
        cls.image_settings(entry)
        if entry['status']!='pending' or not entry.get('staged'):raise StorageConflict('사전 준비된 이미지 영수증이 필요합니다.')
        plan=entry['image_plan']
        if entry['storage']=='local':
            root=P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data,'images')
            if os.path.abspath(root)!=plan['root']:raise StorageConflict('이미지 로컬 루트 설정 변경')
            MetaUtil.validate_image_local_target(entry['target'],plan['root'])
            if MetaUtil._manual_file_stat(entry['target']):raise StorageConflict('이미지 목적지에 예상하지 않은 파일이 있습니다.')
        elif not entry.get('initial_observation'):
            if P.ModelSetting.get('jav_censored_image_gdrive_remote_path')!=plan['root']:raise StorageConflict('이미지 리모트 설정 변경')
            lookup=dict(rclone=entry['rclone'],config=entry['config'],remote_dir=plan['remote_dir'],category=entry['category'])
            if MetaUtil._manual_drive_rows(lookup,plan['filename']):raise StorageConflict('이미지 정식 이름이 비워지지 않았습니다.')
        changes={'image_write_started':True}
        if source:changes['image_write_checksum']=MetaUtil._user_image_checksum(source)
        entry.update(status='pending', **changes)

    @classmethod
    def image_generated(cls, entry, file_id='', observation=None):
        from .util_metadata import MetaUtil
        from .util_runtime import StorageConflict
        if entry['storage'] == 'gdrive':
            if file_id == entry['image_plan']['old'].get('fid'):
                raise StorageConflict('신규 이미지 ID가 이전 파일과 같습니다.')
            if entry.get('initial_observation'):
                cls.validate_upload_observation(entry, file_id, observation)
            else:
                entry['new_id'] = file_id or ''
                candidate = dict(category=entry['category'], old=dict(url=MetaUtil.format_gdrive_thumbnail_url(file_id), fid=file_id, relative_path=entry['relative_path']))
                if MetaUtil._manual_image_references(candidate, exclude_owner=False):
                    raise StorageConflict('신규 ID가 다른 DB 등록에서 사용되어 보존합니다.')
                if observation is None:
                    plan = entry['image_plan']
                    rows = MetaUtil._manual_drive_rows(dict(remote_dir=plan['remote_dir'], rclone=entry['rclone'], config=entry['config'], category=entry['category']), plan['filename'])
                    if len(rows) != 1 or rows[0].get('Name') != plan['filename'] or (rows[0].get('ID') or rows[0].get('id')) != file_id:
                        raise StorageConflict('생성된 정식 이미지의 동명/ID 확인 실패')
                cls.image_identity(entry, file_id, entry['image_plan']['filename'], entry['image_plan']['parent_id'])
            MetaPreviewUtil.register_remote_file(entry['relative_path'], file_id, entry['category'], is_image=True)
        else:
            if not os.path.isfile(entry['target']) or MetaUtil._user_image_checksum(entry['target']) != entry.get('image_write_checksum'):
                raise StorageConflict('게시 이미지 내용 확인 실패')
            entry.update(new_file=True, new_checksum=entry['image_write_checksum'])
            MetaPreviewUtil.register_local_file(entry['target'])
        entry.update(status='generated', new_id=file_id or '', image_verified=True)

    @classmethod
    def image_finish(cls, entry, commit):
        from .util_metadata import MetaUtil
        from .util_runtime import StorageConflict
        plan = entry['image_plan']; old = plan['old']
        cls.image_settings(entry)
        if entry['storage'] == 'gdrive' and 'system_candidates' in plan:
            return cls.system_image_finish(entry, commit)
        if commit:
            if entry['status'] not in ('generated', 'removed'):
                raise StorageConflict('이미지 교체가 완료되지 않아 백업 정리를 보류합니다.')
            if entry['had_original'] and entry.get('stage_started'):
                if not entry.get('initial_observation') and MetaUtil._manual_image_references(dict(category=entry['category'], old=old)):
                    raise StorageConflict('공유된 이전 이미지를 보존합니다.')
                if entry['storage'] == 'gdrive':
                    cls.image_mutate(entry, old['fid'], old['name'] + '.old_' + entry['identifier'], old.get('parent_id'))
                else:
                    os.remove(entry['backup'])
                logger.debug('[MediaReplacement] DB 저장 후 이미지 백업 정리 완료: %s', entry['relative_path'])
            entry['status'] = 'committed'
        else:
            if entry['storage'] == 'gdrive':
                if entry.get('new_id'):
                    cls.image_mutate(entry, entry['new_id'], plan['filename'], plan.get('parent_id'))
                elif entry.get('image_write_started'):
                    lookup = dict(rclone=entry['rclone'], config=entry['config'], remote_dir=plan['remote_dir'], category=entry['category'])
                    if MetaUtil._manual_drive_rows(lookup, plan['filename']):
                        raise StorageConflict('신규 이미지 ID 불명으로 파일 삭제·백업 복원을 보류합니다.')
                if entry['had_original'] and entry.get('stage_started'):
                    if entry.get('initial_observation') and entry.get('staged'):
                        cls.image_mutate(entry, old['fid'], old['name'] + '.old_' + entry['identifier'], old.get('parent_id'), old['name'])
                    else:
                        identity = cls.image_identity(entry, old['fid'], parent=old.get('parent_id'))
                        if identity['name'] == old['name'] + '.old_' + entry['identifier']:
                            lookup = dict(rclone=entry['rclone'], config=entry['config'], remote_dir=old['target'].rsplit('/', 1)[0], category=entry['category'])
                            if MetaUtil._manual_drive_rows(lookup, old['name']):
                                raise StorageConflict('원본 이미지 복원 이름 충돌')
                            cls.image_mutate(entry, old['fid'], identity['name'], old.get('parent_id'), old['name'])
                        elif identity['name'] != old['name']:
                            raise StorageConflict('기존 이미지 복원 상태 불명')
            else:
                MetaUtil.validate_image_local_target(entry['target'], plan['root'])
                if entry.get('new_file') and os.path.isfile(entry['target']):
                    os.remove(entry['target'])
                if entry['had_original'] and entry.get('stage_started'):
                    if os.path.lexists(entry['original_target']):
                        raise StorageConflict('로컬 원본 복원 이름 충돌')
                    os.replace(entry['backup'], entry['original_target'])
            entry['status'] = 'rolled_back'
            logger.debug('[MediaReplacement] 이미지 롤백 완료: %s', entry['relative_path'])
        if entry['storage'] == 'local':
            for target in {entry['target'], entry['original_target']}:
                MetaPreviewUtil._local_dir_cache.pop(os.path.dirname(target), None)
                if os.path.isfile(target):
                    MetaPreviewUtil.register_local_file(target)
        else:
            for relative in {entry['relative_path'], old.get('relative_path', '')}:
                if relative:
                    key = (entry['category'], True, os.path.dirname(relative))
                    MetaPreviewUtil._remote_dir_cache.pop(key, None); MetaPreviewUtil._verified_remote_dirs.discard(key)
            if entry['status'] == 'committed' and entry.get('new_id'):
                MetaPreviewUtil.register_remote_file(entry['relative_path'], entry['new_id'], entry['category'], is_image=True)
            elif entry['status'] == 'rolled_back':
                MetaPreviewUtil._remote_file_paths.pop((entry['category'], True, str(entry.get('new_id'))), None)
                if old.get('fid'):
                    MetaPreviewUtil.register_remote_file(old['relative_path'], old['fid'], entry['category'], is_image=True)

    @classmethod
    def system_image_finish(cls, entry, commit):
        """Settle only this system slot; never delete unowned or DB-referenced IDs."""
        from .util_metadata import MetaUtil
        from .util_runtime import StorageConflict
        plan = entry['image_plan']
        name = plan['filename']
        stem = re.sub(r'_(?:p|pl|art_\d+)\.[^.]+$', '', name, flags=re.I)
        files = MetaUtil.query_gdrive_remote_dir_files(os.path.dirname(entry['relative_path']),
            entry['category'], code_lower=stem, strict=True)
        observed = MetaUtil.image_initial_observation(entry['relative_path'], entry['category'], {}, files)['files']
        old = {row['ID']: row for row in plan['system_candidates']}
        known = set(old) | ({entry['new_id']} if entry.get('new_id') else set())
        current = [row for row in observed if row['Name'].casefold() == name.casefold()]
        by_id = {row['ID']: row for row in observed}
        changed_backups = any(row.get('staged') and (row['ID'] not in by_id or
                              by_id[row['ID']]['Name'] != row['backup_name']) for row in old.values())
        if changed_backups or any(row['ID'] not in known for row in current):
            entry['cleanup_pending'] = True
            if commit:
                entry['status'] = 'committed'
                return
            raise StorageConflict('동시 업로드의 소유권이 불명확하여 해당 슬롯을 보존합니다.')
        backups = [row for row in observed if row['ID'] in old and
                   row['Name'] == old[row['ID']].get('backup_name')]
        def referenced(row):
            return MetaUtil._manual_image_references(dict(category=entry['category'], old=dict(
                fid=row['ID'], url=MetaUtil.format_gdrive_thumbnail_url(row['ID']),
                relative_path=entry['relative_path'])), exclude_owner=False)
        if commit:
            if entry['status'] not in ('generated', 'removed'):
                raise StorageConflict('이미지 교체 미완료로 백업 정리를 보류합니다.')
            if entry['status'] == 'generated' and not any(row['ID'] == entry.get('new_id') for row in current):
                raise StorageConflict('정식 이름의 신규 이미지 ID를 확인하지 못했습니다.')
            keep = entry.get('new_id')
        else:
            entry['status'] = 'rolling_back'
            chosen = MetaUtil.select_image_candidate(current + backups)
            if not chosen and (current or backups):
                raise StorageConflict('복구 이미지의 최신 시각을 확인하지 못해 보존합니다.')
            keep = chosen['ID'] if chosen else None
            if chosen and chosen['Name'] != name and any(referenced(row) for row in current if row['ID'] != keep):
                raise StorageConflict('다른 DB가 참조하는 정식 이미지가 있어 복원을 보류합니다.')
        for row in current + backups:
            if row['ID'] == keep:
                continue
            if referenced(row):
                entry['cleanup_pending'] = True
                continue
            cls.image_mutate(entry, row['ID'], row['Name'], None)
        if not commit and keep:
            if chosen['Name'] != name:
                cls.image_mutate(entry, keep, chosen['Name'], None, name)
            entry['recovered_id'] = keep
        entry['status'] = 'committed' if commit else 'rolled_back'
        key = (entry['category'], True, os.path.dirname(entry['relative_path']))
        MetaPreviewUtil._remote_dir_cache.pop(key, None)
        MetaPreviewUtil._verified_remote_dirs.discard(key)
        if keep:
            MetaPreviewUtil.register_remote_file(entry['relative_path'], keep, entry['category'], is_image=True)

    @classmethod
    def original_at_target(cls, entry):
        if entry['storage'] == 'local':
            return not entry.get('stage_started') and os.path.isfile(entry['target'])
        original_id = entry.get('old_id')
        if not original_id:
            return False
        item = MetaPreviewUtil.remote_stat([entry['rclone'], '--config', entry['config'], 'lsjson', '--stat', entry['target']],
            category=entry.get('category', ''), purpose='cleanup')
        return bool(item and (item.get('ID') or item.get('id')) == original_id)

    @classmethod
    def finish(cls, entry, commit):
        if entry.get('_finished'):
            return
        entry['_finished'] = True
        if entry.get('image_contract'):
            return cls.image_finish(entry, commit)
        if entry.get('manual_user_image'):
            from .util_metadata import MetaUtil
            return MetaUtil.finish_user_image_replacement(entry, commit)
        from .util_runtime import StorageConflict
        if entry.get('initial_observation') and entry.get('media_type') in ('trailer', 'preview'):
            from .util_metadata import MetaUtil
            if commit:
                if entry['status'] not in ('generated', 'removed'):
                    raise StorageConflict('생성 완료 전 트레일러 백업 정리를 보류합니다.')
                entry['status'] = 'committing'
                if entry.get('retired_prebackup'):
                    MetaUtil.prebackup_registered_video(entry, 'delete')
                entry['status'] = 'committed'
            else:
                entry['status'] = 'rolling_back'
                if entry.get('new_id'):
                    root = MetaPreviewUtil.get_setting('preview_gdrive_remote_path', entry['category'], default='') or MetaPreviewUtil.get_setting('preview_video_folder_id', entry['category'], default='')
                    cls.mutate_observed_file(entry, entry['new_id'], os.path.basename(entry['relative_path']), root)
                elif entry.get('video_write_started') and cls.exists(entry, entry['target']):
                    raise StorageConflict('신규 트레일러 ID 불명으로 파일 삭제·백업 복원을 보류합니다.')
                if entry.get('retired_prebackup'):
                    MetaUtil.prebackup_registered_video(entry, 'restore')
                entry['status'] = 'rolled_back'
            return
        if commit:
            if entry['status'] not in ('generated', 'removed'):
                raise StorageConflict('미디어 교체 미완료로 백업 정리를 보류합니다.')
            entry['status'] = 'committing'
            if entry['backup'] and entry.get('stage_started'):
                cls.delete(entry, entry['backup'])
            if entry.get('retired_video'):
                from .util_metadata import MetaUtil
                MetaUtil.cleanup_replaced_video(entry)
            entry['status'] = 'committed'
            return
        entry['status'] = 'rolling_back'
        backup_exists = bool(entry['backup'] and cls.exists(entry, entry['backup']))
        if entry['had_original'] and not backup_exists and not cls.original_at_target(entry):
            raise StorageConflict('원본 백업이 없어 파일 복원을 완료하지 못했습니다.')
        target_exists = cls.exists(entry, entry['target'])
        if (backup_exists or not entry['had_original']) and target_exists:
            if entry['storage'] == 'local':
                if not entry.get('new_file'):
                    raise StorageConflict('현재 호출의 생성 파일을 확인하지 못해 삭제·복원을 보류합니다.')
            else:
                if not entry.get('new_id'):
                    raise StorageConflict('새 File ID 불명으로 파일 삭제·백업 복원을 보류합니다.')
                current = MetaPreviewUtil.remote_stat([entry['rclone'], '--config', entry['config'], 'lsjson', '--stat', entry['target']], category=entry.get('category', ''), purpose='cleanup')
                if current is None or (current.get('ID') or current.get('id')) != entry['new_id']:
                    raise StorageConflict('새 원격 파일 ID가 달라 삭제·복원을 보류합니다.')
            cls.delete(entry, entry['target'])
        if backup_exists:
            cls.move(entry, entry['backup'], entry['target'])
            logger.debug('[MediaReplacement] 원본 백업 복원 완료: %s', entry['relative_path'])
        entry['status'] = 'rolled_back'
