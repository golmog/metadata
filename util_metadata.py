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
from sqlalchemy.orm.attributes import flag_modified

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
            crop_info_tmp = None

            try:
                if isinstance(crop_data_or_base64, str) and crop_data_or_base64.startswith('{'):
                    crop_info_tmp = json.loads(crop_data_or_base64)
                    if isinstance(crop_info_tmp, dict) and crop_info_tmp.get('source_type'):
                        source_type = str(crop_info_tmp['source_type']).lower()
            except:
                pass

            # 사용자가 세로 포스터(P)를 직접 업로드한 경우
            if p_image_base64_data:
                raw_b64 = p_image_base64_data.split(',', 1)[1] if ',' in p_image_base64_data else p_image_base64_data
                src_img = Image.open(BytesIO(base64.b64decode(raw_b64)))

            # 사용자가 가로 커버(PL)를 직접 업로드한 경우
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

            # 직접 입력된 원격 이미지 URL 소스 확인 (이미지 누락 항목 구출)
            custom_url = None
            if isinstance(crop_info_tmp, dict):
                custom_url = crop_info_tmp.get('crop_url') or crop_info_tmp.get('url')

            if src_img is None and custom_url and custom_url.startswith(('http://', 'https://')):
                from support_site import SiteAvBase
                src_img = SiteAvBase.imopen(custom_url)
                if src_img is not None:
                    logger.debug(f"[MetaImageUtil] 원격 직접 입력 URL에서 크롭 소스 획득 성공: {custom_url}")

            # 소스가 세로 포스터(P)로 선택된 경우 로컬 파일 확인
            if src_img is None and source_type == 'p':
                for cand in [f"{stem}_p_user.jpg", f"{stem}_p.jpg", f"{stem}_p.png", f"{stem}_p.webp"]:
                    cp = os.path.join(target_folder, cand)
                    if os.path.exists(cp):
                        src_img = Image.open(cp)
                        break
                if src_img is None and item.poster_url and item.poster_url.startswith('http'):
                    from support_site import SiteAvBase
                    src_img = SiteAvBase.imopen(item.poster_url)

            # 소스가 가로 커버(PL)인 경우 로컬 파일 확인
            elif src_img is None:
                for cand in [f"{stem}_pl_user.jpg", f"{stem}_pl.jpg", f"{stem}_pl.png", f"{stem}_pl.webp"]:
                    cp = os.path.join(target_folder, cand)
                    if os.path.exists(cp):
                        src_img = Image.open(cp)
                        break

            # 디스크에 없으면 DB에 기록된 메타 미디어 URL에서 로드
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


    @classmethod
    def save_direct_user_image(cls, code, source_path_or_url=None, image_type='pl', category=None, image_base64_data=None):
        """
        URL, 로컬 파일 경로, 또는 Base64 데이터를 받아 크롭 없이 직접
        _pl_user.jpg 또는 _p_user.jpg 파일로 저장하고 DB를 갱신합니다. (외부 툴 API 호환)
        """
        if not category:
            logger.error(f"[MetaImageUtil] save_direct_user_image: category 누락. code='{code}'")
            return False, '카테고리가 지정되지 않았습니다.'

        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia
        from support_site import SiteAvBase
        from sqlalchemy.orm.attributes import flag_modified

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

            target_folder, server_url_prefix = cls.get_server_folder_and_prefix(
                domain, std_cat, stem, studio=studio, year=year
            )
            if not target_folder or not server_url_prefix:
                return False, '이미지 서버 로컬 경로 또는 URL 설정이 비어있습니다.'

            os.makedirs(target_folder, exist_ok=True)

            target_type = 'p' if str(image_type).lower() == 'p' else 'pl'
            clean_source = str(source_path_or_url or '').strip()
            src_img = None

            # Base64 데이터 소스 확인
            if image_base64_data:
                try:
                    raw_b64 = image_base64_data.split(',', 1)[1] if ',' in image_base64_data else image_base64_data
                    src_img = Image.open(BytesIO(base64.b64decode(raw_b64)))
                    logger.debug(f"[MetaImageUtil] save_direct_user_image: Base64 이미지 디코딩 로드 성공")
                except Exception as e_b64:
                    logger.warning(f"[MetaImageUtil] Base64 디코딩 실패: {e_b64}")

            # 로컬 파일 경로 또는 원격 URL 소스 확인
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

            # 기존 시스템 파일(_pl.jpg 또는 _p.jpg) 정리
            for ext_cand in ['jpg', 'jpeg', 'png', 'webp']:
                old_file = os.path.join(target_folder, f"{stem}_{target_type}.{ext_cand}")
                if os.path.exists(old_file):
                    try:
                        os.remove(old_file)
                    except Exception:
                        pass

            new_url = f"{server_url_prefix}/{target_filename}"

            # DB MetaItem 갱신 및 original.thumb 원본 주소 보존
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

            # MetaMedia 레코드 갱신
            media_rec = next((m for m in item.media_files if m.media_type == media_type_name), None)
            if media_rec:
                media_rec.url = new_url
                media_rec.is_user = True
            else:
                sort_idx = 0 if target_type == 'p' else 1
                item.media_files.append(MetaMedia(media_type=media_type_name, url=new_url, is_user=True, sort_order=sort_idx))

            item.updated_time = datetime.now()
            sess.add(item)
            sess.commit()
            ModuleMetaDb.checkpoint_wal()

            logger.info(f"[MetaImageUtil] 원본 이미지 직접 저장 및 DB 즉시 반영 완료: [{std_cat}] {item.code} ➔ {target_filename}")
            return True, {
                'new_url': new_url,
                'target_type': target_type,
                'code': item.code,
                'poster_url': item.poster_url
            }

        except Exception as e:
            logger.error(f"[MetaImageUtil] save_direct_user_image 오류 ({code}): {e}")
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
                if user_p_file:
                    # 유저 커스텀 포스터가 존재하는 경우에만 MetaMedia 관리
                    if p_media:
                        if p_media.url != new_p_url or not p_media.is_user:
                            p_media.url = new_p_url
                            p_media.is_user = True
                            is_modified = True
                    else:
                        meta_record.media_files.append(MetaMedia(media_type="poster", url=new_p_url, is_user=True, sort_order=0))
                        is_modified = True
                elif p_media and p_media.is_user:
                    # 유저 파일이 디스크에서 삭제되어 시스템 파일로 복원된 경우
                    meta_record.media_files.remove(p_media)
                    is_modified = True

            # 랜드스케이프 동기화
            target_pl = user_pl_file or sys_pl_file
            if target_pl:
                new_pl_url = f"{server_url_prefix}/{target_pl}"
                pl_media = next((m for m in meta_record.media_files if m.media_type == 'landscape'), None)
                if user_pl_file:
                    # 유저 커스텀 가로커버가 존재하는 경우에만 MetaMedia 관리
                    if pl_media:
                        if pl_media.url != new_pl_url or not pl_media.is_user:
                            pl_media.url = new_pl_url
                            pl_media.is_user = True
                            is_modified = True
                    else:
                        meta_record.media_files.append(MetaMedia(media_type="landscape", url=new_pl_url, is_user=True, sort_order=1))
                        is_modified = True
                elif pl_media and pl_media.is_user:
                    # 유저 파일이 삭제되어 시스템 파일로 복원된 경우
                    meta_record.media_files.remove(pl_media)
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


