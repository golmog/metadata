# -*- coding: utf-8 -*-
import os
import re
import json
import base64
import traceback
import time
import copy
from datetime import datetime
from io import BytesIO
from PIL import Image
from urllib.parse import urlparse

from .setup import *


class MetaImageUtil:
    """
    메타데이터 이미지 가공, 크롭, 24비트 정규화 JPEG 저장 및 이미지 서버 디스크 관리 유틸리티
    """

    @classmethod
    def get_server_folder_and_prefix(cls, domain, category, stem, studio="", year=1900):
        """
        카테고리 및 도메인 규칙에 따라 실제 로컬 저장 폴더 경로와 서빙 URL Prefix를 산출합니다.
        SiteAvBase의 단일 경로 결정 엔진으로 일원화하여 위임합니다.
        """
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
        """
        Plex 및 모든 미디어 서버 호환성을 보장하기 위해
        모든 이미지를 24비트 표준 RGB JPEG로 무손실/고화질 정규화 변환하여 저장합니다.
        """
        try:
            logger.debug(f"[MetaImageUtil] JPEG 정규화 저장 시작 -> 대상: '{save_filepath}', 모드: {pil_img.mode}, 크기: {pil_img.size}")
            if pil_img.mode not in ('RGB', 'L'):
                rgb_converted = pil_img.convert('RGB')
                rgb_converted.save(save_filepath, 'JPEG', quality=95, optimize=True)
                rgb_converted.close()
            else:
                pil_img.save(save_filepath, 'JPEG', quality=95, optimize=True)
            
            if os.path.exists(save_filepath):
                file_size = os.path.getsize(save_filepath)
                logger.debug(f"[MetaImageUtil] 디스크 파일 쓰기 완료 -> '{save_filepath}' (크기: {file_size:,} bytes)")
            else:
                logger.error(f"[MetaImageUtil] 디스크 파일 쓰기 실패 (파일 없음) -> '{save_filepath}'")
        except Exception as e_save_norm:
            logger.error(f"[MetaImageUtil] save_normalized_jpeg 파일 저장 중 예외 발생 ('{save_filepath}'): {e_save_norm}")
            logger.error(traceback.format_exc())
            raise

    @classmethod
    def save_user_cropped_poster(cls, code, crop_data_or_base64, pl_image_base64_data=None, p_image_base64_data=None, category=None):
        if not category:
            logger.error(f"[MetaImageUtil] save_user_cropped_poster: category 인자가 지정되지 않았습니다. code='{code}'")
            return False, '카테고리가 지정되지 않았습니다.'

        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia

        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess:
            return False, f"유효하지 않은 카테고리입니다: '{category}'"

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

            # 1. 대상 폴더 및 URL Prefix 산출
            target_folder, server_url_prefix = cls.get_server_folder_and_prefix(
                domain, std_cat, stem, studio=studio, year=year
            )

            if not target_folder or not server_url_prefix:
                return False, '이미지 서버 로컬 경로 또는 URL 설정이 비어있습니다.'

            os.makedirs(target_folder, exist_ok=True)

            # 2. 소스 이미지 타입 및 원본 데이터 로드
            src_img = None
            source_type = 'pl'

            try:
                if isinstance(crop_data_or_base64, str) and crop_data_or_base64.startswith('{'):
                    crop_info_tmp = json.loads(crop_data_or_base64)
                    if isinstance(crop_info_tmp, dict) and crop_info_tmp.get('source_type'):
                        source_type = str(crop_info_tmp['source_type']).lower()
            except:
                pass

            # (Case 1) 사용자가 세로 포스터(P)를 직접 업로드한 경우
            if p_image_base64_data:
                raw_b64 = p_image_base64_data.split(',', 1)[1] if ',' in p_image_base64_data else p_image_base64_data
                src_img = Image.open(BytesIO(base64.b64decode(raw_b64)))

            # (Case 2) 사용자가 가로 커버(PL)를 직접 업로드한 경우
            elif pl_image_base64_data:
                raw_b64 = pl_image_base64_data.split(',', 1)[1] if ',' in pl_image_base64_data else pl_image_base64_data
                src_img = Image.open(BytesIO(base64.b64decode(raw_b64)))
                user_pl_path = os.path.join(target_folder, f"{stem}_pl_user.jpg")
                cls.save_normalized_jpeg(src_img, user_pl_path)

                # 기존 시스템 _pl 파일 정리
                for ext_cand in ['jpg', 'jpeg', 'png', 'webp']:
                    old_pl = os.path.join(target_folder, f"{stem}_pl.{ext_cand}")
                    if os.path.exists(old_pl):
                        try: os.remove(old_pl)
                        except: pass

            # (Case 3) 소스가 세로 포스터(P)로 선택된 경우
            elif source_type == 'p':
                for cand in [f"{stem}_p_user.jpg", f"{stem}_p.jpg", f"{stem}_p.png", f"{stem}_p.webp"]:
                    cp = os.path.join(target_folder, cand)
                    if os.path.exists(cp):
                        src_img = Image.open(cp)
                        break
                if src_img is None and item.poster_url and item.poster_url.startswith('http'):
                    from support_site import SiteAvBase
                    src_img = SiteAvBase.imopen(item.poster_url)

            # (Case 4) 소스가 가로 커버(PL)인 경우 (기본값)
            else:
                for cand in [f"{stem}_pl_user.jpg", f"{stem}_pl.jpg", f"{stem}_pl.png", f"{stem}_pl.webp"]:
                    cp = os.path.join(target_folder, cand)
                    if os.path.exists(cp):
                        src_img = Image.open(cp)
                        break

            # (Case 5) 디스크에 없으면 원격 URL에서 로드
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

            # 3. 정밀 좌표 기반 무손실 회전 및 크롭 연산
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
                logger.debug(f"[MetaImageUtil] 크롭 좌표 파싱 실패 (원본 사용): {e_parse}")

            if cropped_p_img is None:
                cropped_p_img = src_img

            # 4. _p_user.jpg로 정규화 저장 및 기존 시스템 _p 파일 정리
            user_poster_path = os.path.join(target_folder, f"{stem}_p_user.jpg")
            cls.save_normalized_jpeg(cropped_p_img, user_poster_path)
            cropped_p_img.close()
            src_img.close()

            for ext_cand in ['jpg', 'jpeg', 'png', 'webp']:
                old_p = os.path.join(target_folder, f"{stem}_p.{ext_cand}")
                if os.path.exists(old_p):
                    try: os.remove(old_p)
                    except: pass

            # 5. DB 메타데이터 및 미디어 테이블 동기화
            new_poster_url = f"{server_url_prefix}/{stem}_p_user.jpg"
            item.poster_url = new_poster_url

            p_media = next((m for m in item.media_files if m.media_type == 'poster'), None)
            if p_media:
                p_media.url = new_poster_url
                p_media.is_user = True
            else:
                item.media_files.append(MetaMedia(media_type="poster", url=new_poster_url, is_user=True, sort_order=0))

            if pl_image_base64_data:
                new_pl_url = f"{server_url_prefix}/{stem}_pl_user.jpg"
                pl_media = next((m for m in item.media_files if m.media_type == 'landscape'), None)
                if pl_media:
                    pl_media.url = new_pl_url
                    pl_media.is_user = True
                else:
                    item.media_files.append(MetaMedia(media_type="landscape", url=new_pl_url, is_user=True, sort_order=1))

            item.updated_time = datetime.now()
            sess.commit()
            ModuleMetaDb.checkpoint_wal()

            logger.info(f"[MetaImageUtil] 유저 포스터 저장 성공: [{std_cat}] {item.code} ➔ {new_poster_url}")
            return True, new_poster_url

        except Exception as e:
            logger.error(f"[MetaImageUtil] save_user_cropped_poster 오류 ({code}): {e}")
            logger.error(traceback.format_exc())
            sess.rollback()
            return False, str(e)
        finally:
            sess.remove()


    # 인물 프로필 사진 크롭/업로드 및 _user.jpg 저장
    @classmethod
    def save_user_cropped_person_image(cls, person_identifier, crop_data_or_base64, image_base64_data=None, image_url=None, domain='JAV'):
        """인물 프로필 사진을 크롭/정규화하여 이미지 서버 폴더에 _user.jpg로 저장하고 MetaPerson을 갱신합니다."""
        from .mod_meta_db import ModuleMetaDb, MetaPerson
        from support_site import SiteAvBase

        logger.info(f"[MetaImageUtil] 인물 프로필 사진 저장 프로세스 시작 -> 대상 식별자: '{person_identifier}', 도메인: '{domain}'")
        logger.debug(f"[MetaImageUtil] 수신 데이터 -> image_base64 유무: {bool(image_base64_data)}, image_url: '{image_url}', crop_data: {crop_data_or_base64[:100] if crop_data_or_base64 else 'None'}")

        sess, _, _ = ModuleMetaDb.get_session_and_domain('PERSON')
        if not sess:
            logger.error("[MetaImageUtil] 인물 DB 세션 획득에 실패했습니다.")
            return False, "인물 DB 세션 생성 실패", None

        try:
            target_str = str(person_identifier).strip()
            p_rec = None
            if target_str.isdigit():
                p_rec = sess.query(MetaPerson).filter_by(id=int(target_str)).first()
            if not p_rec and target_str:
                p_rec = sess.query(MetaPerson).filter_by(person_idx=target_str).first()
            if not p_rec:
                logger.warning(f"[MetaImageUtil] 인물 DB에서 대상을 찾을 수 없음: '{person_identifier}'")
                return False, f"대상 인물을 찾을 수 없습니다: {person_identifier}", None

            logger.info(f"[MetaImageUtil] 인물 레코드 확인 -> ID: {p_rec.id}, Code: {p_rec.person_idx}, NameKo: '{p_rec.name_ko}', NameOrg: '{p_rec.name_org}'")

            dom = (p_rec.domain or domain or 'JAV').upper()

            # 유저 설정 로컬 저장 경로 및 서버 URL 로드
            if dom == 'WESTERN':
                root_path = (
                    P.ModelSetting.get('western_image_server_local_path') or
                    P.ModelSetting.get('jav_censored_image_server_local_path') or
                    os.path.join(path_data, 'images')
                )
                actor_sub_path = (
                    P.ModelSetting.get('western_image_server_actor_path') or 
                    '/western/actors'
                ).strip('/\\')
                server_url = (
                    P.ModelSetting.get('western_image_server_url') or
                    P.ModelSetting.get('jav_censored_image_server_url') or
                    f"{F.SystemModelSetting.get('ddns')}/images"
                ).rstrip('/')
            else:
                root_path = (
                    P.ModelSetting.get('jav_censored_image_server_local_path') or 
                    os.path.join(path_data, 'images')
                )
                actor_sub_path = (
                    P.ModelSetting.get('jav_censored_image_server_actor_path') or 
                    '/jav/actors'
                ).strip('/\\')
                server_url = (
                    P.ModelSetting.get('jav_censored_image_server_url') or
                    f"{F.SystemModelSetting.get('ddns')}/images"
                ).rstrip('/')

            logger.debug(f"[MetaImageUtil] 경로 설정 확인 -> Root: '{root_path}', ActorFolder: '{actor_sub_path}', ServerURL: '{server_url}'")

            if not root_path or not server_url:
                logger.error("[MetaImageUtil] 이미지 서버 로컬 경로 또는 URL 설정이 비어있어 저장을 중단합니다.")
                return False, "이미지 서버 로컬 경로 또는 URL 설정이 비어있습니다.", None

            # 파일명 및 서브폴더 생성 (한국어명_(원문명)_PA식별자_user.jpg)
            def clean_name(n):
                return re.sub(r'\s+', '_', str(n).strip())

            kor_name = p_rec.name_ko or ''
            org_name = p_rec.name_org or ''
            eng_name = p_rec.name_en or ''
            clean_idx = clean_name(p_rec.person_idx or str(p_rec.id))

            if dom == 'WESTERN':
                base_name = eng_name or org_name or kor_name or 'Actor'
                clean_base = re.sub(r'[^\w\s가-힣-]', '', clean_name(base_name)).replace(' ', '_')
                filename_base = f"{clean_base}_{clean_idx}_user.jpg" if clean_idx else f"{clean_base}_user.jpg"
                first_char = base_name[0] if base_name else '#'
                sub_folder = SiteAvBase._get_actor_folder_name_western(first_char)
            else:
                first_char = kor_name[0] if kor_name else (org_name[0] if org_name else '#')
                sub_folder = SiteAvBase._get_actor_folder_name(first_char)
                name_part = clean_name(kor_name) or clean_name(org_name)
                if org_name and org_name != kor_name:
                    name_part += f"_({clean_name(org_name)})"

                filename_base = f"{name_part}_{clean_idx}_user.jpg" if clean_idx else f"{name_part}_user.jpg"

            relative_path = f"{actor_sub_path.strip('/')}/{sub_folder}/{filename_base}"
            target_filepath = os.path.join(root_path, relative_path.replace('/', os.path.sep))

            os.makedirs(os.path.dirname(target_filepath), exist_ok=True)
            logger.info(f"[MetaImageUtil] 최종 저장 대상 전체 경로 -> '{target_filepath}'")

            # 소스 이미지 다중 탐색 (Base64 ➔ URL ➔ Thumb ➔ MediaSrc ➔ Google Drive)
            src_img = None
            is_pre_cropped_canvas = False

            if image_base64_data:
                try:
                    logger.debug(f"[MetaImageUtil] 1차 소스 시도: 전송된 Base64 데이터 파싱 (길이: {len(image_base64_data)})")
                    raw_b64 = image_base64_data.split(',', 1)[1] if ',' in image_base64_data else image_base64_data
                    src_img = Image.open(BytesIO(base64.b64decode(raw_b64)))
                    is_pre_cropped_canvas = True
                    logger.debug(f"[MetaImageUtil] Base64 소스 로드 성공 (크기: {src_img.size}, 포맷: {src_img.format})")
                except Exception as e_b64:
                    logger.warning(f"[MetaImageUtil] Base64 디코딩 실패: {e_b64}")

            if src_img is None and image_url:
                logger.debug(f"[MetaImageUtil] 2차 소스 시도: image_url 로드 -> '{image_url}'")
                src_img = SiteAvBase.imopen(image_url)

            if src_img is None:
                current_active_thumb = ModuleMetaDb.resolve_person_active_thumb(p_rec)
                if current_active_thumb:
                    logger.debug(f"[MetaImageUtil] 3차 소스 시도: 현재 활성 썸네일 로드 -> '{current_active_thumb}'")
                    src_img = SiteAvBase.imopen(current_active_thumb)

            if src_img is None and p_rec.media_src:
                m_src = p_rec.media_src
                logger.debug(f"[MetaImageUtil] 4차 소스 시도: media_src 딕셔너리 탐색 -> {m_src}")
                for key_cand in ['site_img_url', 'local_img_path', 'google_fileid']:
                    cand_val = m_src.get(key_cand)
                    if cand_val:
                        if key_cand == 'google_fileid':
                            cand_url = f"https://drive.google.com/thumbnail?id={cand_val}"
                            src_img = SiteAvBase.imopen(cand_url)
                        else:
                            src_img = SiteAvBase.imopen(cand_val)
                        if src_img is not None:
                            logger.debug(f"[MetaImageUtil] media_src[{key_cand}] 로드 성공 -> '{cand_val}'")
                            break

            if src_img is None:
                logger.error(f"[MetaImageUtil] 모든 소스에서 원본 이미지 로드 실패 (Identifier: '{person_identifier}')")
                return False, "처리할 원본 이미지를 디스크 또는 원격지에서 찾을 수 없습니다.", None

            # 크롭 및 회전 좌표 연산 (이미 캔버스에서 잘려온 데이터가 아닐 경우에만 적용)
            cropped_img = None
            if not is_pre_cropped_canvas:
                try:
                    crop_info = json.loads(crop_data_or_base64) if isinstance(crop_data_or_base64, str) and crop_data_or_base64.startswith('{') else None
                    if crop_info and 'width' in crop_info and 'height' in crop_info:
                        rotate_angle = crop_info.get('rotate', 0)
                        working_img = src_img
                        if rotate_angle != 0:
                            logger.debug(f"[MetaImageUtil] 이미지 회전 적용: {-rotate_angle}도")
                            working_img = src_img.rotate(-rotate_angle, expand=True)

                        img_w, img_h = working_img.size
                        cx = max(0, int(round(crop_info['x'])))
                        cy = max(0, int(round(crop_info['y'])))
                        cw = min(int(round(crop_info['width'])), img_w - cx)
                        ch = min(int(round(crop_info['height'])), img_h - cy)

                        logger.debug(f"[MetaImageUtil] 이미지 크롭 적용 -> X:{cx}, Y:{cy}, W:{cw}, H:{ch} (원본 크기: {img_w}x{img_h})")
                        cropped_img = working_img.crop((cx, cy, cx + cw, cy + ch))
                except Exception as e_crop_parse:
                    logger.warning(f"[MetaImageUtil] 크롭 좌표 연산 실패: {e_crop_parse}")

            if cropped_img is None:
                cropped_img = src_img

            # 디스크에 24비트 RGB JPEG 정규화 저장 실행
            logger.info(f"[MetaImageUtil] 디스크 파일 저장 실행 -> '{target_filepath}'")
            cls.save_normalized_jpeg(cropped_img, target_filepath)
            cropped_img.close()
            src_img.close()

            # DB 및 미디어 소스 갱신 (MetaPerson ORM 스키마 규격 준수)
            new_server_url = f"{server_url}/{relative_path.lstrip('/')}"
            pure_sub_rel = f"{sub_folder}/{filename_base}"

            media_dict = copy.deepcopy(p_rec.media_src or {})
            media_dict['local_img_path'] = pure_sub_rel
            media_dict['local_img_url'] = new_server_url
            p_rec.media_src = media_dict

            extra_dict = copy.deepcopy(p_rec.extra_info or {})
            extra_dict['local_img_url'] = new_server_url
            p_rec.extra_info = extra_dict

            sess.commit()
            ModuleMetaDb.checkpoint_wal()

            logger.info(f"[MetaImageUtil] 인물 프로필 사진 저장 프로세스 성공 완료 -> Code: {p_rec.person_idx}, LocalFile: '{target_filepath}', URL: '{new_server_url}'")
            return True, new_server_url, p_rec

        except Exception as e:
            sess.rollback()
            logger.error(f"[MetaImageUtil] save_user_cropped_person_image 치명적 오류 ({person_identifier}): {e}")
            logger.error(traceback.format_exc())
            return False, str(e), None
        finally:
            sess.remove()


    @classmethod
    def sync_single_record_disk_images(cls, meta_record, custom_root_path=None):
        """
        단일 레코드 디스크 파일 동기화 실제 구현체:
        1. _p_user / _pl_user 존재 시 DB URL 갱신
        2. _user 파일 존재 시 불필요한 시스템 파일(_p.jpg, _pl.jpg) 안전 삭제
        """
        try:
            from .mod_meta_db import MetaMedia

            if meta_record.category == 'WESTERN':
                stem = (meta_record.code or meta_record.ui_code or '').lower()
            else:
                stem = (meta_record.ui_code or meta_record.originaltitle or meta_record.code or '').lower()

            studio = meta_record.studio or ''
            year = meta_record.year or 1900

            target_folder, server_url_prefix = cls.get_server_folder_and_prefix(
                meta_record.domain, meta_record.category, stem, studio=studio, year=year
            )
            if custom_root_path:
                default_root = P.ModelSetting.get("jav_censored_image_server_local_path") or ""
                if default_root and target_folder and target_folder.startswith(default_root):
                    rel_path = os.path.relpath(target_folder, default_root)
                    target_folder = os.path.join(custom_root_path, rel_path)

            if not target_folder or not os.path.exists(target_folder):
                return 'no_folder', False

            files_in_folder = os.listdir(target_folder)
            files_lower = {f.lower(): f for f in files_in_folder}

            exts = ['jpg', 'jpeg', 'png', 'webp']
            user_p_file = next((files_lower[f"{stem}_p_user.{e}"] for e in exts if f"{stem}_p_user.{e}" in files_lower), None)
            sys_p_file = next((files_lower[f"{stem}_p.{e}"] for e in exts if f"{stem}_p.{e}" in files_lower), None)

            user_pl_file = next((files_lower[f"{stem}_pl_user.{e}"] for e in exts if f"{stem}_pl_user.{e}" in files_lower), None)
            sys_pl_file = next((files_lower[f"{stem}_pl.{e}"] for e in exts if f"{stem}_pl.{e}" in files_lower), None)

            # _user 파일 존재 시 기존 시스템 파일 삭제
            if user_p_file and sys_p_file:
                try: os.remove(os.path.join(target_folder, sys_p_file))
                except: pass

            if user_pl_file and sys_pl_file:
                try: os.remove(os.path.join(target_folder, sys_pl_file))
                except: pass

            is_modified = False

            # 대표 포스터 동기화
            target_p = user_p_file or sys_p_file
            if target_p:
                new_p_url = f"{server_url_prefix}/{target_p}"
                if meta_record.poster_url != new_p_url:
                    meta_record.poster_url = new_p_url
                    is_modified = True

                p_media = next((m for m in meta_record.media_files if m.media_type == 'poster'), None)
                if p_media:
                    if p_media.url != new_p_url:
                        p_media.url = new_p_url
                        p_media.is_user = bool(user_p_file)
                        is_modified = True
                else:
                    meta_record.media_files.append(MetaMedia(media_type="poster", url=new_p_url, is_user=bool(user_p_file), sort_order=0))
                    is_modified = True

            # 랜드스케이프 동기화
            target_pl = user_pl_file or sys_pl_file
            if target_pl:
                new_pl_url = f"{server_url_prefix}/{target_pl}"
                pl_media = next((m for m in meta_record.media_files if m.media_type == 'landscape'), None)
                if pl_media:
                    if pl_media.url != new_pl_url:
                        pl_media.url = new_pl_url
                        pl_media.is_user = bool(user_pl_file)
                        is_modified = True
                else:
                    meta_record.media_files.append(MetaMedia(media_type="landscape", url=new_pl_url, is_user=bool(user_pl_file), sort_order=1))
                    is_modified = True

            if is_modified:
                meta_record.updated_time = datetime.now()

            is_completely_missing = (not user_p_file and not sys_p_file and not user_pl_file and not sys_pl_file)
            return ('updated' if is_modified else 'synced'), is_completely_missing

        except Exception as e:
            logger.error(f"[MetaImageUtil] sync_single_record_disk_images 에러: {e}")
            return 'error', False


