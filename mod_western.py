# -*- coding: utf-8 -*-
import os
import re
import traceback
import threading
import time
import json
import sqlite3
from datetime import datetime
from io import BytesIO
from urllib.parse import urlparse, parse_qs, quote

from flask import send_from_directory, send_file, jsonify, Response, abort
import requests
from sqlalchemy import or_

from .setup import *
from support_site.site_av.site_tpdb import SiteTpdb
from support_site.site_av.site_stashdb import SiteStashdb
from support_site.site_av.site_av_base import SiteAvBase
from support_site.entity_av import EntityAVSearch
from support_site import SiteUtil, UtilNfo

from .mod_meta_db import ModuleMetaDb
from .util_metadata import MetaImageUtil, MetaWorkerUtil, MetaResponseUtil


class ModuleWestern(PluginModuleBase):
    
    def __init__(self, P):
        super(ModuleWestern, self).__init__(P, name='western', first_menu='setting')
        self.category = 'WESTERN'
        self.web_list_model = None
        self.site_map = {
            "stashdb": SiteStashdb,
            "tpdb": SiteTpdb,
        }

        self.db_default = {
            f"{self.name}_db_version": "1",
            
            # 사이트 순환 검색 우선순위
            f"{self.name}_order": "stashdb, tpdb",
            
            # StashDB 설정
            f"{self.name}_stashdb_api_key": "",
            f"{self.name}_stashdb_test_code": "",
            f"{self.name}_stashdb_user_schema": "studio:czechvr|{raw_title}|{studio_code} - {raw_title}",
            f"{self.name}_stashdb_use_fingerprint": "False",
            f"{self.name}_stashdb_fingerprint_type": "OSHASH",
            f"{self.name}_stashdb_ffmpeg_path": "/usr/bin/ffmpeg",

            # TPDB 설정
            f"{self.name}_tpdb_api_token": "",
            f"{self.name}_tpdb_test_code": "",

            # 공통 메타 설정
            f"{self.name}_trans_option": "using",
            f"{self.name}_trans_title": "True",
            f"{self.name}_include_male": "False",
            f"{self.name}_title_format": "[{studio}] {actor} - {title}",
            f"{self.name}_tag_option": "studio",
            f"{self.name}_use_extras": "False",

            f"{self.name}_search_regex_removal": r"[._\-\s]+xxx[._\-\s]+(?:internal|remastered|webrip|web-dl)?[._\-\s]*\d+[pk][._\-\s]+.*$",
            f"{self.name}_search_regex_removal_2nd": r"(?:solo|vr)$",

            f"{self.name}_trust_single_result": "False",
            f"{self.name}_json_include_male": "False",

            f"{self.name}_use_proxy": "False",
            f"{self.name}_proxy_url": "",
            f"{self.name}_use_trailer_proxy": "False",

            f"{self.name}_use_movie_title_format": "True",
            f"{self.name}_movie_title_format": "[{studio}] {title}",

            f"{self.name}_use_smart_crop": "False",
            f"{self.name}_poster_force_studios": "",

            f"{self.name}_image_mode": "image_server",
            f"{self.name}_actor_image_mode": "site",
            f"{self.name}_image_server_actor_path": "/western/actors",
            f"{self.name}_actor_img_order": "site_img_url, local_img_path",

            f"{self.name}_use_preview_clip": "False",
            f"{self.name}_preview_auto_create": "False",
        }

        # 백그라운드 작업 상태 관리
        self.enrich_status = {'is_running': False, 'status': '대기 중', 'total': 0, 'current': 0, 'success': 0, 'fail': 0, 'current_code': '', 'stop_flag': False}
        self.sync_status = {'is_running': False, 'status': '대기 중', 'total': 0, 'current': 0, 'updated': 0, 'rescued': 0, 'current_code': '', 'stop_flag': False}

        try:
            self.keyword_cache = F.get_cache(f"{P.package_name}_{self.name}_keyword_cache")
        except Exception:
            self.keyword_cache = {}

    ################################################
    # region PluginModuleBase 메서드 오버라이드

    def plugin_load(self):
        try:
            for key, value in self.db_default.items():
                if P.ModelSetting.get(key) is None:
                    P.ModelSetting.set(key, value)
        except Exception as e_db_sync:
            logger.error(f"[{self.name}] DB Sync Error: {e_db_sync}")

        try:
            ModuleMetaDb.init_engines()
            self.web_list_model = ModuleMetaDb
            logger.debug(f"[{self.name}] Universal Metadata DB Engine connected.")
        except Exception as e:
            logger.error(f"[{self.name}] DB Init Error: {e}")
        self._set_site_setting()

    def plugin_load_celery(self):
        self._set_site_setting()

    def setting_save(self, req):
        """
        FF 프레임워크의 일괄 덮어쓰기 방어:
        현재 제출된 폼(req.form)에 실제로 존재하는 설정 및 해당 서브페이지 관련 체크박스만 안전하게 갱신
        """
        try:
            change_list = []
            form_keys = set(req.form.keys())

            # 1. 폼에 전송된 모든 텍스트/라디오/체크된 항목 갱신
            for key in form_keys:
                if key in ['sub', 'package_name', 'module_name']: continue
                if key in self.db_default:
                    val = req.form[key].strip()
                    if P.ModelSetting.set(key, val):
                        change_list.append(key)

            # 2. 폼에 없는 체크박스 처리 (현재 전송된 페이지 그룹의 체크박스만 'False' 판단)
            submitted_prefixes = set()
            for k in form_keys:
                if '_db_' in k: submitted_prefixes.add('_db_')
                if '_stashdb_' in k: submitted_prefixes.add('_stashdb_')
                if '_tpdb_' in k: submitted_prefixes.add('_tpdb_')
                if k in ['western_order', 'western_title_format', 'western_trans_option']:
                    submitted_prefixes.add('main_setting')

            for key, default_val in self.db_default.items():
                if default_val in ['True', 'False'] and key not in form_keys:
                    should_turn_off = False
                    if '_db_' in key and '_db_' in submitted_prefixes: should_turn_off = True
                    elif '_stashdb_' in key and '_stashdb_' in submitted_prefixes: should_turn_off = True
                    elif '_tpdb_' in key and '_tpdb_' in submitted_prefixes: should_turn_off = True
                    elif 'main_setting' in submitted_prefixes and not any(p in key for p in ['_db_', '_stashdb_', '_tpdb_']):
                        should_turn_off = True

                    if should_turn_off and P.ModelSetting.set(key, 'False'):
                        change_list.append(key)

            self.setting_save_after(change_list)
            return jsonify(True)
        except Exception as e:
            logger.error(f"[{self.name}] setting_save 에러: {e}")
            return jsonify(False)

    def setting_save_after(self, change_list):
        self._set_site_setting()

    def _set_site_setting(self):
        for site_key, site_cls in self.site_map.items():
            try:
                P.logger.debug(f"[{self.name}] Setting config for {site_cls.__name__}.")
                site_cls.set_config(self.P.ModelSetting)
            except Exception as e:
                P.logger.error(f"[{self.name}] Error initializing site {site_key}: {e}")

    def process_ajax(self, sub, req):
        try:
            command = req.form.get('command')
            arg1 = req.form.get('arg1', '') or ''
            arg2 = req.form.get('arg2', '') or ''
            arg3 = req.form.get('arg3', '') or ''
            list_type = (req.form.get('list_type') or '').strip().lower()

            # 인물(배우) DB 요청 판별 및 meta_db 자동 위임
            is_person_req = (
                sub in ['person_list'] or 
                req.form.get('category') == 'PERSON' or 
                req.form.get('search_domain') is not None or 
                'search_domain=' in arg1 or 
                'category=PERSON' in arg1 or
                (isinstance(command, str) and command.startswith('person_'))
            )

            if is_person_req:
                if command in ['web_list', 'list', 'person_web_list'] or 'search_domain=' in arg1 or req.form.get('search_domain') is not None:
                    default_dom = req.form.get('search_domain', 'WESTERN') or 'WESTERN'
                    return jsonify(ModuleMetaDb.person_web_list(req, default_domain=default_dom))

                if command and (command.startswith('person_') or command == 'db_vacuum'):
                    meta_module = P.get_module('meta_db')
                    if meta_module:
                        res = meta_module.process_command(command, arg1, arg2, arg3, req)
                        if res is not None:
                            return res
                    return jsonify({'ret': 'error', 'msg': f'인물 명령 처리 실패: {command}'})

            # 영상 목록(web_list) 요청 처리
            if list_type == 'meta' or command in ['web_list', 'list'] or sub in ['web_list', 'list'] or req.form.get('search_site') is not None:
                category = req.form.get('category') or getattr(self, 'category', 'WESTERN')
                if str(category).upper() == 'PERSON':
                    return jsonify(ModuleMetaDb.person_web_list(req, default_domain=req.form.get('search_domain', 'WESTERN')))
                return jsonify(ModuleMetaDb.web_list(req, category=category))

            # 백그라운드 상태 폴링
            if command == 'db_enrich_status': return jsonify({'ret': 'success', 'data': self.enrich_status})
            if command == 'db_sync_status': return jsonify({'ret': 'success', 'data': self.sync_status})

            # 모듈 커맨드 우선 처리
            if command:
                res = self.process_command(command, arg1, arg2, arg3, req)
                if res is not None:
                    return res

            # 프레임워크 기본 AJAX 처리
            res = super(ModuleWestern, self).process_ajax(sub, req)
            if res is not None:
                return res

            return jsonify({'ret': 'error', 'msg': f'미처리된 AJAX 요청: sub={sub}, command={command}'})

        except Exception as e:
            logger.error(f"[{self.name}] Exception in process_ajax: {e}")
            logger.error(traceback.format_exc())
            return jsonify({'ret': 'error', 'msg': str(e)})

    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            ret = {'ret': 'success'}

            # --- 포스터 수동 크롭/업로드 저장 (MetaImageUtil 위임) ---
            if command == "db_crop_save":
                code = arg1
                crop_data = arg2
                upload_payload = arg3
                pl_base64, p_base64 = None, None

                if upload_payload:
                    try:
                        p_json = json.loads(upload_payload)
                        if isinstance(p_json, dict):
                            if p_json.get('type') == 'p': p_base64 = p_json.get('data')
                            elif p_json.get('type') == 'pl': pl_base64 = p_json.get('data')
                    except Exception:
                        pl_base64 = upload_payload

                success, result_msg = MetaImageUtil.save_user_cropped_poster(
                    code, crop_data, pl_image_base64_data=pl_base64, p_image_base64_data=p_base64, category=self.category
                )
                return jsonify({'ret': 'success' if success else 'error', 'msg': result_msg, 'new_url': result_msg if success else None})

            # --- 웹 UI 검색 테스트 ---
            elif command == "test":
                call = arg1
                code = arg2
                P.ModelSetting.set(f"{self.name}_{call}_test_code", code)
                SiteClass = self.site_map.get(call)
                if not SiteClass:
                    return jsonify({'ret': 'error', 'msg': f"Site '{call}' not found."})

                search_results = self.search2(code, call, manual=True)
                if not search_results:
                    return jsonify({'ret': 'warning', 'msg': f"'{call}' 검색 결과가 없습니다: '{code}'"})

                info_data = self.info(search_results[0]['code'], keyword=code)
                ret['json'] = {
                    "search": search_results,
                    "info": info_data if info_data else {}
                }
                return jsonify(ret)

            # --- 폼 기반 DB 메타데이터 수정 저장 ---
            elif command == 'db_edit_save':
                code = arg1
                raw_json_str = arg2
                if not raw_json_str:
                    return jsonify({'ret': 'error', 'msg': '수정할 데이터가 없습니다.'})
                try:
                    new_json = json.loads(raw_json_str)
                    success = ModuleMetaDb.save_metadata(self.category, new_json)
                    return jsonify({'ret': 'success' if success else 'error', 'msg': 'DB에 성공적으로 저장되었습니다.' if success else '업데이트 실패'})
                except Exception as e:
                    return jsonify({'ret': 'error', 'msg': str(e)})

            # --- 단일 레코드 삭제 ---
            elif command == 'db_delete':
                success = ModuleMetaDb.delete_record(arg1, category=self.category)
                return jsonify({'ret': 'success' if success else 'error'})

            elif command == 'db_delete_selected':
                success, count = ModuleMetaDb.delete_records(arg1, category=self.category)
                msg = f"{count}건의 메타데이터가 삭제되었습니다." if success else "선택 항목 삭제 실패"
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            # --- 카테고리 DB 초기화 및 최적화 ---
            elif command == 'db_clear':
                success, count = ModuleMetaDb.clear_db(self.category)
                return jsonify({'ret': 'success' if success else 'error', 'msg': f'{count}건의 메타데이터가 삭제되었습니다.' if success else '초기화 실패'})

            elif command == 'db_vacuum':
                success = ModuleMetaDb.vacuum_db()
                return jsonify({'ret': 'success' if success else 'error', 'msg': 'DB 최적화(VACUUM) 완료' if success else '최적화 실패'})

            # --- 미디어 일괄 채우기 (공용 워커 호출) ---
            elif command == 'db_enrich_start':
                if self.enrich_status['is_running']:
                    return jsonify({'ret': 'warning', 'msg': '이미 미디어 채우기 작업이 진행 중입니다.'})
                delay = float(arg1) if arg1 else 2.0
                t = threading.Thread(
                    target=MetaWorkerUtil.run_enrichment_worker,
                    args=(self.category, self.enrich_status, self.info, delay)
                )
                t.daemon = True
                t.start()
                return jsonify({'ret': 'success', 'msg': '일괄 미디어 채우기 작업을 시작했습니다.'})

            elif command == 'db_enrich_stop':
                self.enrich_status['stop_flag'] = True
                return jsonify({'ret': 'success', 'msg': '작업 중단을 요청했습니다.'})

            # --- 로컬 이미지 동기화 & 잔여 파일 정리 (공용 워커 호출) ---
            elif command == 'db_sync_local_start':
                if self.sync_status['is_running']:
                    return jsonify({'ret': 'warning', 'msg': '이미 로컬 동기화 작업이 진행 중입니다.'})
                custom_root = arg1.strip() if arg1 else None
                auto_rescue = (arg2 == 'true')
                t = threading.Thread(
                    target=MetaWorkerUtil.run_sync_worker,
                    args=(self.category, self.sync_status, self.info, custom_root, auto_rescue)
                )
                t.daemon = True
                t.start()
                return jsonify({'ret': 'success', 'msg': '로컬 이미지 동기화 및 정리 작업을 시작했습니다.'})

            elif command == 'db_sync_local_stop':
                self.sync_status['stop_flag'] = True
                return jsonify({'ret': 'success', 'msg': '작업 중단을 요청했습니다.'})

            # --- 이미지/미디어만 재동기화 ---
            elif command == 'db_refresh_image_only':
                code = arg1
                cached_json = ModuleMetaDb.get_metadata(code, category=self.category)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})

                ui_code = cached_json.get('originaltitle') or cached_json.get('title') or code
                try: self.keyword_cache.set(f"BYPASS_{code}", "1")
                except: pass

                fresh_media = self.info(code, keyword=ui_code, skip_trans=True)
                if fresh_media and (fresh_media.get('thumb') or fresh_media.get('original', {}).get('thumb')):
                    cached_json['thumb'] = fresh_media.get('thumb', [])
                    cached_json['fanart'] = fresh_media.get('fanart', [])

                    # 원본 썸네일 및 팬아트 원천 주소 전체 최신화
                    if fresh_media.get('original'):
                        fresh_orig = fresh_media['original']
                        if 'original' not in cached_json or not isinstance(cached_json['original'], dict):
                            cached_json['original'] = {}
                        if fresh_orig.get('thumb'):
                            cached_json['original']['thumb'] = fresh_orig['thumb']
                        if fresh_orig.get('fanart'):
                            cached_json['original']['fanart'] = fresh_orig['fanart']
                        if fresh_orig.get('extras'):
                            cached_json['original']['extras'] = fresh_orig['extras']

                    if fresh_media.get('extras'):
                        cached_json['extras'] = fresh_media.get('extras', [])

                    ModuleMetaDb.save_metadata(self.category, cached_json)
                    logger.info(f"[{self.name}] [{code}] 이미지 및 팬아트 주소 갱신 완료 (Arts: {len(cached_json.get('fanart', []))}개)")
                    return jsonify({'ret': 'success', 'msg': f"[{ui_code}] 이미지 및 미디어 동기화 완료"})
                else:
                    return jsonify({'ret': 'warning', 'msg': '미디어 정보를 가져오지 못했습니다.'})

            # 현재 사이트 정보 제자리 갱신
            elif command == 'db_refresh_in_place':
                code = arg1
                cached_json = ModuleMetaDb.get_metadata(code, category=self.category)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})

                ui_code = cached_json.get('originaltitle') or cached_json.get('title') or code
                try: self.keyword_cache.set(f"BYPASS_{code}", "1")
                except: pass

                fresh_data = self.info(code, keyword=ui_code, skip_trans=False)
                if fresh_data:
                    ModuleMetaDb.save_metadata(self.category, fresh_data)
                    logger.info(f"[{self.name}] [{code}] 메타데이터 및 팬아트 갱신 완료")
                    return jsonify({'ret': 'success', 'msg': f"[{ui_code}] 메타데이터가 성공적으로 갱신되었습니다."})
                else:
                    return jsonify({'ret': 'warning', 'msg': '정보 조회에 실패했습니다.'})

            # --- 자동 재검색 갱신 ---
            elif command == 'db_refresh_auto_search':
                code = arg1
                cached_json = ModuleMetaDb.get_metadata(code, category=self.category)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})

                ui_code = cached_json.get('originaltitle') or cached_json.get('title') or code
                logger.info(f"[{self.name}] 전체 우선순위 자동 재검색 갱신 시작: [{code}] ➔ 키워드: '{ui_code}'")

                search_res = self.search(ui_code, manual=False, use_db=False)
                if not search_res:
                    return jsonify({'ret': 'warning', 'msg': '검색 결과가 없습니다.'})

                best_item = next((item for item in search_res if not item.get('is_db_cached') and item.get('score', 0) >= 90), None)
                if not best_item:
                    return jsonify({'ret': 'warning', 'msg': '일치하는 메타데이터를 찾지 못했습니다.'})

                new_code = best_item['code']
                logger.info(f"[{self.name}] 자동 재검색 채택: [{best_item.get('site_key', '').upper()}] Code: {new_code} (기존: {code})")

                try:
                    self.keyword_cache.set(f"BYPASS_{new_code}", "1")
                    self.keyword_cache.set(f"BYPASS_{code}", "1")
                except: pass

                fresh_data = self.info(new_code, keyword=ui_code, skip_trans=False)
                if fresh_data:
                    # 기존 출처 데이터는 보존하고, 신규/갱신 메타데이터를 DB에 확정 저장
                    ModuleMetaDb.save_metadata(self.category, fresh_data)

                    title_log = fresh_data.get('title', '')
                    if new_code != code:
                        logger.info(f"[{self.name}] 자동 재검색 신규 출처 메타 저장 완료 ({code} ➔ {new_code}): {title_log}")
                    else:
                        logger.info(f"[{self.name}] [{new_code}] 자동 재검색 기존 메타 갱신 완료: {title_log}")

                    return jsonify({'ret': 'success', 'msg': f"[{ui_code}] 메타데이터가 성공적으로 갱신되었습니다."})
                else:
                    return jsonify({'ret': 'warning', 'msg': '정보 조회에 실패했습니다.'})

            # --- 배우 검색/선택 명령 ---
            elif command == 'person_search':
                kw = arg1 or ''
                domain = arg2 or 'JAV'
                results = ModuleMetaDb.person_search(kw, domain=domain)
                return jsonify({'ret': 'success', 'data': results})

            elif command == 'person_web_list':
                return jsonify(ModuleMetaDb.person_web_list(req))

            elif command == 'person_save':
                p_data = json.loads(arg1) if arg1 else {}
                success, msg = ModuleMetaDb.person_save(p_data)
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == 'person_crop_save':
                meta_module = P.get_module('meta_db')
                if meta_module:
                    return meta_module.process_command(command, arg1, arg2, arg3, req)
                return jsonify({'ret': 'error', 'msg': 'meta_db 모듈을 찾을 수 없습니다.'})

            elif command == 'person_delete':
                success = ModuleMetaDb.person_delete(arg1)
                return jsonify({'ret': 'success' if success else 'error'})

            elif command == 'person_clear':
                domain = arg1 or 'WESTERN'
                success, count = ModuleMetaDb.person_clear_db(domain)
                return jsonify({'ret': 'success' if success else 'error', 'msg': f'{count}건 삭제 완료' if success else '인물 DB 초기화 실패'})

            elif command == 'person_sync_jav_actors':
                success, msg = ModuleMetaDb.sync_jav_actors_db()
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == 'person_version_status':
                _, detected_ver = ModuleMetaDb.find_latest_jav_actors_db()
                file_ver = P.ModelSetting.get("person_jav_file_version") or detected_ver or "0"
                last_ver = P.ModelSetting.get("person_jav_last_synced_version") or "0"
                version_info = f"파일 버전: {file_ver} / DB 반영 버전: {last_ver}"
                return jsonify({'ret': 'success', 'version_info': version_info, 'file_version': file_ver, 'db_version': last_ver})

            elif command in ['person_sub_set_master', 'person_sub_split', 'person_crop_save']:
                meta_module = P.get_module('meta_db')
                if meta_module:
                    return meta_module.process_command(command, arg1, arg2, arg3, req)
                return jsonify({'ret': 'error', 'msg': 'meta_db 모듈을 찾을 수 없습니다.'})

            elif command == 'get_meta_by_code':
                meta_module = P.get_module('meta_db')
                if meta_module:
                    return meta_module.process_command('get_meta_by_code', arg1, arg2, arg3, req)
                return jsonify({'ret': 'error', 'msg': 'meta_db 모듈을 찾을 수 없습니다.'})

            elif command in ['make_preview_clip', 'delete_preview_clip']:
                meta_module = P.get_module('meta_db')
                if meta_module:
                    return meta_module.process_command(command, arg1, arg2, arg3, req)
                return jsonify({'ret': 'error', 'msg': 'meta_db 모듈을 찾을 수 없습니다.'})

            return jsonify(ret)
                
        except Exception as e:
            P.logger.error(f"[{self.name}] Exception: {str(e)}")
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})

    # endregion PluginModuleBase 메서드 오버라이드
    ################################################

    ################################################
    # region SEARCH & INFO

    def search(self, keyword, manual=False, media_path=None, use_db=True):
        target_video_file = media_path
        if not target_video_file and os.path.isabs(keyword) and os.path.exists(keyword):
            target_video_file = keyword
            cleaned_keyword = self._clean_search_keyword(os.path.splitext(os.path.basename(keyword))[0])
        else:
            cleaned_keyword = self._clean_search_keyword(keyword)

        logger.info(f"======= Western search START - keyword:[{cleaned_keyword}] video:[{target_video_file}] manual:[{manual}] use_db:[{use_db}] =======")
        all_results = []
        
        # 1. Local DB 캐시 선행 검색 (전역 meta_db_use 기준)
        has_db_match = False
        if use_db and P.ModelSetting.get_bool("meta_db_use"):
            try:
                target_hash = None
                if target_video_file and os.path.exists(target_video_file):
                    target_hash = SiteAvBase.calculate_oshash(target_video_file)
                elif re.match(r'^[0-9a-fA-F]{16}$', keyword.strip()):
                    target_hash = keyword.strip().lower()

                if target_hash:
                    sess, _, _ = ModuleMetaDb.get_session_and_domain(self.category)
                    from .mod_meta_db import MetaItem
                    try:
                        db_items = sess.query(MetaItem).filter(MetaItem.category == self.category).all()
                        for m in db_items:
                            e_info = m.extra_info or {}
                            hash_matched = False
                            m_oshash = e_info.get('oshash') or e_info.get('hash')
                            m_phash = e_info.get('phash')
                            if (m_oshash and str(m_oshash).lower() == target_hash.lower()) or (m_phash and str(m_phash).lower() == target_hash.lower()):
                                hash_matched = True

                            if not hash_matched and isinstance(e_info.get('fingerprints'), list):
                                for fp_item in e_info['fingerprints']:
                                    if isinstance(fp_item, dict) and str(fp_item.get('hash', '')).lower() == target_hash.lower():
                                        hash_matched = True
                                        break

                            if hash_matched:
                                logger.info(f"[{self.name}] Local DB Fingerprint Match Hit! (Hash: {target_hash})")
                                meta_dict = ModuleMetaDb.to_entity_dict(m)
                                db_item = self._create_search_item_from_dict(meta_dict, 105)
                                item_dict = db_item.as_dict()
                                item_dict['original_score'] = 105
                                item_dict['is_db_cached'] = True
                                all_results.append(item_dict)
                                
                    finally:
                        sess.remove()

                valid_db_records = ModuleMetaDb.search_for_auto_match(self.category, cleaned_keyword)
                if valid_db_records:
                    for record in valid_db_records:
                        db_item = self._create_search_item_from_dict(record['json_data'], 105)
                        item_dict = db_item.as_dict()
                        item_dict['original_score'] = 105
                        item_dict['is_db_cached'] = True
                        all_results.append(item_dict)

                if any(x.get('original_score', 0) >= 100 for x in all_results):
                    has_db_match = True
                    logger.info(f"[{self.name}] Local DB 일치 확인 ({len(all_results)}건)")

            except Exception as e_db:
                logger.error(f"[{self.name}] DB Search Error: {e_db}")

        skip_external_search = (has_db_match and not manual and use_db)
        site_order_list = [s.strip().lower() for s in P.ModelSetting.get_list(f"{self.name}_order", ",") if s.strip()]

        if not skip_external_search:
            early_exit_triggered = False

            for idx, site_key in enumerate(site_order_list):
                if early_exit_triggered: break
                SiteClass = self.site_map.get(site_key)
                if not SiteClass: continue

                try:
                    data = SiteClass.search(cleaned_keyword, manual=manual, media_path=target_video_file, filename=target_video_file)
                    if data and data.get("ret") == "success" and data.get("data"):
                        results = data["data"]
                        for item in results:
                            item['site_key'] = site_key
                            all_results.append(item)
                            
                            if not manual and item.get('score', 0) >= 100:
                                logger.info(f"[{self.name}] '{site_key}'에서 100점 매칭 확정: {cleaned_keyword}")
                                early_exit_triggered = True
                                break

                except Exception as e_site:
                    logger.error(f"[{self.name}] Error searching on {site_key}: {e_site}")

        # 3. 우선순위 정렬 및 동점자 분리
        if all_results:
            priority_map = {site: idx for idx, site in enumerate(site_order_list)}
            default_prio = len(site_order_list)

            all_results_sorted = sorted(
                all_results,
                key=lambda x: (-int(x.get('score', 0)), priority_map.get(x.get('site_key', '').lower(), default_prio))
            )

            for i, item in enumerate(all_results_sorted):
                raw_score = int(round(item.get('score', 0)))
                raw_score = max(0, min(100, raw_score))

                if i == 0:
                    item['score'] = raw_score
                else:
                    prev_score = all_results_sorted[i-1]['score']
                    item['score'] = max(0, prev_score - 1) if raw_score >= prev_score else raw_score

                if manual:
                    try: self.keyword_cache.set(f"BYPASS_{item['code']}", "1")
                    except Exception:
                        if not hasattr(self, 'keyword_cache'): self.keyword_cache = {}
                        self.keyword_cache[f"BYPASS_{item['code']}"] = "1"

            all_results = all_results_sorted
            logger.info(f"[{self.name}] 최종 검색 결과(우선순위 정렬 완료, 총 {len(all_results)}건):")
            for idx, item_log in enumerate(all_results[:10]):
                logger.info(f"  {idx+1}. [{item_log.get('site_key', '').upper()}] 점수={item_log.get('score')} | UI={item_log.get('ui_code')} | Title='{item_log.get('title')}'")
        else:
            logger.info(f"======= Western search END - No results found: {cleaned_keyword} =======")

        return all_results

    def _create_search_item_from_dict(self, jd, score):
        db_item = EntityAVSearch(jd.get('site', 'stashdb'))
        db_item.code = jd.get('code')
        db_item.ui_code = jd.get('ui_code') or jd.get('originaltitle') or jd.get('code')
        db_item.title = f"📁 [Meta DB] {jd.get('title', '')}"
        db_item.originaltitle = jd.get('originaltitle', '')
        db_item.title_ko = db_item.title
        try: db_item.year = int(jd.get('year', 1900))
        except: db_item.year = 1900
        
        poster_url = ""
        for t in jd.get('thumb', []):
            if isinstance(t, dict) and t.get('aspect') == 'poster':
                poster_url = t.get('value', '')
                break
        db_item.image_url = poster_url
        
        studio_str = jd.get('studio', 'Unknown')
        actor_names = [(a.get('name_ko') or a.get('name_org', '')) if isinstance(a, dict) else str(a) for a in jd.get('actor', []) if a]
        actor_str = ", ".join(actor_names[:3]) if actor_names else "배우 정보 없음"
        premiered_str = jd.get('premiered', '') or (str(db_item.year) if db_item.year != 1900 else '미상')
        plot_snippet = (jd.get('plot', '')[:120] + "...") if len(jd.get('plot', '')) > 120 else (jd.get('plot', '') or "줄거리 없음")

        db_item.desc = f"스튜디오: {studio_str} | 출시: {premiered_str} | 출연: {actor_str}\n{plot_snippet}"
        db_item.score = score
        db_item.content_type = jd.get('content_type', 'scene')
        return db_item

    def _clean_search_keyword(self, keyword):
        cleaned = keyword
        cleaned = re.sub(r'^\[[^\]]+\]\s*', '', cleaned)
        cleaned = re.sub(r'[\-_.]', ' ', cleaned)

        regex_string_1st = P.ModelSetting.get(f"{self.name}_search_regex_removal")
        if regex_string_1st and regex_string_1st.strip():
            patterns = [p.strip() for p in regex_string_1st.split('\n') if p.strip()]
            for pattern in patterns:
                try: cleaned = re.sub(pattern, ' ', cleaned, flags=re.IGNORECASE).strip()
                except Exception as e: logger.error(f"[{self.name}] 1차 정규식 오류 '{pattern}': {e}")
                    
        regex_string_2nd = P.ModelSetting.get(f"{self.name}_search_regex_removal_2nd")
        if regex_string_2nd and regex_string_2nd.strip():
            patterns = [p.strip() for p in regex_string_2nd.split('\n') if p.strip()]
            for pattern in patterns:
                try: cleaned = re.sub(pattern, ' ', cleaned, flags=re.IGNORECASE).strip()
                except Exception as e: logger.error(f"[{self.name}] 2차 정규식 오류 '{pattern}': {e}")

        return re.sub(r'\s+', ' ', cleaned).strip()

    def search2(self, keyword, site, manual=False):
        SiteClass = self.site_map.get(site)
        if SiteClass:
            cleaned_keyword = self._clean_search_keyword(keyword)
            res = SiteClass.search(cleaned_keyword, manual=manual)
            if res and res.get("ret") == "success" and res.get("data"):
                return res["data"]
        return None

    def info(self, code, keyword=None, extra_opts=None, **kwargs):
        opts = dict(extra_opts or {})
        opts.update(kwargs)

        skip_trans = opts.get('skip_trans', False)
        media_path = opts.get('media_path', None)

        if len(code) < 3 or code[0] != 'W':
            logger.error(f"[{self.name}] 처리할 수 없는 코드: {code}")
            return None

        site_key = 'stashdb' if code[1] == 'S' else 'tpdb'
        SiteClass = self.site_map.get(site_key)
        if not SiteClass:
            logger.error(f"[{self.name}] 사이트 인스턴스 없음: {site_key}")
            return None

        bypass_cache = False
        if not hasattr(self, 'keyword_cache'): self.keyword_cache = {}
        try:
            if self.keyword_cache.get(f"BYPASS_{code}") == "1":
                bypass_cache = True
                self.keyword_cache.set(f"BYPASS_{code}", "0")
        except Exception: pass

        use_db = P.ModelSetting.get_bool("meta_db_use")
        save_db = P.ModelSetting.get_bool("meta_db_save")
        
        # 캐시 히트 시: DB 마스터는 보존하고 반환 직전 임시 가공 적용
        if use_db and not bypass_cache:
            cached_json = ModuleMetaDb.get_metadata(code, category=self.category)

            if cached_json:
                is_db_untranslated = False
                db_plot = cached_json.get('plot', '')
                if db_plot and not skip_trans:
                    if not SiteUtil.is_include_hangul(db_plot):
                        is_db_untranslated = True

                if not is_db_untranslated:
                    logger.info(f"[{self.name}] DB 캐시 로드: {code} -> {cached_json.get('title', '')}")

                    include_male_opt = P.ModelSetting.get_bool(f"{self.name}_json_include_male")
                    if include_male_opt is None:
                        include_male_opt = P.ModelSetting.get_bool(f"{self.name}_include_male") or False

                    if not include_male_opt and cached_json.get('actor'):
                        females_only = [a for a in cached_json['actor'] if str(a.get('gender')).lower() == 'female']
                        if females_only:
                            cached_json['actor'] = females_only

                    return MetaResponseUtil.finalize_info_return(cached_json, extra_opts=opts, category=self.category)

        data = None
        try:
            scrape_opts = {'skip_trans': skip_trans, 'media_path': media_path}
            data = SiteClass.info(code, extra_opts=scrape_opts)
        except Exception as e:
            logger.exception(f"[{self.name}] Info 조회 중 오류: {e}")
            return None

        if not data or data.get("ret") != "success" or not data.get("data"):
            logger.warning(f"[{self.name}] Info 조회 실패: {code}")
            return None

        ret = data["data"]
        ret["plex_is_proxy_preview"] = True
        ret["plex_is_landscape_to_art"] = True
        ret["plex_art_count"] = len(ret.get("fanart", []))

        original_calculated_title = ret.get("title", "")
        safe_studio = ret.get("studio", "Unknown")
        type_char = code[2] if len(code) > 2 else 'S'
        content_type = 'movie' if type_char == 'M' else 'scene'

        # 서양 배우 이미지는 전역 설정 기준대로 로컬 디스크에 정상 보관
        if ret.get('actor'):
            is_img_srv = P.ModelSetting.get(f"{self.name}_image_mode") == 'image_server'
            actor_img_mode = P.ModelSetting.get(f"{self.name}_actor_image_mode") or 'site'
            save_actor_enabled = is_img_srv and (actor_img_mode == 'image_server')

            for a_item in ret['actor']:
                try:
                    raw_thumb = a_item.get('thumb') if isinstance(a_item, dict) else getattr(a_item, 'thumb', '')
                    if raw_thumb:
                        if isinstance(a_item, dict):
                            if not a_item.get('extra_info'): a_item['extra_info'] = {}
                            a_item['extra_info']['site_img_url'] = raw_thumb
                            a_item['site_img_url'] = raw_thumb
                        elif hasattr(a_item, 'extra_info'):
                            if not a_item.extra_info: a_item.extra_info = {}
                            a_item.extra_info['site_img_url'] = raw_thumb
                            setattr(a_item, 'site_img_url', raw_thumb)

                    if save_actor_enabled:
                        SiteAvBase.save_western_actor_image(a_item)
                except Exception as e_act_img:
                    logger.debug(f"[{self.name}] 배우 이미지 처리 예외: {e_act_img}")

        # --- DB 저장용 완전체 배우 목록과 최종 반환용 배우 목록 분리 ---
        actors_for_db_save = []
        for a_obj in ret.get('actor', []):
            if isinstance(a_obj, dict):
                actors_for_db_save.append({
                    'name_org': a_obj.get('name_org', ''),
                    'name_ko': a_obj.get('name_ko', ''),
                    'name_en': a_obj.get('name_en', ''),
                    'thumb': a_obj.get('thumb', ''),
                    'actor_idx': a_obj.get('actor_idx', '') or a_obj.get('person_idx', ''),
                    'role': a_obj.get('role', '출연'),
                    'gender': (a_obj.get('extra_info') or {}).get('gender') or a_obj.get('gender', ''),
                    'extra_info': a_obj.get('extra_info', {})
                })
            else:
                extra_d = getattr(a_obj, 'extra_info', {}) or {}
                actors_for_db_save.append({
                    'name_org': getattr(a_obj, 'name_org', ''),
                    'name_ko': getattr(a_obj, 'name_ko', ''),
                    'name_en': getattr(a_obj, 'name_en', ''),
                    'thumb': getattr(a_obj, 'thumb', ''),
                    'actor_idx': getattr(a_obj, 'actor_idx', '') or getattr(a_obj, 'person_idx', ''),
                    'role': getattr(a_obj, 'role', '출연'),
                    'gender': extra_d.get('gender') or getattr(a_obj, 'gender', ''),
                    'extra_info': extra_d
                })

        ret['actor'] = actors_for_db_save

        # 타이틀 포맷에 사용할 배우명 추출 (한국어 표기가 있으면 한국어, 없으면 원문명)
        include_male_opt = P.ModelSetting.get_bool(f"{self.name}_json_include_male")
        if include_male_opt is None:
            include_male_opt = P.ModelSetting.get_bool(f"{self.name}_include_male") or False

        females_for_title = [(a['name_ko'] or a['name_org']) for a in actors_for_db_save if str(a.get('gender')).lower() == 'female' and (a.get('name_ko') or a.get('name_org'))]
        males_for_title = [(a['name_ko'] or a['name_org']) for a in actors_for_db_save if str(a.get('gender')).lower() != 'female' and (a.get('name_ko') or a.get('name_org'))]

        if include_male_opt:
            selected_names_for_title = (females_for_title + males_for_title)[:3]
        else:
            selected_names_for_title = females_for_title[:3] if females_for_title else males_for_title[:3]

        actor_str = ", ".join(selected_names_for_title) if selected_names_for_title else ""
        year_val = ret.get("year", "")
        if not year_val and ret.get("premiered"):
            year_val = str(ret.get("premiered"))[:4]

        studio_code = ""
        raw_code_candidate = ret.get('original', {}).get('code') or ""
        if not raw_code_candidate and original_calculated_title:
            match_code = re.search(r'\b([a-zA-Z0-9]{2,8}[-_]\d{2,7}|[a-zA-Z]{2,6}\d{3,5})\b', original_calculated_title)
            if match_code: raw_code_candidate = match_code.group(0)

        if raw_code_candidate:
            uncen_parsed = SiteAvBase._parse_ui_code_uncensored(raw_code_candidate)
            if uncen_parsed and '-' in uncen_parsed and not uncen_parsed.startswith(raw_code_candidate.upper()):
                studio_code = uncen_parsed.upper()
            else:
                cen_parsed, _, _ = SiteAvBase._parse_ui_code(raw_code_candidate)
                studio_code = cen_parsed.upper() if cen_parsed and '-' in cen_parsed else (uncen_parsed.upper() if uncen_parsed else raw_code_candidate.upper())

        # 장르 번역
        if ret.get('genre'):
            translated_genres = []
            for g in ret['genre']:
                t_g = SiteAvBase.get_translated_tag(g)
                if t_g and t_g not in translated_genres: translated_genres.append(t_g)
            ret['genre'] = translated_genres

        trans_title_enabled = P.ModelSetting.get_bool(f"{self.name}_trans_title")
        if trans_title_enabled is None: trans_title_enabled = True

        translated_title = ret.get("tagline") if trans_title_enabled else original_calculated_title
        effective_title = translated_title or original_calculated_title

        format_dict = {
            'originaltitle': ret.get("originaltitle", "") or original_calculated_title,
            'plot': ret.get("plot", ""),
            'title': effective_title,
            'studio': safe_studio,
            'year': year_val,
            'actor': actor_str,
            'tagline': effective_title,
            'code': studio_code,
            'ui_code': studio_code
        }

        use_movie_format = P.ModelSetting.get_bool(f"{self.name}_use_movie_title_format")
        if site_key == 'tpdb' and content_type == 'movie' and use_movie_format:
            title_format = P.ModelSetting.get(f"{self.name}_movie_title_format") or "[{studio}] {title}"
        else:
            title_format = P.ModelSetting.get(f"{self.name}_title_format") or "[{studio}] {actor} - {title}"

        try:
            final_title = title_format.format(**format_dict)
            final_title = re.sub(r'\[([^\]]+)\]\s*-\s*', r'[\1] ', final_title)
            final_title = re.sub(r'\s*-\s*$', '', final_title)
            final_title = re.sub(r'^\s*-\s*', '', final_title)
            final_title = re.sub(r'(\s*-\s*){2,}', ' - ', final_title)
            final_title = re.sub(r'\s+', ' ', final_title).strip()

            ret["title"] = final_title
            clean_sort_title = re.sub(r'[\[\]\-_]', ' ', final_title)
            ret["sorttitle"] = re.sub(r'\s+', ' ', clean_sort_title).strip()
            ret["originaltitle"] = original_calculated_title
            ret["tagline"] = ret.get("tagline") or final_title

            if ret.get('extras'):
                for extra in ret['extras']:
                    if isinstance(extra, dict) and extra.get('content_type') == 'trailer':
                        extra['title'] = final_title
        except Exception as e_fmt:
            ret["title"] = original_calculated_title

        # 태그(컬렉션) 옵션
        tag_option = P.ModelSetting.get(f"{self.name}_tag_option")
        ret["tag"] = []
        if tag_option != "not_using":
            safe_studio = ret.get("original", {}).get("studio", "")
            safe_network = ret.get("original", {}).get("network", "")

            if tag_option in ["studio", "studio_network"]:
                if safe_studio and safe_studio != 'Unknown' and safe_studio not in ret["tag"]:
                    ret["tag"].append(safe_studio)
            if tag_option in ["network", "studio_network"]:
                if safe_network and safe_network != 'Unknown' and safe_network not in ret["tag"]:
                    ret["tag"].append(safe_network)

        if ret:
            title_log = ret.get('title', 'No Title')
            year_log = ret.get('year', '????')
            site_log = ret.get('site', 'unknown').upper()
            logger.info(f"[{site_log} Success] Code: {code}, Title: {title_log} ({year_log})")

        # 전달된 동영상 파일 경로를 extra_info에 보관하고 조건 충족 시 프리뷰 클립 자동 생성
        media_path = opts.get('media_path')
        if media_path and os.path.exists(media_path):
            if 'extra_info' not in ret or not isinstance(ret['extra_info'], dict):
                ret['extra_info'] = {}
            ret['extra_info']['source_video_path'] = media_path

            from .util_preview import MetaPreviewUtil
            if MetaPreviewUtil.is_auto_create_enabled(self.category) and not ret.get('extras'):
                code_val = ret.get('code') or code
                cat_val = self.category
                threading.Thread(
                    target=MetaPreviewUtil.process_preview_workflow,
                    args=(code_val, media_path, cat_val),
                    daemon=True
                ).start()
                logger.info(f"[{self.name}] 공식 트레일러 부재 감지 -> 백그라운드 프리뷰 클립 자동 생성 트리거: {code_val}")

        # 메타 DB 단일 저장 (남녀 배우 전원 및 extra_info가 포함된 상태로 저장하여 MetaPerson 구축)
        save_only_trans = P.ModelSetting.get_bool("meta_db_save_only_translated")
        should_save = use_db and save_db and ret
        if should_save:
            if skip_trans:
                should_save = False
            elif save_only_trans:
                plot_val = ret.get('plot', '')
                if plot_val and not SiteUtil.is_include_hangul(plot_val):
                    should_save = False

        if should_save:
            ModuleMetaDb.save_metadata(self.category, ret)

        final_clean_actors = []
        candidate_actors = actors_for_db_save
        if not include_male_opt:
            females_only = [a for a in actors_for_db_save if str(a.get('gender')).lower() == 'female']
            candidate_actors = females_only if females_only else actors_for_db_save

        for act_it in candidate_actors:
            act_name = act_it.get('name') or act_it.get('name_ko') or act_it.get('name_org', '')
            final_clean_actors.append({
                'name': act_name,
                'name_org': act_it.get('name_org', ''),
                'name_ko': act_it.get('name_ko', ''),
                'name_en': act_it.get('name_en', ''),
                'thumb': act_it.get('thumb', ''),
                'actor_idx': act_it.get('actor_idx', ''),
                'role': act_it.get('role', '출연')
            })

        ret['actor'] = final_clean_actors

        logger.info(f"[{self.name}] Info Success: {code} -> {ret['title']} ({ret.get('year', '')})")

        return MetaResponseUtil.finalize_info_return(ret, extra_opts=opts, category=self.category)


    def _finalize_info_return(self, entity_dict, extra_opts=None):
        """호출자에게 메타데이터를 반환하기 직전, 최종 가공(줄거리 폴백 및 옵션 오버라이드)을 적용합니다."""
        if not entity_dict or not isinstance(entity_dict, dict):
            return entity_dict

        import copy
        opts = dict(extra_opts or {})
        res = copy.deepcopy(entity_dict)

        # 줄거리가 비어있으면 부제(tagline)로 동적 폴백 (DB 사용 여부와 무관하게 항상 동작)
        current_plot = str(res.get('plot') or '').strip()
        fallback_tagline = str(res.get('tagline') or '').strip()
        if not current_plot and fallback_tagline:
            res['plot'] = fallback_tagline

        # meta_db 활성화 상태에서 공유 라이브러리 등 임시 오버라이드 요청이 있는 경우에만 위임
        if P.ModelSetting.get_bool("meta_db_use"):
            try:
                res = ModuleMetaDb.apply_transient_overrides(res, opts, category=self.category)
            except Exception as e_override:
                logger.debug(f"[{self.name}] apply_transient_overrides 예외: {e_override}")

        # 이미지 필드 제거 옵션 처리 (공유 라이브러리 전용)
        if opts.get('strip_images'):
            res['poster_url'] = ''
            res['landscape_url'] = ''
            res['thumb'] = []
            res['fanart'] = []

        return res


    # endregion SEARCH & INFO
    ################################################

    ################################################
    # region API & DOWNLOADS

    def process_api(self, sub, req):
        try:
            call = req.args.get("call", "")
            if sub == "search" and call in ["plex", "kodi"]:
                keyword = req.args.get("keyword", "").strip()
                manual = req.args.get("manual") == "True"
                media_path = req.args.get("media_path") or req.args.get("path")
                search_results = self.search(keyword, manual=manual, media_path=media_path)
                return jsonify(search_results)

            if sub == "info":
                code = req.args.get("code")
                data = self.info(code)
                return jsonify(data)

            if sub == "crop_save":
                if req.is_json:
                    body_json = req.get_json(silent=True) or {}
                    code = body_json.get("code")
                    crop_data = body_json.get("crop_data")
                    pl_base64 = body_json.get("pl_base64")
                    p_base64 = body_json.get("p_base64")
                else:
                    code = req.form.get("code") or req.args.get("code")
                    crop_data = req.form.get("crop_data") or req.args.get("crop_data")
                    pl_base64 = req.form.get("pl_base64") or req.args.get("pl_base64")
                    p_base64 = req.form.get("p_base64") or req.args.get("p_base64")

                if isinstance(crop_data, dict): crop_data = json.dumps(crop_data)
                if not code or (not crop_data and not p_base64):
                    return jsonify({'ret': 'error', 'msg': 'code 또는 크롭 데이터 누락'}), 400

                success, result_msg = MetaImageUtil.save_user_cropped_poster(
                    code, crop_data or "{}", pl_image_base64_data=pl_base64, p_image_base64_data=p_base64, category=self.category
                )
                return jsonify({'ret': 'success' if success else 'error', 'msg': result_msg}), (200 if success else 500)

            if sub == "user_image_update":
                return self._api_user_image_update(req)

            return jsonify({'ret': 'failed', 'msg': f'Invalid sub command: {sub}'}), 400
        
        except Exception as e:
            logger.error(f"[{self.name}] Exception in process_api (sub={sub}): {e}")
            return jsonify({'ret': 'exception', 'msg': str(e)}), 500

    def process_normal(self, sub, req):
        def get_download_filename(info, ext, suffix=""):
            safe_studio = info.get('studio', 'Unknown')
            combined_title = f"[{safe_studio}] {info.get('originaltitle', '')}"
            safe_filename = SiteTpdb._make_safe_filename(combined_title)
            scene_id = info.get('code', '')[2:] if len(info.get('code', '')) > 2 else ''
            if scene_id: safe_filename += f"_{scene_id}"
            if suffix: safe_filename += f"_{suffix}"
            return f"{safe_filename}.{ext}"

        if sub == "nfo_download":
            keyword = req.args.get("code")
            call = req.args.get("call")
            if call in self.site_map:
                SiteClass = self.site_map.get(call)
                search_result_dict = SiteClass.search(keyword, manual=True)
                if search_result_dict and search_result_dict.get('ret') == 'success' and search_result_dict.get('data'):
                    real_code = search_result_dict['data'][0]['code']
                    info = self.info(real_code, keyword=keyword)
                    if info:
                        return UtilNfo.make_nfo_movie(info, output="file", filename=get_download_filename(info, "nfo"))

        elif sub == "yaml_download":
            keyword = req.args.get("code")
            call = req.args.get("call")
            if call in self.site_map:
                SiteClass = self.site_map.get(call)
                search_result_dict = SiteClass.search(keyword, manual=True)
                if search_result_dict and search_result_dict.get('ret') == 'success' and search_result_dict.get('data'):
                    real_code = search_result_dict['data'][0]['code']
                    info = self.info(real_code, keyword=keyword)
                    if info:
                        return UtilNfo.make_yaml_movie(info, output="file", filename=get_download_filename(info, "yaml"))

        elif sub == "image_download":
            try:                
                keyword = req.args.get("code")
                call = req.args.get("call")
                image_type = req.args.get("type") 
                
                if call in self.site_map:
                    SiteClass = self.site_map.get(call)
                    search_result_dict = SiteClass.search(keyword, manual=True)
                    if not search_result_dict or search_result_dict.get('ret') != 'success' or not search_result_dict.get('data'):
                        return "Search failed", 404
                    
                    real_code = search_result_dict['data'][0]['code']
                    info = self.info(real_code, keyword=keyword)
                    if not info: return "Info failed", 404

                    target_url = None
                    target_aspect = 'poster' if image_type == 'p' else 'landscape'
                    for thumb in info.get('thumb', []):
                        if thumb.get('aspect') == target_aspect:
                            target_url = thumb.get('value')
                            break
                    
                    if not target_url and image_type == 'pl' and info.get('fanart'):
                        target_url = info['fanart'][0]
                    
                    if not target_url: return f"Image '{image_type}' not found", 404

                    img_res = requests.get(target_url, verify=False, timeout=30)
                    if img_res.status_code != 200: return "Download failed", 500

                    return send_file(BytesIO(img_res.content), as_attachment=True, download_name=get_download_filename(info, "jpg", suffix=image_type), mimetype='image/jpeg')
            except Exception as e:
                return f"Error: {e}", 500

        elif sub == "db_download":
            filename = req.args.get('filename')
            if filename:
                filepath = os.path.join(path_data, 'tmp', filename)
                if os.path.exists(filepath):
                    return send_file(filepath, as_attachment=True, download_name=filename)
            return "File not found.", 404

        return None

    def _api_user_image_update(self, req):
        ret = {'ret': 'success', 'msg': '', 'total_input': 0, 'updated_count': 0, 'errors': []}
        try:
            files = []
            if req.is_json:
                json_body = req.get_json(silent=True) or {}
                if isinstance(json_body, list): files = json_body
                elif isinstance(json_body, dict): files = json_body.get('files') or []
            
            if not files:
                raw_files = req.form.get('files') or req.args.get('files')
                if raw_files:
                    try:
                        p = json.loads(raw_files)
                        files = p if isinstance(p, list) else [p]
                    except:
                        files = [f.strip() for f in re.split(r'[\n,]', raw_files) if f.strip()]

            ret['total_input'] = len(files)
            sess, domain, std_cat = ModuleMetaDb.get_session_and_domain(self.category)
            from .mod_meta_db import MetaItem
            try:
                for f in files:
                    clean_name = os.path.basename(f).strip()
                    stem = re.split(r'_(?:p|pl)(?:_user)?\.', clean_name, flags=re.I)[0]
                    if not stem: continue
                    meta = sess.query(MetaItem).filter(
                        MetaItem.category == std_cat,
                        or_(MetaItem.originaltitle.ilike(stem), MetaItem.code.ilike(stem))
                    ).first()
                    if meta:
                        res, _ = MetaImageUtil.sync_single_record_disk_images(meta)
                        if res == 'updated': ret['updated_count'] += 1
                sess.commit()
                ModuleMetaDb.checkpoint_wal()
            finally:
                sess.remove()

            return jsonify(ret), 200
        except Exception as e:
            ret['ret'] = 'error'; ret['msg'] = str(e)
            return jsonify(ret), 200

    # endregion API & DOWNLOADS
    ################################################
