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
from .util_runtime import storage_logger, redact_diagnostic

logger = storage_logger(logger)


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
    def preferred_user_image(cls, assets, kind, mode):
        def value(asset, key):
            return asset.get(key) if isinstance(asset,dict) else getattr(asset,key,None)
        def stamp(asset):
            date=value(asset,'updated_time')
            return date.isoformat() if hasattr(date,'isoformat') else str(date or '')
        candidates=[a for a in assets if value(a,'media_type')==kind and cls.is_user_media(a)
                    and cls.media_in_storage(a,mode) and isinstance(value(a,'url'),str)
                    and value(a,'url').startswith(('http://','https://'))]
        return max(candidates,key=stamp,default=None)

    @classmethod
    def adopt_item_user_images(cls, item, mode, rel_dir, stem, selected, use_dir_cache=True, observations=None):
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
                if mode == 'gdrive' and observations is not None:
                    initial=cls.image_initial_observation((folder+'/_').lstrip('/'),item.category,observations)
                    class ObservedFiles(dict):
                        listing_complete=True
                    files=ObservedFiles({row['Name'].lower():row['ID'] for row in initial['files']})
                    files.actual_names={str(row['ID']):row['Name'] for row in initial['files']}
                    folders[folder]=files
                elif mode == 'gdrive':
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
                if mode == 'gdrive':
                    MetaPreviewUtil.validate_directory_media(observed, filename)
                found = observed.get(filename.lower())
                if not found:
                    continue
                actual_name=getattr(observed,'actual_names',{}).get(str(found),filename)
                found_path = '/'.join(part for part in (folder, actual_name) if part)
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
    def save_normalized_jpeg(cls, pil_img, save_filepath, replacement=None):
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
            if replacement is None:
                SiteAvBase.publish_image_file(staging, save_filepath)
            else:
                SiteAvBase.publish_image_file(staging, save_filepath, replacement=replacement)
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
    def upload_image_to_gdrive(cls, local_tmp_path, dest_rel_path, category='JAV_CEN', overwrite=True, replacements=None, prechecked=False):
        """신규 통합 원격 업로드 엔진을 활용하여 이미지 업로드 및 사전 검사 수행."""
        if not local_tmp_path or not os.path.exists(local_tmp_path):
            return None

        try:
            with Image.open(local_tmp_path) as image:
                image.verify()
        except Exception as exc:
            logger.error('[MetaUtil] 유효하지 않은 이미지는 업로드하지 않습니다: %s (%s)', local_tmp_path, exc)
            return None

        from .util_preview import MetaPreviewUtil

        clean_rel = dest_rel_path.replace('\\', '/')
        cat_upper = str(category or 'JAV_CEN').upper()
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')
        if os.path.isabs(clean_rel) or '..' in clean_rel.split('/'):
            raise ValueError('Expected a remote relative image path')

        staged = next((e for e in replacements or [] if e['relative_path'] == clean_rel), None)
        # Site-info actor collection has no DB commit owner. Keep its usable cache;
        # actual replacement belongs to the DB-backed PERSON job or explicit crop.
        actor_cache = ('/actors/' in '/' + clean_rel or re.search(r'_(?:PA\d+|P[SPT][A-Za-z0-9_-]+)(?:_user)?\.(?:jpg|jpeg|png|webp)$',clean_rel,re.I))
        safe_overwrite = overwrite if staged is not None or not actor_cache else False
        fid, err = MetaPreviewUtil.sync_file_to_gdrive(
            local_path=local_tmp_path,
            dest_rel_path=clean_rel,
            category=category,
            overwrite=safe_overwrite,
            is_image=True, prechecked=prechecked or staged is not None, replacement=staged
        )

        if not fid:
            logger.error('[MetaUtil] [%s] Google Drive 이미지 업로드 실패', category)
            return None

        return fid

    @classmethod
    def upload_images_to_gdrive(cls, images, category='JAV_CEN', overwrite=True, replacements=None, code=''):
        """Validate and stage each asset, then upload the ready files in one move."""
        from .util_preview import MetaPreviewUtil, MediaReplacement
        entries = replacements if replacements is not None else []
        ready = {}
        observations = {}
        for relative, local in images.items():
            try:
                if relative.startswith('/') or '..' in relative.replace('\\', '/').split('/'):
                    raise ValueError('Expected a remote relative image path')
                with Image.open(local) as image:
                    image.verify()
                entry = next((e for e in entries if e['relative_path'] == relative), None)
                if entry is None:
                    kind = 'landscape' if relative.lower().endswith('_pl.jpg') else 'poster'
                    entry = cls.prepare_storage_replacement(code, category, 'gdrive', relative, kind,
                                                            1 if kind == 'landscape' else 0, observation=observations)
                    entries.append(entry)
                    if entry['had_original'] and not overwrite:
                        MediaReplacement.finish(entry, False)
                        continue
                    MediaReplacement.stage(entry)
                ready[relative] = local
            except Exception as exc:
                logger.warning('[MetaUtil] 이미지 일괄 업로드 준비 실패 (%s): %s', relative, exc)
        confirmed = {}
        try:
            confirmed, error = MetaPreviewUtil.sync_images_to_gdrive(ready, category=category, replacements=entries)
            if error:
                logger.warning('[MetaUtil] 이미지 일괄 업로드: %s', error)
            for relative, file_id in confirmed.items():
                MetaPreviewUtil.register_remote_file(relative, file_id, category=category, is_image=True)
            return confirmed
        finally:
            if replacements is None:
                cls.finish_media_replacements(entries, None if confirmed else False)

    @classmethod
    def prepare_image_replacements(cls, entity, decision):
        from .util_preview import MediaReplacement
        from .util_preview import MetaPreviewUtil
        mode = decision.get('image_mode')
        if mode not in ('gdrive', 'image_server'):
            return []
        entries = []
        entity.extra_info['_media_replacements'] = entries
        decision['_media_replacements'] = entries
        category = 'WESTERN' if entity.code.startswith('W') else 'JAV_UNCEN' if entity.code.startswith('E') else 'JAV_CEN'
        stem = str(entity.ui_code or '').lower()
        pattern = re.compile(r'^' + re.escape(stem) + r'_(p|pl|art_(\d+))\.(jpg|jpeg|png|webp|gif)$', re.I)
        remote = decision.get('gdrive_data') or {}
        local = decision.get('image_server_paths') or {}
        old_files = remote.get('remote_files', {}) if mode == 'gdrive' else {}
        observations = {}
        if mode == 'gdrive':
            cls.image_initial_observation((remote.get('rel_dir', '') + '/_').strip('/'), category, observations, old_files)
        actual_names = getattr(old_files, 'actual_names', {})
        if mode == 'image_server' and local.get('target_folder') and os.path.isdir(local['target_folder']):
            old_files = {name: '' for name in os.listdir(local['target_folder'])}
        names = {name.casefold() if mode == 'gdrive' else name: fid for name, fid in old_files.items() if pattern.match(name)}
        if mode == 'gdrive' and decision.get('rewrite_flag'):
            for row in next(iter(observations.values()))['files']:
                if pattern.match(row['Name']):
                    names.setdefault(row['Name'].casefold(), '')
        sources = decision.get('final_image_sources') or {}
        planned = []
        if sources.get('poster_source') and not sources.get('skip_poster_download'):
            planned.append(stem + '_p.jpg')
        if sources.get('landscape_source') and not sources.get('skip_landscape_download'):
            planned.append(stem + '_pl.jpg')
        planned.extend(stem + '_art_%s.jpg' % (i + 1) for i, _ in enumerate(sources.get('arts') or []))
        for name in planned:
            names.setdefault(name, '')
        for name, old_id in names.items():
            if mode == 'gdrive' and not decision.get('rewrite_flag') and name in getattr(old_files, 'ambiguous_names', set()):
                continue
            match = pattern.match(name)
            exists = bool(old_id) if mode == 'gdrive' else os.path.isfile(os.path.join(local['target_folder'], name))
            relative = ((remote.get('rel_dir', '') + '/' + name).strip('/') if mode == 'gdrive' else
                        os.path.relpath(os.path.join(local['target_folder'], name),
                            P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data, 'images')).replace(os.path.sep, '/'))
            kind = 'poster' if match.group(1).lower() == 'p' else 'landscape' if match.group(1).lower() == 'pl' else 'fanart'
            # Selecting a user asset does not request retirement of its system sibling.
            if kind in ('poster','landscape'):
                suffix = 'p' if kind == 'poster' else 'pl'
                if mode == 'gdrive':
                    selected_id = remote.get('user_' + suffix + '_fid') or (
                        cls._item_drive_id(sources.get(kind + '_source')) if sources.get('skip_' + kind + '_download') else '')
                    if selected_id and any(fid == selected_id and re.fullmatch(
                            re.escape(stem + '_' + suffix + '_user') + r'\.(jpg|jpeg|png|webp|gif)', filename, re.I)
                            for filename,fid in old_files.items()):
                        continue
                else:
                    selected_name = (decision.get('user_files_exist') or {}).get(kind)
                    prefix = str(local.get('url_prefix') or '').rstrip('/') + '/'
                    source = str(sources.get(kind + '_source') or '')
                    if not selected_name and sources.get('skip_' + kind + '_download') and source.startswith(prefix):
                        selected_name = source[len(prefix):]
                    if (selected_name in old_files and re.fullmatch(
                            re.escape(stem + '_' + suffix + '_user') + r'\.(jpg|jpeg|png|webp|gif)', selected_name, re.I)
                            and os.path.isfile(os.path.join(local['target_folder'], selected_name))):
                        continue
            elif 0 < int(match.group(2)) <= len(sources.get('arts') or []):
                # Match the existing finalizer's per-index user extensions and selection.
                user_base = stem + '_art_' + str(int(match.group(2))) + '_user.'
                extensions = ('jpg','jpeg','png','webp') if mode == 'gdrive' else ('jpg','png','webp')
                if mode == 'gdrive':
                    if any((remote.get('remote_files') or {}).get(user_base + ext) and any(
                            filename.casefold() == user_base + ext and fid == remote['remote_files'][user_base + ext]
                            for filename,fid in old_files.items()) for ext in extensions):
                        continue
                elif any(user_base + ext in old_files and os.path.isfile(os.path.join(
                        local['target_folder'], user_base + ext)) for ext in extensions):
                    continue
            spec = dict(storage='gdrive' if mode == 'gdrive' else 'local', relative_path=relative,
                        media_type=kind, sort_order=int(match.group(2)) - 1 if match.group(2) else 0 if kind == 'poster' else 1,
                        code=entity.code, category=category, old_id=old_id, owner_code=(decision.get('refresh_owner') or {}).get('code'))
            if mode == 'gdrive':
                spec.update(target=MetaPreviewUtil.build_rclone_target_remote_path(
                    P.ModelSetting.get('jav_censored_image_gdrive_remote_path'), dest_rel_path=relative),
                    rclone=MetaPreviewUtil.get_setting('rclone_path', category, default='rclone'),
                    config=MetaPreviewUtil.get_setting('rclone_conf', category, default='/data/db/rclone.conf'))
            else:
                spec['target'] = os.path.join(local['target_folder'], name)
            if (mode == 'image_server' and exists and kind in ('poster', 'landscape')
                    and sources.get('skip_' + kind + '_download') and local.get('url_prefix')
                    and sources.get(kind + '_source') == str(local['url_prefix']).rstrip('/') + '/' + name
                    and os.path.getsize(spec['target']) > 0):
                # This selected system asset is already reused by the finalizer.
                MetaPreviewUtil.has_local_file(spec['target'], use_cache=False)
                continue
            if exists and not decision.get('rewrite_flag'):
                if mode == 'image_server':
                    MetaPreviewUtil.has_local_file(spec['target'], use_cache=False)
                continue
            entry = cls.prepare_image_replacement(spec, observations=observations)
            entries.append(entry)
            MediaReplacement.stage(entry)
        return entries

    @classmethod
    def prepare_storage_replacement(cls, code, category, mode, relative_path, media_type, sort_order=0, subject=None, observation=None):
        from .util_preview import MediaReplacement
        from .util_preview import MetaPreviewUtil
        spec = dict(storage='gdrive' if mode == 'gdrive' else 'local', category=category, code=code,
                    relative_path=relative_path, media_type=media_type, sort_order=sort_order, old_id='')
        if subject:
            spec.update(subject)
        if mode == 'gdrive':
            spec.update(target=MetaPreviewUtil.build_rclone_target_remote_path(
                P.ModelSetting.get('jav_censored_image_gdrive_remote_path'), dest_rel_path=relative_path),
                rclone=MetaPreviewUtil.get_setting('rclone_path', category, default='rclone'),
                config=MetaPreviewUtil.get_setting('rclone_conf', category, default='/data/db/rclone.conf'))
        else:
            spec['target'] = os.path.join(P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data, 'images'), relative_path)
        if spec.get('subject_type','item')=='item' and media_type in ('poster','landscape','fanart'):
            return cls.prepare_image_replacement(spec, observations=observation)
        if mode == 'gdrive' and observation is not None:
            spec['old_id'] = observation.get('file_id') or ''
            exists = bool(spec['old_id'])
        else:
            exists = MediaReplacement.exists(spec, spec['target'])
        return MediaReplacement.begin(spec, exists)

    @classmethod
    def image_initial_observation(cls, relative, category, observations, files=None):
        directory = os.path.dirname(relative)
        if directory not in observations:
            from .util_runtime import StorageConflict
            if files is None or not getattr(files, 'listing_complete', False):
                name = os.path.basename(relative)
                match = re.match(r'(.+)_(?:p|pl|art_\d+)\.(?:jpe?g|png|webp|gif)$', name, re.I)
                files = cls.query_gdrive_remote_dir_files(directory, category, code_lower=match.group(1) if match else None, strict=True)
            groups = getattr(files, 'rows_by_name', None)
            rows = ([dict(row) for group in groups.values() for row in group] if groups is not None else
                    [dict(Name=getattr(files, 'actual_names', {}).get(str(fid), name), ID=fid, IsDir=False)
                     for name, fid in files.items()])
            names = [row['Name'].casefold() for row in rows]
            ids = [row['ID'] for row in rows]
            if len(ids) != len(set(ids)):
                raise StorageConflict('초기 저장소 목록의 이름/ID가 중복되어 보류합니다.')
            observations[directory] = dict(directory=directory, files=rows)
        return observations[directory]

    @staticmethod
    def select_image_candidate(rows):
        """Choose a valid image deterministically; missing times cannot prove recency."""
        from datetime import datetime, timezone
        unique = {str(row.get('ID') or ''): row for row in rows if row.get('ID') and
                  row.get('IsDir') is False and type(row.get('Size')) is int and row['Size'] > 0}
        if len(unique) == 1:
            return next(iter(unique.values()))
        dated = []
        for fid, row in unique.items():
            try:
                stamp = datetime.fromisoformat(str(row.get('ModTime') or '').replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                dated.append((stamp.timestamp(), fid, row))
            except (TypeError, ValueError, OverflowError):
                return None
        return max(dated, key=lambda value: value[:2])[2] if dated else None

    @classmethod
    def prepare_image_replacement(cls, spec, manual_plan=None, observations=None):
        """Policy preflight only; all admitted images use MediaReplacement's same lifecycle."""
        from .mod_meta_db import MetaItem, MetaMedia
        from .util_preview import MetaPreviewUtil, MediaReplacement
        from .util_runtime import StorageConflict
        from sqlalchemy.orm import sessionmaker, lazyload, selectinload
        from urllib.parse import urlsplit, unquote
        spec=dict(spec);relative=spec['relative_path'].replace('\\','/')
        if relative.startswith('/') or any(p in ('','.','..') for p in relative.split('/')) or ':' in relative:
            raise StorageConflict('이미지 상대 경로가 저장소를 벗어납니다.')
        name=relative.rsplit('/',1)[-1]
        if '.user_image_' in name or ('_user.' in name.lower() and not manual_plan):
            raise StorageConflict('자동 작업에서 사용자 이미지를 교체할 수 없습니다.')
        plan=dict(root=P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or '' if spec['storage']=='gdrive' else
                  os.path.abspath(P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data,'images')),
                  relative_path=relative,filename=name,created_at=datetime.now().isoformat(),old={})
        module={'WESTERN':'western','JAV_UNCEN':'jav_uncensored','JAV_CEN':'jav_censored'}[spec['category']]
        plan['configured_mode']=(manual_plan or {}).get('configured_mode') or P.ModelSetting.get(module+'_image_mode') or P.ModelSetting.get('jav_censored_image_mode') or 'ff_proxy'
        registry=cls.delete_reference_registry(spec['category'])
        with sessionmaker(bind=registry.get_bind())() as session:
            item=session.query(MetaItem).options(lazyload('*'),selectinload(MetaItem.media_files)).filter_by(code=spec.get('owner_code') or spec['code'],category=spec['category']).first()
            if item:
                rows=[m for m in item.media_files if m.media_type==spec['media_type'] and
                      (spec['media_type']!='fanart' or (m.sort_order or 0)==spec.get('sort_order',0)) and
                      cls.media_in_storage(m,'gdrive' if spec['storage']=='gdrive' else 'image_server')]
                if not manual_plan:rows=[m for m in rows if not cls.is_user_media(m)]
                if manual_plan:
                    expected=manual_plan.get('old') or {}
                    current=next((m for m in rows if m.id==expected.get('id')),None)
                    if (expected and (current is None or
                            (current.updated_time.isoformat() if current.updated_time else '')!=expected['updated_time'] or
                            (current.gdrive_fileid or '')!=expected.get('registered_fid',expected.get('fid','')) or
                            (current.relative_path or '')!=expected.get('registered_path',expected.get('relative_path','')))
                            or not expected and rows):
                        raise StorageConflict('선택한 이미지의 DB 세대가 변경되어 교체를 보류합니다.')
                old=next((m for m in rows if manual_plan and m.id==(manual_plan.get('old') or {}).get('id')),None)
                old=old or next((m for m in rows if m.relative_path==relative),None)
                old=old or next(iter(rows),None)
                if old is not None:
                    equivalent_ids=[media.id for media in rows if cls._item_media_signature(media)==cls._item_media_signature(old)]
                    plan['old']=dict(id=old.id,url=old.url,relative_path=old.relative_path or '',fid=old.gdrive_fileid or '',
                        registered_fid=old.gdrive_fileid or '',registered_path=old.relative_path or '',updated_time=old.updated_time.isoformat() if old.updated_time else '',checksum=old.checksum or '')
                    owned=old.source in ('user','local','image_server','gdrive') and bool(old.relative_path)
                    if manual_plan and old.source=='gds' and spec['storage']=='gdrive':
                        legacy_media=old;plan['old']['legacy_gds']=True
                        plan['old']['equivalent_ids']=equivalent_ids
                    elif not owned:plan['old'].update(fid='',relative_path='')
        plan['old'].setdefault('id',None)
        if manual_plan:
            plan['old']['owner_code']=spec.get('owner_code') or spec['code']
        if not manual_plan:
            # DB fields record the prior registration; the initial storage observation
            # below alone identifies the physical file replaced by automatic work.
            plan['old'].update(fid='',relative_path='',automatic_image=True)
        old=plan['old'];old_relative=old.get('relative_path') or ''
        old_slot_valid=True
        if old_relative:
            # Only the selected item's same P/PL/art slot may authorize a different old filename.
            slot='_pl' if spec['media_type']=='landscape' else '_p' if spec['media_type']=='poster' else '_art_'+str(spec.get('sort_order',0)+1)
            expected=name[:name.lower().rfind(slot)] + slot
            old_name=old_relative.replace('\\','/').rsplit('/',1)[-1]
            old_slot_valid=bool(re.fullmatch(re.escape(expected)+r'(?:_user)?\.(?:jpe?g|png|webp|gif)',old_name,re.I))
            if not manual_plan and cls.is_user_media(old):raise StorageConflict('자동 이미지의 사용자 파일 교체를 차단했습니다.')
        if spec['storage']=='gdrive':
            if not plan['root']:raise StorageConflict('이미지 리모트 루트가 없습니다.')
            spec.update(rclone=MetaPreviewUtil.get_setting('rclone_path',spec['category'],default='rclone'),
                        config=MetaPreviewUtil.get_setting('rclone_conf',spec['category'],default='/data/db/rclone.conf'))
            spec['target']=MetaPreviewUtil.build_rclone_target_remote_path(plan['root'],relative)
            plan['remote_dir']=spec['target'].rsplit('/',1)[0]
            observation = cls.image_initial_observation(relative, spec['category'], observations if observations is not None else {})
            spec['initial_observation'] = observation
            rows = [row for row in observation['files'] if row['Name'].casefold() == name.casefold()]
            if not manual_plan and re.search(r'_(?:p|pl|art_\d+)\.(?:jpe?g|png|webp|gif)$', name, re.I):
                plan['system_candidates'] = [dict(row) for row in rows]
            if rows:
                system = not manual_plan and bool(re.search(r'_(?:p|pl|art_\d+)\.(?:jpe?g|png|webp|gif)$', name, re.I))
                if len(rows)!=1 and not system:raise StorageConflict('정식 이미지 이름이 중복되어 교체를 보류합니다.')
                chosen = cls.select_image_candidate(rows) or sorted(rows, key=lambda row: row['ID'])[0]
                if system:
                    plan['system_candidates'] = [dict(row) for row in rows]
                old_relative='/'.join(filter(None,(observation['directory'],chosen['Name'])))
                old.update(fid=chosen['ID'],relative_path=old_relative,url=cls.format_gdrive_thumbnail_url(chosen['ID']))
                old.pop('legacy_gds',None)
            if old.get('fid'):
                if not rows and not old_slot_valid:
                    raise StorageConflict('다른 작품/타입의 기존 이미지 파일은 변경할 수 없습니다.')
                old_target=MetaPreviewUtil.build_rclone_target_remote_path(plan['root'],old_relative)
                old.update(name=old_relative.rsplit('/',1)[-1],target=old_target)
                if os.path.dirname(old_relative) == observation['directory']:
                    observed = [row for row in observation['files'] if row['Name'].casefold()==old['name'].casefold()]
                    if not any(row['ID']==old['fid'] and row['Name']==old['name'] for row in observed):
                        raise StorageConflict('기존 등록 ID와 최초 폴더 목록이 달라 보존합니다.')
            exists=bool(old.get('fid'))
        else:
            spec['target']=os.path.abspath(os.path.join(plan['root'],relative.replace('/',os.sep)))
            cls.validate_image_local_target(spec['target'],plan['root'])
            current=cls._manual_file_stat(spec['target'])
            if old_relative and not current:
                if not old_slot_valid:
                    raise StorageConflict('다른 작품/타입의 기존 이미지 파일은 변경할 수 없습니다.')
                if '/' not in old_relative:
                    # Legacy basename is admitted only with this configured folder and exact registered URL.
                    prefix=(manual_plan or {}).get('prefix')
                    if not prefix:
                        prefix=(P.ModelSetting.get('jav_censored_image_server_url') or '').rstrip('/')+'/'+os.path.dirname(relative)
                    if old['url']!=prefix.rstrip('/')+'/'+old_relative:raise StorageConflict('구 파일명만 있는 이미지의 등록 URL/폴더가 일치하지 않습니다.')
                    old_relative='/'.join(filter(None,(os.path.dirname(relative),old_relative)))
                old_path=os.path.abspath(os.path.join(plan['root'],old_relative.replace('/',os.sep)))
                cls.validate_image_local_target(old_path,plan['root']);old.update(target=old_path,stat=cls._manual_file_stat(old_path),name=os.path.basename(old_path))
            if current:
                old.update(target=spec['target'],stat=current,name=name,relative_path=relative,
                    url=(P.ModelSetting.get('jav_censored_image_server_url') or '').rstrip('/')+'/'+relative)
            exists=bool(old.get('stat'))
        if exists and not plan.get('system_candidates'):
            reference=dict(category=spec['category'],old=old)
            if cls._manual_image_references(reference):raise StorageConflict('공유된 기존 이미지의 교체를 보류합니다.')
        spec.update(image_contract=1,image_plan=plan,old_id=old.get('fid') or '',original_target=old.get('target') if exists else spec['target'])
        if manual_plan:spec.update(user_plan=manual_plan,explicit_user_image=True)
        entry=MediaReplacement.begin(spec, exists)
        return entry

    @classmethod
    def prepare_person_user_image_replacement(cls, person, relative):
        """Compatibility adapter for the existing PERSON crop caller of the shared transfer helper."""
        from .util_preview import MetaPreviewUtil,MediaReplacement
        from .util_runtime import StorageConflict
        category='WESTERN' if person.domain=='WESTERN' else 'JAV_CEN';root=P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or ''
        if relative.startswith('/') or any(p in ('','.','..') for p in relative.split('/')):raise StorageConflict('인물 이미지 상대 경로 오류')
        binary=MetaPreviewUtil.get_setting('rclone_path',category,default='rclone');config=MetaPreviewUtil.get_setting('rclone_conf',category,default='/data/db/rclone.conf')
        target=MetaPreviewUtil.build_rclone_target_remote_path(root,relative);directory=target.rsplit('/',1)[0]
        plan=dict(root=root,relative_path=relative,filename=relative.rsplit('/',1)[-1],remote_dir=directory,
            created_at=datetime.now().isoformat(),
            configured_mode=P.ModelSetting.get(('western' if category=='WESTERN' else 'jav_censored')+'_actor_image_mode') or 'gds',person_custom=True,old={})
        fid=person.custom_gdrive_fileid or '';old_path=person.custom_remote_path or ''
        if fid:
            if not old_path or old_path.startswith('/') or '..' in old_path.split('/'):raise StorageConflict('인물 구 사용자 이미지 등록 경로가 불명확합니다.')
            old_target=MetaPreviewUtil.build_rclone_target_remote_path(root,old_path)
            plan['old']=dict(id=None,person_id=person.id,fid=fid,url=cls.format_gdrive_thumbnail_url(fid),relative_path=old_path,
                name=old_path.rsplit('/',1)[-1],target=old_target)
        initial = cls.image_initial_observation(relative, category, {})
        rows=[row for row in initial['files'] if row['Name'].casefold()==plan['filename'].casefold()]
        if rows and (len(rows)!=1 or rows[0].get('ID')!=fid or rows[0]['Name']!=plan['filename']):raise StorageConflict('인물 이미지 정식 파일 소유/중복 확인 실패')
        if fid and cls._manual_image_references(dict(category=category,old=plan['old'])):raise StorageConflict('인물 사용자 이미지가 공유되어 보존합니다.')
        spec=dict(image_contract=1,image_plan=plan,initial_observation=initial,subject_type='person',person_id=person.id,person_idx=person.person_idx,person_domain=person.domain,
            category=category,code=person.person_idx,storage='gdrive',relative_path=relative,target=target,rclone=binary,config=config,old_id=fid,
            original_target=plan['old'].get('target',target),media_type='poster',sort_order=0)
        return MediaReplacement.begin(spec, bool(fid))

    @classmethod
    def validate_image_local_target(cls, target, root):
        from .util_runtime import StorageConflict
        root=os.path.abspath(root);target=os.path.abspath(target)
        if root==os.path.sep or os.path.commonpath((root,target))!=root or target==root:
            raise StorageConflict('이미지 저장 경로가 설정 루트를 벗어납니다.')
        cls._manual_file_stat(target)  # Final symlinks rejected; administrator-managed parent links are retained.


    @classmethod
    def prepare_video_replacement(cls, target, category, relative_path, video_type='trailer', observation=None, previous=None, prebackup=False,
                                  replace_existing_preview=False):
        from .util_preview import MediaReplacement
        from .util_preview import MetaPreviewUtil
        extra = target.setdefault('extra_info', {}) if isinstance(target, dict) else target.extra_info
        entries = extra.setdefault('_media_replacements', [])
        storage = MetaPreviewUtil.get_setting('preview_storage_type', category, default='local')
        code = target.get('code') if isinstance(target, dict) else target.code
        spec = dict(storage=storage, category=category, code=code, relative_path=relative_path,
                    media_type=video_type, sort_order=0, old_id='')
        if video_type == 'preview' and replace_existing_preview:
            spec['replace_existing_preview'] = True
        if storage == 'gdrive':
            spec.update(target=MetaPreviewUtil.build_rclone_target_remote_path(
                MetaPreviewUtil.get_setting('preview_gdrive_remote_path', category, default='') or
                MetaPreviewUtil.get_setting('preview_video_folder_id', category, default=''), dest_rel_path=relative_path),
                rclone=MetaPreviewUtil.get_setting('rclone_path', category, default='rclone'),
                config=MetaPreviewUtil.get_setting('rclone_conf', category, default='/data/db/rclone.conf'))
            if video_type in ('trailer', 'preview'):
                from .util_runtime import StorageConflict
                initial = observation.get('initial_observation') if observation else None
                if initial is None:
                    files = cls.query_gdrive_remote_dir_files(os.path.dirname(relative_path), category, strict=True, is_image=False)
                    initial = dict(directory=os.path.dirname(relative_path), files=[dict(Name=getattr(files,'actual_names',{}).get(str(fid),name),ID=fid,IsDir=False) for name,fid in files.items()])
                spec['initial_observation'] = initial
                name = os.path.basename(relative_path)
                rows = [row for row in initial['files'] if row['Name'].casefold()==name.casefold()]
                if rows and (len(rows)!=1 or rows[0].get('IsDir') is not False or
                        not re.fullmatch(r'[A-Za-z0-9_-]+', rows[0].get('ID') or '') or
                        sum(row.get('ID') == rows[0]['ID'] for row in initial['files']) != 1):
                    raise StorageConflict('영상 정식 파일의 초기 이름/ID가 모호합니다.')
                fid = rows[0]['ID'] if rows else ''
                if fid:
                    old_path='/'.join(filter(None,(initial['directory'],rows[0]['Name'])))
                    spec['retired_video']=dict(relative_path=old_path,file_id=fid,target=MetaPreviewUtil.build_rclone_target_remote_path(
                        MetaPreviewUtil.get_setting('preview_gdrive_remote_path',category,default='') or
                        MetaPreviewUtil.get_setting('preview_video_folder_id',category,default=''),old_path))
                    spec['retired_prebackup']=True
                    cls.verify_replaced_video_registration(spec,committed=False)
                    if video_type == 'preview' and not replace_existing_preview:
                        raise StorageConflict('명시적 재생성 없이 기존 프리뷰를 교체할 수 없습니다.')
                exists=False  # ID rename owns the old file; generic path backup must not also run.
            elif observation is not None:
                spec['old_id'] = observation.get('file_id') or ''
                exists = bool(spec['old_id'])
            else:
                exists = MediaReplacement.exists(spec, spec['target'])
        else:
            spec['target'] = os.path.join(MetaPreviewUtil.get_setting('preview_local_path', category, default='/data/previews'), relative_path)
            exists = os.path.isfile(spec['target'])
        entry = MediaReplacement.begin(spec, exists)
        entries.append(entry)
        if not prebackup:
            MediaReplacement.stage(entry)
        return entry

    @classmethod
    def verify_replaced_video_registration(cls, entry, committed=True):
        from .mod_meta_db import MetaMedia, MetaItem, MetaPerson
        from .util_runtime import StorageConflict
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy import or_
        old = entry['retired_video']
        fid = old.get('file_id')
        if not fid or fid == entry.get('new_id'):
            raise StorageConflict('기존 등록 트레일러 ID를 확인할 수 없습니다.')
        owner_engine = cls.delete_reference_registry(entry['category']).get_bind()
        person_engine = cls.delete_reference_registry('PERSON').get_bind()
        engines = {cls.delete_reference_registry(cat).get_bind()
                   for cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'PERSON')}
        for engine in engines:
            with sessionmaker(bind=engine)() as session:
                references = session.query(MetaMedia.id).join(MetaItem, MetaMedia.item_id == MetaItem.id).filter(
                    MetaMedia.gdrive_fileid == fid)
                if engine is owner_engine:
                    references = references.filter(~((MetaItem.category == entry['category']) &
                        (MetaItem.code == entry['code'])))
                if references.first():
                    raise StorageConflict('기존 트레일러 ID가 다른 등록행에 공유되어 보존합니다.')
                if engine is person_engine and session.query(MetaPerson.id).filter(or_(
                        MetaPerson.gdrive_fileid == fid, MetaPerson.official_cached_gdrive_fileid == fid,
                        MetaPerson.custom_gdrive_fileid == fid)).first():
                    raise StorageConflict('기존 트레일러 ID가 인물 이미지에 참조되어 보존합니다.')

    @classmethod
    def prebackup_registered_video(cls, entry, operation='stage'):
        """Current-request ID-only backup/restore/cleanup of a registered old trailer."""
        from .util_preview import MetaPreviewUtil, MediaReplacement
        from .util_runtime import StorageConflict
        import requests
        phases = {'stage': 'pending', 'restore': 'rolling_back', 'delete': 'committing'}
        if (not entry.get('retired_prebackup') or operation not in phases or
                entry['status'] != phases[operation]):
            raise StorageConflict('기존 트레일러 이름 변경/정리 단계가 확정되지 않았습니다.')
        if operation == 'restore' and not entry.get('retired_rename_started'):
            return
        if operation == 'restore' and entry.get('retired_restored'):
            return
        if operation == 'delete' and entry.get('retired_deleted'):
            return
        if entry.get('initial_observation'):
            old=entry['retired_video'];name=entry['retired_original_name'];backup=entry['retired_backup_name']
            root=MetaPreviewUtil.get_setting('preview_gdrive_remote_path',entry['category'],default='') or MetaPreviewUtil.get_setting('preview_video_folder_id',entry['category'],default='')
            if (MetaPreviewUtil.build_rclone_target_remote_path(root,old['relative_path'])!=old['target'] or
                    MetaPreviewUtil.build_rclone_target_remote_path(root,entry['relative_path'])!=entry['target'] or
                    MetaPreviewUtil.get_setting('rclone_conf',entry['category'],default='/data/db/rclone.conf')!=entry['config']):
                raise StorageConflict('트레일러 저장소 설정 변경으로 보존합니다.')
            if operation=='stage' and entry.get('retired_staged'):return
            if operation=='stage':
                if any(row['Name'].casefold()==backup.casefold() for row in entry['initial_observation']['files']):raise StorageConflict('트레일러 백업 이름 충돌')
                entry.update(status='pending', **{'retired_rename_started': True})
            elif operation=='restore':
                entry.update(status='rolling_back', **{'retired_restore_started': True})
            else:
                if not entry.get('retired_staged'):raise StorageConflict('구 트레일러 백업 완료 근거 없음')
                entry.update(status='committing', **{'retired_delete_started': True})
            MediaReplacement.mutate_observed_file(entry,old['file_id'],
                name if operation=='stage' else backup, root,
                backup if operation=='stage' else name if operation=='restore' else None)
            entry.update(status=phases[operation], **{{'stage': 'retired_staged', 'restore': 'retired_restored', 'delete': 'retired_deleted'}[operation]: True})
            return
        cls.verify_replaced_video_registration(entry, committed=operation == 'delete')
        old = entry['retired_video']
        fid = old['file_id']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', fid):
            raise StorageConflict('기존 트레일러 File ID 형식 오류입니다.')
        root = MetaPreviewUtil.get_setting('preview_gdrive_remote_path', entry['category'], default='') or MetaPreviewUtil.get_setting('preview_video_folder_id', entry['category'], default='')
        if (MetaPreviewUtil.build_rclone_target_remote_path(root, old['relative_path']) != old['target'] or
                MetaPreviewUtil.build_rclone_target_remote_path(root, entry['relative_path']) != entry['target'] or
                MetaPreviewUtil.get_setting('rclone_conf', entry['category'], default='/data/db/rclone.conf') != entry['config']):
            raise StorageConflict('저장소 설정이 변경되어 기존 트레일러 파일 변경을 보류합니다.')
        directory, original_name = old['target'].rsplit('/', 1)
        backup_name = original_name + '.old_' + entry['identifier']
        if any(c in original_name for c in '*?[]{}'):
            raise StorageConflict('기존 트레일러 이름 조회 범위를 확인할 수 없습니다.')
        parent = MetaPreviewUtil.remote_stat([entry['rclone'], '--config', entry['config'], 'lsjson', '--stat', directory],
            category=entry['category'], purpose='replacement')
        if not parent or parent.get('IsDir') is not True:
            raise StorageConflict('기존 트레일러 부모 폴더를 확인하지 못했습니다.')
        parent_id = parent.get('ID') or parent.get('id')
        token = MetaPreviewUtil.get_gdrive_access_token(entry['config'], old['target'].split(':', 1)[0], file_id=fid)
        if not token:
            raise StorageConflict('기존 트레일러 인증을 확인할 수 없습니다.')
        url = 'https://www.googleapis.com/drive/v3/files/' + fid
        options = dict(params={'fields': 'id,name,parents,trashed', 'supportsAllDrives': 'true'},
                       headers={'Authorization': 'Bearer ' + token}, timeout=15)
        response = requests.get(url, **options)
        try:
            identity = response.json() if response.status_code == 200 else {}
            if identity.get('id') != fid or identity.get('trashed') is not False or identity.get('parents') != [parent_id]:
                raise StorageConflict('기존 트레일러 ID/부모 확인 실패로 보류합니다 (HTTP %s).' % response.status_code)
        finally:
            response.close()
        if not entry.get('retired_parents'):
            if operation != 'stage' or identity.get('name') != original_name:
                raise StorageConflict('기존 트레일러 원래 이름/부모 증거가 없습니다.')
            entry.update(status='pending', **dict(retired_parents=[parent_id], retired_original_name=original_name, retired_backup_name=backup_name))
        if (entry['retired_parents'] != [parent_id] or entry['retired_original_name'] != original_name or
                entry['retired_backup_name'] != backup_name or identity.get('name') not in (original_name, backup_name)):
            raise StorageConflict('기존 트레일러 이름/부모가 예상과 달라 변경을 보류합니다.')

        def name_rows(name):
            result = MetaPreviewUtil.run_rclone_lookup([entry['rclone'], '--config', entry['config'], 'lsjson', directory,
                '--files-only', '--max-depth', '1', '--ignore-case', '--include', name], category=entry['category'], purpose='replacement')
            if result.returncode or not result.stdout.strip():
                raise StorageConflict('기존 트레일러 이름 조회가 실패했습니다.')
            rows = json.loads(result.stdout)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise StorageConflict('기존 트레일러 이름 조회 형식 오류입니다.')
            return [row for row in rows if str(row.get('Name') or '').lower() == name.lower()]

        if operation == 'delete':
            rows = name_rows(backup_name)
            if (not entry.get('retired_staged') or identity.get('name') != backup_name or len(rows) != 1 or
                    rows[0].get('Name') != backup_name or (rows[0].get('ID') or rows[0].get('id')) != fid):
                raise StorageConflict('구 트레일러 백업 이름/ID가 일치하지 않아 삭제를 보류합니다.')
            cls.verify_replaced_video_registration(entry, committed=True)
            entry.update(status='committing', **{'retired_delete_started': True})
            response = requests.delete(url, params={'supportsAllDrives': 'true'}, headers=options['headers'], timeout=15)
            try:
                if response.status_code not in (200, 204):
                    raise StorageConflict('구 트레일러 백업 삭제 확인 실패 (HTTP %s)' % response.status_code)
            finally:
                response.close()
            entry.update(status='committing', **{'retired_deleted': True})
            logger.debug('[MediaReplacement] DB commit 후 구 트레일러 백업 삭제 완료: %s', old['relative_path'])
            return

        wanted = backup_name if operation == 'stage' else original_name
        if identity['name'] == wanted:
            if operation == 'stage' and not entry.get('retired_rename_started'):
                raise StorageConflict('사전 rename 호출 근거 없이 이름이 변경되어 보류합니다.')
            entry.update(status=phases[operation], **{'retired_staged': True} if operation == 'stage' else {'retired_restored': True})
            return
        rows = name_rows(original_name)
        if operation == 'stage' and sum(row.get('Name') == original_name and
                (row.get('ID') or row.get('id')) == fid and row.get('IsDir') is False for row in rows) != 1:
            raise StorageConflict('기존 트레일러의 실제 파일 이름/ID를 확인하지 못했습니다.')
        allowed = {(original_name, fid), (os.path.basename(entry['relative_path']), entry.get('new_id') or entry.get('old_id'))}
        if (len({row.get('Name') for row in rows}) != len(rows) or any(
                (row.get('Name'), row.get('ID') or row.get('id')) not in allowed or row.get('IsDir') is not False for row in rows)):
            raise StorageConflict('기존 트레일러 원래 이름의 중복/다른 ID로 변경을 보류합니다.')
        if operation == 'stage':
            backup = MetaPreviewUtil.remote_stat([entry['rclone'], '--config', entry['config'], 'lsjson', '--stat',
                directory + '/' + backup_name], category=entry['category'], purpose='backup_probe')
            if backup is not None:
                raise StorageConflict('트레일러 백업 이름이 이미 사용 중입니다.')
        elif any(row.get('Name') == original_name and (row.get('ID') or row.get('id')) != fid for row in rows):
            raise StorageConflict('다른 파일이 원래 트레일러 이름을 사용하여 복원을 보류합니다.')
        cls.verify_replaced_video_registration(entry, committed=False)
        entry.update(status=phases[operation], **{'retired_rename_started': True} if operation == 'stage' else {'retired_restore_started': True})
        response = requests.patch(url, json={'name': wanted}, **options)
        try:
            changed = response.json() if response.status_code == 200 else {}
            if (changed.get('id') != fid or changed.get('name') != wanted or changed.get('parents') != entry['retired_parents']
                    or changed.get('trashed') is not False):
                raise StorageConflict('트레일러 rename 결과의 ID/이름/부모 확인 실패 (HTTP %s)' % response.status_code)
        finally:
            response.close()
        entry.update(status=phases[operation], **{'retired_staged': True} if operation == 'stage' else {'retired_restored': True})
        logger.debug('[MediaReplacement] 구 트레일러 %s 완료: %s',
            '사전 백업 rename' if operation == 'stage' else '원래 이름 복원', old['relative_path'])

    @classmethod
    def cleanup_replaced_video(cls, entry):
        """Only a committed current replacement may retire its exact registered Drive ID."""
        from .util_preview import MetaPreviewUtil, MediaReplacement
        from .util_runtime import StorageConflict
        old = entry['retired_video']
        if entry.get('retired_prebackup'):
            return cls.prebackup_registered_video(entry, 'delete')
        fid = old.get('file_id')
        if entry['status'] != 'committing' or not fid or fid == entry.get('new_id'):
            raise StorageConflict('구 트레일러 ID 또는 DB commit 근거가 없어 정리를 보류합니다.')
        def verify_registration():
            cls.verify_replaced_video_registration(entry, committed=True)
        verify_registration()
        directory, name = old['target'].rsplit('/', 1)
        if any(c in name for c in '*?[]{}'):
            raise StorageConflict('구 트레일러 파일명 조회 범위를 확인할 수 없습니다.')
        result = MetaPreviewUtil.run_rclone_lookup([entry['rclone'], '--config', entry['config'],
            'lsjson', directory, '--files-only', '--max-depth', '1', '--ignore-case', '--include', name],
            category=entry['category'], purpose='cleanup')
        if result.returncode or not result.stdout.strip():
            raise StorageConflict('구 트레일러의 실제 경로/ID 조회가 실패해 보존합니다.')
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise StorageConflict('구 트레일러 목록을 확인할 수 없어 보존합니다.')
        matches = [row for row in rows if str(row.get('Name') or '').lower() == name.lower()]
        exact = [row for row in matches if row.get('Name') == name and (row.get('ID') or row.get('id')) == fid]
        # The known old/new case-only pair is unambiguous by ID. Any additional
        # same-name/case alias remains blocked; deletion itself is by exact ID.
        allowed = {(name, fid), (os.path.basename(entry['relative_path']), entry['new_id'])}
        if len(exact) != 1 or len({row.get('Name') for row in matches}) != len(matches) or any(
                (row.get('Name'), row.get('ID') or row.get('id')) not in allowed or row.get('IsDir') is not False for row in matches):
            raise StorageConflict('구 트레일러의 동명/대소문자 중복 또는 ID 불일치로 정리를 보류합니다.')
        root = MetaPreviewUtil.get_setting('preview_gdrive_remote_path', entry['category'], default='') or MetaPreviewUtil.get_setting('preview_video_folder_id', entry['category'], default='')
        if MetaPreviewUtil.build_rclone_target_remote_path(root, old['relative_path']) != old['target']:
            raise StorageConflict('트레일러 저장소 설정이 변경되어 구파일을 보존합니다.')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', fid):
            raise StorageConflict('구 트레일러 File ID 형식이 올바르지 않습니다.')
        token = MetaPreviewUtil.get_gdrive_access_token(entry['config'], old['target'].split(':', 1)[0], file_id=fid)
        if not token:
            raise StorageConflict('구 트레일러 정리 인증을 확인할 수 없습니다.')
        import requests
        url = 'https://www.googleapis.com/drive/v3/files/' + fid
        headers = {'Authorization': 'Bearer ' + token}
        response = requests.get(url, params={'fields': 'id,name,trashed', 'supportsAllDrives': 'true'}, headers=headers, timeout=15)
        try:
            identity = response.json() if response.status_code == 200 else {}
            if identity.get('id') != fid or identity.get('name') != name or identity.get('trashed') is not False:
                raise StorageConflict('구 트레일러의 Drive ID/이름을 확인하지 못해 보존합니다.')
        finally:
            response.close()
        verify_registration()
        response = requests.delete(url, params={'supportsAllDrives': 'true'}, headers=headers, timeout=15)
        try:
            if response.status_code not in (200, 204):
                raise StorageConflict('구 트레일러 삭제 확인 실패 (HTTP %s)' % response.status_code)
        finally:
            response.close()
        logger.debug('[MediaReplacement] DB commit 후 구 트레일러 삭제 완료: %s', old['relative_path'])

    @classmethod
    def settle_image_replacements(cls, entity, decision):
        from .util_preview import MediaReplacement
        entries = decision.get('_media_replacements') or []
        observations = (entity.extra_info or {}).get('_storage_media') or []
        state = (entity.extra_info or {}).get('_image_storage_state') or {}
        for entry in entries:
            observed = next((a for a in observations if a['relative_path'].rsplit('/', 1)[-1] == entry['relative_path'].rsplit('/', 1)[-1]), None)
            if observed:
                if entry.get('storage')=='local' and not entry.get('image_contract'):
                    MediaReplacement.generated(entry, observed.get('gdrive_fileid'))
                elif not entry.get('image_verified'):
                    MediaReplacement.generated(entry, observed.get('gdrive_fileid'))
                if entry.get('new_checksum'):observed['checksum']=entry['new_checksum']
            elif state.get('status') == 'complete' or any(
                    a['media_type'] == entry['media_type'] and a.get('sort_order', 0) == entry['sort_order'] for a in observations):
                MediaReplacement.removed(entry)
            else:
                MediaReplacement.finish(entry, False)

    @classmethod
    def finish_media_replacements(cls, entries, commit):
        from .util_preview import MediaReplacement
        warnings = []
        for entry in entries or []:
            if not entry or entry.get('_finished'):
                continue
            try:
                if commit is None:
                    entry['_finished'] = True
                    entry['commit_unknown'] = True
                    entry['cleanup_pending'] = True
                    logger.warning('[MediaReplacement] [%s/%s/%s] DB 반영 결과 불명, 현재 파일/백업 보존: %s (백업=%s, 신규 ID=%s)', entry.get('category'), entry.get('code'), entry.get('media_type'), entry.get('relative_path'), entry.get('backup') or entry.get('retired_backup_name'), entry.get('new_id'))
                    warnings.append('commit_outcome_unknown')
                else:
                    MediaReplacement.finish(entry, commit)
                    if entry.get('cleanup_pending'):
                        warnings.append('media_cleanup_pending')
            except Exception:
                entry['cleanup_pending'] = True
                logger.exception('[MediaReplacement] [%s/%s/%s] 현재 요청 파일 정리/복원 실패: %s (백업=%s, 기존 ID=%s, 신규 ID=%s)', entry.get('category'), entry.get('code'), entry.get('media_type'), entry.get('relative_path'), entry.get('backup') or entry.get('retired_backup_name'), entry.get('old_id') or (entry.get('retired_video') or {}).get('file_id'), entry.get('new_id'))
                warnings.append('media_cleanup_pending')
        return warnings

    @classmethod
    def defer_media_replacements(cls, session, entries):
        from sqlalchemy import event
        actual_session = session() if hasattr(session, 'registry') else session
        actual_session.info.setdefault('media_replacements', []).extend(entries)
        if actual_session.info.get('media_replacement_events'):
            return
        actual_session.info['media_replacement_events'] = True
        def committing(sess):
            if not sess.in_nested_transaction():
                sess.info['media_replacement_commit_pending'] = True
        def committed(sess):
            if not sess.in_nested_transaction():
                sess.info.pop('media_replacement_commit_pending', None)
                cls.finish_media_replacements(sess.info.pop('media_replacements', []), True)
        def rolled_back(sess):
            if not sess.in_nested_transaction():
                outcome = None if sess.info.pop('media_replacement_commit_pending', False) else False
                cls.finish_media_replacements(sess.info.pop('media_replacements', []), outcome)
        def ended(sess, transaction):
            if transaction.parent is None:
                # Session.close() also abandons an uncommitted transaction.
                outcome = None if sess.info.pop('media_replacement_commit_pending', False) else False
                cls.finish_media_replacements(sess.info.pop('media_replacements', []), outcome)
        event.listen(actual_session, 'before_commit', committing)
        event.listen(actual_session, 'after_commit', committed)
        event.listen(actual_session, 'after_rollback', rolled_back)
        event.listen(actual_session, 'after_transaction_end', ended)


    @classmethod
    def query_gdrive_remote_dir_files(cls, dest_rel_dir, category='JAV_CEN', code_lower=None, strict=False, is_image=True):
        """rclone lsjson으로 구글 드라이브 원격 대상 디렉토리의 파일 목록과 File ID를 직접 타겟팅 조회"""
        from .util_preview import MetaPreviewUtil
        cat_upper = str(category or 'JAV_CEN').upper()
        module_pfx = 'western' if cat_upper in ['WESTERN', 'WEST'] else ('jav_uncensored' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else 'jav_censored')

        rclone_bin = MetaPreviewUtil.get_setting("rclone_path", category, default="rclone")
        rclone_conf = MetaPreviewUtil.get_setting("rclone_conf", category, default="/data/db/rclone.conf")

        raw_upload_remote = (
            (P.ModelSetting.get("jav_censored_image_gdrive_remote_path") or '') if is_image else
            (MetaPreviewUtil.get_setting('preview_gdrive_remote_path', category, default='') or
             MetaPreviewUtil.get_setting('preview_video_folder_id', category, default=''))
        ).strip()

        if not raw_upload_remote:
            if strict:
                raise ValueError('GDrive 미디어 리모트 경로가 설정되지 않았습니다.')
            return {}

        remote_dir = MetaPreviewUtil.build_rclone_target_remote_path(raw_upload_remote, dest_rel_path=dest_rel_dir)
        if not remote_dir:
            if strict:
                raise ValueError('GDrive 미디어 리모트 경로를 해석하지 못했습니다.')
            return {}

        cmd = [rclone_bin, "--config", rclone_conf, "lsjson"]
        clean_code = str(code_lower or '').strip().lower()
        if clean_code:
            cmd.extend(["--include", f"{clean_code}_*", "--ignore-case"])
        cmd.append(remote_dir)

        class ObservedFileMap(dict):
            listing_complete = False

        try:
            res = MetaPreviewUtil.run_rclone_lookup(cmd, category=category, purpose='image_batch' if is_image else 'video')
            if strict and res.returncode != 0:
                missing_dir = MetaPreviewUtil.remote_missing(res, directory=True)
                if missing_dir:
                    file_map = ObservedFileMap()
                    file_map.actual_names = {}
                    if is_image:
                        file_map.rows_by_name = {}
                        file_map.ambiguous_names = set()
                    file_map.listing_complete = True
                    return file_map
                raise RuntimeError('GDrive 미디어 폴더 목록 조회에 실패했습니다 (exit=%s): %s' %
                                   (res.returncode, str(res.stderr or '').strip()[-300:]))
            if strict and res.returncode == 0 and not res.stdout.strip():
                raise RuntimeError('GDrive 미디어 폴더 목록 응답이 비어 있습니다.')
            if res.returncode == 0 and res.stdout.strip():
                items = json.loads(res.stdout.strip())
                if strict and not isinstance(items, list):
                    raise ValueError('GDrive 미디어 폴더 목록 응답 형식이 올바르지 않습니다.')
                file_map = ObservedFileMap()
                file_map.actual_names = {}
                if is_image:
                    file_map.rows_by_name = {}
                    file_map.ambiguous_names = set()
                for item in items:
                    if strict and not isinstance(item, dict):
                        raise ValueError('GDrive 미디어 목록 항목 형식이 올바르지 않습니다.')
                    system_image = False
                    if strict and isinstance(item, dict):
                        name = str(item.get('Name') or item.get('Path') or '')
                        system_image = is_image and bool(re.search(r'_(?:p|pl|art_\d+)\.(?:jpe?g|png|webp|gif)$', name, re.I))
                        if name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif') if is_image else ('.mp4',)):
                            damaged_system_image = (system_image and item.get('IsDir') is False and
                                                    type(item.get('Size')) is int and item['Size'] <= 0)
                            if not damaged_system_image:
                                MetaPreviewUtil.validate_remote_media(item)
                    if isinstance(item, dict) and not item.get('IsDir'):
                        fname = item.get('Name') or item.get('Path')
                        fid = item.get('ID') or item.get('id')
                        if strict and not fname:
                            raise ValueError('GDrive 미디어 목록 항목에 파일명이 없습니다.')
                        if strict and not system_image and fname and str(fname).lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif') if is_image else ('.mp4',)) and item.get('Size') == 0:
                            raise RuntimeError('GDrive에 비어 있는 이미지 파일이 있습니다: ' + str(fname))
                        if strict and fname and str(fname).lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif') if is_image else ('.mp4',)) and not fid:
                            raise RuntimeError('GDrive 미디어 File ID가 없습니다: ' + str(fname))
                        if is_image and fname and fid:
                            key = fname.lower()
                            if str(fid) in file_map.actual_names:
                                if file_map.actual_names[str(fid)].casefold() != key:
                                    raise ValueError('동일 이미지 ID의 파일명이 서로 다릅니다.')
                                continue
                            file_map.rows_by_name.setdefault(key, []).append(dict(item, Name=fname, ID=str(fid)))
                            file_map.actual_names[str(fid)] = fname
                            system = bool(re.search(r'_(?:p|pl|art_\d+)\.(?:jpe?g|png|webp|gif)$', key))
                            if system:
                                selected = cls.select_image_candidate(file_map.rows_by_name[key])
                                if selected:
                                    file_map[key] = selected['ID']
                                    file_map.ambiguous_names.discard(key)
                                else:
                                    file_map.pop(key, None)
                                    file_map.ambiguous_names.add(key)
                                continue
                            if strict and key in file_map:
                                raise ValueError('보호 이미지 파일명이 중복됩니다.')
                            file_map[key] = str(fid)
                            continue
                        if strict and fname and fid and (fname.lower() in file_map or str(fid) in file_map.actual_names):
                            raise ValueError('GDrive 미디어 목록의 이름/ID가 중복됩니다.')
                        if fname and fid:
                            file_map[fname.lower()] = fid
                            file_map.actual_names[str(fid)] = fname
                file_map.listing_complete = bool(strict)
                target_log = f" (타겟 필터: {clean_code}_*)" if clean_code else ""
                logger.debug('[MetaUtil] [%s] 저장소 미디어 목록 조회 완료: %s개', category, len(file_map))
                return file_map
        except Exception as e:
            logger.exception('[MetaUtil] 구글 드라이브 폴더 조회 예외: category=%s', category)
            if strict:
                raise

        return {}

    @classmethod
    def api_exception(cls, exc, site=None):
        """Only request-local transport evidence can identify an upstream failure."""
        from requests.exceptions import Timeout, HTTPError, ConnectionError, RequestException
        status, code, reason = 500, 'ff_internal_error', 'internal_error'
        upstream = None
        if isinstance(exc, (Timeout, HTTPError, ConnectionError)):
            response = getattr(exc, 'response', None)
            remote_status = getattr(response, 'status_code', None)
            remote_status = remote_status if type(remote_status) is int else None
            if isinstance(exc, Timeout):
                status, code, reason = 504, 'upstream_timeout', 'timeout'
            elif isinstance(exc, ConnectionError):
                status, code, reason = 502, 'upstream_unavailable', 'connection_failed'
            elif remote_status is not None:
                status = 503 if remote_status == 429 else 504 if remote_status == 504 else 502
                code, reason = 'upstream_http_error', 'http_error'
            if status != 500:
                upstream = {'http_status': remote_status, 'reason': reason}
                if site:
                    upstream['site'] = site
        body = {'ret': 'error', 'error_code': code,
                'error_origin': 'external' if upstream else 'unknown' if isinstance(exc, RequestException) else 'ff',
                'msg': '외부 서비스 요청을 완료하지 못했습니다.' if upstream else 'FF 요청 처리에 실패했습니다. 로그를 확인하세요.'}
        if upstream is not None:
            body['upstream'] = upstream
        return body, status

    @staticmethod
    def _image_error(code, message, origin='ff', upstream=None):
        error = ValueError(message)
        error.error_code, error.error_origin = code, origin
        error.upstream = upstream
        return error

    @staticmethod
    def image_api_response(result):
        """Map explicit image/command outcomes without changing their commit evidence."""
        from flask import jsonify
        if result.get('ret') in ('success', 'warning') and not result.get('uncertain'):
            return jsonify(result), 200
        result = dict(result)
        code = result.get('error_code') or 'ff_internal_error'
        status = {'invalid_request': 400, 'unsupported_command': 400, 'not_found': 404,
                  'storage_conflict': 409, 'schema_not_writable': 409,
                  'database_unavailable': 503, 'commit_outcome_unknown': 503,
                  'storage_asset_incomplete': 502, 'upstream_http_error': 502,
                  'download_failed': 502,
                  'upstream_unavailable': 502, 'upstream_timeout': 504}.get(code, 500)
        if result.get('uncertain') or result.get('cleanup_pending'):
            status = 502 if result.get('committed') else 503
        elif result.get('error_origin') == 'external' and isinstance(result.get('upstream'), dict):
            remote = result['upstream'].get('http_status')
            status = 503 if remote == 429 else 504 if remote == 504 or code == 'upstream_timeout' else 502
        result.setdefault('error_code', code)
        result.setdefault('error_origin', 'unknown')
        result.pop('log', None)
        if code == 'ff_internal_error':
            result['msg'] = '요청 처리에 실패했습니다. FF 로그를 확인하세요.'
        if result.get('uncertain') or result.get('cleanup_pending'):
            result['msg'] = '처리 결과 확인이 필요합니다. 재시도하지 말고 상세 상태를 새로 조회하세요.'
        return jsonify(result), status

    @classmethod
    def api_command_response(cls, response):
        from flask import make_response, jsonify
        if response is None:
            return jsonify(ret='error', error_code='unsupported_command', error_origin='ff', msg='지원하지 않는 명령입니다.'), 400
        response = make_response(response)
        body = response.get_json(silent=True) if response.is_json else None
        mapped_codes = ('invalid_request', 'unsupported_command', 'not_found', 'storage_conflict',
                        'schema_not_writable', 'database_unavailable', 'lookup_failed', 'ff_internal_error',
                        'commit_outcome_unknown', 'storage_asset_incomplete', 'download_failed',
                        'upstream_http_error', 'upstream_unavailable', 'upstream_timeout', 'preview_failed')
        if (response.status_code == 200 and isinstance(body, dict) and
                body.get('ret') in ('error', 'fail', 'failed', 'exception') and
                body.get('error_code') in mapped_codes):
            return cls.image_api_response(body)
        return response

    @classmethod
    def user_image_command(cls, command, code, crop_data=None, source_payload=None, category=None, request=None):
        """One legacy-compatible input adapter for login/API protected image routes."""
        from .mod_meta_db import ModuleMetaDb
        phase = 'validation'
        try:
            form = request.values if request is not None else {}
            body = request.get_json(silent=True) if request is not None and request.is_json else {}
            if not isinstance(body, dict): raise cls._image_error('invalid_request', '요청은 JSON 객체여야 합니다.')
            code = code or body.get('code') or form.get('code')
            if not isinstance(code, str) or not code.strip(): raise cls._image_error('invalid_request', '작품 코드가 필요합니다.')
            body = dict(body)
            for legacy, canonical in (('image_url','url'), ('path','url'), ('image_base64','data')):
                value = body.get(legacy) or form.get(legacy)
                if value:
                    if (body.get(canonical) or form.get(canonical)) not in (None, '', value): raise cls._image_error('invalid_request', '이미지 요청 원본이 충돌합니다.')
                    body[canonical] = value
            payload = {}
            if isinstance(source_payload, dict): payload = dict(source_payload)
            elif source_payload and str(source_payload).strip().startswith('{'): payload = json.loads(source_payload)
            elif source_payload in ('p', 'pl'): payload['type'] = source_payload
            elif source_payload: raise cls._image_error('invalid_request', '이미지 소스 payload는 JSON이어야 합니다.')
            if not isinstance(payload, dict): raise cls._image_error('invalid_request', '이미지 소스 payload는 JSON 객체여야 합니다.')
            for key in ('type', 'data', 'url', 'crop_url', 'category', 'p_base64', 'pl_base64'):
                value = body.get(key) or form.get(key)
                if value:
                    if payload.get(key) and payload[key] != value: raise cls._image_error('invalid_request', '이미지 요청 필드가 서로 충돌합니다: ' + key)
                    payload[key] = value
            requested = payload.get('category')
            if category and requested and category != requested: raise cls._image_error('invalid_request', '작품 카테고리가 일치하지 않습니다.')
            category = category or requested
            if not category:
                phase = 'database'
                category = ModuleMetaDb.find_item_category(code)
                phase = 'validation'
            if category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') or not code: raise cls._image_error('invalid_request', '작품 코드와 AV 카테고리를 확인하세요.')
            is_crop = command in ('crop_save', 'db_crop_save')
            if is_crop:
                coords = body.get('crop_data') or crop_data or form.get('crop_data')
                if isinstance(coords, str): coords = json.loads(coords)
                if not isinstance(coords, dict): raise cls._image_error('invalid_request', '크롭 좌표가 없습니다.')
                kind = payload.get('type') or coords.get('source_type') or ('p' if payload.get('p_base64') else 'pl')
                if kind not in ('p', 'pl') or (coords.get('source_type') and coords['source_type'] != kind): raise cls._image_error('invalid_request', '크롭 소스 타입이 일치하지 않습니다.')
                data = payload.get('data') or payload.get('p_base64' if kind == 'p' else 'pl_base64')
                if payload.get('p_base64') and kind != 'p' or payload.get('pl_base64') and kind != 'pl': raise cls._image_error('invalid_request', '선택한 이미지와 업로드 타입이 다릅니다.')
                aliases = [v for v in (payload.get('data'), payload.get('p_base64'), payload.get('pl_base64')) if v]
                if len(set(aliases)) > 1: raise cls._image_error('invalid_request', '업로드 원본 필드가 충돌합니다.')
                url = payload.get('crop_url') or payload.get('url') or coords.get('crop_url')
                if payload.get('crop_url') and payload.get('url') and payload['crop_url'] != payload['url']: raise cls._image_error('invalid_request', '원본 URL이 충돌합니다.')
                success, result = cls._save_manual_user_images(code, category, kind, data, url, coords)
            else:
                kind = payload.get('type') or body.get('image_type') or form.get('image_type')
                url = payload.get('url') or crop_data
                if payload.get('url') and crop_data and payload['url'] != crop_data: raise cls._image_error('invalid_request', '원본 URL이 충돌합니다.')
                success, result = cls._save_manual_user_images(code, category, kind, payload.get('data'), url)
            if not isinstance(result, dict): result = {'msg': str(result)}
            return dict(result, ret='success' if success else 'error', new_url=str(result.get('new_url') or ''),
                        data=result.get('data') or {}, msg=str(result.get('msg') or ('사용자 이미지 저장 완료' if success else '사용자 이미지 저장 실패')))
        except (ValueError, TypeError, KeyError) as exc:
            logger.exception('[MetaUtil] 사용자 이미지 요청 검증 실패')
            if phase == 'database':
                return dict(ret='error', error_code='ff_internal_error', error_origin='ff',
                            msg='메타 DB 조회에 실패했습니다.', new_url='', data={}, committed=False)
            return {'ret': 'error', 'new_url': '', 'data': {}, 'committed': False,
                    'error_code': 'invalid_request', 'error_origin': 'ff',
                    'msg': str(exc) if getattr(exc, 'error_code', None) == 'invalid_request' else '이미지 요청의 코드·타입·원본·크롭 좌표를 확인하세요.'}
        except Exception as exc:
            logger.exception('[MetaUtil] 사용자 이미지 요청 처리 실패')
            body, _ = cls.api_exception(exc)
            return dict(body, new_url='', data={}, committed=False)

    @classmethod
    def _manual_image_source(cls, item, category, source_type, data, url):
        """Load exactly one selected original; never fall back to the DB image."""
        from PIL import ImageOps
        from support_site import SiteAvBase
        from support_site.ssrf_guard import public_scope, unwrap_image_url
        if source_type not in ('p', 'pl') or bool(data) == bool(url):
            raise cls._image_error('invalid_request', 'P/PL 타입과 하나의 원본 파일 또는 URL만 지정하세요.')
        limit = 32 * 1024 * 1024
        if data:
            if not isinstance(data, str) or len(data) > (limit * 4 // 3 + 256): raise cls._image_error('invalid_request', '이미지는 최대 32MB입니다.')
            if not re.match(r'^data:image/(?:jpeg|png|webp);base64,', data, re.I): raise cls._image_error('invalid_request', 'JPEG/PNG/WebP 원본 DataURL만 허용합니다.')
            try:
                raw = base64.b64decode(data.split(',', 1)[1], validate=True)
            except ValueError as exc:
                raise cls._image_error('invalid_request', '이미지 Base64 형식이 올바르지 않습니다.') from exc
        else:
            if not isinstance(url, str) or not url.startswith(('http://', 'https://')): raise cls._image_error('invalid_request', '원격 HTTP(S) 이미지 URL만 허용합니다.')
            url = unwrap_image_url(url)
            import requests
            from http.cookiejar import CookieJar
            module = P.get_module({'JAV_CEN':'jav_censored','JAV_UNCEN':'jav_uncensored','WESTERN':'western'}[category])
            config = (getattr(module, 'site_map', {}) or {}).get(item.site) or {}
            site = config.get('instance') if isinstance(config, dict) else config
            site = site if site is not None and hasattr(site, 'get_response') else SiteAvBase
            # Borrow the site's implementation/configuration, never its cache or live session.
            # A request-local subclass keeps set_config and cookie updates off the crawler class.
            with requests.Session() as transport:
                transport.trust_env = False
                cookies = getattr(getattr(site,'session',None),'cookies',None)
                if isinstance(cookies,CookieJar):
                    for cookie in cookies:
                        if cookie.domain: transport.cookies.set_cookie(copy.copy(cookie))
                provider = type('_ManualImageSource',(site,),dict(
                    session=None,config=copy.deepcopy(getattr(site,'config',None) or {}),
                    default_headers=dict(getattr(site,'default_headers',None) or {}),
                    get_session=classmethod(lambda source_cls: transport)))
                provider.set_config(P.ModelSetting)
                # Existing configured local-image boundary, only for this item's registered image URL.
                registered = any(m.url == url and m.media_type in ('poster','landscape','fanart') for m in item.media_files)
                local = provider._local_image_path(url) if registered else None
                if local is not None:
                    with open(local, 'rb') as fp: raw = fp.read(limit + 1)
                else:
                    if provider.session is None: provider.session = transport
                    with public_scope():
                        try:
                            response = provider.get_response(url, timeout=(10, 30), verify=False, stream=True)
                        except requests.exceptions.RequestException as exc:
                            detail, _ = cls.api_exception(exc, site=getattr(provider, 'site_name', None))
                            raise cls._image_error(detail['error_code'], detail['msg'], detail['error_origin'], detail.get('upstream')) from exc
                        if response is None: raise cls._image_error('download_failed', '선택한 URL의 이미지를 받지 못했습니다.', 'unknown')
                        try:
                            if response.status_code != 200:
                                upstream = {'site': getattr(provider, 'site_name', item.site), 'http_status': response.status_code, 'reason': 'http_error'}
                                raise cls._image_error('upstream_http_error', '원격 이미지 요청을 완료하지 못했습니다.', 'external', upstream)
                            chunks = bytearray()
                            for chunk in response.iter_content(65536):
                                if len(chunks) + len(chunk) > limit: raise ValueError('이미지는 최대 32MB입니다.')
                                chunks.extend(chunk)
                            raw = bytes(chunks)
                        finally: response.close()
        if not raw or len(raw) > limit: raise cls._image_error('invalid_request' if data else 'download_failed', '이미지가 비어 있거나 32MB를 초과합니다.', 'ff' if data else 'unknown')
        try:
            image = Image.open(BytesIO(raw))
        except (OSError, ValueError) as exc:
            raise cls._image_error('invalid_request' if data else 'download_failed', '원본을 이미지로 읽지 못했습니다.', 'ff' if data else 'unknown') from exc
        with image:
            if image.format not in ('JPEG','PNG','WEBP'): raise cls._image_error('invalid_request', 'JPEG/PNG/WebP 이미지 형식만 허용합니다.')
            if max(image.size) > 16000 or image.width * image.height > 40000000: raise cls._image_error('invalid_request', '이미지 최대 변은 16000px, 최대 크기는 4000만 픽셀입니다.')
            image.load()
            oriented = ImageOps.exif_transpose(image)
            if oriented.mode in ('RGBA','LA') or 'transparency' in oriented.info:
                rgba = oriented.convert('RGBA'); result = Image.new('RGB', rgba.size, 'white'); result.paste(rgba, mask=rgba.getchannel('A')); rgba.close()
            else: result = oriented.convert('RGB')
            if oriented is not image: oriented.close()
        return result

    @classmethod
    def _manual_image_references(cls, plan, exclude_owner=True):
        from .mod_meta_db import MetaItem, MetaMedia, MetaPerson
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy import or_
        old = plan.get('old') or {}
        if not old: return False
        # Automatic system files are identified by the initial storage observation,
        # not by DB reference ownership (including the new registration at this URL).
        if exclude_owner and old.get('automatic_image'):return False
        seen = set()
        owner = cls.delete_reference_registry(plan['category']).get_bind()
        for cat in ('JAV_CEN','JAV_UNCEN','WESTERN','PERSON'):
            engine = cls.delete_reference_registry(cat).get_bind()
            if engine in seen: continue
            seen.add(engine)
            with sessionmaker(bind=engine)() as session:
                predicates = [MetaMedia.url == old['url']]
                if old.get('fid'): predicates.append(MetaMedia.gdrive_fileid == old['fid'])
                elif old.get('relative_path'): predicates.append(MetaMedia.relative_path == old['relative_path'])
                query = session.query(MetaMedia.id).filter(or_(*predicates))
                if exclude_owner and engine is owner and not old.get('person_id'):
                    if old.get('owner_code'):
                        query=query.join(MetaItem,MetaMedia.item_id==MetaItem.id).filter(~(
                            (MetaItem.category==plan['category']) & (MetaItem.code==old['owner_code'])))
                    else:
                        excluded = [mid for mid in (old.get('equivalent_ids') or [old.get('id')]) if mid is not None]
                        if excluded:query=query.filter(MetaMedia.id.notin_(excluded))
                if query.first(): return True
                person_predicates=[MetaPerson.thumb_url==old['url'],MetaPerson.site_img_url==old['url']]
                if old.get('person_id') and exclude_owner:
                    person_predicates[0]=(MetaPerson.thumb_url==old['url']) & (MetaPerson.id!=old['person_id'])
                if old.get('fid'):
                    for key in ('gdrive_fileid','custom_gdrive_fileid','official_cached_gdrive_fileid'):
                        predicate=getattr(MetaPerson,key)==old['fid']
                        if old.get('person_id') and exclude_owner and key=='custom_gdrive_fileid':predicate=predicate & (MetaPerson.id!=old['person_id'])
                        person_predicates.append(predicate)
                if old.get('relative_path') and not old.get('fid'):
                    for key in ('local_path','custom_local_path','official_cached_local_path','official_remote_path','custom_remote_path'):
                        predicate=getattr(MetaPerson,key)==old['relative_path']
                        if old.get('person_id') and exclude_owner and key=='custom_remote_path':predicate=predicate & (MetaPerson.id!=old['person_id'])
                        person_predicates.append(predicate)
                if session.query(MetaPerson.id).filter(or_(*person_predicates)).first(): return True
        return False

    @staticmethod
    def _manual_file_stat(path):
        if os.path.islink(path): raise ValueError('사용자 이미지 대상의 심볼릭 링크는 변경할 수 없습니다.')
        if not os.path.exists(path): return None
        value = os.stat(path)
        if not os.path.isfile(path): raise ValueError('이미지 대상이 일반 파일이 아닙니다.')
        return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]

    @classmethod
    def _manual_drive_rows(cls, plan, name):
        from .util_preview import MetaPreviewUtil
        result = MetaPreviewUtil.run_rclone_lookup([plan['rclone'], '--config', plan['config'], 'lsjson',
            plan['remote_dir'], '--files-only', '--max-depth', '1', '--ignore-case', '--include', name],
            category=plan['category'], purpose='backup_probe' if '.old_' in name else 'image')
        if MetaPreviewUtil.remote_missing(result, directory=True): return []
        if result.returncode or not result.stdout.strip(): raise ValueError('사용자 이미지 폴더 조회에 실패했습니다.')
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows): raise ValueError('이미지 폴더 응답 형식 오류입니다.')
        for row in rows:
            if (not isinstance(row.get('Name'),str) or not row['Name'] or '/' in row['Name'] or '\\' in row['Name']
                    or row.get('IsDir') is not False or not re.fullmatch(r'[A-Za-z0-9_-]+', str(row.get('ID') or row.get('id') or ''))):
                raise ValueError('이미지 폴더의 파일명/ID를 확인하지 못했습니다.')
        return [r for r in rows if str(r.get('Name') or '').lower() == name.lower()]

    @classmethod
    def _manual_drive(cls, entry, method, file_id, name=None):
        """Legacy receipt adapter only; ID verification/mutations use the common image engine."""
        from .util_preview import MediaReplacement
        if method=='get':return MediaReplacement.image_identity(entry,file_id,parent=entry.get('user_parent_id'))
        if method not in ('patch','delete') or not entry.get('user_parent_id'):raise ValueError('변경 전 검증된 부모 ID가 필요합니다.')
        identity=MediaReplacement.image_identity(entry,file_id,parent=entry['user_parent_id'])
        return MediaReplacement.image_mutate(entry,file_id,identity['name'],entry['user_parent_id'],name if method=='patch' else None)

    @classmethod
    def user_image_commit_state(cls, entry):
        from .mod_meta_db import MetaItem, MetaMedia
        from sqlalchemy.orm import sessionmaker
        plan = entry['user_plan']; engine = cls.delete_reference_registry(plan['category']).get_bind()
        with sessionmaker(bind=engine)() as session:
            rows = session.query(MetaMedia).join(MetaItem, MetaMedia.item_id == MetaItem.id).filter(
                MetaItem.code==plan['code'], MetaItem.category==plan['category'], MetaMedia.media_type==entry['media_type']).all()
            for row in rows:
                if (row.is_user and row.source=='user' and row.checksum==plan['checksum'] and
                        row.updated_time and row.updated_time.isoformat()==plan['updated_time'] and
                        (row.gdrive_fileid or '')==(entry.get('new_id') or '') and row.relative_path in (plan['canonical_rel'],plan['staged_rel'])):
                    return True
            rows = [r for r in rows if cls.media_in_storage(r, plan['mode'])]
            old = plan.get('old')
            if old and any(row.id==old['id'] and (row.gdrive_fileid or '')==old.get('registered_fid',old['fid']) and (row.relative_path or '')==old.get('registered_path',old['relative_path']) and
                           (row.updated_time.isoformat() if row.updated_time else '')==old['updated_time'] for row in rows): return False
            if not old and not rows: return False
        return None

    @classmethod
    def finish_user_image_replacement(cls, entry, commit):
        from .util_preview import MediaReplacement, MetaPreviewUtil
        from .util_runtime import StorageConflict
        from support_site import SiteAvBase
        from .mod_meta_db import MetaItem, MetaMedia
        from sqlalchemy.orm import sessionmaker
        plan = entry['user_plan']; old = plan.get('old') or {}; mode=plan['mode']
        if entry['status'] in ('committed','rolled_back'): return
        if entry['status']=='committing': commit=True
        if mode=='gdrive' and not commit:
            if cls.user_image_commit_state(entry) is not False:raise StorageConflict('구 임시 이미지의 DB 미반영 근거가 불분명합니다.')
            if entry.get('new_id'):
                candidate=dict(category=plan['category'],old=dict(url=cls.format_gdrive_thumbnail_url(entry['new_id']),fid=entry['new_id'],relative_path=plan['staged_rel']))
                if cls._manual_image_references(candidate,exclude_owner=False):raise StorageConflict('구 임시 이미지 ID가 DB에서 참조되어 보존합니다.')
                if not entry.get('user_parent_id'):
                    value=cls._manual_drive(entry,'get',entry['new_id'])
                    if P.ModelSetting.get('jav_censored_image_gdrive_remote_path')!=plan['remote_root']:raise StorageConflict('구 이미지 루트 설정이 변경되었습니다.')
                    parent=MetaPreviewUtil.resolve_image_parent(plan['remote_dir'],plan['rclone'],plan['config'],plan['category'],refresh=True)
                    if value['name']!=plan['staged_name'] or value['parents']!=[parent]:raise StorageConflict('구 임시 이미지 이름/부모 근거 불일치')
                    entry.update(status=entry['status'], **{'user_parent_id': parent})
        if commit:
            if cls.user_image_commit_state(entry) is not True: raise StorageConflict('사용자 이미지 DB 반영 근거를 확인할 수 없습니다.')
            entry['status'] = 'committing'
            current_mode=P.ModelSetting.get(plan['module']+'_image_mode') or 'image_server'
            if current_mode!=plan.get('configured_mode',mode) or (mode=='gdrive' and P.ModelSetting.get('jav_censored_image_gdrive_remote_path')!=plan['remote_root']): raise StorageConflict('이미지 저장 방식이 변경되어 게시를 보류합니다.')
            if mode=='gdrive' and (MetaPreviewUtil.get_setting('rclone_conf',plan['category'],default='/data/db/rclone.conf')!=plan['config']
                    or MetaPreviewUtil.get_setting('rclone_path',plan['category'],default='rclone')!=plan['rclone']):
                raise StorageConflict('이미지 리모트 설정이 변경되어 게시를 보류합니다.')
            if mode=='image_server':
                folder,prefix=cls.get_server_folder_and_prefix(plan['domain'],plan['category'],plan['stem'],studio=plan['studio'],year=plan['year'])
                if folder != plan['folder'] or prefix != plan['prefix']: raise StorageConflict('로컬 저장 경로 설정이 변경되어 게시를 보류합니다.')
            if mode=='gdrive':
                image=cls._manual_drive(entry,'get',entry['new_id'])
                if not entry.get('user_published'):
                    rows=cls._manual_drive_rows(plan,plan['filename'])
                    if old.get('fid') and plan['old_on_target']:
                        if cls._manual_image_references(plan): raise StorageConflict('이전 사용자 이미지가 공유되어 게시를 보류합니다.')
                        original=cls._manual_drive(entry,'get',old['fid']); backup=old['name']+'.old_'+entry['identifier']
                        if original['name']==old['name']:
                            collisions=cls._manual_drive_rows(plan,backup)
                            if collisions: raise StorageConflict('사용자 이미지 백업 이름 충돌입니다.')
                            entry.update(status='committing', **{'user_backup_started': True})
                            cls._manual_drive(entry,'patch',old['fid'],backup)
                            entry.update(status='committing', **{'user_backup_done': True})
                        elif original['name']!=backup or not entry.get('user_backup_started'): raise StorageConflict('이전 사용자 이미지 이름이 변경되었습니다.')
                        rows=cls._manual_drive_rows(plan,plan['filename'])
                    if any((r.get('ID') or r.get('id'))!=entry['new_id'] for r in rows): raise StorageConflict('사용자 이미지 대상 이름에 다른 파일이 있습니다.')
                    if image['name']==plan['staged_name']:
                        entry.update(status='committing', **{'user_publish_started': True})
                        cls._manual_drive(entry,'patch',entry['new_id'],plan['filename'])
                    elif image['name']!=plan['filename'] or not entry.get('user_publish_started'): raise StorageConflict('신규 이미지 게시 이름이 일치하지 않습니다.')
                    entry.update(status='committing', **{'user_published': True})
                if not entry.get('user_path_committed'):
                    # Register the real staged name first; finalize its canonical name after PATCH.
                    engine=cls.delete_reference_registry(plan['category']).get_bind()
                    with sessionmaker(bind=engine)() as session:
                        count=session.query(MetaMedia).filter(MetaMedia.item_id==plan['item_id'],MetaMedia.media_type==entry['media_type'],
                            MetaMedia.gdrive_fileid==entry['new_id'],MetaMedia.checksum==plan['checksum'],MetaMedia.updated_time==datetime.fromisoformat(plan['updated_time'])).update(
                            {'relative_path':plan['canonical_rel'],'updated_time':datetime.fromisoformat(plan['updated_time'])},synchronize_session=False)
                        if count!=1: raise StorageConflict('사용자 이미지 경로 확정 중 DB 충돌입니다.')
                        session.commit()
                    entry.update(status='committing', **{'user_path_committed': True})
                if old.get('fid') and not entry.get('user_old_deleted'):
                    if cls._manual_image_references(plan):
                        entry.update(status='committing', **{'user_old_retained': True})
                    else:
                        previous=cls._manual_drive(entry,'get',old['fid']);expected=old['name']+'.old_'+entry['identifier'] if plan['old_on_target'] else old['name']
                        if previous['name']!=expected: raise StorageConflict('이전 이미지 이름/ID가 변경되어 정리를 보류합니다.')
                        entry.update(status='committing', **{'user_old_delete_started': True})
                        cls._manual_drive(entry,'delete',old['fid']);entry.update(status='committing', **{'user_old_deleted': True})
            else:
                target=entry['target']; backup=target+'.old_'+entry['identifier']
                if not entry.get('user_published'):
                    current=cls._manual_file_stat(target)
                    if current and entry.get('user_publish_started') and cls._user_image_checksum(target)==plan['checksum']:
                        entry.update(status='committing', **{'user_published': True})
                    else:
                        if current:
                            if current!=plan['target_stat']: raise StorageConflict('사용자 이미지 대상이 다른 파일로 변경되었습니다.')
                            if cls._manual_file_stat(backup): raise StorageConflict('로컬 이미지 백업 충돌입니다.')
                            if cls._manual_image_references(plan): raise StorageConflict('이전 이미지가 다른 등록에 공유되어 게시를 보류합니다.')
                            entry.update(status='committing', **{'user_backup_started': True})
                            os.replace(target,backup);entry.update(status='committing', **{'user_backup_done': True})
                        elif plan['target_stat'] and cls._manual_file_stat(backup)!=plan['target_stat']: raise StorageConflict('로컬 원본/백업 소유 근거가 일치하지 않습니다.')
                        if cls._user_image_checksum(plan['staged_file'])!=plan['checksum']: raise StorageConflict('이미지 임시 파일 검증 실패입니다.')
                        entry.update(status='committing', **{'user_publish_started': True})
                        SiteAvBase.publish_image_file(plan['staged_file'],target)
                        if cls._user_image_checksum(target)!=plan['checksum']: raise StorageConflict('게시 이미지 검증 실패입니다.')
                        entry.update(status='committing', **{'user_published': True})
                if cls._manual_file_stat(backup):
                    if cls._manual_image_references(plan): raise StorageConflict('공유된 이전 백업의 정리를 보류합니다.')
                    if cls._manual_file_stat(backup)!=plan['target_stat']: raise StorageConflict('이전 로컬 백업 소유 근거가 변경되었습니다.')
                    os.remove(backup)
                if old.get('local_path') and old['local_path']!=target and old.get('stat') and not cls._manual_image_references(plan):
                    if cls._manual_file_stat(old['local_path'])!=old['stat']: raise StorageConflict('이전 시스템 이미지가 변경되어 정리를 보류합니다.')
                    os.remove(old['local_path'])
            entry['status'] = 'committed'
        else:
            if cls.user_image_commit_state(entry) is not False: raise StorageConflict('DB 미반영 근거가 없어 파일 정리를 보류합니다.')
            if mode=='gdrive' and entry.get('user_upload_started') and not entry.get('new_id'): raise StorageConflict('업로드 결과/ID가 불명확하여 확인되지 않은 파일은 변경하지 않습니다.')
            entry['status'] = 'rolling_back'
            # Neither old canonical image nor a system image has been touched before DB commit.
            if mode=='gdrive' and entry.get('new_id'):
                staged=cls._manual_drive(entry,'get',entry['new_id'])
                if staged['name']!=plan['staged_name']: raise StorageConflict('실패한 업로드의 소유 이름을 확인할 수 없습니다.')
                cls._manual_drive(entry,'delete',entry['new_id'])
            entry['status'] = 'rolled_back'
        if os.path.isfile(plan['staged_file']):
            if cls._user_image_checksum(plan['staged_file']) != plan['checksum']:
                raise ValueError('이미지 임시 파일 내용 확인 실패입니다.')
            os.remove(plan['staged_file'])

    @staticmethod
    def _user_image_checksum(path):
        import hashlib
        with open(path,'rb') as fp: return hashlib.sha256(fp.read()).hexdigest()


    @classmethod
    def _save_manual_user_images(cls, code, category, source_type, data=None, url=None, crop=None):
        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia
        from .util_preview import MetaPreviewUtil, MediaReplacement
        from .util_runtime import stage_storage_record, commit_storage_record
        from sqlalchemy.orm import lazyload,selectinload
        import uuid,hashlib,math
        session=None;entry=None;workspace=None;image=None;committed=False;unknown=False
        phase = 'validation'
        try:
            if category not in ('JAV_CEN','JAV_UNCEN','WESTERN') or source_type not in ('p','pl'): raise cls._image_error('invalid_request', 'AV 카테고리와 P/PL 타입을 확인하세요.')
            phase = 'database'
            session,domain,category=ModuleMetaDb.get_session_and_domain(category)
            if session is None: raise cls._image_error('database_unavailable', '메타 DB를 사용할 수 없습니다.')
            if not ModuleMetaDb._ensure_meta_db_writable(category): raise cls._image_error('schema_not_writable', '메타 DB에 쓸 수 없는 상태입니다.')
            item=session.query(MetaItem).options(lazyload('*'),selectinload(MetaItem.media_files)).filter_by(code=code,category=category).first()
            if not item: raise cls._image_error('not_found', '등록된 작품을 찾을 수 없습니다.')
            snapshot=stage_storage_record(session,item,('media_files',))
            phase = 'source'
            image=cls._manual_image_source(item,category,source_type,data,url)
            phase = 'validation'
            if crop is not None:
                if crop.get('save_source_pl'): raise ValueError('PL 원본은 별도의 가로 커버 직접 저장을 이용하세요.')
                vals={k:float(crop.get(k,0)) for k in ('x','y','width','height','rotate')}
                if not all(math.isfinite(v) for v in vals.values()): raise ValueError('유효하지 않은 크롭 좌표입니다.')
                if vals['rotate']%360:
                    angle=math.radians(vals['rotate']%360)
                    bounds=(math.ceil(round(abs(image.width*math.cos(angle))+abs(image.height*math.sin(angle)),8)),
                            math.ceil(round(abs(image.width*math.sin(angle))+abs(image.height*math.cos(angle)),8)))
                    if max(bounds)>16000 or bounds[0]*bounds[1]>40000000: raise ValueError('회전된 이미지가 크기 상한을 초과합니다.')
                    rotated=image.rotate(-vals['rotate']%360,expand=True);image.close();image=rotated
                x,y,w,h=[int(round(vals[k])) for k in ('x','y','width','height')]
                if x<0 or y<0 or w<=0 or h<=0 or x+w>image.width or y+h>image.height: raise ValueError('선택된 원본의 크롭 범위를 벗어났습니다.')
                cropped=image.crop((x,y,x+w,y+h));image.close();image=cropped
            kind='p' if crop is not None else source_type;media_type='poster' if kind=='p' else 'landscape'
            stem=(item.code if category=='WESTERN' else item.ui_code or item.originaltitle or item.code).lower()
            if not re.fullmatch(r'[\w.-]+',stem) or stem in ('.','..'): raise ValueError('이미지 파일명으로 사용할 품번이 올바르지 않습니다.')
            module={'JAV_CEN':'jav_censored','JAV_UNCEN':'jav_uncensored','WESTERN':'western'}[category]
            phase = 'storage'
            configured_mode=P.ModelSetting.get(module+'_image_mode') or 'image_server'
            # Explicit manual uploads have always used local storage outside GDrive mode.
            mode='gdrive' if configured_mode=='gdrive' else 'image_server'
            folder,prefix=cls.get_server_folder_and_prefix(domain,category,stem,studio=item.studio or '',year=item.year or 1900)
            relative=cls.get_image_relative_dir(domain,category,stem,studio=item.studio or '',year=item.year or 1900)
            if mode=='image_server' and (not folder or not prefix): raise ValueError('이미지 서버 저장 경로/URL이 없습니다.')
            candidates=[m for m in item.media_files if m.media_type==media_type and cls.media_in_storage(m,mode)]
            previous=cls.preferred_user_image(candidates,media_type,mode) or next(iter(candidates),None)
            # Site/GDS rows may be replaced in the DB, but never authorize deletion of their source files.
            managed_previous = previous is not None and previous.source in ('user', 'gdrive', 'local', 'image_server') and bool(previous.relative_path)
            old={} if previous is None else dict(id=previous.id,url=previous.url,relative_path=previous.relative_path or '',fid=previous.gdrive_fileid or '',
                name=os.path.basename(previous.relative_path or ''),updated_time=previous.updated_time.isoformat() if previous.updated_time else '',checksum=previous.checksum or '')
            if old:
                old['registered_fid'] = old['fid']
                old['registered_path'] = old['relative_path']
            if old and not managed_previous:
                old['fid'] = ''
                old['relative_path'] = ''
                old['name'] = ''
            tmp=os.path.join(path_data,'tmp');os.makedirs(tmp,exist_ok=True);workspace=tempfile.TemporaryDirectory(prefix='manual_image_',dir=tmp)
            source=os.path.join(workspace.name,'image.jpg');cls.save_normalized_jpeg(image,source);image.close();image=None
            filename=stem+'_'+kind+'_user.jpg';canonical='/'.join(filter(None,(relative,filename)))
            plan=dict(code=code,category=category,item_id=item.id,module=module,mode=mode,configured_mode=configured_mode,filename=filename,
                canonical_rel=canonical,staged_rel=canonical,staged_name=filename,domain=domain,stem=stem,studio=item.studio or '',year=item.year or 1900,
                folder=folder,prefix=prefix,checksum=cls._user_image_checksum(source),updated_time=datetime.now().isoformat(),old=old)
            plan['new_url']=prefix.rstrip('/')+'/'+filename
            spec=dict(storage='gdrive' if mode=='gdrive' else 'local',category=category,code=code,relative_path=canonical,media_type=media_type,sort_order=0 if kind=='p' else 1)
            entry=cls.prepare_image_replacement(spec,manual_plan=plan)
            MediaReplacement.stage(entry)
            fid=''
            if mode=='gdrive':
                fid=cls.upload_image_to_gdrive(source,canonical,category=category,replacements=[entry])
                if not fid:raise ValueError('정식 사용자 이미지 업로드/ID 검증 실패')
            else:
                if os.path.abspath(folder)!=os.path.dirname(entry['target']):raise ValueError('설정 이미지 폴더와 상대 경로가 다릅니다.')
                from support_site import SiteAvBase
                SiteAvBase.publish_image_file(source,entry['target'],replacement=entry)
            stage_rel=canonical
            new_url=cls.format_gdrive_thumbnail_url(fid) if fid else plan['new_url']
            media=previous
            if media is None:media=MetaMedia(media_type=media_type,sort_order=0 if kind=='p' else 1);item.media_files.append(media)
            media.url=new_url;media.is_user=True;media.source='user';media.gdrive_fileid=fid or None;media.relative_path=stage_rel
            media.checksum=plan['checksum'];media.updated_time=datetime.fromisoformat(plan['updated_time'])
            extra=dict(item.extra_info or {});extra['poster_url' if kind=='p' else 'landscape_url']=new_url;item.extra_info=extra
            if kind=='p':item.poster_url=new_url
            item.updated_time=datetime.now()
            session.info['storage_commit_state'] = 'not_attempted'
            try:commit_storage_record(session,item,snapshot,('media_files',));committed=True
            except Exception:
                state=session.info.get('storage_commit_state');committed=state=='committed';unknown=state=='unknown'
                if not committed:raise
            warnings=cls.finish_media_replacements([entry],True)
            payload=cls.user_image_detail(code,category,kind)
            if warnings:return False,dict(msg='DB 저장 완료; 사용자 이미지 게시/정리 확인이 필요합니다. 상세 정보를 새로 조회해 상태를 확인하세요.',error_code='storage_asset_incomplete',error_origin='unknown',committed=True,cleanup_pending=True,uncertain=True,data=payload,new_url=new_url)
            return True,dict(msg='사용자 이미지 저장 완료',committed=True,cleanup_pending=False,data=payload,new_url=new_url)
        except Exception as exc:
            if session is not None:
                try:session.rollback()
                except Exception:unknown=True
            warnings=cls.finish_media_replacements([entry],None if unknown else committed) if entry else []
            logger.exception('[MetaUtil] 명시적 사용자 이미지 저장 실패: %s/%s',category,code)
            detail, _ = cls.api_exception(exc)
            code = getattr(exc, 'error_code', None)
            if code:
                detail.update(error_code=code, error_origin=getattr(exc, 'error_origin', 'ff' if code == 'storage_conflict' else 'unknown'))
                if getattr(exc, 'upstream', None) is not None:
                    detail['upstream'] = exc.upstream
                detail['msg'] = str(exc) if hasattr(exc, 'error_origin') else '이미지 저장 상태가 충돌했습니다. 상세 상태를 확인하세요.'
            elif phase == 'validation' and isinstance(exc, (ValueError, TypeError)):
                detail.update(error_code='invalid_request', error_origin='ff', msg='이미지 타입·파일명·크롭 좌표를 확인하세요.')
            elif phase not in ('database', 'storage') and detail['error_origin'] == 'ff':
                detail['error_origin'] = 'unknown'
            message=detail.pop('msg')
            if unknown or warnings:
                message+=' DB 반영 또는 파일 정리 확인이 필요합니다. 상세 상태를 새로 조회하세요.'
            detail.pop('ret', None)
            return False,dict(detail,msg=message,committed=committed,uncertain=unknown or bool(warnings),cleanup_pending=bool(warnings),data={},new_url='')
        finally:
            if image is not None:image.close()
            if workspace is not None:workspace.cleanup()
            if session is not None:session.remove()

    @classmethod
    def user_image_detail(cls, code, category, target_type=None):
        from .mod_meta_db import ModuleMetaDb,MetaItem
        from sqlalchemy.orm import lazyload,selectinload,sessionmaker
        registry,_,_=ModuleMetaDb.get_session_and_domain(category)
        with sessionmaker(bind=registry.get_bind())() as session:
            item=session.query(MetaItem).options(lazyload('*'),selectinload(MetaItem.media_files)).filter_by(code=code,category=category).one()
            media=[dict(media_type=m.media_type,url=m.url,relative_path=m.relative_path or '',gdrive_fileid=m.gdrive_fileid or '',is_user=bool(m.is_user),source=m.source or '',checksum=m.checksum or '',updated_time=m.updated_time.isoformat() if m.updated_time else '') for m in item.media_files]
            stored=dict(poster_url=item.poster_url or '',landscape_url=(item.extra_info or {}).get('landscape_url') or '')
            mode=P.ModelSetting.get({'JAV_CEN':'jav_censored','JAV_UNCEN':'jav_uncensored','WESTERN':'western'}[category]+'_image_mode') or 'image_server'
            for kind in ('poster','landscape'):
                user=cls.preferred_user_image(media,kind,mode)
                if user: stored[kind+'_url']=user['url']
            return dict(code=code,category=category,target_type=target_type,media=media,stored_images=stored,
                new_url=stored['poster_url' if target_type=='p' else 'landscape_url'] if target_type else '',poster_url=stored['poster_url'],landscape_url=stored['landscape_url'])

    @classmethod
    def preserve_user_images_in_edit(cls, code, category, payload):
        if category not in ('JAV_CEN','JAV_UNCEN','WESTERN'):return payload
        if not isinstance(payload,dict) or payload.get('code') != code or payload.get('category',category) != category:
            raise ValueError('편집 대상의 코드/카테고리가 일치하지 않습니다.')
        latest=cls.user_image_detail(code,category)
        mode=P.ModelSetting.get({'JAV_CEN':'jav_censored','JAV_UNCEN':'jav_uncensored','WESTERN':'western'}[category]+'_image_mode') or 'image_server'
        for kind in ('poster','landscape'):
            user=cls.preferred_user_image(latest['media'],kind,mode)
            if not user:continue
            payload['thumb']=[t for t in payload.get('thumb') or [] if t.get('aspect')!=kind]+[{'aspect':kind,'value':user['url']}]
            payload.setdefault('extra_info',{})[kind+'_url']=user['url']
            if kind=='poster':payload['poster_url']=user['url']
            for key in ('media','_storage_media'):
                target=payload if key=='media' else payload['extra_info']
                target[key]=[m for m in target.get(key) or [] if m.get('media_type')!=kind or not cls.media_in_storage(m,mode)]+[dict(user)]
        return payload

    @classmethod
    def save_user_cropped_poster(cls, code, crop_data_or_base64, pl_image_base64_data=None, p_image_base64_data=None, category=None):
        try:
            coords=json.loads(crop_data_or_base64) if isinstance(crop_data_or_base64,str) else crop_data_or_base64
            if not isinstance(coords,dict):raise ValueError('크롭 좌표 JSON이 필요합니다.')
            source=coords.get('source_type') or ('p' if p_image_base64_data else 'pl')
            return cls._save_manual_user_images(code,category,source,p_image_base64_data or pl_image_base64_data,coords.get('crop_url') or coords.get('url'),coords)
        except Exception as exc:return False,{'msg':str(exc)}

    @classmethod
    def save_direct_user_image(cls, code, source_path_or_url=None, image_type='pl', category=None, image_base64_data=None):
        return cls._save_manual_user_images(code,category,image_type,image_base64_data,source_path_or_url)


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

        image_entry = None
        person_commit_started = False
        person_committed = False
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
            actor_fd, tmp_actor_file = tempfile.mkstemp(prefix='user_actor_',suffix='.jpg',dir=tmp_dir)
            os.close(actor_fd)

            cls.save_normalized_jpeg(cropped_img, tmp_actor_file)
            cropped_img.close()
            src_img.close()

            final_thumb_url = ""
            user_gid = ""

            if is_gdrive_mode:
                from .util_preview import MediaReplacement
                image_entry=cls.prepare_person_user_image_replacement(p_rec,relative_path)
                MediaReplacement.stage(image_entry)
                gdrive_id=cls.upload_image_to_gdrive(tmp_actor_file,relative_path,category=image_entry['category'],replacements=[image_entry])
                if gdrive_id:
                    user_gid = gdrive_id
                    final_thumb_url = cls.format_gdrive_thumbnail_url(gdrive_id)
                    logger.info(f"[MetaUtil] 인물 사진 구글 드라이브 다이렉트 업로드 성공: {p_rec.person_idx} -> {gdrive_id}")
                else:
                    logger.error(f"[MetaUtil] 인물 사진 구글 드라이브 업로드 실패: {p_rec.person_idx}")
                    cls.finish_media_replacements([image_entry],False)
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

            person_commit_started=True
            sess.commit()
            person_committed=True
            if image_entry and cls.finish_media_replacements([image_entry],True):
                return False,'DB 저장 완료; 인물 이미지 후처리 확인이 필요합니다.',p_rec

            logger.info(f"[MetaUtil] 인물 사진 저장 성공 완료 -> ID: {p_rec.person_idx}, URL: '{final_thumb_url}'")
            return True, final_thumb_url, p_rec

        except Exception as e:
            sess.rollback()
            if image_entry:cls.finish_media_replacements([image_entry],True if person_committed else None if person_commit_started else False)
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
    def delete_asset_identity(cls, asset, canonical=False):
        """Storage identity, independent of record codes and display fallbacks."""
        import hashlib
        from .util_preview import MetaPreviewUtil
        if asset['storage'] == 'local':
            path = os.path.abspath(asset['path'])
            return 'local:' + (os.path.realpath(path) if canonical else path)
        remote = str(asset.get('remote') or '').strip()
        if not re.fullmatch(r'[^/\\\s:]+:.*', remote):
            asset['blocked'] = 'D01_STORAGE: 저장소 스코프를 확인할 수 없습니다.'
        conf = MetaPreviewUtil.get_setting('rclone_conf', asset['category'], '/data/db/rclone.conf')
        scope = hashlib.sha256((str(conf) + '\n' + remote.split(':', 1)[0]).encode()).hexdigest()
        asset['scope'] = scope
        return 'gdrive:' + scope + ':' + (asset.get('file_id') or 'path:' + asset['path'])

    @classmethod
    def delete_reference_registry(cls, category):
        from .mod_meta_db import ModuleMetaDb, DOMAIN_MAP
        # A missing domain is uncertainty, not permission to initialize an empty DB.
        key = 'postgres' if ModuleMetaDb._is_postgres else DOMAIN_MAP[category][0]
        registry = ModuleMetaDb._sessions.get(key)
        if registry is None:
            raise ValueError('D01_LOOKUP: 전체 참조 DB를 확인할 수 없습니다.')
        return registry

    @classmethod
    def delete_owner_key(cls, record, person=False):
        import hashlib
        from .mod_meta_db import ModuleMetaDb
        category = 'PERSON' if person else record.category
        registry = cls.delete_reference_registry(category)
        # Do not remove/close the caller's scoped session or include credentials.
        database = hashlib.sha256(str(registry.get_bind().url).encode()).hexdigest()
        return (database, 'person' if person else 'item', category, record.id,
                str(record.person_idx or '') if person else record.code)

    @classmethod
    def video_delete_assets(cls, extra_info, category, video_type=None):
        from types import SimpleNamespace
        keys = [('preview', 'preview_clip'), ('trailer', 'trailer_video')]
        extra = {}
        for kind, key in keys:
            info = (extra_info or {}).get(key)
            if (video_type and kind != video_type) or not info:
                continue
            info = dict(info)
            storage = info.get('storage_type', 'local')
            if storage not in ('local', 'gdrive'):
                raise ValueError('D01_STORAGE: 영상 저장 방식을 확인할 수 없습니다.')
            # The legacy video helper acts only on storage_type, even when stale
            # fields from the previous mode remain in the same JSON object.
            info.pop('local_path' if storage == 'gdrive' else 'google_fileid', None)
            extra[key] = info
        record = SimpleNamespace(category=category, media_files=[], extra_info=extra, poster_url='')
        return cls.collect_delete_assets(record)

    @classmethod
    def user_image_delete_assets(cls, item):
        """Use precisely the legacy helper's storage mode and filename policy."""
        from types import SimpleNamespace
        prefix = 'western' if item.category == 'WESTERN' else ('jav_uncensored' if item.category == 'JAV_UNCEN' else 'jav_censored')
        mode = P.ModelSetting.get(prefix + '_image_mode') or 'ff_proxy'
        if mode == 'gdrive':
            rows = [m for m in item.media_files if m.is_user and m.gdrive_fileid and m.source != 'gds']
            if any(m.media_type not in ('poster', 'landscape', 'fanart') for m in rows):
                raise ValueError('D01_STORAGE: 사용자 이미지의 저장소 종류를 확인할 수 없습니다.')
            if any(not m.relative_path for m in rows):
                raise ValueError('D01_STORAGE: 사용자 이미지의 저장 경로가 없습니다.')
        elif mode == 'image_server':
            stem = ((item.code or item.ui_code) if item.category == 'WESTERN' else
                    (item.ui_code or item.originaltitle or item.code) or '').lower()
            if not stem or '/' in stem or '\\' in stem or stem in ('.', '..'):
                raise ValueError('D01_PATH: 이미지 파일명을 확인할 수 없습니다.')
            folder, _ = cls.get_server_folder_and_prefix(item.domain, item.category, stem, studio=item.studio, year=item.year)
            root = P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data, 'images')
            # Validate the directory before reading its entries, including parent symlinks.
            cls.check_delete_local(dict(path=folder, local_root=root), directory=True)
            rows = [SimpleNamespace(media_type='poster', relative_path=os.path.relpath(os.path.join(folder, name), root),
                        url='', gdrive_fileid='', source='user', is_user=True)
                    for name in (os.listdir(folder) if os.path.isdir(folder) else [])
                    if name.startswith((stem + '_p_user.', stem + '_pl_user.'))]
        else:
            return []
        return cls.collect_delete_assets(SimpleNamespace(category=item.category, media_files=rows, extra_info={}, poster_url=''))

    @classmethod
    def check_delete_local(cls, asset, directory=False):
        import stat
        root = os.path.abspath(asset['local_root'])
        path = os.path.abspath(asset['path'])
        if (os.path.commonpath([root, path]) != root or path == root or
                '..' in str(asset.get('relative_path') or '').replace('\\', '/').split('/')):
            raise ValueError('D01_PATH: 허용된 저장소 경로가 아닙니다.')
        # Check every component, not only the final filename (including the root).
        cursor = os.path.sep
        observed = []
        for part in path.strip(os.path.sep).split(os.path.sep):
            cursor = os.path.join(cursor, part)
            try:
                info = os.lstat(cursor)
            except FileNotFoundError:
                observed.append((cursor, None))
                break
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('D01_PATH: 심볼릭 링크는 삭제할 수 없습니다.')
            if cursor == path and not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                raise ValueError('D01_PATH: 유효한 미디어 파일이 아닙니다.')
            observed.append((cursor, (info.st_dev, info.st_ino, info.st_mode,
                                     info.st_mtime_ns if cursor == path else 0,
                                     info.st_ctime_ns if cursor == path else 0)))
        return observed

    @classmethod
    def delete_reference_category(cls, assets, selected, category=None):
        """Require one owner category; PERSON storage settings may use AV prefixes."""
        from .mod_meta_db import DOMAIN_MAP
        try:
            if any(len(owner) not in (5, 6) for owner in selected):
                raise ValueError()
            owners = {owner[2] for owner in selected}
            if len(owners) > 1:
                raise ValueError()
            storage_categories = {a.get('category') for a in assets}
            target = category or (next(iter(owners)) if owners else (
                next(iter(storage_categories)) if len(storage_categories) == 1 else None))
            if target is None and not assets and not selected:
                return None
            if target not in DOMAIN_MAP or owners and owners != {target}:
                raise ValueError()
            allowed = {'JAV_CEN', 'WESTERN', 'PERSON'} if target == 'PERSON' else {target}
            if not storage_categories.issubset(allowed):
                raise ValueError()
            return target
        except (TypeError, ValueError, IndexError):
            raise ValueError('D02_CATEGORY: 단일 카테고리의 소유권 증거가 필요합니다.') from None


    @classmethod
    def delete_reference_snapshot(cls, assets, selected, canonical=False, category=None):
        """One candidate-bounded pass over normalized references, never legacy JSON."""
        from sqlalchemy import select, or_
        from sqlalchemy.orm import sessionmaker
        from types import SimpleNamespace
        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia, MetaPerson
        if not assets:
            return {}
        selected = {tuple(o) for o in selected}
        allowed = {'JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'PERSON'}
        requested = {owner[2] for owner in selected}
        if category not in (None, 'AV_ALL'):
            if category not in allowed or requested and requested != {category}:
                raise ValueError('D02_CATEGORY')
            requested = {category}
        if not requested:
            requested = {a.get('owner_category') or a.get('category') for a in assets}
        if not requested or not requested.issubset(allowed):
            raise ValueError('D02_CATEGORY')
        by_key = {a['key']: a for a in assets}
        scopes = {}
        for key, asset in by_key.items():
            owners = [tuple(o) for o in asset.get('reference_evidence', [])]
            cats = {o[2] for o in owners if o in selected or o[:5] in selected}
            if asset.get('owner_category'):
                cats.add(asset['owner_category'])
            if not cats:
                # PERSON uses AV storage settings; its owner category is PERSON.
                cats = {'PERSON'} if requested == {'PERSON'} else {asset.get('category')}
            scopes[key] = cats & requested
            if not scopes[key]:
                raise ValueError('D02_CATEGORY')
        index = {key: {tuple(o) for o in a.get('reference_evidence', []) if o[2] in scopes[key]}
                 for key, a in by_key.items()}
        def remember(owner, references):
            for ref in references:
                for key, target in by_key.items():
                    if owner[2] not in scopes[key]:
                        continue
                    same = ref['key'] == key or (ref['storage'] == target['storage'] == 'gdrive' and (
                        (ref.get('file_id') and ref['file_id'] == target.get('file_id')) or
                        (ref.get('scope') == target.get('scope') and ref.get('relative_path') and ref['path'] == target['path'])))
                    if not same:
                        continue
                    selected_ref = owner in selected or owner[:5] in selected
                    if target.get('canonical_file') and selected_ref:
                        # The selected record may have a stale ID or a legacy GDS
                        # label; the observed canonical file is its current target.
                        continue
                    if ref.get('file_id') and target.get('file_id') and ref['file_id'] != target['file_id']:
                        target['retained'] = '파일 ID와 등록 경로가 불일치하여 보존'
                    slot = 'external' if ref.get('external') else ('custom' if ref['is_user'] else 'official') if owner[1] == 'person' else (
                        ref['media_type'] if ref['media_type'] in ('preview', 'trailer') else 'user_image' if ref['is_user'] else 'official_image')
                    index[key].add(owner + (slot,))
        for cat in sorted(requested):
            candidates = [a for key, a in by_key.items() if cat in scopes[key]]
            if not candidates:
                continue
            ids = {a['file_id'] for a in candidates if a.get('file_id')}
            # Bound both ID and path checks to this command's candidates.
            paths = {a['relative_path'] for a in candidates if a.get('relative_path')}
            paths.update(a['path'] for a in candidates if a['storage'] == 'local')
            root = P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data, 'images')
            paths.update(os.path.relpath(a['path'], root).replace(os.path.sep, '/') for a in candidates if a['storage'] == 'local')
            registry = cls.delete_reference_registry(cat)
            engine = registry.get_bind()
            import hashlib
            database = hashlib.sha256(str(engine.url).encode()).hexdigest()
            with sessionmaker(bind=engine, autoflush=False)() as reader:
                item_cats = [cat] if cat != 'PERSON' else []
                if item_cats:
                    predicates = []
                    if ids: predicates.append(MetaMedia.gdrive_fileid.in_(ids))
                    if paths: predicates.append(MetaMedia.relative_path.in_(paths))
                    if predicates:
                        columns = [MetaItem.id, MetaItem.category, MetaItem.code, MetaMedia.media_type, MetaMedia.url,
                                   MetaMedia.relative_path, MetaMedia.gdrive_fileid, MetaMedia.is_user, MetaMedia.source]
                        query = select(*columns).join(MetaMedia, MetaMedia.item_id == MetaItem.id).where(
                            MetaItem.category.in_(item_cats), or_(*predicates))
                        for row in reader.execute(query).yield_per(250):
                            v = dict(row._mapping)
                            item = SimpleNamespace(id=v.pop('id'), category=v.pop('category'), code=v.pop('code'),
                                media_files=[SimpleNamespace(**v)], extra_info={}, poster_url='')
                            owner = (database, 'item', item.category, item.id, item.code)
                            remember(owner, cls.collect_delete_assets(item, references=True))
                if cat == 'PERSON':
                    predicates = []
                    for name in ('official_cached_gdrive_fileid', 'custom_gdrive_fileid', 'gdrive_fileid'):
                        if ids: predicates.append(getattr(MetaPerson, name).in_(ids))
                    for name in ('official_cached_local_path', 'custom_local_path', 'official_remote_path', 'custom_remote_path'):
                        if paths: predicates.append(getattr(MetaPerson, name).in_(paths))
                    if predicates:
                        names = ['id', 'person_idx', 'domain'] + [c for _, _, c in ModuleMetaDb.PERSON_MEDIA_COLUMNS]
                        query = select(*(getattr(MetaPerson, n) for n in names)).where(or_(*predicates))
                        for row in reader.execute(query).yield_per(250):
                            person = SimpleNamespace(**dict(row._mapping), media_src={})
                            owner = (database, 'person', 'PERSON', person.id, str(person.person_idx or ''))
                            remember(owner, cls.collect_delete_assets(person, person=True, references=True))
        for key, asset in by_key.items():
            if any(ref[-1] == 'external' or (ref not in selected and ref[:5] not in selected) for ref in index[key]):
                asset['retained'] = asset.get('retained') or '선택되지 않은 등록 DB 참조에서 사용하는 공유 파일'
        return {key: sorted(owners) for key, owners in index.items()}


    @classmethod
    def prepare_delete_context(cls, records=(), assets=None, selected=None, images=False, videos=False, inspect_storage=True, slots=None, category=None, request_wide=False):
        from .util_preview import MetaPreviewUtil
        selected = {cls.delete_owner_key(record) for record in records} if selected is None else {tuple(o) for o in selected}
        if assets is None:
            assets = []
            directory_cache = {}
            for record in records:
                remove_video = videos and MetaPreviewUtil.is_delete_video_on_meta_delete_enabled(record.category)
                kinds = (('poster','landscape','fanart') if images else ()) + (('preview','trailer') if remove_video else ())
                assets.extend(asset for asset in cls.collect_delete_assets(record,storage_kinds=kinds,directory_cache=directory_cache) if
                    (images and asset['is_user'] and asset['media_type'] in ('poster', 'landscape', 'fanart')) or
                    (remove_video and asset['media_type'] in ('preview', 'trailer')))
        assets = list(assets)
        category = 'AV_ALL' if request_wide else cls.delete_reference_category(assets, selected, category)
        unique = {}
        for incoming in assets:
            asset = dict(incoming)
            asset['key'] = cls.delete_asset_identity(asset)
            old = unique.get(asset['key'])
            if old:
                if old['path'] != asset['path'] and old.get('relative_path') and asset.get('relative_path'):
                    old['retained'] = '동일 파일의 등록 경로가 불명확하여 보존'
                old.setdefault('reference_evidence', []).extend(asset.get('reference_evidence', []))
                continue
            unique[asset['key']] = asset
        context = dict(assets=unique, selected=selected, deleted=set(), ready=True, category=category, proof={})
        if not unique:
            return context
        try:
            context['proof'] = cls.delete_reference_snapshot(list(unique.values()), selected, category=category)
            if not selected:
                owners = {owner[:5] for refs in context['proof'].values() for owner in refs if owner[-1] != 'external'}
                if len(owners) == 1:
                    selected.update({owner + (slot,) for owner in owners for slot in slots} if slots else owners)
                    for asset in unique.values():
                        outside = [r for r in context['proof'][asset['key']] if r[-1] == 'external' or (r not in selected and r[:5] not in selected)]
                        if not outside and not asset.get('external'):
                            asset['retained'] = ''
            for asset in unique.values():
                asset['reference_checked'] = True
                if asset.get('blocked') or not context['proof'].get(asset['key']):
                    asset['retained'] = asset.get('retained') or '관리 귀속 또는 등록 참조를 확인할 수 없어 보존'
                if inspect_storage and not asset.get('retained') and asset['storage'] == 'local':
                    asset['observation'] = cls.check_delete_local(asset)
        except Exception:
            for asset in unique.values():
                asset['retained'] = '등록 참조 조회가 완료되지 않아 파일을 보존'
                context['proof'][asset['key']] = asset.get('reference_evidence', [])
        return context

    @classmethod
    def authorize_asset_delete(cls, asset, context=None):
        if context is None:
            context = cls.prepare_delete_context(assets=[asset])
        key = cls.delete_asset_identity(dict(asset))
        key = context.get('aliases', {}).get(key, key)
        if key not in context['assets']:
            raise ValueError('D01_CHANGED: 사전 확인에 없는 자산입니다.')
        checked = context['assets'][key]
        if checked['retained'] or key in context['deleted']:
            return False
        try:
            if checked['storage'] == 'local':
                if cls.check_delete_local(checked) != checked['observation']:
                    raise ValueError('D01_CHANGED')
        except Exception:
            raise ValueError('D01_CHANGED: 최종 파일 상태를 확인하지 못해 삭제를 보류했습니다.') from None
        context.setdefault('authorized', set()).add(key)
        return True

    @classmethod
    def collect_delete_assets(cls, record, person=False, references=False, storage_kinds=None, directory_cache=None):
        """Inventory managed media; retain source movies and external shared GDS."""
        from .mod_meta_db import ModuleMetaDb
        from .util_preview import MetaPreviewUtil
        from urllib.parse import urlparse, parse_qs, unquote
        category = ('WESTERN' if str(record.domain).upper() == 'WESTERN' else 'JAV_CEN') if person else record.category
        root = P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data, 'images')
        image_remote = P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or ''
        assets = {}

        def add_url(kind, url, user=False):
            if not isinstance(url, str) or not url:
                return
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            if parsed.hostname == 'drive.google.com':
                fid = (query.get('id') or [''])[0]
                if fid: add(kind, fid=fid, user=user)
            elif kind in ('preview', 'trailer') and query.get('mode') == ['preview_gdrive']:
                fid = (query.get('fileid') or [''])[0]
                if fid: add(kind, fid=fid, user=user)
            elif kind in ('preview', 'trailer') and query.get('mode') == ['preview_local']:
                add(kind, local=(query.get('path') or [''])[0], user=user)
            else:
                base_url = (P.ModelSetting.get('jav_censored_image_server_url') or '').rstrip('/')
                if base_url and url.startswith(base_url + '/'):
                    add(kind, unquote(url[len(base_url) + 1:].split('?', 1)[0]), user=user)

        def add(kind, relative='', fid='', local='', user=False, actor=False, remote_asset=False, external=False):
            video = kind in ('trailer', 'preview')
            storage = 'gdrive' if fid or remote_asset else 'local'
            base = MetaPreviewUtil.get_setting('preview_local_path', category, '/data/previews') if video else root
            remote = MetaPreviewUtil.get_setting('preview_gdrive_remote_path', category, '') if video else image_remote
            relative = str(relative or '').replace('\\', '/')
            reason = ''
            if '..' in relative.split('/') or (storage == 'gdrive' and relative.startswith('/')):
                reason = 'D01_PATH: 유효하지 않은 상대 경로입니다.'
            if actor and storage == 'local':
                prefix = 'western' if category == 'WESTERN' else 'jav_censored'
                sub = (P.ModelSetting.get(prefix + '_image_server_actor_path') or
                       ('/western/actors' if category == 'WESTERN' else '/jav/actors')).strip('/\\')
                if '..' in sub.replace('\\', '/').split('/'):
                    reason = 'D01_PATH: 배우 이미지 폴더가 저장소 범위를 벗어납니다.'
                base = os.path.join(base, sub)
            if storage == 'gdrive':
                if fid and not re.fullmatch(r'[A-Za-z0-9_-]+', str(fid)):
                    reason = '유효하지 않은 GDrive 파일 ID'
                try:
                    target = MetaPreviewUtil.build_rclone_target_remote_path(remote, relative) if remote else ''
                except ValueError:
                    target, reason = relative, '유효하지 않은 GDrive 상대 경로'
                if not remote:
                    reason = 'GDrive 리모트 설정이 없습니다.'
                if not fid and not relative:
                    return
                identity = 'gdrive:' + str(fid) if fid else 'gdrive-path:' + target
            else:
                if not local and not relative:
                    return
                base = os.path.abspath(base)
                candidate = str(local or relative)
                if '..' in candidate.replace('\\', '/').split('/'):
                    reason = 'D01_PATH: 유효하지 않은 상대 경로'
                target = os.path.abspath(candidate if os.path.isabs(candidate) else os.path.join(base, candidate))
                if os.path.commonpath([base, target]) != base or target == base:
                    reason = '설정된 미디어 저장소 밖의 경로'
                identity = 'local:' + target
            entry = dict(key=identity, media_type=kind, storage=storage, path=target,
                         relative_path=relative, file_id=str(fid or ''), is_user=bool(user),
                         category=category, remote=remote, local_root=os.path.abspath(base), blocked=reason, retained='')
            if storage == 'gdrive' and (not fid or not relative):
                entry['retained'] = '파일 ID 또는 상대 경로가 불명확하여 보존'
            if external:
                entry['external'] = True
                entry['retained'] = '외부 GDS 또는 관리 출처가 불명확한 등록 파일 보존'
            identity = cls.delete_asset_identity(entry)
            entry['key'] = identity
            if entry['blocked']:
                entry['retained'] = entry['retained'] or '관리 경로를 확인할 수 없어 파일 보존'
            if references:
                # Preserve distinct same-owner slots (custom vs official, preview vs trailer).
                identity += ':reference:' + kind + ':' + str(bool(user)) + ':' + str(external)
            if storage == 'gdrive' and relative:
                for old_key, old in list(assets.items()):
                    if (old['storage'] == storage and old['path'] == target and not (old['file_id'] and fid)
                            and (not references or (old['media_type'] == kind and old['is_user'] == bool(user)
                                                   and bool(old.get('external')) == external))):
                        entry['is_user'] = entry['is_user'] or old['is_user']
                        if old['file_id']:
                            old['is_user'] = entry['is_user']
                            return
                        assets.pop(old_key)
            if identity not in assets:
                assets[identity] = entry
            else:
                previous = assets[identity]
                if previous['relative_path'] and relative and previous['path'] != target:
                    previous['blocked'] = 'D01_STORAGE: 동일 파일의 저장 경로가 불명확합니다.'
                if user:
                    previous['is_user'] = True

        if person:
            media = ModuleMetaDb.person_media(record)
            official, custom = media['official'], media['custom']
            add('person', official.get('cached_local_img_path'), actor=True)
            if official.get('official_cached_google_fileid') or official.get('remote_img_path'):
                add('person', official.get('remote_img_path'), official.get('official_cached_google_fileid'), remote_asset=True)
            if references and official.get('google_fileid'):
                add('person', fid=official['google_fileid'], external=True)
            add('person', custom.get('local_img_path'), user=True, actor=True)
            if custom.get('custom_user_google_fileid') or custom.get('remote_img_path'):
                add('person', custom.get('remote_img_path'), custom.get('custom_user_google_fileid'), user=True, remote_asset=True)
        else:
            for media in record.media_files:
                kind = media.media_type
                if kind not in ('poster', 'landscape', 'fanart', 'trailer', 'preview'):
                    continue
                relative, fid = media.relative_path, media.gdrive_fileid
                parsed = urlparse(media.url or '')
                if not fid and parsed.hostname == 'drive.google.com':
                    fid = (parse_qs(parsed.query).get('id') or [''])[0]
                observed = next((a for a in (getattr(record, 'extra_info', None) or {}).get('_storage_media', [])
                    if isinstance(a, dict) and a.get('source') == 'gdrive' and a.get('media_type') == kind
                    and a.get('gdrive_fileid') == fid and a.get('relative_path') == relative and fid and relative), None)
                external = media.source == 'gds' and observed is None
                if fid or media.source in ('gdrive', 'gds'):
                    add(kind, relative, fid, user=media.is_user, remote_asset=True, external=external or (media.source not in ('gdrive', 'user') and observed is None))
                elif kind in ('trailer', 'preview'):
                    query = parse_qs(urlparse(media.url or '').query)
                    local = (query.get('path') or [''])[0] if query.get('mode') == ['preview_local'] else ''
                    add(kind, relative, local=local, user=media.is_user or kind == 'preview')
                elif relative:
                    add(kind, relative, user=media.is_user)
        if not person and not references and storage_kinds:
            # The command selects kinds; only a complete configured-folder snapshot
            # can add unregistered physical files to the existing D02 inventory.
            kinds = set(storage_kinds)
            stems = cls._reconcile_item_stems(record)
            if not stems:
                raise ValueError('삭제할 작품의 정식 파일명을 확인할 수 없습니다.')
            prefix = 'western' if category=='WESTERN' else 'jav_uncensored' if category=='JAV_UNCEN' else 'jav_censored'
            mode = P.ModelSetting.get(prefix+'_image_mode') or P.ModelSetting.get('jav_censored_image_mode') or 'ff_proxy'
            groups = {}
            if kinds & {'poster','landscape','fanart'} and mode in ('gdrive','image_server'):
                folder = cls.get_image_relative_dir(record.domain,category,stems[0],studio=record.studio or '',year=record.year or 1900)
                groups[(mode,image_remote if mode=='gdrive' else root,folder)] = dict(image=True,videos={})
            video_mode = MetaPreviewUtil.get_setting('preview_storage_type',category,'local')
            if video_mode in ('gdrive','local'):
                video_root = (MetaPreviewUtil.get_setting('preview_gdrive_remote_path',category,'') or
                    MetaPreviewUtil.get_setting('preview_video_folder_id',category,'')) if video_mode=='gdrive' else MetaPreviewUtil.get_setting('preview_local_path',category,'/data/previews')
                for kind in sorted(kinds & {'trailer','preview'}):
                    folder,name = MetaPreviewUtil.get_video_relative_path(record,kind,category)
                    groups.setdefault(('gdrive' if video_mode=='gdrive' else 'image_server',video_root,folder),dict(image=False,videos={}))['videos'][kind]=name
            cache = directory_cache if directory_cache is not None else {}
            owner = cls.delete_owner_key(record)
            for (mode,base,folder),group in groups.items():
                key = (mode,base,folder,MetaPreviewUtil.get_setting('rclone_conf',category,'/data/db/rclone.conf'))
                try:
                    if key not in cache:
                        try:
                            snapshot = cls._reconcile_storage_folder(folder,category,group['image'],mode,base,directory_cache=cache)
                            count = sum(len(value.get('entries',())) for value in cache.values() if isinstance(value,dict))
                            for old_key in list(cache):
                                if count+len(snapshot['entries'])<=10000:break
                                if isinstance(cache[old_key],dict) and cache[old_key].get('complete') is True:
                                    count-=len(cache.pop(old_key)['entries'])
                            cache[key] = snapshot
                        except Exception as exc:
                            cache[key] = exc
                    if isinstance(cache[key],Exception):raise cache[key]
                    observed = cls._reconcile_directory_index(cache[key])
                except Exception as exc:
                    # Preserve known files for this folder; other folders remain usable.
                    for asset in assets.values():
                        if asset['storage']==('gdrive' if mode=='gdrive' else 'local') and (
                                os.path.dirname(asset['relative_path'])==folder):
                            asset['retained']='정식 저장소 목록 확인 실패로 보존'
                    # Surface an unobserved slot in the existing preview/receipt
                    # instead of silently presenting a failed listing as no files.
                    pending = dict(group['videos'])
                    if group['image']:
                        pending.update({kind:stems[0]+'_'+suffix+'.jpg' for kind,suffix in
                            (('poster','p'),('landscape','pl'),('fanart','art_1')) if kind in kinds})
                    for kind,name in pending.items():
                        relative='/'.join(filter(None,(folder,name)))
                        add(kind,relative,remote_asset=mode=='gdrive')
                        for asset in assets.values():
                            if asset['relative_path']==relative and asset['storage']==('gdrive' if mode=='gdrive' else 'local'):
                                asset['retained']='폴더 조회 실패: 이 슬롯의 실파일 목록·존재 미확인'
                                asset['reference_evidence']=[owner+(kind if kind in ('trailer','preview') else 'official_image',)]
                    logger.warning('[MetaDB Delete] [%s] %s: 정식 폴더 조회 보류 (%s): %s',category,record.code,folder,exc)
                    continue
                candidates = {}
                if group['image']:
                    for stem in stems:
                        for entry,slot in observed['by_stem'].get(stem,()):
                            if slot[0] in kinds & {'poster','landscape','fanart'}:
                                candidates.setdefault(slot,[]).append(entry)
                for kind,name in group['videos'].items():
                    candidates[(kind,0,False)] = observed['by_name'].get(name.casefold(),[])
                for (kind,order,user),entries in candidates.items():
                    for entry in entries:
                        ambiguous = (len(entries)!=1 or len(observed['by_name'][entry['name'].casefold()])!=1 or
                            bool(entry['file_id'] and len(observed['by_id'][entry['file_id']])!=1) or not entry['valid'])
                        target = (MetaPreviewUtil.build_rclone_target_remote_path(base,entry['path']) if mode=='gdrive' else
                            os.path.abspath(os.path.join(base,entry['path'])))
                        # A stale DB ID at this path does not identify the current file.
                        for old_key,old in list(assets.items()):
                            if old['path']==target and old['storage']==('gdrive' if mode=='gdrive' else 'local'):
                                assets.pop(old_key)
                        add(kind,entry['path'],entry['file_id'] if mode=='gdrive' else '',user=user or kind=='preview',remote_asset=mode=='gdrive')
                        asset = next(a for a in assets.values() if a['path']==target and a['file_id']==(entry['file_id'] if mode=='gdrive' else ''))
                        asset['canonical_file']=True
                        slot = kind if kind in ('trailer','preview') else 'user_image' if user else 'official_image'
                        asset['reference_evidence']=[owner+(slot,)]
                        if ambiguous:asset['retained']='정식 파일의 이름·ID 또는 형식이 모호하여 보존'
                        elif mode!='gdrive':asset['observation']=cls.check_delete_local(asset)
            for asset in assets.values():
                if not asset.get('canonical_file'):
                    asset['retained']=asset['retained'] or '현재 정식 관리 파일 목록에서 확인되지 않아 보존'
        return list(assets.values())

    @classmethod
    def _delete_storage_identity(cls, asset):
        """Recheck current storage configuration without network or file writes."""
        from .util_preview import MetaPreviewUtil
        category = asset['category']
        video = asset['media_type'] in ('trailer', 'preview')
        if asset['storage'] == 'local':
            root = (MetaPreviewUtil.get_setting('preview_local_path', category, '/data/previews') if video else
                    P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data, 'images'))
            if asset['media_type'] == 'person':
                prefix = 'western' if category == 'WESTERN' else 'jav_censored'
                sub = (P.ModelSetting.get(prefix + '_image_server_actor_path') or
                       ('/western/actors' if category == 'WESTERN' else '/jav/actors')).strip('/\\')
                root = os.path.join(root, sub)
            if os.path.abspath(root) != asset['local_root']:
                raise ValueError('D02_STORAGE_CHANGED')
        elif asset['storage'] == 'gdrive':
            remote = (MetaPreviewUtil.get_setting('preview_gdrive_remote_path', category, '') if video else
                      P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or '')
            if (not remote or remote.strip() != asset['remote'].strip() or not asset.get('relative_path')
                    or not re.fullmatch(r'[A-Za-z0-9_-]+', asset.get('file_id') or '')):
                raise ValueError('D02_STORAGE_CHANGED')
            target = MetaPreviewUtil.build_rclone_target_remote_path(remote, asset['relative_path'])
            if target != asset['path']:
                raise ValueError('D02_STORAGE_CHANGED')
        else:
            raise ValueError('D02_STORAGE_CHANGED')
        if cls.delete_asset_identity(dict(asset)) != asset['key']:
            raise ValueError('D02_STORAGE_CHANGED')

    @staticmethod
    def _delete_drive_missing(response):
        if response.status_code != 404:
            return False
        try:
            error = response.json().get('error') or {}
            return error.get('code') == 404 and (error.get('status') == 'NOT_FOUND' or
                any(e.get('reason') == 'notFound' for e in error.get('errors', [])))
        except (TypeError, ValueError, AttributeError):
            return False

    @classmethod
    def _execute_receipt_asset(cls, asset, category, request=None):
        """Internal executor, called only by a verified committed request ticket."""
        from .util_preview import MetaPreviewUtil
        from .asset_delete import DeleteReceipts
        import requests
        if (request is None or not request.busy or request.executing_asset is not asset
                or cls.delete_reference_registry(category).get_bind() is not request.engine
                or not DeleteReceipts._request_valid(request.engine, request.op_id, request)):
            return 'BLOCKED', 'REQUEST_PROOF_REQUIRED'
        checked = getattr(request, 'checked_assets', {}).get(asset['key']) if request.manual else asset
        if not checked or (not request.manual and not asset.get('reference_checked')):
            return 'BLOCKED', 'REFERENCE_PROOF_REQUIRED'
        if checked.get('retained') or checked.get('external'):
            return 'RETAINED', 'REGISTERED_REFERENCE_RETAINED'
        if checked.get('blocked'):
            return 'BLOCKED', 'REFERENCE_UNVERIFIED'
        try:
            cls._delete_storage_identity(asset)
        except Exception:
            return 'BLOCKED', 'STORAGE_CHANGED'
        if asset['storage'] == 'local':
            try:
                current = cls.check_delete_local(asset)
                before = asset.get('observation')
                if not before:
                    return 'BLOCKED', 'LOCAL_IDENTITY_MISSING'
                # A missing final file is unambiguous only with the same checked
                # ancestors. A missing mount/root/parent is not proof of absence.
                absent = current[-1][0] == asset['path'] and current[-1][1] is None
                if absent:
                    if json.dumps(current[:-1]) != json.dumps(before[:-1]):
                        return 'BLOCKED', 'LOCAL_PARENT_CHANGED'
                    return 'DELETED', 'CONFIRMED_ABSENT'
                if json.dumps(current) != json.dumps(before):
                    return 'BLOCKED', 'LOCAL_IDENTITY_CHANGED'
                os.remove(asset['path'])
                return 'DELETED', 'LOCAL_UNLINKED'
            except ValueError:
                return 'BLOCKED', 'LOCAL_PATH_UNVERIFIED'
            except FileNotFoundError:
                # A race during unlink is rechecked by explicit retry, not assumed.
                return 'UNKNOWN_OUTCOME', 'LOCAL_DISAPPEARED_DURING_DELETE'
            except OSError:
                return 'RETRY_PENDING', 'LOCAL_ACCESS_FAILED'
        config = MetaPreviewUtil.get_setting('rclone_conf', asset['category'], '/data/db/rclone.conf')
        remote_name = asset['remote'].split(':', 1)[0].strip()
        try:
            token = MetaPreviewUtil.get_gdrive_access_token(config, remote_name, file_id=asset['file_id'])
        except Exception:
            token = None
        if not token:
            return 'RETRY_PENDING', 'REMOTE_AUTH_UNAVAILABLE'
        if asset.get('canonical_file'):
            # Reuse one post-commit folder view for all candidates in that folder.
            # An ID no longer at the reviewed canonical name is never deleted.
            cache = getattr(request,'storage_observations',None)
            if cache is None:
                request.storage_observations = cache = {}
            folder = os.path.dirname(asset['relative_path'])
            key = (asset['remote'],folder,config)
            try:
                if key not in cache:
                    try:
                        cache[key]=cls._reconcile_storage_folder(folder,asset['category'],
                            asset['media_type'] not in ('trailer','preview'),'gdrive',asset['remote'])
                    except Exception as exc:
                        cache[key]=exc
                if isinstance(cache[key],Exception):raise cache[key]
                observed=cls._reconcile_directory_index(cache[key])
                entries=observed['by_name'].get(os.path.basename(asset['relative_path']).casefold(),[])
                if (len(entries)!=1 or not entries[0]['valid'] or entries[0]['file_id']!=asset['file_id'] or
                        len(observed['by_id'].get(asset['file_id'],[]))!=1):
                    return 'BLOCKED', 'REMOTE_IDENTITY_CHANGED'
            except Exception:
                return 'RETRY_PENDING', 'REMOTE_LIST_UNAVAILABLE'
        url = 'https://www.googleapis.com/drive/v3/files/' + asset['file_id']
        headers = {'Authorization': 'Bearer ' + token}
        try:
            response = requests.delete(url, params={'supportsAllDrives': 'true'}, headers=headers, timeout=15)
            if response.status_code in (200, 204):
                try:
                    MetaPreviewUtil.clear_dir_cache()
                except Exception:
                    logger.debug('[MetaDB Delete] 원격 삭제 후 캐시 갱신 보류')
                return 'DELETED', 'REMOTE_DELETED'
            if response.status_code == 404:
                return 'UNKNOWN_OUTCOME', 'REMOTE_DELETE_NOT_CONFIRMED'
            return 'RETRY_PENDING', 'REMOTE_DELETE_FAILED'
        except Exception:
            return 'UNKNOWN_OUTCOME', 'REMOTE_DELETE_UNCERTAIN'

    @classmethod
    def stage_asset_deletion(cls, session, category, request_type, context):
        """Record intent with the owner change; this function never commits."""
        from .asset_delete import DeleteReceipts, DeleteReceiptError
        assets = list((context or {}).get('assets', {}).values())
        if not assets:
            return None
        if not P.ModelSetting.get_bool('meta_db_use'):
            raise DeleteReceiptError('D02_DB_REQUIRED')
        if context.get('category') != category:
            raise DeleteReceiptError('D02_CATEGORY')
        engine = session.get_bind()
        if context.get('_receipt_engine') is not engine:
            DeleteReceipts.ensure(engine)
        owners = (context or {}).get('selected', ())
        checked = []
        for asset in assets:
            asset = dict(asset)
            evidence = (context or {}).get('proof', {}).get(asset['key'], [])
            if not any(tuple(ref) in owners or tuple(ref[:5]) in owners for ref in evidence):
                raise DeleteReceiptError('D02_OWNER_UNVERIFIED')
            asset['reference_evidence'] = evidence
            if not asset.get('retained'):
                cls._delete_storage_identity(asset)
                if asset['storage'] == 'local':
                    asset['observation'] = cls.check_delete_local(asset)
            checked.append(asset)
        request = DeleteReceipts.new_request(engine, category, session)
        op_id = DeleteReceipts.record(session, category, request_type, checked, owners, request=request)
        return {'engine': engine, 'op_id': op_id, 'request': request}

    @classmethod
    def finish_asset_deletion(cls, ticket):
        """Call only after commit returned successfully; cleanup cannot undo it."""
        if ticket is None:
            return {}
        from .asset_delete import DeleteReceipts
        result = {'cleanup_pending': True, 'delete_op_id': ticket['op_id']}
        try:
            request = ticket.get('request')
            registry = cls.delete_reference_registry(request.category) if request is not None else None
            if request is None or registry.get_bind() is not ticket['engine']:
                raise ValueError('D02_REQUEST_PROOF')
            page = DeleteReceipts.complete(ticket['engine'], ticket['op_id'], request,
                lambda asset, category: cls._execute_receipt_asset(asset, category, request))
            result.update(cleanup_pending=page['cleanup_pending'], cleanup_counts=page['cleanup_counts'],
                          cleanup_error=page['error_code'], receipt_pending_remaining=page['pending_remaining'],
                          receipt_next_cursor=page['next_cursor'], receipt_page_processed=page['processed'],
                          receipt_pages=page['pages'])
            logger.debug('[MetaDB Delete] DB commit 후 미디어 처리: pages=%d receipts=%d states=%s pending=%s error=%s',
                         page['pages'], page['returned'], page['cleanup_counts'], page['cleanup_pending'], page['error_code'])
        except Exception:
            # The atomic receipt survives a crash/error here. No session rollback
            # or unbounded retry, and no external file operation in this path.
            result['cleanup_error'] = 'POSTCOMMIT_CHECK_FAILED'
            logger.warning('[MetaDB Delete] DB 삭제 반영 완료 — 저장소 파일 후처리 확인 필요')
        return result

    @classmethod
    def replay_asset_deletion(cls, category, op_id, limit=100, cursor=None, paged=False, ticket=None):
        """Internal explicit re-query; no scheduler, purge, or new HTTP API."""
        from .asset_delete import DeleteReceipts, DeleteReceiptError
        if not P.ModelSetting.get_bool('meta_db_use'):
            raise DeleteReceiptError('D02_DB_REQUIRED')
        registry = cls.delete_reference_registry(category)
        engine = registry.get_bind()
        DeleteReceipts.ensure(engine)
        if ticket is not None:
            if ticket.get('engine') is not engine or ticket.get('op_id') != op_id:
                raise DeleteReceiptError('D02_REQUEST_PROOF')
            if cursor is not None and not paged:
                raise DeleteReceiptError('D02_PAGE_CURSOR_REQUIRES_PAGED')
            page = DeleteReceipts.execute_page(engine, op_id, ticket.get('request'),
                lambda asset, cat: cls._execute_receipt_asset(asset, cat, ticket.get('request')),
                limit, cursor, retry=True)
            return page if paged else page['items']
        if paged:
            return DeleteReceipts.replay_page(engine, op_id, limit, cursor)
        if cursor is not None:
            raise DeleteReceiptError('D02_PAGE_CURSOR_REQUIRES_PAGED')
        return DeleteReceipts.replay(engine, op_id, limit)

    @classmethod
    def pending_asset_deletions(cls, category):
        from .asset_delete import DeleteReceipts, DeleteReceiptError
        if not P.ModelSetting.get_bool('meta_db_use'):
            raise DeleteReceiptError('D02_DB_REQUIRED')
        if category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'PERSON'):
            raise DeleteReceiptError('D02_CATEGORY')
        registry = cls.delete_reference_registry(category)
        try:
            return DeleteReceipts.pending_operations(registry.get_bind(), category)
        finally:
            registry.remove()

    @classmethod
    def retry_asset_deletion(cls, category, op_id, req=None):
        """Separate explicit admin action; status replay never calls this path."""
        from .mod_meta_db import ModuleMetaDb
        from .asset_delete import DeleteReceipts, DeleteReceiptError
        from sqlalchemy import select, inspect
        if not ModuleMetaDb._asset_cleanup_authorized(req):
            raise DeleteReceiptError('D02_ADMIN_REQUIRED')
        if not P.ModelSetting.get_bool('meta_db_use'):
            raise DeleteReceiptError('D02_DB_REQUIRED')
        if category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'PERSON'):
            raise DeleteReceiptError('D02_CATEGORY')
        DeleteReceipts._scope('replay', op_id)
        registry = cls.delete_reference_registry(category)
        engine = registry.get_bind()
        try:
            with engine.connect() as conn:
                if not inspect(conn).has_table(DeleteReceipts.table.name):
                    raise DeleteReceiptError('D02_OPERATION_NOT_FOUND')
                DeleteReceipts._validate(conn)
                if not conn.execute(select(DeleteReceipts.table.c.receipt_id).where(
                        DeleteReceipts.table.c.op_id == op_id, DeleteReceipts.table.c.category == category).limit(1)).first():
                    raise DeleteReceiptError('D02_OPERATION_NOT_FOUND')
            request = DeleteReceipts.manual_request(engine, category, registry, op_id)
            with engine.connect() as conn:
                payloads = conn.execute(select(DeleteReceipts.table.c.payload).where(
                    DeleteReceipts.table.c.op_id == op_id, DeleteReceipts.table.c.category == category)).scalars().all()
            candidates = [json.loads(value)['asset'] for value in payloads]
            for asset in candidates:
                asset.pop('reference_evidence', None)
            cls.delete_reference_snapshot(candidates, set(), category=category)
            request.checked_assets = {asset['key']: asset for asset in candidates}
            page = DeleteReceipts.complete(engine, op_id, request,
                lambda asset, cat: cls._execute_receipt_asset(asset, cat, request), retry=True)
            logger.debug('[MetaDB Delete] 관리자 명시 재시도: category=%s op=%s pages=%d states=%s pending=%s error=%s',
                category, op_id[:8], page['pages'], page['cleanup_counts'], page['cleanup_pending'], page['error_code'])
            return dict(cleanup_pending=page['cleanup_pending'], cleanup_counts=page['cleanup_counts'],
                cleanup_error=page['error_code'], delete_op_id=op_id, receipt_page_processed=page['processed'],
                receipt_pages=page['pages'], receipt_pending_remaining=page['pending_remaining'],
                receipt_next_cursor=page['next_cursor'], receipt_deferred_active=page['deferred_active'])
        finally:
            registry.remove()

    @classmethod
    def delete_owned_asset(cls, asset, delete_context=None):
        # No caller, including a stale D01 context, may bypass the DB transaction.
        raise ValueError('D02_RECEIPT_REQUIRED: DB 트랜잭션에 삭제 의도를 먼저 기록해야 합니다.')

    @classmethod
    def delete_item_user_images(cls, item, delete_context=None):
        raise ValueError('D02_RECEIPT_REQUIRED: 사용자 이미지의 직접 삭제를 보류했습니다.')

    @classmethod
    def delete_person_custom_image(cls, p_rec, delete_context=None):
        return False, 'D02_RECEIPT_REQUIRED: 인물 이미지의 직접 삭제를 보류했습니다.'

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
    def note_media_completion(cls, options, result, media_path):
        """Request-local receipt; never insert this control state into metadata."""
        receipt = (options or {}).get('_media_completion')
        if not isinstance(receipt, dict):
            return
        blocked = result.get('commit_state') == 'unknown' or result.get('error_code') in (
            'commit_outcome_unknown', 'storage_conflict', 'storage_lookup_failed', 'storage_asset_incomplete')
        warnings = bool(result.get('failures') or result.get('warnings') or result.get('cleanup_pending'))
        state = 'blocked' if blocked else 'finished_with_warnings' if warnings else 'finished'
        if media_path and not os.path.isfile(media_path) and not blocked:
            state = 'invalid_input'
        if state == 'finished' and result.get('ret') == 'warning' and not result.get('data') and not warnings:
            state = 'policy_skipped'
        receipt.update(path=str(media_path or ''), state=state)
        for key in ('ret', 'commit_state', 'cleanup_pending', 'warnings', 'failures', 'error_code'):
            if key in result:
                receipt[key] = copy.deepcopy(result[key])

    @classmethod
    def finalize_info_return(cls, entity_dict, extra_opts=None, category=None):
        """DB 사용 여부와 무관하게 호출자에게 반환할 최종 데이터의 사본을 안전하게 가공"""
        if not entity_dict or not isinstance(entity_dict, dict):
            return entity_dict

        opts = dict(extra_opts or {})
        res = copy.deepcopy(entity_dict)
        (res.get('extra_info') or {}).pop('source_video_path', None)
        from support_site import SiteAvBase
        SiteAvBase.sanitize_incomplete_images(res)

        current_plot = str(res.get('plot') or '').strip()
        fallback_tagline = str(res.get('tagline') or '').strip()
        if not current_plot and fallback_tagline:
            res['plot'] = fallback_tagline

        # 내부 저장 및 갱신 파이프라인 호출일 때는 프록시 래핑을 건너뛰고 순수 원본 데이터 반환
        if opts.get('for_db') or opts.get('internal'):
            return res

        (res.get('extra_info') or {}).pop('media_errors', None)
        entries = (res.get('extra_info') or {}).pop('_media_replacements', [])
        if entries:
            cls.finish_media_replacements(entries, not P.ModelSetting.get_bool('meta_db_use') or not P.ModelSetting.get_bool('meta_db_save'))
        res.pop('stored_images', None)

        # 외부 API 호출 및 웹 화면 출력 시에만 프록시 래핑 적용
        try:
            from .mod_meta_db import ModuleMetaDb
            res = ModuleMetaDb.apply_transient_overrides(res, opts, category=category)
        except Exception as e_override:
            logger.debug(f"[MetaUtil] apply_transient_overrides 예외: {e_override}")

        # Display fallback belongs only to the returned copy, after DB/internal exits.
        thumbs = res.get('thumb') or []
        if not any(isinstance(t, dict) and t.get('aspect') == 'poster' and t.get('value') for t in thumbs):
            landscape = next((t for t in thumbs if isinstance(t, dict) and t.get('aspect') == 'landscape' and t.get('value')), None)
            if landscape:
                fallback = copy.deepcopy(landscape)
                fallback['aspect'] = 'poster'
                res['thumb'] = list(thumbs) + [fallback]
                res['poster_url'] = fallback['value']

        if opts.get('strip_images'):
            res['poster_url'] = ''
            res['landscape_url'] = ''
            res['thumb'] = []
            res['fanart'] = []

        return res

    @staticmethod
    def _item_media_signature(media):
        return (media.media_type,media.sort_order or 0,media.gdrive_fileid or '',media.relative_path or '',media.url,
                bool(media.is_user),media.source,media.checksum or '')

    @staticmethod
    def _item_drive_id(value):
        from urllib.parse import urlsplit, parse_qs
        try:parsed=urlsplit(str(value or ''))
        except ValueError:return ''
        host=(parsed.hostname or '').lower()
        if host not in ('drive.google.com','googleusercontent.com') and not host.endswith('.googleusercontent.com'):return ''
        match=re.search(r'/d/([^/]+)',parsed.path)
        return (parse_qs(parsed.query).get('id') or [''])[0] or (match[1] if match else '')

    @classmethod
    def _legacy_drive_image_match(cls, item, media, folder, root, observed, strict=True):
        """A unique private-folder ID, exact file name and URL prove a legacy asset."""
        if not root or observed is None or item.category not in ('JAV_CEN','JAV_UNCEN','WESTERN'):
            return None
        if not media.gdrive_fileid or cls._item_drive_id(media.url) != media.gdrive_fileid:
            return None
        index = cls._reconcile_directory_index(observed)
        matches = index['by_id'].get(media.gdrive_fileid,())
        if len(matches) != 1:
            return None
        value = matches[0]
        parsed = cls._reconcile_file_slot(value['name'])
        if (not value['valid'] or len(index['by_name'][value['name'].casefold()]) != 1 or
                value['path'].rpartition('/')[0] != folder or not parsed or parsed[0] not in cls._reconcile_item_stems(item) or
                parsed[1] not in ('poster','landscape','fanart')):
            return None
        if not strict and sum(slot == parsed[1:] for stem in cls._reconcile_item_stems(item)
                for _,slot in index['by_stem'].get(stem,())) != 1:
            return None
        if strict and (parsed[1] != media.media_type or parsed[1]=='fanart' and parsed[2] != (media.sort_order or 0) or
                media.relative_path and media.relative_path != value['path'] or media.is_user and not parsed[3]):
            return None
        return value

    @classmethod
    def repair_item_media_registration(cls, item, mode, root, folder, observed=None, extra=None, server_url='', allow_poster=True, private_only=False):
        """Pure DB normalization in the caller's transaction; never fetch, transfer or commit."""
        from .mod_meta_db import MetaMedia
        from urllib.parse import urlsplit, parse_qs
        extra=extra if extra is not None else copy.deepcopy(item.extra_info or {})
        observed = cls._reconcile_directory_index(observed) if observed is not None else None
        counts={}
        def changed(kind):counts[kind]=counts.get(kind,0)+1
        for media in item.media_files:
            if media.source not in ('gds','site'):continue
            value=cls._legacy_drive_image_match(item,media,folder,root,observed,strict=not private_only) if mode=='gdrive' else None
            if value:
                if not media.relative_path or private_only and media.relative_path != value['path']:media.relative_path=value['path'];changed('누락 경로 복원')
                parsed = cls._reconcile_file_slot(value['name'])
                if private_only:media.media_type,media.sort_order,media.is_user = parsed[1:]
                else:media.is_user=bool(media.is_user or '_user.' in value['name'].lower())
                media.source='user' if media.is_user else 'gdrive';changed('출처 정상화')
            elif not private_only and media.source=='gds' and not media.gdrive_fileid and not media.relative_path and not cls._item_drive_id(media.url):
                if str(media.url or '').startswith(('http://','https://')):media.source='site';changed('외부 출처 정상화')
        # No foreign key targets MetaMedia. IDs/timestamps are registration metadata, not asset identity.
        seen={}
        for media in list(item.media_files):
            key=cls._item_media_signature(media)
            if not key[2]:continue
            if private_only and (media.source=='gds' or mode!='gdrive' or not
                    cls._legacy_drive_image_match(item,media,folder,root,observed,strict=False)):continue
            if key in seen:item.media_files.remove(media);changed('중복 등록 제거')
            else:seen[key]=media
        def false_poster(value):
            try:parsed=urlsplit(str(value or ''))
            except ValueError:return False
            wrapped=False
            if re.search(r'/metadata/normal/jav_image(?:_un)?$',parsed.path):
                raw=(parse_qs(parsed.query).get('url') or [''])[0]
                try:parsed=urlsplit(raw)
                except ValueError:return False
                wrapped=True
            if not wrapped and not (server_url and str(value).startswith(server_url.rstrip('/')+'/')):return False
            for media in item.media_files:
                if media.media_type!='landscape' or not media.relative_path:continue
                path=media.relative_path
                if (path.startswith('/') or ':' in path or any(p in ('','.','..') for p in path.split('/'))):continue
                name=path.rsplit('/',1)[-1]
                stem=str((item.code if item.category=='WESTERN' else item.ui_code or item.originaltitle) or item.code)
                if (re.fullmatch(re.escape(stem)+r'_pl\.(jpg|jpeg|png|webp|gif)',name,re.I) and
                        parsed.path.endswith('/'+path) and not cls._item_drive_id(value)):
                    return True
            return False
        bad_column=allow_poster and false_poster(item.poster_url);bad_extra=allow_poster and false_poster(extra.get('poster_url'))
        if allow_poster and (bad_column or bad_extra) and any(m.media_type=='poster' for m in item.media_files):
            registered=[m for m in item.media_files if m.media_type=='poster' and mode=='gdrive' and
                        cls._legacy_drive_image_match(item,m,folder,root,observed,strict=not private_only)]
            users=[m for m in registered if m.is_user];registered=users or registered
            if len(registered)==1:
                if bad_column:item.poster_url=registered[0].url;changed('포스터 URL 보정')
                if bad_extra:extra['poster_url']=registered[0].url;changed('포스터 URL 보정')
                item.extra_info=copy.deepcopy(extra)
        if allow_poster and (bad_column or bad_extra) and not any(m.media_type=='poster' for m in item.media_files):
            candidates=[]
            stem=str((item.code if item.category=='WESTERN' else item.ui_code or item.originaltitle) or item.code)
            for value,_ in (observed or {}).get('by_stem',{}).get(stem.casefold(),()):
                if value['valid'] and value['path'].rpartition('/')[0]==folder and re.fullmatch(re.escape(stem)+r'_p(_user)?\.(jpg|jpeg|png|webp|gif)',value['name'],re.I):candidates.append(value)
            users=[v for v in candidates if '_user.' in v['name'].lower()];candidates=users or candidates
            url=''
            if len(candidates)==1 and (mode=='gdrive' and root or mode=='image_server' and root and server_url):
                value=candidates[0];url=cls.format_gdrive_thumbnail_url(value['file_id']) if mode=='gdrive' else server_url.rstrip('/')+'/'+value['path']
                user='_user.' in value['name'].lower()
                item.media_files.append(MetaMedia(media_type='poster',sort_order=0,url=url,relative_path=value['path'],
                    gdrive_fileid=value['file_id'] or None,is_user=user,source='user' if user else 'gdrive' if mode=='gdrive' else 'local'))
            if bad_column:item.poster_url=url;changed('포스터 URL 보정')
            if bad_extra:
                if url:extra['poster_url']=url
                else:extra.pop('poster_url',None)
                changed('포스터 URL 보정')
        if counts:item.extra_info=copy.deepcopy(extra)
        return counts

    @staticmethod
    def _reconcile_file_slot(name):
        match = re.fullmatch(r'(.+)_(p|pl|art_([1-9]\d*))(_user)?\.(jpg|jpeg|png|webp|gif)', name, re.I)
        if match:
            kind = 'poster' if match[2].lower() == 'p' else 'landscape' if match[2].lower() == 'pl' else 'fanart'
            return match[1].casefold(), kind, 0 if kind == 'poster' else 1 if kind == 'landscape' else int(match[3])-1, bool(match[4])
        match = re.fullmatch(r'(.+)_(trailer|preview)\.mp4', name, re.I)
        return (match[1].casefold(), match[2].lower(), 0, False) if match else None

    @staticmethod
    def _reconcile_item_stems(item):
        values = [item.code] if item.category == 'WESTERN' else [item.ui_code, item.originaltitle, item.code]
        return list(dict.fromkeys(str(value).casefold() for value in values if value and
            str(value) not in ('.', '..') and not re.search(r'[\x00-\x1f/\\:*?"<>|\[\]{}]', str(value))))

    @classmethod
    def _reconcile_directory_index(cls, observed):
        if isinstance(observed, dict):
            if observed.get('complete') is not True or any(key not in observed for key in ('entries','by_name','by_id','by_stem')):
                raise ValueError('LIST_INCOMPLETE')
            return observed
        if not isinstance(observed, list):
            raise ValueError('LIST_INCOMPLETE')
        index = dict(complete=True, entries=observed, by_name={}, by_id={}, by_stem={})
        for entry in observed:
            if not isinstance(entry, dict) or any(key not in entry for key in ('name','path','file_id','valid')):
                raise ValueError('LIST_INCOMPLETE')
            index['by_name'].setdefault(entry['name'].casefold(), []).append(entry)
            if entry['file_id']:
                index['by_id'].setdefault(entry['file_id'], []).append(entry)
            slot = cls._reconcile_file_slot(entry['name'])
            if slot:
                index['by_stem'].setdefault(slot[0], []).append((entry, slot[1:]))
        return index

    @classmethod
    def _reconcile_storage_folder(cls, folder, category, image, mode, root, prefix=None, unfiltered=False,
                                  directory_cache=None, config_identity=()):
        """Read one complete directory. Selection and ambiguity checks are in-memory."""
        import stat
        from .util_preview import MetaPreviewUtil
        def relative(value):
            value = str(value or '').replace('\\', '/')
            if value.startswith('/') or ':' in value or re.search(r'[\x00-\x1f]', value) or any(p in ('.','..') for p in value.split('/')):
                raise ValueError('LIST_PATH_INVALID')
            return value.strip('/')
        folder = relative(folder)
        if not root:
            raise ValueError('LIST_ROOT_UNCONFIGURED')
        # None is reserved for root evidence, never a directory snapshot.
        root_key = (mode,root,None,config_identity)
        if mode == 'gdrive':
            target = MetaPreviewUtil.build_rclone_target_remote_path(root, folder)
            if not target:
                raise ValueError('LIST_ROOT_INVALID')
            command = [MetaPreviewUtil.get_setting('rclone_path', category, 'rclone'), '--config',
                MetaPreviewUtil.get_setting('rclone_conf', category, '/data/db/rclone.conf'), 'lsjson', target]
            response = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
            if MetaPreviewUtil.remote_missing(response, directory=True):
                if directory_cache is None or directory_cache.get(root_key,{}).get('root_verified') is not True:
                    checked = subprocess.run(command[:-1] + ['--dirs-only', MetaPreviewUtil.build_rclone_target_remote_path(root, '')],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
                    if checked.returncode or checked.stderr.strip() or not checked.stdout.strip():
                        raise ValueError('LIST_ROOT_UNVERIFIED')
                    info = json.loads(checked.stdout)
                    if not isinstance(info, list) or any(not isinstance(entry,dict) or entry.get('IsDir') is not True or
                            not isinstance(entry.get('Name') or entry.get('Path'),str) or
                            not (entry.get('Name') or entry.get('Path')) or
                            re.search(r'[\x00-\x1f/\\]',entry.get('Name') or entry.get('Path')) or
                            (entry.get('Name') or entry.get('Path')) in ('.','..') or
                            entry.get('Path') not in (None,'',entry.get('Name') or entry.get('Path')) for entry in info):
                        raise ValueError('LIST_ROOT_UNVERIFIED')
                    if directory_cache is not None:
                        directory_cache[root_key] = dict(root_verified=True)
                raise ValueError('LIST_DIRECTORY_MISSING')
            if response.returncode or response.stderr.strip() or not response.stdout.strip():
                raise ValueError('LIST_PERMISSION_DENIED' if re.search(r'permission denied|access denied|forbidden',response.stderr,re.I) else 'LIST_FAILED')
            entries = json.loads(response.stdout)
            if not isinstance(entries, list):
                raise ValueError('LIST_INCOMPLETE')
        else:
            if not stat.S_ISDIR(os.stat(root).st_mode) or not os.access(root, os.R_OK | os.X_OK):
                raise ValueError('LIST_ROOT_UNVERIFIED')
            base = os.path.realpath(root)
            target = os.path.abspath(os.path.join(root, folder))
            if os.path.commonpath([base, os.path.realpath(target)]) != base or os.path.islink(target):
                raise ValueError('LIST_PATH_INVALID')
            entries = []
            try:
                with os.scandir(target) as directory:
                    for child in directory:
                        info = child.stat(follow_symlinks=False)
                        entries.append(dict(Name=child.name,Path=child.name,Size=info.st_size,
                            IsDir=not stat.S_ISREG(info.st_mode)))
            except FileNotFoundError:
                # Recheck the root; a vanished/unmounted root is not proof of absence.
                with os.scandir(root):
                    pass
                if directory_cache is not None:
                    directory_cache[root_key] = dict(root_verified=True)
                raise ValueError('LIST_DIRECTORY_MISSING')
        observed = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('LIST_INCOMPLETE')
            name = entry.get('Name') or entry.get('Path')
            if (not isinstance(name, str) or not name or name in ('.','..') or re.search(r'[\x00-\x1f/\\]', name)
                    or entry.get('Path') not in (None,'',name)):
                raise ValueError('LIST_INCOMPLETE')
            directory = entry.get('IsDir')
            if type(directory) is not bool or (not directory and (type(entry.get('Size')) is not int or
                    mode == 'gdrive' and not isinstance(entry.get('ID',entry.get('id')), str))):
                raise ValueError('LIST_INCOMPLETE')
            fid = entry.get('ID',entry.get('id',''))
            valid = not directory and entry['Size'] > 0 and (mode != 'gdrive' or re.fullmatch(r'[A-Za-z0-9_-]+',fid) is not None)
            slot = cls._reconcile_file_slot(name)
            mime = str(entry.get('MimeType') or '').lower()
            if slot and mime:
                valid = valid and (mime.startswith('video/') if slot[1] in ('trailer','preview') else mime.startswith('image/'))
            observed.append(dict(name=name,path='/'.join(p for p in (folder,name) if p),file_id=fid,
                valid=bool(valid),size=entry.get('Size'),mime_type=mime,is_dir=directory))
        return cls._reconcile_directory_index(observed)

    @classmethod
    def reconcile_item_media_with_storage(cls, code, category, use_dir_cache=False, directory_cache=None, legacy_only=False):
        """Repair DB registrations using complete, request/batch-local storage snapshots."""
        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia
        from .util_preview import MetaPreviewUtil
        from .util_runtime import stage_storage_record, commit_storage_record, StorageConflict, storage_logger
        from sqlalchemy.orm import lazyload, selectinload, sessionmaker
        log = storage_logger(logger)
        result = dict(ret='success',is_updated=False,changes=[],warnings=[],checked=0,updated=0,unchanged=0,
            conflicts=0,unknown=0,observations=[],skipped=[],repair_counts={},deferred=[])
        summary = result['summary'] = dict(folder_entries=0,regular_files=0,user_files=0,created=0,relinked=0,
            removed=0,normalized_gds=0,confirmed=0,deferred=0,list_failures=0)
        category = str(category or '').strip().upper()
        if category not in ('JAV_CEN','JAV_UNCEN','WESTERN') or not isinstance(code,str) or not code.strip():
            return dict(result,ret='error',msg='정확한 AV 카테고리와 내부 코드를 지정하세요.')
        if not P.ModelSetting.get_bool('meta_db_use'):
            return dict(result,ret='error',msg='메타 DB가 비활성화되어 있습니다.')
        allowed, message = ModuleMetaDb._schema_write_guard(category,'미디어 저장소 대조')
        if not allowed:
            return dict(result,ret='error',msg=message)
        registry, _, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if registry is None or std_cat != category:
            return dict(result,ret='error',msg='DB 카테고리 또는 Session을 확인하세요.')
        session = sessionmaker(bind=registry.get_bind(),autoflush=False)()
        stage, original_gds = 'DB_REPAIR', None
        deferred, deferred_slots, conflict_slots = set(), set(), set()
        def hold(stage_name, reason, slot=None, basename='', error=None):
            key = (stage_name,reason,slot)
            if key in deferred:
                return
            deferred.add(key)
            result['deferred'].append(dict(stage=stage_name,reason=reason,slot=slot,basename=basename,
                exception_type=type(error).__name__ if error else ''))
            slot_key = slot if slot is not None else (stage_name,reason)
            if slot_key not in deferred_slots:
                deferred_slots.add(slot_key)
                summary['deferred'] += 1
                result['unknown'] += 1
            if stage_name=='IDENTITY' and reason in ('multiple_files','duplicate_name_or_id','registered_id_other_file','registered_id_ambiguous'):
                conflict_slots.add(slot_key);result['conflicts']=len(conflict_slots)
            if stage_name == 'LIST':summary['list_failures'] += 1
        try:
            item = session.query(MetaItem).options(lazyload('*'),selectinload(MetaItem.media_files)).filter_by(category=category,code=code).first()
            if item is None:
                return dict(result,ret='error',msg='DB에서 해당 작품을 찾을 수 없습니다.')
            original_gds = sum(m.source == 'gds' for m in item.media_files)
            snapshot = stage_storage_record(session,item,('media_files',))
            prefix = 'western' if category == 'WESTERN' else 'jav_uncensored' if category == 'JAV_UNCEN' else 'jav_censored'
            def settings():
                return (P.ModelSetting.get(prefix+'_image_mode') or P.ModelSetting.get('jav_censored_image_mode') or 'ff_proxy',
                    P.ModelSetting.get('jav_censored_image_server_local_path') or os.path.join(path_data,'images'),
                    P.ModelSetting.get('jav_censored_image_server_url') or ((F.SystemModelSetting.get('ddns') or '').rstrip('/')+'/images'
                        if F.SystemModelSetting.get('ddns') else ''),P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or '',
                    MetaPreviewUtil.get_setting('preview_storage_type',category,'local'),
                    MetaPreviewUtil.get_setting('preview_local_path',category,''),
                    MetaPreviewUtil.get_setting('preview_gdrive_remote_path',category,'') or MetaPreviewUtil.get_setting('preview_video_folder_id',category,''),
                    MetaPreviewUtil.get_setting('rclone_path',category,'rclone'),MetaPreviewUtil.get_setting('rclone_conf',category,'/data/db/rclone.conf'))
            configuration = settings()
            image_mode, local_root, server_url, image_remote, video_mode, video_local, video_remote = configuration[:7]
            stems = cls._reconcile_item_stems(item)
            if not stems:
                raise ValueError('IDENTITY_STEM_INVALID')
            stem = stems[0]
            extra = copy.deepcopy(item.extra_info or {})
            cache = directory_cache if directory_cache is not None else {}
            groups = {}
            if image_mode in ('gdrive','image_server'):
                folder = cls.get_image_relative_dir(item.domain,category,stem,studio=item.studio or '',year=item.year or 1900)
                groups[(image_mode,image_remote if image_mode == 'gdrive' else local_root,folder)] = dict(image=True,videos={})
            else:result['skipped'].append('이미지: 실물 저장 방식이 아닙니다.')
            if not legacy_only and video_mode in ('local','gdrive'):
                mode, root = ('gdrive',video_remote) if video_mode == 'gdrive' else ('image_server',video_local)
                for kind in ('trailer','preview'):
                    folder,name = MetaPreviewUtil.get_video_relative_path(item,kind,category)
                    groups.setdefault((mode,root,folder),dict(image=False,videos={}))['videos'][kind] = name
            claimed, repaired_rows = set(), set()
            def slot_name(slot):return ':'.join(map(str,slot))
            def active(media, mode):
                if media.source=='gds':
                    path = str(media.relative_path or '').replace('\\','/')
                    parsed = cls._reconcile_file_slot(path.rsplit('/',1)[-1])
                    current_path = (parsed and parsed[0] in stems and parsed[1] in kinds and
                        path.rpartition('/')[0] == folder and not path.startswith('/') and
                        ':' not in path and not any(part in ('','.','..') for part in path.split('/')))
                    ids = observed['by_id'].get(media.gdrive_fileid,()) if mode=='gdrive' and media.gdrive_fileid else ()
                    actual = cls._reconcile_file_slot(ids[0]['name']) if len(ids)==1 else None
                    return bool(current_path or actual and actual[0] in stems and actual[1] in kinds)
                if mode=='gdrive':
                    return media.source in ('gdrive','user') and (cls.media_in_storage(media,mode) or media.source=='gdrive')
                return media.source!='gdrive' and cls.media_in_storage(media,mode) and (
                    media.source in ('user','local','image_server') or
                    bool(server_url and str(media.url or '').startswith(server_url.rstrip('/')+'/')))
            for (mode,root,folder),group in groups.items():
                stage = 'LIST'
                kinds = set(group['videos']) | ({'poster','landscape','fanart'} if group['image'] else set())
                try:
                    config_identity = ()
                    if mode == 'gdrive':
                        try:
                            st = os.stat(configuration[8]); stamp = (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)
                        except OSError:stamp = None
                        config_identity = configuration[7:] + (stamp,)
                    key = (mode,root,folder,config_identity)
                    root_key = (mode,root,None,config_identity)
                    if key in cache:
                        cached = cache.pop(key)
                        if isinstance(cached,dict) and cached.get('missing') is True:
                            cache[key] = cached
                            log.debug('[MediaSync] %s 폴더 없음 (배치 내 앞선 조회 결과 재사용): %s',
                                '이미지' if group['image'] else '영상',folder)
                            raise ValueError('LIST_DIRECTORY_MISSING')
                        observed = cls._reconcile_directory_index(cached);cache[key] = observed
                        log.debug('[MediaSync] %s 폴더 목록 재사용: %s (%s개, %s, 캐시 재사용)',
                            '이미지' if group['image'] else '영상',folder,format(len(observed['entries']),','),'Google Drive' if mode=='gdrive' else '로컬')
                    else:
                        observed = cls._reconcile_directory_index(cls._reconcile_storage_folder(folder,category,group['image'],mode,root,
                            directory_cache=cache,config_identity=config_identity))
                        # Evict whole snapshots only. One large directory is never truncated.
                        count = sum(len(value.get('entries',())) for value in cache.values())
                        for evict_key in list(cache):
                            if count + len(observed['entries']) <= 10000:
                                break
                            if cache[evict_key].get('complete') is True:
                                evicted = cache.pop(evict_key);count -= len(evicted['entries'])
                        cache[key] = observed
                        cache[root_key] = dict(root_verified=True)
                        log.debug('[MediaSync] %s 폴더 목록 조회 완료: %s (%s개, %s, 새로 조회)',
                            '이미지' if group['image'] else '영상',folder,format(len(observed['entries']),','),'Google Drive' if mode=='gdrive' else '로컬')
                    summary['folder_entries'] += len(observed['entries'])
                except (ValueError,OSError,subprocess.SubprocessError) as exc:
                    if (type(exc) is ValueError and str(exc)=='LIST_DIRECTORY_MISSING'
                            and cache.get(root_key,{}).get('root_verified') is True and key not in cache):
                        cache[key] = dict(missing=True)
                        log.debug('[MediaSync] %s 폴더 없음 (새로 조회): %s',
                            '이미지' if group['image'] else '영상',folder)
                    if (type(exc) is ValueError and str(exc)=='LIST_DIRECTORY_MISSING' and not group['image']
                            and not any(m.media_type in ('trailer','preview') for m in item.media_files)
                            and not extra.get('trailer_video') and not extra.get('preview_clip')):
                        # This exception follows a successful root check. Do not turn
                        # the missing directory into an empty snapshot or remove rows.
                        result['skipped'].append('영상: 등록된 영상 없음 — 동기화 대상 없음')
                        log.debug('[MediaSync] [%s] %s: 등록된 영상 없음 — 동기화 대상 없음 (%s)',category,item.ui_code or code,folder)
                        continue
                    reason = ('timeout' if isinstance(exc,subprocess.TimeoutExpired) else
                        'invalid_json' if isinstance(exc,json.JSONDecodeError) else
                        'permission_denied' if isinstance(exc,PermissionError) else
                        str(exc).lower() if isinstance(exc,ValueError) and re.fullmatch(r'LIST_[A-Z_]+',str(exc)) else 'folder_unverified')
                    hold('LIST',reason,('folder',mode,folder),error=exc)
                    continue
                except Exception as exc:
                    hold('LIST','unexpected_list_error',('folder',mode,folder),error=exc)
                    log.exception('[MediaSync] [%s] %s: 저장소 폴더 목록을 확인하지 못해 관련 등록을 보존합니다.',category,item.ui_code or code)
                    continue
                stage = 'VALIDATE'
                candidates = {}
                for alias in stems:
                    for entry,slot in observed['by_stem'].get(alias,()):
                        kind,order,user = slot
                        if kind not in kinds:
                            continue
                        if kind in group['videos'] and entry['name'].casefold() not in (
                                group['videos'][kind].casefold(), alias+'_'+kind+'.mp4'):
                            continue
                        candidates.setdefault(slot,[]).append(entry)
                # Video path formats may use a basename unrelated to the image stem.
                for kind,name in group['videos'].items():
                    slot = (kind,0,False)
                    for entry in observed['by_name'].get(name.casefold(),()):
                        if entry not in candidates.setdefault(slot,[]):candidates[slot].append(entry)
                selected, blocked = {}, set()
                for slot,entries in candidates.items():
                    if len(entries) != 1:
                        blocked.add(slot);hold('IDENTITY','multiple_files',slot,entries[0]['name']);continue
                    entry = entries[0]
                    if len(observed['by_name'][entry['name'].casefold()]) != 1 or entry['file_id'] and len(observed['by_id'][entry['file_id']]) != 1:
                        blocked.add(slot);hold('IDENTITY','duplicate_name_or_id',slot,entry['name']);continue
                    if not entry['valid']:
                        blocked.add(slot);hold('VALIDATE','invalid_file_metadata',slot,entry['name']);continue
                    selected[slot] = entry
                    summary['user_files' if slot[2] else 'regular_files'] += 1
                if group['image'] and mode == 'image_server' and not server_url:
                    image_slots = {slot for slot in candidates if slot[0] not in group['videos']}
                    image_slots.update({('poster',0,False),('poster',0,True),('landscape',1,False),('landscape',1,True)})
                    for slot in sorted(image_slots):hold('VALIDATE','image_server_url_missing',slot)
                    # Local videos use FF proxy URLs and remain usable without an image server URL.
                    group = dict(group,image=False)
                    kinds = set(group['videos'])
                    candidates = {slot:entries for slot,entries in candidates.items() if slot[0] in kinds}
                    selected = {slot:entry for slot,entry in selected.items() if slot[0] in kinds}
                    blocked = {slot for slot in blocked if slot[0] in kinds}
                    if not kinds:continue
                if group['image']:
                    stage = 'DB_REPAIR'
                    before_rows = {id(m):cls._item_media_signature(m) for m in item.media_files}
                    before_gds = sum(m.source=='gds' for m in item.media_files)
                    counts = cls.repair_item_media_registration(item,mode,root,folder,observed,extra,server_url,
                        allow_poster=legacy_only and not any(slot[0]=='poster' for slot in blocked),private_only=True)
                    for name,count in counts.items():result['repair_counts'][name] = result['repair_counts'].get(name,0)+count
                    summary['removed'] += len(before_rows.keys() - {id(m) for m in item.media_files})
                    summary['created'] += len({id(m) for m in item.media_files} - before_rows.keys())
                    repaired_rows.update(id(m) for m in item.media_files if id(m) in before_rows and cls._item_media_signature(m)!=before_rows[id(m)])
                    summary['relinked'] = len(repaired_rows)
                    summary['normalized_gds'] += before_gds - sum(m.source=='gds' for m in item.media_files)
                if legacy_only:
                    result['checked'] += len(selected);summary['confirmed'] += len(selected)
                    continue
                stage = 'IDENTITY'
                rows, slots, foreign = {}, set(candidates), set()
                originals = list(item.media_files)
                def row_slot(media):
                    return (media.media_type,(media.sort_order or 0) if media.media_type=='fanart' else 1 if media.media_type=='landscape' else 0,
                        bool(cls.is_user_media(media)) if media.media_type not in ('trailer','preview') else False)
                def classify(media):
                    path = str(media.relative_path or '').replace('\\','/')
                    placeholder = {'_p_user':('poster',0,True),'_pl_user':('landscape',1,True)}
                    placeholder_sources = ('user','gdrive') if mode=='gdrive' else ('user','local','image_server')
                    if path in placeholder and media.source in placeholder_sources and not media.gdrive_fileid and media.url in ('',None,path):return placeholder[path]
                    if not active(media,mode):return None
                    ids = observed['by_id'].get(media.gdrive_fileid,()) if mode == 'gdrive' and media.gdrive_fileid else ()
                    if media.media_type in group['videos'] and ids:
                        slot = row_slot(media)
                        actual = cls._reconcile_file_slot(ids[0]['name']) if len(ids)==1 else None
                        if (len(ids)==1 and ids[0]['valid'] and len(observed['by_name'][ids[0]['name'].casefold()])==1
                                and actual and actual[0] in stems and actual[1]==media.media_type):
                            return actual[1:]
                        # A known video ID is present, so absence of its old name
                        # cannot authorize removing its row or extra_info slot.
                        blocked.add(slot);foreign.add(id(media))
                        reason = ('registered_id_ambiguous' if len(ids)>1 else
                            'registered_id_other_file' if actual else 'registered_video_name_unverified')
                        hold('IDENTITY',reason,slot,ids[0]['name']);return None
                    parsed = cls._reconcile_file_slot(path.rsplit('/',1)[-1])
                    canonical = (parsed and parsed[0] in stems and parsed[1] in kinds and
                        path.rpartition('/')[0] == folder and not path.startswith('/') and
                        ':' not in path and not any(part in ('','.','..') for part in path.split('/')))
                    if canonical:return parsed[1:]
                    if mode=='image_server' and '/' not in path and parsed and parsed[0] in stems and parsed[1] in kinds:
                        current = selected.get(parsed[1:])
                        if current and current['name'].casefold()==path.casefold():
                            # Old local registrations stored only a basename. Reuse
                            # this confirmed folder entry instead of adding a second row.
                            return parsed[1:]
                    if len(ids) > 1:
                        foreign.add(id(media));hold('IDENTITY','registered_id_ambiguous',row_slot(media),os.path.basename(path));return None
                    if len(ids) == 1:
                        parsed = cls._reconcile_file_slot(ids[0]['name'])
                        if parsed and parsed[0] in stems and parsed[1] in kinds:return parsed[1:]
                        foreign.add(id(media));hold('IDENTITY','registered_id_other_file',row_slot(media),os.path.basename(path));return None
                    parsed = cls._reconcile_file_slot(path.rsplit('/',1)[-1])
                    if (parsed and parsed[0] in stems and parsed[1] in kinds and
                            path.rpartition('/')[0] == folder and not path.startswith('/') and
                            ':' not in path and not any(part in ('','.','..') for part in path.split('/'))):return parsed[1:]
                    if media.relative_path or media.gdrive_fileid:
                        hold('IDENTITY','registered_location_unverified',row_slot(media),os.path.basename(path))
                    return None
                for media in originals:
                    if media.media_type not in kinds:continue
                    slot = classify(media)
                    if slot is not None:rows.setdefault(slot,[]).append(media);slots.add(slot)
                if group['image']:slots.update({('poster',0,False),('poster',0,True),('landscape',1,False),('landscape',1,True)})
                slots.update((kind,0,False) for kind in group['videos'])
                def active_reference(value):
                    if not value or any(m.url == value and (id(m) in foreign or not active(m,mode)) for m in originals):return False
                    matching = [m for values in rows.values() for m in values if m.url == value]
                    if value in ('_p_user','_pl_user'):return bool(matching)
                    if mode == 'gdrive':return bool(cls._item_drive_id(value) and (matching or cls._item_drive_id(value) in {e['file_id'] for entries in candidates.values() for e in entries}))
                    if not str(value).startswith(server_url.rstrip('/')+'/'):return False
                    path = str(value)[len(server_url.rstrip('/'))+1:]
                    parsed = cls._reconcile_file_slot(path.rsplit('/',1)[-1])
                    return bool(parsed and parsed[0] in stems and path.rpartition('/')[0]==folder)
                for slot in sorted(slots):
                    kind,order,user = slot
                    if slot in blocked:continue
                    matching = rows.get(slot,[])
                    entry = selected.get(slot)
                    if kind in group['videos'] and mode=='gdrive' and entry is None:
                        info = extra.get('trailer_video' if kind=='trailer' else 'preview_clip')
                        if (isinstance(info,dict) and info.get('storage_type')=='gdrive' and
                                observed['by_id'].get(info.get('google_fileid'))):
                            # Legacy JSON-only registrations carry the same ID evidence.
                            hold('IDENTITY','registered_video_name_unverified',slot,os.path.basename(info.get('relative_path') or ''))
                            continue
                    result['checked'] += 1;summary['confirmed'] += 1
                    stage = 'DB_REPAIR'
                    changed = False
                    if entry:
                        exact = [m for m in matching if id(m) not in claimed and (m.relative_path and m.relative_path.casefold()==entry['path'].casefold() or mode=='gdrive' and m.gdrive_fileid==entry['file_id'])]
                        reusable = exact or [m for m in matching if id(m) not in claimed]
                        media = reusable[0] if reusable else None
                        info_key = 'trailer_video' if kind == 'trailer' else 'preview_clip'
                        current_info = extra.get(info_key)
                        info_active = isinstance(current_info,dict) and current_info.get('storage_type','local') == video_mode
                        url = cls.format_gdrive_thumbnail_url(entry['file_id']) if mode=='gdrive' else server_url.rstrip('/')+'/'+entry['path']
                        info = dict(current_info) if info_active else {}
                        if kind in group['videos']:
                            info.update(storage_type=video_mode,relative_path=entry['path'],filename=entry['name'],
                                google_fileid=entry['file_id'] if mode=='gdrive' else '',local_path='' if mode=='gdrive' else os.path.join(root,entry['path']))
                            url = MetaPreviewUtil.get_video_stream_url(info,item,kind,category)
                        values = dict(media_type=kind,sort_order=order,url=url,relative_path=entry['path'],gdrive_fileid=entry['file_id'] or None,
                            is_user=user if kind not in group['videos'] else bool(media.is_user) if media is not None else False,
                            source='user' if user else 'gdrive' if mode=='gdrive' else 'local')
                        if media is None:
                            media = MetaMedia(**values);item.media_files.append(media);summary['created'] += 1;changed = True
                        else:
                            dirty = any(getattr(media,k)!=v for k,v in values.items())
                            for k,v in values.items():setattr(media,k,v)
                            if dirty:repaired_rows.add(id(media));summary['relinked']=len(repaired_rows);changed = True
                        claimed.add(id(media))
                        for duplicate in matching:
                            if duplicate is not media and duplicate in item.media_files:
                                item.media_files.remove(duplicate);summary['removed'] += 1;changed = True
                        if kind in group['videos'] and (not current_info or info_active) and current_info != info:
                            extra[info_key] = info;changed = True
                    else:
                        for media in matching:
                            if media in item.media_files:item.media_files.remove(media);summary['removed'] += 1;changed = True
                        if kind in group['videos']:
                            info_key = 'trailer_video' if kind == 'trailer' else 'preview_clip'
                            info = extra.get(info_key)
                            if isinstance(info,dict) and info.get('storage_type','local')==video_mode:
                                path = str(info.get('relative_path') or '')
                                if path and path.rpartition('/')[0]==folder and path.rsplit('/',1)[-1].casefold() in [s+'_'+kind+'.mp4' for s in stems]+[group['videos'][kind].casefold()]:
                                    extra.pop(info_key,None);changed = True
                                elif info:hold('IDENTITY','video_location_unverified',slot)
                    result['observations'].append(dict(slot=slot_name(slot),status='confirmed_present' if entry else 'confirmed_absent',relative_path=entry['path'] if entry else ''))
                    if changed:result['changes'].append(slot_name(slot))
                    else:result['unchanged'] += 1
                if group['image']:
                    for kind,order in (('poster',0),('landscape',1)):
                        if (kind,order,True) in blocked:continue
                        entry = selected.get((kind,order,True)) or selected.get((kind,order,False))
                        if entry:
                            url = cls.format_gdrive_thumbnail_url(entry['file_id']) if mode=='gdrive' else server_url.rstrip('/')+'/'+entry['path']
                            if kind=='poster' and item.poster_url != url:item.poster_url=url;result['changes'].append('poster URL')
                            if extra.get(kind+'_url') != url:extra[kind+'_url']=url;result['changes'].append(kind+' URL')
                        elif (kind,order,False) not in blocked:
                            if kind=='poster' and active_reference(item.poster_url):item.poster_url='';result['changes'].append('poster URL')
                            if active_reference(extra.get(kind+'_url')):extra.pop(kind+'_url',None);result['changes'].append(kind+' URL')
            result['unresolved_gds'] = sum(m.source=='gds' for m in item.media_files)
            for media in item.media_files:
                if media.source=='gds':
                    slot = (media.media_type,media.sort_order or 0,bool(cls.is_user_media(media)))
                    hold('IDENTITY','unresolved_gds',slot,os.path.basename(media.relative_path or ''))
            result['changes'].extend(name+': '+str(count) for name,count in result['repair_counts'].items())
            if result['changes']:
                stage = 'COMMIT'
                if configuration != settings():raise StorageConflict('SETTINGS_CHANGED')
                allowed,message = ModuleMetaDb._schema_write_guard(category,'미디어 저장소 대조')
                if not allowed:raise StorageConflict('SCHEMA_GUARD_CHANGED')
                item.extra_info = extra
                commit_storage_record(session,item,snapshot,('media_files',))
                result.update(is_updated=True,committed=True,updated=len(result['changes']))
            pending_reason = ''
            if deferred:
                detail = result['deferred'][0]
                reason = detail['reason']
                if detail['stage']=='LIST':
                    slot = detail['slot']
                    image_folder = slot and any(g['image'] for (m,r,f),g in groups.items() if m==slot[1] and f==slot[2])
                    pending_reason = ('이미지' if image_folder else '영상') + ' 폴더 '
                    if 'permission_denied' in reason:pending_reason += '접근 권한 확인 보류'
                    elif reason=='timeout':pending_reason += '조회 시간 초과로 확인 보류'
                    elif reason in ('invalid_json','list_incomplete'):pending_reason += '목록이 불완전하여 확인 보류'
                    elif reason=='list_directory_missing':pending_reason += '없음으로 관련 등록 확인 보류'
                    elif reason in ('list_root_unverified','list_root_unconfigured','list_root_invalid'):pending_reason += '저장소 루트를 확인하지 못해 대조 보류'
                    else:pending_reason += '조회 실패로 확인 보류'
                    if slot:pending_reason += ': '+str(slot[2])
                elif reason=='unresolved_gds':pending_reason = '기존 GDS 등록의 개인 저장소 소유 여부 확인 보류'
                elif reason in ('multiple_files','duplicate_name_or_id','registered_id_ambiguous'):pending_reason = '파일 이름·식별 정보가 중복되어 연결 확인 보류'
                elif reason=='registered_id_other_file':pending_reason = '기존 등록이 다른 파일을 가리켜 연결 확인 보류'
                elif reason=='registered_video_name_unverified':pending_reason = '기존 영상 ID는 존재하나 정식 파일명이 달라 등록 보존'
                elif detail['stage']=='VALIDATE':pending_reason = '미디어 파일 또는 저장소 설정의 유효성 확인 보류'
                else:pending_reason = '미디어 경로·연결 정보 확인 보류'
                result['warnings'].append(pending_reason+' ('+str(summary['deferred'])+'건 보류)')
            result['ret'] = 'warning' if deferred else 'success'
            registration_changes = []
            if summary['created']:registration_changes.append('DB 미디어 등록 %d건 추가'%summary['created'])
            if summary['relinked']:registration_changes.append('연결 정보 %d건 보정'%summary['relinked'])
            if summary['removed']:registration_changes.append('DB 미디어 등록 %d건 해제'%summary['removed'])
            if summary['normalized_gds']:registration_changes.append('기존 GDS 등록 %d건 정상화'%summary['normalized_gds'])
            result['msg'] = (('일부 미디어 등록 갱신 완료'+(' ('+', '.join(registration_changes)+')' if registration_changes else '')+' — ') if result['is_updated'] else '등록 변경 없음 — ') + result['warnings'][-1] if deferred else (
                '미디어 등록 정보 갱신 완료'+(': '+', '.join(registration_changes) if registration_changes else '') if result['is_updated'] else '변경 없음')
            log.info('[MediaSync] [%s] %s: %s%s',category,item.ui_code or code,
                '저장소에 해당 작품의 미디어 %d개 확인, '%(summary['regular_files']+summary['user_files']) if summary['regular_files']+summary['user_files'] else '',result['msg'])
            log.debug('[MediaSync] 진단: category=%s code=%s observed=%d regular=%d user=%d created=%d repaired=%d removed=%d gds=%d deferred=%d updated=%s',
                category,code,summary['folder_entries'],summary['regular_files'],summary['user_files'],summary['created'],summary['relinked'],summary['removed'],summary['normalized_gds'],summary['deferred'],result['is_updated'])
            if deferred:log.warning('[MediaSync] [%s] %s: %s. 확인하지 못한 등록과 파일은 보존합니다.',category,item.ui_code or code,pending_reason)
            if deferred:log.debug('[MediaSync] 보류 진단: category=%s code=%s deferred=%s',category,code,'; '.join(
                '%s/%s slot=%s file=%s exception=%s'%(d['stage'],d['reason'],d['slot'],d['basename'],d['exception_type'])
                for d in result['deferred'][:8]) + ('; more='+str(len(result['deferred'])-8) if len(result['deferred'])>8 else ''))
            return result
        except Exception as exc:
            committed = session.info.get('storage_commit_state') == 'committed'
            uncertain = session.info.get('storage_commit_state') == 'unknown'
            if not committed:
                try:session.rollback()
                except Exception:uncertain = True
                for name in ('created','relinked','removed','normalized_gds'):summary[name]=0
            log.debug('[MediaSync] 실패 진단: 단계=%s, 분류=%s, 코드=%s, 예외형=%s',stage,category,code,type(exc).__name__)
            log.exception('[MediaSync] [%s] %s: 미디어 등록 반영을 완료하지 못해 추가 확인이 필요합니다.',category,code)
            result.update(ret='warning' if committed or uncertain else 'error',is_updated=committed,committed=committed,uncertain=uncertain,
                updated=len(result['changes']) if committed else 0,repair_counts=result['repair_counts'] if committed else {},
                changes=result['changes'] if committed else [],unresolved_gds=result.get('unresolved_gds') if committed else None if uncertain else original_gds,
                msg='DB 저장 완료 — 후속 확인 필요' if committed else 'DB 반영 결과 확인 필요' if uncertain else '저장소 대조 또는 DB 등록 변경 보류')
            return result
        finally:
            try:session.close()
            except Exception:
                result['warnings'].append('DB 연결 정리 확인 필요');result['ret']='warning'
                log.exception('[MediaSync] [%s] %s: DB 연결 정리를 완료하지 못했습니다.',category,code)


    @staticmethod
    def media_error_warning(data):
        errors = (data.get('extra_info') or {}).get('media_errors') or {}
        labels = {'trailer': '트레일러', 'preview': '프리뷰', 'video': '영상'}
        return '; '.join(labels.get(kind, kind) + ': ' + redact_diagnostic(str(
            error.get('message') or kind) if isinstance(error, dict) else str(error))
            for kind, error in errors.items())

    @classmethod
    def refresh_image_only(cls, module, code, category, site_activity=None, storage_action=None, media_path=None):
        """미디어(포스터/커버/팬아트/트레일러) 전용 재동기화 단일 공용 로직"""
        from .mod_meta_db import ModuleMetaDb

        if storage_action in ('reuse','check'):
            return cls.reconcile_item_media_with_storage(code,category)

        cached_json = ModuleMetaDb.get_metadata(code, category=category)
        if not cached_json:
            return {'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

        # Old cached pages may still send 'check'; synchronize without prompting.
        if storage_action == 'check':
            storage_action = 'reuse'
        if storage_action not in (None, 'reuse', 'replace'):
            return {'ret': 'error', 'msg': '올바르지 않은 미디어 처리 옵션입니다.', 'is_updated': False}
        if media_path is not None and not isinstance(media_path, str):
            return {'ret': 'error', 'msg': 'media_path는 영상 파일 경로 문자열이어야 합니다.', 'is_updated': False}
        media_path = (media_path or '').strip()
        source_warning = ''
        if media_path and not os.path.isfile(media_path):
            source_warning = 'FF에서 원본 영상 파일을 확인할 수 없어 프리뷰 생성을 생략했습니다: ' + media_path
            logger.warning('[MetaUtil] %s (%s)', source_warning, code)
        operation_label = '재처리' if storage_action == 'replace' else '동기화'

        ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
        title_log = str(cached_json.get('title') or ui_code)[:35]
        logger.info(f"[{category}] {ui_code}: 미디어 {operation_label} 시작 ({'기존 미디어 재사용' if storage_action != 'replace' else '기존 미디어 교체'})")

        try:
            if hasattr(module, 'keyword_cache') and module.keyword_cache:
                module.keyword_cache.set(f"BYPASS_{code}", "1")
        except Exception:
            pass

        # for_db=True 플래그를 전달하여 프록시 래핑 없는 순수 원본 미디어 취득
        info_opts = {'skip_trans': True, 'for_db': True, 'skip_actor_image_storage': True, 'preserve_preview': True,
                     '_existing_media_item': copy.deepcopy(cached_json)}
        if media_path:
            info_opts['media_path'] = media_path
            info_opts['_existing_media_item'] = copy.deepcopy(cached_json)
        if storage_action in ('reuse', 'replace'):
            info_opts['reuse_existing_media'] = storage_action == 'reuse'
            if storage_action == 'reuse':
                info_opts['_existing_media_item'] = copy.deepcopy(cached_json)

        if site_activity is not None:
            info_opts['_batch_site_activity'] = site_activity
        try:
            fresh_media = module.info(code, keyword=ui_code, extra_opts=info_opts)
        except TypeError:
            fresh_media = module.info(code, extra_opts=info_opts)

        if not fresh_media:
            return {'ret': 'warning', 'msg': f"[{ui_code}] 원격 사이트에서 미디어 정보를 가져오지 못했습니다.", 'title_log': title_log, 'is_updated': False}

        from support_site import SiteAvBase
        media_warning = SiteAvBase.image_storage_error(fresh_media)
        SiteAvBase.sanitize_incomplete_images(fresh_media)
        video_warning = cls.media_error_warning(fresh_media)
        media_warning = '; '.join(warning for warning in (media_warning, source_warning, video_warning) if warning)
        if media_warning:
            logger.warning('[MetaUtil] 미디어 일부 실패, 메타데이터 갱신 계속 (%s): %s', code, media_warning)

        # The receipt belongs to this processing attempt, not to the cached record.
        # Validate the fresh result above before replacing any historical error flags.
        cached_extra = dict(cached_json.get('extra_info') or {})
        cached_extra.pop('storage_error', None)
        cached_extra.pop('_image_storage_state', None)
        cached_extra.pop('media_errors', None)
        fresh_state = (fresh_media.get('extra_info') or {}).get('_image_storage_state')
        if fresh_state:
            cached_extra['_image_storage_state'] = copy.deepcopy(fresh_state)
        cached_json['extra_info'] = cached_extra

        cached_json['thumb'] = fresh_media.get('thumb', [])
        cached_json['fanart'] = fresh_media.get('fanart', [])

        # fresh_media의 thumb 리스트에서 최신 포스터와 랜드스케이프를 직접 추출하여 확정 갱신
        fresh_p = next((t['value'] for t in cached_json['thumb'] if isinstance(t, dict) and t.get('aspect') == 'poster' and t.get('value')), '')
        fresh_pl = next((t['value'] for t in cached_json['thumb'] if isinstance(t, dict) and t.get('aspect') == 'landscape' and t.get('value')), '')

        if not fresh_p and not media_warning:
            cached_json['poster_url'] = ''
            cached_json['extra_info'].pop('poster_url', None)

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
            for k_ei in ['landscape_url', 'poster_url', 'trailer_video', 'preview_clip',
                         '_storage_media', '_media_replacements']:
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
            if isinstance(fresh_orig.get('fanart'), list):
                cached_json['original']['fanart'] = copy.deepcopy(fresh_orig['fanart'])
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

        completion = {}
        if not ModuleMetaDb.save_metadata(category, cached_json, _completion_receipt=completion):
            return {'ret': 'error', 'msg': '이미지 및 미디어 정보를 DB에 저장하지 못했습니다.', 'is_updated': False}
        if completion.get('cleanup_pending'):
            media_warning = '; '.join(filter(None, (media_warning, 'DB 저장 완료; 이전 미디어 파일 정리 보류')))
        # Return the persisted assets, including paths and inactive storage rows.
        cached_json = ModuleMetaDb.get_metadata(code, category=category) or cached_json
        (cached_json.get('extra_info') or {}).pop('media_errors', None)
        logger.info('[%s] [%s] 메타 DB의 이미지/미디어 정보 저장 완료%s', module.name, code,
                    ' (일부 미디어 실패)' if media_warning else '')
        return {'ret': 'success', 'msg': f"[{ui_code}] 이미지 및 미디어 {operation_label} 완료" + (' (일부 미디어 실패)' if media_warning else ''), 'media_warning': media_warning, 'cleanup_pending': bool(completion.get('cleanup_pending')), 'title_log': title_log, 'is_updated': True, 'data': cached_json}

    @classmethod
    def _site_refresh_failure(cls, code, category, reason='사이트 정보 없음'):
        result=cls.reconcile_item_media_with_storage(code,category,legacy_only=True)
        result['site_updated']=False
        result['ret']='warning' if result['ret']!='error' else 'error'
        result['msg']=reason+'; '+(result['msg'] if result.get('is_updated') else '복구할 등록 오류 없음' if not result.get('warnings') else result['msg'])
        result['detail_msg']='; '.join([reason]+result.get('changes',[])+result.get('warnings',[]))
        return result

    @classmethod
    def _explicit_site_refresh(cls, module, code, category, cached, site_activity=None, selected_code=None):
        from .mod_meta_db import ModuleMetaDb
        from support_site import SiteAvBase
        selected_code=selected_code or code
        opts={'skip_trans':False,'for_db':True,'reuse_existing_media':False,'preserve_preview':True,
              '_explicit_site_refresh':True,'_refresh_owner':{'code':code,'category':category}}
        opts['_existing_media_item']={'code':code,'category':category,
            'extra_info':{key:copy.deepcopy(value) for key,value in (cached.get('extra_info') or {}).items()
                          if key in ('preview_clip','trailer_video')},
            'media':[copy.deepcopy(media) for media in cached.get('media') or []
                     if media.get('media_type') in ('preview','trailer')]}
        if site_activity is not None:opts['_batch_site_activity']=site_activity
        try:
            if hasattr(module,'keyword_cache'):module.keyword_cache.set('BYPASS_'+selected_code,'1')
            fresh=module.info(selected_code,keyword=cached.get('ui_code') or cached.get('originaltitle') or code,extra_opts=opts)
        except Exception:
            logger.exception('[MetaUtil] 사이트 갱신 실패: %s/%s',category,code)
            return cls._site_refresh_failure(code,category,'사이트 수집 오류')
        if not isinstance(fresh,dict) or not fresh or not (fresh.get('title') or fresh.get('plot') or fresh.get('originaltitle')):
            if isinstance(fresh,dict):
                cls.finish_media_replacements((fresh.get('extra_info') or {}).get('_media_replacements'),False)
            return cls._site_refresh_failure(code,category)
        if fresh.get('code')!=selected_code or fresh.get('category',category)!=category:
            cls.finish_media_replacements((fresh.get('extra_info') or {}).get('_media_replacements'),False)
            return cls._site_refresh_failure(code,category,'사이트 작품 식별 불일치')
        warning=SiteAvBase.image_storage_error(fresh)
        SiteAvBase.sanitize_incomplete_images(fresh)
        warning='; '.join(filter(None,(warning,cls.media_error_warning(fresh))))
        completion={}
        saved=ModuleMetaDb.save_metadata(category,fresh,_completion_receipt=completion,
            _refresh_owner={'code':code,'category':category})
        if not saved:
            return dict(ret='error',is_updated=False,site_updated=False,uncertain=completion.get('commit_state')=='unknown',
                        msg='최신 메타데이터 저장 실패',warnings=completion.get('warnings',[]))
        fresh = copy.deepcopy(fresh)
        (fresh.get('extra_info') or {}).pop('media_errors', None)
        return dict(ret='success',is_updated=True,committed=True,site_updated=True,changes=['사이트 메타 갱신: 1'],
                    media_warning=warning,cleanup_pending=bool(completion.get('cleanup_pending')),title_log=fresh.get('title',''),
                    warnings=completion.get('warnings',[]),unresolved_gds=completion.get('unresolved_gds',0),
                    msg='최신 사이트 메타데이터로 갱신 완료'+(' (일부 미디어 실패)' if warning else '')+
                        ('; 소유 미확인 GDS 등록 '+str(completion['unresolved_gds'])+'개 원행 보존' if completion.get('unresolved_gds') else ''),data=fresh)

    @classmethod
    def refresh_in_place(cls, module, code, category, site_activity=None):
        """현재 출처 사이트 정보 제자리 갱신 단일 공용 로직"""
        from .mod_meta_db import ModuleMetaDb

        cached_json = ModuleMetaDb.get_metadata(code, category=category)
        if not cached_json:
            return {'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

        return cls._explicit_site_refresh(module,code,category,cached_json,site_activity)

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
        try:
            search_res = module.search(ui_code, manual=False, use_db=False)
        except Exception:
            logger.exception('[MetaUtil] 자동 재검색 실패: %s/%s',category,code)
            return cls._site_refresh_failure(code,category,'사이트 검색 오류')
        if not search_res:
            return cls._site_refresh_failure(code,category,'사이트 검색 결과 없음')

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
            return cls._site_refresh_failure(code,category,'일치하는 사이트 정보 없음')

        new_code = best_item['code']
        logger.info(f"[{module.name}] 자동 재검색 채택: [{best_item.get('site_key', '').upper()}] Code: {new_code} (기존: {code})")

        return cls._explicit_site_refresh(module,code,category,cached_json,site_activity,new_code)

    @classmethod
    def heal_metadata(cls, module, code, category, site_activity=None):
        """Explicit fresh-site replacement, or media-registration repair when offline."""
        from .mod_meta_db import ModuleMetaDb
        cached=ModuleMetaDb.get_metadata(code,category=category)
        if not cached:return dict(ret='error',is_updated=False,msg='DB에서 해당 항목을 찾을 수 없습니다.')
        return cls._explicit_site_refresh(module,code,category,cached,site_activity)

    @classmethod
    def repair_metadata_db(cls, code, category, memory_index=None):
        """기존 DB 필드 복구. 장르 번역은 유지하고 메타 사이트·미디어 조회는 하지 않는다."""
        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaItemTagMap
        from sqlalchemy.orm import lazyload, selectinload

        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess:
            return {'ret': 'error', 'msg': '세션 획득 실패', 'is_updated': False}

        try:
            item = sess.query(MetaItem).options(
                lazyload('*'),
                selectinload(MetaItem.tag_maps).joinedload(MetaItemTagMap.tag),
            ).filter_by(code=code, category=std_cat).first()
            if not item:
                return {'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

            ui_code = item.ui_code or item.originaltitle or item.code
            title_for_log = str(item.title or ui_code).strip()
            if len(title_for_log) > 40:
                title_for_log = title_for_log[:37] + '...'

            changes = []
            is_modified = False

            extra_info = dict(item.extra_info or {})
            actors_list = copy.deepcopy(extra_info.get('_actors') or extra_info.get('actor_cache') or [])

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
                    if target_idx and (not act.get('actor_idx') or not act.get('person_idx')):
                        if not act.get('actor_idx'):act['actor_idx'] = curr_idx or target_idx
                        if not act.get('person_idx'):act['person_idx'] = curr_idx or target_idx
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
            if std_cat == 'WESTERN' and (not str(item.ui_code or '').strip() or item.ui_code == item.code):
                original_title = str(item.originaltitle or '').strip()
                if original_title and original_title != item.code and not re.fullmatch(r'W(?:SS|PS|PM)_.+', original_title):
                    item.ui_code = original_title
                    changes.append('Western UI Code 복구');is_modified = True

            if std_cat == 'JAV_CEN' and item.site == 'dmm' and not str(item.content_type or '').strip():
                texts = [item.title,item.originaltitle,item.tagline,raw_original.get('title'),raw_original.get('tagline')]
                for key in ('genre','tag'):
                    values = raw_original.get(key)
                    if isinstance(values,list):texts.extend(v for v in values if isinstance(v,str))
                for relation in item.tag_maps:
                    if relation.tag:texts.extend((relation.tag.name,relation.tag.name_org))
                evidence = ' '.join(str(v) for v in texts if v)
                vr = bool(re.search(r'(?i)(?<![A-Za-z0-9])VR(?![A-Za-z0-9])|バーチャルリアリティ|가상현실',evidence))
                url = urlparse(str(extra_info.get('info_url') or ''))
                recovered = ''
                if url.hostname == 'video.dmm.co.jp':
                    if url.path.startswith('/amateur/content/'):recovered = 'amateur'
                    elif url.path.startswith('/av/content/'):recovered = 'vr' if vr else 'videoa'
                    elif url.path.startswith('/mono/dvd/'):recovered = 'dvd'
                elif url.hostname == 'www.dmm.co.jp' and url.path.startswith('/mono/dvd/'):
                    recovered = 'dvd'
                if not recovered:
                    # Only saved item-type fields are evidence; extras[].content_type
                    # describes trailers and is deliberately never inspected here.
                    saved_type = str(raw_original.get('content_type') or extra_info.get('content_type') or '').strip().lower()
                    if saved_type in ('amateur','videoa','vr','dvd','bluray'):
                        recovered = 'dvd' if saved_type in ('dvd','bluray') else 'vr' if saved_type=='videoa' and vr else saved_type
                    elif re.search(r'(?i)(?<![A-Za-z])(?:DVD|Blu[- ]?ray)(?![A-Za-z])',evidence):recovered = 'dvd'
                    elif re.search(r'配信限定|ダウンロード|(?i:Digital Download|Streaming)',evidence):recovered = 'vr' if vr else 'videoa'
                if recovered:
                    item.content_type = recovered;changes.append('DMM 작품 타입 복구: '+recovered);is_modified = True

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
                        if '장르 번역 최신화' not in changes:changes.append('장르 번역 최신화')
                        is_modified = True

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

                    if '장르 번역 최신화' not in changes:changes.append('장르 번역 최신화')
                    is_modified = True

            if is_modified:
                item.updated_time = datetime.now()
                sess.commit()
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
            logger.error(f"[MetaUtil] 메타 DB 복구 오류 ({code}): {e}")
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
        from sqlalchemy.orm import lazyload
        from .util_runtime import stage_storage_record, commit_storage_record, batch_stage

        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess:
            return {'ret': 'error', 'error_code': 'database_unavailable', 'msg': '세션 획득 실패', 'is_updated': False}

        file_operations = []
        files_committed = False
        result = {'ret': 'success', 'is_updated': False, 'changes': [], 'failures': []}
        sess.info['storage_commit_state'] = 'not_attempted'
        pending_gdrive_images = {}
        image_observations = {}
        prepared_image_results = {}
        defer_gdrive_images = False
        try:
            with batch_stage(logger, 'DB read', code):
                item = sess.query(MetaItem).options(lazyload('*')).filter_by(code=code, category=std_cat).first()
            if not item:
                return {'ret': 'error', 'error_code': 'not_found', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.', 'is_updated': False}

            snapshot = stage_storage_record(sess, item, ('media_files',))
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
            failure_details = []
            tmp_dir = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)

            synced_image_paths = {}

            def sync_single_image_asset(source_url, filename_base, media_type, current_url="", sort_idx=0):
                if any(op.get('staged') and op.get('status') not in ('generated', 'removed') for op in file_operations):
                    raise ValueError('현재 작품의 앞선 미디어 교체가 실패하여 후속 처리를 중단합니다.')
                if (media_type, sort_idx) in prepared_image_results:
                    return prepared_image_results[(media_type, sort_idx)]
                operation_start = len(file_operations)
                try:
                    with batch_stage(logger, 'image ' + media_type, code):
                        outcome = perform_image_sync(source_url, filename_base, media_type, current_url, sort_idx)
                    if any(op.get('staged') and op.get('status') not in ('generated', 'removed') for op in file_operations[operation_start:]):
                        raise ValueError('현재 작품의 이미지 교체가 완료되지 않았습니다: ' + media_type)
                    return outcome
                except Exception as exc:
                    logger.exception('[MetaUtil] %s 이미지 처리 실패 (%s)', media_type, code)
                    failure_details.append({'asset': media_type, 'error_code': getattr(exc, 'error_code', 'image_processing_failed')})
                    if any(op.get('staged') or op.get('stage_started') for op in file_operations):
                        raise
                    return None, False
                finally:
                    cls.finish_media_replacements([op for op in file_operations[operation_start:] if op['status'] != 'generated'], False)

            def perform_image_sync(source_url, filename_base, media_type, current_url="", sort_idx=0):
                operation = None
                def image_result(url, changed):
                    if url:
                        if image_mode == 'gdrive':
                            match = re.search(r'[?&]id=([^&]+)', str(url))
                            fid = match.group(1) if match else ''
                            synced_image_paths[url] = MetaPreviewUtil.get_recorded_remote_path(fid, dest_rel, category=std_cat, is_image=True)
                        else:
                            synced_image_paths[url] = dest_rel
                        if operation is not None:
                            from .util_preview import MediaReplacement
                            MediaReplacement.generated(operation, fid if image_mode == 'gdrive' else '')
                    return url, changed

                target_filename = f"{filename_base}.jpg"
                dest_rel = f"{rel_dir}/{target_filename}".strip('/\\')
                local_dest = os.path.join(target_folder, target_filename)
                expected_url = f"{server_url_prefix}/{target_filename}"
                if image_mode == 'image_server':
                    root = P.ModelSetting.get('jav_censored_image_server_local_path') or '/data/images'
                    server = str(P.ModelSetting.get('jav_censored_image_server_url') or f"{F.SystemModelSetting.get('ddns')}/images").rstrip('/')
                    local_dest = os.path.join(root, dest_rel.replace('/', os.path.sep))
                    expected_url = f'{server}/{dest_rel}'

                if image_mode in ('gdrive', 'image_server'):
                    from .util_preview import MediaReplacement
                    remote_target = MetaPreviewUtil.build_rclone_target_remote_path(
                        P.ModelSetting.get('jav_censored_image_gdrive_remote_path'), dest_rel) if image_mode == 'gdrive' else local_dest

                # 개인 저장소(구글 드라이브 / 로컬 디스크)에 실물 파일이 존재하는지 우선 검사
                if image_mode == 'gdrive':
                    initial=cls.image_initial_observation(dest_rel,std_cat,image_observations)
                    fid=next((row['ID'] for row in initial['files'] if row['Name'].casefold()==target_filename.casefold()),'')
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

                if image_mode in ('gdrive', 'image_server'):
                    from .util_preview import MediaReplacement
                    operation = cls.prepare_storage_replacement(code, std_cat, image_mode, dest_rel, media_type, sort_idx,
                        observation=image_observations if image_mode == 'gdrive' else None)
                    if operation is not None:
                        file_operations.append(operation)
                        if skip_existing and operation['had_original']:
                            # An earlier soft/cache lookup was inconclusive. Do not overwrite.
                            return None, False
                        MediaReplacement.stage(operation)

                # Rebuilding TPDB movie covers must keep the same front/back layout as initial scraping.
                original_thumb = (getattr(item, 'original', None) or {}).get('thumb') or {}
                back_cover = original_thumb.get('back_cover')
                if media_type == 'landscape' and getattr(item, 'site', '') == 'tpdb' and back_cover and source_url:
                    from support_site.site_av.site_tpdb import SiteTpdb
                    merged = SiteTpdb._merge_covers(back_cover, source_url)
                    if merged:
                        try:
                            if image_mode == 'gdrive':
                                if defer_gdrive_images:
                                    fd, queued = tempfile.mkstemp(prefix='sync_cover_', suffix='.jpg', dir=tmp_dir)
                                    os.close(fd)
                                    shutil.copyfile(merged, queued)
                                    pending_gdrive_images[dest_rel] = (queued, media_type, sort_idx)
                                    return None, False
                                file_id = cls.upload_image_to_gdrive(merged, dest_rel, category=std_cat, overwrite=not skip_existing,replacements=file_operations)
                                if file_id:
                                    MetaPreviewUtil.register_remote_file(dest_rel, file_id, category=std_cat, is_image=True)
                                    return image_result(cls.format_gdrive_thumbnail_url(file_id), True)
                                return None, False
                            if image_mode == 'image_server':
                                SiteAvBase.publish_image_file(merged, local_dest,replacement=operation)
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
                            if defer_gdrive_images:
                                pending_gdrive_images[dest_rel] = (tmp_file, media_type, sort_idx)
                                return None, False
                            uploaded_fid = cls.upload_image_to_gdrive(tmp_file, dest_rel, category=std_cat, overwrite=not skip_existing,replacements=file_operations)
                            if uploaded_fid:
                                MetaPreviewUtil.register_remote_file(dest_rel, uploaded_fid, category=std_cat, is_image=True)
                                return image_result(cls.format_gdrive_thumbnail_url(uploaded_fid), True)
                    finally:
                        if os.path.exists(tmp_file) and not any(value[0] == tmp_file for value in pending_gdrive_images.values()):
                            try: os.remove(tmp_file)
                            except Exception: pass
                    logger.warning('[MetaUtil] [%s/%s] %s 이미지 다운로드/처리 실패: %s (HTTP %s)', std_cat, code, media_type, dest_rel, getattr(resp, 'status_code', None))
                    return None, False

                elif image_mode == 'image_server':
                    os.makedirs(os.path.dirname(local_dest), exist_ok=True)
                    resp = SiteAvBase.get_response(source_url, timeout=15)
                    if resp and resp.status_code == 200:
                        if not SiteAvBase._save_image_data(BytesIO(resp.content), local_dest,replacement=operation):
                            img_obj = SiteAvBase.imopen(source_url)
                            if img_obj:
                                cls.save_normalized_jpeg(img_obj, local_dest,replacement=operation)
                                img_obj.close()
                            else:
                                return None, False
                        if not os.path.isfile(local_dest) or os.path.getsize(local_dest) == 0:
                            return None, False
                        MetaPreviewUtil.register_local_file(local_dest)
                        return image_result(expected_url, True)
                    logger.warning('[MetaUtil] [%s/%s] %s 이미지 다운로드 실패: %s (HTTP %s)', std_cat, code, media_type, dest_rel, getattr(resp, 'status_code', None))
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
            adopted = cls.adopt_item_user_images(item, image_mode, rel_dir, stem, selected, use_dir_cache=use_dir_cache, observations=image_observations)
            initial=image_observations.get(rel_dir)
            observed=[dict(name=row['Name'],path=rel_dir+'/'+row['Name'],file_id=row['ID'],valid=True) for row in initial['files']] if initial else None
            repairs=cls.repair_item_media_registration(item,image_mode,P.ModelSetting.get('jav_censored_image_gdrive_remote_path') or '',
                rel_dir,observed,dict(item.extra_info or {}),server_url_prefix.rsplit('/',1)[0])
            if repairs:
                changes.extend(name+': '+str(count) for name,count in repairs.items());is_modified=True
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

            if image_mode == 'gdrive':
                # Prepare all selected images before opening the upload connection.
                # Reused/user files stay out of the transfer directory entirely.
                defer_gdrive_images = True
                try:
                    slots=[('poster',sync_poster,0,'p'),('landscape',sync_landscape,1,'pl')]
                    if sync_fanart:
                        slots.extend(('fanart',True,i,'art_'+str(i+1)) for i in range(min(
                            int(P.ModelSetting.get('jav_censored_art_count') or 0),len(orig_data.get('fanart') or []))))
                    for kind, enabled, order, suffix in slots:
                        if not enabled:
                            continue
                        media = next((m for m in sorted(item.media_files, key=cls.is_user_media, reverse=True)
                                      if m.media_type == kind and (kind!='fanart' or (m.sort_order or 0)==order) and cls.media_in_storage(m, image_mode)), None)
                        if media is not None and cls.is_user_media(media):
                            continue
                        current = (item.poster_url or '') if kind == 'poster' else (media.url if media else extra_info.get('landscape_url', '') if kind=='landscape' else '')
                        raw=(orig_data.get('fanart') or [])[order] if kind=='fanart' else orig_thumb.get(kind)
                        source = official_source(kind, raw, current)
                        try:
                            prepared_image_results[(kind, order)] = perform_image_sync(source, f'{stem}_{suffix}', kind, current, order)
                        except Exception as exc:
                            logger.exception('[MetaUtil] %s 이미지 준비 실패 (%s)', kind, code)
                            failure_details.append({'asset': kind, 'error_code': getattr(exc, 'error_code', 'image_processing_failed')})
                            prepared_image_results[(kind, order)] = (None, False)
                finally:
                    defer_gdrive_images = False
                if pending_gdrive_images:
                    uploaded = cls.upload_images_to_gdrive({path: value[0] for path, value in pending_gdrive_images.items()},
                        category=std_cat, overwrite=not skip_existing, replacements=file_operations, code=code)
                    for relative, file_id in uploaded.items():
                        _, kind, order = pending_gdrive_images[relative]
                        url = cls.format_gdrive_thumbnail_url(file_id)
                        synced_image_paths[url] = relative
                        prepared_image_results[(kind, order)] = (url, True)
                cls.finish_media_replacements([entry for entry in file_operations if entry['status'] != 'generated'], False)

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
                                if p_media.source != ('gdrive' if image_mode == 'gdrive' else 'site'):
                                    p_media.source = 'gdrive' if image_mode == 'gdrive' else 'site'
                                    is_modified = True
                                if p_media.url != new_p_url:
                                    p_media.url = new_p_url
                                    is_modified = True
                                    p_db_changed = True
                            else:
                                item.media_files.append(MetaMedia(
                                    media_type='poster', url=new_p_url, is_user=False, sort_order=0,
                                    source='gdrive' if image_mode == 'gdrive' else 'site'
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
                                if current_pl_media.source != ('gdrive' if image_mode == 'gdrive' else 'site'):
                                    current_pl_media.source = 'gdrive' if image_mode == 'gdrive' else 'site'
                                    is_modified = True
                                if current_pl_media.url != new_pl_url:
                                    current_pl_media.url = new_pl_url
                                    is_modified = True
                                    pl_db_changed = True
                            else:
                                item.media_files.append(MetaMedia(
                                    media_type='landscape', url=new_pl_url, is_user=False, sort_order=1,
                                    source='gdrive' if image_mode == 'gdrive' else 'site'
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
                                    if target_art_m.source != ('gdrive' if image_mode == 'gdrive' else 'site'):
                                        target_art_m.source = 'gdrive' if image_mode == 'gdrive' else 'site'
                                        is_modified = True
                                    if target_art_m.url != new_fa_url:
                                        target_art_m.url = new_fa_url
                                        is_modified = True
                                else:
                                    item.media_files.append(MetaMedia(
                                        media_type='fanart', url=new_fa_url, is_user=False, sort_order=fa_idx,
                                        source='gdrive' if image_mode == 'gdrive' else 'site'
                                    ))
                                    is_modified = True
                                if fa_changed:
                                    is_modified = True
                                    synced_arts_count += 1

                        if synced_arts_count > 0:
                            changes.append(f'팬아트 {synced_arts_count}장 동기화')

            if any(op.get('staged') and op.get('status') not in ('generated', 'removed') for op in file_operations):
                raise ValueError('현재 작품의 미디어 교체 실패로 후속 처리와 부분 저장을 중단합니다.')
            if failures:
                logger.warning('[MetaUtil] 일부 이미지 실패, 성공한 미디어만 DB 반영 (%s): %s', code, ', '.join(failures))

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
                    from .util_preview import MediaReplacement
                    target = MetaPreviewUtil.build_rclone_target_remote_path(
                        MetaPreviewUtil.get_setting('preview_gdrive_remote_path', std_cat, default='') or
                        MetaPreviewUtil.get_setting('preview_video_folder_id', std_cat, default=''), dest_rel_tr)
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
                    from .util_preview import MediaReplacement

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
                    with batch_stage(logger, 'trailer download/upload', code):
                        t_succ, t_res = MetaPreviewUtil.process_trailer_download_workflow(
                            code,
                            raw_tr_url,
                            category=std_cat,
                            force=not skip_existing,
                            item_dict=trailer_item,
                            use_dir_cache=use_dir_cache
                        )
                    file_operations.extend(trailer_item['extra_info'].pop('_media_replacements', []))
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

            if any(op.get('staged') and op.get('status') not in ('generated', 'removed') for op in file_operations):
                raise ValueError('현재 작품의 미디어 교체 실패로 부분 저장을 중단합니다.')
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
                with batch_stage(logger, 'DB commit', code):
                    for operation in file_operations:
                        if operation.get('image_contract') and operation.get('new_checksum'):
                            for media in item.media_files:
                                if media.relative_path==operation['relative_path'] and media.media_type==operation['media_type']:
                                    media.checksum=operation['new_checksum'];media.updated_time=datetime.now()
                    commit_storage_record(sess, item, snapshot, ('media_files',))
                files_committed = True

            detail_msg = ", ".join(changes) if changes else "변경 없음"
            if failures:
                detail_msg += " (미디어 실패: " + ", ".join(failures) + ")"
            result.update({
                'ret': 'success',
                'is_updated': is_modified,
                'title_log': title_for_log,
                'detail_msg': detail_msg,
                'changes': changes,
                'failures': failures,
                'failure_details': failure_details,
                'commit_state': 'committed' if files_committed else 'not_attempted'
            })
            result['repair_counts']=repairs
            result['unresolved_gds']=sum(media.source=='gds' for media in item.media_files)
            if result['unresolved_gds']:
                result['ret']='warning'
                result['warnings']=['GDS 출처 미확인 등록 '+str(result['unresolved_gds'])+'개: 원행 보존']
            return result

        except Exception as exc:
            state = sess.info.get('storage_commit_state', 'not_attempted')
            if state == 'committed':
                files_committed = True
                result.update(ret='success', is_updated=True, changes=changes, failures=failures,
                              commit_state='committed', cleanup_pending=True, warnings=['post_commit_error'])
                return result
            try:
                sess.rollback()
            except Exception:
                logger.exception('[MetaUtil] storage rollback 실패 (%s)', code)
            logger.exception('[MetaUtil] sync_item_media_to_storage 실패 (%s)', code)
            error_code = ('commit_outcome_unknown' if state == 'unknown' else
                          getattr(exc, 'error_code', 'storage_conflict' if isinstance(exc, FileExistsError) else 'storage_sync_failed'))
            result.update(ret='error', error_code=error_code,
                          msg='저장소 처리 결과를 확인할 수 없습니다.' if state == 'unknown' else '저장소 동기화에 실패했습니다.',
                          is_updated=False, commit_state=state)
            return result
        finally:
            state = sess.info.get('storage_commit_state', 'not_attempted')
            warnings = cls.finish_media_replacements(file_operations, True if files_committed else None if state == 'unknown' else False)
            for temporary, _, _ in pending_gdrive_images.values():
                try:
                    if os.path.exists(temporary):
                        os.remove(temporary)
                except Exception:
                    logger.exception('[MetaUtil] storage 임시 파일 정리 실패 (%s)', code)
                    warnings.append('temporary_cleanup_failed')
            try:
                sess.remove()
            except Exception:
                logger.exception('[MetaUtil] storage 세션 정리 실패 (%s)', code)
                warnings.append('session_cleanup_failed')
            if warnings:
                result['warnings'] = list(dict.fromkeys(result.get('warnings', []) + warnings))
                result['cleanup_pending'] = True


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
        from .util_runtime import batch_database_limits
        from .mod_meta_db import ModuleMetaDb
        try:
            with batch_database_limits(logger):
                return cls._run_legacy_batch(category, kind, options)
        finally:
            ModuleMetaDb.checkpoint_wal()

    @classmethod
    def _run_legacy_batch(cls, category, kind, options):
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
        from .util_runtime import cooperate
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
                    cooperate()
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
        from .util_runtime import cooperate
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
                    cooperate()
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
