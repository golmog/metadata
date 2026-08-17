# -*- coding: utf-8 -*-
import os
import re
import traceback
import requests

from flask import jsonify, send_file
from io import BytesIO
from urllib.parse import urlparse, parse_qs

from .setup import *
from support_site.site_av.site_tpdb import SiteTpdb
from support_site.site_av.site_av_base import SiteAvBase
from support_site.entity_av import EntityAVSearch
from support_site import UtilNfo

class ModuleWestern(PluginModuleBase):
    
    def __init__(self, P):
        super(ModuleWestern, self).__init__(P, name='western', first_menu='setting')
        self.category = 'WEST'
        self.site_map = {
            "tpdb": SiteTpdb,
        }

        self.db_default = {
            f"{self.name}_db_version": "1",
            
            f"{self.name}_tpdb_api_token": "",
            f"{self.name}_tpdb_test_code": "",

            f"{self.name}_trans_option": "using", 
            f"{self.name}_title_format": "[{studio}] {actor} - {title}",
            f"{self.name}_tag_option": "studio",
            f"{self.name}_use_extras": "False",

            f"{self.name}_search_regex_removal": r"[._\-\s]+xxx[._\-\s]+(?:internal|remastered|webrip|web-dl)?[._\-\s]*\d+[pk][._\-\s]+.*$",
            f"{self.name}_search_regex_removal_2nd": r"(?:solo|vr)$",

            f"{self.name}_trust_single_result": "True",

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
        try:
            P.logger.debug(f"[{self.name}] Setting config for SiteTpdb.")
            SiteTpdb.set_config(self.P.ModelSetting)
        except Exception as e:
            P.logger.error(f"[{self.name}] Error initializing site TPDB: {str(e)}")


    def process_ajax(self, sub, req):
        try:
            command = req.form.get('command')
            logger.debug(f"[{self.name}] process_ajax 요청됨 - command: {command}")
            
            custom_commands = [
                'db_edit_save', 'db_delete', 'db_clear', 'db_vacuum', 'db_import', 'db_export',
                'db_enrich_start', 'db_enrich_stop', 'db_enrich_status', 'db_refresh_image'
            ]
            if command in custom_commands:
                res = self.process_command(command, req.form.get('arg1'), req.form.get('arg2'), req.form.get('arg3'), req)
                return res if res is not None else jsonify({'ret': 'error', 'msg': '처리 결과가 없습니다.'})
                
            res = super(ModuleWestern, self).process_ajax(sub, req)
            return res if res is not None else jsonify({'ret': 'error', 'msg': '기본 처리 결과가 없습니다.'})
        except Exception as e:
            logger.error(f"[{self.name}] Exception in process_ajax: {e}")
            return jsonify({'ret': 'error', 'msg': str(e)})


    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            ret = {'ret': 'success'}
            if command == "test":
                code = arg2
                call = arg1 
                P.ModelSetting.set(f"{self.name}_{call}_test_code", code)
                SiteClass = self.site_map.get(call)
                if not SiteClass:
                    return jsonify({'ret': 'error', 'msg': f"Site '{call}' not found."})

                search_results = self.search(code, manual=True)
                if not search_results:
                    return jsonify({'ret': 'warning', 'msg': f"No results for '{code}'"})

                info_data = self.info(search_results[0]['code'], keyword=code)
                ret['json'] = {
                    "search": search_results,
                    "info": info_data if info_data else {}
                }
                return jsonify(ret)

            elif command == 'db_list':
                from .model_metadata_db import ModelAvMetadata
                return jsonify(ModelAvMetadata.web_list(req, category=self.category))

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

            elif command == 'db_delete':
                from .model_metadata_db import ModelAvMetadata
                code = arg1
                success = ModelAvMetadata.delete_record(code)
                return jsonify({'ret': 'success'} if success else {'ret': 'error', 'msg': '삭제 실패'})

            elif command == 'db_clear':
                from .model_metadata_db import ModelAvMetadata
                success, count = ModelAvMetadata.clear_db(self.category)
                return jsonify({'ret': 'success', 'msg': f'{count}건의 메타데이터가 삭제되었습니다.'} if success else {'ret': 'error', 'msg': '초기화 실패'})

            elif command == 'db_vacuum':
                from .model_metadata_db import ModelAvMetadata
                success = ModelAvMetadata.vacuum_db()
                return jsonify({'ret': 'success', 'msg': 'DB 최적화(VACUUM) 완료'} if success else {'ret': 'error', 'msg': '최적화 실패'})

            elif command == 'db_import':
                from .model_metadata_db import ModelAvMetadata, av_db_session
                import json, sqlite3
                
                raw_paths = arg1
                mode = arg2 # 'update' or 'missing'
                auto_enrich = (arg3 == 'true')
                delay = float(req.form.get('delay', 2.0))
                
                import_paths = [p.strip() for p in raw_paths.split('\n') if p.strip()]
                
                try:
                    insert_count, update_count, skip_count = 0, 0, 0
                    batch_size = 500  # 500건 단위 커밋 & 로그 출력 주기
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

                    # 대용량 작업 완료 후 WAL 파일 청소
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

            elif command == 'db_export':
                from .model_metadata_db import ModelAvMetadata
                import json, sqlite3
                from datetime import datetime

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

            elif command == 'db_refresh_image':
                from .model_metadata_db import ModelAvMetadata
                code = arg1
                cached_json = ModelAvMetadata.get_metadata(code)
                if not cached_json:
                    return jsonify({'ret': 'error', 'msg': 'DB에서 해당 항목을 찾을 수 없습니다.'})
                
                SiteClass = self.site_map.get("tpdb")
                res = SiteClass.info(code, fp_meta_mode=False, skip_trans=True)
                if res and res.get('ret') == 'success' and res.get('data'):
                    fresh_ret = res['data']
                    cached_json['thumb'] = fresh_ret.get('thumb', [])
                    cached_json['fanart'] = fresh_ret.get('fanart', [])
                    if fresh_ret.get('extras'):
                        for extra in fresh_ret['extras']:
                            if isinstance(extra, dict):
                                extra['title'] = cached_json.get('title', '')
                            elif hasattr(extra, 'title'):
                                extra.title = cached_json.get('title', '')
                        cached_json['extras'] = fresh_ret['extras']
                    
                    ModelAvMetadata.save_metadata(self.category, cached_json)
                    return jsonify({'ret': 'success', 'msg': f"[{cached_json.get('originaltitle', code)}] 이미지 및 미디어 정보가 갱신되었습니다."})
                else:
                    return jsonify({'ret': 'warning', 'msg': '사이트에서 최신 이미지 정보를 가져오지 못했습니다.'})

            return jsonify(ret)
                
        except Exception as e:
            P.logger.error(f"[{self.name}] Exception: {str(e)}")
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})


    def process_api(self, sub, req):
        try:
            call = req.args.get("call", "")
            if sub == "search" and call in ["plex", "kodi"]:
                keyword = req.args.get("keyword", "").strip()
                manual = req.args.get("manual") == "True"
                search_results = self.search(keyword, manual=manual)
                return jsonify(search_results)

            if sub == "info":
                code = req.args.get("code")
                data = self.info(code)
                return jsonify(data)

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
            import json, re

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


    def _clean_search_keyword(self, keyword):
        cleaned = keyword
        
        cleaned = re.sub(r'^\[[^\]]+\]\s*', '', cleaned)
        cleaned = re.sub(r'[\-_.]', ' ', cleaned)

        regex_string_1st = P.ModelSetting.get(f"{self.name}_search_regex_removal")
        if regex_string_1st and regex_string_1st.strip():
            patterns = [p.strip() for p in regex_string_1st.split('\n') if p.strip()]
            for pattern in patterns:
                try:
                    cleaned = re.sub(pattern, ' ', cleaned, flags=re.IGNORECASE).strip()
                except Exception as e:
                    logger.error(f"[{self.name}] 1차 정규식 오류 '{pattern}': {e}")
                    
        regex_string_2nd = P.ModelSetting.get(f"{self.name}_search_regex_removal_2nd")
        if regex_string_2nd and regex_string_2nd.strip():
            patterns = [p.strip() for p in regex_string_2nd.split('\n') if p.strip()]
            for pattern in patterns:
                try:
                    cleaned = re.sub(pattern, ' ', cleaned, flags=re.IGNORECASE).strip()
                except Exception as e:
                    logger.error(f"[{self.name}] 2차 정규식 오류 '{pattern}': {e}")

        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        
        if cleaned != keyword:
            logger.debug(f"[{self.name}] Keyword cleaned: '{keyword}' -> '{cleaned}'")
            return cleaned
            
        return keyword


    def search(self, keyword, manual=False):
        cleaned_keyword = self._clean_search_keyword(keyword)
        logger.info(f"======= Western search START - keyword:[{cleaned_keyword}] manual:[{manual}] =======")
        all_results = []
        
        use_db = P.ModelSetting.get_bool(f"{self.name}_db_use")
        if use_db and not manual:
            try:
                from .model_metadata_db import ModelAvMetadata, av_db_session
                kw_norm = re.sub(r'[^a-zA-Z0-9]', '', cleaned_keyword).lower()
                query_kw = f"%{cleaned_keyword.replace('-', '%')}%"
                db_records = av_db_session.query(ModelAvMetadata).filter(
                    ModelAvMetadata.category == self.category,
                    ModelAvMetadata.originaltitle.ilike(query_kw)
                ).all()

                valid_db_records = []
                for record in db_records:
                    record_orig_norm = re.sub(r'[^a-zA-Z0-9]', '', record.originaltitle).lower()
                    if kw_norm in record_orig_norm or record_orig_norm in kw_norm:
                        valid_db_records.append(record)

                for record in valid_db_records:
                    db_item = EntityAVSearch(record.site)
                    db_item.code = record.code
                    db_item.ui_code = record.json_data.get('ui_code', record.originaltitle)
                    db_item.title = f"📁 [DB 저장됨] {record.title}"
                    db_item.originaltitle = record.originaltitle
                    db_item.title_ko = db_item.title
                    try: db_item.year = int(record.json_data.get('year', 1900))
                    except: db_item.year = 1900
                    db_item.image_url = record.poster_url or ''
                    db_item.desc = record.json_data.get('plot', '')
                    db_item.score = 105
                    db_item.content_type = record.json_data.get('content_type', 'movie')

                    item_dict = db_item.as_dict()
                    item_dict['original_score'] = 105
                    item_dict['site_key'] = record.site
                    item_dict['is_db_cached'] = True
                    all_results.append(item_dict)

                if all_results:
                    logger.info(f"[{self.name}] Auto-match satisfied by Local DB.")
                    for item in all_results: item['score'] = min(100, item['score'])
                    return all_results
            except Exception as e_db:
                logger.error(f"[{self.name}] DB Search Error: {e_db}")

        live_results = []
        for site_name, SiteClass in self.site_map.items():
            try:
                data = SiteClass.search(cleaned_keyword, manual=manual)
                
                # --- [폴백 로직] 1차 검색 실패 시 날짜 패턴 제거 후 2차 검색 시도 ---
                if not data or data.get("ret") != "success" or not data.get("data"):
                    logger.debug(f"[{self.name}] 1st search failed on {site_name}. Fallback triggered.")
                    date_pattern = r'[ ._\-]*(?:\d{2}|\d{4})[ ._\-]\d{2}[ ._\-](?:\d{2}|\d{4})[ ._\-]*'
                    ep_pattern = r'[ ._\-]*(?:[e][p]?\d+)[ ._\-]*'
                    
                    fallback_keyword = re.sub(date_pattern, ' ', cleaned_keyword)
                    fallback_keyword = re.sub(ep_pattern, ' ', fallback_keyword, flags=re.IGNORECASE)
                    fallback_keyword = re.sub(r'\s+', ' ', fallback_keyword).strip()
                    
                    if fallback_keyword and fallback_keyword != cleaned_keyword:
                        logger.info(f"[{self.name}] Fallback search - keyword:[{fallback_keyword}]")
                        data = SiteClass.search(fallback_keyword, manual=manual)
                
                if data and data.get("ret") == "success" and data.get("data"):
                    results = data["data"]
                    for item in results:
                        item['site_key'] = site_name
                        studio_str = ""
                        match_studio = re.match(r'^\[(.*?)\]', item.get('title', ''))
                        if match_studio: studio_str = match_studio.group(1).lower()
                        if 'clip4sale' in studio_str:
                            item['score'] = max(0, item.get('score', 0) - 5)
                        live_results.append(item)
            except Exception as e:
                logger.error(f"[{self.name}] Error during search on site '{site_name}': {e}")
                
        if live_results:
            all_results.extend(live_results)

        if all_results:
            all_results = sorted(all_results, key=lambda k: k.get("score", 0), reverse=True)
            if manual:
                for item in all_results:
                    try: self.keyword_cache.set(f"BYPASS_{item['code']}", "1")
                    except AttributeError:
                        if not hasattr(self, 'keyword_cache'): self.keyword_cache = {}
                        self.keyword_cache[f"BYPASS_{item['code']}"] = "1"

        logger.debug(f"======= Western search END - Returning {len(all_results)} results. =======")
        return all_results


    def search2(self, keyword, site, manual=False):
        if site == "tpdb":
            return self.search(keyword, manual=manual)
        return None


    def info(self, code, keyword=None, fp_meta_mode=False, skip_trans=False):
        if code[0] != 'W':
            logger.error(f"[{self.name}] 처리할 수 없는 코드: {code}")
            return None
            
        site = "tpdb"
        SiteClass = self.site_map.get(site)

        logger.debug(f"[{self.name}] Info 조회 시작: Code='{code}', Keyword='{keyword}'")
        
        bypass_cache = False
        if not hasattr(self, 'keyword_cache'): self.keyword_cache = {}
        try:
            if self.keyword_cache.get(f"BYPASS_{code}") == "1":
                bypass_cache = True
                self.keyword_cache.set(f"BYPASS_{code}", "0")
        except AttributeError:
            if self.keyword_cache.get(f"BYPASS_{code}") == "1":
                bypass_cache = True
                self.keyword_cache[f"BYPASS_{code}"] = "0"

        if bypass_cache: logger.info(f"[{self.name}] 수동 갱신 요청 감지. DB를 무시합니다: {code}")

        use_db = P.ModelSetting.get_bool(f"{self.name}_db_use")
        save_db = P.ModelSetting.get_bool(f"{self.name}_db_save")
        SiteClass = self.site_map.get("tpdb")
        
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
                        logger.info(f"[{self.name}] DB 캐시에 한글 번역이 없어 캐시를 건너뛰고 새로 번역을 수행합니다: {code}")

                if not is_db_untranslated:
                    logger.info(f"[{self.name}] DB 캐시를 로드했습니다: {code}")
                    needs_enrichment = not cached_json.get('thumb')
                    
                    if needs_enrichment:
                        logger.info(f"[{self.name}] 이미지/트레일러 누락 감지. Enrichment를 수행합니다...")
                        fresh_data = SiteClass.info(code, fp_meta_mode=False, skip_trans=True)
                        if fresh_data and fresh_data.get('ret') == 'success' and fresh_data.get('data'):
                            fresh_ret = fresh_data['data']
                            cached_json['thumb'] = fresh_ret.get('thumb', [])
                            cached_json['fanart'] = fresh_ret.get('fanart', [])
                            if fresh_ret.get('extras'):
                                for extra in fresh_ret['extras']:
                                    if isinstance(extra, dict): extra['title'] = cached_json.get('title', '')
                                    elif hasattr(extra, 'title'): extra.title = cached_json.get('title', '')
                                cached_json['extras'] = fresh_ret['extras']
                            if save_db: ModelAvMetadata.save_metadata(self.category, cached_json)

                    if cached_json.get('extras'):
                        for extra in cached_json['extras']:
                            if isinstance(extra, dict): extra['title'] = cached_json.get('title', '')
                            elif hasattr(extra, 'title'): extra.title = cached_json.get('title', '')

                    return cached_json

        data = None
        try:
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

        original_calculated_title = ret.get("title", "")
        safe_studio = ret.get("studio", "Unknown")
        type_char = code[2] if len(code) > 2 else 'S'
        content_type = 'movie' if type_char == 'M' else 'scene'

        try:
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

            format_dict = {
                'originaltitle': ret.get("originaltitle", ""),
                'plot': ret.get("plot", ""),
                'title': original_calculated_title,
                'studio': safe_studio,
                'year': year_val,
                'actor': actor_str,
                'tagline': ret.get("tagline", "") 
            }
            
            use_movie_format = P.ModelSetting.get_bool(f"{self.name}_use_movie_title_format")
            if content_type == 'movie' and use_movie_format:
                title_format = P.ModelSetting.get(f"{self.name}_movie_title_format") or "[{studio}] {title}"
            else:
                title_format = P.ModelSetting.get(f"{self.name}_title_format") or "[{studio}] {actor} - {title}"
            
            final_title = title_format.format(**format_dict)
            final_title = re.sub(r'\[([^\]]+)\]\s*-\s*', r'[\1] ', final_title)
            final_title = re.sub(r'\s*-\s*$', '', final_title)
            final_title = re.sub(r'^\s*-\s*', '', final_title)
            final_title = re.sub(r'(\s*-\s*){2,}', ' - ', final_title)
            final_title = re.sub(r'\s+', ' ', final_title).strip()
            
            ret["title"] = final_title
            clean_sort_title = re.sub(r'[\[\]\-_]', ' ', final_title)
            clean_sort_title = re.sub(r'\s+', ' ', clean_sort_title).strip()
            
            ret["sorttitle"] = clean_sort_title
            ret["originaltitle"] = original_calculated_title
            ret["tagline"] = final_title

            if ret.get('extras'):
                for extra in ret['extras']:
                    if isinstance(extra, dict) and extra.get('content_type') == 'trailer':
                        extra['title'] = final_title

        except Exception as e:
            logger.error(f"[{self.name}] 타이틀 포맷 오류: {e}")
            ret["title"] = original_calculated_title
            ret["originaltitle"] = original_calculated_title
            ret["sorttitle"] = original_calculated_title

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

        # DB 자동 저장
        save_only_trans = P.ModelSetting.get_bool(f"{self.name}_db_save_only_translated")
        should_save = save_db and ret
        
        if should_save and save_only_trans:
            if skip_trans:
                should_save = False
                logger.debug(f"[{self.name}] 미번역(skip_trans=True) 조회 모드이므로 DB 캐시 저장을 건너뜁니다: {code}")

        if should_save:
            from .model_metadata_db import ModelAvMetadata
            ModelAvMetadata.save_metadata(self.category, ret)

        return ret


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

            # 작업 완료 후 부풀어 오른 WAL 파일 즉시 청소
            ModelAvMetadata.checkpoint_wal()

        except Exception as e_main:
            logger.error(f"[{self.name}] 일괄 작업 치명적 오류: {e_main}")
            self.enrich_status['status'] = f'오류 발생: {e_main}'
        finally:
            self.enrich_status['is_running'] = False
