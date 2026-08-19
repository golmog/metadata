# -*- coding: utf-8 -*-
import os
import re
import traceback
import json
import sqlite3
from datetime import datetime
import requests

from flask import jsonify, send_file
from io import BytesIO
from urllib.parse import urlparse, parse_qs, quote
from sqlalchemy import func

from .setup import *
from support_site.site_av.site_tpdb import SiteTpdb
from support_site.site_av.site_stashdb import SiteStashdb
from support_site.site_av.site_av_base import SiteAvBase
from support_site.entity_av import EntityAVSearch
from support_site import UtilNfo

class ModuleWestern(PluginModuleBase):
    
    def __init__(self, P):
        super(ModuleWestern, self).__init__(P, name='western', first_menu='setting')
        self.category = 'WEST'
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
            f"{self.name}_title_format": "[{studio}] {actor} - {title}",
            f"{self.name}_tag_option": "studio",
            f"{self.name}_use_extras": "False",

            f"{self.name}_search_regex_removal": r"[._\-\s]+xxx[._\-\s]+(?:internal|remastered|webrip|web-dl)?[._\-\s]*\d+[pk][._\-\s]+.*$",
            f"{self.name}_search_regex_removal_2nd": r"(?:solo|vr)$",

            f"{self.name}_trust_single_result": "False",

            f"{self.name}_use_proxy": "False",
            f"{self.name}_proxy_url": "",
            f"{self.name}_use_trailer_proxy": "False",

            f"{self.name}_use_movie_title_format": "True",
            f"{self.name}_movie_title_format": "[{studio}] {title}",

            f"{self.name}_use_smart_crop": "False",
            f"{self.name}_poster_force_studios": "",

            f"{self.name}_image_mode": "image_server",
            f"{self.name}_image_server_url": f"{F.SystemModelSetting.get('ddns')}/images",
            f"{self.name}_image_server_local_path": "/data/images",
            f"{self.name}_image_server_save_format": "/western/{studio}",
            f"{self.name}_image_server_rewrite": "True",
            
            # 로컬 DB 캐시 설정
            f"{self.name}_db_use": "False",
            f"{self.name}_db_save": "False",
            f"{self.name}_db_save_only_translated": "True",
            f"{self.name}_db_auto_enrich": "True",
            f"{self.name}_enrich_delay": "2.0",
            f"{self.name}_db_import_path": "",
            f"{self.name}_db_image_url_mapping": "",
        }

        self.enrich_status = {
            'is_running': False, 'status': '대기 중', 'total': 0,
            'current': 0, 'success': 0, 'fail': 0, 'current_code': '', 'stop_flag': False
        }

        try:
            self.keyword_cache = F.get_cache(f"{P.package_name}_{self.name}_keyword_cache")
        except Exception:
            self.keyword_cache = {}


    ################################################
    # region PluginModuleBase 메서드 오버라이드

    def plugin_load(self):
        try:
            from .model_metadata_db import engine, Base, ModelAvMetadata
            Base.metadata.create_all(bind=engine)
            self.web_list_model = ModelAvMetadata
        except Exception as e:
            logger.error(f"[{self.name}] DB Init Error: {e}")
        self._set_site_setting()


    def plugin_load_celery(self):
        self._set_site_setting()


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
            logger.debug(f"[{self.name}] process_ajax 요청됨 - command: {command}")
            
            custom_commands = [
                'test', 'db_list', 'db_edit_save', 'db_delete', 'db_clear', 'db_vacuum',
                'db_import', 'db_export', 'db_enrich_start', 'db_enrich_stop',
                'db_enrich_status', 'db_refresh_image'
            ]
            if command in custom_commands:
                res = self.process_command(command, req.form.get('arg1'), req.form.get('arg2'), req.form.get('arg3'), req)
                return res if res is not None else jsonify({'ret': 'error', 'msg': '처리 결과가 없습니다.'})
                
            res = super(ModuleWestern, self).process_ajax(sub, req)
            return res if res is not None else jsonify({'ret': 'error', 'msg': '기본 처리 결과가 없습니다.'})
        except Exception as e:
            logger.error(f"[{self.name}] Exception in process_ajax: {e}")
            logger.error(traceback.format_exc())
            return jsonify({'ret': 'error', 'msg': str(e)})


    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            ret = {'ret': 'success'}

            # --- 1. 웹 UI 검색 테스트 ---
            if command == "test":
                call = arg1 # 'stashdb' 또는 'tpdb'
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

            # --- 2. 로컬 DB 리스트 조회 ---
            elif command == 'db_list':
                from .model_metadata_db import ModelAvMetadata
                return jsonify(ModelAvMetadata.web_list(req, category=self.category))

            # --- 3. 로컬 DB JSON 직접 수정 저장 ---
            elif command == 'db_edit_save':
                from .model_metadata_db import ModelAvMetadata
                code = arg1
                raw_json_str = arg2
                logger.info(f"[{self.name}] DB Edit Save 요청 - code: {code}")
                if not raw_json_str:
                    return jsonify({'ret': 'error', 'msg': '수정할 JSON 데이터가 전달되지 않았습니다.'})
                try:
                    new_json = json.loads(raw_json_str)
                    success = ModelAvMetadata.update_json(code, new_json)
                    if success:
                        return jsonify({'ret': 'success', 'msg': 'DB에 성공적으로 반영되었습니다.'})
                    else:
                        return jsonify({'ret': 'error', 'msg': 'DB 업데이트 실패 (해당 코드 없음)'})
                except json.JSONDecodeError as je:
                    logger.error(f"[{self.name}] JSON 문법 에러: {je}")
                    return jsonify({'ret': 'error', 'msg': '올바른 JSON 형식이 아닙니다.'})
                except Exception as e:
                    logger.error(f"[{self.name}] DB Edit Save 예외: {e}")
                    return jsonify({'ret': 'error', 'msg': str(e)})

            # --- 4. 로컬 DB 단일 레코드 삭제 ---
            elif command == 'db_delete':
                from .model_metadata_db import ModelAvMetadata
                code = arg1
                success = ModelAvMetadata.delete_record(code)
                return jsonify({'ret': 'success'} if success else {'ret': 'error', 'msg': '삭제 실패'})

            # --- 5. 로컬 DB 전체 초기화 ---
            elif command == 'db_clear':
                from .model_metadata_db import ModelAvMetadata
                success, count = ModelAvMetadata.clear_db(self.category)
                return jsonify({'ret': 'success', 'msg': f'{count}건의 메타데이터가 삭제되었습니다.'} if success else {'ret': 'error', 'msg': '초기화 실패'})

            # --- 6. 로컬 DB VACUUM 최적화 ---
            elif command == 'db_vacuum':
                from .model_metadata_db import ModelAvMetadata
                success = ModelAvMetadata.vacuum_db()
                return jsonify({'ret': 'success', 'msg': 'DB 최적화(VACUUM) 완료'} if success else {'ret': 'error', 'msg': '최적화 실패'})

            # --- 7. 스마트 병합 / 누락분 Import ---
            elif command == 'db_import':
                from .model_metadata_db import ModelAvMetadata, av_db_session
                raw_paths = arg1
                mode = arg2 # 'update' or 'missing'
                auto_enrich = (arg3 == 'true')
                delay = float(req.form.get('delay', 2.0))
                
                import_paths = [p.strip() for p in raw_paths.split('\n') if p.strip()]
                
                try:
                    insert_count, update_count, skip_count = 0, 0, 0
                    batch_size = 500
                    processed_in_batch = 0
                    
                    for import_path in import_paths:
                        if not os.path.exists(import_path):
                            logger.warning(f"[{self.name}] Import 경로 없음: {import_path}")
                            continue

                        # Case A: .db 파일 병합
                        if os.path.isfile(import_path) and import_path.lower().endswith(('.db', '.sqlite')):
                            try:
                                conn = sqlite3.connect(import_path)
                                c = conn.cursor()
                                c.execute("SELECT category, code, json_data FROM av_metadata_cache WHERE category = ?", (self.category,))
                                rows = c.fetchall()
                                total_rows = len(rows)
                                conn.close()

                                logger.info(f"[{self.name}] DB 파일 내 {total_rows}개 레코드 병합 시작 -> {import_path}")

                                for idx, (r_cat, r_code, r_json) in enumerate(rows, 1):
                                    try:
                                        jd = json.loads(r_json) if isinstance(r_json, str) else r_json
                                        res = ModelAvMetadata.merge_record(self.category, jd, mode=mode)
                                        if res == 'inserted': insert_count += 1
                                        elif res == 'updated': update_count += 1
                                        else: skip_count += 1

                                        processed_in_batch += 1
                                        if processed_in_batch >= batch_size:
                                            av_db_session.commit()
                                            processed_in_batch = 0
                                            percent = (idx / total_rows) * 100 if total_rows > 0 else 100
                                            logger.info(f"[{self.name}] DB Import 진행 중: {idx}/{total_rows} ({percent:.1f}%) | 신규: {insert_count}, 갱신: {update_count}, 스킵: {skip_count}")
                                    except Exception as e_row:
                                        logger.error(f"Row 파싱 에러 ({r_code}): {e_row}")

                                if processed_in_batch > 0:
                                    av_db_session.commit()
                                    processed_in_batch = 0
                                    logger.info(f"[{self.name}] DB Import 진행 중: {total_rows}/{total_rows} (100.0%) | 최종 커밋 완료")

                            except Exception as e_db_file:
                                logger.error(f"[{self.name}] DB 파일 읽기 실패: {e_db_file}")

                        # Case B: 폴더 내 .json 파일들 병합
                        else:
                            json_files = []
                            if os.path.isfile(import_path) and import_path.lower().endswith('.json'):
                                json_files.append(import_path)
                            else:
                                for root, _, files in os.walk(import_path):
                                    for f in files:
                                        if f.lower().endswith('.json'):
                                            json_files.append(os.path.join(root, f))
                            
                            total_files = len(json_files)
                            logger.info(f"[{self.name}] JSON 파일 {total_files}개 병합 시작 -> {import_path}")

                            for idx, jf in enumerate(json_files, 1):
                                try:
                                    with open(jf, 'r', encoding='utf-8') as file:
                                        data = json.load(file)
                                        res = ModelAvMetadata.merge_record(self.category, data, mode=mode)
                                        if res == 'inserted': insert_count += 1
                                        elif res == 'updated': update_count += 1
                                        else: skip_count += 1

                                        processed_in_batch += 1
                                        if processed_in_batch >= batch_size:
                                            av_db_session.commit()
                                            processed_in_batch = 0
                                            percent = (idx / total_files) * 100 if total_files > 0 else 100
                                            logger.info(f"[{self.name}] JSON Import 진행 중: {idx}/{total_files} ({percent:.1f}%) | 신규: {insert_count}, 갱신: {update_count}, 스킵: {skip_count}")
                                except Exception as e_jf:
                                    logger.error(f"JSON 파일 파싱 에러 ({jf}): {e_jf}")

                            if processed_in_batch > 0:
                                av_db_session.commit()
                                processed_in_batch = 0
                                logger.info(f"[{self.name}] JSON Import 진행 중: {total_files}/{total_files} (100.0%) | 최종 커밋 완료")

                    ModelAvMetadata.checkpoint_wal()

                    final_msg = f"병합 완료! (신규 등록: {insert_count}건, 번역/메타 갱신: {update_count}건, 건너뜀: {skip_count}건)"
                    logger.info(f"[{self.name}] {final_msg}")

                    if auto_enrich:
                        if not self.enrich_status['is_running']:
                            import threading
                            t = threading.Thread(target=self._run_enrichment_worker, args=(delay,))
                            t.daemon = True
                            t.start()
                            final_msg += " ➔ [미디어 일괄 채우기] 작업을 백그라운드에서 시작했습니다."

                    return jsonify({'ret': 'success', 'msg': final_msg})

                except Exception as e:
                    logger.error(f"[{self.name}] DB Import 치명적 오류: {e}")
                    av_db_session.rollback()
                    return jsonify({'ret': 'error', 'msg': str(e)})

            # --- 8. 로컬 DB Export ---
            elif command == 'db_export':
                from .model_metadata_db import ModelAvMetadata
                mode = arg1 # 'current' or 'all'
                try:
                    tmp_dir = os.path.join(path_data, 'tmp')
                    os.makedirs(tmp_dir, exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    category_suffix = "WEST" if mode == 'current' else "ALL"
                    filename = f"metadata_av_{category_suffix}_{timestamp}.db"
                    filepath = os.path.join(tmp_dir, filename)

                    new_conn = sqlite3.connect(filepath)
                    new_c = new_conn.cursor()
                    new_c.execute('''CREATE TABLE av_metadata_cache (
                        id INTEGER PRIMARY KEY,
                        category VARCHAR(20) NOT NULL,
                        code VARCHAR(100) NOT NULL,
                        originaltitle VARCHAR(255) NOT NULL,
                        site VARCHAR(50) NOT NULL,
                        title VARCHAR(255) NOT NULL,
                        poster_url VARCHAR(500),
                        json_data JSON NOT NULL,
                        created_time DATETIME,
                        updated_time DATETIME
                    )''')

                    records = ModelAvMetadata.query.filter_by(category=self.category).all() if mode == 'current' else ModelAvMetadata.query.all()
                    count = 0
                    for r in records:
                        clean_data = ModelAvMetadata.sanitize_for_export(r.json_data)
                        c_time = r.created_time.strftime('%Y-%m-%d %H:%M:%S') if r.created_time else None
                        u_time = r.updated_time.strftime('%Y-%m-%d %H:%M:%S') if r.updated_time else None
                        new_c.execute('''INSERT INTO av_metadata_cache
                        (category, code, originaltitle, site, title, poster_url, json_data, created_time, updated_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (r.category, r.code, r.originaltitle, r.site, r.title, '', json.dumps(clean_data, ensure_ascii=False), c_time, u_time))
                        count += 1

                    new_conn.commit()
                    new_conn.close()
                    return jsonify({'ret': 'success', 'msg': f'{count}개의 데이터가 포함된 DB 파일 준비 완료', 'filename': filename})
                except Exception as e:
                    logger.error(f"[{self.name}] DB Export Error: {e}")
                    return jsonify({'ret': 'error', 'msg': str(e)})

            # --- 9. 미디어 일괄 채우기 (Enrichment) 제어 ---
            elif command == 'db_enrich_start':
                if self.enrich_status['is_running']:
                    return jsonify({'ret': 'warning', 'msg': '이미 일괄 작업이 진행 중입니다.'})
                delay = float(arg1) if arg1 else 2.0
                import threading
                t = threading.Thread(target=self._run_enrichment_worker, args=(delay,))
                t.daemon = True
                t.start()
                return jsonify({'ret': 'success', 'msg': '일괄 미디어 채우기 작업을 시작했습니다.'})

            elif command == 'db_enrich_stop':
                self.enrich_status['stop_flag'] = True
                return jsonify({'ret': 'success', 'msg': '작업 중단을 요청했습니다. 현재 처리 중인 항목 완료 후 멈춥니다.'})

            elif command == 'db_enrich_status':
                return jsonify({'ret': 'success', 'data': self.enrich_status})

            # --- 10. 단일 항목 이미지 최신 갱신 ---
            elif command == 'db_refresh_image':
                from .model_metadata_db import ModelAvMetadata
                code = arg1
                cached_json = ModelAvMetadata.get_metadata(code)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})
                
                # 식별자 판별 (S: stashdb, P: tpdb)
                site_key = 'stashdb' if len(code) > 1 and code[1] == 'S' else 'tpdb'
                SiteClass = self.site_map.get(site_key)
                if not SiteClass:
                    return jsonify({'ret': 'error', 'msg': f"사이트 클래스를 찾을 수 없습니다: {site_key}"})
                
                res = SiteClass.info(code, fp_meta_mode=False, skip_trans=True)
                if res and res.get('ret') == 'success' and res.get('data'):
                    fresh_ret = res['data']
                    cached_json['thumb'] = fresh_ret.get('thumb', [])
                    cached_json['fanart'] = fresh_ret.get('fanart', [])
                    if fresh_ret.get('extras'):
                        for extra in fresh_ret['extras']:
                            if isinstance(extra, dict): extra['title'] = cached_json.get('title', '')
                            elif hasattr(extra, 'title'): extra.title = cached_json.get('title', '')
                        cached_json['extras'] = fresh_ret['extras']
                    
                    ModelAvMetadata.save_metadata(self.category, cached_json)
                    return jsonify({'ret': 'success', 'msg': f"[{cached_json.get('originaltitle', code)}] 이미지 정보가 갱신되었습니다."})
                else:
                    return jsonify({'ret': 'warning', 'msg': '사이트에서 최신 이미지 정보를 가져오지 못했습니다.'})

            return jsonify(ret)
                
        except Exception as e:
            P.logger.error(f"[{self.name}] Exception: {str(e)}")
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})

    # endregion PluginModuleBase 메서드 오버라이드
    ################################################


    ################################################
    # region SEARCH & INFO

    def search(self, keyword, manual=False, media_path=None):
        target_video_file = media_path
        if not target_video_file and os.path.isabs(keyword) and os.path.exists(keyword):
            target_video_file = keyword
            cleaned_keyword = self._clean_search_keyword(os.path.splitext(os.path.basename(keyword))[0])
        else:
            cleaned_keyword = self._clean_search_keyword(keyword)

        logger.info(f"======= Western search START - keyword:[{cleaned_keyword}] video:[{target_video_file}] manual:[{manual}] =======")
        all_results = []
        
        # 1. Local DB 캐시 선행 검색
        use_db = P.ModelSetting.get_bool(f"{self.name}_db_use")
        if use_db and not manual:
            try:
                from .model_metadata_db import ModelAvMetadata, av_db_session
                
                # (1) 비디오 파일이 있거나 검색어가 해시 문자열일 때 로컬 DB 해시 매칭 대조
                target_hash = None
                if target_video_file and os.path.exists(target_video_file):
                    target_hash = SiteAvBase.calculate_oshash(target_video_file)
                elif re.match(r'^[0-9a-fA-F]{16}$', keyword.strip()):
                    target_hash = keyword.strip().lower()

                if target_hash:
                    db_hash_record = av_db_session.query(ModelAvMetadata).filter(
                        ModelAvMetadata.category == 'WEST',
                        func.json_extract(ModelAvMetadata.json_data, '$.extra_info.oshash') == target_hash
                    ).first()
                    
                    if not db_hash_record:
                        db_hash_record = av_db_session.query(ModelAvMetadata).filter(
                            ModelAvMetadata.category == 'WEST',
                            func.json_extract(ModelAvMetadata.json_data, '$.extra_info.phash') == target_hash
                        ).first()

                    if db_hash_record:
                        logger.info(f"[{self.name}] ★★★ Local DB Fingerprint Match Hit! (Hash: {target_hash})")
                        db_item = self._create_search_item_from_record(db_hash_record, 105)
                        item_dict = db_item.as_dict()
                        item_dict['score'] = 100
                        return [item_dict]

                # (2) 텍스트 기반 로컬 DB 검색
                kw_norm = re.sub(r'[^a-zA-Z0-9]', '', cleaned_keyword).lower()
                query_kw = f"%{cleaned_keyword.replace('-', '%')}%"
                db_records = av_db_session.query(ModelAvMetadata).filter(
                    ModelAvMetadata.category == 'WEST',
                    ModelAvMetadata.originaltitle.ilike(query_kw)
                ).all()

                for record in db_records:
                    record_orig_norm = re.sub(r'[^a-zA-Z0-9]', '', record.originaltitle).lower()
                    if kw_norm == record_orig_norm:
                        db_item = self._create_search_item_from_record(record, 105)
                        item_dict = db_item.as_dict()
                        item_dict['score'] = 100
                        all_results.append(item_dict)

                if all_results:
                    return all_results
            except Exception as e_db:
                logger.error(f"[{self.name}] DB Search Error: {e_db}")

        # 2. 사이트 순환 검색 (western_order: "stashdb, tpdb")
        site_order_list = [s.strip().lower() for s in P.ModelSetting.get_list(f"{self.name}_order", ",") if s.strip()]
        early_exit_triggered = False

        for site_key in site_order_list:
            if early_exit_triggered: break
            SiteClass = self.site_map.get(site_key)
            if not SiteClass: continue

            try:
                data = SiteClass.search(cleaned_keyword, manual=manual, media_path=target_video_file)
                if data and data.get("ret") == "success" and data.get("data"):
                    results = data["data"]
                    for item in results:
                        item['site_key'] = site_key
                        all_results.append(item)
                        
                        # 자동 검색 시 100점 매칭 발견 시 즉시 조기 종료
                        if not manual and item.get('score', 0) >= 100:
                            logger.info(f"[{self.name}] Early Exit: '{site_key}'에서 100점 매칭 발견. 순환 중단: {cleaned_keyword}")
                            early_exit_triggered = True
                            break
            except Exception as e_site:
                logger.error(f"[{self.name}] Error searching on {site_key}: {e_site}")

        # 3. 우선순위 정렬 및 동점자 처리
        if all_results:
            # (1) 사이트 우선순위 맵 생성 (stashdb=0, tpdb=1)
            priority_map = {site: idx for idx, site in enumerate(site_order_list)}
            default_prio = len(site_order_list)

            # (2) 1차 정렬: 점수 높은 순(내림차순) -> 사이트 우선순위 앞선 순(오름차순)
            all_results_sorted = sorted(
                all_results,
                key=lambda x: (-int(x.get('score', 0)), priority_map.get(x.get('site_key', '').lower(), default_prio))
            )

            # (3) 동점자 순위 분리 (Plex 화면에서 1위가 명확히 선택되도록 1점씩 차감)
            for i, item in enumerate(all_results_sorted):
                raw_score = int(round(item.get('score', 0)))
                raw_score = max(0, min(100, raw_score))

                if i == 0:
                    item['score'] = raw_score
                else:
                    prev_score = all_results_sorted[i-1]['score']
                    if raw_score >= prev_score:
                        item['score'] = max(0, prev_score - 1)
                    else:
                        item['score'] = raw_score

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
            logger.info(f"======= Western search END - No results found for: {cleaned_keyword} =======")

        return all_results


    def _create_search_item_from_record(self, record, score):
        db_item = EntityAVSearch(record.site)
        db_item.code = record.code
        db_item.ui_code = record.json_data.get('ui_code', record.originaltitle)
        db_item.title = f"📁 [DB 저장됨] {record.title}"
        db_item.originaltitle = record.originaltitle
        db_item.title_ko = db_item.title
        try: db_item.year = int(record.json_data.get('year', 1900))
        except: db_item.year = 1900
        db_item.image_url = record.poster_url or ''
        
        jd = record.json_data or {}
        studio_str = jd.get('studio', 'Unknown')
        actor_names = [a.get('name') if isinstance(a, dict) else str(a) for a in jd.get('actor', []) if a]
        actor_str = ", ".join(actor_names[:3]) if actor_names else "배우 정보 없음"
        premiered_str = jd.get('premiered', '') or (str(db_item.year) if db_item.year != 1900 else '미상')
        plot_snippet = (jd.get('plot', '')[:120] + "...") if len(jd.get('plot', '')) > 120 else (jd.get('plot', '') or "줄거리 없음")

        db_item.desc = f"스튜디오: {studio_str} | 출시: {premiered_str} | 출연: {actor_str}\n{plot_snippet}"
        db_item.score = score
        db_item.content_type = jd.get('content_type', 'movie')
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


    def info(self, code, keyword=None, fp_meta_mode=False, skip_trans=False, media_path=None):
        if len(code) < 3 or code[0] != 'W':
            logger.error(f"[{self.name}] 처리할 수 없는 코드: {code}")
            return None

        # 식별자 판별 (S: stashdb, P: tpdb)
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
        except Exception:
            pass

        use_db = P.ModelSetting.get_bool(f"{self.name}_db_use")
        save_db = P.ModelSetting.get_bool(f"{self.name}_db_save")
        
        if use_db and not bypass_cache:
            from .model_metadata_db import ModelAvMetadata
            cached_json = ModelAvMetadata.get_metadata(code)
            
            if cached_json:
                is_db_untranslated = False
                db_plot = cached_json.get('plot', '')
                if db_plot and not skip_trans:
                    from support_site import SiteUtil
                    if not SiteUtil.is_include_hangul(db_plot):
                        is_db_untranslated = True

                if not is_db_untranslated:
                    logger.info(f"[{self.name}] DB 캐시 로드: {code}")
                    return cached_json

        data = None
        try:
            if site_key == 'stashdb':
                data = SiteClass.info(code, fp_meta_mode=fp_meta_mode, skip_trans=skip_trans, media_path=media_path)
            else:
                data = SiteClass.info(code, fp_meta_mode=fp_meta_mode, skip_trans=skip_trans)
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

        # --- 사용자 타이틀 포맷팅 및 태그 처리 (StashDB / TPDB 공통 적용) ---
        original_calculated_title = ret.get("title", "")
        safe_studio = ret.get("studio", "Unknown")
        type_char = code[2] if len(code) > 2 else 'S'
        content_type = 'movie' if type_char == 'M' else 'scene'

        actor_names = []
        for a in ret.get('actor', []):
            name = ""
            if isinstance(a, dict): name = str(a.get('name') or a.get('originalname') or "")
            elif hasattr(a, 'name'): name = str(a.name or a.originalname or "")
            if name: actor_names.append(name)

        actor_str = ", ".join(actor_names[:3]) if actor_names else ""
        year_val = ret.get("year", "")
        if not year_val and ret.get("premiered"):
            year_val = str(ret.get("premiered"))[:4]

        # JAV 표준 품번 파서 엔진(_parse_ui_code_uncensored / _parse_ui_code)을 통한 정규화
        studio_code = ""
        raw_code_candidate = ret.get('original', {}).get('code') or ""
        if not raw_code_candidate and original_calculated_title:
            match_code = re.search(r'\b([a-zA-Z0-9]{2,8}[-_]\d{2,7}|[a-zA-Z]{2,6}\d{3,5})\b', original_calculated_title)
            if match_code:
                raw_code_candidate = match_code.group(0)

        if raw_code_candidate:
            # (1) Uncensored 파서 우선 시도 (FC2, 1pondo, Heyzo, Carib 등)
            uncen_parsed = SiteAvBase._parse_ui_code_uncensored(raw_code_candidate)
            if uncen_parsed and '-' in uncen_parsed and not uncen_parsed.startswith(raw_code_candidate.upper()):
                studio_code = uncen_parsed.upper()
            else:
                # (2) Censored 파서 시도 (DMM/MGS 일반 품번: ssni00123 -> SSNI-123 등)
                cen_parsed, _, _ = SiteAvBase._parse_ui_code(raw_code_candidate)
                if cen_parsed and '-' in cen_parsed:
                    studio_code = cen_parsed.upper()
                else:
                    studio_code = uncen_parsed.upper() if uncen_parsed else raw_code_candidate.upper()

        # JAV 표준 장르 번역 (tags.json 사전 및 trans 엔진 적용)
        if ret.get('genre'):
            translated_genres = []
            for g in ret['genre']:
                t_g = SiteAvBase.get_translated_tag('uncen_tags', g)
                if t_g and t_g not in translated_genres:
                    translated_genres.append(t_g)
            ret['genre'] = translated_genres

        # 제목 번역 옵션(western_trans_title)에 따른 포맷팅 대상 제목 결정
        trans_title_enabled = P.ModelSetting.get_bool(f"{self.name}_trans_title")
        if trans_title_enabled is None:
            trans_title_enabled = True

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

        # Movie 포맷은 TPDB 전용으로만 분기 적용
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
            logger.error(f"[{self.name}] 타이틀 포맷 오류: {e_fmt}")
            ret["title"] = original_calculated_title

        # 태그(컬렉션) 옵션 처리
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

        logger.info(f"[{self.name}] Info Success: {code} -> {ret['title']} ({ret.get('year', '')})")

        # DB 저장
        save_only_trans = P.ModelSetting.get_bool(f"{self.name}_db_save_only_translated")
        should_save = save_db and ret
        if should_save and save_only_trans and skip_trans:
            should_save = False

        if should_save:
            from .model_metadata_db import ModelAvMetadata
            ModelAvMetadata.save_metadata(self.category, ret)

        return ret

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

            if sub == "user_image_update":
                return self._api_user_image_update(req)

            return jsonify({'ret': 'failed', 'msg': f'Invalid sub command: {sub}'}), 400
        except Exception as e:
            logger.error(f"[{self.name}] Exception in process_api (sub={sub}): {e}")
            logger.error(traceback.format_exc())
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
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_code", keyword)

                SiteClass = self.site_map.get(call)
                search_result_dict = SiteClass.search(keyword, manual=True)
                
                if search_result_dict and search_result_dict.get('ret') == 'success' and search_result_dict.get('data'):
                    search_results = search_result_dict['data']
                    real_code = search_results[0]['code']
                    
                    info = self.info(real_code, keyword=keyword)
                    if info:
                        filename = get_download_filename(info, "nfo")
                        return UtilNfo.make_nfo_movie(info, output="file", filename=filename)

        elif sub == "yaml_download":
            keyword = req.args.get("code")
            call = req.args.get("call")
            if call in self.site_map:
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_code", keyword)

                SiteClass = self.site_map.get(call)
                search_result_dict = SiteClass.search(keyword, manual=True)
                
                if search_result_dict and search_result_dict.get('ret') == 'success' and search_result_dict.get('data'):
                    search_results = search_result_dict['data']
                    real_code = search_results[0]['code']

                    info = self.info(real_code, keyword=keyword)
                    if info:
                        filename = get_download_filename(info, "yaml")
                        return UtilNfo.make_yaml_movie(info, output="file", filename=filename)

        elif sub == "image_download":
            try:                
                keyword = req.args.get("code")
                call = req.args.get("call")
                image_type = req.args.get("type") 
                
                if call in self.site_map:
                    db_prefix = f"{self.name}_{call}"
                    P.ModelSetting.set(f"{db_prefix}_test_code", keyword)
                    
                    SiteClass = self.site_map.get(call)
                    search_result_dict = SiteClass.search(keyword, manual=True)
                    
                    if not search_result_dict or search_result_dict.get('ret') != 'success' or not search_result_dict.get('data'):
                        return "Search failed", 404
                    
                    search_results = search_result_dict['data']
                    real_code = search_results[0]['code']

                    info = self.info(real_code, keyword=keyword)
                    if not info:
                        return "Info failed", 404

                    target_url = None
                    target_aspect = 'poster' if image_type == 'p' else 'landscape'
                    
                    for thumb in info.get('thumb', []):
                        if thumb.get('aspect') == target_aspect:
                            target_url = thumb.get('value')
                            break
                    
                    if not target_url and image_type == 'pl' and info.get('fanart'):
                        target_url = info['fanart'][0]
                    
                    if not target_url:
                        return f"Image type '{image_type}' not found in metadata", 404

                    try:
                        img_res = requests.get(target_url, verify=False, timeout=30)
                        if img_res.status_code != 200:
                            return f"Failed to download image from {target_url}", 500
                    except Exception as e_req:
                        return f"Request error for {target_url}: {e_req}", 500

                    filename = get_download_filename(info, "jpg", suffix=image_type)
                    
                    return send_file(
                        BytesIO(img_res.content),
                        as_attachment=True,
                        download_name=filename,
                        mimetype='image/jpeg'
                    )
                
            except Exception as e:
                logger.error(f"Image download error: {e}")
                logger.error(traceback.format_exc())
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
        ret = {
            'ret': 'success', 'msg': '', 'total_input': 0, 'updated_count': 0,
            'skipped_count': 0, 'not_found_count': 0, 'updated_items': [],
            'not_found_files': [], 'errors': []
        }
        try:
            from .model_metadata_db import ModelAvMetadata, av_db_session
            files = []
            if req.is_json:
                json_body = req.get_json(silent=True) or {}
                if isinstance(json_body, list): files = json_body
                elif isinstance(json_body, dict):
                    files = json_body.get('files') or json_body.get('filenames') or []
            
            if not files:
                raw_files = req.form.get('files') or req.args.get('files')
                if raw_files:
                    try:
                        p = json.loads(raw_files)
                        files = p if isinstance(p, list) else [p]
                    except:
                        files = [f.strip() for f in re.split(r'[\n,]', raw_files) if f.strip()]

            if not files:
                ret['ret'] = 'warning'
                ret['msg'] = '업데이트할 파일 목록(files)이 전달되지 않았습니다.'
                return jsonify(ret), 200

            ret['total_input'] = len(files)
            batch_size = 100
            processed_in_batch = 0

            for filename in files:
                if not filename or not isinstance(filename, str): continue
                res, code, detail = ModelAvMetadata.update_user_image_by_filename(filename)
                
                if res == 'updated':
                    ret['updated_count'] += 1
                    ret['updated_items'].append(detail)
                    processed_in_batch += 1
                elif res == 'not_found':
                    ret['not_found_count'] += 1
                    ret['not_found_files'].append(filename)
                elif res == 'skipped':
                    ret['skipped_count'] += 1
                elif res == 'error':
                    ret['errors'].append({'file': filename, 'error': detail})

                if processed_in_batch >= batch_size:
                    av_db_session.commit()
                    processed_in_batch = 0

            if processed_in_batch > 0:
                av_db_session.commit()

            ModelAvMetadata.checkpoint_wal()

            ret['msg'] = f"총 {len(files)}개 중 {ret['updated_count']}개 DB 레코드 업데이트 완료"
            logger.info(f"[{self.name}] User Image API: {ret['msg']}")
            return jsonify(ret), 200

        except Exception as e:
            logger.error(f"[{self.name}] user_image_update API Exception: {e}")
            ret['ret'] = 'error'
            ret['code'] = 'INTERNAL_EXCEPTION'
            ret['msg'] = str(e)
            return jsonify(ret), 200


    def _run_enrichment_worker(self, delay):
        from .model_metadata_db import ModelAvMetadata, av_db_session
        import time

        try:
            records = av_db_session.query(ModelAvMetadata).filter_by(category=self.category).all()
            targets = [r for r in records if not r.json_data.get('thumb')]

            self.enrich_status.update({
                'is_running': True, 'status': '작업 중', 'total': len(targets),
                'current': 0, 'success': 0, 'fail': 0, 'current_code': '', 'stop_flag': False
            })

            logger.info(f"[{self.name}] 일괄 미디어 채우기 시작 - 대상: {len(targets)}건, 딜레이: {delay}초")

            if not targets:
                self.enrich_status.update({'is_running': False, 'status': '채울 항목 없음 (완료)'})
                return

            for idx, record in enumerate(targets):
                if self.enrich_status['stop_flag']:
                    logger.info(f"[{self.name}] 일괄 작업이 사용자에 의해 중단되었습니다.")
                    self.enrich_status['status'] = '중단됨'
                    break

                code = record.code
                self.enrich_status['current'] = idx + 1
                self.enrich_status['current_code'] = record.originaltitle

                try:
                    res = self.info(code, skip_trans=True)
                    if res and res.get('thumb'):
                        self.enrich_status['success'] += 1
                    else:
                        self.enrich_status['fail'] += 1
                except Exception as e_item:
                    logger.error(f"[{self.name}] Enrichment 실패 ({code}): {e_item}")
                    self.enrich_status['fail'] += 1

                time.sleep(delay)

            if not self.enrich_status['stop_flag']:
                self.enrich_status['status'] = '완료'
                logger.info(f"[{self.name}] 일괄 작업 완료 (성공: {self.enrich_status['success']}, 실패: {self.enrich_status['fail']})")

            ModelAvMetadata.checkpoint_wal()

        except Exception as e_main:
            logger.error(f"[{self.name}] 일괄 작업 치명적 오류: {e_main}")
            self.enrich_status['status'] = f'오류 발생: {e_main}'
        finally:
            self.enrich_status['is_running'] = False

    # endregion API & DOWNLOADS
    ################################################
