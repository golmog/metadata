# -*- coding: utf-8 -*-
import os
import re
import shutil
import traceback
import threading
import time
import json
from io import BytesIO
from urllib.parse import urlparse

from flask import send_from_directory, send_file, jsonify, Response, abort
import requests
from sqlalchemy import or_

from support_site import (
    SiteAvBase,
    Site1PondoTv,
    Site10Musume,
    SitePaco,
    SiteCarib,
    SiteHeyzo,
    SiteFc2com,
    SiteAvdbs,
    SiteUtil,
    UtilNfo,
)
from support_site.entity_av import EntityAVSearch

from .setup import *
from .mod_meta_db import ModuleMetaDb
from .util_metadata import MetaImageUtil, MetaWorkerUtil, MetaResponseUtil


class ModuleJavUncensored(PluginModuleBase):

    def __init__(self, P):
        super(ModuleJavUncensored, self).__init__(P, name='jav_uncensored', first_menu='setting')
        self.category = 'JAV_UNCEN'
        self.web_list_model = None
        self.site_map = {
            "1pondo": {
                "instance": Site1PondoTv,
                "keyword": ["1pon"],
                "regex": r"(1pon|1pondo)-(?P<code>\d{6}_\d{2,3})",
            },
            "10musume": {
                "instance": Site10Musume,
                "keyword": ["10mu"],
                "regex": r"(10mu|10musume)-(?P<code>\d{6}_\d{2})",
            },
            "paco": {
                "instance": SitePaco,
                "keyword": ["paco", "pacopacom", "pacopacomama"],
                "regex": r"(paco|pacopacom|pacopacomama)-(?P<code>\d{6}_\d{3})",
            },
            "heyzo": {
                "instance": SiteHeyzo,
                "keyword": ["heyzo"],
                "regex": r"heyzo-(?P<code>\d{4})",
            },
            "carib": {
                "instance": SiteCarib,
                "keyword": ["carib", "caribbeancom"],
                "regex": r"(carib|caribbeancom)-(?P<code>\d{6}-\d{3})",
            },
            "fc2com": {
                "instance": SiteFc2com,
                "keyword": ["fc2", "fc2-ppv"],
                "regex": r"(fc2|fc2-ppv)-(?P<code>\d{5,7})",
            },
        }

        self.db_default = {
            f"{self.name}_db_version": "1",

            f"{self.name}_image_mode": "image_server",
            f"{self.name}_image_server_save_format": "/jav/uncen/{label}",

            f'{self.name}_1pondo_use_proxy': 'False',
            f'{self.name}_1pondo_proxy_url': '',
            f'{self.name}_1pondo_test_code': '092121_001',

            f'{self.name}_10musume_use_proxy': 'False',
            f'{self.name}_10musume_proxy_url': '',
            f'{self.name}_10musume_test_code': '010620_01',

            f'{self.name}_paco_use_proxy': 'False',
            f'{self.name}_paco_proxy_url': '',
            f'{self.name}_paco_test_code': '111825_100',

            f'{self.name}_heyzo_use_proxy': 'False',
            f'{self.name}_heyzo_proxy_url': '',
            f'{self.name}_heyzo_test_code': '2681',

            f'{self.name}_carib_use_proxy': 'False',
            f'{self.name}_carib_proxy_url': '',
            f'{self.name}_carib_test_code': '062015-904',

            f'{self.name}_fc2com_use_fc2_com': 'True',
            f'{self.name}_fc2com_use_proxy': 'False',
            f'{self.name}_fc2com_proxy_url': '',
            f'{self.name}_fc2com_test_code': '3669846',
            
            f'{self.name}_fc2com_use_javten_web': 'True',
            f'{self.name}_fc2com_use_javten_proxy': 'False',
            f'{self.name}_fc2com_javten_proxy_url': '',

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
                for s in self.site_map.keys():
                    if f"_{s}_" in k:
                        submitted_prefixes.add(f"_{s}_")

            for key, default_val in self.db_default.items():
                if default_val in ['True', 'False'] and key not in form_keys:
                    should_turn_off = False
                    if '_db_' in key and '_db_' in submitted_prefixes:
                        should_turn_off = True
                    else:
                        for s in self.site_map.keys():
                            if f"_{s}_" in key and f"_{s}_" in submitted_prefixes:
                                should_turn_off = True
                                break

                    if should_turn_off and P.ModelSetting.set(key, 'False'):
                        change_list.append(key)

            self.setting_save_after(change_list)
            return jsonify(True)
        except Exception as e:
            logger.error(f"[{self.name}] setting_save 에러: {e}")
            return jsonify(False)

    def setting_save_after(self, change_list):
        ins_list = []
        if any(key.startswith('jav_censored_') for key in change_list):
            ins_list = [v['instance'] for v in self.site_map.values()]
        else:
            for key in change_list:
                if key.endswith("_test_code"): continue
                if key.startswith(self.name):
                    for site, site_info in self.site_map.items():
                        if site in key:
                            instance = site_info['instance']
                            if instance not in ins_list:
                                ins_list.append(instance)

        if ins_list:
            self._set_site_setting(ins_list)

    def _set_site_setting(self, ins_list=None):
        if ins_list is None:
            ins_list = [v['instance'] for v in self.site_map.values()]

        censored_module = P.get_module('jav_censored')
        jav_settings = censored_module.get_jav_settings() if censored_module else {}

        SiteAvBase.set_yaml_settings(jav_settings)
        SiteAvBase.set_config(self.P.ModelSetting)

        for ins in ins_list:
            try:
                ins.set_config(P.ModelSetting)
            except Exception as e:
                P.logger.error(f"Error initializing site {ins.__name__}: {str(e)}")
                P.logger.error(traceback.format_exc())

    def process_ajax(self, sub, req):
        try:
            command = req.form.get('command')
            arg1 = req.form.get('arg1', '') or ''
            arg2 = req.form.get('arg2', '') or ''
            arg3 = req.form.get('arg3', '') or ''
            list_type = (req.form.get('list_type') or '').strip().lower()

            # 인물(배우) DB 요청 판별 및 처리
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
                    default_dom = req.form.get('search_domain', 'JAV') or 'JAV'
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
                category = req.form.get('category') or getattr(self, 'category', 'JAV_UNCEN')
                if str(category).upper() == 'PERSON':
                    return jsonify(ModuleMetaDb.person_web_list(req, default_domain=req.form.get('search_domain', 'JAV')))
                return jsonify(ModuleMetaDb.web_list(req, category=category))

            # 백그라운드 상태 폴링
            if command == 'db_enrich_status':
                return jsonify({'ret': 'success', 'data': self.enrich_status})
            if command == 'db_sync_status':
                return jsonify({'ret': 'success', 'data': self.sync_status})

            # 모듈 커맨드 우선 처리
            if command:
                res = self.process_command(command, arg1, arg2, arg3, req)
                if res is not None:
                    return res

            # 프레임워크 기본 AJAX 처리
            res = super(ModuleJavUncensored, self).process_ajax(sub, req)
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

            # --- 포스터 수동 크롭 저장 (MetaImageUtil 위임) ---
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
                code = arg2
                call = arg1
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_code", code)

                site_info = self.site_map.get(call)
                if not site_info or not site_info.get('instance'):
                    return jsonify({'ret': 'error', 'msg': f"Site '{call}' not found."})

                site_instance = site_info['instance']
                search_code = code
                if site_info.get('keyword'):
                    prefix = site_info['keyword'][0]
                    if not any(k in code.lower() for k in site_info['keyword']):
                        search_code = f"{prefix}-{code}"

                search_result_dict = site_instance.search(search_code, manual=True)
                if not search_result_dict or search_result_dict.get('ret') != 'success' or not search_result_dict.get('data'):
                    return jsonify({'ret': 'warning', 'msg': f"no results for '{code}' from site '{call}'"})

                search_results = search_result_dict['data']
                info_data = self.info(search_results[0]['code'])

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
                    return jsonify({'ret': 'success' if success else 'error', 'msg': 'DB에 성공적으로 저장되었습니다.' if success else '저장 실패'})
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

                ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
                logger.info(f"[{self.name}] 미디어 전용 재동기화 시작: [{code}] ({ui_code})")

                try: self.keyword_cache.set(f"BYPASS_{code}", "1")
                except: pass

                fresh_media = self.info(code, skip_trans=True)
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
                    logger.info(f"[{self.name}] [{code}] 이미지 및 팬아트 주소 갱신 완료 (Arts: {len(cached_json.get('fanart', []))}개, OrigArts: {len(cached_json.get('original', {}).get('fanart', []))}개)")
                    return jsonify({'ret': 'success', 'msg': f"[{ui_code}] 이미지 및 미디어 동기화 완료"})
                else:
                    return jsonify({'ret': 'warning', 'msg': f"[{ui_code}] 미디어 정보를 가져오지 못했습니다."})

            # 현재 사이트 정보 제자리 갱신
            elif command == 'db_refresh_in_place':
                code = arg1
                cached_json = ModuleMetaDb.get_metadata(code, category=self.category)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})

                ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
                logger.info(f"[{self.name}] 현재 사이트 제자리 갱신 시작: [{code}] ({ui_code})")

                try: self.keyword_cache.set(f"BYPASS_{code}", "1")
                except: pass

                fresh_data = self.info(code, skip_trans=False)
                if fresh_data:
                    # 사용자 명시적 갱신 요청이므로 설정과 무관하게 강제 DB 저장
                    ModuleMetaDb.save_metadata(self.category, fresh_data)
                    title_log = fresh_data.get('title', '')
                    logger.info(f"[{self.name}] [{code}] 메타데이터 및 팬아트 갱신 완료: {title_log}")
                    return jsonify({'ret': 'success', 'msg': f"[{ui_code}] 메타데이터 갱신 완료!\n{title_log}"})
                else:
                    return jsonify({'ret': 'warning', 'msg': f"[{ui_code}] 최신 정보를 가져오지 못했습니다."})

            # --- 자동 재검색 갱신 ---
            elif command == 'db_refresh_auto_search':
                code = arg1
                cached_json = ModuleMetaDb.get_metadata(code, category=self.category)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})

                ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
                logger.info(f"[{self.name}] 전체 우선순위 자동 재검색 갱신 시작: [{code}] ➔ 키워드: '{ui_code}'")

                search_res = self.search(ui_code, manual=False, use_db=False)
                if not search_res:
                    return jsonify({'ret': 'warning', 'msg': f"'{ui_code}' 검색 결과가 없습니다."})

                best_item = next((item for item in search_res if not item.get('is_db_cached') and item.get('score', 0) >= 90), None)
                if not best_item:
                    return jsonify({'ret': 'warning', 'msg': f"[{ui_code}] 일치하는 메타데이터를 찾지 못했습니다."})

                new_code = best_item['code']
                logger.info(f"[{self.name}] 자동 재검색 채택: [{best_item.get('site_key', '').upper()}] Code: {new_code} (기존: {code})")

                try:
                    self.keyword_cache.set(f"BYPASS_{new_code}", "1")
                    self.keyword_cache.set(f"BYPASS_{code}", "1")
                except: pass

                fresh_data = self.info(new_code, skip_trans=False)
                if fresh_data:
                    # 기존 출처 데이터는 보존하고, 신규/갱신 메타데이터를 DB에 확정 저장
                    ModuleMetaDb.save_metadata(self.category, fresh_data)

                    title_log = fresh_data.get('title', '')
                    if new_code != code:
                        logger.info(f"[{self.name}] 자동 재검색 신규 출처 메타 저장 완료 ({code} ➔ {new_code}): {title_log}")
                    else:
                        logger.info(f"[{self.name}] [{new_code}] 자동 재검색 기존 메타 갱신 완료: {title_log}")

                    return jsonify({'ret': 'success', 'msg': f"[{ui_code}] 자동 재검색 갱신 완료!\n{title_log}"})
                else:
                    return jsonify({'ret': 'warning', 'msg': f"[{ui_code}] 최신 정보를 가져오지 못했습니다."})

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
                domain = arg1 or 'JAV'
                success, count = ModuleMetaDb.person_clear_db(domain)
                return jsonify({'ret': 'success' if success else 'error', 'msg': f'{count}건 삭제 완료' if success else '인물 DB 초기화 실패'})

            elif command == 'person_sync_jav_actors':
                success, msg = ModuleMetaDb.sync_jav_actors_db()
                file_ver = P.ModelSetting.get("person_jav_file_version") or "0"
                last_ver = P.ModelSetting.get("person_jav_last_synced_version") or "0"
                version_info = f"파일 버전: {file_ver} / DB 반영 버전: {last_ver}"
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg, 'version_info': version_info})

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
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})

    def process_api(self, sub, req):
        try:
            call = req.args.get("call", "")
            if sub == "search" and call in ["plex", "kodi"]:
                keyword = req.args.get("keyword", "").rstrip("-").strip()
                manual = req.args.get("manual") == "True"
                return jsonify(self.search(keyword, manual=manual))

            if sub == "info":
                code = req.args.get("code")
                media_path = req.args.get("media_path") or req.args.get("path")
                data = self.info(code, extra_opts={'media_path': media_path} if media_path else None)
                if call == "kodi" and data:
                    data = SiteUtil.info_to_kodi(data)
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

                if isinstance(crop_data, dict):
                    crop_data = json.dumps(crop_data)

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
            logger.error(f"Exception in process_api (sub={sub}): {e}")
            return jsonify({'ret': 'exception', 'msg': str(e)}), 500

    def process_normal(self, sub, req):
        if sub == "nfo_download":
            keyword = req.args.get("code")
            call = req.args.get("call")
            if call in self.site_map:
                search_results = self.search2(keyword, call)
                if search_results:
                    try: self.keyword_cache.set(search_results[0]['code'], keyword)
                    except AttributeError: self.keyword_cache[search_results[0]['code']] = keyword
                    
                    info = self.info(search_results[0]["code"])
                    if info:
                        return UtilNfo.make_nfo_movie(info, output="file", filename=info["originaltitle"].upper() + ".nfo")

        elif sub == "yaml_download":
            keyword = req.args.get("code")
            call = req.args.get("call")
            if call in self.site_map:
                search_results = self.search2(keyword, call)
                if search_results:
                    try: self.keyword_cache.set(search_results[0]['code'], keyword)
                    except AttributeError: self.keyword_cache[search_results[0]['code']] = keyword

                    info = self.info(search_results[0]["code"])
                    if info:
                        return UtilNfo.make_yaml_movie(info, output="file", filename=f"{info['originaltitle'].upper()}.yaml")

        elif sub == "image_download":
            try:
                keyword = req.args.get("code")
                call = req.args.get("call")
                image_type = req.args.get("type")
                
                if call in self.site_map:
                    search_results = self.search2(keyword, call)
                    if not search_results: return "Search failed", 404
                    
                    real_code = search_results[0]['code']
                    try: self.keyword_cache.set(real_code, keyword)
                    except AttributeError: self.keyword_cache[real_code] = keyword

                    info = self.info(real_code)
                    if not info: return "Info failed", 404

                    target_url = None
                    target_aspect = 'poster' if image_type == 'p' else 'landscape'
                    for thumb in info.get('thumb', []):
                        if thumb.get('aspect') == target_aspect:
                            target_url = thumb.get('value')
                            break
                    
                    if not target_url and image_type == 'pl' and info.get('fanart'):
                        target_url = info['fanart'][0]
                    
                    if not target_url: return f"Image type '{image_type}' not found", 404

                    img_res = requests.get(target_url, verify=False, timeout=30)
                    if img_res.status_code != 200: return "Download failed", 500

                    filename = f"{info['originaltitle'].lower()}_{image_type}.jpg"
                    return send_file(BytesIO(img_res.content), as_attachment=True, download_name=filename, mimetype='image/jpeg')
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
            # 공용 싱글 레코드 디스크 동기화
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

    # endregion PluginModuleBase 메서드 오버라이드
    ################################################     

    ################################################
    # region SEARCH

    def search(self, keyword, manual=False, use_db=True):
        logger.info(f'======= jav uncensored search START - keyword:[{keyword}] manual:[{manual}] use_db:[{use_db}] =======')
        all_results = []
        
        # 1. DB 선행 검색 (전역 meta_db_use 기준)
        if use_db and P.ModelSetting.get_bool("meta_db_use") and not manual:
            try:
                valid_db_records = ModuleMetaDb.search_for_auto_match(self.category, keyword)
                if valid_db_records:
                    for record in valid_db_records:
                        jd = record['json_data']
                        db_item = EntityAVSearch(record['site'])
                        db_item.code = record['code']
                        db_item.ui_code = jd.get('ui_code') or record['originaltitle']
                        db_item.title = f"📁 [Meta DB] {record['title']}"
                        db_item.originaltitle = record['originaltitle']
                        db_item.title_ko = db_item.title
                        try: db_item.year = int(jd.get('year') or 1900)
                        except: db_item.year = 1900
                        db_item.image_url = record['poster_url'] or ''
                        
                        actor_list = jd.get('actor') or []
                        actor_names = [(a.get('name_ko') or a.get('name_org', '')) if isinstance(a, dict) else (a.name_ko or a.name_org) for a in actor_list if a]
                        actor_str = ", ".join(actor_names[:3]) if actor_names else "배우 정보 없음"

                        premiered_str = jd.get('premiered', '') or (str(db_item.year) if db_item.year != 1900 else '미상')
                        raw_plot = str(jd.get('plot') or '')
                        plot_snippet = (raw_plot[:120] + "...") if len(raw_plot) > 120 else (raw_plot or "줄거리 없음")
                        
                        db_item.desc = f"출처: {record['site'].upper()} | 출시: {premiered_str} | 출연: {actor_str}\n{plot_snippet}"
                        db_item.score = 105
                        db_item.content_type = jd.get('content_type', 'unknown')

                        item_dict = db_item.as_dict()
                        item_dict['original_score'] = 105
                        item_dict['site_key'] = record['site']
                        item_dict['is_db_cached'] = True
                        item_dict['is_priority_label_site'] = True 
                        all_results.append(item_dict)

                    if all_results:
                        for item in all_results: item['score'] = min(100, item['score'])
                        logger.info(f"[{self.name}] Auto-match satisfied by Local DB ({len(all_results)}건)")
                        return all_results
                
            except Exception as e_db:
                logger.error(f"[{self.name}] DB Search Error: {e_db}")

        # 2. 직결 라우팅 (Uncensored 고유 방식)
        for site_name, site_info in self.site_map.items():
            if any(k in keyword.lower() for k in site_info['keyword']) or re.search(site_info['regex'], keyword.lower()):
                instance = site_info['instance']
                data = instance.search(keyword, manual=manual)
                if data and data.get('ret') == 'success' and data.get('data'):
                    all_results = data['data']
                    for item in all_results:
                        item['site_key'] = site_name
                break

        # 3. 수동 검색 플래그 캐싱
        if all_results:
            if manual:
                for item in all_results:
                    try: self.keyword_cache.set(f"BYPASS_{item['code']}", "1")
                    except AttributeError: self.keyword_cache[f"BYPASS_{item['code']}"] = "1"

            logger.info(f"[{self.name}] 최종 검색 결과 (총 {len(all_results)}건):")
            for idx, item_log in enumerate(all_results):
                ui_code = item_log.get('ui_code', '미상')
                site_key = item_log.get('site_key', 'unknown').upper()
                score = item_log.get('score')
                code = item_log.get('code')
                raw_title = item_log.get('title', '')
                title_preview = (raw_title[:60] + "...") if len(raw_title) > 60 else raw_title
                logger.info(f"  {idx+1}. [{site_key}] 점수={score} | 품번={ui_code} | Code={code} | Title='{title_preview}'")
        else:
            logger.info(f'======= jav uncensored search END - NOT FOUND: {keyword} =======')

        return all_results

    def search2(self, keyword, site, manual=False):
        site_info = self.site_map.get(site)
        if not site_info or not site_info.get('instance'): return None
        SiteClass = site_info['instance']

        search_keyword = keyword
        if site_info.get('keyword'):
            prefix = site_info['keyword'][0] 
            if not any(k in keyword.lower() for k in site_info['keyword']):
                search_keyword = f"{prefix}-{keyword}"

        try:
            data = SiteClass.search(search_keyword, manual=manual) 
            if data and data.get("ret") == "success" and data.get("data"):
                return data["data"] if isinstance(data["data"], list) else None
        except Exception as e:
            logger.error(f"Error searching '{site}' for '{keyword}': {e}")
        return None

    # endregion SEARCH
    ################################################

    def process_actor(self, entity_actor):
        censored_module = P.get_module('jav_censored')
        if censored_module:
            censored_module.process_actor(entity_actor)
        else:
            if not entity_actor.get("name_ko") and entity_actor.get("name_org"):
                entity_actor["name_ko"] = entity_actor.get("name_org")

    ################################################
    # region INFO

    def info(self, code, extra_opts=None, **kwargs):
        opts = dict(extra_opts or {})
        opts.update(kwargs)

        skip_trans = opts.get('skip_trans', False)

        bypass_cache = False
        try:
            if self.keyword_cache.get(f"BYPASS_{code}") == "1":
                bypass_cache = True
                self.keyword_cache.set(f"BYPASS_{code}", "0")
        except: pass

        if bypass_cache: logger.info(f"[{self.name}] 수동 갱신 요청 감지. DB를 무시합니다: {code}")

        target_instance = None
        for site_info in self.site_map.values():
            instance = site_info['instance']
            if instance.site_char == code[1]:
                target_instance = instance
                break

        if not target_instance:
            logger.error(f"No site found for site_char '{code[1]}' in code '{code}'")
            return None

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
                        logger.info(f"[{self.name}] DB 캐시에 한글 번역이 없어 새로 번역을 수행합니다: {code}")

                if not is_db_untranslated:
                    needs_enrichment = not cached_json.get('thumb')
                    if needs_enrichment:
                        logger.info(f"[{self.name}] 이미지/트레일러 누락 감지. Enrichment를 수행합니다...")
                        fresh_data = target_instance.info(code, extra_opts={'skip_trans': True})
                        if fresh_data and fresh_data.get('ret') == 'success' and fresh_data.get('data'):
                            fresh_ret = fresh_data['data']
                            cached_json['thumb'] = fresh_ret.get('thumb', [])
                            cached_json['fanart'] = fresh_ret.get('fanart', [])
                            if fresh_ret.get('extras'):
                                for extra in fresh_ret['extras']:
                                    if isinstance(extra, dict): extra['title'] = cached_json.get('title', '')
                                    elif hasattr(extra, 'title'): extra.title = cached_json.get('title', '')
                                cached_json['extras'] = fresh_ret['extras']
                            if save_db:
                                ModuleMetaDb.save_metadata(self.category, cached_json)

                    if cached_json.get('extras'):
                        for extra in cached_json['extras']:
                            if isinstance(extra, dict): extra['title'] = cached_json.get('title', '')
                            elif hasattr(extra, 'title'): extra.title = cached_json.get('title', '')

                    title_log = cached_json.get('title', 'No Title')
                    year_log = cached_json.get('year', '????')
                    site_log = cached_json.get('site', 'unknown').upper()
                    ui_code_log = cached_json.get('originaltitle') or cached_json.get('ui_code') or code
                    logger.info(f"[Meta DB Success] Code: {code} ({ui_code_log}), Site: {site_log}, Title: {title_log} ({year_log})")
                    
                    return MetaResponseUtil.finalize_info_return(cached_json, extra_opts=opts, category=self.category)

        ret = None
        res = target_instance.info(code, extra_opts={'skip_trans': skip_trans})
        if res and res.get('ret') == 'success':
            ret = res['data']

        if ret is None: return None

        ret["plex_is_proxy_preview"] = True
        ret["plex_is_landscape_to_art"] = True
        ret["plex_art_count"] = len(ret.get("fanart", []))

        # 배우 처리는 전역 설정대로 항상 온전하게 수행
        actor_names_for_log = []
        if ret.get('actor'):
            for item in ret['actor']:
                self.process_actor(item)
                actor_names_for_log.append(item.get("name_ko") or item.get("name_org", "?"))

        original_calculated_title = ret.get("title", "")

        try:
            title_format = P.ModelSetting.get('jav_censored_title_format')
            format_dict = {
                'originaltitle': ret.get("originaltitle", ""),
                'plot': ret.get("plot", ""),
                'title': original_calculated_title, 
                'sorttitle': ret.get("sorttitle", ""),
                'runtime': ret.get("runtime", ""),
                'country': ', '.join(ret.get("country", [])),
                'premiered': ret.get("premiered", ""),
                'year': ret.get("year", ""),
                'actor': actor_names_for_log[0] if actor_names_for_log else "",
                'tagline': ret.get("tagline", ""),
            }
            
            final_title = title_format.format(**format_dict)
            ret["title"] = final_title

            if ret.get("extras"):
                for extra in ret["extras"]:
                    if isinstance(extra, dict):
                        if extra.get("content_type") == "trailer": extra["title"] = final_title
                    elif hasattr(extra, 'content_type') and extra.content_type == "trailer":
                        if hasattr(extra, 'title'): extra.title = final_title

        except Exception as e_fmt:
            logger.exception(f"타이틀 포맷팅 중 예외 발생: {e_fmt}")
            ret["title"] = original_calculated_title

        if "tag" in ret:
            tag_option = P.ModelSetting.get("jav_censored_tag_option")
            if tag_option == "not_using":
                ret["tag"] = []
            elif tag_option == "label":
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                ret["tag"] = [label] if label else []
            elif tag_option == "site":
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                ret["tag"] = [_ for _ in ret.get("tag", []) if label is None or _ != label]

        if not P.ModelSetting.get_bool('jav_censored_use_extras'):
            ret['extras'] = []

        clean_actors = []
        for act_it in (ret.get('actor') or []):
            if isinstance(act_it, dict):
                act_name = act_it.get('name') or act_it.get('name_ko') or act_it.get('name_org', '')
                clean_actors.append({
                    'name': act_name,
                    'name_org': act_it.get('name_org', ''),
                    'name_ko': act_it.get('name_ko', ''),
                    'name_en': act_it.get('name_en', ''),
                    'thumb': act_it.get('thumb', ''),
                    'actor_idx': act_it.get('actor_idx', '') or act_it.get('person_idx', ''),
                    'role': act_it.get('role', '출연')
                })
            else:
                act_name = getattr(act_it, 'name', '') or getattr(act_it, 'name_ko', '') or getattr(act_it, 'name_org', '')
                clean_actors.append({
                    'name': act_name,
                    'name_org': getattr(act_it, 'name_org', ''),
                    'name_ko': getattr(act_it, 'name_ko', ''),
                    'name_en': getattr(act_it, 'name_en', ''),
                    'thumb': getattr(act_it, 'thumb', ''),
                    'actor_idx': getattr(act_it, 'actor_idx', '') or getattr(act_it, 'person_idx', ''),
                    'role': getattr(act_it, 'role', '출연')
                })

        ret['actor'] = clean_actors

        if ret:
            title_log = ret.get('title', 'No Title')
            year_log = ret.get('year', '????')
            logger.info(f"[{self.name}] Info Success: Code: {code} -> {title_log} ({year_log})")

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

        # DB에는 정규 마스터 데이터 영구 저장
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

    # endregion INFO
    ################################################
