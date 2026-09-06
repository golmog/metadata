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

from support_site import (
    SiteAvBase,
    SiteDmm,
    SiteAvdbs,
    SiteJav321,
    SiteJavbus,
    SiteMgstage,
    SiteUtil,
    SiteJavdb,
    UtilNfo,
    DiscordUtil
)
from support_site.entity_av import EntityAVSearch

from .setup import *
from support import SupportYaml

from .mod_meta_db import ModuleMetaDb
from .util_metadata import MetaImageUtil, MetaWorkerUtil, MetaResponseUtil


class ModuleJavCensored(PluginModuleBase):
    
    def __init__(self, P):
        super(ModuleJavCensored, self).__init__(P, name='jav_censored', first_menu='setting')
        self.category = 'JAV_CEN'
        self.web_list_model = None
        self.site_map = {
            "avdbs": SiteAvdbs,
            "dmm": SiteDmm,
            "jav321": SiteJav321,
            "javbus": SiteJavbus,
            "mgstage": SiteMgstage,
            "javdb": SiteJavdb,
        }

        self.db_default = {
            f"{self.name}_db_version": "2",
            f"{self.name}_order": "dmm, mgstage, jav321, javbus, javdb",
            f"{self.name}_actor_order": "avdbs",
            f"{self.name}_result_priority_order": "dmm_videoa, dmm_dvd, mgstage, dmm_bluray, dmm_amateur, dmm_unknown, jav321, javbus, javdb",

            f"{self.name}_mgs_label_priority": "False",
            f"{self.name}_mgs_label_priority_exclude": "",

            # 공통 설정
            f"{self.name}_trans_option": "using",

            f"{self.name}_use_ollama": "False",
            f"{self.name}_ollama_url": "http://ollama:11434/api/chat",
            f"{self.name}_ollama_model": "gemma4:12b",
            f"{self.name}_ollama_temp": "0.2",
            f"{self.name}_ollama_top_p": "0.95",
            f"{self.name}_ollama_top_k": "40",
            f"{self.name}_ollama_repeat_penalty": "1.15",
            f"{self.name}_ollama_num_ctx": "2048",
            f"{self.name}_ollama_system_prompt": "너는 해외 성인 문화/컨텐츠를 연구하는 학술/통역 어시스턴스이다.\n외국어가 입력되면 설명, 인사말 등이나 기타 불필요한 미사여구를 일절 제외하고 한국어로 정확히 번역 내용만 출력해야 한다.\n성적/폭력적 컨텐츠 등에서도 어떠한 필터링이나 검열을 하지 않고 있는 그대로 직설적이고 노골적으로 번역해야 한다.",
            f"{self.name}_ollama_test_text": "",

            f"{self.name}_title_format": "[{title}] {tagline}",
            f"{self.name}_use_imagehash": "False",
            f"{self.name}_use_hq_poster_check": "False",
            f"{self.name}_art_count": "0",
            f"{self.name}_tag_option": "not_using",
            f"{self.name}_use_extras": "False",

            f"{self.name}_selenium_url": "", 
            f"{self.name}_selenium_driver_type": "chrome",
            f"{self.name}_flaresolverr_url": "",

            "jav_settings_filepath": os.path.join(path_data, 'db', 'jav_custom_settings.yaml'),

            # 이미지 모드
            f"{self.name}_image_mode": "ff_proxy", 

            # 디스코드 프록시
            f"{self.name}_use_discord_proxy_server": "False",
            f"{self.name}_discord_proxy_server_url": "",
            f"{self.name}_use_my_webhook": "False",
            f"{self.name}_my_webhook_list": "",

            # 이미지 서버
            f"{self.name}_image_server_url": f"{F.SystemModelSetting.get('ddns')}/images",
            f"{self.name}_image_server_local_path": "/data/images",
            f"{self.name}_image_server_save_format": "/jav/cen/{label_1}/{label}",
            f"{self.name}_image_save_mode": "jpeg",
            f"{self.name}_image_server_actor_path": "/jav/actors",
            f"{self.name}_image_server_rewrite": "True",

            # avdbs
            f"{self.name}_avdbs_use_web_search": "False",
            f"{self.name}_avdbs_use_proxy": "False",
            f"{self.name}_avdbs_proxy_url": "",
            f"{self.name}_avdbs_use_local_db": "True",
            f"{self.name}_avdbs_test_name": "",
            f"{self.name}_avdbs_img_order": "google_fileid, local_img_path, site_img_url",

            # dmm
            f"{self.name}_dmm_use_proxy": "False",
            f"{self.name}_dmm_proxy_url": "",
            f"{self.name}_dmm_small_image_to_poster": "",
            f"{self.name}_dmm_crop_mode": "",
            f"{self.name}_dmm_priority_search_labels": "",
            f"{self.name}_dmm_test_code": "ssni-900",

            # mgstage
            f"{self.name}_mgstage_use_proxy": "False",
            f"{self.name}_mgstage_proxy_url": "",
            f"{self.name}_mgstage_small_image_to_poster": "",
            f"{self.name}_mgstage_crop_mode": "",
            f"{self.name}_mgstage_priority_search_labels": "",
            f"{self.name}_mgstage_test_code": "abf-010",

            # jav321
            f"{self.name}_jav321_use_proxy": "False",
            f"{self.name}_jav321_proxy_url": "",
            f"{self.name}_jav321_small_image_to_poster": "",
            f"{self.name}_jav321_crop_mode": "",
            f"{self.name}_jav321_priority_search_labels": "",
            f"{self.name}_jav321_test_code": "abw-354",

            # javdb
            f"{self.name}_javdb_use_proxy": "False",
            f"{self.name}_javdb_proxy_url": "",
            f"{self.name}_javdb_use_selenium": "False",
            f"{self.name}_javdb_use_flaresolverr": "False",
            f"{self.name}_javdb_small_image_to_poster": "",
            f"{self.name}_javdb_crop_mode": "",
            f"{self.name}_javdb_priority_search_labels": "",
            f"{self.name}_javdb_test_code": "JUFE-487",

            # javbus
            f"{self.name}_javbus_use_proxy": "False",
            f"{self.name}_javbus_proxy_url": "",
            f"{self.name}_javbus_small_image_to_poster": "",
            f"{self.name}_javbus_crop_mode": "",
            f"{self.name}_javbus_priority_search_labels": "",
            f"{self.name}_javbus_test_code": "abw-354",

            # Smart Crop
            f"{self.name}_use_smart_crop": "False",
            f"{self.name}_face_landmarker_model_path": f"{path_data}/db/face_landmarker.task",
            f"{self.name}_use_pose_landmarker": "False",
            f"{self.name}_pose_landmarker_model_path": f"{path_data}/db/pose_landmarker_heavy.task",
        }

        # 백그라운드 작업 상태 관리
        self.enrich_status = {'is_running': False, 'status': '대기 중', 'total': 0, 'current': 0, 'success': 0, 'fail': 0, 'current_code': '', 'stop_flag': False}
        self.sync_status = {'is_running': False, 'status': '대기 중', 'total': 0, 'current': 0, 'updated': 0, 'rescued': 0, 'current_code': '', 'stop_flag': False}
        self.migrate_status = {'is_running': False, 'status': '대기 중', 'total': 0, 'current': 0, 'success': 0, 'fail': 0, 'stop_flag': False}
        self.transfer_status = {'is_running': False, 'status': '대기 중', 'total': 0, 'current': 0, 'success': 0, 'fail': 0, 'stop_flag': False}
        self.import_status = {
            'is_running': False, 'status': '대기 중', 'total': 0,
            'current': 0, 'inserted': 0, 'updated': 0, 'skipped': 0, 'fail': 0,
            'current_code': '', 'stop_flag': False
        }

        try:
            self.keyword_cache = F.get_cache(f"{P.package_name}_{self.name}_keyword_cache")
        except Exception:
            self.keyword_cache = {}


    ################################################
    # region PluginModuleBase 메서드 오버라이드

    def setting_save(self, req):
        """
        FF 프레임워크의 일괄 덮어쓰기 방어:
        현재 제출된 폼(req.form)에 실제로 존재하는 설정 및 해당 페이지 관련 체크박스만 안전하게 갱신
        """
        try:
            change_list = []
            form_keys = set(req.form.keys())

            # 1. 폼에 전송된 모든 텍스트/라디오/체크된 항목 갱신
            for key in form_keys:
                if key in ['sub', 'package_name', 'module_name']:
                    continue
                if key in self.db_default:
                    val = req.form[key].strip()
                    if P.ModelSetting.set(key, val):
                        change_list.append(key)

            # 2. 폼에 없는 체크박스 처리 (현재 전송된 페이지 그룹의 체크박스만 'False' 처리)
            submitted_prefixes = set()
            for k in form_keys:
                if '_db_' in k: submitted_prefixes.add('_db_')
                if '_dmm_' in k: submitted_prefixes.add('_dmm_')
                if '_mgstage_' in k: submitted_prefixes.add('_mgstage_')
                if '_jav321_' in k: submitted_prefixes.add('_jav321_')
                if '_javbus_' in k: submitted_prefixes.add('_javbus_')
                if '_javdb_' in k: submitted_prefixes.add('_javdb_')
                if '_avdbs_' in k: submitted_prefixes.add('_avdbs_')
                if k in ['jav_censored_order', 'jav_censored_title_format', 'jav_censored_trans_option']:
                    submitted_prefixes.add('main_setting')

            for key, default_val in self.db_default.items():
                # 체크박스 형태의 기본값을 가진 항목 검사
                if default_val in ['True', 'False'] and key not in form_keys:
                    should_turn_off = False
                    
                    if '_db_' in key and '_db_' in submitted_prefixes: should_turn_off = True
                    elif '_dmm_' in key and '_dmm_' in submitted_prefixes: should_turn_off = True
                    elif '_mgstage_' in key and '_mgstage_' in submitted_prefixes: should_turn_off = True
                    elif '_jav321_' in key and '_jav321_' in submitted_prefixes: should_turn_off = True
                    elif '_javbus_' in key and '_javbus_' in submitted_prefixes: should_turn_off = True
                    elif '_javdb_' in key and '_javdb_' in submitted_prefixes: should_turn_off = True
                    elif '_avdbs_' in key and '_avdbs_' in submitted_prefixes: should_turn_off = True
                    elif 'main_setting' in submitted_prefixes and not any(p in key for p in ['_db_', '_dmm_', '_mgstage_', '_jav321_', '_javbus_', '_javdb_', '_avdbs_']):
                        should_turn_off = True

                    if should_turn_off:
                        if P.ModelSetting.set(key, 'False'):
                            change_list.append(key)

            self.setting_save_after(change_list)
            return jsonify(True)

        except Exception as e:
            logger.error(f"[{self.name}] setting_save 에러: {e}")
            logger.error(traceback.format_exc())
            return jsonify(False)


    def plugin_load(self):
        try:
            cache_filepath = os.path.join(path_data, 'db', 'av_cache.sqlite')
            if os.path.exists(cache_filepath):
                os.remove(cache_filepath)
        except Exception as e:
            logger.error(f"Failed to delete AV cache file: {e}")

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
        except Exception as e_db_create:
            logger.error(f"[{self.name}] Failed to initialize Metadata DB engine: {e_db_create}")
            logger.error(traceback.format_exc())

        self.create_default_settings_yaml()
        self._set_site_setting()

    def plugin_load_celery(self):
        self._set_site_setting()

    def setting_save_after(self, change_list):
        ins_list = []
        always_all_set = ["jav_censored_use_extras", "jav_censored_art_count"]
        for tmp in always_all_set:
            if tmp in change_list:
                ins_list = list(self.site_map.values())
                break

        for key in change_list:
            if key.endswith("_test_code"):
                continue
            for site, ins in self.site_map.items():
                if site in key and ins not in ins_list:
                    ins_list.append(ins)
                    break
        self._set_site_setting(ins_list)

    def _set_site_setting(self, ins_list=None):
        if ins_list is None:
            ins_list = self.site_map.values()

        self.jav_settings = self.get_jav_settings()
        SiteAvBase.set_yaml_settings(self.jav_settings)
        SiteAvBase.set_config(self.P.ModelSetting)

        for ins in ins_list:
            try:
                P.logger.debug(f"set_config site {ins.__name__} with settings.")
                ins.set_config(self.P.ModelSetting)
            except Exception as e:
                P.logger.error(f"Error initializing site {ins}: {str(e)}")

    def _sort_search_results(self, search_results_raw, call_site=None):
        if not search_results_raw:
            return []

        priority_string = P.ModelSetting.get('jav_censored_result_priority_order')
        priority_list = [x.strip() for x in priority_string.split(',') if x.strip()]
        dynamic_priority_map = {key: index for index, key in enumerate(priority_list)}
        lowest_priority = len(priority_list)

        def get_priority_value(item_to_sort):
            site_key = item_to_sort.get('site_key', call_site)
            content_type = item_to_sort.get('content_type')
            
            if site_key == 'dmm' and content_type:
                type_specific_key = f"dmm_{content_type}"
                if type_specific_key in dynamic_priority_map:
                    return dynamic_priority_map[type_specific_key]
            
            return dynamic_priority_map.get(site_key, lowest_priority)

        def get_sort_key(item):
            return (-item.get("score", 0), get_priority_value(item))

        return sorted(search_results_raw, key=get_sort_key)

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
                category = req.form.get('category') or getattr(self, 'category', 'JAV_CEN')
                if str(category).upper() == 'PERSON':
                    return jsonify(ModuleMetaDb.person_web_list(req, default_domain=req.form.get('search_domain', 'JAV')))
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
            res = super(ModuleJavCensored, self).process_ajax(sub, req)
            if res is not None:
                return res

            return jsonify({'ret': 'error', 'msg': f'미처리된 AJAX 요청: sub={sub}, command={command}'})

        except Exception as e:
            logger.error(f"[{self.name}] process_ajax 에러: {e}")
            logger.error(traceback.format_exc())
            return jsonify({'ret': 'error', 'msg': str(e)})

    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            ret = {'ret': 'success'}

            # --- 포스터 수동 크롭 저장 ---
            if command == "db_crop_save":
                code = arg1
                crop_data = arg2
                pl_base64 = arg3
                success, result_msg = MetaImageUtil.save_user_cropped_poster(
                    code, crop_data, pl_image_base64_data=pl_base64, category=self.category
                )
                return jsonify({'ret': 'success' if success else 'error', 'msg': result_msg, 'new_url': result_msg if success else None})

            # --- 단일 레코드 삭제 ---
            elif command == 'db_delete':
                success = ModuleMetaDb.delete_record(arg1, category=self.category)
                return jsonify({'ret': 'success' if success else 'error'})

            elif command == 'db_delete_selected':
                success, count = ModuleMetaDb.delete_records(arg1, category=self.category)
                msg = f"{count}건의 메타데이터가 삭제되었습니다." if success else "선택 항목 삭제 실패"
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

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

            # --- 카테고리 DB 초기화 및 최적화 ---
            elif command == 'db_clear':
                success, count = ModuleMetaDb.clear_db(self.category)
                return jsonify({'ret': 'success' if success else 'error', 'msg': f'{count}건 삭제 완료' if success else '초기화 실패'})

            elif command == 'db_vacuum':
                success = ModuleMetaDb.vacuum_db()
                return jsonify({'ret': 'success' if success else 'error', 'msg': 'DB 최적화 완료' if success else '최적화 실패'})

            # --- 이미지/미디어만 재동기화 (번역 생략, 기존 텍스트 유지, _user 파일 보호) ---
            elif command == 'db_refresh_image_only':
                code = arg1
                cached_json = ModuleMetaDb.get_metadata(code, category=self.category)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})

                ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
                logger.info(f"[{self.name}] 미디어 전용 재동기화 시작: [{code}] ({ui_code})")

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
                    logger.info(f"[{self.name}] [{code}] 이미지 및 팬아트 주소 갱신 완료 (Arts: {len(cached_json.get('fanart', []))}개, OrigArts: {len(cached_json.get('original', {}).get('fanart', []))}개)")
                    return jsonify({'ret': 'success', 'msg': f"[{ui_code}] 이미지 및 미디어 동기화 완료"})
                else:
                    return jsonify({'ret': 'warning', 'msg': f"[{ui_code}] 원격 사이트에서 미디어 정보를 가져오지 못했습니다."})

            # 현재 사이트 정보 제자리 갱신
            elif command == 'db_refresh_in_place':
                code = arg1
                cached_json = ModuleMetaDb.get_metadata(code, category=self.category)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})

                ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
                logger.info(f"[{self.name}] 현재 출처 사이트 제자리 갱신 시작: [{code}] ({ui_code})")

                try: self.keyword_cache.set(f"BYPASS_{code}", "1")
                except: pass

                fresh_data = self.info(code, keyword=ui_code, skip_trans=False)
                if fresh_data:
                    ModuleMetaDb.save_metadata(self.category, fresh_data)
                    title_log = fresh_data.get('title', '')
                    logger.info(f"[{self.name}] [{code}] 메타데이터 및 팬아트 갱신 완료: {title_log}")
                    return jsonify({'ret': 'success', 'msg': f"[{ui_code}] 현재 사이트 메타데이터 갱신 완료!\n{title_log}"})
                else:
                    return jsonify({'ret': 'warning', 'msg': f"[{ui_code}] 출처 사이트에서 정보를 가져오지 못했습니다. 삭제된 항목이면 '자동 재검색 갱신'을 사용하세요."})

            # --- 전체 우선순위 자동 재검색 갱신 (순차 탐색, 1위 항목으로 교체/생성) ---
            elif command == 'db_refresh_auto_search':
                code = arg1
                cached_json = ModuleMetaDb.get_metadata(code, category=self.category)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})

                ui_code = cached_json.get('ui_code') or cached_json.get('originaltitle') or code
                logger.info(f"[{self.name}] 전체 우선순위 자동 재검색 갱신 시작: [{code}] ➔ 키워드: '{ui_code}'")

                # DB 캐시를 타지 않는 라이브 계층형 순차 검색 실행
                search_res = self.search(ui_code, manual=False, use_db=False)
                if not search_res:
                    return jsonify({'ret': 'warning', 'msg': f"'{ui_code}' 검색 결과가 없습니다."})

                # DB 캐시 결과 제외 및 품번이 정확히 일치하는 최상위 라이브 후보 선택
                best_item = None
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
                    best_item = next((item for item in search_res if not item.get('is_db_cached') and item.get('score', 0) >= 90), None)

                if not best_item:
                    return jsonify({'ret': 'warning', 'msg': f"[{ui_code}] 일치하는 메타데이터를 찾지 못했습니다."})

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

                    msg = f"[{ui_code}] 자동 재검색 갱신 완료! ({best_item.get('site_key', '').upper()})\n{title_log}"
                    return jsonify({'ret': 'success', 'msg': msg})
                else:
                    return jsonify({'ret': 'warning', 'msg': f"[{ui_code}] 상세 정보(Info)를 가져오지 못했습니다."})

            # --- 미디어 일괄 채우기 (Enrichment) ---
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

            elif command == "test":
                code = arg2
                call = arg1
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_code", code)

                search_results_raw = self.search2(code, call, manual=True)
                if not search_results_raw:
                    ret['ret'] = "warning"
                    ret['msg'] = f"no results for '{code}'"
                    return jsonify(ret)

                search_results = self._sort_search_results(search_results_raw, call_site=call)
                info_data = self.info(search_results[0]['code'], keyword=code)
                ret['json'] = {
                    "search": search_results,
                    "info": info_data if info_data else {}
                }
                return jsonify(ret)

            elif command == "reload_jav_settings":
                self._set_site_setting() 
                try:
                    uncensored_module = P.get_module('jav_uncensored')
                    if uncensored_module:
                        uncensored_module._set_site_setting()
                except Exception as e:
                    logger.error(f"Uncensored module reload error: {e}")
                ret['msg'] = "모든 JAV 설정을 새로고침했습니다."
                return jsonify(ret)

            elif command == "actor_test":
                name = arg2
                call = arg1
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_name", name)

                entity_actor = {"name_org": name}
                SiteClass = self.site_map.get(call)
                SiteClass.get_actor_info(entity_actor)
                ret['title'] = f"{arg2} 검색결과"
                ret['json'] = entity_actor
                return jsonify(ret)

            elif command == "rcache_clear":
                for instance in self.site_map.values():
                    try: instance.session.cache.clear()
                    except: pass
                return jsonify({"msg": "초기화 성공"})

            elif command == 'model_action':
                action = arg1
                model_type = arg2
                db_dir = os.path.join(path_data, 'db')
                os.makedirs(db_dir, exist_ok=True)
                
                if model_type == 'face':
                    filename = 'face_landmarker.task'
                    url = 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task'
                    setting_key = 'jav_censored_face_landmarker_model_path'
                elif model_type == 'pose':
                    filename = 'pose_landmarker_heavy.task'
                    url = 'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task'
                    setting_key = 'jav_censored_pose_landmarker_model_path'
                else:
                    return jsonify({'ret':'error', 'msg':'Unknown model type'})
                
                filepath = os.path.join(db_dir, filename)
                if action == 'download':
                    try:
                        response = requests.get(url, stream=True)
                        response.raise_for_status()
                        with open(filepath, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                f.write(chunk)
                        P.ModelSetting.set(setting_key, filepath)
                        return jsonify({'ret':'success', 'msg':f'{filename} 다운로드 완료', 'filepath': filepath})
                    except Exception as e:
                        return jsonify({'ret':'error', 'msg':f'다운로드 실패: {str(e)}'})
                return jsonify({'ret':'error', 'msg':'Unknown action'})

            elif command == "ollama_test":
                text_to_translate = arg1
                try:
                    translated_text = SiteDmm.trans_by_llm(text_to_translate)
                    ret['title'] = "Ollama 번역 결과"
                    ret['data'] = translated_text.replace('\n', '<br>')
                except Exception as e_ollama:
                    ret['ret'] = 'error'
                    ret['data'] = str(e_ollama)
                return jsonify(ret)

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

            elif command in ['person_sub_set_master', 'person_sub_split']:
                meta_module = P.get_module('meta_db')
                if meta_module:
                    return meta_module.process_command(command, arg1, arg2, arg3, req)
                return jsonify({'ret': 'error', 'msg': 'meta_db 모듈을 찾을 수 없습니다.'})

            elif command == 'get_meta_by_code':
                meta_module = P.get_module('meta_db')
                if meta_module:
                    return meta_module.process_command('get_meta_by_code', arg1, arg2, arg3, req)
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
                keyword = req.args.get("keyword").rstrip("-").strip()
                manual = req.args.get("manual") == "True"
                return jsonify(self.search(keyword, manual=manual))

            if sub == "info":
                data = self.info(req.args.get("code"))
                if call == "kodi":
                    data = SiteUtil.info_to_kodi(data)
                return jsonify(data)

            return jsonify({'ret': 'failed', 'msg': f'Invalid sub command: {sub}'}), 400

        except Exception as e:
            logger.error(f"Exception in process_api (sub={sub}): {e}")
            return jsonify({'ret': 'exception', 'msg': str(e)}), 500


    def process_normal(self, sub, req):
        if sub == "nfo_download":
            keyword = req.args.get("code")
            call = req.args.get("call")
            if call in self.site_map:
                search_results_raw = self.search2(keyword, call)
                if search_results_raw:
                    search_results = self._sort_search_results(search_results_raw, call_site=call)
                    try: self.keyword_cache.set(search_results[0]['code'], keyword)
                    except AttributeError: self.keyword_cache[search_results[0]['code']] = keyword

                    info = self.info(search_results[0]["code"])
                    if info:
                        return UtilNfo.make_nfo_movie(info, output="file", filename=info["originaltitle"].upper() + ".nfo")

        elif sub == "yaml_download":
            keyword = req.args.get("code")
            call = req.args.get("call")
            if call in self.site_map:
                search_results_raw = self.search2(keyword, call)
                if search_results_raw:
                    search_results = self._sort_search_results(search_results_raw, call_site=call)
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
                    search_results_raw = self.search2(keyword, call)
                    if not search_results_raw: return "Search failed", 404
                    
                    search_results = self._sort_search_results(search_results_raw, call_site=call)
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
                    
                    if not target_url: return f"Image '{image_type}' not found", 404

                    img_res = requests.get(target_url, verify=False, timeout=30)
                    if img_res.status_code != 200: return "Download error", 500

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

    def create_default_settings_yaml(self):
        try:
            settings_filepath = self.P.ModelSetting.get("jav_settings_filepath")
            if not os.path.exists(settings_filepath):
                template_path = os.path.join(PLUGIN_ROOT, 'files', 'jav_settings_sample.yaml')
                if os.path.exists(template_path):
                    os.makedirs(os.path.dirname(settings_filepath), exist_ok=True)
                    shutil.copyfile(template_path, settings_filepath)
        except Exception as e:
            logger.error(f"YAML 설정 파일 생성 오류: {e}")

    def get_jav_settings(self):
        settings_filepath = self.P.ModelSetting.get("jav_settings_filepath")
        if settings_filepath and os.path.exists(settings_filepath):
            try: return SupportYaml.read_yaml(settings_filepath)
            except: pass
        return {}

    # endregion PluginModuleBase 메서드 오버라이드
    ################################################     

    ################################################
    # region SEARCH

    def _reconcile_official_results(self, all_results):
        if not all_results: return all_results
        official_items = [x for x in all_results if x.get('site_key') in ['dmm', 'mgstage'] and int(x.get('original_score', 0)) == 99]
        secondary_items = [x for x in all_results if x.get('site_key') in ['jav321', 'javbus', 'javdb'] and int(x.get('original_score', 0)) >= 100]

        if not official_items or not secondary_items: return all_results

        import difflib
        for off_item in official_items:
            off_ui = str(off_item.get('ui_code') or '').upper()
            off_match = re.match(r'^(\d*)([A-Z]+)-?(\d+.*)$', off_ui)
            if not off_match: continue
            off_prefix, off_label, off_num = off_match.group(1), off_match.group(2), (off_match.group(3).lstrip('0') or '0')

            off_title_clean = re.sub(r'\[.*?\]|【.*?】|\(.*?\)|<.*?>|\s+', '', str(off_item.get('title') or ''))
            off_title_norm = re.sub(r'[^a-zA-Z0-9가-힣\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', off_title_clean).lower()

            for sec_item in secondary_items:
                sec_ui = str(sec_item.get('ui_code') or '').upper()
                sec_match = re.match(r'^(\d*)([A-Z]+)-?(\d+.*)$', sec_ui)
                if not sec_match: continue
                sec_prefix, sec_label, sec_num = sec_match.group(1), sec_match.group(2), (sec_match.group(3).lstrip('0') or '0')

                if off_label == sec_label and off_num == sec_num:
                    sec_title_clean = re.sub(r'\[.*?\]|【.*?】|\(.*?\)|<.*?>|\s+', '', str(sec_item.get('title') or ''))
                    sec_title_norm = re.sub(r'[^a-zA-Z0-9가-힣\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', sec_title_clean).lower()

                    similarity = difflib.SequenceMatcher(None, off_title_norm, sec_title_norm).ratio() if off_title_norm and sec_title_norm else 0.0
                    is_contained = (len(off_title_norm) >= 5 and off_title_norm in sec_title_norm) or (len(sec_title_norm) >= 5 and sec_title_norm in off_title_norm)
                    
                    if is_contained or (similarity >= 0.65):
                        off_item['original_score'] = 100
                        off_item['is_reconciled'] = True
                        break

        return all_results

    def search(self, keyword, manual=False, use_db=True):
        logger.info(f"======= jav censored search START - keyword:[{keyword}] manual:[{manual}] use_db:[{use_db}] =======")
        
        if keyword.startswith('http://') or keyword.startswith('https://'):
            logger.info(f"[{self.name}] Direct URL matching triggered for: '{keyword}'")
            direct_item = self._process_direct_url(keyword)
            if direct_item:
                return [direct_item]
            else:
                logger.warning(f"[{self.name}] Failed to parse direct URL: '{keyword}'")
                return []

        all_results = []
        
        # 1. DB 선행 검색 (전역 meta_db_use 기준)
        has_db_perfect_match = False
        if use_db and P.ModelSetting.get_bool("meta_db_use"):
            try:
                valid_db_records = ModuleMetaDb.search_for_auto_match(self.category, keyword)
                if valid_db_records:
                    from support_site import SiteAvBase
                    for record in valid_db_records:
                        jd = record['json_data']
                        db_item = EntityAVSearch(record['site'])
                        db_item.code = record['code']
                        db_item.ui_code = jd.get('ui_code') or record['originaltitle'] or record['code']
                        db_item.title = f"📁 [DB] {record['title']}"
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
                        
                        calc_score = SiteAvBase._calculate_score(keyword, db_item.ui_code) or 99
                        db_item.score = calc_score
                        db_item.content_type = jd.get('content_type', 'unknown')

                        item_dict = db_item.as_dict()
                        item_dict['original_score'] = calc_score
                        item_dict['site_key'] = record['site']
                        item_dict['is_db_cached'] = True
                        item_dict['is_priority_label_site'] = True 
                        all_results.append(item_dict)

                    if any(x.get('original_score', 0) >= 99 for x in all_results):
                        has_db_perfect_match = True
                        logger.info(f"[{self.name}] DB 선행 검색 매칭 확인 ({len(all_results)}건)")

            except Exception as e_db:
                logger.error(f"[{self.name}] DB Search Error: {e_db}")

        skip_external_search = (has_db_perfect_match and not manual and use_db)

        if not skip_external_search:
            original_site_order_list = P.ModelSetting.get_list(f"{self.name}_order", ",")
            current_keyword_label = ""
            is_special_format = False
            
            special_format_match = re.match(r'^(741[a-z]\d{3})-g\d{2,}$', keyword.lower())
            if special_format_match:
                is_special_format = True
                current_keyword_label = special_format_match.group(1).upper()

            if not is_special_format:
                if keyword and '-' in keyword:
                    current_keyword_label = keyword.split('-', 1)[0].upper()
                elif keyword: 
                    match_kw_label = re.match(r'^([A-Z]+)', keyword.upper())
                    if match_kw_label: current_keyword_label = match_kw_label.group(1)

            is_mgs_forced_priority = False
            if current_keyword_label and P.ModelSetting.get_bool(f"{self.name}_mgs_label_priority"):
                exclude_raw_str = P.ModelSetting.get(f"{self.name}_mgs_label_priority_exclude")
                exclude_labels_set = {x.strip().upper() for x in re.split(r'[\s,\n]', exclude_raw_str) if x.strip()} if exclude_raw_str else set()
                
                if current_keyword_label.upper() not in exclude_labels_set:
                    try:
                        from support_site.constants import MGS_LABEL_MAP
                        if current_keyword_label.upper() in MGS_LABEL_MAP:
                            is_mgs_forced_priority = True
                            logger.debug(f"[{self.name}] MGS Forced Priority: Label '{current_keyword_label}' is in MGS_LABEL_MAP. Forcing MGStage priority.")
                    except: pass

            special_priority_site = None 
            if current_keyword_label:
                for site_key_check_priority in original_site_order_list:
                    if site_key_check_priority not in self.site_map: continue
                    db_prefix_check = f"{self.name}_{site_key_check_priority}"
                    priority_labels_str = P.ModelSetting.get(f"{db_prefix_check}_priority_search_labels")
                    if priority_labels_str:
                        site_priority_labels_set = {lbl.strip().upper() for lbl in priority_labels_str.split(',') if lbl.strip()}
                        if current_keyword_label in site_priority_labels_set:
                            special_priority_site = site_key_check_priority
                            logger.debug(f"User Specified Priority: Label '{current_keyword_label}' is assigned to '{special_priority_site}' by user.")
                            break

            if not special_priority_site and is_mgs_forced_priority:
                special_priority_site = 'mgstage'
                logger.debug(f"Automatic Priority: Label '{current_keyword_label}' is automatically assigned to 'mgstage'.")

            site_list_for_current_search = list(original_site_order_list)
            if special_priority_site and special_priority_site in site_list_for_current_search:
                site_list_for_current_search.remove(special_priority_site)
                site_list_for_current_search.insert(0, special_priority_site)
                logger.debug(f"Dynamically adjusted site search order: {site_list_for_current_search}")
            else:
                logger.debug(f"Using default site search order: {site_list_for_current_search}")

            def process_site_search(site_key):
                results = []
                if site_key not in self.site_map: return results
                logger.debug(f"--- Searching on site: {site_key} ---")
                data_from_search2 = self.search2(keyword, site_key, manual=manual)
                if data_from_search2:
                    logger.debug(f"  Got {len(data_from_search2)} result(s) from {site_key}")
                    for item in data_from_search2:
                        item['original_score'] = item.get("score", 0)
                        item['site_key'] = item.get("site_key", site_key)
                        item['is_priority_label_site'] = (special_priority_site and site_key == special_priority_site)
                        if 'code' in item: self.keyword_cache.set(item['code'], keyword)
                        results.append(item)
                return results

            # 3-Tier 계층 검색
            tier1_sites = [s for s in site_list_for_current_search if s in ['dmm', 'mgstage']]
            tier2_sites = [s for s in site_list_for_current_search if s in ['jav321', 'javbus']]
            tier3_sites = [s for s in site_list_for_current_search if s in ['javdb']]
            other_sites = [s for s in site_list_for_current_search if s not in tier1_sites + tier2_sites + tier3_sites]

            early_exit = False
            for site_key in tier1_sites:
                site_results = process_site_search(site_key)
                if site_results:
                    all_results.extend(site_results)
                    if not manual and any(item.get('original_score', 0) >= 100 for item in site_results):
                        logger.info(f"[{self.name}] Tier 1 Early Exit: '{site_key}'에서 100점 매칭 확정 (후속 검색 생략): {keyword}")
                        early_exit = True
                        break

            if not early_exit and tier2_sites:
                for site_key in tier2_sites:
                    site_results = process_site_search(site_key)
                    if site_results: all_results.extend(site_results)

                all_results = self._reconcile_official_results(all_results)
                if not manual and any(int(x.get('original_score', 0)) >= 100 for x in all_results):
                    logger.info(f"[{self.name}] Tier 2 검증 완료: 100점 매칭 확보. JavDB 조회 생략: {keyword}")
                    early_exit = True

            if not early_exit and tier3_sites:
                max_score_so_far = max([int(x.get('original_score', 0)) for x in all_results]) if all_results else 0
                if manual or max_score_so_far < 95:
                    logger.info(f"[{self.name}] Tier 3 폴백 진입: 1·2차 검색 결과 부족 (최고점: {max_score_so_far}점). JavDB 조회 시작...")
                    for site_key in tier3_sites:
                        site_results = process_site_search(site_key)
                        if site_results: all_results.extend(site_results)
                    all_results = self._reconcile_official_results(all_results)
                else:
                    logger.info(f"[{self.name}] Tier 3 생략: 1·2차에서 고득점({max_score_so_far}점) 확보로 JavDB 조회 생략: {keyword}")

            if not early_exit and other_sites:
                for site_key in other_sites:
                    site_results = process_site_search(site_key)
                    if site_results: all_results.extend(site_results)

        logger.info(f"--- 검색 완료. 결과: {len(all_results)} ---")
        if not all_results:
            logger.info("======= jav censored search END - No results found. =======")
            return []

        all_results = self._reconcile_official_results(all_results)

        priority_string = P.ModelSetting.get('jav_censored_result_priority_order')
        priority_list = [x.strip() for x in priority_string.split(',') if x.strip()]
        dynamic_priority_map = {key: index for index, key in enumerate(priority_list)}
        lowest_priority = len(priority_list)

        def get_priority_value_for_sort(item_to_sort):
            site_key_prio = item_to_sort.get('site_key')
            content_type_prio = item_to_sort.get('content_type')
            calculated_prio = lowest_priority
            if site_key_prio == 'dmm' and content_type_prio:
                type_specific_key = f"dmm_{content_type_prio}"
                calculated_prio = dynamic_priority_map.get(type_specific_key, lowest_priority)
            if calculated_prio >= lowest_priority: 
                calculated_prio = dynamic_priority_map.get(site_key_prio, lowest_priority)
            return calculated_prio

        def get_custom_sort_key_for_final(item_for_final_sort):
            label_prio_flag_sort_val = 0 if item_for_final_sort.get('is_priority_label_site') else 1
            adj_score = -item_for_final_sort.get("original_score", 0) 
            prio_val = get_priority_value_for_sort(item_for_final_sort)
            return (adj_score, label_prio_flag_sort_val, prio_val)

        all_results_sorted = sorted(all_results, key=get_custom_sort_key_for_final)

        if all_results_sorted:
            for i, item_in_sorted_list in enumerate(all_results_sorted):
                raw_score = min(100, item_in_sorted_list.get('original_score', 0))
                if i == 0:
                    item_in_sorted_list['score'] = raw_score
                else:
                    prev_score = all_results_sorted[i-1]['score']
                    item_in_sorted_list['score'] = max(0, prev_score - 1) if raw_score >= prev_score else raw_score

                if manual:
                    try: self.keyword_cache.set(f"BYPASS_{item_in_sorted_list['code']}", "1")
                    except AttributeError: self.keyword_cache[f"BYPASS_{item_in_sorted_list['code']}"] = "1"

            logger.info(f"최종 결과(우선순위 점수 반영, 총 {len(all_results_sorted)}건):")
            for i, item_log in enumerate(all_results_sorted):
                ui_code = item_log.get('ui_code', '미상')
                site_key = item_log.get('site_key', 'unknown').upper()
                score = item_log.get('score')
                orig_score = item_log.get('original_score')
                code = item_log.get('code')
                content_type = item_log.get('content_type')
                db_cache = item_log.get('is_db_cached', False)
                prio_label = item_log.get('is_priority_label_site', False)
                raw_title = item_log.get('title', '')
                title_preview = (raw_title[:60] + "...") if len(raw_title) > 60 else raw_title
                type_str = f"Type={content_type}, " if content_type and content_type != 'unknown' else ""
                prio_str = "PrioLabel=True, " if prio_label else ""
                logger.info(f"  {i+1}. [{site_key}] 점수={score}(원점수={orig_score}) | 품번={ui_code} | Code={code} | {type_str}{prio_str}DB_Cache={db_cache} | Title='{title_preview}'")

        logger.info(f"======= jav censored search END - Returning {len(all_results_sorted)} results. =======")
        return all_results_sorted

    def search2(self, keyword, site, manual=False, site_settings_override=None):
        SiteClass = self.site_map.get(site, None)
        if SiteClass is None: return None
        try:
            data = SiteClass.search(keyword, do_trans=manual, manual=manual) 
            if data and data.get("ret") == "success" and data.get("data"):
                return data["data"] if isinstance(data["data"], list) else None
        except Exception as e:
            logger.error(f"Error searching '{site}' for keyword '{keyword}': {e}")
        return None

    def _process_direct_url(self, url):
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname.lower() if parsed.hostname else ""
        except: return None
        
        site_key, extracted_code = None, None
        if 'dmm.co.jp' in hostname or 'dmm.com' in hostname:
            site_key = 'dmm'
            m = re.search(r'[?&]id=(?P<code>[^&?]+)|/cid=(?P<code>[^/&?]+)', url)
            if m: extracted_code = m.group('code')
        elif 'mgstage.com' in hostname:
            site_key = 'mgstage'
            m = re.search(r'/product_detail/(?P<code>[^/&?]+)', url)
            if m: extracted_code = m.group('code')
        elif 'jav321.com' in hostname:
            site_key = 'jav321'
            m = re.search(r'/video/([^/&?]+)', url)
            if m: extracted_code = m.group(1)
        elif 'javbus.com' in hostname:
            site_key = 'javbus'
            m = re.search(r'javbus\.com/([^/&?]+)', url)
            if m: extracted_code = m.group(1)
        elif 'javdb.com' in hostname:
            site_key = 'javdb'
            m = re.search(r'/v/([^/&?]+)', url)
            if m: extracted_code = m.group(1)

        if not site_key or not extracted_code: return None
        SiteClass = self.site_map.get(site_key)
        if not SiteClass: return None

        try: final_ui_code, _, _ = SiteClass._parse_ui_code(extracted_code)
        except: final_ui_code = extracted_code.upper()

        mock_code = 'C' + SiteClass.site_char + extracted_code
        try: self.keyword_cache.set(mock_code, final_ui_code)
        except AttributeError: self.keyword_cache[mock_code] = final_ui_code

        return {
            'code': mock_code,
            'ui_code': final_ui_code,
            'score': 100,
            'original_score': 100,
            'adjusted_score': 100,
            'title': f"[{site_key.upper()} URL 직접 매칭] {final_ui_code}",
            'desc': f"직접 우회 진입: {url}",
            'image_url': '',
            'site_key': site_key,
            'content_type': 'unknown',
            'is_priority_label_site': True
        }


    # endregion SEARCH
    ################################################

    ################################################
    # region INFO

    def info(self, code, keyword=None, extra_opts=None, **kwargs):
        opts = dict(extra_opts or {})
        opts.update(kwargs)

        skip_trans = opts.get('skip_trans', False)

        bypass_cache = False
        try:
            if self.keyword_cache.get(f"BYPASS_{code}") == "1":
                bypass_cache = True
                self.keyword_cache.set(f"BYPASS_{code}", "0")
        except: pass

        if bypass_cache:
            logger.info(f"[{self.name}] 수동 검색(Fix Match)을 통한 Info 요청 감지. 로컬 DB 캐시를 무시하고 최신 라이브 데이터를 긁어와 갱신합니다: {code}")

        if code[1] == "B": site = "javbus"
        elif code[1] == "D": site = "dmm"
        elif code[1] == "T": site = "jav321"
        elif code[1] == "M": site = "mgstage"
        elif code[1] == "J": site = "javdb"
        else:
            logger.error("처리할 수 없는 코드: code=%s", code)
            return None

        if keyword is None:
            keyword = self.keyword_cache.get(code)
            if keyword:
                logger.debug(f"info: Found keyword '{keyword}' in cache for code '{code}'.")

        cached_json = None
        ps_url = opts.get('ps_url')

        use_db = P.ModelSetting.get_bool("meta_db_use")
        save_db = P.ModelSetting.get_bool("meta_db_save")
        
        # 캐시 히트 시: DB 원본은 보존하고 반환 직전 임시 가공 적용
        if use_db and not bypass_cache:
            cached_json = ModuleMetaDb.get_metadata(code, category=self.category)

            if cached_json:
                original_thumb = cached_json.get('original', {}).get('thumb', {})
                if not ps_url:
                    ps_url = original_thumb.get('ps_url') if isinstance(original_thumb, dict) else None
                is_db_untranslated = False
                db_plot = cached_json.get('plot', '')
                if db_plot and not skip_trans:
                    if not SiteUtil.is_include_hangul(db_plot):
                        is_db_untranslated = True
                        logger.info(f"[{self.name}] DB 캐시에 한글 번역이 없어 캐시를 건너뛰고 새로 번역을 수행합니다: {code}")

                if not is_db_untranslated and not (site != 'javdb' and not ps_url):
                    needs_enrichment = not cached_json.get('thumb')
                    if needs_enrichment:
                        logger.info(f"[{self.name}] 이미지/트레일러 누락 감지. Enrichment를 수행합니다...")
                        fresh_opts = {'skip_trans': True, 'is_validating': False}
                        if ps_url:
                            fresh_opts['ps_url'] = ps_url
                        fresh_data = self.info2(code, site, keyword, extra_opts=fresh_opts)
                        if fresh_data:
                            if fresh_data.get('thumb'):
                                cached_json['thumb'] = fresh_data['thumb']
                                cached_json['fanart'] = fresh_data.get('fanart', [])
                            if fresh_data.get('extras'):
                                for extra in fresh_data['extras']:
                                    if isinstance(extra, dict): extra['title'] = cached_json.get('title', '')
                                    elif hasattr(extra, 'title'): extra.title = cached_json.get('title', '')
                                cached_json['extras'] = fresh_data['extras']
                            if fresh_data.get('original'):
                                cached_json['original'] = fresh_data['original']
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

        if not ps_url and site != 'javdb':
            site_class = self.site_map.get(site)
            search_results = site_class.search(keyword or code, do_trans=False, manual=False) if site_class else []
            site_cache = getattr(site_class, '_ps_url_cache', {}).get(code, {}) if site_class else {}
            if isinstance(site_cache, dict):
                ps_url = (
                    site_cache.get('ps') or
                    site_cache.get(site_cache.get('main_content_type')) or
                    site_cache.get('poster') or
                    ''
                )
            matching_result = next(
                (
                    item for item in search_results
                    if not ps_url and item.get('site_key') == site and item.get('code') == code
                ),
                None
            )
            if matching_result:
                ps_url = matching_result.get('image_url') or ''

        if ps_url:
            opts['ps_url'] = ps_url

        ret = self.info2(code, site, keyword, extra_opts=opts)
        if ret is None:
            logger.debug(f"info2 returned None for code: {code}")
            return None

        # 가짜/누락/깨진 이미지 감지 시 구출 (is_invalid_image 판정)
        has_valid_thumb = bool(ret and ret.get('thumb'))
        is_invalid_image = False
        SiteClass = self.site_map.get(site)
        
        if not has_valid_thumb:
            is_invalid_image = True
            logger.info(f"[{self.name}] [{site}] 썸네일(Thumb) 완전 누락 감지 ➔ 타 사이트 구출 검색 시작: {code}")
        else:
            target_poster_url = next((t['value'] for t in ret.get('thumb', []) if t.get('aspect') == 'poster'), None)
            if not target_poster_url:
                is_invalid_image = True
                logger.info(f"[{self.name}] [{site}] 대표 포스터 URL 누락 ➔ 타 사이트 구출 검색 시작: {code}")
            else:
                if SiteClass:
                    im_obj = SiteClass.imopen(target_poster_url)
                    if im_obj is None:
                        is_invalid_image = True
                        logger.info(f"[{self.name}] [{site}] 포스터 이미지 로드 실패(403/404/파일없음) 감지 ➔ 타 사이트 구출 검색 시작: {target_poster_url}")
                    else:
                        try:
                            if SiteClass.is_placeholder_image(im_obj):
                                is_invalid_image = True
                                logger.info(f"[{self.name}] [{site}] 플레이스홀더(Now Printing 등) 이미지 감지 ➔ 타 사이트 구출 검색 시작: {code}")
                        finally:
                            im_obj.close()

        # 무효/깨진/누락 이미지일 때 후순위 사이트 구출 실행
        if is_invalid_image and ret:
            raw_ui_code = ret.get('originaltitle') or ret.get('ui_code') or keyword or code
            ui_code = str(raw_ui_code).strip()

            logger.info(f"[{self.name}] 이미지 구출 파이프라인 가동 ({site} 실패) ➔ 대상 품번: {ui_code}")

            # 사용자가 설정한 메타 우선순위(jav_censored_order) 로드
            user_order_list = [
                s.strip().lower() for s in P.ModelSetting.get_list(f"{self.name}_order", ",") 
                if s.strip()
            ]
            
            # 현재 사이트의 '후순위(다음 순위)' 사이트들만 슬라이싱
            current_site = site.lower()
            if current_site in user_order_list:
                site_idx = user_order_list.index(current_site)
                candidate_sites = user_order_list[site_idx + 1:]
            else:
                candidate_sites = [s for s in user_order_list if s != current_site]

            backup_sites = [s for s in candidate_sites if s in self.site_map]
            logger.debug(f"[{self.name}] 구출 대상 후순위 백업 사이트 목록: {backup_sites}")
            
            from support_site import SiteAvBase
            _, target_label, target_num = SiteAvBase._parse_ui_code(ui_code)

            for b_site in backup_sites:
                b_SiteClass = self.site_map.get(b_site)
                if not b_SiteClass:
                    continue
                try:
                    b_search = self.search2(ui_code, b_site, manual=True)
                    if b_search and len(b_search) > 0:
                        for s_cand in b_search:
                            cand_score = s_cand.get('score', 0)
                            cand_code = s_cand.get('code')
                            cand_ui = str(s_cand.get('ui_code') or '').strip()
                            
                            _, c_label, c_num = SiteAvBase._parse_ui_code(cand_ui)
                            if cand_score >= 99 and target_label == c_label and target_num == c_num and cand_code:
                                b_info = self.info2(cand_code, b_site, keyword=ui_code, skip_trans=True)
                                if b_info and b_info.get('thumb'):
                                    b_target_url = next((t['value'] for t in b_info['thumb'] if t.get('aspect') == 'poster'), None)
                                    b_im_obj = b_SiteClass.imopen(b_target_url) if b_target_url else None
                                    b_is_valid = False
                                    if b_im_obj is not None:
                                        try:
                                            if not b_SiteClass.is_placeholder_image(b_im_obj):
                                                b_is_valid = True
                                        finally:
                                            b_im_obj.close()
                                    
                                    if b_is_valid:
                                        logger.info(f"[{self.name}] '{b_site}'에서 유효한 백업 이미지 획득/교체 성공 ({ui_code})")
                                        ret['thumb'] = b_info['thumb']
                                        ret['fanart'] = b_info.get('fanart', [])
                                        is_invalid_image = False
                                        break
                        if not is_invalid_image:
                            break
                except Exception as e_rescue:
                    logger.debug(f"[{self.name}] '{b_site}' 구출 시도 중 예외: {e_rescue}")

        ret["plex_is_proxy_preview"] = True
        ret["plex_is_landscape_to_art"] = True
        ret["plex_art_count"] = len(ret.get("fanart", []))

        actors = ret.get("actor") or []
        actor_names_for_log = []

        # 배우 처리는 시스템 기본 설정대로 항상 온전하게 수행 (로컬 이미지 서버 저장 포함)
        if actors:
            for item in actors:
                self.process_actor(item)
                if isinstance(item, dict):
                    actor_names_for_log.append(item.get("name_ko") or item.get("name_org", "?"))
                else:
                    actor_names_for_log.append(getattr(item, "name_ko", "") or getattr(item, "name_org", "?"))

        # 번역을 수행한 경우에만 오역된 배우명을 정식 한글 표기명으로 치환
        if not skip_trans:
            instance = self.site_map.get(site, None)
            if instance:
                for item in actors:
                    try:
                        if isinstance(item, dict):
                            name_ja = item.get("name_org")
                            name_ko = item.get("name_ko")
                        else:
                            name_ja = getattr(item, "name_org", "")
                            name_ko = getattr(item, "name_ko", "")

                        if name_ja and name_ko and name_ja != name_ko:
                            name_trans = instance.trans(name_ja)
                            if name_trans != name_ko:
                                if ret.get("plot"): ret["plot"] = ret["plot"].replace(name_trans, name_ko)
                                if ret.get("tagline"): ret["tagline"] = ret["tagline"].replace(name_trans, name_ko)
                                for extra in ret.get("extras") or []:
                                    if extra.get("title"): extra["title"] = extra["title"].replace(name_trans, name_ko)
                    except Exception as e_act_rep:
                        logger.debug(f"[{self.name}] 배우 이름 치환 예외: {e_act_rep}")

        original_calculated_title = ret.get("title", "")
        try:
            title_format = P.ModelSetting.get(f"{self.name}_title_format")
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
                    if isinstance(extra, dict): extra["title"] = final_title
                    elif hasattr(extra, 'title'): extra.title = final_title

        except Exception as e_fmt:
            logger.exception(f"타이틀 포맷팅 중 예외 발생: {e_fmt}")
            ret["title"] = original_calculated_title

        if "tag" in ret:
            tag_option = P.ModelSetting.get(f"{self.name}_tag_option")
            if tag_option == "not_using":
                ret["tag"] = []
            elif tag_option == "label":
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                ret["tag"] = [label] if label else []
            elif tag_option == "site":
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                ret["tag"] = [_ for _ in ret.get("tag", []) if label is None or _ != label]

        if ret:
            title_log = ret.get('title', 'No Title')
            year_log = ret.get('year', '????')
            logger.info(f"[{site.upper()} Success] Code: {code}, Title: {title_log} ({year_log})")

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


    def info2(self, code, site, keyword, ps_url=None, extra_opts=None, **kwargs):
        opts = dict(extra_opts or {})
        opts.update(kwargs)

        SiteClass = self.site_map.get(site, None)
        if SiteClass is None: return None
        try:
            data = SiteClass.info(code, keyword=keyword, extra_opts=opts)
            if data and data.get("ret") == "success" and data.get("data"):
                return data["data"]
        except Exception as e:
            logger.exception(f"info2 error ({site}, {code}): {e}")
        return None

    # endregion INFO
    ################################################

    ################################################
    # region ACTOR

    def process_actor(self, entity_actor):
        # 배우 탐색 순서 및 처리는 항상 전역 설정에 따라 정규 수행
        actor_site_list = P.ModelSetting.get_list(f"{self.name}_actor_order", ",")
        for site in actor_site_list:
            is_avdbs = site == 'avdbs'
            if self.process_actor2(entity_actor, site, is_avdbs=is_avdbs):
                return


    def process_actor2(self, entity_actor, site, is_avdbs=False) -> bool:
        if isinstance(entity_actor, dict):
            name_org = entity_actor.get("name_org")
        else:
            name_org = getattr(entity_actor, "name_org", None)

        if not name_org: return False

        SiteClass = self.site_map.get(site, None)
        if SiteClass is None: return False

        get_info_success = False
        try:
            if SiteClass in [SiteAvdbs]:
                get_info_success = SiteClass.get_actor_info(entity_actor)
                if get_info_success and entity_actor.get('site') == 'avdbs_web':
                    image_mode = P.ModelSetting.get('jav_censored_image_mode')
                    if image_mode == 'image_server':
                        try: SiteClass.save_actor_image(entity_actor)
                        except Exception as e: logger.error(f"배우 이미지 저장 실패: {e}")
        except Exception as e:
            get_info_success = False

        return get_info_success

    # endregion ACTOR
    ################################################
