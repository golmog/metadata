# -*- coding: utf-8 -*-
import os
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

from .setup import *


class MetaPreviewUtil:
    _token_cache = {}
    _impersonate_indices = {}
    _rotation_lock = threading.Lock()

    @classmethod
    def get_setting(cls, key, category='JAV_CEN', default=''):
        cat_upper = str(category or 'JAV_CEN').upper()
        if cat_upper in ['JAV_UNCEN', 'UNCENSORED']:
            module_pfx = 'jav_uncensored'
        elif cat_upper in ['WESTERN', 'WEST']:
            module_pfx = 'western'
        else:
            module_pfx = 'jav_censored'

        val = P.ModelSetting.get(f"{module_pfx}_{key}")
        if val is None or str(val).strip() == '':
            val = P.ModelSetting.get(f"jav_censored_{key}")
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
        ffprobe_bin = cls.get_setting("preview_ffprobe_path", category, default="ffprobe")
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
                    logger.debug(f"[MetaPreview] 구간 #{idx} 컷팅 건너뜀 (ts={ts:.1f}s): {res_cut.stderr[-150:] if res_cut.stderr else '파일 크기 부족'}")

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
                logger.error(f"[MetaPreview] FFmpeg 무인코딩 병합 실패 (코드 {res_concat.returncode}): {res_concat.stderr[-300:]}")
                return False, f"병합 에러: {res_concat.stderr[-150:]}"

            if os.path.exists(output_filepath) and os.path.getsize(output_filepath) > 0:
                elapsed = time.time() - t_start
                fsize_mb = os.path.getsize(output_filepath) / (1024 * 1024)
                actual_dur = cls.get_video_duration(output_filepath, ffprobe_bin)
                logger.info(f"[MetaPreview] 무인코딩 프리뷰 클립 생성 완료: {output_filepath} ({fsize_mb:.2f}MB, 길이: {actual_dur:.1f}초, {elapsed:.1f}초 소요, 총 {len(segment_files)}구간)")
                return True, output_filepath
            else:
                return False, "최종 결합된 프리뷰 파일이 비어있습니다."

        except Exception as e:
            logger.error(f"[MetaPreview] 프리뷰 생성 예외 발생: {e}")
            logger.error(traceback.format_exc())
            return False, str(e)
        finally:
            if os.path.exists(tmp_dir):
                try:
                    shutil.rmtree(tmp_dir)
                except Exception as e_clean:
                    logger.debug(f"[MetaPreview] 임시 컷팅 폴더 정리 예외: {e_clean}")

    @classmethod
    def get_preview_relative_path(cls, item, category='JAV_CEN'):
        """카테고리별 표준 계층 경로 및 파일명 반환 ({카테고리}/{label_1|studio_1}/{label|studio}/{code}_preview.mp4)"""
        cat_upper = str(category or 'JAV_CEN').upper()
        code_clean = (item.code or item.ui_code or 'unknown').strip()
        filename = f"{code_clean}_preview.mp4"

        if cat_upper == 'WESTERN':
            studio_raw = (item.studio or 'Unknown').strip()
            safe_studio = re.sub(r'[^A-Za-z0-9가-힣]', '_', studio_raw).strip('_') or 'Unknown'
            studio_1 = safe_studio[0].upper() if safe_studio else 'ETC'
            if studio_1.isdigit():
                studio_1 = '09'
            rel_dir = f"western/{studio_1}/{safe_studio}"

        elif cat_upper == 'JAV_UNCEN':
            label_raw = (item.label or (item.ui_code.split('-')[0] if '-' in (item.ui_code or '') else '') or 'ETC').strip().upper()
            rel_dir = f"jav_uncen/{label_raw}"

        else:
            # JAV_CEN
            ui_code = item.ui_code or item.code or ''
            label_raw = (ui_code.split('-')[0] if '-' in ui_code else 'ETC').strip().upper()
            label_1 = label_raw[0] if label_raw else 'ETC'
            if label_raw.startswith('741') or label_1.isdigit():
                label_1 = '09'
            rel_dir = f"jav_cen/{label_1}/{label_raw}"

        return rel_dir, filename


    @classmethod
    def upload_to_gdrive(cls, local_clip_path, dest_rel_path, category='JAV_CEN'):
        if not local_clip_path or not os.path.exists(local_clip_path):
            return None, "로컬 클립 파일이 없습니다."

        rclone_bin = cls.get_setting("preview_rclone_path", category, default="rclone")
        rclone_conf = cls.get_setting("preview_rclone_conf", category, default="/root/.config/rclone/rclone.conf")
        
        # 업로드 전용 리모트 경로 설정
        target_remote_path = (
            cls.get_setting("preview_rclone_upload_remote", category, default="") or
            cls.get_setting("preview_rclone_target_path", category, default="")
        ).rstrip('/')
        extra_options = cls.get_setting("preview_rclone_options", category, default="")

        if not target_remote_path:
            return None, "rclone 업로드 리모트 경로가 설정되지 않았습니다."

        dest_remote_file = f"{target_remote_path}/{dest_rel_path.strip('/')}"

        cmd = [rclone_bin, "--config", rclone_conf, "moveto", local_clip_path, dest_remote_file]
        if extra_options:
            cmd.extend(extra_options.split())

        logger.info(f"[MetaPreview] Rclone 구글 드라이브 업로드 시작: {dest_remote_file}")
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
            if p.returncode != 0:
                logger.error(f"[MetaPreview] Rclone 업로드 실패: {p.stderr[-300:]}")
                return None, p.stderr[-200:]

            max_retries = 5
            retry_delay = 2

            for attempt in range(1, max_retries + 1):
                time.sleep(retry_delay)

                ls_cmd = [rclone_bin, "--config", rclone_conf, "lsjson", "--stat", dest_remote_file]
                p_ls = subprocess.run(ls_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)

                if p_ls.returncode == 0 and p_ls.stdout.strip():
                    try:
                        parsed_json = json.loads(p_ls.stdout.strip())
                        item_obj = parsed_json[0] if isinstance(parsed_json, list) and parsed_json else parsed_json
                        if isinstance(item_obj, dict):
                            file_id = item_obj.get('ID') or item_obj.get('id')
                            if file_id:
                                logger.debug(f"[MetaPreview] Rclone 업로드 및 Google File ID 획득 성공 ({attempt}번째 시도): {file_id} ({os.path.basename(dest_remote_file)})")
                                return file_id, None
                    except Exception as e_json:
                        logger.debug(f"[MetaPreview] lsjson 결과 파싱 중 예외: {e_json}")

                logger.warning(f"[MetaPreview] File ID 조회 미반영, 재시도 예정 ({attempt}/{max_retries})")

            logger.error(f"[MetaPreview] {max_retries}회 재시도 후에도 Google File ID 획득 실패: {dest_remote_file}")
            return None, "업로드 후 Google File ID 획득 실패 (재시도 초과)"

        except Exception as e:
            logger.error(f"[MetaPreview] Rclone 업로드 중 예외: {e}")
            return None, str(e)

    @classmethod
    def delete_preview_clip(cls, clip_info, category='JAV_CEN'):
        if not clip_info or not isinstance(clip_info, dict):
            return True, "삭제할 클립 정보가 없습니다."

        storage_type = clip_info.get('storage_type', 'local')

        if storage_type == 'gdrive':
            file_id = clip_info.get('google_fileid')
            if file_id:
                rclone_conf = cls.get_setting("preview_rclone_conf", category, default="/root/.config/rclone/rclone.conf")
                # 삭제 시에는 쓰기 권한이 있는 업로드 리모트 계정을 우선 참조
                remote_name = (
                    cls.get_setting("preview_rclone_upload_remote", category, default="").split(':')[0] or
                    cls.get_setting("preview_rclone_playback_remote", category, default="") or
                    cls.get_setting("preview_rclone_remote", category, default="my_gdrive")
                )
                access_token = cls.get_gdrive_access_token(rclone_conf, remote_name)

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
                            logger.warning(f"[MetaPreview] 구글 드라이브 파일 삭제 실패 (HTTP {res.status_code}): {res.text}")
                    except Exception as e_del:
                        logger.error(f"[MetaPreview] 구글 드라이브 파일 삭제 API 예외: {e_del}")

        local_path = clip_info.get('local_path')
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
                logger.info(f"[MetaPreview] 로컬 프리뷰 파일 삭제 완료: {local_path}")
            except Exception as e_rm:
                logger.error(f"[MetaPreview] 로컬 프리뷰 파일 삭제 실패: {e_rm}")

        return True, "기존 프리뷰 파일 삭제 완료"

    @classmethod
    def process_preview_workflow(cls, code, source_video_path, category='JAV_CEN', force=False):
        """기존 파일 삭제 ➔ FFmpeg 몽타주 추출 ➔ Rclone 업로드/로컬 이동 ➔ 실패 시 임시 파일 강제 정리 ➔ DB extra_info 갱신"""
        from .mod_meta_db import ModuleMetaDb, MetaItem

        logger.debug(f"[MetaPreview] 프리뷰 워크플로우 가동 -> 대상: [{category}] {code}, 원본: '{source_video_path}', 강제실행: {force}")

        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess:
            return False, "DB 세션 획득 실패"

        temp_output = None
        workflow_success = False

        try:
            item = sess.query(MetaItem).filter_by(code=code).first()
            if not item:
                return False, f"작품 메타데이터를 찾을 수 없습니다: {code}"

            extra = dict(item.extra_info or {})
            existing_clip = extra.get('preview_clip')

            # 이미 클립이 존재할 경우 기존 파일(구글 드라이브/로컬)을 먼저 완전 삭제하여 중복 방지
            if existing_clip:
                cls.delete_preview_clip(existing_clip, category=std_cat)

            storage_type = cls.get_setting("preview_storage_type", std_cat, default="local")
            rel_dir, clip_filename = cls.get_preview_relative_path(item, category=std_cat)
            dest_rel_full_path = f"{rel_dir}/{clip_filename}"

            # 임시 인코딩 대상 로컬 경로 결정
            tmp_dir = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            temp_output = os.path.join(tmp_dir, clip_filename)

            # 생성 시점에 이미 동일한 이름의 파일이 임시 디렉터리에 남아있으면 사전 삭제
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                    logger.debug(f"[MetaPreview] 잔여 임시 파일 사전 정리 완료: {temp_output}")
                except Exception as e_pre_rm:
                    logger.warning(f"[MetaPreview] 잔여 임시 파일 삭제 실패: {e_pre_rm}")

            # FFmpeg 몽타주 클립 생성
            success, msg = cls.generate_preview_clip(source_video_path, temp_output, category=std_cat)
            if not success:
                return False, msg

            clip_meta = {
                'storage_type': storage_type,
                'filename': clip_filename,
                'duration': int(cls.get_setting("preview_duration", std_cat, default="60")),
                'created_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            # 구글 드라이브 업로드 처리
            if storage_type == 'gdrive':
                file_id, err = cls.upload_to_gdrive(temp_output, dest_rel_full_path, category=std_cat)
                if not file_id:
                    return False, f"구글 드라이브 업로드 실패: {err}"
                clip_meta['google_fileid'] = file_id
                clip_meta['local_path'] = ''
            else:
                # 로컬 저장 처리 (계층 구조 반영)
                local_root = cls.get_setting("preview_local_path", std_cat, default="/data/previews")
                final_local_file = os.path.join(local_root, dest_rel_full_path.replace('/', os.path.sep))
                os.makedirs(os.path.dirname(final_local_file), exist_ok=True)

                import shutil
                if os.path.exists(final_local_file):
                    try:
                        os.remove(final_local_file)
                    except Exception:
                        pass
                shutil.move(temp_output, final_local_file)
                clip_meta['local_path'] = final_local_file
                clip_meta['google_fileid'] = ''

            # DB extra_info에 무손실 저장 및 소스 비디오 경로 백업
            extra['preview_clip'] = clip_meta
            extra['source_video_path'] = source_video_path
            item.extra_info = extra
            sess.commit()
            ModuleMetaDb.checkpoint_wal()

            workflow_success = True
            logger.info(f"[MetaPreview] [{std_cat}] {code} 프리뷰 클립 등록 완료 ({storage_type}) -> {clip_filename}")
            return True, clip_meta

        except Exception as e:
            sess.rollback()
            logger.error(f"[MetaPreview] 워크플로우 예외 발생 ({code}): {e}")
            logger.error(traceback.format_exc())
            return False, str(e)
        finally:
            sess.remove()
            # 업로드 실패, 예외 등으로 인해 임시 파일이 아직 남아있는 경우 무조건 강제 삭제
            if not workflow_success and temp_output and os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                    logger.info(f"[MetaPreview] 실패 후 잔여 임시 파일 정리 완료: {temp_output}")
                except Exception as e_clean:
                    logger.debug(f"[MetaPreview] 임시 파일 정리 실패: {e_clean}")


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
    def get_gdrive_access_token(cls, rclone_conf_path, remote_name):
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

                # 위임 대상 사용자 선택 및 스레드 안전 순차 로테이션
                target_user = None
                total_users = len(impersonate_users)
                user_seq = 0
                if impersonate_users:
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
                            logger.debug(f"[MetaPreview] SA 권한 위임 토큰 캐시 히트: '{remote_name}' -> '{target_user}'")
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