# -------------------------------------------------------------
# 공용 백그라운드 워커 클래스
# -------------------------------------------------------------

class MetaWorkerUtil:
    @classmethod
    def run_sync_worker(cls, category, sync_status, info_func=None, custom_root=None, auto_rescue=False):
        """로컬 디스크 이미지(_user) 동기화 & 잔여 파일 정리 공용 워커"""
        if not category:
            logger.error("[MetaWorker] run_sync_worker: category 인자가 누락되었습니다.")
            return

        from .mod_meta_db import ModuleMetaDb, MetaItem
        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess: return

        sync_status.update({
            'is_running': True, 'status': '작업 중', 'total': 0,
            'current': 0, 'updated': 0, 'rescued': 0,
            'current_code': '', 'stop_flag': False
        })

        t_start = time.time()

        try:
            records = sess.query(MetaItem).filter_by(category=std_cat).all()
            total_len = len(records)
            sync_status['total'] = total_len
            logger.info(f"[MetaWorker] [{std_cat}] 로컬 이미지 동기화 시작 ➔ 대상: {total_len}건, 누락 복구: {auto_rescue}")

            batch_size = 50
            processed = 0

            for idx, record in enumerate(records, 1):
                if sync_status.get('stop_flag'):
                    sync_status['status'] = '중단됨'
                    logger.info(f"[MetaWorker] [{std_cat}] 사용자에 의해 동기화 작업이 중단되었습니다.")
                    break

                sync_status['current'] = idx
                sync_status['current_code'] = record.originaltitle or record.code

                res_type, is_missing = MetaImageUtil.sync_single_record_disk_images(record, custom_root_path=custom_root)
                if res_type == 'updated':
                    sync_status['updated'] += 1
                    processed += 1

                if is_missing and auto_rescue and info_func:
                    try:
                        fresh = info_func(record.code, skip_trans=True)
                        if fresh and fresh.get('thumb'):
                            sync_status['rescued'] += 1
                            processed += 1
                            logger.debug(f"[MetaWorker] [{std_cat}] 누락 미디어 복구 성공: {record.code}")
                    except Exception as e_r:
                        logger.debug(f"[MetaWorker] [{std_cat}] 누락 복구 실패 ({record.code}): {e_r}")

                if idx % 200 == 0 or idx == total_len:
                    percent = (idx / total_len * 100) if total_len > 0 else 100
                    logger.info(f"[MetaWorker] [{std_cat}] 동기화 진행 중: {idx}/{total_len} ({percent:.1f}%) | URL갱신: {sync_status['updated']}, 복구: {sync_status['rescued']}")

                if processed >= batch_size:
                    sess.commit()
                    processed = 0

            if processed > 0:
                sess.commit()

            ModuleMetaDb.checkpoint_wal()
            elapsed = time.time() - t_start
            if not sync_status.get('stop_flag'):
                sync_status['status'] = '완료'
                logger.info(f"[MetaWorker] [{std_cat}] 동기화 완료 ➔ 총 {total_len}건 (URL갱신: {sync_status['updated']}건, 복구: {sync_status['rescued']}건, 소요시간: {elapsed:.2f}초)")

        except Exception as e:
            logger.error(f"[MetaWorker] [{std_cat}] 동기화 워커 치명적 오류: {e}")
            logger.error(traceback.format_exc())
            sess.rollback()
            sync_status['status'] = f'오류: {e}'
        finally:
            sess.remove()
            sync_status['is_running'] = False


    @classmethod
    def run_enrichment_worker(cls, category, enrich_status, info_func, delay=2.0):
        """미디어(포스터/팬아트/트레일러) 누락 항목 자동 일괄 채우기 공용 워커"""
        if not category:
            logger.error("[MetaWorker] run_enrichment_worker: category 인자가 누락되었습니다.")
            return

        from .mod_meta_db import ModuleMetaDb, MetaItem
        sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(category)
        if not sess: return

        enrich_status.update({
            'is_running': True, 'status': '작업 중', 'total': 0,
            'current': 0, 'success': 0, 'fail': 0,
            'current_code': '', 'stop_flag': False
        })

        t_start = time.time()
        try:
            records = sess.query(MetaItem).filter_by(category=std_cat).all()
            targets = [r for r in records if not r.media_files]
            total_targets = len(targets)
            enrich_status['total'] = total_targets
            logger.info(f"[MetaWorker] [{std_cat}] 미디어 일괄 채우기 시작 ➔ 대상: {total_targets}건 (요청 간격: {delay}초)")

            if total_targets == 0:
                enrich_status['status'] = '완료 (대상 없음)'
                logger.info(f"[MetaWorker] [{std_cat}] 채울 미디어가 누락된 항목이 없습니다.")
                return

            for idx, record in enumerate(targets, 1):
                if enrich_status.get('stop_flag'):
                    enrich_status['status'] = '중단됨'
                    logger.info(f"[MetaWorker] [{std_cat}] 사용자에 의해 미디어 채우기 작업이 중단되었습니다.")
                    break

                enrich_status['current'] = idx
                enrich_status['current_code'] = record.originaltitle or record.code

                try:
                    res = info_func(record.code, skip_trans=True)
                    if res and res.get('thumb'):
                        enrich_status['success'] += 1
                        logger.debug(f"[MetaWorker] [{std_cat}] 미디어 획득 성공 ({idx}/{total_targets}): {record.code}")
                    else:
                        enrich_status['fail'] += 1
                except Exception as e_item:
                    enrich_status['fail'] += 1
                    logger.debug(f"[MetaWorker] [{std_cat}] 미디어 획득 실패 ({record.code}): {e_item}")

                if idx % 20 == 0 or idx == total_targets:
                    percent = (idx / total_targets * 100) if total_targets > 0 else 100
                    logger.info(f"[MetaWorker] [{std_cat}] 미디어 채우기 진행 중: {idx}/{total_targets} ({percent:.1f}%) | 성공: {enrich_status['success']}, 실패: {enrich_status['fail']}")

                time.sleep(delay)

            ModuleMetaDb.checkpoint_wal()
            elapsed = time.time() - t_start
            if not enrich_status.get('stop_flag'):
                enrich_status['status'] = '완료'
                logger.info(f"[MetaWorker] [{std_cat}] 미디어 일괄 채우기 완료 ➔ 총 {total_targets}건 (성공: {enrich_status['success']}건, 실패: {enrich_status['fail']}건, 소요시간: {elapsed:.2f}초)")

        except Exception as e:
            logger.error(f"[MetaWorker] [{std_cat}] 미디어 채우기 치명적 오류: {e}")
            logger.error(traceback.format_exc())
            enrich_status['status'] = f'오류: {e}'
        finally:
            sess.remove()
            enrich_status['is_running'] = False


