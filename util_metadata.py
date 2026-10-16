# -*- coding: utf-8 -*-
import os
import re
import json
import base64
import traceback
import time
import copy
import subprocess
import tempfile
import shutil

from datetime import datetime
from io import BytesIO
from PIL import Image
from urllib.parse import urlparse
from sqlalchemy.orm.attributes import flag_modified

from .setup import *


class MetaUtil:
    """
    메타데이터 통합 유틸리티
    이미지 가공/서버 저장, 4대 갱신 파이프라인, 게이트웨이 필터, 워커 일원화
    """

    @staticmethod
    def media_in_storage(media, mode):
        def value(key):
            return media.get(key) if isinstance(media, dict) else getattr(media, key, None)
        url = str(value('url') or '')
        remote = bool(value('gdrive_fileid') or 'drive.google.com/' in url or 'googleusercontent.com/' in url)
        return remote if mode == 'gdrive' else not remote if mode == 'image_server' else True

    @staticmethod
    def is_user_media(media):
        """Recognize user assets even when a legacy boolean flag was lost."""
        from urllib.parse import urlsplit, parse_qs, unquote
        def value(key):
            return media.get(key) if isinstance(media, dict) else getattr(media, key, None)
        if bool(value('is_user')) or str(value('source') or '').lower() == 'user':
            return True
        for key in ('relative_path', 'filename', 'url'):
            text = str(value(key) or '')
            for _ in range(3):
                try:
                    parsed = urlsplit(text.replace('\\', '/'))
                except ValueError:
                    break
                filename = unquote(parsed.path).rsplit('/', 1)[-1]
                if re.search(r'_user\.(?:jpe?g|png|webp)$', filename, re.I):
                    return True
                nested = parse_qs(parsed.query).get('url')
                if not nested:
                    break
                text = nested[0]
        return False

    @classmethod
    def adopt_item_user_images(cls, item, mode, rel_dir, stem, selected, use_dir_cache=True):
        """Discover/repair active-storage user assets without writing image files."""
        from .mod_meta_db import MetaMedia
        from .util_preview import MetaPreviewUtil
        from urllib.parse import urlsplit, parse_qs, unquote
        if mode not in ('gdrive', 'image_server') or not selected:
            return []
        root = P.ModelSetting.get('jav_censored_image_server_local_path') or '/data/images'
        server = str(P.ModelSetting.get('jav_censored_image_server_url') or
                     f"{F.SystemModelSetting.get('ddns')}/images").rstrip('/')
        active = [m for m in item.media_files if cls.media_in_storage(m, mode)]
        changes, folders = [], {}
        def order(media):
            return 0 if media.media_type == 'poster' else 1 if media.media_type == 'landscape' else media.sort_order or 0
        def safe_path(path):
            path = str(path or '').replace('\\', '/')
            return path if path and not path.startswith('/') and ':' not in path and '..' not in path.split('/') else ''
        def files_in(folder):
            if folder not in folders:
                if mode == 'gdrive':
                    folders[folder] = MetaPreviewUtil.get_remote_directory_files(folder, category=item.category,
                                                              is_image=True, use_cache=use_dir_cache)
                else:
                    directory = os.path.join(root, folder)
                    folders[folder] = {name.lower(): name for name in os.listdir(directory)
                                      if os.path.isfile(os.path.join(directory, name))} if os.path.isdir(directory) else {}
            return folders[folder]
        for (kind, index), suffix in selected.items():
            rows = [m for m in active if m.media_type == kind and order(m) == index]
            current = item.poster_url if kind == 'poster' else (item.extra_info or {}).get('landscape_url', '') if kind == 'landscape' else ''
            users = [m for m in rows if cls.is_user_media(m)]
            user = next((m for m in users if m.url == current), next(iter(users), None))
            path = safe_path(user.relative_path) if user else ''
            fid = (user.gdrive_fileid or '') if user else ''
            url = user.url if user else ''
            if user and mode == 'gdrive' and not fid:
                parsed = urlsplit(url or '')
                fid = (parse_qs(parsed.query).get('id') or [''])[0]
                if not fid:
                    matched_id = re.search(r'/d/([^/]+)', parsed.path)
                    fid = matched_id[1] if matched_id else ''
            if not url and fid:
                url = cls.format_gdrive_thumbnail_url(fid)
            elif not url and path and mode == 'image_server':
                url = f'{server}/{path}'
            if not user and current and cls.is_user_media({'url': current}) and cls.media_in_storage({'url': current}, mode):
                url = current
                fid = (parse_qs(urlsplit(current).query).get('id') or [''])[0]
                if mode == 'image_server' and current.startswith(server + '/'):
                    path = safe_path(unquote(current[len(server) + 1:]))
            # Prefer recorded locations, then the current folder format; match exact filenames.
            candidates = []
            for media in rows:
                recorded = safe_path(media.relative_path)
                if recorded:
                    base = recorded.rsplit('/', 1)[-1]
                    if not cls.is_user_media({'relative_path': recorded}):
                        name, ext = os.path.splitext(base)
                        base = name + '_user' + ext
                    candidates.append((os.path.dirname(recorded), base.lower()))
            candidates.extend((rel_dir, f'{stem}_{suffix}_user.{ext}') for ext in ('jpg', 'jpeg', 'png', 'webp'))
            for folder, filename in dict.fromkeys(candidates):
                # An already registered user asset remains protected even if its file is unavailable.
                if url and path:
                    break
                observed = files_in(folder)
                found = observed.get(filename.lower())
                if not found:
                    continue
                found_path = '/'.join(part for part in (folder, filename) if part)
                if mode == 'gdrive':
                    # Never attach another file's path to an existing user file ID.
                    if fid and str(fid) != str(found):
                        continue
                    fid = str(found)
                    path = MetaPreviewUtil.get_recorded_remote_path(fid, found_path, category=item.category, is_image=True)
                    url = cls.format_gdrive_thumbnail_url(fid)
                else:
                    path = '/'.join(part for part in (folder, found) if part)
                    url = f'{server}/{path}'
                break
            if not url:
                continue
            if user is None:
                user = next((m for m in rows if m.url == url or (fid and m.gdrive_fileid == fid) or
                             (path and m.relative_path == path)), None)
            created = user is None
            if created:
                user = MetaMedia(media_type=kind, sort_order=index, is_user=True)
                item.media_files.append(user)
                active.append(user)
            modified = created
            values = {'is_user': True, 'source': 'user', 'url': url}
            if path: values['relative_path'] = path
            if fid: values['gdrive_fileid'] = fid
            for key, value in values.items():
                if getattr(user, key) != value:
                    setattr(user, key, value)
                    modified = True
            if kind == 'poster' and item.poster_url != url:
                item.poster_url = url; modified = True
            elif kind == 'landscape' and (item.extra_info or {}).get('landscape_url') != url:
                extra = dict(item.extra_info or {}); extra['landscape_url'] = url
                item.extra_info = extra; modified = True
            if modified:
                changes.append({'poster': '사용자 포스터 연동', 'landscape': '사용자 가로커버 연동'}.get(kind, f'사용자 팬아트 {index + 1} 연동'))
        return changes

    @staticmethod
    def mark_batch_site_request(options):
        """Track site lookups within one batch item, including failed requests."""
        activity = (options or {}).get('_batch_site_activity')
        if activity is not None:
            activity['requested'] = True

    @staticmethod
    def wait_after_batch_site_request(activity, delay):
        if activity.get('requested') and delay > 0:
            time.sleep(delay)

    @classmethod
    def get_server_folder_and_prefix(cls, domain, category, stem, studio="", year=1900):
        from support_site import SiteAvBase
        return SiteAvBase.get_server_folder_and_prefix(
            domain=domain,
            category=category,
            stem=stem,
            studio=studio,
            year=year
        )

    @classmethod
    def save_normalized_jpeg(cls, pil_img, save_filepath):
        from support_site import SiteAvBase
        tmp_dir = os.path.join(path_data, 'tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        fd, staging = tempfile.mkstemp(prefix='jpeg_', suffix='.jpg', dir=tmp_dir)
        os.close(fd)
        converted = None
        try:
            image = pil_img
            if pil_img.mode not in ('RGB', 'L'):
                converted = pil_img.convert('RGB')
                image = converted
            image.save(staging, 'JPEG', quality=95, optimize=True)
            SiteAvBase.publish_image_file(staging, save_filepath)
        finally:
            if converted is not None: converted.close()
            if os.path.exists(staging): os.remove(staging)

    @classmethod
    def get_image_relative_dir(cls, domain, category, stem, studio='', year=1900):
        from support_site import SiteAvBase
        return SiteAvBase.get_image_relative_dir(domain, category, stem, studio=studio, year=year)

    @classmethod
    def publish_user_workspace(cls, workspace, destination):
        from support_site import SiteAvBase
        for filename in os.listdir(workspace):
            if '_user.' not in filename:
                continue
            SiteAvBase.publish_image_file(os.path.join(workspace, filename), os.path.join(destination, filename))
            # Remove replaceable system versions only after the user asset exists.
            stem = filename.rsplit('_user.', 1)[0]
            for ext in ('jpg', 'jpeg', 'png', 'webp'):
                old = os.path.join(destination, f'{stem}.{ext}')
                if os.path.isfile(old): os.remove(old)

    @classmethod
    def get_gdrive_file_id_from_path(cls, filepath, category='JAV_CEN'):
        if not filepath:
            return None

        from .util_preview import MetaPreviewUtil
        cat_upper = str(category or 'JAV_CEN').upper()
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')

        rclone_bin = MetaPreviewUtil.get_setting("rclone_path", category, default="rclone")
        rclone_conf = MetaPreviewUtil.get_setting("rclone_conf", category, default="/data/db/rclone.conf")

        raw_upload_remote = (
            P.ModelSetting.get("jav_censored_image_gdrive_remote_path") or
            ""
        ).strip()

        norm_file = str(filepath).replace('\\', '/')
        if os.path.isabs(norm_file) or '..' in norm_file.split('/'):
            raise ValueError('Expected a remote relative image path')
        rel_path = norm_file

        if not raw_upload_remote:
            logger.error("[MetaUtil] Rclone 업로드 리모트가 설정되지 않아 Google Drive File ID 조회를 중단합니다.")
            return None

        filename = os.path.basename(norm_file)
        remote_candidates = []

        target_path = MetaPreviewUtil.build_rclone_target_remote_path(raw_upload_remote, dest_rel_path=rel_path)
        if target_path:
            remote_candidates.append(target_path)

        target_file_only = MetaPreviewUtil.build_rclone_target_remote_path(raw_upload_remote, dest_rel_path=filename)
        if target_file_only and target_file_only not in remote_candidates:
            remote_candidates.append(target_file_only)

        max_attempts = 15
        for attempt in range(1, max_attempts + 1):
            for remote_target in remote_candidates:
                try:
                    cmd = [rclone_bin, "--config", rclone_conf, "lsjson", "--stat", remote_target]
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
                    if res.returncode == 0 and res.stdout.strip():
                        parsed = json.loads(res.stdout.strip())
                        item_obj = parsed[0] if isinstance(parsed, list) and parsed else parsed
                        if isinstance(item_obj, dict):
                            fid = item_obj.get('ID') or item_obj.get('id')
                            if fid:
                                logger.debug(f"[MetaUtil] rclone 원격 경로로 File ID 획득 성공 ({attempt}번째 시도): {fid} ({remote_target})")
                                return fid
                except subprocess.TimeoutExpired:
                    logger.debug(f"[MetaUtil] rclone lsjson 타임아웃 발생 ({attempt}번째 시도): {remote_target}")
                except Exception as e_cmd:
                    logger.debug(f"[MetaUtil] rclone lsjson 실행 예외: {e_cmd}")

            time.sleep(1.0)

        logger.warning(f"[MetaUtil] Google Drive File ID 추출 실패 (총 {max_attempts}회 시도 초과): {filename}")
        return None

    @classmethod
    def format_gdrive_thumbnail_url(cls, file_id):
        if not file_id:
            return ""
        return f"https://drive.google.com/thumbnail?id={file_id}&sz=s0"

    @classmethod
    def upload_image_to_gdrive(cls, local_tmp_path, dest_rel_path, category='JAV_CEN', overwrite=True):
        """신규 통합 원격 업로드 엔진을 활용하여 이미지 업로드 및 사전 검사 수행."""
        if not local_tmp_path or not os.path.exists(local_tmp_path):
            return None

        from .util_preview import MetaPreviewUtil

        clean_rel = dest_rel_path.replace('\\', '/')
        cat_upper = str(category or 'JAV_CEN').upper()
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')
        if os.path.isabs(clean_rel) or '..' in clean_rel.split('/'):
            raise ValueError('Expected a remote relative image path')

        fid, err = MetaPreviewUtil.sync_file_to_gdrive(
            local_path=local_tmp_path,
            dest_rel_path=clean_rel,
            category=category,
            overwrite=overwrite,
            is_image=True
        )

        if not fid:
            logger.error(f"[MetaUtil] 이미지 구글 드라이브 통합 업로드 실패: {err}")
            return None

        return fid

    @classmethod
    def query_gdrive_remote_dir_files(cls, dest_rel_dir, category='JAV_CEN', code_lower=None):
        """rclone lsjson으로 구글 드라이브 원격 대상 디렉토리의 파일 목록과 File ID를 직접 타겟팅 조회"""
        from .util_preview import MetaPreviewUtil
        cat_upper = str(category or 'JAV_CEN').upper()
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')

        rclone_bin = MetaPreviewUtil.get_setting("rclone_path", category, default="rclone")
        rclone_conf = MetaPreviewUtil.get_setting("rclone_conf", category, default="/data/db/rclone.conf")

        raw_upload_remote = (
            P.ModelSetting.get("jav_censored_image_gdrive_remote_path") or
            ""
        ).strip()

        if not raw_upload_remote:
            return {}

        remote_dir = MetaPreviewUtil.build_rclone_target_remote_path(raw_upload_remote, dest_rel_path=dest_rel_dir)
        if not remote_dir:
            return {}

        cmd = [rclone_bin, "--config", rclone_conf, "lsjson"]
        clean_code = str(code_lower or '').strip().lower()
        if clean_code:
            cmd.extend(["--include", f"{clean_code}_*", "--ignore-case"])
        cmd.append(remote_dir)

        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12)
            if res.returncode == 0 and res.stdout.strip():
                items = json.loads(res.stdout.strip())
                class ObservedFileMap(dict):
                    pass
                file_map = ObservedFileMap()
                file_map.actual_names = {}
                for item in items:
                    if isinstance(item, dict) and not item.get('IsDir'):
                        fname = item.get('Name') or item.get('Path')
                        fid = item.get('ID') or item.get('id')
                        if fname and fid:
                            file_map[fname.lower()] = fid
                            file_map.actual_names[str(fid)] = fname
                target_log = f" (타겟 필터: {clean_code}_*)" if clean_code else ""
                logger.debug(f"[MetaUtil] 구글 드라이브 원격 디렉토리 직접 조회 완료 ({len(file_map)}개 파일 발견{target_log}): {remote_dir}")
                return file_map
        except Exception as e:
            logger.debug(f"[MetaUtil] 구글 드라이브 원격 디렉토리 조회 예외 ({remote_dir}): {e}")

        return {}

    @classmethod
    def save_user_cropped_poster(cls, code, crop_data_or_base64, pl_image_base64_data=None, p_image_base64_data=None, category=None):
        if not category:
            logger.error(f"[MetaUtil] save_user_cropped_poster: category 인자가 지정되지 않았습니다. code='{code}'")
            return False, '카테고리가 지정되지 않았습니다.'

        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia

        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess:
            return False, f"유효하지 않은 카테고리입니다: '{category}'"
        if not ModuleMetaDb._ensure_meta_db_writable(category):
            return False, '메타 DB 마이그레이션 완료 전에는 사용자 이미지 변경을 실행할 수 없습니다.'

        workspace = None
        try:
            item = sess.query(MetaItem).filter_by(code=code).first()
            if not item:
                return False, f"해당 코드의 메타데이터를 찾을 수 없습니다: {code}"

            if std_cat == 'WESTERN':
                stem = (item.code or item.ui_code or '').lower()
            else:
                stem = (item.ui_code or item.originaltitle or item.code or '').lower()

            studio = item.studio or ''
            year = item.year or 1900

            target_folder, server_url_prefix = cls.get_server_folder_and_prefix(
                domain, std_cat, stem, studio=studio, year=year
            )

            if not target_folder or not server_url_prefix:
                return False, '이미지 서버 로컬 경로 또는 URL 설정이 비어있습니다.'

            final_folder = target_folder
            module_prefix = 'western' if std_cat == 'WESTERN' else ('jav_uncensored' if std_cat == 'JAV_UNCEN' else 'jav_censored')
            image_mode = P.ModelSetting.get(f'{module_prefix}_image_mode') or 'image_server'
            relative_dir = cls.get_image_relative_dir(domain, std_cat, stem, studio=studio, year=year)
            tmp_root = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_root, exist_ok=True)
            workspace = tempfile.TemporaryDirectory(prefix='user_image_', dir=tmp_root)
            target_folder = workspace.name

            src_img = None
            source_type = 'pl'
            crop_info_tmp = None

            try:
                if isinstance(crop_data_or_base64, str) and crop_data_or_base64.startswith('{'):
                    crop_info_tmp = json.loads(crop_data_or_base64)
                    if isinstance(crop_info_tmp, dict) and crop_info_tmp.get('source_type'):
                        source_type = str(crop_info_tmp['source_type']).lower()
            except:
                pass

            if p_image_base64_data:
                raw_b64 = p_image_base64_data.split(',', 1)[1] if ',' in p_image_base64_data else p_image_base64_data
                src_img = Image.open(BytesIO(base64.b64decode(raw_b64)))

            elif pl_image_base64_data:
                raw_b64 = pl_image_base64_data.split(',', 1)[1] if ',' in pl_image_base64_data else pl_image_base64_data
                src_img = Image.open(BytesIO(base64.b64decode(raw_b64)))
                user_pl_path = os.path.join(target_folder, f"{stem}_pl_user.jpg")
                cls.save_normalized_jpeg(src_img, user_pl_path)

                for ext_cand in ['jpg', 'jpeg', 'png', 'webp']:
                    old_pl = os.path.join(target_folder, f"{stem}_pl.{ext_cand}")
                    if os.path.exists(old_pl):
                        try: os.remove(old_pl)
                        except: pass

            custom_url = None
            if isinstance(crop_info_tmp, dict):
                custom_url = crop_info_tmp.get('crop_url') or crop_info_tmp.get('url')

            if src_img is None and custom_url and custom_url.startswith(('http://', 'https://')):
                from support_site import SiteAvBase
                src_img = SiteAvBase.imopen(custom_url)
                if src_img is not None:
                    logger.debug(f"[MetaUtil] 원격 직접 입력 URL에서 크롭 소스 획득 성공: {custom_url}")

            if src_img is None and source_type == 'p':
                for cand in [f"{stem}_p_user.jpg", f"{stem}_p.jpg", f"{stem}_p.png", f"{stem}_p.webp"]:
                    cp = os.path.join(final_folder, cand) if image_mode == 'image_server' else ''
                    if os.path.exists(cp):
                        src_img = Image.open(cp)
                        break
                if src_img is None and item.poster_url and item.poster_url.startswith('http'):
                    from support_site import SiteAvBase
                    src_img = SiteAvBase.imopen(item.poster_url)

            elif src_img is None:
                for cand in [f"{stem}_pl_user.jpg", f"{stem}_pl.jpg", f"{stem}_pl.png", f"{stem}_pl.webp"]:
                    cp = os.path.join(final_folder, cand) if image_mode == 'image_server' else ''
                    if os.path.exists(cp):
                        src_img = Image.open(cp)
                        break

            if src_img is None:
                target_url = None
                if source_type == 'p':
                    target_url = item.poster_url
                else:
                    for m in item.media_files:
                        if m.media_type == 'landscape':
                            target_url = m.url
                            break
                    if not target_url:
                        fanarts = [m.url for m in item.media_files if m.media_type == 'fanart']
                        if fanarts:
                            target_url = fanarts[0]
                    if not target_url:
                        target_url = item.poster_url

                if target_url and target_url.startswith('http'):
                    from support_site import SiteAvBase
                    src_img = SiteAvBase.imopen(target_url)

            if src_img is None:
                return False, '처리할 원본 이미지를 디스크 또는 원격지에서 찾을 수 없습니다.'

            cropped_p_img = None
            try:
                crop_info = json.loads(crop_data_or_base64) if isinstance(crop_data_or_base64, str) and crop_data_or_base64.startswith('{') else None
                if crop_info and 'width' in crop_info and 'height' in crop_info:
                    rotate_angle = crop_info.get('rotate', 0)
                    working_img = src_img
                    if rotate_angle != 0:
                        working_img = src_img.rotate(-rotate_angle, expand=True)

                    img_w, img_h = working_img.size
                    cx = max(0, int(round(crop_info['x'])))
                    cy = max(0, int(round(crop_info['y'])))
                    cw = min(int(round(crop_info['width'])), img_w - cx)
                    ch = min(int(round(crop_info['height'])), img_h - cy)

                    cropped_p_img = working_img.crop((cx, cy, cx + cw, cy + ch))
            except Exception as e_parse:
                logger.debug(f"[MetaUtil] 크롭 좌표 파싱 실패 (원본 사용): {e_parse}")

            if cropped_p_img is None:
                cropped_p_img = src_img

            user_poster_path = os.path.join(target_folder, f"{stem}_p_user.jpg")
            cls.save_normalized_jpeg(cropped_p_img, user_poster_path)
            cropped_p_img.close()
            src_img.close()

            for ext_cand in ['jpg', 'jpeg', 'png', 'webp']:
                old_p = os.path.join(target_folder, f"{stem}_p.{ext_cand}")
                if os.path.exists(old_p):
                    try: os.remove(old_p)
                    except: pass

            module_prefix = (
                'western' if std_cat == 'WESTERN'
                else 'jav_uncensored' if std_cat == 'JAV_UNCEN'
                else 'jav_censored'
            )
            image_mode = P.ModelSetting.get(f"{module_prefix}_image_mode") or 'image_server'
            gdrive_poster_id = ''
            gdrive_landscape_id = ''

            def get_remote_relative_path(filename):
                return f'{relative_dir}/{filename}'.strip('/')

            if image_mode == 'gdrive':
                gdrive_poster_id = cls.upload_image_to_gdrive(
                    user_poster_path,
                    get_remote_relative_path(f"{stem}_p_user.jpg"),
                    category=std_cat
                ) or ''
                if not gdrive_poster_id:
                    return False, '구글 드라이브 포스터 업로드에 실패했습니다.'
                new_poster_url = cls.format_gdrive_thumbnail_url(gdrive_poster_id)
            else:
                new_poster_url = f"{server_url_prefix}/{stem}_p_user.jpg"
            item.poster_url = new_poster_url

            p_media = next((m for m in item.media_files if m.media_type == 'poster' and cls.media_in_storage(m, image_mode)), None)
            if p_media:
                p_media.url = new_poster_url
                p_media.is_user = True
                p_media.gdrive_fileid = gdrive_poster_id or None
                p_media.relative_path = get_remote_relative_path(f"{stem}_p_user.jpg") if image_mode == 'gdrive' else f"{stem}_p_user.jpg"
                p_media.source = 'user'
                p_media.updated_time = datetime.now()
            else:
                item.media_files.append(MetaMedia(
                    media_type="poster", url=new_poster_url, is_user=True, sort_order=0,
                    gdrive_fileid=gdrive_poster_id or None,
                    relative_path=get_remote_relative_path(f"{stem}_p_user.jpg") if image_mode == 'gdrive' else f"{stem}_p_user.jpg",
                    source='user', updated_time=datetime.now()
                ))

            if pl_image_base64_data:
                pl_path = os.path.join(target_folder, f"{stem}_pl_user.jpg")
                if image_mode == 'gdrive':
                    gdrive_landscape_id = cls.upload_image_to_gdrive(
                        pl_path,
                        get_remote_relative_path(f"{stem}_pl_user.jpg"),
                        category=std_cat
                    ) or ''
                    if not gdrive_landscape_id:
                        return False, '구글 드라이브 가로 커버 업로드에 실패했습니다.'
                    new_pl_url = cls.format_gdrive_thumbnail_url(gdrive_landscape_id)
                else:
                    new_pl_url = f"{server_url_prefix}/{stem}_pl_user.jpg"
                pl_media = next((m for m in item.media_files if m.media_type == 'landscape' and cls.media_in_storage(m, image_mode)), None)
                if pl_media:
                    pl_media.url = new_pl_url
                    pl_media.is_user = True
                    pl_media.gdrive_fileid = gdrive_landscape_id or None
                    pl_media.relative_path = get_remote_relative_path(f"{stem}_pl_user.jpg") if image_mode == 'gdrive' else f"{stem}_pl_user.jpg"
                    pl_media.source = 'user'
                    pl_media.updated_time = datetime.now()
                else:
                    item.media_files.append(MetaMedia(
                        media_type="landscape", url=new_pl_url, is_user=True, sort_order=1,
                        gdrive_fileid=gdrive_landscape_id or None,
                        relative_path=get_remote_relative_path(f"{stem}_pl_user.jpg") if image_mode == 'gdrive' else f"{stem}_pl_user.jpg",
                        source='user', updated_time=datetime.now()
                    ))

            if image_mode != 'gdrive':
                cls.publish_user_workspace(target_folder, final_folder)
            item.updated_time = datetime.now()
            sess.commit()
            ModuleMetaDb.checkpoint_wal()

            logger.info(f"[MetaUtil] 유저 포스터 저장 성공: [{std_cat}] {item.code} ➔ {new_poster_url}")
            return True, new_poster_url

        except Exception as e:
            logger.error(f"[MetaUtil] save_user_cropped_poster 오류 ({code}): {e}")
            logger.error(traceback.format_exc())
            sess.rollback()
            return False, str(e)
        finally:
            if workspace is not None: workspace.cleanup()
            sess.remove()

    @classmethod
    def save_direct_user_image(cls, code, source_path_or_url=None, image_type='pl', category=None, image_base64_data=None):
        if not category:
            logger.error(f"[MetaUtil] save_direct_user_image: category 누락. code='{code}'")
            return False, '카테고리가 지정되지 않았습니다.'

        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia
        from support_site import SiteAvBase

        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess:
            return False, f"유효하지 않은 카테고리입니다: '{category}'"
        if not ModuleMetaDb._ensure_meta_db_writable(category):
            return False, '메타 DB 마이그레이션 완료 전에는 사용자 이미지 변경을 실행할 수 없습니다.'

        workspace = None
        try:
            item = sess.query(MetaItem).filter_by(code=code).first()
            if not item:
                return False, f"해당 코드의 메타데이터를 찾을 수 없습니다: {code}"

            if std_cat == 'WESTERN':
                stem = (item.code or item.ui_code or '').lower()
            else:
                stem = (item.ui_code or item.originaltitle or item.code or '').lower()

            studio = item.studio or ''
            year = item.year or 1900

            target_folder, server_url_prefix = cls.get_server_folder_and_prefix(
                domain, std_cat, stem, studio=studio, year=year
            )
            if not target_folder or not server_url_prefix:
                return False, '이미지 서버 로컬 경로 또는 URL 설정이 비어있습니다.'

            final_folder = target_folder
            module_prefix = 'western' if std_cat == 'WESTERN' else ('jav_uncensored' if std_cat == 'JAV_UNCEN' else 'jav_censored')
            image_mode = P.ModelSetting.get(f'{module_prefix}_image_mode') or 'image_server'
            relative_dir = cls.get_image_relative_dir(domain, std_cat, stem, studio=studio, year=year)
            tmp_root = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_root, exist_ok=True)
            workspace = tempfile.TemporaryDirectory(prefix='user_image_', dir=tmp_root)
            target_folder = workspace.name

            target_type = 'p' if str(image_type).lower() == 'p' else 'pl'
            clean_source = str(source_path_or_url or '').strip()
            src_img = None

            if image_base64_data:
                try:
                    raw_b64 = image_base64_data.split(',', 1)[1] if ',' in image_base64_data else image_base64_data
                    src_img = Image.open(BytesIO(base64.b64decode(raw_b64)))
                    logger.debug(f"[MetaUtil] save_direct_user_image: Base64 이미지 디코딩 로드 성공")
                except Exception as e_b64:
                    logger.warning(f"[MetaUtil] Base64 디코딩 실패: {e_b64}")

            if src_img is None and clean_source:
                if os.path.exists(clean_source):
                    src_img = Image.open(clean_source)
                elif clean_source.startswith(('http://', 'https://')):
                    src_img = SiteAvBase.imopen(clean_source)

            if src_img is None:
                return False, '저장할 원본 이미지를 로컬 또는 원격지에서 열 수 없습니다.'

            target_filename = f"{stem}_{target_type}_user.jpg"
            target_filepath = os.path.join(target_folder, target_filename)

            cls.save_normalized_jpeg(src_img, target_filepath)
            src_img.close()

            for ext_cand in ['jpg', 'jpeg', 'png', 'webp']:
                old_file = os.path.join(target_folder, f"{stem}_{target_type}.{ext_cand}")
                if os.path.exists(old_file):
                    try: os.remove(old_file)
                    except Exception: pass

            module_prefix = (
                'western' if std_cat == 'WESTERN'
                else 'jav_uncensored' if std_cat == 'JAV_UNCEN'
                else 'jav_censored'
            )
            image_mode = P.ModelSetting.get(f"{module_prefix}_image_mode") or 'image_server'
            gdrive_fileid = ''
            if image_mode == 'gdrive':
                remote_relative_path = os.path.join(
                    relative_dir, target_filename
                ).replace('\\', '/').lstrip('/')
                gdrive_fileid = cls.upload_image_to_gdrive(
                    target_filepath, remote_relative_path, category=std_cat
                ) or ''
                if not gdrive_fileid:
                    return False, '구글 드라이브 이미지 업로드에 실패했습니다.'
                new_url = cls.format_gdrive_thumbnail_url(gdrive_fileid)
            else:
                remote_relative_path = target_filename
                new_url = f"{server_url_prefix}/{target_filename}"

            orig_dict = copy.deepcopy(item.original) if isinstance(item.original, dict) else {}
            orig_thumb = orig_dict.get('thumb')
            if not isinstance(orig_thumb, dict):
                orig_thumb = {}

            media_type_name = 'poster' if target_type == 'p' else 'landscape'

            if target_type == 'p':
                item.poster_url = new_url
                if clean_source and clean_source.startswith(('http://', 'https://')) and not orig_thumb.get('poster'):
                    orig_thumb['poster'] = clean_source
            else:
                if not item.poster_url:
                    item.poster_url = new_url
                if clean_source and clean_source.startswith(('http://', 'https://')) and not orig_thumb.get('landscape'):
                    orig_thumb['landscape'] = clean_source

            orig_dict['thumb'] = orig_thumb
            item.original = orig_dict
            flag_modified(item, 'original')

            media_rec = next((m for m in item.media_files if m.media_type == media_type_name and cls.media_in_storage(m, image_mode)), None)
            if media_rec:
                media_rec.url = new_url
                media_rec.is_user = True
                media_rec.gdrive_fileid = gdrive_fileid or None
                media_rec.relative_path = remote_relative_path
                media_rec.source = 'user'
                media_rec.updated_time = datetime.now()
            else:
                sort_idx = 0 if target_type == 'p' else 1
                item.media_files.append(MetaMedia(
                    media_type=media_type_name, url=new_url, is_user=True,
                    sort_order=sort_idx, gdrive_fileid=gdrive_fileid or None,
                    relative_path=remote_relative_path, source='user',
                    updated_time=datetime.now()
                ))

            if image_mode != 'gdrive':
                cls.publish_user_workspace(target_folder, final_folder)
            item.updated_time = datetime.now()
            sess.add(item)
            sess.commit()
            ModuleMetaDb.checkpoint_wal()

            logger.info(f"[MetaUtil] 원본 이미지 직접 저장 및 DB 즉시 반영 완료: [{std_cat}] {item.code} ➔ {target_filename}")
            return True, {
                'new_url': new_url,
                'target_type': target_type,
                'code': item.code,
                'poster_url': item.poster_url
            }

        except Exception as e:
            logger.error(f"[MetaUtil] save_direct_user_image 오류 ({code}): {e}")
            logger.error(traceback.format_exc())
            sess.rollback()
            return False, str(e)
        finally:
            if workspace is not None: workspace.cleanup()
            sess.remove()

    @classmethod
    def save_user_cropped_person_image(cls, person_identifier, crop_data_or_base64, image_base64_data=None, image_url=None, domain='JAV'):
        import shutil
        from .mod_meta_db import ModuleMetaDb, MetaPerson
        from support_site import SiteAvBase

        logger.info(f"[MetaUtil] 인물 프로필 사진 저장 프로세스 시작 -> 대상 식별자: '{person_identifier}', 도메인: '{domain}'")

        sess, _, _ = ModuleMetaDb.get_session_and_domain('PERSON')
        if not sess:
            logger.error("[MetaUtil] 인물 DB 세션 획득에 실패했습니다.")
            return False, "인물 DB 세션 생성 실패", None

        tmp_actor_file = None
        try:
            target_str = str(person_identifier).strip()
            p_rec = None
            if target_str.isdigit():
                p_rec = sess.query(MetaPerson).filter_by(id=int(target_str)).first()
            if not p_rec and target_str:
                p_rec = sess.query(MetaPerson).filter_by(person_idx=target_str).first()
            if not p_rec:
                logger.warning(f"[MetaUtil] 인물 DB에서 대상을 찾을 수 없음: '{person_identifier}'")
                return False, f"대상 인물을 찾을 수 없습니다: {person_identifier}", None

            dom = (p_rec.domain or domain or 'JAV').upper()
            pfx = 'western' if dom == 'WESTERN' else 'jav_censored'
            actor_img_mode = P.ModelSetting.get(f"{pfx}_actor_image_mode") or ('site' if dom == 'WESTERN' else 'gds')
            is_gdrive_mode = (actor_img_mode == 'gdrive')

            root_path = P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data, 'images')
            server_url = (P.ModelSetting.get('jav_censored_image_server_url') or f"{F.SystemModelSetting.get('ddns')}/images").rstrip('/')

            actor_sub_path = (
                P.ModelSetting.get(f"{pfx}_image_server_actor_path") or
                f"/{dom.lower()}/actors"
            ).strip('/\\')

            if not is_gdrive_mode and (not root_path or not server_url):
                logger.error("[MetaUtil] 이미지 서버 로컬 경로 또는 URL 설정이 비어있어 저장을 중단합니다.")
                return False, "이미지 서버 로컬 경로 또는 URL 설정이 비어있습니다.", None

            def clean_name(n):
                return re.sub(r'[^\w가-힣-]', '_', re.sub(r'\s+', '_', str(n or '').strip()))

            kor_name = p_rec.name_ko or ''
            org_name = p_rec.name_org or ''
            eng_name = p_rec.name_en or ''
            clean_idx = clean_name(p_rec.person_idx or str(p_rec.id))

            if dom == 'WESTERN':
                base_name = eng_name or org_name or kor_name or 'Actor'
                clean_base = clean_name(base_name)
                filename_base = f"{clean_base}_{clean_idx}_user.jpg" if clean_idx else f"{clean_base}_user.jpg"
                first_char = base_name[0] if base_name else '#'
                sub_folder = SiteAvBase._get_actor_folder_name_western(first_char)
            else:
                first_char = kor_name[0] if kor_name else (org_name[0] if org_name else '#')
                sub_folder = SiteAvBase._get_actor_folder_name(first_char)
                clean_k = clean_name(kor_name)
                clean_o = clean_name(org_name)
                name_part = clean_k or clean_o
                if clean_o and clean_o != clean_k:
                    name_part += f"_({clean_o})"
                filename_base = f"{name_part}_{clean_idx}_user.jpg" if clean_idx else f"{name_part}_user.jpg"

            pure_sub_rel = f"{sub_folder}/{filename_base}"
            relative_path = f"{actor_sub_path}/{pure_sub_rel}".lstrip('/')
            target_filepath = os.path.join(root_path, relative_path.replace('/', os.path.sep))

            src_img = None
            is_pre_cropped_canvas = False

            if image_base64_data:
                try:
                    raw_b64 = image_base64_data.split(',', 1)[1] if ',' in image_base64_data else image_base64_data
                    src_img = Image.open(BytesIO(base64.b64decode(raw_b64)))
                    is_pre_cropped_canvas = True
                except Exception as e_b64:
                    logger.warning(f"[MetaUtil] Base64 디코딩 실패: {e_b64}")

            if src_img is None and image_url:
                src_img = SiteAvBase.imopen(image_url)

            if src_img is None:
                current_active_thumb = ModuleMetaDb.resolve_person_active_thumb(p_rec)
                if current_active_thumb:
                    src_img = SiteAvBase.imopen(current_active_thumb)

            if src_img is None and p_rec.media_src:
                m_src = ModuleMetaDb.person_media(p_rec)
                candidate_values = [
                    ('site_img_url', m_src['official'].get('site_img_url')),
                    ('local_img_path', m_src['official'].get('local_img_path')),
                    ('custom_user_google_fileid', m_src['custom'].get('custom_user_google_fileid')),
                    ('google_fileid', m_src['official'].get('google_fileid')),
                ]
                for key_cand, cand_val in candidate_values:
                    if cand_val:
                        if key_cand in ['google_fileid', 'custom_user_google_fileid']:
                            cand_url = f"https://drive.google.com/thumbnail?id={cand_val}&sz=s0"
                            src_img = SiteAvBase.imopen(cand_url)
                        else:
                            src_img = SiteAvBase.imopen(cand_val)
                        if src_img is not None:
                            break

            if src_img is None:
                logger.error(f"[MetaUtil] 원본 소스 이미지 로드 실패: '{person_identifier}'")
                return False, "처리할 원본 이미지를 찾을 수 없습니다.", None

            cropped_img = None
            if not is_pre_cropped_canvas:
                try:
                    crop_info = json.loads(crop_data_or_base64) if isinstance(crop_data_or_base64, str) and crop_data_or_base64.startswith('{') else None
                    if crop_info and 'width' in crop_info and 'height' in crop_info:
                        rotate_angle = crop_info.get('rotate', 0)
                        working_img = src_img
                        if rotate_angle != 0:
                            working_img = src_img.rotate(-rotate_angle, expand=True)

                        img_w, img_h = working_img.size
                        cx = max(0, int(round(crop_info['x'])))
                        cy = max(0, int(round(crop_info['y'])))
                        cw = min(int(round(crop_info['width'])), img_w - cx)
                        ch = min(int(round(crop_info['height'])), img_h - cy)
                        cropped_img = working_img.crop((cx, cy, cx + cw, cy + ch))
                except Exception as e_crop_parse:
                    logger.warning(f"[MetaUtil] 크롭 연산 예외: {e_crop_parse}")

            if cropped_img is None:
                cropped_img = src_img

            tmp_dir = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_actor_file = os.path.join(tmp_dir, f"tmp_user_act_{int(time.time())}_{filename_base}")

            cls.save_normalized_jpeg(cropped_img, tmp_actor_file)
            cropped_img.close()
            src_img.close()

            final_thumb_url = ""
            user_gid = ""

            if is_gdrive_mode:
                gdrive_id = cls.upload_image_to_gdrive(tmp_actor_file, relative_path, category=dom)
                if gdrive_id:
                    user_gid = gdrive_id
                    final_thumb_url = cls.format_gdrive_thumbnail_url(gdrive_id)
                    logger.info(f"[MetaUtil] 인물 사진 구글 드라이브 다이렉트 업로드 성공: {p_rec.person_idx} -> {gdrive_id}")
                else:
                    logger.error(f"[MetaUtil] 인물 사진 구글 드라이브 업로드 실패: {p_rec.person_idx}")
                    return False, "구글 드라이브 업로드에 실패했습니다.", None
            else:
                os.makedirs(os.path.dirname(target_filepath), exist_ok=True)
                SiteAvBase.publish_image_file(tmp_actor_file, target_filepath)
                final_thumb_url = f"{server_url}/{relative_path.lstrip('/')}"
                logger.info(f"[MetaUtil] 인물 사진 로컬 이미지 서버 저장 완료: {target_filepath}")

            media_dict = ModuleMetaDb.person_media(p_rec)
            if is_gdrive_mode:
                media_dict['custom']['remote_img_path'] = relative_path
            else:
                media_dict['custom']['local_img_path'] = pure_sub_rel
            if dom not in ('JAV', 'WESTERN') and not is_gdrive_mode and final_thumb_url:
                media_dict['custom']['local_img_url'] = final_thumb_url
            if user_gid:
                media_dict['custom']['custom_user_google_fileid'] = user_gid

            p_rec.media_src = media_dict
            p_rec.thumb = final_thumb_url

            extra_dict = copy.deepcopy(p_rec.extra_info or {})
            if dom in ('JAV', 'WESTERN'):
                extra_dict.pop('local_img_url', None)
            elif not is_gdrive_mode and final_thumb_url:
                extra_dict['local_img_url'] = final_thumb_url
            p_rec.extra_info = extra_dict

            ModuleMetaDb.sync_person_media_columns(p_rec)
            if user_gid:
                p_rec.custom_gdrive_fileid = user_gid
            p_rec.thumb_url = final_thumb_url
            p_rec.has_photo = True

            sess.commit()
            ModuleMetaDb.checkpoint_wal()

            logger.info(f"[MetaUtil] 인물 사진 저장 성공 완료 -> ID: {p_rec.person_idx}, URL: '{final_thumb_url}'")
            return True, final_thumb_url, p_rec

        except Exception as e:
            sess.rollback()
            logger.error(f"[MetaUtil] save_user_cropped_person_image 오류 ({person_identifier}): {e}")
            logger.error(traceback.format_exc())
            return False, str(e), None
        finally:
            if tmp_actor_file and os.path.exists(tmp_actor_file):
                try: os.remove(tmp_actor_file)
                except Exception: pass
            sess.remove()

    @classmethod
    def _register_asset_rollback(cls, person, undo):
        from sqlalchemy import event
        from sqlalchemy.orm import object_session
        session = object_session(person)
        if session is None:
            return
        if not session.info.get('av_asset_hooks'):
            def rolled_back(current):
                for operation in reversed(current.info.pop('av_asset_undo', [])):
                    try:
                        operation()
                    except Exception as exc:
                        logger.error(f'[MetaUtil] 이미지 이동 롤백 실패: {exc}')
            def committed(current):
                current.info.pop('av_asset_undo', None)
            event.listen(session, 'after_rollback', rolled_back)
            event.listen(session, 'after_commit', committed)
            session.info['av_asset_hooks'] = True
        session.info.setdefault('av_asset_undo', []).append(undo)

    @classmethod
    def sync_renamed_person_user_image(cls, p_rec, old_name_ko, new_name_ko, old_local_path, new_local_path, actor_id):
        from .mod_meta_db import ModuleMetaDb
        from .util_preview import MetaPreviewUtil
        from support_site import SiteAvBase
        domain = str(p_rec.domain or 'JAV').upper()
        prefix = 'western' if domain == 'WESTERN' else 'jav_censored'
        mode = P.ModelSetting.get(f'{prefix}_actor_image_mode') or ('site' if domain == 'WESTERN' else 'gds')
        if mode not in ('image_server', 'gdrive'):
            return None
        custom = ModuleMetaDb.person_media(p_rec)['custom']
        path_key = 'remote_img_path' if mode == 'gdrive' else 'local_img_path'
        old_path = str(custom.get(path_key) or '').replace('\\', '/').strip('/')
        if not old_path or '..' in old_path.split('/'):
            # Ambiguous legacy cloud paths are repaired only by an explicit future batch.
            return None
        clean = lambda value: re.sub(r'[^\w가-힣()-]', '_', str(value or '').strip())
        name = clean(new_name_ko or p_rec.name_org)
        original = clean(p_rec.name_org)
        if domain != 'WESTERN' and original and original != name:
            name += f'_({original})'
        filename = f'{name}_{clean(actor_id)}_user{os.path.splitext(old_path)[1] or ".jpg"}'
        folder = SiteAvBase._get_actor_folder_name_western((new_name_ko or '#')[0]) if domain == 'WESTERN' else SiteAvBase._get_actor_folder_name((new_name_ko or '#')[0])
        actor_sub = (P.ModelSetting.get(f'{prefix}_image_server_actor_path') or f'/{domain.lower()}/actors').strip('/\\')
        new_path = f'{actor_sub}/{folder}/{filename}' if mode == 'gdrive' else f'{folder}/{filename}'
        if old_path == new_path:
            return None
        if mode == 'image_server':
            root = P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data, 'images')
            base = os.path.abspath(os.path.join(root, actor_sub))
            source = os.path.abspath(os.path.join(base, old_path))
            destination = os.path.abspath(os.path.join(base, new_path))
            if os.path.commonpath([base, source, destination]) != base or not os.path.isfile(source):
                return None
            if os.path.exists(destination):
                raise FileExistsError(f'User image already exists: {new_path}')
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.move(source, destination)
            def undo_local():
                if os.path.exists(destination) and not os.path.exists(source):
                    os.makedirs(os.path.dirname(source), exist_ok=True)
                    shutil.move(destination, source)
            cls._register_asset_rollback(p_rec, undo_local)
        else:
            if not custom.get('custom_user_google_fileid'):
                return None
            remote = P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or ''
            old_remote = MetaPreviewUtil.build_rclone_target_remote_path(remote, old_path)
            new_remote = MetaPreviewUtil.build_rclone_target_remote_path(remote, new_path)
            if not old_remote or not new_remote:
                return None
            if MetaPreviewUtil.get_remote_file_id(new_path, category=domain, is_image=True):
                raise FileExistsError(f'Remote user image already exists: {new_path}')
            rclone = MetaPreviewUtil.get_setting('rclone_path', domain, default='rclone')
            config = MetaPreviewUtil.get_setting('rclone_conf', domain, default='/data/db/rclone.conf')
            result = subprocess.run([rclone, '--config', config, 'moveto', old_remote, new_remote],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError(f'User image rename failed: {result.stderr[-200:]}')
            file_id = MetaPreviewUtil.get_remote_file_id(new_path, category=domain, is_image=True)
            if not file_id:
                # Restore the name if the new asset cannot be verified.
                subprocess.run([rclone, '--config', config, 'moveto', new_remote, old_remote], timeout=60)
                raise RuntimeError('Cannot verify renamed user image')
            def undo_remote():
                undone = subprocess.run([rclone, '--config', config, 'moveto', new_remote, old_remote], timeout=60)
                if undone.returncode:
                    raise RuntimeError('Remote rename rollback failed')
                MetaPreviewUtil.clear_dir_cache()
            cls._register_asset_rollback(p_rec, undo_remote)
            MetaPreviewUtil.clear_dir_cache()
            return {path_key: new_path, 'custom_user_google_fileid': file_id}
        return {path_key: new_path}

    @classmethod
    def delete_item_user_images(cls, item):
        from .util_preview import MetaPreviewUtil
        prefix = 'western' if item.category == 'WESTERN' else ('jav_uncensored' if item.category == 'JAV_UNCEN' else 'jav_censored')
        mode = P.ModelSetting.get(f'{prefix}_image_mode') or 'ff_proxy'
        if mode == 'gdrive':
            remote = P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or ''
            assets = [m for m in item.media_files if m.is_user and m.gdrive_fileid]
            # Do not guess the location of legacy assets using local file names.
            if any(not m.relative_path or os.path.isabs(m.relative_path) or '..' in m.relative_path.replace('\\', '/').split('/') for m in assets):
                raise ValueError('사용자 GDrive 이미지의 원격 상대경로를 먼저 확인해야 합니다.')
            for media in assets:
                target = MetaPreviewUtil.build_rclone_target_remote_path(remote, media.relative_path)
                if not target:
                    raise ValueError('GDrive 리모트 Root가 없습니다.')
                result = subprocess.run([
                    MetaPreviewUtil.get_setting('rclone_path', item.category, 'rclone'), '--config',
                    MetaPreviewUtil.get_setting('rclone_conf', item.category, '/data/db/rclone.conf'),
                    'deletefile', target], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
                if result.returncode:
                    raise RuntimeError(f'사용자 GDrive 이미지 삭제 실패: {result.stderr[-200:]}')
            MetaPreviewUtil.clear_dir_cache()
        elif mode == 'image_server':
            stem = (item.code or item.ui_code).lower() if item.category == 'WESTERN' else (item.ui_code or item.originaltitle or item.code).lower()
            folder, _ = cls.get_server_folder_and_prefix(item.domain, item.category, stem, studio=item.studio, year=item.year)
            if folder and os.path.isdir(folder):
                for filename in os.listdir(folder):
                    if filename.startswith((f'{stem}_p_user.', f'{stem}_pl_user.')):
                        os.remove(os.path.join(folder, filename))

    @classmethod
    def delete_person_custom_image(cls, p_rec):
        """사용자 custom 이미지의 로컬/원격 자산을 삭제하고 실패를 호출자에게 반환한다."""
        from .mod_meta_db import ModuleMetaDb
        from .util_preview import MetaPreviewUtil

        media = ModuleMetaDb.person_media(p_rec)
        custom = media['custom']
        local_rel = str(custom.get('local_img_path') or '').strip()
        remote_rel = str(custom.get('remote_img_path') or '').strip()
        gdrive_id = str(custom.get('custom_user_google_fileid') or '').strip()
        errors = []
        if gdrive_id and (not remote_rel or os.path.isabs(remote_rel) or '..' in remote_rel.replace('\\', '/').split('/')):
            return False, '사용자 GDrive 이미지의 원격 상대경로를 먼저 확인해야 합니다.'

        pfx = 'western' if str(p_rec.domain).upper() == 'WESTERN' else 'jav_censored'
        actor_path = (
            P.ModelSetting.get(f'{pfx}_image_server_actor_path') or
            f'/{str(p_rec.domain).lower()}/actors'
        ).strip('/\\')

        if local_rel:
            root = (
                P.ModelSetting.get('jav_censored_image_server_local_path') or
                os.path.join(path_data, 'images')
            ).rstrip('/\\')
            local_file = os.path.join(root, actor_path.replace('/', os.path.sep), local_rel.replace('/', os.path.sep))
            if os.path.exists(local_file):
                try:
                    os.remove(local_file)
                    logger.info(f"[MetaUtil] 인물 custom 로컬 이미지 삭제: {local_file}")
                except OSError as exc:
                    errors.append(f"로컬 파일 삭제 실패: {exc}")

        if gdrive_id:
            cat = str(p_rec.domain or 'JAV').upper()
            pfx = 'western' if cat == 'WESTERN' else 'jav_censored'
            remote = (
                P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or ''
            ).strip()
            if not remote or not remote_rel:
                errors.append("GDrive custom 이미지의 원격 경로 정보가 없습니다.")
            else:
                rclone = MetaPreviewUtil.get_setting('rclone_path', cat, default='rclone')
                config = MetaPreviewUtil.get_setting('rclone_conf', cat, default='/data/db/rclone.conf')
                remote_file = MetaPreviewUtil.build_rclone_target_remote_path(remote, dest_rel_path=remote_rel)
                if not remote_file:
                    errors.append("GDrive custom 이미지의 원격 경로를 구성할 수 없습니다.")
                    remote_file = None
                if not remote_file:
                    return False, '; '.join(errors)
                try:
                    result = subprocess.run(
                        [rclone, '--config', config, 'deletefile', remote_file],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30
                    )
                    if result.returncode != 0:
                        errors.append(f"GDrive 파일 삭제 실패: {result.stderr[-200:]}")
                    else:
                        for attempt in range(1, 11):
                            probe = subprocess.run(
                                [rclone, '--config', config, 'lsjson', '--stat', remote_file],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25
                            )
                            if probe.returncode != 0 or not probe.stdout.strip():
                                logger.info(f"[MetaUtil] 인물 custom GDrive 이미지 삭제 확인 ({attempt}회): {remote_file}")
                                break
                            time.sleep(min(2.0, attempt * 0.4))
                        else:
                            errors.append("GDrive 파일 삭제 후 원격 목록에서 사라지지 않았습니다.")
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(f"GDrive 삭제 명령 실패: {exc}")

        if errors:
            logger.error(f"[MetaUtil] 인물 custom 이미지 삭제 실패: {errors}")
            return False, '; '.join(errors)

        media['custom'] = {'local_img_path': '', 'remote_img_path': '', 'custom_user_google_fileid': ''}
        p_rec.media_src = media
        ModuleMetaDb.sync_person_media_columns(p_rec)
        p_rec.thumb_url = ModuleMetaDb.resolve_person_active_thumb(p_rec)
        p_rec.has_photo = bool(p_rec.thumb_url and str(p_rec.thumb_url).strip())

        return True, ""

    @classmethod
    def sync_renamed_person_gdrive_image(cls, p_rec, old_local_path, new_local_path):
        return cls.sync_renamed_person_user_image(
            p_rec, p_rec.name_ko, p_rec.name_ko or p_rec.name_org,
            old_local_path, new_local_path, p_rec.person_idx
        )

    @classmethod
    def sync_single_record_disk_images(cls, meta_record, custom_root_path=None):
        from .mod_meta_db import MetaMedia
        prefix = 'western' if meta_record.category == 'WESTERN' else 'jav_uncensored' if meta_record.category == 'JAV_UNCEN' else 'jav_censored'
        mode = P.ModelSetting.get(f'{prefix}_image_mode') or P.ModelSetting.get('jav_censored_image_mode')
        if mode != 'image_server' and not custom_root_path:
            return 'not_local', False
        try:
            root = custom_root_path or P.ModelSetting.get('jav_censored_image_server_local_path') or '/data/images'
            root = os.path.abspath(root)
            server = str(P.ModelSetting.get('jav_censored_image_server_url') or f"{F.SystemModelSetting.get('ddns')}/images").rstrip('/')
            stem = ((meta_record.code or meta_record.ui_code) if meta_record.category == 'WESTERN' else
                    (meta_record.ui_code or meta_record.originaltitle or meta_record.code) or '').lower()
            folder, _ = cls.get_server_folder_and_prefix(meta_record.domain, meta_record.category, stem,
                studio=meta_record.studio or '', year=meta_record.year or 1900)
            default_root = P.ModelSetting.get('jav_censored_image_server_local_path') or '/data/images'
            if custom_root_path and folder:
                folder = os.path.join(root, os.path.relpath(folder, default_root))
            assets = {}
            local_rows = [m for m in meta_record.media_files if m.media_type in ('poster', 'landscape', 'fanart') and cls.media_in_storage(m, 'image_server')]
            for media in local_rows:
                relative = str(media.relative_path or '').replace('\\', '/')
                if not relative or relative.startswith('/') or '..' in relative.split('/'):
                    continue
                candidate = os.path.join(root, relative)
                if not os.path.isfile(candidate) and '/' not in relative and folder:
                    candidate = os.path.join(folder, relative)
                if os.path.isfile(candidate):
                    order = 0 if media.media_type == 'poster' else 1 if media.media_type == 'landscape' else media.sort_order or 0
                    assets[(media.media_type, cls.is_user_media(media), order)] = (candidate, media)
            if folder and os.path.isdir(folder):
                for filename in sorted(os.listdir(folder)):
                    low = filename.lower()
                    matched = re.fullmatch(re.escape(stem) + r'_(p|pl|art_(\d+))(_user)?\.(jpg|jpeg|png|webp)', low)
                    if not matched:
                        continue
                    kind = 'poster' if matched[1] == 'p' else 'landscape' if matched[1] == 'pl' else 'fanart'
                    order = 0 if kind == 'poster' else 1 if kind == 'landscape' else int(matched[2]) - 1
                    key = (kind, bool(matched[3]), order)
                    assets.setdefault(key, (os.path.join(folder, filename), None))
            modified = False
            for (kind, is_user, order), (fullpath, existing) in assets.items():
                relative = os.path.relpath(fullpath, root).replace('\\', '/')
                if relative.startswith('../'):
                    continue
                url = f'{server}/{relative}'
                media = existing or next((m for m in local_rows if m.media_type == kind and cls.is_user_media(m) == is_user and (0 if m.media_type == 'poster' else 1 if m.media_type == 'landscape' else m.sort_order or 0) == order), None)
                if media is None:
                    media = MetaMedia(media_type=kind, is_user=is_user, sort_order=order)
                    meta_record.media_files.append(media)
                    modified = True
                for key, value in (('is_user', is_user), ('sort_order', order), ('url', url), ('relative_path', relative), ('source', 'user' if is_user else 'site')):
                    if getattr(media, key) != value:
                        setattr(media, key, value)
                        modified = True
            if mode == 'image_server':
                for kind in ('poster', 'landscape'):
                    found = [(key, pair) for key, pair in assets.items() if key[0] == kind]
                    if not found:
                        continue
                    key, (fullpath, _) = max(found, key=lambda pair: pair[0][1])
                    url = f"{server}/{os.path.relpath(fullpath, root).replace(os.path.sep, '/')}"
                    if kind == 'poster' and meta_record.poster_url != url:
                        meta_record.poster_url = url
                        modified = True
                    if kind == 'landscape':
                        extra = dict(meta_record.extra_info or {})
                        if extra.get('landscape_url') != url:
                            extra['landscape_url'] = url
                            meta_record.extra_info = extra
                            modified = True
            if modified:
                meta_record.updated_time = datetime.now()
            missing = any(not any(key[0] == kind for key in assets) for kind in ('poster', 'landscape'))
            return ('updated' if modified else 'synced'), missing
        except Exception as exc:
            logger.error(f'[MetaUtil] 로컬 이미지 동기화 실패: {exc}')
            return 'error', False

    @classmethod
    def finalize_info_return(cls, entity_dict, extra_opts=None, category=None):
        """DB 사용 여부와 무관하게 호출자에게 반환할 최종 데이터의 사본을 안전하게 가공"""
        if not entity_dict or not isinstance(entity_dict, dict):
            return entity_dict

        opts = dict(extra_opts or {})
        res = copy.deepcopy(entity_dict)

        thumbs = res.get('thumb') or []
        has_poster = any(isinstance(t, dict) and t.get('aspect') == 'poster' and t.get('value') for t in thumbs)
        if not has_poster:
            pl_thumb = next((t for t in thumbs if isinstance(t, dict) and t.get('aspect') == 'landscape' and t.get('value')), None)
            if pl_thumb:
                fallback_poster = copy.deepcopy(pl_thumb)
                fallback_poster['aspect'] = 'poster'
                res.setdefault('thumb', []).append(fallback_poster)

        current_plot = str(res.get('plot') or '').strip()
        fallback_tagline = str(res.get('tagline') or '').strip()
        if not current_plot and fallback_tagline:
            res['plot'] = fallback_tagline

        # 내부 저장 및 갱신 파이프라인 호출일 때는 프록시 래핑을 건너뛰고 순수 원본 데이터 반환
        if opts.get('for_db') or opts.get('internal'):
            return res

        # 외부 API 호출 및 웹 화면 출력 시에만 프록시 래핑 적용
        try:
            from .mod_meta_db import ModuleMetaDb
            res = ModuleMetaDb.apply_transient_overrides(res, opts, category=category)
        except Exception as e_override:
            logger.debug(f"[MetaUtil] apply_transient_overrides 예외: {e_override}")

        if opts.get('strip_images'):
            res['poster_url'] = ''
            res['landscape_url'] = ''
            res['thumb'] = []
            res['fanart'] = []

        return res

    @classmethod
    def refresh_image_only(cls, module, code, category, site_activity=None):
        """미디어(포스터/커버/팬아트/트레일러) 전용 재동기화 단일 공용 로직"""
        from .mod_meta_db import ModuleMetaDb

        cached_json = ModuleMetaDb.get_metadata(code, category=category)
        if not cached_json:
            return {'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

        ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
        title_log = str(cached_json.get('title') or ui_code)[:35]
        logger.info(f"[{module.name}] [{category}] 미디어 전용 재동기화 시작: [{code}] ({ui_code})")

        try:
            if hasattr(module, 'keyword_cache') and module.keyword_cache:
                module.keyword_cache.set(f"BYPASS_{code}", "1")
        except Exception:
            pass

        # for_db=True 플래그를 전달하여 프록시 래핑 없는 순수 원본 미디어 취득
        info_opts = {'skip_trans': True, 'for_db': True}
        if site_activity is not None:
            info_opts['_batch_site_activity'] = site_activity
        try:
            fresh_media = module.info(code, keyword=ui_code, extra_opts=info_opts)
        except TypeError:
            fresh_media = module.info(code, extra_opts=info_opts)

        if not fresh_media or (not fresh_media.get('thumb') and not (fresh_media.get('original') or {}).get('thumb')):
            return {'ret': 'warning', 'msg': f"[{ui_code}] 원격 사이트에서 미디어 정보를 가져오지 못했습니다.", 'title_log': title_log, 'is_updated': False}

        cached_json['thumb'] = fresh_media.get('thumb', [])
        cached_json['fanart'] = fresh_media.get('fanart', [])

        # fresh_media의 thumb 리스트에서 최신 포스터와 랜드스케이프를 직접 추출하여 확정 갱신
        fresh_p = next((t['value'] for t in cached_json['thumb'] if isinstance(t, dict) and t.get('aspect') == 'poster' and t.get('value')), '')
        fresh_pl = next((t['value'] for t in cached_json['thumb'] if isinstance(t, dict) and t.get('aspect') == 'landscape' and t.get('value')), '')

        if fresh_p:
            cached_json['poster_url'] = fresh_p
            if 'extra_info' not in cached_json or not isinstance(cached_json['extra_info'], dict):
                cached_json['extra_info'] = {}
            cached_json['extra_info']['poster_url'] = fresh_p

        if fresh_pl:
            cached_json['landscape_url'] = fresh_pl
            if 'extra_info' not in cached_json or not isinstance(cached_json['extra_info'], dict):
                cached_json['extra_info'] = {}
            cached_json['extra_info']['landscape_url'] = fresh_pl

        if fresh_media.get('extra_info'):
            if 'extra_info' not in cached_json or not isinstance(cached_json['extra_info'], dict):
                cached_json['extra_info'] = {}
            for k_ei in ['landscape_url', 'poster_url', 'trailer_video', '_storage_media']:
                if fresh_media['extra_info'].get(k_ei):
                    cached_json['extra_info'][k_ei] = fresh_media['extra_info'][k_ei]

        if isinstance(fresh_media.get('media'), list):
            cached_json['media'] = copy.deepcopy(fresh_media['media'])

        if fresh_media.get('original'):
            fresh_orig = fresh_media['original']
            if 'original' not in cached_json or not isinstance(cached_json['original'], dict):
                cached_json['original'] = {}
            if fresh_orig.get('thumb'):
                cached_json['original']['thumb'] = fresh_orig['thumb']
            if fresh_orig.get('fanart'):
                cached_json['original']['fanart'] = fresh_orig['fanart']
            if isinstance(fresh_orig.get('extras'), list):
                if not fresh_orig['extras']:
                    from .util_preview import MetaPreviewUtil
                    old_original = cached_json['original']
                    old_urls = [entry.get('content_url') for entry in old_original.get('extras') or []
                                if isinstance(entry, dict) and str(entry.get('content_type') or '').lower() == 'trailer']
                    old_urls += [old_original.get('trailer'), old_original.get('trailer_url')]
                    for old_url in old_urls:
                        if old_url:
                            MetaPreviewUtil.remove_official_trailer(cached_json, old_url)
                cached_json['original']['extras'] = copy.deepcopy(fresh_orig['extras'])

        if fresh_media.get('extras'):
            cached_json['extras'] = fresh_media.get('extras', [])

        if not ModuleMetaDb.save_metadata(category, cached_json):
            return {'ret': 'error', 'msg': '이미지 및 미디어 정보를 DB에 저장하지 못했습니다.', 'is_updated': False}
        # Return the persisted assets, including paths and inactive storage rows.
        cached_json = ModuleMetaDb.get_metadata(code, category=category) or cached_json
        logger.info(f"[{module.name}] [{code}] 이미지 및 미디어 주소 갱신 완료")
        return {'ret': 'success', 'msg': f"[{ui_code}] 이미지 및 미디어 동기화 완료", 'title_log': title_log, 'is_updated': True, 'data': cached_json}

    @classmethod
    def refresh_in_place(cls, module, code, category, site_activity=None):
        """현재 출처 사이트 정보 제자리 갱신 단일 공용 로직"""
        from .mod_meta_db import ModuleMetaDb

        cached_json = ModuleMetaDb.get_metadata(code, category=category)
        if not cached_json:
            return {'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

        ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
        logger.info(f"[{module.name}] [{category}] 현재 사이트 제자리 갱신 시작: [{code}] ({ui_code})")

        try:
            if hasattr(module, 'keyword_cache') and module.keyword_cache:
                module.keyword_cache.set(f"BYPASS_{code}", "1")
        except Exception:
            pass

        info_opts = {'skip_trans': False, 'for_db': True}
        if site_activity is not None:
            info_opts['_batch_site_activity'] = site_activity
        try:
            fresh_data = module.info(code, keyword=ui_code, extra_opts=info_opts)
        except TypeError:
            fresh_data = module.info(code, extra_opts=info_opts)

        if not fresh_data:
            return {'ret': 'warning', 'msg': f"[{ui_code}] 출처 사이트에서 정보를 가져오지 못했습니다.", 'is_updated': False}

        ModuleMetaDb.save_metadata(category, fresh_data)
        title_log = fresh_data.get('title', '')
        logger.info(f"[{module.name}] [{code}] 메타데이터 갱신 완료: {title_log}")
        return {'ret': 'success', 'msg': f"[{ui_code}] 메타데이터 갱신 완료!\n{title_log}", 'title_log': title_log, 'is_updated': True, 'data': fresh_data}

    @classmethod
    def refresh_auto_search(cls, module, code, category, site_activity=None):
        """전체 우선순위 자동 재검색 갱신 단일 공용 로직"""
        from .mod_meta_db import ModuleMetaDb

        cached_json = ModuleMetaDb.get_metadata(code, category=category)
        if not cached_json:
            return {'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

        ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
        logger.info(f"[{module.name}] [{category}] 전체 우선순위 자동 재검색 갱신 시작: [{code}] ➔ 키워드: '{ui_code}'")

        if site_activity is not None:
            site_activity['requested'] = True
        search_res = module.search(ui_code, manual=False, use_db=False)
        if not search_res:
            return {'ret': 'warning', 'msg': f"'{ui_code}' 검색 결과가 없습니다.", 'is_updated': False}

        best_item = None
        if category == 'JAV_CEN':
            from support_site import SiteAvBase
            _, target_label, target_num = SiteAvBase._parse_ui_code(ui_code)
            for item in search_res:
                if item.get('is_db_cached'):
                    continue
                cand_ui = str(item.get('ui_code') or '').strip()
                _, c_label, c_num = SiteAvBase._parse_ui_code(cand_ui)
                cand_score = item.get('original_score', item.get('score', 0))
                if cand_score >= 99 and target_label == c_label and target_num == c_num:
                    best_item = item
                    break

        if not best_item:
            best_item = next((s for s in search_res if not s.get('is_db_cached') and s.get('score', 0) >= 90), None)

        if not best_item:
            return {'ret': 'warning', 'msg': f"[{ui_code}] 일치하는 메타데이터를 찾지 못했습니다.", 'is_updated': False}

        new_code = best_item['code']
        logger.info(f"[{module.name}] 자동 재검색 채택: [{best_item.get('site_key', '').upper()}] Code: {new_code} (기존: {code})")

        try:
            if hasattr(module, 'keyword_cache') and module.keyword_cache:
                module.keyword_cache.set(f"BYPASS_{new_code}", "1")
                module.keyword_cache.set(f"BYPASS_{code}", "1")
        except Exception:
            pass

        info_opts = {'skip_trans': False, 'for_db': True}
        if site_activity is not None:
            info_opts['_batch_site_activity'] = site_activity
        try:
            fresh_data = module.info(new_code, keyword=ui_code, extra_opts=info_opts)
        except TypeError:
            fresh_data = module.info(new_code, extra_opts=info_opts)

        if not fresh_data:
            return {'ret': 'warning', 'msg': f"[{ui_code}] 상세 정보(Info)를 가져오지 못했습니다.", 'is_updated': False}

        ModuleMetaDb.save_metadata(category, fresh_data)
        title_log = fresh_data.get('title', '')
        site_tag = best_item.get('site_key', '').upper()
        msg = f"[{ui_code}] 자동 재검색 갱신 완료! ({site_tag})\n{title_log}"
        return {'ret': 'success', 'msg': msg, 'title_log': title_log, 'is_updated': True, 'data': fresh_data}

    @classmethod
    def heal_metadata(cls, module, code, category, site_activity=None):
        """누락 정보 지능형 치유 갱신 단일 공용 로직"""
        from .mod_meta_db import ModuleMetaDb
        from support_site import SiteUtil

        cached_json = ModuleMetaDb.get_metadata(code, category=category)
        if not cached_json:
            return {'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

        ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
        title_for_log = str(cached_json.get('title') or ui_code).strip()
        if len(title_for_log) > 40:
            title_for_log = title_for_log[:37] + '...'

        cat_upper = str(category or '').upper()
        is_western = (cat_upper in ['WESTERN', 'WEST'])

        existing_title = str(cached_json.get('title') or '').strip()
        existing_tagline = str(cached_json.get('tagline') or '').strip()
        existing_plot = str(cached_json.get('plot') or '').strip()

        has_valid_plot = bool(existing_plot)
        plot_is_korean = SiteUtil.is_include_hangul(existing_plot)
        tagline_is_korean = SiteUtil.is_include_hangul(existing_tagline)

        need_trans = False
        if is_western:
            trans_enabled = P.ModelSetting.get_bool('western_trans_title')
            if trans_enabled and has_valid_plot and not plot_is_korean:
                need_trans = True
        else:
            trans_opt = P.ModelSetting.get('jav_censored_trans_option') or 'using'
            if trans_opt != 'not_using':
                if has_valid_plot and not plot_is_korean:
                    need_trans = True
                elif existing_tagline and not tagline_is_korean:
                    need_trans = True

        skip_trans = not need_trans

        ps_url = cached_json.get('original', {}).get('thumb', {}).get('ps_url')
        info_opts = {'skip_trans': skip_trans, 'for_db': True}
        if site_activity is not None:
            info_opts['_batch_site_activity'] = site_activity
        if ps_url:
            info_opts['ps_url'] = ps_url

        try:
            module.keyword_cache.set(f"BYPASS_{code}", "1")
        except Exception:
            pass

        try:
            if is_western or cat_upper == 'JAV_CEN':
                fresh_data = module.info(code, keyword=ui_code, extra_opts=info_opts)
            else:
                fresh_data = module.info(code, extra_opts=info_opts)
        except TypeError:
            fresh_data = module.info(code, extra_opts=info_opts)

        if not fresh_data:
            return {'ret': 'warning', 'msg': '최신 정보를 가져오지 못했습니다.', 'title_log': title_for_log, 'is_updated': False}

        changes = []

        if skip_trans:
            if existing_title: fresh_data['title'] = existing_title
            if existing_tagline: fresh_data['tagline'] = existing_tagline
            if existing_plot: fresh_data['plot'] = existing_plot
        else:
            if not existing_plot and fresh_data.get('plot'):
                changes.append('줄거리 등록')
            elif has_valid_plot and not plot_is_korean and SiteUtil.is_include_hangul(str(fresh_data.get('plot') or '')):
                changes.append('줄거리 번역')
            elif has_valid_plot and plot_is_korean:
                fresh_data['plot'] = existing_plot

            if not existing_tagline and fresh_data.get('tagline'):
                changes.append('부제 등록')
            elif existing_tagline and not tagline_is_korean and SiteUtil.is_include_hangul(str(fresh_data.get('tagline') or '')):
                changes.append('부제 번역')

        cached_actors = cached_json.get('actor') or []
        fresh_actors = fresh_data.get('actor') or []

        if len(cached_actors) == 0 and len(fresh_actors) > 0:
            changes.append(f'인물 등록 {len(fresh_actors)}명')
        elif len(fresh_actors) > len(cached_actors):
            changes.append(f'인물 추가 {len(fresh_actors) - len(cached_actors)}명')
        else:
            cached_ko_count = sum(1 for a in cached_actors if isinstance(a, dict) and a.get('name_ko'))
            fresh_ko_count = sum(1 for a in fresh_actors if isinstance(a, dict) and a.get('name_ko'))
            if fresh_ko_count > cached_ko_count:
                changes.append(f'인물 정보 업데이트 {fresh_ko_count - cached_ko_count}명')

            cached_has_thumb = any(a.get('thumb') for a in cached_actors if isinstance(a, dict))
            fresh_has_thumb = any(a.get('thumb') for a in fresh_actors if isinstance(a, dict))
            if not cached_has_thumb and fresh_has_thumb:
                changes.append('인물 사진 보완')

        cached_orig = cached_json.get('original') or {}
        fresh_orig = fresh_data.get('original') or {}
        if not cached_orig and fresh_orig:
            changes.append('오리지널 메타 보완')
        else:
            if not cached_orig.get('fanart') and fresh_orig.get('fanart'):
                changes.append('팬아트 보완')
            if not cached_orig.get('extras') and fresh_orig.get('extras'):
                changes.append('예고편 보완')

        if not cached_json.get('studio') and fresh_data.get('studio'):
            changes.append('제작사 등록')
        if not cached_json.get('director') and fresh_data.get('director'):
            changes.append('감독 등록')
        if not cached_json.get('series') and fresh_data.get('series'):
            changes.append('시리즈 등록')
        if not cached_json.get('premiered') and fresh_data.get('premiered'):
            changes.append('출시일 등록')

        cached_thumbs = cached_json.get('thumb') or []
        fresh_thumbs = fresh_data.get('thumb') or []

        cached_p = next((t.get('value') for t in cached_thumbs if isinstance(t, dict) and t.get('aspect') == 'poster'), '')
        fresh_p = next((t.get('value') for t in fresh_thumbs if isinstance(t, dict) and t.get('aspect') == 'poster'), '')
        cached_pl = next((t.get('value') for t in cached_thumbs if isinstance(t, dict) and t.get('aspect') == 'landscape'), '')
        fresh_pl = next((t.get('value') for t in fresh_thumbs if isinstance(t, dict) and t.get('aspect') == 'landscape'), '')

        if fresh_p and fresh_p != cached_p:
            changes.append('포스터 GDrive 전환' if 'drive.google.com' in fresh_p and 'drive.google.com' not in cached_p else '포스터 URL 갱신')

        if fresh_pl and fresh_pl != cached_pl:
            changes.append('가로커버 GDrive 전환' if 'drive.google.com' in fresh_pl and 'drive.google.com' not in cached_pl else '가로커버 URL 갱신')

        cached_fanarts = cached_json.get('fanart') or []
        fresh_fanarts = fresh_data.get('fanart') or []
        if fresh_fanarts and set(fresh_fanarts) != set(cached_fanarts):
            changes.append(f'팬아트 갱신 {len(fresh_fanarts)}장')

        cached_extra = cached_json.get('extra_info') or {}
        if 'preview_clip' in cached_extra:
            if 'extra_info' not in fresh_data or not isinstance(fresh_data['extra_info'], dict):
                fresh_data['extra_info'] = {}
            fresh_data['extra_info']['preview_clip'] = cached_extra['preview_clip']
            if 'source_video_path' in cached_extra:
                fresh_data['extra_info']['source_video_path'] = cached_extra['source_video_path']
            if not fresh_data.get('extras') and cached_json.get('extras'):
                preview_extras = [ex for ex in cached_json['extras'] if isinstance(ex, dict) and 'mode=preview_' in str(ex.get('content_url', ''))]
                if preview_extras:
                    fresh_data['extras'] = preview_extras

        cached_tags = cached_json.get('tag') or []
        fresh_tags = fresh_data.get('tag') or []
        for ct in cached_tags:
            if ct and ct not in fresh_tags:
                fresh_tags.append(ct)
        fresh_data['tag'] = fresh_tags

        is_updated = (len(changes) > 0)

        if is_updated:
            ModuleMetaDb.save_metadata(category, fresh_data)

            sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
            if sess:
                try:
                    from .mod_meta_db import MetaItem
                    meta_item = sess.query(MetaItem).filter_by(code=code, category=std_cat).first()
                    if meta_item:
                        old_p = str(meta_item.poster_url or '').strip()
                        old_pl_m = next((m for m in meta_item.media_files if m.media_type == 'landscape'), None)
                        old_pl = str(old_pl_m.url or '').strip() if old_pl_m else ''
                        old_pl_user = bool(old_pl_m.is_user) if old_pl_m else False

                        sync_res, _ = cls.sync_single_record_disk_images(meta_item)
                        if sync_res == 'updated':
                            sess.commit()
                            new_p = str(meta_item.poster_url or '').strip()
                            new_pl_m = next((m for m in meta_item.media_files if m.media_type == 'landscape'), None)
                            new_pl = str(new_pl_m.url or '').strip() if new_pl_m else ''
                            new_pl_user = bool(new_pl_m.is_user) if new_pl_m else False

                            if '_user.' in new_p and '_user.' not in old_p:
                                changes.append('유저 포스터 반영')
                            elif new_p and new_p != old_p:
                                changes.append('포스터 주소 동기화')

                            if new_pl and ('_user.' in new_pl or new_pl_user) and ('_user.' not in old_pl and not old_pl_user):
                                changes.append('유저 가로커버 반영')
                            elif new_pl and new_pl != old_pl:
                                changes.append('가로 커버 동기화')
                except Exception as e_disk_sync:
                    logger.debug(f"[{module.name}] 디스크 이미지 동기화 예외: {e_disk_sync}")
                finally:
                    sess.remove()

        detail_msg = ", ".join(changes) if changes else "변경 없음"
        return {
            'ret': 'success',
            'is_updated': is_updated,
            'title_log': title_for_log,
            'detail_msg': detail_msg,
            'msg': f"처리 완료 ({detail_msg})" if is_updated else "변경 없음",
            'data': fresh_data
        }

    @classmethod
    def sync_local_data(cls, module, code, category, memory_index=None):
        """외부 사이트 접속 없이 로컬 DB와 디스크 파일 상태만 대조하여 동기화"""
        from .mod_meta_db import ModuleMetaDb, MetaItem

        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess:
            return {'ret': 'error', 'msg': '세션 획득 실패', 'is_updated': False}

        try:
            item = sess.query(MetaItem).filter_by(code=code, category=std_cat).first()
            if not item:
                return {'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

            ui_code = item.ui_code or item.originaltitle or item.code
            title_for_log = str(item.title or ui_code).strip()
            if len(title_for_log) > 40:
                title_for_log = title_for_log[:37] + '...'

            changes = []
            is_modified = False

            old_poster_url = str(item.poster_url or '').strip()
            old_pl_media = next((m for m in item.media_files if m.media_type == 'landscape'), None)
            old_pl_url = str(old_pl_media.url or '').strip() if old_pl_media else ''
            old_pl_is_user = bool(old_pl_media.is_user) if old_pl_media else False
            old_has_p_media = any(m.media_type == 'poster' for m in item.media_files)
            old_has_pl_media = bool(old_pl_media)

            sync_res, _ = cls.sync_single_record_disk_images(item)
            if sync_res == 'updated':
                new_poster_url = str(item.poster_url or '').strip()
                new_pl_media = next((m for m in item.media_files if m.media_type == 'landscape'), None)
                new_pl_url = str(new_pl_media.url or '').strip() if new_pl_media else ''
                new_pl_is_user = bool(new_pl_media.is_user) if new_pl_media else False
                new_has_p_media = any(m.media_type == 'poster' for m in item.media_files)
                new_has_pl_media = bool(new_pl_media)

                if '_user.' in new_poster_url and '_user.' not in old_poster_url:
                    changes.append('유저 포스터 반영')
                elif '_user.' not in new_poster_url and '_user.' in old_poster_url:
                    changes.append('시스템 포스터 복원')
                elif new_poster_url and new_poster_url != old_poster_url:
                    changes.append('포스터 주소 동기화')

                if new_pl_url and ('_user.' in new_pl_url or new_pl_is_user) and ('_user.' not in old_pl_url and not old_pl_is_user):
                    changes.append('유저 가로커버 반영')
                elif old_pl_url and ('_user.' not in new_pl_url and not new_pl_is_user) and ('_user.' in old_pl_url or old_pl_is_user):
                    changes.append('시스템 가로커버 복원')
                elif new_pl_url and new_pl_url != old_pl_url:
                    changes.append('가로 커버 동기화')

                if (not old_has_p_media and new_has_p_media) or (not old_has_pl_media and new_has_pl_media):
                    changes.append('미디어 테이블 복원')

                if not any(k in changes for k in ['포스터', '가로커버', '미디어']):
                    changes.append('디스크 파일 동기화')

                is_modified = True

            extra_info = dict(item.extra_info or {})
            actors_list = list(extra_info.get('_actors') or extra_info.get('actor_cache') or [])

            if actors_list:
                idx_engine = memory_index
                if not idx_engine:
                    person_dom = ModuleMetaDb._person_domain_from_item_category(category)
                    idx_engine = PersonMemoryIndex(domain=person_dom)

                updated_ko_count = 0
                updated_idx_count = 0
                updated_en_count = 0
                actor_data_changed = False

                for act in actors_list:
                    if not isinstance(act, dict):
                        continue

                    matched = idx_engine.match(act)
                    if not matched:
                        continue

                    target_ko = matched.get('name_ko') or ''
                    target_idx = matched.get('person_idx') or ''
                    target_en = matched.get('name_en') or ''

                    curr_ko = act.get('name_ko') or ''
                    curr_name = act.get('name') or ''
                    if target_ko and (curr_ko != target_ko or curr_name != target_ko):
                        act['name_ko'] = target_ko
                        act['name'] = target_ko
                        updated_ko_count += 1
                        actor_data_changed = True

                    curr_en = act.get('name_en') or ''
                    if target_en and not curr_en:
                        act['name_en'] = target_en
                        updated_en_count += 1
                        actor_data_changed = True

                    curr_idx = act.get('actor_idx') or act.get('person_idx') or ''
                    if target_idx and not curr_idx:
                        act['actor_idx'] = target_idx
                        updated_idx_count += 1
                        actor_data_changed = True

                if updated_ko_count > 0:
                    changes.append(f'인물 한글화 {updated_ko_count}명')
                if updated_idx_count > 0:
                    changes.append(f'인물 코드 등록 {updated_idx_count}명')
                if updated_en_count > 0:
                    changes.append(f'인물 영문명 보완 {updated_en_count}명')
                if actor_data_changed:
                    extra_info['_actors'] = actors_list
                    item.extra_info = extra_info
                    flag_modified(item, 'extra_info')
                    is_modified = True

            raw_original = item.original if isinstance(item.original, dict) else {}
            orig_genres = raw_original.get('genre') or []
            if isinstance(orig_genres, list) and orig_genres:
                from support_site import SiteAvBase
                from .mod_meta_db import MetaTag, MetaItemTagMap
                try:
                    from support_site.constants import AV_GENRE_IGNORE_JA, AV_GENRE_IGNORE_KO
                except Exception:
                    AV_GENRE_IGNORE_JA, AV_GENRE_IGNORE_KO = [], []

                new_tag_records = []
                seen_translated_genres = set()

                for g_raw in orig_genres:
                    if not isinstance(g_raw, str) or not g_raw.strip():
                        continue
                    g_clean = g_raw.strip()
                    if g_clean in AV_GENRE_IGNORE_JA or "％OFF" in g_clean:
                        continue
                    g_trans = SiteAvBase.get_translated_tag(g_clean)
                    if not g_trans or g_trans in AV_GENRE_IGNORE_KO:
                        continue

                    if g_trans in seen_translated_genres:
                        continue
                    seen_translated_genres.add(g_trans)

                    tag_rec = sess.query(MetaTag).filter_by(domain=item.domain, name_org=g_clean, tag_type="genre").first()
                    if not tag_rec:
                        try:
                            nested = sess.begin_nested()
                            tag_rec = MetaTag(domain=item.domain, name=g_trans, name_org=g_clean, tag_type="genre")
                            sess.add(tag_rec)
                            sess.flush()
                            nested.commit()
                        except Exception:
                            nested.rollback()
                            tag_rec = sess.query(MetaTag).filter_by(domain=item.domain, name_org=g_clean, tag_type="genre").first()
                    elif tag_rec.name != g_trans:
                        tag_rec.name = g_trans

                    if tag_rec:
                        new_tag_records.append(tag_rec)

                current_genre_tag_ids = [tm.tag_id for tm in item.tag_maps if tm.tag and tm.tag.tag_type == 'genre']
                new_genre_tag_ids = [tr.id for tr in new_tag_records if tr.id]

                if set(current_genre_tag_ids) != set(new_genre_tag_ids):
                    for tm in list(item.tag_maps):
                        if tm.tag and tm.tag.tag_type == 'genre':
                            item.tag_maps.remove(tm)

                    for tag_rec in new_tag_records:
                        item.tag_maps.append(MetaItemTagMap(tag_id=tag_rec.id))

                    changes.append('장르 번역 최신화')
                    is_modified = True

            if is_modified:
                item.updated_time = datetime.now()
                sess.commit()
                ModuleMetaDb.checkpoint_wal()
                detail_msg = ", ".join(changes)
                return {
                    'ret': 'success',
                    'is_updated': True,
                    'title_log': title_for_log,
                    'detail_msg': detail_msg
                }
            else:
                return {
                    'ret': 'success',
                    'is_updated': False,
                    'title_log': title_for_log,
                    'detail_msg': '변경 없음'
                }

        except Exception as e:
            sess.rollback()
            logger.error(f"[MetaUtil] sync_local_data 오류 ({code}): {e}")
            return {'ret': 'error', 'msg': str(e), 'is_updated': False}
        finally:
            sess.remove()


    @classmethod
    def sync_item_media_to_storage(cls, module, code, category, batch_opts=None, use_dir_cache=True):
        """
        외부 사이트 재스크래핑 없이 DB에 보존된 원본 미디어 URL(포스터/커버/팬아트/트레일러)을
        설정된 개인 저장소(구글 드라이브 또는 로컬 디스크)로 일괄 다운로드/업로드하여 동기화합니다.
        """
        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia
        from .util_preview import MetaPreviewUtil
        from support_site import SiteAvBase

        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess:
            return {'ret': 'error', 'error_code': 'database_unavailable', 'msg': '세션 획득 실패', 'is_updated': False}

        try:
            item = sess.query(MetaItem).filter_by(code=code, category=std_cat).first()
            if not item:
                return {'ret': 'error', 'error_code': 'not_found', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

            ui_code = item.ui_code or item.originaltitle or item.code
            title_for_log = str(item.title or ui_code).strip()
            if len(title_for_log) > 40:
                title_for_log = title_for_log[:37] + '...'

            batch_opts = batch_opts or {}
            skip_existing = batch_opts.get('media_sync_skip_existing', True)
            sync_poster = batch_opts.get('media_sync_poster', True)
            sync_landscape = batch_opts.get('media_sync_landscape', True)
            sync_fanart = batch_opts.get('media_sync_fanart', True)
            sync_trailer = batch_opts.get('media_sync_trailer', True)

            cat_upper = str(std_cat or '').upper()
            module_prefix = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')
            image_mode = P.ModelSetting.get(f"{module_prefix}_image_mode") or P.ModelSetting.get("jav_censored_image_mode") or "ff_proxy"

            stem = (item.code or item.ui_code or '').lower() if cat_upper == 'WESTERN' else (item.ui_code or item.originaltitle or item.code or '').lower()
            target_folder, server_url_prefix = cls.get_server_folder_and_prefix(
                item.domain, item.category, stem, studio=item.studio or '', year=item.year or 1900
            )

            if not target_folder or not server_url_prefix:
                return {'ret': 'error', 'msg': '저장 폴더 경로 또는 URL 생성 실패', 'is_updated': False}

            rel_dir = cls.get_image_relative_dir(item.domain, std_cat, stem, studio=item.studio or '', year=item.year or 1900)

            is_modified = False
            changes = []
            failures = []
            tmp_dir = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)

            synced_image_paths = {}

            def sync_single_image_asset(source_url, filename_base, media_type, current_url="", sort_idx=0):
                def image_result(url, changed):
                    if url:
                        if image_mode == 'gdrive':
                            match = re.search(r'[?&]id=([^&]+)', str(url))
                            fid = match.group(1) if match else ''
                            synced_image_paths[url] = MetaPreviewUtil.get_recorded_remote_path(fid, dest_rel, category=std_cat, is_image=True)
                        else:
                            synced_image_paths[url] = dest_rel
                    return url, changed

                target_filename = f"{filename_base}.jpg"
                dest_rel = f"{rel_dir}/{target_filename}".strip('/\\')
                registered = next((m for m in getattr(item, 'media_files', []) if m.media_type == media_type and not cls.is_user_media(m) and (media_type != 'fanart' or (m.sort_order or 0) == sort_idx) and cls.media_in_storage(m, image_mode)), None)
                if registered and registered.relative_path:
                    recorded = registered.relative_path.replace('\\', '/')
                    if recorded.startswith('/') or ':' in recorded or '..' in recorded.split('/'):
                        raise ValueError('Invalid recorded image path')
                    dest_rel = recorded
                    target_filename = recorded.rsplit('/', 1)[-1]
                local_dest = os.path.join(target_folder, target_filename)
                expected_url = f"{server_url_prefix}/{target_filename}"
                if image_mode == 'image_server':
                    root = P.ModelSetting.get('jav_censored_image_server_local_path') or '/data/images'
                    server = str(P.ModelSetting.get('jav_censored_image_server_url') or f"{F.SystemModelSetting.get('ddns')}/images").rstrip('/')
                    local_dest = os.path.join(root, dest_rel.replace('/', os.path.sep))
                    expected_url = f'{server}/{dest_rel}'


                # 개인 저장소(구글 드라이브 / 로컬 디스크)에 실물 파일이 존재하는지 우선 검사
                if image_mode == 'gdrive':
                    fid = MetaPreviewUtil.get_remote_file_id(dest_rel, category=std_cat, is_image=True, use_dir_cache=use_dir_cache)
                    if fid and skip_existing:
                        # 원격 구글 드라이브에 실물 파일이 존재하는 경우: DB 등록 파일 ID와 대조
                        m_fid = re.search(r'[?&]id=([^&]+)', str(current_url or ''))
                        current_fid = m_fid.group(1).strip() if m_fid else ''

                        if skip_existing and current_fid == fid:
                            # DB에 이미 실제 파일 ID와 동일하게 등록되어 있으면 처리 생략
                            return image_result(current_url, False)
                        else:
                            # DB가 사이트 주소이거나 파일 ID가 다른 경우 새 파일 ID로 DB 갱신
                            return image_result(cls.format_gdrive_thumbnail_url(fid), True)

                elif image_mode == 'image_server':
                    if skip_existing and MetaPreviewUtil.has_local_file(local_dest, use_cache=use_dir_cache):
                        # 로컬 저장소 경로에 실물 파일이 존재하는 경우: DB 등록 주소와 대조
                        clean_curr = str(current_url or '').strip()
                        is_registered = (
                            clean_curr == expected_url or
                            (ModuleMetaDb.is_local_server_url(clean_curr) and target_filename in clean_curr.lower())
                        )

                        if skip_existing and is_registered:
                            # DB에 이미 로컬 경로/주소가 등록되어 있으면 처리 생략
                            return image_result(current_url, False)
                        else:
                            # DB가 사이트 주소이거나 주소가 다른 경우 로컬 서버 주소로 DB 갱신
                            return image_result(expected_url, True)

                # Rebuilding TPDB movie covers must keep the same front/back layout as initial scraping.
                original_thumb = (getattr(item, 'original', None) or {}).get('thumb') or {}
                back_cover = original_thumb.get('back_cover')
                if media_type == 'landscape' and getattr(item, 'site', '') == 'tpdb' and back_cover and source_url:
                    from support_site.site_av.site_tpdb import SiteTpdb
                    merged = SiteTpdb._merge_covers(back_cover, source_url)
                    if merged:
                        try:
                            if image_mode == 'gdrive':
                                file_id = cls.upload_image_to_gdrive(merged, dest_rel, category=std_cat, overwrite=not skip_existing)
                                if file_id:
                                    MetaPreviewUtil.register_remote_file(dest_rel, file_id, category=std_cat, is_image=True)
                                    return image_result(cls.format_gdrive_thumbnail_url(file_id), True)
                                return None, False
                            if image_mode == 'image_server':
                                SiteAvBase.publish_image_file(merged, local_dest)
                                return image_result(expected_url, True)
                        finally:
                            if os.path.exists(merged): os.remove(merged)

                # 개인 저장소에 실물 파일이 존재하지 않는 경우에만 원본 사이트에서 다운로드 후 저장 실행
                if not source_url or not str(source_url).startswith(('http://', 'https://')):
                    return None, False

                if image_mode == 'gdrive':
                    tmp_file = os.path.join(tmp_dir, f"tmp_sync_{item.id}_{filename_base}_{int(time.time()*1000)}.jpg")
                    try:
                        resp = SiteAvBase.get_response(source_url, timeout=15)
                        if resp and resp.status_code == 200:
                            if not SiteAvBase._save_image_data(BytesIO(resp.content), tmp_file):
                                img_obj = SiteAvBase.imopen(source_url)
                                if img_obj:
                                    cls.save_normalized_jpeg(img_obj, tmp_file)
                                    img_obj.close()
                            uploaded_fid = cls.upload_image_to_gdrive(tmp_file, dest_rel, category=std_cat, overwrite=not skip_existing)
                            if uploaded_fid:
                                MetaPreviewUtil.register_remote_file(dest_rel, uploaded_fid, category=std_cat, is_image=True)
                                return image_result(cls.format_gdrive_thumbnail_url(uploaded_fid), True)
                    finally:
                        if os.path.exists(tmp_file):
                            try: os.remove(tmp_file)
                            except Exception: pass
                    return None, False

                elif image_mode == 'image_server':
                    os.makedirs(os.path.dirname(local_dest), exist_ok=True)
                    resp = SiteAvBase.get_response(source_url, timeout=15)
                    if resp and resp.status_code == 200:
                        if not SiteAvBase._save_image_data(BytesIO(resp.content), local_dest):
                            img_obj = SiteAvBase.imopen(source_url)
                            if img_obj:
                                cls.save_normalized_jpeg(img_obj, local_dest)
                                img_obj.close()
                        MetaPreviewUtil.register_local_file(local_dest)
                        return image_result(expected_url, True)
                    return None, False

                return None, False

            orig_data = item.original if isinstance(item.original, dict) else {}
            orig_thumb = orig_data.get('thumb') if isinstance(orig_data.get('thumb'), dict) else {}
            selected = {}
            if sync_poster: selected[('poster', 0)] = 'p'
            if sync_landscape: selected[('landscape', 1)] = 'pl'
            if sync_fanart:
                for art_index in range(min(int(P.ModelSetting.get('jav_censored_art_count') or 0), len(orig_data.get('fanart') or []))):
                    selected[('fanart', art_index)] = f'art_{art_index + 1}'
            adopted = cls.adopt_item_user_images(item, image_mode, rel_dir, stem, selected, use_dir_cache=use_dir_cache)
            if adopted:
                changes.extend(adopted)
                is_modified = True
            extra_info = dict(item.extra_info or {})

            def official_source(kind, *candidates):
                # A user thumbnail may also be the current representative URL in another mode.
                # Never copy that URL into an official _p/_pl asset.
                for candidate in candidates:
                    if not candidate or cls.is_user_media({'url': candidate}):
                        continue
                    matches_user = any(cls.is_user_media(media) and ModuleMetaDb._media_asset_matches(
                        {'media_type': media.media_type, 'url': media.url, 'gdrive_fileid': media.gdrive_fileid},
                        kind, candidate) for media in item.media_files)
                    if not matches_user:
                        return candidate
                return ''

            # 이미지 자산 동기화 (개인 저장소 모드일 때만 수행)
            if image_mode in ['gdrive', 'image_server']:
                # 포스터 자산 동기화
                if sync_poster:
                    current_p = str(item.poster_url or '')
                    user_p = next((m for m in item.media_files if m.media_type == 'poster' and cls.is_user_media(m) and cls.media_in_storage(m, image_mode)), None)
                    is_user_p = bool(user_p)
                    if not is_user_p:
                        src_p = official_source('poster', orig_thumb.get('poster'), current_p)
                        new_p_url, p_changed = sync_single_image_asset(src_p, f"{stem}_p", 'poster', current_url=current_p, sort_idx=0)
                        if not new_p_url and str(src_p or '').startswith(('http://', 'https://')):
                            failures.append('poster')
                        if new_p_url:
                            p_db_changed = False
                            if item.poster_url != new_p_url:
                                item.poster_url = new_p_url
                                is_modified = True
                                p_db_changed = True
                            p_media = next((m for m in item.media_files if m.media_type == 'poster' and not cls.is_user_media(m) and cls.media_in_storage(m, image_mode)), None)
                            if p_media:
                                if p_media.url != new_p_url:
                                    p_media.url = new_p_url
                                    is_modified = True
                                    p_db_changed = True
                            else:
                                item.media_files.append(MetaMedia(
                                    media_type='poster', url=new_p_url, is_user=False, sort_order=0,
                                    source='gds' if image_mode == 'gdrive' else 'site'
                                ))
                                is_modified = True
                                p_db_changed = True
                            if p_changed:
                                is_modified = True
                                changes.append('포스터 연동' if item.poster_url != current_p and not ModuleMetaDb.is_local_server_url(current_p) else ('포스터 저장' if image_mode == 'image_server' else '포스터 GDrive'))
                            elif p_db_changed:
                                changes.append('포스터 DB 연동')

                # 가로 커버 자산 동기화
                if sync_landscape:
                    current_pl_media = next((m for m in sorted(item.media_files, key=lambda m: cls.is_user_media(m), reverse=True) if m.media_type == 'landscape' and cls.media_in_storage(m, image_mode)), None)
                    current_pl = current_pl_media.url if current_pl_media else extra_info.get('landscape_url', '')
                    is_user_pl = bool(current_pl_media and cls.is_user_media(current_pl_media))

                    if not is_user_pl:
                        src_pl = official_source('landscape', orig_thumb.get('landscape'), current_pl)
                        new_pl_url, pl_changed = sync_single_image_asset(src_pl, f"{stem}_pl", 'landscape', current_url=current_pl, sort_idx=1)
                        if not new_pl_url and str(src_pl or '').startswith(('http://', 'https://')):
                            failures.append('landscape')
                        if new_pl_url:
                            pl_db_changed = False
                            if extra_info.get('landscape_url') != new_pl_url:
                                extra_info['landscape_url'] = new_pl_url
                                item.extra_info = extra_info
                                is_modified = True
                                pl_db_changed = True
                            if current_pl_media:
                                if current_pl_media.url != new_pl_url:
                                    current_pl_media.url = new_pl_url
                                    is_modified = True
                                    pl_db_changed = True
                            else:
                                item.media_files.append(MetaMedia(
                                    media_type='landscape', url=new_pl_url, is_user=False, sort_order=1,
                                    source='gds' if image_mode == 'gdrive' else 'site'
                                ))
                                is_modified = True
                                pl_db_changed = True
                            if pl_changed:
                                is_modified = True
                                changes.append('가로커버 연동' if extra_info.get('landscape_url') != current_pl and not ModuleMetaDb.is_local_server_url(current_pl) else ('가로커버 저장' if image_mode == 'image_server' else '가로커버 GDrive'))
                            elif pl_db_changed:
                                changes.append('가로커버 DB 연동')

                # 팬아트 갤러리 자산 동기화
                if sync_fanart:
                    raw_fanarts = orig_data.get('fanart') or []
                    max_arts = int(P.ModelSetting.get("jav_censored_art_count") or 0)
                    if max_arts > 0 and raw_fanarts:
                        synced_arts_count = 0
                        for fa_idx, fa_src in enumerate(raw_fanarts[:max_arts]):
                            target_art_m = next((m for m in sorted(item.media_files, key=cls.is_user_media, reverse=True) if m.media_type == 'fanart' and m.sort_order == fa_idx and cls.media_in_storage(m, image_mode)), None)
                            if target_art_m and cls.is_user_media(target_art_m):
                                continue
                            cur_art_url = target_art_m.url if target_art_m else ''
                            if not cur_art_url:
                                cur_art_url = fa_src

                            fa_src = official_source('fanart', fa_src)
                            new_fa_url, fa_changed = sync_single_image_asset(fa_src, f"{stem}_art_{fa_idx+1}", 'fanart', current_url=cur_art_url, sort_idx=fa_idx)
                            if not new_fa_url and str(fa_src or '').startswith(('http://', 'https://')):
                                failures.append(f'fanart:{fa_idx}')
                            if new_fa_url:
                                if target_art_m:
                                    if target_art_m.url != new_fa_url:
                                        target_art_m.url = new_fa_url
                                        is_modified = True
                                else:
                                    item.media_files.append(MetaMedia(
                                        media_type='fanart', url=new_fa_url, is_user=False, sort_order=fa_idx,
                                        source='gds' if image_mode == 'gdrive' else 'site'
                                    ))
                                    is_modified = True
                                if fa_changed:
                                    is_modified = True
                                    synced_arts_count += 1

                        if synced_arts_count > 0:
                            changes.append(f'팬아트 {synced_arts_count}장 동기화')

            # 공식 트레일러 영상 자산 동기화
            if sync_trailer and MetaPreviewUtil.is_trailer_download_enabled(std_cat):
                target_storage = MetaPreviewUtil.get_setting("preview_storage_type", std_cat, default="local")
                existing_video = MetaPreviewUtil.get_stored_video(item, 'trailer', target_storage, std_cat)
                raw_tr_url = None

                # 원본 메타 또는 extras에서 공식 트레일러 원격 URL 추출
                for ex in (orig_data.get('extras') or []):
                    if isinstance(ex, dict) and ex.get('content_type') == 'trailer' and ex.get('content_url'):
                        raw_tr_url = ex['content_url']
                        break

                if not raw_tr_url:
                    raw_tr_url = orig_data.get('trailer')

                target_storage = MetaPreviewUtil.get_setting("preview_storage_type", std_cat, default="local")
                rel_dir_tr, tr_filename = MetaPreviewUtil.get_video_relative_path(item, video_type='trailer', category=std_cat)
                dest_rel_tr = f"{rel_dir_tr}/{tr_filename}"
                if isinstance(existing_video, dict) and existing_video.get('storage_type') == target_storage and existing_video.get('relative_path'):
                    dest_rel_tr = existing_video['relative_path']
                    tr_filename = dest_rel_tr.replace('\\', '/').rsplit('/', 1)[-1]


                tr_file_found = False

                # 개인 저장소(구글 드라이브 / 로컬 디스크)에 트레일러 실물 파일이 존재하는지 우선 검사
                if target_storage == 'gdrive':
                    remote_tr_fid = MetaPreviewUtil.get_remote_file_id(dest_rel_tr, category=std_cat, is_image=False, use_dir_cache=use_dir_cache)
                    if remote_tr_fid and skip_existing:
                        tr_file_found = True
                        actual_tr_path = MetaPreviewUtil.get_recorded_remote_path(remote_tr_fid, dest_rel_tr, category=std_cat)
                        db_tr_fid = str((existing_video or {}).get('google_fileid') or '').strip()

                        if not (skip_existing and db_tr_fid == remote_tr_fid and (existing_video or {}).get('relative_path') == actual_tr_path and extra_info.get('trailer_video') == existing_video):
                            # DB에 미등록되었거나 파일 ID가 다른 경우 신규 File ID로 DB 갱신
                            tr_meta = {
                                'storage_type': 'gdrive',
                                'filename': actual_tr_path.rsplit('/', 1)[-1],
                                'relative_path': actual_tr_path,
                                'duration': (existing_video or {}).get('duration') or 0,
                                'created_time': (existing_video or {}).get('created_time') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                'google_fileid': remote_tr_fid,
                                'local_path': ''
                            }
                            extra_info['trailer_video'] = tr_meta
                            item.extra_info = extra_info
                            is_modified = True
                            changes.append('트레일러 연동')

                elif target_storage == 'local':
                    local_root_tr = MetaPreviewUtil.get_setting("preview_local_path", std_cat, default="/data/previews")
                    final_tr_file = os.path.join(local_root_tr, dest_rel_tr.replace('/', os.path.sep))

                    if skip_existing and MetaPreviewUtil.has_local_file(final_tr_file, use_cache=use_dir_cache) and os.path.exists(final_tr_file):
                        tr_file_found = True
                        db_tr_path = str((existing_video or {}).get('local_path') or '').strip()

                        if not (skip_existing and db_tr_path == final_tr_file and (existing_video or {}).get('relative_path') == dest_rel_tr and extra_info.get('trailer_video') == existing_video):
                            # DB에 미등록되었거나 경로가 다른 경우 로컬 파일 정보로 DB 갱신
                            dur = MetaPreviewUtil.get_video_duration(final_tr_file, MetaPreviewUtil.get_ffprobe_path(std_cat))
                            tr_meta = {
                                'storage_type': 'local',
                                'filename': tr_filename,
                                'relative_path': dest_rel_tr,
                                'duration': int(round(dur)) if dur else ((existing_video or {}).get('duration') or 0),
                                'created_time': (existing_video or {}).get('created_time') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                'local_path': final_tr_file,
                                'google_fileid': ''
                            }
                            extra_info['trailer_video'] = tr_meta
                            item.extra_info = extra_info
                            is_modified = True
                            changes.append('트레일러 연동')

                # 개인 저장소에 트레일러 실물 파일이 없는 경우에만 사이트 원본 URL에서 다운로드 실행
                if not tr_file_found and raw_tr_url:
                    # Keep DB ownership in this batch: the standalone workflow commits
                    # and removes the same scoped session when item_dict is omitted.
                    trailer_item = {
                        key: getattr(item, key, None)
                        for key in ('code', 'ui_code', 'originaltitle', 'studio', 'site')
                    }
                    trailer_item['extra_info'] = copy.deepcopy(extra_info)
                    trailer_item['original'] = copy.deepcopy(orig_data)
                    if existing_video:
                        trailer_item['extra_info']['trailer_video'] = dict(existing_video)
                    t_succ, t_res = MetaPreviewUtil.process_trailer_download_workflow(
                        code,
                        raw_tr_url,
                        category=std_cat,
                        force=not skip_existing,
                        item_dict=trailer_item,
                        use_dir_cache=use_dir_cache
                    )
                    if t_succ:
                        extra_info = trailer_item['extra_info']
                        item.extra_info = extra_info
                        is_modified = True
                        changes.append('트레일러 GDrive' if target_storage == 'gdrive' else '트레일러 저장')
                    else:
                        failures.append('trailer')
                    if not t_succ and trailer_item.get('original') != orig_data:
                        MetaPreviewUtil.remove_official_trailer(item, raw_tr_url)
                        extra_info = dict(item.extra_info or {})
                        is_modified = True
                        changes.append('다운로드 실패 공식 트레일러 URL 제거')

            paths_updated = False
            for media in item.media_files:
                if not cls.is_user_media(media) and media.url in synced_image_paths and media.relative_path != synced_image_paths[media.url]:
                    media.relative_path = synced_image_paths[media.url]
                    paths_updated = True
                    is_modified = True
            if paths_updated and not changes:
                changes.append('저장 경로 등록')

            # Playback extras are derived from extra_info by the DB serializer;
            # MetaItem has no persistent extras attribute.
            if is_modified:
                for media in item.media_files:
                    if cls.is_user_media(media) or media.url not in synced_image_paths:
                        continue
                    media.relative_path = synced_image_paths[media.url]
                    if image_mode == 'gdrive':
                        matched = re.search(r'[?&]id=([^&]+)', str(media.url or ''))
                        media.gdrive_fileid = matched.group(1) if matched else media.gdrive_fileid
                ModuleMetaDb.sync_item_video_assets(item)
                item.updated_time = datetime.now()
                sess.commit()
                ModuleMetaDb.checkpoint_wal()

            detail_msg = ", ".join(changes) if changes else "변경 없음"
            return {
                'ret': 'success',
                'is_updated': is_modified,
                'title_log': title_for_log,
                'detail_msg': detail_msg,
                'changes': changes,
                'failures': failures
            }

        except Exception as exc:
            sess.rollback()
            logger.error(f"[MetaUtil] sync_item_media_to_storage 오류 ({code}): {exc}")
            return {'ret': 'error', 'msg': str(exc), 'is_updated': False}
        finally:
            sess.remove()


    @classmethod
    def extract_keyword_from_path(cls, media_path, category='JAV_CEN'):
        if not media_path or not isinstance(media_path, str):
            return ""

        clean_path = media_path.strip().replace('\\', '/')
        filename = os.path.basename(clean_path)
        if not filename:
            return ""

        cat_upper = str(category or 'JAV_CEN').upper()
        if cat_upper in ['WESTERN', 'WEST']:
            return os.path.splitext(filename)[0]

        from support_site import SiteAvBase

        ui_code, label, num = SiteAvBase._parse_ui_code(filename, category=cat_upper)
        if ui_code and (num or '-' in ui_code):
            return ui_code

        parent_dir = os.path.basename(os.path.dirname(clean_path))
        if parent_dir:
            combined_candidate = f"{parent_dir}-{filename}"
            ui_code_comb, label_comb, num_comb = SiteAvBase._parse_ui_code(combined_candidate, category=cat_upper)
            if ui_code_comb and (num_comb or '-' in ui_code_comb):
                return ui_code_comb

        return ui_code or os.path.splitext(filename)[0]

    @classmethod
    def legacy_batch_status(cls, category, kind, fallback=None, stop=False):
        from .setup import P
        from framework import F
        cache = F.get_cache(f'{P.package_name}_legacy_batch_{category}_{kind}')
        if stop:
            cache.set('stop_flag', 'true')
        raw = cache.get('status')
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        return json.loads(raw) if raw else dict(fallback or {})

    @classmethod
    def start_legacy_batch(cls, category, kind, **options):
        from .mod_meta_db import F, celery, task_meta_db_legacy_batch
        from .setup import P
        if cls.legacy_batch_status(category, kind).get('is_running'):
            return {'ret': 'warning', 'msg': '이미 작업이 진행 중입니다.'}
        if not P.get_module('meta_db')._ensure_meta_db_writable(category):
            return {'ret': 'error', 'msg': 'DB 마이그레이션 완료 후 실행하세요.'}
        cache = F.get_cache(f'{P.package_name}_legacy_batch_{category}_{kind}')
        cache.set('stop_flag', 'false')
        cache.set('status', json.dumps({'is_running': True, 'status': '대기 중', 'current': 0, 'total': 0}))
        try:
            if celery and F.config.get('use_celery', False):
                task_meta_db_legacy_batch.apply_async(args=[category, kind, options])
            else:
                import threading
                threading.Thread(target=cls.run_legacy_batch, args=(category, kind, options), daemon=True).start()
        except Exception:
            cache.set('status', json.dumps({'is_running': False, 'status': '시작 실패'}))
            raise
        return {'ret': 'success', 'msg': '배치 작업을 시작했습니다.'}

    @classmethod
    def run_legacy_batch(cls, category, kind, options):
        from framework import F
        from .setup import P
        cache = F.get_cache(f'{P.package_name}_legacy_batch_{category}_{kind}')
        class SharedStatus(dict):
            def publish(self):
                cache.set('status', json.dumps(self, ensure_ascii=False))
            def update(self, *args, **kwargs):
                super().update(*args, **kwargs)
                self.publish()
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                self.publish()
            def get(self, key, default=None):
                if key == 'stop_flag':
                    return cache.get('stop_flag') in ('true', b'true')
                return super().get(key, default)
        status = SharedStatus()
        try:
            module = P.get_module({'JAV_CEN': 'jav_censored', 'JAV_UNCEN': 'jav_uncensored', 'WESTERN': 'western'}[category])
            if kind == 'enrich':
                cls.run_enrichment_worker(category, status, module.info, **options)
            else:
                cls.run_sync_worker(category, status, module.info, **options)
        except Exception as exc:
            status.update(is_running=False, status=f'오류: {exc}')
            logger.exception('[MetaWorker] 카테고리 배치 실행 실패')

    @classmethod
    def run_sync_worker(cls, category, sync_status, info_func=None, custom_root=None, auto_rescue=False):
        from sqlalchemy.orm import sessionmaker, lazyload
        from .mod_meta_db import ModuleMetaDb, MetaItem
        registry, _, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if registry is None:
            return
        # Site refreshes can remove the scoped session; keep this scan independent.
        sess = sessionmaker(bind=registry.get_bind(), expire_on_commit=False)()
        sync_status.update(is_running=True, status='작업 중', total=0, current=0,
                           updated=0, rescued=0, current_code='')
        try:
            query = sess.query(MetaItem).options(lazyload('*')).filter_by(category=std_cat)
            sync_status['total'] = query.count()
            logger.debug('[MetaWorker] [%s] 로컬 이미지 동기화 시작: 대상=%s, 경로=%s, 누락 복구=%s',
                         std_cat, sync_status['total'], custom_root or '공통 설정', auto_rescue)
            last_id = 0
            while not sync_status.get('stop_flag'):
                rows = query.filter(MetaItem.id > last_id).order_by(MetaItem.id).limit(50).all()
                if not rows:
                    break
                last_id = rows[-1].id
                for record in rows:
                    if sync_status.get('stop_flag'):
                        break
                    code = record.code
                    sync_status['current'] += 1
                    sync_status['current_code'] = record.originaltitle or code
                    result, missing = cls.sync_single_record_disk_images(record, custom_root_path=custom_root)
                    if result == 'updated':
                        sync_status['updated'] += 1
                    # Release SQLite writes before an independent site refresh writes.
                    sess.commit()
                    if missing and auto_rescue and info_func:
                        try:
                            fresh = info_func(code, skip_trans=True, extra_opts={'for_db': True})
                            if fresh and fresh.get('thumb'):
                                sync_status['rescued'] += 1
                        except Exception as exc:
                            logger.debug(f'[MetaWorker] 누락 복구 실패 ({code}): {exc}')
                    logger.debug('[MetaWorker] [%s] %s: 동기화 결과=%s, 이미지 누락=%s',
                                 std_cat, code, result, missing)
                sess.expunge_all()
            ModuleMetaDb.checkpoint_wal()
            sync_status['status'] = '중단됨' if sync_status.get('stop_flag') else '완료'
        except Exception as exc:
            sess.rollback()
            logger.exception(f'[MetaWorker] [{std_cat}] 동기화 실패: {exc}')
            sync_status['status'] = f'오류: {exc}'
        finally:
            sess.close()
            sync_status['is_running'] = False
            logger.debug('[MetaWorker] [%s] 로컬 이미지 동기화 종료: 상태=%s, 처리=%s/%s, 갱신=%s, 복구=%s',
                         std_cat, sync_status.get('status'), sync_status.get('current'), sync_status.get('total'),
                         sync_status.get('updated'), sync_status.get('rescued'))

    @classmethod
    def run_enrichment_worker(cls, category, enrich_status, info_func, delay=2.0):
        from sqlalchemy import and_, or_, exists
        from sqlalchemy.orm import sessionmaker
        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia
        registry, _, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if registry is None:
            return
        sess = sessionmaker(bind=registry.get_bind())()
        enrich_status.update(is_running=True, status='작업 중', total=0, current=0,
                             success=0, fail=0, current_code='')
        try:
            # Video rows alone do not mean that the required images are present.
            missing = or_(*[~exists().where(and_(MetaMedia.item_id == MetaItem.id,
                        MetaMedia.media_type == kind, MetaMedia.url != ''))
                        for kind in ('poster', 'landscape')])
            query = sess.query(MetaItem.id, MetaItem.code, MetaItem.originaltitle).filter(
                MetaItem.category == std_cat, missing)
            enrich_status['total'] = query.count()
            logger.debug('[MetaWorker] [%s] 미디어 채우기 시작: 대상=%s, 간격=%s초',
                         std_cat, enrich_status['total'], delay)
            last_id = 0
            while not enrich_status.get('stop_flag'):
                rows = query.filter(MetaItem.id > last_id).order_by(MetaItem.id).limit(100).all()
                if not rows:
                    break
                last_id = rows[-1].id
                # End the read transaction before site refresh opens its own session.
                sess.rollback()
                for record in rows:
                    if enrich_status.get('stop_flag'):
                        break
                    enrich_status['current'] += 1
                    enrich_status['current_code'] = record.originaltitle or record.code
                    success = False
                    site_activity = {}
                    try:
                        result = info_func(record.code, skip_trans=True, extra_opts={
                            'for_db': True, '_batch_site_activity': site_activity})
                        success = bool(result and result.get('thumb'))
                        enrich_status['success' if success else 'fail'] += 1
                    except Exception as exc:
                        enrich_status['fail'] += 1
                        logger.debug(f'[MetaWorker] 미디어 획득 실패 ({record.code}): {exc}')
                    logger.debug('[MetaWorker] [%s] %s: 미디어 채우기 결과=%s (%s/%s)',
                                 std_cat, record.code, '성공' if success else '실패',
                                 enrich_status['current'], enrich_status['total'])
                    if enrich_status['current'] < enrich_status['total'] and not enrich_status.get('stop_flag'):
                        cls.wait_after_batch_site_request(site_activity, delay)
            ModuleMetaDb.checkpoint_wal()
            enrich_status['status'] = '중단됨' if enrich_status.get('stop_flag') else '완료'
        except Exception as exc:
            logger.exception(f'[MetaWorker] [{std_cat}] 미디어 채우기 실패: {exc}')
            enrich_status['status'] = f'오류: {exc}'
        finally:
            sess.close()
            enrich_status['is_running'] = False
            logger.debug('[MetaWorker] [%s] 미디어 채우기 종료: 상태=%s, 처리=%s/%s, 성공=%s, 실패=%s',
                         std_cat, enrich_status.get('status'), enrich_status.get('current'), enrich_status.get('total'),
                         enrich_status.get('success'), enrich_status.get('fail'))


class PersonMemoryIndex:
    """
    배우 정보 동기화 시 수만 건의 DB 쿼리 반복을 없애기 위해
    인물 DB의 핵심 필드를 메모리에 1회 사전 적재하여 O(1) 속도로 대조하는 인덱서
    """

    def __init__(self, domain='JAV'):
        from .mod_meta_db import ModuleMetaDb, MetaPerson

        self.domain = str(domain or 'JAV').upper()
        self.by_site_actor = {}
        self.by_idx = {}
        self.by_name_org = {}
        self.by_name_en = {}
        self.by_name_ko = {}

        sess, _, _ = ModuleMetaDb.get_session_and_domain('PERSON')
        if not sess:
            return

        try:
            # 필수 경량 컬럼들만 1회의 단일 쿼리로 메모리에 적재
            persons = sess.query(
                MetaPerson.id,
                MetaPerson.person_idx,
                MetaPerson.name_org,
                MetaPerson.name_ko,
                MetaPerson.name_en,
                MetaPerson.media_src,
                MetaPerson.extra_info
            ).filter(MetaPerson.domain == self.domain).all()

            for pid, p_idx, n_org, n_ko, n_en, m_src, e_info in persons:
                p_extra = e_info if isinstance(e_info, dict) else {}
                p_media = m_src if isinstance(m_src, dict) else {}

                p_data = {
                    'id': pid,
                    'person_idx': p_idx or '',
                    'name_org': (n_org or '').strip(),
                    'name_ko': (n_ko or '').strip(),
                    'name_en': (n_en or '').strip(),
                    'media_src': p_media,
                    'thumb': None,
                    'extra_info': p_extra
                }

                if p_idx:
                    self.by_idx[p_idx.strip().upper()] = p_data

                if n_org:
                    self.by_name_org[n_org.strip().lower()] = p_data

                if n_ko:
                    self.by_name_ko[n_ko.strip().lower()] = p_data

                if n_en:
                    self.by_name_en[n_en.strip().lower()] = p_data

                # 사이트 고유 배우 ID (DMM 등) 직결 색인
                site_actors = p_extra.get('site_actors') or {}
                if isinstance(site_actors, dict):
                    for s_site, s_obj in site_actors.items():
                        if isinstance(s_obj, dict) and s_obj.get('id'):
                            s_id = str(s_obj['id']).strip()
                            self.by_site_actor[s_id] = p_data
        finally:
            sess.remove()

    def match(self, actor_dict):
        if not isinstance(actor_dict, dict):
            return None

        # 사이트 고유 ID 우선 대조
        extra = actor_dict.get('extra_info') or {}
        s_id = str(extra.get('site_actor_id') or actor_dict.get('site_actor_id') or '').strip()
        if s_id and s_id in self.by_site_actor:
            return self.by_site_actor[s_id]

        # 고유 식별코드 대조
        a_idx = str(actor_dict.get('actor_idx') or actor_dict.get('person_idx') or '').strip().upper()
        if a_idx and a_idx in self.by_idx:
            return self.by_idx[a_idx]

        # 원문 이름 대조
        n_org = str(actor_dict.get('name_org') or actor_dict.get('name') or '').strip().lower()
        if n_org and n_org in self.by_name_org:
            return self.by_name_org[n_org]

        # 영문 이름 대조
        n_en = str(actor_dict.get('name_en') or '').strip().lower()
        if n_en and n_en in self.by_name_en:
            return self.by_name_en[n_en]

        # 한국어 표기명 대조
        n_ko = str(actor_dict.get('name_ko') or '').strip().lower()
        if n_ko and n_ko in self.by_name_ko:
            return self.by_name_ko[n_ko]

        return None
