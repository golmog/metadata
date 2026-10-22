# -*- coding: utf-8 -*-
import os
import copy
import hashlib
import fcntl
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
from .util_runtime import storage_logger

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
    def remote_stat(cls, command, lookup=None):
        from .util_runtime import StorageLookupError
        try:
            result = (lookup or cls.run_rclone_lookup)(command)
            if cls.remote_missing(result):
                return None
            if result.returncode != 0 or not result.stdout.strip():
                raise StorageLookupError('원격 파일 상태 조회 실패')
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
    def run_rclone_lookup(command):
        """Retry transient read failures without treating them as missing files."""
        timeout = 45
        attempts = 3
        target = command[-1]
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - started
                logger.warning('[GDrive Lookup] 시간 초과 (%d/%d, %.1fs, 제한 %ds): %s',
                               attempt, attempts, elapsed, timeout, target)
                if attempt == attempts:
                    raise
            else:
                elapsed = time.monotonic() - started
                detail = str(result.stderr or '').strip().replace('\n', ' ')
                logger.debug('[GDrive Lookup] 조회 완료 (%d/%d, %.1fs, exit=%s): %s',
                             attempt, attempts, elapsed, result.returncode, target)
                transient = result.returncode in (5, 10) or (
                    result.returncode != 0 and any(marker in detail.lower() for marker in (
                        'timeout', 'timed out', 'connection reset', 'connection refused',
                        'connection closed', 'temporary failure', 'temporarily unavailable',
                        'unexpected eof', 'tls handshake', '429', 'rate limit', 'ratelimit',
                        'backenderror', 'http 500', 'http 502', 'http 503', 'http 504',
                        'error 500:', 'error 502:', 'error 503:', 'error 504:',
                        'status code 500', 'status code 502', 'status code 503', 'status code 504')))
                if not transient:
                    if result.returncode:
                        logger.debug('[GDrive Lookup] 조회 응답 (exit=%s): %s (%s)',
                                     result.returncode, 'lookup_failed', target)
                    return result
                logger.warning('[GDrive Lookup] 일시 오류 (%d/%d, %.1fs, exit=%s): %s (%s)',
                               attempt, attempts, elapsed, result.returncode, 'transient_lookup_failure', target)
                if attempt == attempts:
                    return result
            delay = attempt * 2
            logger.debug('[GDrive Lookup] %ds 후 조회 재시도: %s', delay, target)
            time.sleep(delay)

    @classmethod
    def complete_media(cls, data, category='JAV_CEN', media_path=None, options=None):
        """Complete optional videos using the same policy for fresh and cached metadata."""
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
        extra.pop('source_video_path', None)
        before = copy.deepcopy(extra)
        original_before = copy.deepcopy(data.get('original') or {})
        existing_item = opts.get('_existing_media_item') or {}
        if opts.get('preserve_preview') and isinstance(existing_item, dict):
            existing_preview = (existing_item.get('extra_info') or {}).get('preview_clip')
            if existing_preview:
                extra['preview_clip'] = copy.deepcopy(existing_preview)
        cls.check_and_adopt_existing_video(data, category=category,
            reuse_gdrive=opts.get('reuse_existing_media', bool(media_path)), existing_item=opts.get('_existing_media_item'),
            reprocess=opts.get('reuse_existing_media') is False)
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
            logger.info('[MetaPreview] [%s] 공식 트레일러 저장: %s', category, code)
            success, result = cls.process_trailer_download_workflow(code, raw_trailer, category,
                force=not opts.get('reuse_existing_media', False), item_dict=data)
            if not success:
                failures['trailer'] = str(result)
        extra = data.setdefault('extra_info', {})
        if (valid_source and cls.is_auto_create_enabled(category)
                and not extra.get('preview_clip') and not extra.get('trailer_video')):
            logger.info('[MetaPreview] [%s] 저장 영상 부재 -> 프리뷰 생성: %s (%s)', category, code, media_path)
            success, result = cls.process_preview_workflow(code, str(media_path), category, force=False, item_dict=data)
            if not success:
                failures['preview'] = str(result)
        extra = data.setdefault('extra_info', {})
        for kind, key in (('trailer', 'trailer_video'), ('preview', 'preview_clip')):
            if kind in failures:
                errors[kind] = {'message': failures[kind], 'time': datetime.now().isoformat(timespec='seconds')}
                logger.warning('[MetaPreview] [%s] %s 처리 실패, 메타데이터 유지 (%s): %s', category, kind, code, failures[kind])
            elif extra.get(key):
                errors.pop(kind, None)
        if errors:
            extra['media_errors'] = errors
        else:
            extra.pop('media_errors', None)
        logger.debug('[MetaPreview] [%s] 미디어 확인 (%s): source=%s trailer=%s preview=%s failures=%s',
            category, code, valid_source, bool(extra.get('trailer_video')), bool(extra.get('preview_clip')), ','.join(failures))
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
        
        start_bound = duration * 0.01
        end_bound = duration * 0.99
        span = end_bound - start_bound

        segment_interval = span / float(num_segments)
        timestamps = [start_bound + (i * segment_interval) for i in range(num_segments)]

        logger.info(f"[MetaPreview] 무인코딩(Codec Copy) 몽타주 프리뷰 생성 시작: {os.path.basename(source_video_path)} (총 {duration:.1f}초, 목표: {total_duration}초, {num_segments}구간 추출, 코덱: {v_codec})")

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
                    "-ss", f"{ts:.2f}",
                    "-t", f"{target_unit:.2f}",
                    "-i", source_video_path,
                    "-map", "0:v:0",
                    "-c:v", "copy"
                ]

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
        item_obj = cls.remote_stat(cmd)
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
        logger.debug(f"[MetaPreview] 원격 폴더 파일 목록 1회 일괄 적재 시작 ➔ {remote_target_dir}")

        try:
            res = cls.run_rclone_lookup(cmd)
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
                logger.debug(f"[MetaPreview] 원격 폴더 캐시 적재 완료 ({len(file_map)}개 파일): {remote_target_dir}")
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
    def sync_file_to_gdrive(cls, local_path, dest_rel_path, category='JAV_CEN', overwrite=True, is_image=False):
        """
        구글 드라이브 통합 업로드 파이프라인:
        원격 파일 사전 검사 -> 존재 시 ID만 추출 후 스킵 -> 교체 시 임시 리네임 후 직업로드 -> 새 ID 검증 -> 구파일 비동기 삭제
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
            item = cls.remote_stat(cmd, lookup=lambda command: subprocess.run(command,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20))
            if item is None:
                return None
            cls.validate_remote_media(item)
            return item.get('ID') or item.get('id')

        # 원격 파일 존재 여부 사전 검사
        existing_fid = get_remote_file_stat(dest_remote_file)

        # 이미 원격에 파일이 있고 덮어쓰기/재처리가 불필요한 경우: 다운로드/업로드 없이 File ID만 즉시 반환
        if existing_fid and not overwrite:
            logger.debug(f"[MetaPreview] 원격 파일 이미 존재 ➔ 즉시 기존 File ID 반환 및 스킵: {existing_fid} ({os.path.basename(dest_remote_file)})")
            return existing_fid, "already_exists"

        # 재처리/교체 대상인 경우: 기존 파일을 임시 백업 파일명으로 리네임
        backup_remote_file = None
        if existing_fid:
            backup_remote_file = f"{dest_remote_file}.old_{int(time.time())}"
            rename_cmd = [rclone_bin, "--config", rclone_conf, "moveto", dest_remote_file, backup_remote_file]
            p_ren = subprocess.run(rename_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            if p_ren.returncode != 0:
                logger.warning('[MetaPreview] 원격 파일 백업 실패, 교체 중단: exit=%s', p_ren.returncode)
                return None, '기존 파일 백업에 실패해 교체를 중단했습니다.'

        # 정식 파일명으로 직접 업로드 실행
        cmd = [rclone_bin, "--config", rclone_conf, "moveto", local_path, dest_remote_file]
        if extra_options:
            cmd.extend(extra_options.split())

        logger.debug(f"[MetaPreview] Rclone 구글 드라이브 업로드 시작: {dest_remote_file}")
        try:
            p_up = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=240)
            if p_up.returncode != 0:
                logger.error('[MetaPreview] Rclone 업로드 실패: exit=%s', p_up.returncode)
                # 업로드 실패 시 백업해둔 기존 파일 원상 복구
                if backup_remote_file:
                    subprocess.run([rclone_bin, "--config", rclone_conf, "moveto", backup_remote_file, dest_remote_file], timeout=20)
                return None, 'Rclone 업로드에 실패했습니다 (exit=%s).' % p_up.returncode

            # 업로드 완료 후 신규 File ID 조회 및 검증
            new_fid = None
            for attempt in range(1, 6):
                time.sleep(min(2.0, 0.5 * attempt))
                new_fid = get_remote_file_stat(dest_remote_file)
                if new_fid:
                    break

            if not new_fid:
                if backup_remote_file:
                    subprocess.run([rclone_bin, "--config", rclone_conf, "moveto", backup_remote_file, dest_remote_file], timeout=20)
                return None, "업로드 후 신규 Google File ID 획득 실패"

            # 신규 파일 ID 교체가 정상 확인되면 임시 백업해둔 구파일을 비동기 스레드로 백그라운드 삭제
            if backup_remote_file:
                def run_async_remote_delete(target_path):
                    try:
                        del_cmd = [rclone_bin, "--config", rclone_conf, "deletefile", target_path]
                        subprocess.run(del_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                        logger.debug(f"[MetaPreview] 원격 구파일 정리 완료: {target_path}")
                    except Exception as e_del:
                        logger.debug(f"[MetaPreview] 원격 구파일 삭제 예외 (무시): {e_del}")

                threading.Thread(target=run_async_remote_delete, args=(backup_remote_file,), daemon=True).start()

            return new_fid, None

        except Exception as exc:
            logger.exception('[MetaPreview] 구글 드라이브 업로드 예외')
            if backup_remote_file:
                try:
                    subprocess.run([rclone_bin, "--config", rclone_conf, "moveto", backup_remote_file, dest_remote_file], timeout=20)
                except Exception:
                    pass
            return None, '구글 드라이브 업로드 중 오류가 발생했습니다.'

    @classmethod
    def sync_images_to_gdrive(cls, images, category='JAV_CEN'):
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
            for name, relative in names.items():
                if '_user.' in name.lower() or not name or '\n' in name or '\r' in name:
                    raise ValueError('Invalid batch image filename')
                shutil.copyfile(images[relative], os.path.join(staged, name))
                sizes[name] = os.path.getsize(os.path.join(staged, name))
            file_list = os.path.join(work, 'files.txt')
            with open(file_list, 'w', encoding='utf-8') as fp:
                fp.write('\n'.join(names) + '\n')
            command = [binary, '--config', config, 'move', staged, remote, '--no-traverse'] + options
            logger.debug('[MetaPreview] GDrive 이미지 일괄 업로드 시작 (%s개): %s', len(images), remote)
            # A failed move can still have completed some files. Certify those
            # separately, using one directory query for all requested File IDs.
            for attempt in range(3):
                if any(os.path.exists(os.path.join(staged, name)) for name in names):
                    try:
                        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=240)
                        if result.returncode:
                            error = result.stderr[-300:] or 'rclone move failed'
                    except Exception as exc:
                        error = str(exc)
                try:
                    result = subprocess.run([binary, '--config', config, 'lsjson', remote,
                        '--files-only', '--max-depth', '1', '--files-from-raw', file_list],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
                    if result.returncode == 0:
                        for entry in json.loads(result.stdout):
                            name = entry.get('Name') or entry.get('Path')
                            file_id = entry.get('ID') or entry.get('id')
                            if (name in names and file_id and not entry.get('IsDir') and sizes[name] > 0
                                    and entry.get('Size') == sizes[name]
                                    and not os.path.exists(os.path.join(staged, name))):
                                confirmed[names[name]] = file_id
                except Exception as exc:
                    error = str(exc)
                if len(confirmed) == len(images):
                    break
                if attempt < 2:
                    logger.debug('[MetaPreview] GDrive 이미지 일괄 처리 재시도 (%s/3), 미확인: %s',
                        attempt + 2, ', '.join(os.path.basename(path) for path in images if path not in confirmed))
                    time.sleep(attempt + 1)
        if len(confirmed) != len(images):
            error = error or '일부 이미지의 업로드 또는 File ID 확인 실패'
            logger.warning('[MetaPreview] GDrive 일괄 업로드 일부 실패 (%s/%s): %s', len(confirmed), len(images), error)
        else:
            error = None
        logger.debug('[MetaPreview] GDrive 이미지 일괄 업로드 확인: %s/%s', len(confirmed), len(images))
        return confirmed, error

    @classmethod
    def upload_to_gdrive(cls, local_clip_path, dest_rel_path, category='JAV_CEN'):
        """기존 트레일러 업로드 호출을 통합 엔진으로 연결."""
        fid, err = cls.sync_file_to_gdrive(local_clip_path, dest_rel_path, category=category, overwrite=True, is_image=False)
        return fid, err


    @classmethod
    def delete_preview_clip(cls, clip_info, category='JAV_CEN'):
        if not clip_info or not isinstance(clip_info, dict):
            return True, "삭제할 클립 정보가 없습니다."

        storage_type = clip_info.get('storage_type', 'local')

        if storage_type == 'gdrive':
            file_id = clip_info.get('google_fileid')
            if not file_id:
                return False, '구글 드라이브 파일 ID가 없습니다.'
            if file_id:
                rclone_conf = cls.get_setting("preview_rclone_conf", category, default="/root/.config/rclone/rclone.conf")
                # 동영상 리모트 경로 루트의 콜론(:) 앞부분에서 리모트명을 추출하고, 미지정 시 재생/스트리밍 전용 리모트로 폴백
                video_remote_root = cls.get_setting("preview_gdrive_remote_path", category, default="").strip()
                extracted_remote = video_remote_root.split(':', 1)[0].strip() if video_remote_root else ''

                playback_remote = (
                    cls.get_setting("preview_rclone_playback_remote", category, default="").split(':', 1)[0].strip() or
                    cls.get_setting("preview_rclone_remote", category, default="my_gdrive").split(':', 1)[0].strip()
                )

                remote_name = extracted_remote or playback_remote
                access_token = cls.get_gdrive_access_token(rclone_conf, remote_name)

                if not access_token:
                    return False, "구글 드라이브 삭제 인증 토큰을 가져오지 못했습니다."
                if access_token:
                    del_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?supportsAllDrives=true"
                    headers = {"Authorization": f"Bearer {access_token}"}
                    try:
                        res = requests.delete(del_url, headers=headers, timeout=10)
                        if res.status_code in [200, 204]:
                            logger.info(f"[MetaPreview] 구글 드라이브 기존 파일 영구 삭제 성공 (FileID: {file_id})")
                        elif res.status_code == 404:
                            logger.debug(f"[MetaPreview] 구글 드라이브 파일이 이미 존재하지 않음 (FileID: {file_id})")
                        else:
                            return False, f"구글 드라이브 파일 삭제 실패 (HTTP {res.status_code})"
                    except Exception as e_del:
                        return False, f"구글 드라이브 파일 삭제 실패: {e_del}"

        local_path = clip_info.get('local_path')
        if storage_type == 'local' and local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
                logger.info(f"[MetaPreview] 로컬 프리뷰 파일 삭제 완료: {local_path}")
            except Exception as e_rm:
                return False, f"로컬 영상 파일 삭제 실패: {e_rm}"

        return True, "기존 프리뷰 파일 삭제 완료"

    @classmethod
    def delete_video_assets(cls, extra_info, category='JAV_CEN', video_type=None):
        """Delete selected video assets; None retains the existing bulk-delete API."""
        if video_type not in (None, '', 'trailer', 'preview'):
            return False, "유효하지 않은 영상 종류입니다."
        if not extra_info or not isinstance(extra_info, dict):
            return True, "삭제할 비디오 정보가 없습니다."
        deleted = []
        for kind, key, label in (('preview', 'preview_clip', '프리뷰'), ('trailer', 'trailer_video', '트레일러')):
            if video_type and video_type != kind:
                continue
            if not extra_info.get(key):
                continue
            success, message = cls.delete_preview_clip(extra_info[key], category=category)
            if not success:
                return False, message
            extra_info.pop(key, None)
            deleted.append(label)
        return True, ', '.join(deleted) + ' 삭제 완료' if deleted else '삭제할 비디오 파일 없음'

    @classmethod
    def check_and_adopt_existing_video(cls, item_or_dict, category='JAV_CEN', reuse_gdrive=False, existing_item=None, reprocess=False):
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
            extra = dict((item_or_dict.get('extra_info') if isinstance(item_or_dict, dict) else item_or_dict.extra_info) or {})
            changed = False
            for kind, key in (('trailer', 'trailer_video'), ('preview', 'preview_clip')):
                previous = cls.get_stored_video(existing_item or item_or_dict, kind, 'gdrive', category) or {}
                rel_dir, filename = cls.get_video_relative_path(item_or_dict, video_type=kind, category=category)
                relative_path = previous.get('relative_path') or '/'.join(filter(None, (rel_dir, filename)))
                fid = cls.get_remote_file_id(relative_path, category=category, is_image=False)
                if not fid:
                    continue
                actual_path = cls.get_recorded_remote_path(fid, relative_path, category=category)
                extra[key] = dict(previous, storage_type='gdrive', google_fileid=fid, local_path='',
                    relative_path=actual_path, filename=actual_path.rsplit('/', 1)[-1],
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
        # Publish only a complete replacement; preserve the existing video on failure.
        import tempfile
        parent = os.path.dirname(os.path.abspath(destination))
        os.makedirs(parent, exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix='.video_', suffix='.tmp', dir=parent)
        os.close(fd)
        try:
            shutil.copyfile(source, staged)
            os.replace(staged, destination)
        finally:
            if os.path.exists(staged):
                os.remove(staged)

    @classmethod
    def process_trailer_download_workflow(cls, code, trailer_url, category='JAV_CEN', force=False, item_dict=None, use_dir_cache=False):
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

        try:
            target_obj = item_dict if item_dict is not None else item
            extra = target_obj.get('extra_info', {}) if isinstance(target_obj, dict) else dict(target_obj.extra_info or {})
            storage_type = cls.get_setting("preview_storage_type", std_cat, default="local")
            existing_trailer = cls.get_stored_video(target_obj, 'trailer', storage_type, std_cat)
            rel_dir, trailer_filename = cls.get_video_relative_path(target_obj, video_type='trailer', category=std_cat)
            dest_rel_full_path = f"{rel_dir}/{trailer_filename}"
            previous = existing_trailer or {}
            if previous.get('storage_type') == storage_type and previous.get('relative_path'):
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

            # 구글 드라이브 모드이고 덮어쓰기 목적이 아닐 때: 원격 파일 사전 검사 및 File ID 갱신
            if storage_type == 'gdrive' and not force:
                existing_remote_fid = cls.get_remote_file_id(dest_rel_full_path, category=std_cat, is_image=False, use_dir_cache=use_dir_cache)
                if existing_remote_fid:
                    actual_relative_path = cls.get_recorded_remote_path(existing_remote_fid, dest_rel_full_path, category=std_cat)
                    existing_meta = extra.get('trailer_video') or {}
                    cur_fid = existing_meta.get('google_fileid')

                    # 메타데이터에 등록된 File ID와 다르거나 미등록 상태이면 최신 원격 ID로 동기화
                    if cur_fid != existing_remote_fid or not existing_meta.get('storage_type') or existing_meta.get('relative_path') != actual_relative_path:
                        trailer_meta = {
                            'storage_type': 'gdrive',
                            'filename': actual_relative_path.rsplit('/', 1)[-1],
                            'relative_path': actual_relative_path,
                            'duration': existing_meta.get('duration') or 0,
                            'created_time': existing_meta.get('created_time') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'google_fileid': existing_remote_fid,
                            'local_path': ''
                        }
                        extra['trailer_video'] = trailer_meta
                        if isinstance(target_obj, dict):
                            target_obj['extra_info'] = extra
                        else:
                            target_obj.extra_info = extra
                            if sess:
                                ModuleMetaDb.sync_item_video_assets(target_obj)
                                sess.commit()
                        logger.info(f"[MetaPreview] [GDrive] 원격 트레일러 파일 확인 ➔ File ID 동기화 및 다운로드 스킵: {code} ({existing_remote_fid})")
                        return True, trailer_meta
                    else:
                        logger.debug(f"[MetaPreview] [GDrive] 트레일러 File ID 일치 확인 ➔ 다운로드 스킵: {code}")
                        return True, existing_meta

            from .util_metadata import MetaUtil
            replacement_entry = MetaUtil.prepare_video_replacement(target_obj, std_cat, dest_rel_full_path)
            tmp_dir = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            temp_output = os.path.join(tmp_dir, trailer_filename)

            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except Exception:
                    pass

            # 출처 사이트 식별 및 프록시 엔드포인트 결정
            site_raw = target_obj.get('site') if isinstance(target_obj, dict) else getattr(target_obj, 'site', '')
            site_key = (site_raw or '').strip().lower()
            if not site_key or site_key == 'unknown':
                if 'dmm.co.jp' in trailer_url: site_key = 'dmm'
                elif 'mgstage.com' in trailer_url: site_key = 'mgstage'
                elif 'fc2.com' in trailer_url: site_key = 'fc2com'
                elif '1pondo.tv' in trailer_url: site_key = '1pondo'
                elif '10musume.com' in trailer_url: site_key = '10musume'
                elif 'pacopacomama.com' in trailer_url: site_key = 'paco'
                elif 'caribbeancom.com' in trailer_url: site_key = 'carib'
                elif 'heyzo.com' in trailer_url: site_key = 'heyzo'
                elif 'stashdb.org' in trailer_url: site_key = 'stashdb'
                elif 'theporndb.net' in trailer_url: site_key = 'tpdb'
                else: site_key = 'dmm'

            from urllib.parse import quote_plus
            is_uncen = (std_cat == 'JAV_UNCEN')
            video_endpoint = 'jav_video_un' if is_uncen else 'jav_video'
            proxy_route_path = f"/{P.package_name}/normal/{video_endpoint}?site={site_key}&url={quote_plus(trailer_url)}"

            logger.info(f"[MetaPreview] 트레일러 다운로드 시작(FF Proxy): {code} [{site_key}] ➔ {trailer_url}")

            download_success = False
            last_status_code = None
            source_failure = None

            try:
                with F.app.test_client() as client:
                    proxy_res = client.get(proxy_route_path, follow_redirects=True)
                    last_status_code = proxy_res.status_code
                    source_failure = proxy_res.headers.get('X-Metadata-Trailer-Error')

                    if proxy_res.status_code in [200, 206]:
                        if hasattr(proxy_res, 'iter_encoded'):
                            stream_iter = proxy_res.iter_encoded()
                        elif hasattr(proxy_res, 'response') and hasattr(proxy_res.response, '__iter__'):
                            stream_iter = proxy_res.response
                        else:
                            stream_iter = [proxy_res.data]

                        with open(temp_output, 'wb') as f_out:
                            for chunk in stream_iter:
                                if chunk:
                                    f_out.write(chunk)

                        if os.path.exists(temp_output) and os.path.getsize(temp_output) > 1024:
                            download_success = True
            except Exception as e_internal_proxy:
                logger.debug(f"[MetaPreview] 내부 프록시 라우팅 호출 예외 ({code}): {e_internal_proxy}")

            if not download_success and source_failure not in ('not_found', 'invalid_content', 'tls_error'):
                ddns_host = (F.SystemModelSetting.get('ddns') or '').rstrip('/')
                if ddns_host:
                    full_proxy_url = f"{ddns_host}{proxy_route_path}"
                    try:
                        res = requests.get(full_proxy_url, stream=True, timeout=(10, 90), verify=False)
                        last_status_code = res.status_code
                        source_failure = res.headers.get('X-Metadata-Trailer-Error')
                        if res.status_code in [200, 206]:
                            with open(temp_output, 'wb') as f_out:
                                for chunk in res.iter_content(chunk_size=65536):
                                    if chunk:
                                        f_out.write(chunk)
                            if os.path.exists(temp_output) and os.path.getsize(temp_output) > 1024:
                                download_success = True
                    except Exception as e_ddns_proxy:
                        logger.debug(f"[MetaPreview] DDNS 프록시 다운로드 예외 ({code}): {e_ddns_proxy}")

            if not download_success or not os.path.exists(temp_output) or os.path.getsize(temp_output) < 1024:
                cls.remove_official_trailer(target_obj, trailer_url)
                if sess:
                    sess.commit()
                logger.warning(f'[MetaPreview] 다운로드 실패로 공식 트레일러 URL 제거: {code}')
                return False, f"트레일러 프록시 다운로드 실패 (HTTP {last_status_code}, {source_failure or 'temporary_error'})"

            ffprobe_bin = cls.get_ffprobe_path(std_cat)
            dur = cls.get_video_duration(temp_output, ffprobe_bin)

            trailer_meta = {
                'storage_type': storage_type,
                'filename': trailer_filename,
                'relative_path': dest_rel_full_path,
                'duration': int(round(dur)) if dur else 0,
                'created_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            if storage_type == 'gdrive':
                file_id, err = cls.upload_to_gdrive(temp_output, dest_rel_full_path, category=std_cat)
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
            if replacement_entry is not None:
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
                    sess.commit()
                    MetaUtil.finish_media_replacements([replacement_entry], True)

            workflow_success = True
            logger.info(f"[MetaPreview] [{std_cat}] {code} 트레일러 영상 저장 완료 ({storage_type}) -> {trailer_filename}")
            return True, trailer_meta

        except Exception as e:
            if sess:
                sess.rollback()
            logger.exception('[MetaPreview] 트레일러 다운로드 예외: code=%s', code)
            return False, '트레일러 다운로드 중 오류가 발생했습니다.'
        finally:
            if replacement_entry is not None and not workflow_success:
                from .util_metadata import MetaUtil
                MetaUtil.finish_media_replacements([replacement_entry], False)
            if sess:
                sess.remove()
            if not workflow_success and temp_output and os.path.exists(temp_output):
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

        try:
            target_obj = item_dict if item_dict is not None else item
            extra = target_obj.get('extra_info', {}) if isinstance(target_obj, dict) else dict(target_obj.extra_info or {})
            storage_type = cls.get_setting("preview_storage_type", std_cat, default="local")
            existing_clip = cls.get_stored_video(target_obj, 'preview', storage_type, std_cat)
            rel_dir, clip_filename = cls.get_video_relative_path(target_obj, video_type='preview', category=std_cat)
            dest_rel_full_path = f"{rel_dir}/{clip_filename}"
            previous = existing_clip or {}
            if previous.get('storage_type') == storage_type and previous.get('relative_path'):
                recorded = previous['relative_path'].replace('\\', '/')
                if recorded.startswith('/') or ':' in recorded or '..' in recorded.split('/'):
                    raise ValueError('Invalid recorded video path')
                dest_rel_full_path = recorded
                clip_filename = recorded.rsplit('/', 1)[-1]

            if existing_clip and not force:
                return True, existing_clip

            if not existing_clip:
                from .util_metadata import MetaUtil
                replacement_entry = MetaUtil.prepare_video_replacement(target_obj, std_cat, dest_rel_full_path, video_type='preview')

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

            clip_meta = {
                'storage_type': storage_type,
                'filename': clip_filename,
                'relative_path': dest_rel_full_path,
                'duration': int(cls.get_setting("preview_duration", std_cat, default="60")),
                'created_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            if storage_type == 'gdrive':
                file_id, err = cls.upload_to_gdrive(temp_output, dest_rel_full_path, category=std_cat)
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
            if replacement_entry is not None:
                MediaReplacement.generated(replacement_entry, clip_meta.get('google_fileid'))

            if isinstance(target_obj, dict):
                target_obj['extra_info'] = extra
            else:
                extra.pop('_media_replacements', None)
                target_obj.extra_info = extra
                if sess:
                    ModuleMetaDb.sync_item_video_assets(target_obj)
                    sess.commit()
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
                MetaUtil.finish_media_replacements([replacement_entry], False)
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
                            logger.debug(f"[MetaPreview] SA 권한 위임 토큰 캐시 히트: '{remote_name}' -> '{target_user}' (계정: {user_seq}/{total_users})")
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
                        logger.debug(f"[MetaPreview] SA 권한 위임 토큰 갱신 성공: '{remote_name}' -> '{target_user}' ({user_seq}/{total_users}, 유효: {expires_in}초)")
                    else:
                        logger.debug(f"[MetaPreview] SA 토큰 갱신 성공: '{remote_name}' (유효: {expires_in}초)")
                    return access_token
                else:
                    logger.error(f"[MetaPreview] SA 토큰 요청 실패 (HTTP {res.status_code}): {res.text[:200]}")
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
                logger.error(f"[MetaPreview] 리모트 '{remote_name}'에서 인증 정보(SA 키 파일 또는 refresh_token)를 찾을 수 없습니다.")
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
    @classmethod
    def acquire_subject(cls, folder, domain, person_idx):
        os.makedirs(folder, exist_ok=True)
        key = 'person:' + str(domain).upper() + ':' + str(person_idx).casefold()
        path = os.path.join(folder, 'subject_' + hashlib.sha256(key.encode()).hexdigest() + '.lock')
        token = uuid.uuid4().hex
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            from .util_runtime import StorageConflict
            raise StorageConflict('이 인물의 이미지 작업 또는 복구 확인이 진행 중입니다.') from None
        with os.fdopen(fd, 'w') as fp:
            json.dump({'identifier': token, 'owner_pid': os.getpid(), 'owner_token': cls.owner_token(os.getpid())}, fp)
            fp.flush()
            os.fsync(fp.fileno())
        return {'subject_lock': path, 'subject_token': token}

    @staticmethod
    def release_subject(subject):
        path = subject.get('subject_lock')
        if path and os.path.isfile(path):
            with open(path) as fp:
                owner = json.load(fp)
            if owner.get('identifier') == subject.get('subject_token'):
                os.remove(path)

    @staticmethod
    def assert_available(folder, target):
        if not isinstance(target, str) or not target:
            from .util_runtime import StorageLookupError
            raise StorageLookupError('미디어 저장소 대상을 확인할 수 없습니다.')
        path = os.path.join(folder, hashlib.sha256(target.encode()).hexdigest() + '.lock')
        if os.path.exists(path):
            from .util_runtime import StorageConflict
            raise StorageConflict('대상 파일의 교체 또는 복구 확인이 진행 중입니다.')

    @staticmethod
    def owner_token(pid):
        try:
            with open('/proc/sys/kernel/random/boot_id') as fp:
                boot = fp.read().strip()
            with open('/proc/%s/stat' % pid) as fp:
                start = fp.read().rsplit(')', 1)[1].split()[19]
            return boot + ':' + start
        except (OSError, IndexError):
            return ''

    @staticmethod
    def journal_guard(entry):
        from contextlib import contextmanager
        from .util_runtime import StorageConflict
        @contextmanager
        def locked():
            # Reuse the existing target lock inode. Never create a lock/journal
            # for a stale receipt, and never hold this across network operations.
            with open(entry['lock'], 'rb') as guard:
                try:
                    fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise StorageConflict('복구 기록 갱신이 진행 중입니다.') from None
                try:
                    current = os.stat(entry['lock'])
                    opened = os.fstat(guard.fileno())
                    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                        raise StorageConflict('복구 기록 잠금 소유 상태가 변경되었습니다.')
                    yield
                finally:
                    fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
        return locked()

    @staticmethod
    def validate_journal_owner(entry, current):
        from .util_runtime import StorageConflict
        keys = ('identifier', 'target', 'journal', 'lock', 'owner_pid', 'owner_token',
                'subject_lock', 'subject_token', 'person_id', 'person_domain', 'person_idx')
        if (not isinstance(current, dict) or any(entry.get(key) != current.get(key) for key in keys)
                or entry.get('subject_type', 'item') != current.get('subject_type', 'item')):
            raise StorageConflict('복구 기록의 작업 소유 근거가 일치하지 않습니다.')

    @classmethod
    def mark_pending(cls, entry, commit_unknown=False):
        if not os.path.isfile(entry['journal']):
            return False
        try:
            with cls.journal_guard(entry):
                if not os.path.isfile(entry['journal']):
                    return False
                with open(entry['journal']) as fp:
                    current = json.load(fp)
                cls.validate_journal_owner(entry, current)
                status = current.get('status')
                if status in ('committing', 'committed', 'rolling_back', 'rolled_back'):
                    if commit_unknown:
                        entry.update(current)
                        return False
                    # A confirmed cleanup may still need retry while its owner
                    # process lives. Retain its phase and only abandon cleanup.
                    current['abandoned'] = True
                    cls._save_journal(current)
                    entry.update(current)
                    return True
                if status not in ('pending', 'generated', 'removed'):
                    from .util_runtime import StorageConflict
                    raise StorageConflict('알 수 없는 복구 기록 상태입니다.')
                current['abandoned'] = True
                if commit_unknown:
                    current['commit_unknown'] = True
                cls._save_journal(current)
                entry.update(current)
                return True
        except FileNotFoundError:
            if not os.path.exists(entry['journal']):
                return False
            raise

    @classmethod
    def save(cls, entry, create=False):
        with cls.journal_guard(entry):
            exists = os.path.isfile(entry['journal'])
            from .util_runtime import StorageConflict
            if create:
                raise StorageConflict('복구 기록의 최초 생성은 begin에서만 가능합니다.')
            if not create and not exists:
                raise FileNotFoundError('종료된 복구 기록을 다시 생성할 수 없습니다.')
            if exists:
                with open(entry['journal']) as fp:
                    current = json.load(fp)
                cls.validate_journal_owner(entry, current)
                # Ordinary save may add evidence, never replace an existing
                # fact. Managed phase/evidence changes use _transition instead.
                managed = {'status', 'staged', 'stage_started', 'new_id', 'new_file_stat',
                           'backup', 'had_original', 'original_id', 'original_file_stat'}
                flags = {'commit_unknown', 'abandoned'}
                if (any(entry.get(key) != value for key, value in current.items() if key not in flags)
                        or any(key not in current and key in managed for key in entry)):
                    raise StorageConflict('오래된 복구 기록으로 최신 증거를 변경할 수 없습니다.')
                updated = dict(current)
                updated.update({key: value for key, value in entry.items() if key not in flags})
                for flag in flags:
                    if entry.get(flag):
                        updated[flag] = True
                cls._save_journal(updated)
                entry.clear()
                entry.update(updated)
            else:
                cls._save_journal(entry)

    @classmethod
    def read_current(cls, entry):
        with cls.journal_guard(entry):
            with open(entry['journal']) as fp:
                current = json.load(fp)
            cls.validate_journal_owner(entry, current)
            return current

    @classmethod
    def verify_current(cls, entry):
        from .util_runtime import StorageConflict
        current = cls.read_current(entry)
        flags = {'commit_unknown', 'abandoned'}
        if ({k: v for k, v in current.items() if k not in flags} !=
                {k: v for k, v in entry.items() if k not in flags}):
            raise StorageConflict('파일 작업 전에 복구 증거가 변경되었습니다.')

    @classmethod
    def _transition(cls, entry, status, changes=None, replace_generation=False):
        from .util_runtime import StorageConflict
        allowed = {'pending': {'pending', 'generated', 'removed', 'rolling_back'},
                   'generated': {'generated', 'committing', 'rolling_back'},
                   'removed': {'removed', 'committing', 'rolling_back'},
                   'committing': {'committing', 'committed'}, 'committed': {'committed'},
                   'rolling_back': {'rolling_back', 'rolled_back'}, 'rolled_back': {'rolled_back'}}
        changes = changes or {}
        fields = {'pending': {'staged', 'stage_started'}, 'generated': {'new_id', 'new_file_stat'}}
        if set(changes) - fields.get(status, set()):
            raise StorageConflict('허용되지 않은 복구 증거 변경입니다.')
        with cls.journal_guard(entry):
            with open(entry['journal']) as fp:
                current = json.load(fp)
            cls.validate_journal_owner(entry, current)
            flags = {'commit_unknown', 'abandoned'}
            if ({k: v for k, v in current.items() if k not in flags} !=
                    {k: v for k, v in entry.items() if k not in flags}):
                raise StorageConflict('복구 기록이 변경되어 단계 전이를 보류합니다.')
            previous = current.get('status')
            if status not in allowed.get(previous, set()):
                raise StorageConflict('복구 단계가 변경되어 이전 작업을 보류합니다.')
            if status == 'rolling_back' and current.get('commit_unknown'):
                raise StorageConflict('DB 반영 결과 불명으로 파일 복구를 보류합니다.')
            if status == 'pending' and any(current.get(key) and not value for key, value in changes.items()):
                raise StorageConflict('파일 이동 증거를 이전 단계로 변경할 수 없습니다.')
            if previous == 'generated' and not replace_generation and any(
                    key in current and current[key] != value for key, value in changes.items()):
                raise StorageConflict('기존 생성 증거의 변경에는 명시적인 재시도 근거가 필요합니다.')
            updated = dict(current, status=status, **changes)
            cls._save_journal(updated)
            entry.clear()
            entry.update(updated)

    @staticmethod
    def _save_journal(entry):
        folder = os.path.dirname(entry['journal'])
        fd, temporary = tempfile.mkstemp(prefix='.journal_', dir=folder)
        try:
            with os.fdopen(fd, 'w') as fp:
                json.dump(entry, fp, ensure_ascii=False)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temporary, entry['journal'])
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    @staticmethod
    def command(entry, operation, *paths):
        result = subprocess.run([entry['rclone'], '--config', entry['config'], operation, *paths],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=40)
        if result.returncode:
            raise RuntimeError('rclone %s failed: %s' % (operation, result.stderr[-250:]))
        return result.stdout

    @classmethod
    def exists(cls, entry, path):
        if entry['storage'] == 'local':
            return os.path.isfile(path)
        return MetaPreviewUtil.remote_stat(
            [entry['rclone'], '--config', entry['config'], 'lsjson', '--stat', path]) is not None

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
        else:
            try:
                cls.command(entry, 'deletefile', path)
            except RuntimeError as exc:
                if 'not found' not in str(exc).lower() and 'does not exist' not in str(exc).lower():
                    raise

    @classmethod
    def begin(cls, folder, spec, exists=False):
        basename = spec['relative_path'].replace('\\', '/').rsplit('/', 1)[-1].lower()
        if '_user.' in basename or (spec['media_type'] == 'preview' and exists):
            raise ValueError('Protected media cannot be staged for replacement')
        os.makedirs(folder, exist_ok=True)
        identifier = uuid.uuid4().hex
        lock = os.path.join(folder, hashlib.sha256(spec['target'].encode()).hexdigest() + '.lock')
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        entry = dict(spec, identifier=identifier, journal=os.path.join(folder, identifier + '.json'),
                     lock=lock, owner_pid=os.getpid(), owner_token=cls.owner_token(os.getpid()),
                     backup=spec['target'] + '.old_' + identifier if exists else '',
                     had_original=bool(exists), status='pending', staged=False, new_id='')
        collision = False
        try:
            if exists:
                if spec['storage'] == 'local':
                    stat = os.stat(spec['target'])
                    entry['original_file_stat'] = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
                else:
                    original = MetaPreviewUtil.remote_stat([spec['rclone'], '--config', spec['config'],
                                                           'lsjson', '--stat', spec['target']])
                    if original is None:
                        from .util_runtime import StorageConflict
                        raise StorageConflict('예약 중 원본 파일이 변경되었습니다.')
                    entry['original_id'] = original.get('ID') or original.get('id')
            with cls.journal_guard(entry):
                if os.path.exists(entry['journal']):
                    collision = True
                    from .util_runtime import StorageConflict
                    raise StorageConflict('이미 존재하는 복구 기록입니다.')
                cls._save_journal(entry)
            return entry
        except Exception:
            # Never remove another journal on identifier collision. If initial
            # persistence may have succeeded, retain its lock and evidence.
            if (collision or not os.path.exists(entry['journal'])) and os.path.exists(lock):
                os.remove(lock)
            raise

    @classmethod
    def stage(cls, entry):
        from .util_runtime import StorageConflict
        current = cls.read_current(entry)
        if current['status'] != 'pending':
            raise StorageConflict('파일 이동을 시작할 수 없는 복구 단계입니다.')
        if current['staged']:
            entry.clear()
            entry.update(current)
            return
        if entry['backup']:
            if cls.exists(entry, entry['backup']):
                raise StorageConflict('이미 백업 파일이 있어 이전 이동 결과 확인을 보류합니다.')
            cls._transition(entry, 'pending', {'stage_started': True})
            cls.move(entry, entry['target'], entry['backup'])
            cls._transition(entry, 'pending', {'staged': True})

    @classmethod
    def generated(cls, entry, file_id='', *, expected_new_id=None):
        from .util_runtime import StorageConflict
        current = cls.read_current(entry)
        if current['status'] not in ('pending', 'generated'):
            raise StorageConflict('생성 결과를 기록할 수 없는 복구 단계입니다.')
        changes = {'new_id': file_id or ''}
        if entry.get('storage') == 'local' and os.path.isfile(entry['target']):
            stat = os.stat(entry['target'])
            changes['new_file_stat'] = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        retry = bool(expected_new_id and current.get('new_id') == expected_new_id and entry.get('new_id') == expected_new_id)
        cls._transition(entry, 'generated', changes, replace_generation=retry)

    @classmethod
    def removed(cls, entry):
        cls._transition(entry, 'removed')

    @classmethod
    def original_at_target(cls, entry):
        if entry['storage'] == 'local':
            if not entry.get('original_file_stat') or not os.path.isfile(entry['target']):
                return False
            stat = os.stat(entry['target'])
            return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns] == entry['original_file_stat']
        original_id = entry.get('original_id') or entry.get('old_id')
        if not original_id:
            return False
        item = MetaPreviewUtil.remote_stat([entry['rclone'], '--config', entry['config'], 'lsjson', '--stat', entry['target']])
        return bool(item and (item.get('ID') or item.get('id')) == original_id)

    @classmethod
    def finish(cls, entry, commit):
        if not os.path.isfile(entry['journal']):
            return
        # Refresh under the same guard used for phase changes, then CAS each
        # following transition. No guard is held during file/network operations.
        current = cls.read_current(entry)
        entry.clear()
        entry.update(current)
        if commit and entry['status'] in ('rolling_back', 'rolled_back'):
            from .util_runtime import StorageConflict
            raise StorageConflict('확정 commit과 복구 단계가 충돌하여 정리를 보류합니다.')
        if entry['status'] in ('committed', 'rolled_back'):
            pass
        elif (commit and entry['status'] in ('generated', 'removed')) or entry['status'] == 'committing':
            cls._transition(entry, 'committing')
            if entry['backup']:
                cls.verify_current(entry)
                cls.delete(entry, entry['backup'])
            cls._transition(entry, 'committed')
        else:
            cls._transition(entry, 'rolling_back')
            backup_exists = bool(entry['backup'] and cls.exists(entry, entry['backup']))
            if entry['had_original'] and not backup_exists and not cls.original_at_target(entry):
                from .util_runtime import StorageConflict
                raise StorageConflict('원본 백업이 없어 파일 복구를 완료할 수 없습니다.')
            if (backup_exists or not entry['had_original']) and cls.exists(entry, entry['target']):
                from .util_runtime import StorageConflict
                if (entry.get('storage') == 'local' and not entry.get('new_file_stat')) or (
                        entry.get('storage') == 'gdrive' and not entry.get('new_id')):
                    raise StorageConflict('새 파일의 소유 근거가 없어 복구를 보류합니다.')
                if entry.get('storage') == 'local' and entry.get('new_file_stat'):
                    stat = os.stat(entry['target'])
                    if [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns] != entry['new_file_stat']:
                        raise StorageConflict('새 로컬 파일의 소유 상태가 변경되어 복구를 보류합니다.')
                if entry.get('storage') == 'gdrive' and entry.get('new_id'):
                    item = MetaPreviewUtil.remote_stat([entry['rclone'], '--config', entry['config'], 'lsjson', '--stat', entry['target']])
                    if item is None or (item.get('ID') or item.get('id')) != entry['new_id']:
                        raise StorageConflict('새 원격 파일의 소유 상태가 변경되어 복구를 보류합니다.')
                cls.verify_current(entry)
                cls.delete(entry, entry['target'])
            if backup_exists:
                cls.verify_current(entry)
                cls.move(entry, entry['backup'], entry['target'])
            cls._transition(entry, 'rolled_back')
        with cls.journal_guard(entry):
            with open(entry['journal']) as fp:
                current = json.load(fp)
            cls.validate_journal_owner(entry, current)
            if current.get('status') != entry['status'] or current['status'] not in ('committed', 'rolled_back'):
                from .util_runtime import StorageConflict
                raise StorageConflict('복구 종료 단계가 변경되어 정리를 보류합니다.')
            for path in (entry['journal'], entry['lock']):
                if os.path.exists(path):
                    os.remove(path)
            cls.release_subject(entry)

    @classmethod
    def recover(cls, folder, is_committed, logger):
        if not os.path.isdir(folder):
            return
        # Kernel releases this lock on worker/container exit. Multiple workers may
        # start together, but only one may restore or discard a given backup.
        with open(os.path.join(folder, '.recovery'), 'a') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                cls._recover_entries(folder, is_committed, logger)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @classmethod
    def _recover_entries(cls, folder, is_committed, logger):
        for filename in os.listdir(folder):
            if not filename.endswith('.json'):
                continue
            try:
                with open(os.path.join(folder, filename)) as fp:
                    entry = json.load(fp)
                if (not isinstance(entry, dict) or entry.get('subject_type', 'item') not in ('item', 'person')
                        or entry.get('storage') not in ('local', 'gdrive')):
                    logger.warning('[MediaReplacement] 알 수 없는 소유 형식, 복구 보류')
                    continue
                token = cls.owner_token(entry['owner_pid'])
                if token and token == entry.get('owner_token') and not entry.get('abandoned'):
                    continue
                if entry['status'] in ('committing', 'committed'):
                    committed = True
                elif entry['status'] in ('rolling_back', 'rolled_back'):
                    committed = False
                else:
                    committed = is_committed(entry)
                if committed is None:
                    logger.warning('[MediaReplacement] 복구 판정 보류: category=%s code=%s type=%s status=%s',
                        entry.get('category'), entry.get('code'), entry.get('subject_type', 'item'), entry.get('status'))
                    continue
                cls.finish(entry, committed)
                logger.info('[MediaReplacement] 중단된 미디어 작업 복구: %s (%s)', entry['relative_path'], entry['status'])
            except Exception:
                logger.exception('[MediaReplacement] 미디어 작업 복구 보류: %s', filename)
