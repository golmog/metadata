# third-party
from flask import jsonify, render_template

# sjva 공용
from framework import SystemModelSetting
from lib_metadata import (
    MetadataServerUtil,
    SiteAvdbs,
    SiteDmm,
    SiteHentaku,
    SiteJav321,
    SiteJavbus,
    SiteMgstageDvd,
    SiteUtil,
    UtilNfo,
)

from plugin import LogicModuleBase

# 패키지
from .plugin import P

# search 메소드 수정용
import functools

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting

#########################################################


class LogicJavCensored(LogicModuleBase):
    db_default = {
        "jav_censored_db_version": "1",
        "jav_censored_order": "dmm, mgsdvd, javbus",
        "jav_censored_actor_order": "avdbs, hentaku",

        # 통합 이미지 설정
        "jav_censored_image_mode": "0", # 0:원본, 1:SJVA Proxy, 2:Discord Redirect, 3:Discord Proxy, 4:이미지 서버
        "jav_censored_use_image_server": "False",
        "jav_censored_image_server_url": "",
        "jav_censored_image_server_local_path": "/app/data/images",

        # avdbs
        "jav_censored_avdbs_use_sjva": "False",
        "jav_censored_avdbs_use_proxy": "False",
        "jav_censored_avdbs_proxy_url": "",
        "jav_censored_avdbs_test_name": "",
        "jav_censored_avdbs_use_local_db": "False",
        "jav_censored_avdbs_local_db_path": "/app/data/db/jav_actors.db",
        "jav_actor_img_url_prefix": "",

        # hentaku
        "jav_censored_hentaku_use_sjva": "False",
        "jav_censored_hentaku_use_proxy": "False",
        "jav_censored_hentaku_proxy_url": "",
        "jav_censored_hentaku_test_name": "",

        # dmm
        "jav_censored_dmm_use_sjva": "False",
        "jav_censored_dmm_use_proxy": "False",
        "jav_censored_dmm_proxy_url": "",
        "jav_censored_dmm_small_image_to_poster": "",
        "jav_censored_dmm_crop_mode": "",
        "jav_censored_dmm_title_format": "[{title}] {tagline}",
        "jav_censored_dmm_art_count": "0",
        "jav_censored_dmm_tag_option": "0",
        "jav_censored_dmm_use_extras": "True",
        "jav_censored_dmm_test_code": "ssni-900",

        # mgsdvd
        "jav_censored_mgsdvd_use_sjva": "False",
        "jav_censored_mgsdvd_use_proxy": "False",
        "jav_censored_mgsdvd_proxy_url": "",
        "jav_censored_mgsdvd_small_image_to_poster": "",
        "jav_censored_mgsdvd_crop_mode": "",
        "jav_censored_mgsdvd_title_format": "[{title}] {tagline}",
        "jav_censored_mgsdvd_art_count": "0",
        "jav_censored_mgsdvd_tag_option": "2",
        "jav_censored_mgsdvd_use_extras": "True",
        "jav_censored_mgsdvd_test_code": "abf-010",

        # javbus
        "jav_censored_javbus_use_sjva": "False",
        "jav_censored_javbus_use_proxy": "False",
        "jav_censored_javbus_proxy_url": "",
        "jav_censored_javbus_small_image_to_poster": "",
        "jav_censored_javbus_crop_mode": "",
        "jav_censored_javbus_title_format": "[{title}] {tagline}",
        "jav_censored_javbus_art_count": "0",
        "jav_censored_javbus_tag_option": "2",
        "jav_censored_javbus_use_extras": "True",
        "jav_censored_javbus_test_code": "abw-354",
    }

    site_map = {
        "avdbs": SiteAvdbs,
        "dmm": SiteDmm,
        "hentaku": SiteHentaku,
        "jav321": SiteJav321,
        "javbus": SiteJavbus,
        "mgsdvd": SiteMgstageDvd,
    }

    db_prefix = {
        "jav321": "jav_censored_ama",
    }

    def __init__(self, PM):
        super().__init__(PM, "setting")
        self.name = "jav_censored"

    def process_menu(self, sub, req):
        arg = ModelSetting.to_dict()
        arg["sub"] = self.name
        try:
            return render_template(f"{package_name}_{self.name}_{sub}.html", arg=arg)
        except Exception:
            logger.exception("메뉴 처리 중 예외:")
            return render_template("sample.html", title=f"{package_name} - {sub}")

    def process_ajax(self, sub, req):
        try:
            if sub == "test":
                code = req.form["code"]
                call = req.form["call"] # 'dmm', 'mgsdvd', 'javbus' 등
                db_prefix = f"{self.db_prefix.get(call, self.name)}_{call}"
                ModelSetting.set(f"{db_prefix}_test_code", code)

                # 메타데이터 검색 (search2 사용)
                data = self.search2(code, call, manual=True)
                if data is None or not data: # 검색 결과 없는 경우 처리
                    return jsonify({"ret": "no_match", "log": f"no results for '{code}' by '{call}'"})

                # 첫 번째 검색 결과의 코드로 상세 정보 조회 (info 사용)
                info_data = self.info(data[0]["code"])

                return jsonify({"search": data, "info": info_data if info_data else {}})

            if sub == "actor_test":
                name = req.form["name"]
                call = req.form["call"] # 'avdbs' 또는 'hentaku'
                db_prefix = f"{self.db_prefix.get(call, self.name)}_{call}"
                ModelSetting.set(f"{db_prefix}_test_name", name)

                entity_actor = {"originalname": name}
                sett = self.__site_settings(call)

                if call == 'avdbs':
                    sett['use_local_db'] = ModelSetting.get_bool('jav_censored_avdbs_use_local_db')
                    sett['local_db_path'] = ModelSetting.get('jav_censored_avdbs_local_db_path')
                    sett['db_image_base_url'] = ModelSetting.get('jav_actor_img_url_prefix')
                    sett['site_name_for_db_query'] = call

                # Hentaku에 대한 특별한 설정이 있다면 여기에 추가
                # elif call == 'hentaku':
                #    pass

                SiteClass = self.site_map.get(call)
                if SiteClass:
                    logger.debug(f"Actor Test: Calling {SiteClass.__name__}.get_actor_info with sett: {sett}")
                    SiteClass.get_actor_info(entity_actor, **sett)
                else:
                    logger.error(f"Actor Test: Cannot find SiteClass for call='{call}'")
                    entity_actor['error'] = f"Site class for '{call}' not found."
                return jsonify(entity_actor)

            if sub == "rcache_clear":
                SiteUtil.session.cache.clear()
                return jsonify({"ret": "success"})

            raise NotImplementedError(f"알려지지 않은 sub={sub}")
        except Exception as e:
            logger.exception("AJAX 요청 처리 중 예외:")
            return jsonify({"ret": "exception", "log": str(e)})

    def process_api(self, sub, req):
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
        return None

    def process_normal(self, sub, req):
        if sub == "nfo_download":
            code = req.args.get("code")
            call = req.args.get("call")
            if call in ["dmm", "mgsdvd"]:
                db_prefix = f"{self.db_prefix.get(call, self.name)}_{call}"
                ModelSetting.set(f"{db_prefix}_test_code", code)
                data = self.search2(code, call)
                if data:
                    info = self.info(data[0]["code"])
                    if info:
                        return UtilNfo.make_nfo_movie(
                            info,
                            output="file",
                            filename=info["originaltitle"].upper() + ".nfo",
                        )
        return None

    #########################################################

    def search2(self, keyword, site, manual=False):
        SiteClass = self.site_map.get(site, None)
        if SiteClass is None:
            return None
        sett = self.__site_settings(site)
        try:
            data = SiteClass.search(keyword, do_trans=manual, manual=manual, **sett)
            if data["ret"] == "success" and len(data["data"]) > 0:
                return data["data"]
            logger.debug(f"No results from {site} for '{keyword}'. Response: {data.get('ret')}")
        except Exception as e_site_search:
            logger.error(f"Error during search on site '{site}' for keyword '{keyword}': {e_site_search}")
        return None


    def search(self, keyword, manual=False):
        logger.debug(f"======= jav censored search START - keyword:[{keyword}] manual:[{manual}] =======")
        all_results = []
        site_list = ModelSetting.get_list(f"{self.name}_order", ",")

        # 1단계: 각 사이트 검색 및 기본 정보 수집
        for idx, site in enumerate(site_list):
            if site not in self.site_map:
                logger.warning(f"Site '{site}' not in site_map. Skipping.")
                continue

            logger.debug(f"--- Iteration {idx+1}: Searching on site: {site} ---")
            data_from_search2 = self.search2(keyword, site, manual=manual) # search2 호출 결과

            if data_from_search2:
                logger.debug(f"  Got {len(data_from_search2)} result(s) from {site}")
                for item in data_from_search2: # data_from_search2는 리스트여야 함
                    if not isinstance(item, dict): # 혹시 모를 타입 체크
                        logger.error(f"  Item from {site} is not a dict: {item}")
                        continue
                    item['original_score'] = item.get("score", 0)
                    item['site_key'] = site
                    item['content_type'] = item.get('content_type')
                    item['hq_poster_passed'] = False
                    all_results.append(item)
            else:
                logger.debug(f"  No results from {site}")

            if manual:
                logger.debug(f"  Manual search mode: After site '{site}', current all_results count: {len(all_results)}")

        logger.debug(f"--- All sites searched. Total initial results: {len(all_results)} ---")

        if not all_results:
            logger.debug("======= jav censored search END - No results found. =======")
            return []

        # 2단계: has_hq_poster 검증 및 hq_poster_score_adj 설정 (자동 매칭 시)
        if not manual and all_results:
            logger.debug("--- Starting HQ Poster check (manual=False) ---")
            all_results_sorted_for_hq = sorted(all_results, key=lambda k: k.get("original_score", 0), reverse=True)
            if all_results_sorted_for_hq: # 빈 리스트가 아닐 경우에만 진행
                top_original_score = all_results_sorted_for_hq[0].get("original_score", 0)
                score_threshold = 95
                
                if top_original_score >= score_threshold:
                    candidates_for_hq_check = [item for item in all_results_sorted_for_hq if item.get("original_score", 0) >= score_threshold]
                    logger.info(f"자동 매칭: {len(candidates_for_hq_check)}개 후보에 대해 포스터 확인 시도 (기준 original_score: {score_threshold}).")

                    for candidate_ref in candidates_for_hq_check: # candidate_ref는 정렬된 리스트의 아이템 참조
                        # all_results에서 실제 아이템 찾기 (code와 site_key로 유니크하게 식별)
                        original_item = next((x for x in all_results if x.get('code') == candidate_ref.get('code') and x.get('site_key') == candidate_ref.get('site_key')), None)
                        if not original_item: continue

                        code = original_item.get("code"); site_key = original_item.get("site_key")
                        if not code or not site_key: continue
                        try:
                            info_data = self.info(code) # 상세 정보 조회
                            if info_data:
                                ps_url = None; pl_url = None
                                for thumb_item in info_data.get('thumb', []):
                                    if thumb_item.get('aspect') == 'poster': ps_url = thumb_item.get('value')
                                    if thumb_item.get('aspect') == 'landscape': pl_url = thumb_item.get('value')
                                    if ps_url and pl_url: break
                                
                                if ps_url and pl_url:
                                    site_settings = self.__site_settings(site_key)
                                    proxy_url = site_settings.get("proxy_url")
                                    poster_pos = SiteUtil.has_hq_poster(ps_url, pl_url, proxy_url=proxy_url)
                                    if poster_pos:
                                        logger.info(f"후보 {code}: 포스터 확인 성공. 점수 조정 없음.")
                                        original_item['hq_poster_score_adj'] = 0 # 성공 시 0 (변동 없음)
                                    else:
                                        logger.debug(f"후보 {code}: 포스터 확인 실패. -1점 페널티.")
                                        original_item['hq_poster_score_adj'] = -1 # 실패 시 -1점
                                else:
                                    logger.debug(f"후보 {code}: 상세 정보에서 이미지 URL 부족. -1점 페널티.")
                                    original_item['hq_poster_score_adj'] = -1
                            else:
                                logger.debug(f"후보 {code}: 상세 정보 조회 실패. -1점 페널티.")
                                original_item['hq_poster_score_adj'] = -1
                        except Exception as e_info:
                            logger.error(f"후보 {code}: 상세 정보 조회 또는 포스터 확인 중 오류: {e_info}")
                            original_item['hq_poster_score_adj'] = -2 # 오류 시 더 큰 페널티
            logger.debug("--- HQ Poster check END ---")

        # 3단계: 1차 조정된 점수 계산
        logger.debug("--- Starting Adjusted Score calculation ---")
        for item in all_results:
            item['adjusted_score'] = item.get('original_score', 0) + item.get('hq_poster_score_adj', 0)
        logger.debug("--- Adjusted Score calculation END ---")

        # 4단계: 사용자 정의 우선순위에 따른 정렬
        logger.debug("--- Starting Custom Priority Sort ---")
        priority_order_map = {
            ('mgsdvd', None): 0,
            ('dmm', 'videoa'): 1,
            ('dmm', 'dvd'): 2,
            ('dmm', 'bluray'): 3,
            ('dmm', 'unknown'): 3,
            ('javbus', None): 4,
            ('jav321', None): 5
        }

        def get_custom_sort_key(item):
            site_key = item.get('site_key')
            content_type = item.get('content_type')
            # 3단계에서 계산된 adjusted_score 사용
            current_adjusted_score = item.get("adjusted_score", 0)
            
            site_type_priority = float('inf')
            type_specific_key = (site_key, content_type)
            site_only_key = (site_key, None)

            if type_specific_key in priority_order_map: site_type_priority = priority_order_map[type_specific_key]
            elif site_only_key in priority_order_map: site_type_priority = priority_order_map[site_only_key]
            
            # 정렬 기준: 1. 조정된 점수(내림차순) 2. 사용자정의 우선순위(오름차순)
            return (-current_adjusted_score, site_type_priority)

        sorted_results_step4 = sorted(all_results, key=get_custom_sort_key) # 변수명 변경
        logger.debug("--- Custom Priority Sort END ---")

        # 5단계: 최종 순위 기반 점수 할당 (-1점씩 차감, 100점 이내)
        logger.debug("--- Starting Final Score Assignment ---")
        final_sorted_results = [] # 새 리스트에 최종 결과 저장
        if sorted_results_step4:
            start_score = min(100, sorted_results_step4[0].get('adjusted_score', 0))
            for i, item_to_score in enumerate(sorted_results_step4):
                new_item = item_to_score.copy() # 복사본에 최종 점수 할당
                new_item['score'] = max(0, start_score - i)
                final_sorted_results.append(new_item)
        
        logger.debug("최종 정렬 및 점수 할당 후 상위 결과 (logic_jav_censored):")
        for i, item_log in enumerate(final_sorted_results[:5]):
            logger.debug(f"  {i+1}. Final Score={item_log.get('score')}, AdjustedScore={item_log.get('adjusted_score')}, Site={item_log.get('site_key')}, Type={item_log.get('content_type')}, OrigScore={item_log.get('original_score')}, Code={item_log.get('code')}")
        logger.debug("--- Final Score Assignment END ---")
        # --- 5단계 완료 ---

        logger.debug(f"======= jav censored search END - Returning {len(final_sorted_results)} results. =======")
        return final_sorted_results


    def info(self, code):
        if code[1] == "B":
            site = "javbus"
        elif code[1] == "D":
            site = "dmm"
        elif code[1] == "T":
            site = "jav321"
        elif code[1] == "M":
            site = "mgsdvd"
        else:
            logger.error("처리할 수 없는 코드: code=%s", code)
            return None

        ret = self.info2(code, site)
        if ret is None:
            logger.debug(f"info2 returned None for code: {code}")
            return ret

        db_prefix = f"{self.db_prefix.get(site, self.name)}_{site}"

        ret["plex_is_proxy_preview"] = True
        ret["plex_is_landscape_to_art"] = True
        ret["plex_art_count"] = len(ret.get("fanart", []))

        actors = ret.get("actor") or []
        actor_names_for_log = []
        if actors:
            for item in actors:
                self.process_actor(item)
                actor_names_for_log.append(item.get("name", item.get("originalname", "?")))

        for item in actors:
            self.process_actor(item)

            try:
                name_ja, name_ko = item.get("originalname"), item.get("name")
                if name_ja and name_ko:
                    name_trans = SiteUtil.trans(name_ja)
                    if name_trans != name_ko:
                        if ret.get("plot"): ret["plot"] = ret["plot"].replace(name_trans, name_ko)
                        if ret.get("tagline"): ret["tagline"] = ret["tagline"].replace(name_trans, name_ko)
                        for extra in ret.get("extras") or []:
                            if extra.get("title"): extra["title"] = extra["title"].replace(name_trans, name_ko)
            except Exception:
                logger.exception("오역된 배우 이름이 들어간 항목 수정 중 예외:")

        # 타이틀 포맷 적용 전 원본 타이틀 임시 저장 (로그용)
        original_calculated_title = ret.get("title", "")

        try: # 타이틀 포맷팅
            title_format = ModelSetting.get(f"{db_prefix}_title_format")
            format_dict = {
                'originaltitle': ret.get("originaltitle", ""),
                'plot': ret.get("plot", ""),
                'title': original_calculated_title, # 포맷팅 전 제목 사용
                'sorttitle': ret.get("sorttitle", ""),
                'runtime': ret.get("runtime", ""),
                'country': ', '.join(ret.get("country", [])),
                'premiered': ret.get("premiered", ""),
                'year': ret.get("year", ""),
                # 배우 이름은 이미 처리된 리스트에서 첫 번째 사용
                'actor': actor_names_for_log[0] if actor_names_for_log else "",
                'tagline': ret.get("tagline", ""),
            }
            ret["title"] = title_format.format(**format_dict)

        except KeyError as e:
            logger.error(f"타이틀 포맷팅 오류: 키 '{e}' 없음. 포맷: '{title_format}', 데이터: {format_dict}")
            ret["title"] = original_calculated_title # 오류 시 포맷팅 전 제목으로 복구
        except Exception as e_fmt:
            logger.exception(f"타이틀 포맷팅 중 예외 발생: {e_fmt}")
            ret["title"] = original_calculated_title # 오류 시 포맷팅 전 제목으로 복구

        if "tag" in ret:
            tag_option = ModelSetting.get(f"{db_prefix}_tag_option")
            if tag_option == "0":
                ret["tag"] = []
            elif tag_option == "1":
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                if label: ret["tag"] = [label]
                else: ret["tag"] = []
            elif tag_option == "3":
                tmp = []
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                for _ in ret.get("tag", []):
                    if label is None or _ != label:
                        tmp.append(_)
                ret["tag"] = tmp

        # --- 최종 데이터 로깅 (텍스트 형식) ---
        try:
            logger.debug(f"++++++++++ Final Metadata for Agent (code: {code}) ++++++++++")
            log_order = [ # 주요 필드 순서 정의
                'title', 'tagline', 'plot',
                'premiered', 'year', 'runtime', 'country', 'studio', 'director',
                'genre', 'tag', 'actor', 'ratings', 'extras', 'thumb', 'fanart',
                'mpaa', 'plex_is_proxy_preview', 'plex_is_landscape_to_art', 'plex_art_count',
                'site', 'code', 'score', 'ui_code'
            ]
            logged_keys = set()

            for key in log_order:
                if key in ret:
                    value = ret[key]
                    logged_keys.add(key)
                    log_value = str(value) # 기본 문자열 변환

                    # <<< 리스트 타입 상세 출력 >>>
                    if isinstance(value, list):
                        if key == 'actor':
                            # 배우는 이름 리스트 사용 (이미 생성됨)
                            log_value = ', '.join(actor_names_for_log) if actor_names_for_log else '[]'
                        elif value and isinstance(value[0], dict): # 딕셔너리 리스트 (thumb, ratings, extras)
                            try: # 각 항목을 보기 좋게 문자열로 변환 시도
                                item_strs = [json.dumps(item, ensure_ascii=False) for item in value]
                                log_value = f"[{len(value)} items]:\n    - " + "\n    - ".join(item_strs)
                            except Exception: # 변환 실패 시 간단히 개수만 표시
                                log_value = f"[{len(value)} items]"
                        elif value: # 일반 리스트 (genre, tag, fanart 등)
                            log_value = ', '.join(map(str, value))
                        else: # 빈 리스트
                            log_value = '[]'
                    # <<< 리스트 상세 출력 끝 >>>
                    elif isinstance(value, str) and key == 'plot' and len(value) > 200: # 줄거리 길이 제한
                        log_value = value[:200] + "..."

                    logger.debug(f"  {key}: {log_value}")

            # 나머지 필드 출력
            remaining_keys = sorted(list(set(ret.keys()) - logged_keys))
            if remaining_keys:
                logger.debug("  --- (Other fields) ---")
                for key in remaining_keys:
                    if key in ['trailer', 'userrating']:
                        logger.debug(f"  {key}: {ret[key]}")
                    elif key not in logged_keys: # 이미 출력된 키 제외
                        logger.debug(f"  {key}: {ret[key]}")

            logger.debug(f"---------- End of Final Metadata for Agent (code: {code}) ----------")
        except Exception as log_e:
            logger.exception(f"Error logging final metadata for {code}: {log_e}")
        # --- 로깅 끝 ---

        return ret

    def info2(self, code, site):
        db_prefix = f"{self.db_prefix.get(site, self.name)}_{site}"
        use_sjva = ModelSetting.get_bool(f"{db_prefix}_use_sjva")
        if use_sjva:
            ret = MetadataServerUtil.get_metadata(code)
            if ret is not None:
                logger.debug("서버로부터 메타 정보 가져옴: %s", code)
                return ret

        SiteClass = self.site_map.get(site, None)
        if SiteClass is None:
            return None

        sett = self.__info_settings(site, code)
        sett['url_prefix_segment'] = 'jav/cen'
        data = SiteClass.info(code, **sett)

        if data["ret"] != "success":
            return None

        ret = data["data"]
        trans_ok = (
            SystemModelSetting.get("trans_type") == "1" and SystemModelSetting.get("trans_google_api_key").strip() != ""
        ) or SystemModelSetting.get("trans_type") in ["3", "4"]
        if use_sjva and sett.get("image_mode") == "3" and trans_ok:
            MetadataServerUtil.set_metadata_jav_censored(code, ret, ret.get("title", "").lower())
        return ret

    def process_actor(self, entity_actor):
        actor_site_list = ModelSetting.get_list(f"{self.name}_actor_order", ",")
        for site in actor_site_list:
            is_avdbs = site == 'avdbs'
            if self.process_actor2(entity_actor, site, is_avdbs=is_avdbs):
                return
        if not entity_actor.get("name", None):
            if entity_actor.get("originalname"):
                entity_actor["name"] = entity_actor.get("originalname")

    def process_actor2(self, entity_actor, site, is_avdbs=False) -> bool:
        originalname = entity_actor.get("originalname")
        if not originalname: return False

        SiteClass = self.site_map.get(site, None)
        if SiteClass is None:
            return False

        code = "A" + SiteClass.site_char + originalname
        db_prefix = f"{self.db_prefix.get(site, self.name)}_{site}"
        use_sjva = ModelSetting.get_bool(f"{db_prefix}_use_sjva")
        if use_sjva:
            data = MetadataServerUtil.get_metadata(code)
            if data:
                name = data.get("name", None)
                thumb = data.get("thumb", "")
                if name and name != data.get("originalname") and ".discordapp." in thumb:
                    logger.info("서버로부터 가져온 배우 정보를 사용: %s %s", originalname, code)
                    entity_actor["name"] = name
                    entity_actor["name2"] = data.get("name2")
                    entity_actor["thumb"] = thumb
                    entity_actor["site"] = data.get("site")
                    return True

        sett = self.__site_settings(site)
        if is_avdbs:
            sett['use_local_db'] = ModelSetting.get_bool('jav_censored_avdbs_use_local_db')
            sett['local_db_path'] = ModelSetting.get('jav_censored_avdbs_local_db_path')
            sett['db_image_base_url'] = ModelSetting.get('jav_actor_img_url_prefix')

        SiteClass.get_actor_info(entity_actor, **sett)

        name = entity_actor.get("name", None)
        if not name:
            return False

        thumb = entity_actor.get("thumb", "")
        if use_sjva and sett.get("image_mode") == "3" and name and ".discordapp." in thumb:
            MetadataServerUtil.set_metadata(code, entity_actor, originalname)
        return True

    def __site_settings(self, site: str):
        db_prefix = f"{self.db_prefix.get(site, self.name)}_{site}"
        proxy_url = None
        if ModelSetting.get_bool(f"{db_prefix}_use_proxy"):
            proxy_url = ModelSetting.get(f"{db_prefix}_proxy_url")
        return {
            "proxy_url": proxy_url,
            "image_mode": ModelSetting.get("jav_censored_image_mode"),
            "use_image_server": ModelSetting.get_bool("jav_censored_use_image_server"),
            "image_server_url": ModelSetting.get("jav_censored_image_server_url"),
            "image_server_local_path": ModelSetting.get("jav_censored_image_server_local_path"),
        }

    def __info_settings(self, site: str, code: str):
        db_prefix = f"{self.db_prefix.get(site, self.name)}_{site}"
        sett = self.__site_settings(site)
        sett["max_arts"] = ModelSetting.get_int(f"{db_prefix}_art_count")
        sett["use_extras"] = ModelSetting.get_bool(f"{db_prefix}_use_extras")

        ps_to_poster = False
        for tmp in ModelSetting.get_list(f"{db_prefix}_small_image_to_poster", ","):
            if tmp and tmp in code:
                ps_to_poster = True
                break
        sett["ps_to_poster"] = ps_to_poster

        crop_mode = None
        for tmp in ModelSetting.get(f"{db_prefix}_crop_mode").splitlines():
            if not tmp.strip(): continue
            tmp = list(map(str.strip, tmp.split(":", 1)))
            if len(tmp) != 2:
                continue
            if tmp[0] and tmp[0] in code and tmp[1] in ["r", "l", "c"]:
                crop_mode = tmp[1]
                break
        sett["crop_mode"] = crop_mode

        return sett