class MetaResponseUtil:
    @classmethod
    def finalize_info_return(cls, entity_dict, extra_opts=None, category=None):
        """DB 사용 여부와 무관하게 호출자에게 반환할 최종 데이터의 사본을 안전하게 가공합니다."""
        if not entity_dict or not isinstance(entity_dict, dict):
            return entity_dict

        opts = dict(extra_opts or {})
        res = copy.deepcopy(entity_dict)

        # 포스터(p)가 완전히 누락된 경우 랜드스케이프(pl)를 포스터로 폴백하여 Plex 정상 인식 보장
        thumbs = res.get('thumb') or []
        has_poster = any(isinstance(t, dict) and t.get('aspect') == 'poster' and t.get('value') for t in thumbs)
        if not has_poster:
            pl_thumb = next((t for t in thumbs if isinstance(t, dict) and t.get('aspect') == 'landscape' and t.get('value')), None)
            if pl_thumb:
                fallback_poster = copy.deepcopy(pl_thumb)
                fallback_poster['aspect'] = 'poster'
                res.setdefault('thumb', []).append(fallback_poster)

        # 줄거리가 비어있으면 부제(tagline)로 동적 폴백 (DB에는 순수 빈값 보존)
        current_plot = str(res.get('plot') or '').strip()
        fallback_tagline = str(res.get('tagline') or '').strip()
        if not current_plot and fallback_tagline:
            res['plot'] = fallback_tagline

        # meta_db 활성화 상태에서 공유 라이브러리 등 임시 오버라이드 요청이 있는 경우에만 위임
        if P.ModelSetting.get_bool("meta_db_use"):
            try:
                from .mod_meta_db import ModuleMetaDb
                res = ModuleMetaDb.apply_transient_overrides(res, opts, category=category)
            except Exception as e_override:
                logger.debug(f"[MetaResponseUtil] apply_transient_overrides 예외: {e_override}")

        # 이미지 필드 제거 옵션 처리 (공유 라이브러리 전용)
        if opts.get('strip_images'):
            res['poster_url'] = ''
            res['landscape_url'] = ''
            res['thumb'] = []
            res['fanart'] = []

        return res
