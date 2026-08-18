import os
import re
import shutil
import traceback

from urllib.parse import urlparse
from flask import send_from_directory, send_file, jsonify
from io import BytesIO

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

class ModuleJavUncensored(PluginModuleBase):

    def __init__(self, P):
        super(ModuleJavUncensored, self).__init__(P, name='jav_uncensored', first_menu='setting')
        self.category = 'UNCEN'
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

            f"{self.name}_selenium_url": "",
            f"{self.name}_selenium_driver_type": "chrome",
            f"{self.name}_image_server_save_format": "/jav/uncen/{label}",
            
            f"{self.name}_db_use": "False",
            f"{self.name}_db_save": "False",
            f"{self.name}_db_save_only_translated": "True",
            f"{self.name}_db_auto_enrich": "True",
            f"{self.name}_enrich_delay": "2.0",
            f"{self.name}_db_import_path": "",
            f"{self.name}_db_image_url_mapping": "",

            f'{self.name}_1pondo_use_proxy' : 'False',
            f'{self.name}_1pondo_proxy_url' : '',
            f'{self.name}_1pondo_test_code' : '092121_001',

            f'{self.name}_10musume_use_proxy' : 'False',
            f'{self.name}_10musume_proxy_url' : '',
            f'{self.name}_10musume_test_code' : '010620_01',

            f'{self.name}_paco_use_proxy' : 'False',
            f'{self.name}_paco_proxy_url' : '',
            f'{self.name}_paco_test_code' : '111825_100',

            f'{self.name}_heyzo_use_proxy' : 'False',
            f'{self.name}_heyzo_proxy_url' : '',
            f'{self.name}_heyzo_test_code' : '2681',

            f'{self.name}_carib_use_proxy' : 'False',
            f'{self.name}_carib_proxy_url' : '',
            f'{self.name}_carib_test_code' : '062015-904',

            f'{self.name}_fc2com_use_fc2_com': 'True',
            f'{self.name}_fc2com_use_proxy' : 'False',
            f'{self.name}_fc2com_proxy_url' : '',
            f'{self.name}_fc2com_test_code' : '3669846',
            
            f'{self.name}_fc2com_use_javten_web': 'True',
            f'{self.name}_fc2com_use_javten_proxy' : 'False',
            f'{self.name}_fc2com_javten_proxy_url': '',
        }

        self.enrich_status = {
            'is_running': False, 'status': '대기 중', 'total': 0,
            'current': 0, 'success': 0, 'fail': 0, 'current_code': '', 'stop_flag': False
        }

        try:
            self.keyword_cache = F.get_cache(f"{P.package_name}_{self.name}_keyword_cache")
        except Exception as e:
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
        ins_list = []

        # 공통 설정(jav_censored_)이 변경된 경우, 모든 Uncensored 사이트도 다시 로드
        if any(key.startswith('jav_censored_') for key in change_list):
            ins_list = [v['instance'] for v in self.site_map.values()]
        else:
            for key in change_list:
                if key.endswith("_test_code"):
                    continue
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
        # 1. 전체 설정 파일을 읽어옴
        jav_settings = censored_module.get_jav_settings()

        # 2. YAML에서 읽어온 전체 설정을 SiteAvBase에 설정
        SiteAvBase.set_yaml_settings(jav_settings)

        # SiteAvBase 클래스 자체에도 설정을 주입하여 직접 호출 시 config 누락 방지
        SiteAvBase.set_config(self.P.ModelSetting)

        for ins in ins_list:
            try:
                P.logger.debug(f"set_config site {ins.__name__} with settings.")
                # 읽어온 규칙을 set_config에 전달
                ins.set_config(P.ModelSetting)
            except Exception as e:
                P.logger.error(f"Error initializing site {ins.__name__}: {str(e)}")
                P.logger.error(traceback.format_exc())


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
                
            res = super(ModuleJavUncensored, self).process_ajax(sub, req)
            return res if res is not None else jsonify({'ret': 'error', 'msg': '기본 처리 결과가 없습니다.'})
        except Exception as e:
            logger.error(f"[{self.name}] Exception in process_ajax: {e}")
            return jsonify({'ret': 'error', 'msg': str(e)})


    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            ret = {'ret': 'success'}
            if command == "test":
                code = arg2
                call = arg1 # '1pondo', '10musume', 'heyzo', 'carib', 'fc2'
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_code", code)

                site_info = self.site_map.get(call)
                if not site_info:
                    ret['ret'] = 'error'
                    ret['msg'] = f"Site '{call}' not found."
                    return jsonify(ret)

                site_instance = site_info.get('instance')
                if not site_instance:
                    ret['ret'] = 'error'
                    ret['msg'] = f"Instance for '{call}' not found."
                    return jsonify(ret)

                search_code = code
                if site_info.get('keyword'):
                    prefix = site_info['keyword'][0]
                    if not any(k in code.lower() for k in site_info['keyword']):
                        search_code = f"{prefix}-{code}"

                search_result_dict = site_instance.search(search_code, manual=True)

                if not search_result_dict or search_result_dict['ret'] != 'success' or not search_result_dict.get('data'):
                    ret['ret'] = "warning"
                    ret['msg'] = f"no results for '{code}' from site '{call}'"
                    return jsonify(ret)

                search_results = search_result_dict['data']

                info_data = self.info(search_results[0]['code'])

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
                        return jsonify({'ret': 'error', 'msg': 'DB 업데이트 실패 (해당 품번 없음)'})
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
                    category_suffix = "UNCEN" if mode == 'current' else "ALL"
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
                
                target_instance = None
                for site_info in self.site_map.values():
                    instance = site_info['instance']
                    if instance.site_char == code[1]:
                        target_instance = instance
                        break
                if not target_instance:
                    return jsonify({'ret': 'error', 'msg': '지원하는 사이트 인스턴스를 찾을 수 없습니다.'})

                res = target_instance.info(code, fp_meta_mode=False, skip_trans=True)
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
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})


    def process_api(self, sub, req):
        try:
            call = req.args.get("call", "")
            if sub == "search" and call in ["plex", "kodi"]:
                keyword = req.args.get("keyword", "").rstrip("-").strip()
                manual = req.args.get("manual") == "True"

                search_result = self.search(keyword, manual=manual)
                return jsonify(search_result)

            if sub == "info":
                code = req.args.get("code")
                data = self.info(code)
                if call == "kodi" and data:
                    from support_site import SiteUtil
                    data = SiteUtil.info_to_kodi(data)
                return jsonify(data)

            if sub == "user_image_update":
                return self._api_user_image_update(req)

            return jsonify({'ret': 'failed', 'msg': f'Invalid sub command: {sub}'}), 400

        except Exception as e:
            logger.error(f"Exception in process_api (sub={sub}): {e}")
            logger.error(traceback.format_exc())

            return jsonify({'ret': 'exception', 'msg': str(e)}), 500


    def process_normal(self, sub, req):
        if sub == "nfo_download":
            keyword = req.args.get("code")
            call = req.args.get("call")
            if call in self.site_map:
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_code", keyword)

                search_results = self.search2(keyword, call)
                if search_results:
                    if not hasattr(self, 'keyword_cache'):
                        self.keyword_cache = {}
                    try:
                        self.keyword_cache.set(search_results[0]['code'], keyword)
                    except AttributeError:
                        self.keyword_cache[search_results[0]['code']] = keyword
                    
                    info = self.info(search_results[0]["code"])
                    if info:
                        return UtilNfo.make_nfo_movie(info, output="file", filename=info["originaltitle"].upper() + ".nfo")

        elif sub == "yaml_download":
            keyword = req.args.get("code")
            call = req.args.get("call")
            if call in self.site_map:
                search_results = self.search2(keyword, call)
                if search_results:
                    if not hasattr(self, 'keyword_cache'):
                        self.keyword_cache = {}
                    try:
                        self.keyword_cache.set(search_results[0]['code'], keyword)
                    except AttributeError:
                        self.keyword_cache[search_results[0]['code']] = keyword

                    info = self.info(search_results[0]["code"])
                    if info:
                        return UtilNfo.make_yaml_movie(info, output="file", filename=f"{info['originaltitle'].upper()}.yaml")

        elif sub == "image_download":
            try:
                keyword = req.args.get("code")
                call = req.args.get("call")
                image_type = req.args.get("type") # 'p' (poster/vertical) or 'pl' (landscape/original)
                
                if call in self.site_map:
                    db_prefix = f"{self.name}_{call}"
                    P.ModelSetting.set(f"{db_prefix}_test_code", keyword)
                    
                    search_results = self.search2(keyword, call)
                    
                    if not search_results:
                        return "Search failed", 404
                    
                    real_code = search_results[0]['code']

                    if not hasattr(self, 'keyword_cache'):
                        self.keyword_cache = {}
                    try: self.keyword_cache.set(real_code, keyword)
                    except AttributeError: self.keyword_cache[real_code] = keyword

                    info = self.info(real_code)
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
                            return f"Failed to download image from {target_url} (Status: {img_res.status_code})", 500
                    except Exception as e_req:
                        return f"Request error for {target_url}: {e_req}", 500

                    filename = f"{info['originaltitle'].lower()}_{image_type}.jpg"
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


    # endregion PluginModuleBase 메서드 오버라이드
    ################################################     


    ################################################
    # region SEARCH

    def search(self, keyword, manual=False):
        logger.info(f'======= jav uncensored search START - keyword:[{keyword}] manual:[{manual}] =======')
        all_results = []
        
        # 1. DB 선행 검색
        use_db = P.ModelSetting.get_bool(f"{self.name}_db_use")
        if use_db and not manual:
            try:
                from .model_metadata_db import ModelAvMetadata, av_db_session
                
                parsed_ui = SiteAvBase._parse_ui_code_uncensored(keyword)
                norm_kw = re.sub(r'[^a-zA-Z0-9]', '', parsed_ui or keyword).lower()
                
                # 숫자 파트 기반 1차 쿼리
                match_num = re.search(r'\d+', keyword)
                search_query = f"%{match_num.group()}%" if match_num else f"%{keyword}%"

                db_records = av_db_session.query(ModelAvMetadata).filter(
                    ModelAvMetadata.category == 'UNCEN',
                    ModelAvMetadata.originaltitle.ilike(search_query)
                ).all()

                valid_db_records = []
                for record in db_records:
                    norm_orig = re.sub(r'[^a-zA-Z0-9]', '', record.originaltitle).lower()
                    norm_code = re.sub(r'[^a-zA-Z0-9]', '', record.code).lower()

                    if norm_kw == norm_orig or norm_kw == norm_code:
                        valid_db_records.append(record)
                    elif len(norm_orig) > len(norm_kw) and norm_orig.endswith(norm_kw):
                        prefix = norm_orig[:-len(norm_kw)]
                        if prefix in ['1pon', '10mu', 'paco', 'heyzo', 'carib', 'fc2']:
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
                    
                    jd = record.json_data or {}
                    actor_names = [a.get('name') if isinstance(a, dict) else str(a) for a in jd.get('actor', []) if a]
                    actor_str = ", ".join(actor_names[:3]) if actor_names else "배우 정보 없음"
                    premiered_str = jd.get('premiered', '') or (str(db_item.year) if db_item.year != 1900 else '미상')
                    plot_snippet = (jd.get('plot', '')[:120] + "...") if len(jd.get('plot', '')) > 120 else (jd.get('plot', '') or "줄거리 없음")
                    
                    db_item.desc = f"출처: {record.site.upper()} | 출시: {premiered_str} | 출연: {actor_str}\n{plot_snippet}"
                    db_item.score = 105
                    db_item.content_type = jd.get('content_type', 'unknown')

                    item_dict = db_item.as_dict()
                    item_dict['original_score'] = 105
                    item_dict['site_key'] = record.site
                    item_dict['is_db_cached'] = True
                    item_dict['is_priority_label_site'] = True 
                    all_results.append(item_dict)

                if all_results:
                    for item in all_results: item['score'] = min(100, item['score'])
                    logger.debug(f"[{self.name}] Auto-match satisfied by Local DB ({len(all_results)}건):")
                    for idx, item in enumerate(all_results):
                        year_str = item.get('year') if item.get('year') != 1900 else '????'
                        logger.debug(f"  📁 {idx+1}. [{item.get('site_key', '').upper()}] Code={item.get('code')}, UI={item.get('ui_code')}, Title='{item.get('title')}' ({year_str})")
                    return all_results
                
            except Exception as e_db:
                logger.error(f"[{self.name}] DB Search Error: {e_db}")

        # 2. 라이브 파싱 (Uncensored 구조: 일치하는 단일 타겟 사이트만 즉시 호출)
        live_results = []
        for site_name, site_info in self.site_map.items():
            if any(k in keyword.lower() for k in site_info['keyword']) or re.search(site_info['regex'], keyword.lower()):
                instance = site_info['instance']
                search_code = keyword
                data = instance.search(search_code, manual=manual)

                if data and data.get('ret') == 'success' and data.get('data'):
                    live_results.extend(data['data'])
                    break

        if live_results:
            all_results.extend(live_results)

        # 3. 우회 꼬리표 부착 (manual=True)
        if all_results:
            all_results = sorted(all_results, key=lambda k: k.get('score', 0), reverse=True)
            if manual:
                for item in all_results:
                    try: self.keyword_cache.set(f"BYPASS_{item['code']}", "1")
                    except AttributeError: self.keyword_cache[f"BYPASS_{item['code']}"] = "1"

        logger.info(f'======= jav uncensored search END - Returning {len(all_results)} results =======')
        return all_results


    def search2(self, keyword, site, manual=False):
        site_info = self.site_map.get(site)
        if not site_info:
            logger.warning(f"search2: Site '{site}' not found in site_map.")
            return None
        
        SiteClass = site_info.get('instance')
        if SiteClass is None:
            return None

        search_keyword = keyword
        if site_info.get('keyword'):
            prefix = site_info['keyword'][0] 
            if not any(k in keyword.lower() for k in site_info['keyword']):
                search_keyword = f"{prefix}-{keyword}"
                # logger.debug(f"search2: Modified keyword for test '{keyword}' -> '{search_keyword}'")

        try:
            data = SiteClass.search(search_keyword, manual=manual) 

            if data and data.get("ret") == "success" and data.get("data"):
                if isinstance(data["data"], list) and data["data"]:
                    return data["data"]
                elif not isinstance(data["data"], list):
                    logger.warning(f"search2: Site '{site}' returned data that is not a list: {type(data['data'])}")
            
        except Exception as e_site_search:
            logger.error(f"Error during search on site '{site}' for keyword '{keyword}': {e_site_search}")
        return None

    # endregion SEARCH
    ################################################

    def process_actor(self, entity_actor):
        censored_module = P.get_module('jav_censored')
        if censored_module:
            censored_module.process_actor(entity_actor)
        else:
            if not entity_actor.get("name") and entity_actor.get("originalname"):
                entity_actor["name"] = entity_actor.get("originalname")


    ################################################
    # region INFO

    def info(self, code, fp_meta_mode=False, skip_trans=False):
        bypass_cache = False
        try:
            if self.keyword_cache.get(f"BYPASS_{code}") == "1":
                bypass_cache = True
                self.keyword_cache.set(f"BYPASS_{code}", "0")
        except AttributeError:
            if self.keyword_cache.get(f"BYPASS_{code}") == "1":
                bypass_cache = True
                self.keyword_cache[f"BYPASS_{code}"] = "0"

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
                        logger.info(f"[{self.name}] DB 캐시에 한글 번역이 없어 캐시를 건너뛰고 새로 번역을 수행합니다: {code}")

                if not is_db_untranslated:
                    logger.info(f"[{self.name}] DB 캐시를 로드했습니다: {code}")
                    needs_enrichment = not cached_json.get('thumb')
                    
                    if needs_enrichment:
                        logger.info(f"[{self.name}] 이미지/트레일러 누락 감지. Enrichment를 수행합니다...")
                        fresh_data = target_instance.info(code, fp_meta_mode=False, skip_trans=True)
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

                    title_log = cached_json.get('title', 'No Title')
                    year_log = cached_json.get('year', '????')
                    site_log = cached_json.get('site', 'unknown').upper()
                    ui_code_log = cached_json.get('originaltitle') or cached_json.get('ui_code') or code
                    logger.info(f"[DB Cache Success] Code: {code} ({ui_code_log}), Site: {site_log}, Title: {title_log} ({year_log})")

                    return cached_json

        ret = None
        res = target_instance.info(code, fp_meta_mode=fp_meta_mode, skip_trans=skip_trans)
        if res and res['ret'] == 'success':
            ret = res['data']

        if ret is None: return None

        ret["plex_is_proxy_preview"] = True
        ret["plex_is_landscape_to_art"] = True
        ret["plex_art_count"] = len(ret.get("fanart", []))

        actor_names_for_log = []
        if not fp_meta_mode:
            if ret.get('actor'):
                for item in ret['actor']:
                    self.process_actor(item)
                    actor_names_for_log.append(item.get("name", item.get("originalname", "?")))

        original_calculated_title = ret.get("title", "")

        try: # 타이틀 포맷팅
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
                        if extra.get("content_type") == "trailer":
                            extra["title"] = final_title
                    elif hasattr(extra, 'content_type') and extra.content_type == "trailer":
                        if hasattr(extra, 'title'):
                            extra.title = final_title

        except Exception as e_fmt:
            logger.exception(f"타이틀 포맷팅 중 예외 발생: {e_fmt}")
            ret["title"] = original_calculated_title

        # 태그 옵션
        if "tag" in ret:
            tag_option = P.ModelSetting.get("jav_censored_tag_option")
            if tag_option == "not_using":
                ret["tag"] = []
            elif tag_option == "label":
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                if label: ret["tag"] = [label]
                else: ret["tag"] = []
            elif tag_option == "site":
                tmp = []
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                for _ in ret.get("tag", []):
                    if label is None or _ != label:
                        tmp.append(_)
                ret["tag"] = tmp

        # 부가 영상 사용 여부 (jav_censored 설정값 사용)
        if not P.ModelSetting.get_bool('jav_censored_use_extras'):
            ret['extras'] = []

        if ret:
            title_log = ret.get('title', 'No Title')
            year_log = ret.get('year', '????')
            logger.info(f"[{target_instance.site_name.upper()} Success] Code: {code}, Title: {title_log} ({year_log})")

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


    # endregion INFO
    ################################################


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
