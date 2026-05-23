# -*- coding: utf-8 -*-
import os
import re
import traceback

from flask import jsonify, send_file
from io import BytesIO
from urllib.parse import urlparse, parse_qs

from .setup import *
from support_site.site_av.site_tpdb import SiteTpdb
from support_site.site_av.site_av_base import SiteAvBase
from support_site import UtilNfo

class ModuleWestern(PluginModuleBase):
    
    def __init__(self, P):
        super(ModuleWestern, self).__init__(P, name='western', first_menu='setting')
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
        }

    def plugin_load(self):
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
        # 1. 1차 기본 정제 (대괄호 스튜디오 제거 및 사용자 정규식 적용)
        cleaned_keyword = self._clean_search_keyword(keyword)

        logger.info(f"======= Western search START - keyword:[{cleaned_keyword}] (Original:[{keyword}]) manual:[{manual}] =======")
        all_results = []
        
        SiteClass = self.site_map.get("tpdb")
        if not SiteClass:
            return all_results
            
        try:
            # 1차 검색 시도
            data = SiteClass.search(cleaned_keyword, manual=manual)
            
            # --- [폴백 로직] 1차 검색 실패 시 날짜 패턴 제거 후 2차 검색 시도 ---
            if not data or data.get("ret") != "success" or not data.get("data"):
                logger.debug(f"[{self.name}] 1st search failed. Attempting Fallback (Date Removal) for: '{cleaned_keyword}'")
                
                # 날짜 패턴 (YY-MM-DD, YYYY-MM-DD, DD-MM-YY, DD-MM-YYYY 및 구분자 . _ 공백 모두 허용)
                date_pattern = r'[ ._\-]*(?:\d{2}|\d{4})[ ._\-]\d{2}[ ._\-](?:\d{2}|\d{4})[ ._\-]*'
                # 에피소드 패턴 (EP1, E01, EP01 등, 대소문자 구분 없이 허용)
                ep_pattern = r'[ ._\-]*(?:[e][p]?\d+)[ ._\-]*'
                
                fallback_keyword = re.sub(date_pattern, ' ', cleaned_keyword)
                fallback_keyword = re.sub(ep_pattern, ' ', fallback_keyword, flags=re.IGNORECASE)
                fallback_keyword = re.sub(r'\s+', ' ', fallback_keyword).strip()
                
                if fallback_keyword and fallback_keyword != cleaned_keyword:
                    logger.info(f"[{self.name}] Fallback search triggered - keyword:[{fallback_keyword}]")
                    data = SiteClass.search(fallback_keyword, manual=manual)
            
            if data and data.get("ret") == "success" and data.get("data"):
                results = data["data"]
                
                for item in results:
                    item['site_key'] = "tpdb"
                    
                    studio_str = ""
                    match_studio = re.match(r'^\[(.*?)\]', item.get('title', ''))
                    if match_studio:
                        studio_str = match_studio.group(1).lower()
                    
                    # Clip4Sale 페널티
                    if 'clip4sale' in studio_str:
                        logger.debug(f"[{self.name}] Clip4Sale detected in studio: '{studio_str}'. Applying penalty.")
                        original_score = item.get('score', 0)
                        item['score'] = max(0, original_score - 5)
                        
                    all_results.append(item)
                    
        except Exception as e:
            logger.error(f"[{self.name}] Error during search for keyword '{cleaned_keyword}': {e}")
            logger.error(traceback.format_exc())
                
        if all_results:
            # 점수순 정렬
            all_results = sorted(all_results, key=lambda k: k.get("score", 0), reverse=True)

        logger.debug(f"======= Western search END - Returning {len(all_results)} results. =======")
        return all_results


    def search2(self, keyword, site, manual=False):
        if site == "tpdb":
            return self.search(keyword, manual=manual)
        return None


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
                import requests
                from flask import send_file
                from io import BytesIO
                
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

        return None


    def info(self, code, keyword=None, fp_meta_mode=False):
        if code[0] != 'W':
            logger.error(f"[{self.name}] 처리할 수 없는 코드: {code}")
            return None
            
        site = "tpdb"
        SiteClass = self.site_map.get(site)

        logger.debug(f"[{self.name}] Info 조회 시작: Code='{code}', Keyword='{keyword}'")
        
        data = None
        try:
            data = SiteClass.info(code, fp_meta_mode=fp_meta_mode)
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
                if isinstance(a, dict):
                    name = str(a.get('name') or a.get('originalname') or "")
                elif hasattr(a, 'name'):
                    name = str(a.name or a.originalname or "")
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
                title_format = P.ModelSetting.get(f"{self.name}_movie_title_format")
                if not title_format: 
                    title_format = "[{studio}] {title}"
            else:
                title_format = P.ModelSetting.get(f"{self.name}_title_format")
                if not title_format:
                    title_format = "[{studio}] {actor} - {title}"
            
            final_title = title_format.format(**format_dict)
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
            logger.error(traceback.format_exc())
            ret["title"] = original_calculated_title
            ret["originaltitle"] = original_calculated_title
            ret["sorttitle"] = original_calculated_title

        tag_option = P.ModelSetting.get(f"{self.name}_tag_option")
        ret["tag"] = [] # TPDB에서 받아온 기본 태그 초기화

        if tag_option != "not_using":
            # site_tpdb.py가 original 딕셔너리에 넣어둔 원본 데이터 추출
            safe_studio = ret.get("original", {}).get("studio", "")
            safe_network = ret.get("original", {}).get("network", "")

            # Studio 옵션이 포함된 경우
            if tag_option in ["studio", "studio_network"]:
                if safe_studio and safe_studio != 'Unknown' and safe_studio not in ret["tag"]:
                    ret["tag"].append(safe_studio)
            
            # Network 옵션이 포함된 경우
            if tag_option in ["network", "studio_network"]:
                if safe_network and safe_network != 'Unknown' and safe_network not in ret["tag"]:
                    ret["tag"].append(safe_network)

        logger.info(f"[{self.name}] Info Success: {code} -> {ret['title']} ({ret.get('year', '')})")

        return ret