class MetaHealingUtil:
    """
    기존의 번역본과 유저 커스텀 데이터(_user 이미지, 프리뷰 클립 등)를 보존하면서,
    누락된 인물 정보, 오리지널 메타, 부가 정보를 출처 사이트로부터 효율적으로 보완/치유하는 유틸리티
    """

    @classmethod
    def heal_metadata(cls, module, code, category):
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

        # 기존 텍스트 번역 상태 사전 검사
        existing_title = str(cached_json.get('title') or '').strip()
        existing_tagline = str(cached_json.get('tagline') or '').strip()
        existing_plot = str(cached_json.get('plot') or '').strip()

        has_valid_plot = bool(existing_plot)
        plot_is_korean = SiteUtil.is_include_hangul(existing_plot)
        tagline_is_korean = SiteUtil.is_include_hangul(existing_tagline)

        # 번역 필요 여부 판단: 한글이 없거나 줄거리가 누락된 경우에만 번역 진행
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

        # 캐시된 썸네일 경로가 있으면 전달하여 불필요한 재탐색 차단
        ps_url = cached_json.get('original', {}).get('thumb', {}).get('ps_url')
        info_opts = {'skip_trans': skip_trans}
        if ps_url:
            info_opts['ps_url'] = ps_url

        try:
            module.keyword_cache.set(f"BYPASS_{code}", "1")
        except Exception:
            pass

        # 검색 없이 대상 코드의 상세 정보 직접 조회
        try:
            if is_western or cat_upper == 'JAV_CEN':
                fresh_data = module.info(code, keyword=ui_code, extra_opts=info_opts)
            else:
                fresh_data = module.info(code, extra_opts=info_opts)
        except TypeError:
            fresh_data = module.info(code, extra_opts=info_opts)

        if not fresh_data:
            return {'ret': 'warning', 'msg': '최신 정보를 가져오지 못했습니다.', 'title_log': title_for_log, 'is_updated': False}

        # 변경 사항 감지 추적
        changes = []

        # 번역 및 텍스트 필드 비교
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

        # 인물 정보 변동 비교
        cached_actors = cached_json.get('actor') or []
        fresh_actors = fresh_data.get('actor') or []

        if len(cached_actors) == 0 and len(fresh_actors) > 0:
            changes.append(f'인물 등록 {len(fresh_actors)}명')
        elif len(fresh_actors) > len(cached_actors):
            changes.append(f'인물 추가 {len(fresh_actors) - len(cached_actors)}명')
        else:
            # 기존 인물 정보의 실질 변동(한글명/사진 등) 정밀 대조
            cached_ko_count = sum(1 for a in cached_actors if isinstance(a, dict) and a.get('name_ko'))
            fresh_ko_count = sum(1 for a in fresh_actors if isinstance(a, dict) and a.get('name_ko'))
            if fresh_ko_count > cached_ko_count:
                changes.append(f'인물 정보 업데이트 {fresh_ko_count - cached_ko_count}명')

            cached_has_thumb = any(a.get('thumb') for a in cached_actors if isinstance(a, dict))
            fresh_has_thumb = any(a.get('thumb') for a in fresh_actors if isinstance(a, dict))
            if not cached_has_thumb and fresh_has_thumb:
                changes.append('인물 사진 보완')

        # 오리지널 메타 보완 확인
        cached_orig = cached_json.get('original') or {}
        fresh_orig = fresh_data.get('original') or {}
        if not cached_orig and fresh_orig:
            changes.append('오리지널 메타 보완')
        else:
            if not cached_orig.get('fanart') and fresh_orig.get('fanart'):
                changes.append('팬아트 보완')
            if not cached_orig.get('extras') and fresh_orig.get('extras'):
                changes.append('예고편 보완')

        # 기본 속성값 보완 확인
        if not cached_json.get('studio') and fresh_data.get('studio'):
            changes.append('제작사 등록')
        if not cached_json.get('director') and fresh_data.get('director'):
            changes.append('감독 등록')
        if not cached_json.get('series') and fresh_data.get('series'):
            changes.append('시리즈 등록')
        if not cached_json.get('premiered') and fresh_data.get('premiered'):
            changes.append('출시일 등록')

        # 프리뷰 클립 정보 및 영상 소스 경로 보존 (수동 생성 자산)
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

        # 사용자 태그 결합 보존
        cached_tags = cached_json.get('tag') or []
        fresh_tags = fresh_data.get('tag') or []
        for ct in cached_tags:
            if ct and ct not in fresh_tags:
                fresh_tags.append(ct)
        fresh_data['tag'] = fresh_tags

        is_updated = (len(changes) > 0)

        # 실질적 변경이 있을 때만 DB 저장
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

                        sync_res, _ = MetaImageUtil.sync_single_record_disk_images(meta_item)
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
        """
        외부 사이트 접속 없이 로컬 DB(인물 DB)와 이미지 서버 디스크 파일 상태만을 대조하여
        인물 표기 정보 및 포스터/커버 유저 이미지 수동 교체 내역을 동기화

        배우 썸네일은 런타임에 인물 DB에서 동적으로 구성하므로 작품 캐시에는 저장하지 않는다.
        """
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

            # 디스크 이미지 파일 점검 전 상태 스냅샷 수집
            old_poster_url = str(item.poster_url or '').strip()
            old_pl_media = next((m for m in item.media_files if m.media_type == 'landscape'), None)
            old_pl_url = str(old_pl_media.url or '').strip() if old_pl_media else ''
            old_pl_is_user = bool(old_pl_media.is_user) if old_pl_media else False
            old_has_p_media = any(m.media_type == 'poster' for m in item.media_files)
            old_has_pl_media = bool(old_pl_media)

            sync_res, _ = MetaImageUtil.sync_single_record_disk_images(item)
            if sync_res == 'updated':
                new_poster_url = str(item.poster_url or '').strip()
                new_pl_media = next((m for m in item.media_files if m.media_type == 'landscape'), None)
                new_pl_url = str(new_pl_media.url or '').strip() if new_pl_media else ''
                new_pl_is_user = bool(new_pl_media.is_user) if new_pl_media else False
                new_has_p_media = any(m.media_type == 'poster' for m in item.media_files)
                new_has_pl_media = bool(new_pl_media)

                # 세로 포스터 변동 세분화
                if '_user.' in new_poster_url and '_user.' not in old_poster_url:
                    changes.append('유저 포스터 반영')
                elif '_user.' not in new_poster_url and '_user.' in old_poster_url:
                    changes.append('시스템 포스터 복원')
                elif new_poster_url and new_poster_url != old_poster_url:
                    changes.append('포스터 주소 동기화')

                # 가로 커버 변동 세분화
                if new_pl_url and ('_user.' in new_pl_url or new_pl_is_user) and ('_user.' not in old_pl_url and not old_pl_is_user):
                    changes.append('유저 가로커버 반영')
                elif old_pl_url and ('_user.' not in new_pl_url and not new_pl_is_user) and ('_user.' in old_pl_url or old_pl_is_user):
                    changes.append('시스템 가로커버 복원')
                elif new_pl_url and new_pl_url != old_pl_url:
                    changes.append('가로 커버 동기화')

                # 하위 관계 테이블 에셋 레코드 복원
                if (not old_has_p_media and new_has_p_media) or (not old_has_pl_media and new_has_pl_media):
                    changes.append('미디어 테이블 복원')

                if not any(k in changes for k in ['포스터', '가로커버', '미디어']):
                    changes.append('디스크 파일 동기화')

                is_modified = True

            # 인물 정보 및 프로필 사진 대조
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

                    # 한국어 표기명 반영
                    curr_ko = act.get('name_ko') or ''
                    curr_name = act.get('name') or ''
                    if target_ko and (curr_ko != target_ko or curr_name != target_ko):
                        act['name_ko'] = target_ko
                        act['name'] = target_ko
                        updated_ko_count += 1
                        actor_data_changed = True

                    # 영문 표기명 보완
                    curr_en = act.get('name_en') or ''
                    if target_en and not curr_en:
                        act['name_en'] = target_en
                        updated_en_count += 1
                        actor_data_changed = True

                    # 식별코드 보완
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

            # 장르 및 태그 최신 번역 사전(av_tags.json / constants) 대조
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

                    # 영상 내에서는 동일하게 번역된 장르명이 중복으로 들어가지 않도록 방지
                    if g_trans in seen_translated_genres:
                        continue
                    seen_translated_genres.add(g_trans)

                    # 원문(name_org) 기준으로 태그 마스터 레코드 대조
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

                # 현재 영상에 매핑된 태그 ID들과 비교하여 변동 시에만 재매핑
                current_genre_tag_ids = [tm.tag_id for tm in item.tag_maps if tm.tag and tm.tag.tag_type == 'genre']
                new_genre_tag_ids = [tr.id for tr in new_tag_records if tr.id]

                if set(current_genre_tag_ids) != set(new_genre_tag_ids):
                    # 기존 장르 태그 매핑 제거 후 최신 태그로 재매핑
                    for tm in list(item.tag_maps):
                        if tm.tag and tm.tag.tag_type == 'genre':
                            item.tag_maps.remove(tm)

                    for tag_rec in new_tag_records:
                        item.tag_maps.append(MetaItemTagMap(tag_id=tag_rec.id))

                    changes.append('장르 번역 최신화')
                    is_modified = True

            # 변동 사항이 발생한 경우에만 DB 커밋
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
            logger.error(f"[MetaHealingUtil] sync_local_data 오류 ({code}): {e}")
            return {'ret': 'error', 'msg': str(e), 'is_updated': False}
        finally:
            sess.remove()


class MetaParserUtil:
    """
    파일 경로(media_path)로부터 검색용 키워드(품번/제목)를 추출하는 유틸리티
    YAML 고급 설정의 파싱 룰(SiteAvBase._parse_ui_code)을 단일 소스로 연동하여 동작합니다.
    """

    @classmethod
    def extract_keyword_from_path(cls, media_path, category='JAV_CEN'):
        if not media_path or not isinstance(media_path, str):
            return ""

        clean_path = media_path.strip().replace('\\', '/')
        filename = os.path.basename(clean_path)
        if not filename:
            return ""

        cat_upper = str(category or 'JAV_CEN').upper()

        # 서양 메타데이터: 원본 파일명 반환 (mod_western._clean_search_keyword의 정규식 규칙으로 정제)
        if cat_upper in ['WESTERN', 'WEST']:
            return os.path.splitext(filename)[0]

        from support_site import SiteAvBase

        # 파일명 자체로 YAML 기본 및 특수 파싱 룰 적용
        ui_code, label, num = SiteAvBase._parse_ui_code(filename, category=cat_upper)
        if ui_code and (num or '-' in ui_code):
            return ui_code

        # 접두사가 파일명이 아닌 상위 디렉터리명에 있는 경우 결합 재시도 (예: /1pondo/092121_001.mp4)
        parent_dir = os.path.basename(os.path.dirname(clean_path))
        if parent_dir:
            combined_candidate = f"{parent_dir}-{filename}"
            ui_code_comb, label_comb, num_comb = SiteAvBase._parse_ui_code(combined_candidate, category=cat_upper)
            if ui_code_comb and (num_comb or '-' in ui_code_comb):
                return ui_code_comb

        return ui_code or os.path.splitext(filename)[0]
