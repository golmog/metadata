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
    SiteJavdb,
)

from plugin import LogicModuleBase
from urllib.parse import urlparse
from lib_metadata.discord import DiscordUtil

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
        "jav_censored_order": "mgsdvd, dmm, jav321, javdb, javbus",
        "jav_censored_actor_order": "avdbs, hentaku",
        "jav_censored_result_priority_order": "mgsdvd, dmm_videoa, dmm_dvd, dmm_bluray, dmm_unknown, jav321, javdb, javbus",

        # 통합 이미지 설정
        "jav_censored_image_mode": "0", # 0:원본, 1:SJVA Proxy, 2:Discord Redirect, 3:Discord Proxy, 4:이미지 서버
        "jav_censored_use_image_server": "False",
        "jav_censored_image_server_url": "",
        "jav_censored_image_server_local_path": "/app/data/images",

        # 디스코드 프록시 서버 관련 설정
        "jav_censored_use_discord_proxy_server": "False",
        "jav_censored_discord_proxy_server_url": "",

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

        # javdb
        "jav_censored_javdb_use_sjva": "False",
        "jav_censored_javdb_use_proxy": "False",
        "jav_censored_javdb_proxy_url": "",
        "jav_censored_javdb_small_image_to_poster": "",
        "jav_censored_javdb_crop_mode": "",
        "jav_censored_javdb_title_format": "[{title}] {tagline}",
        "jav_censored_javdb_art_count": "5",
        "jav_censored_javdb_tag_option": "1",
        "jav_censored_javdb_use_extras": "False",
        "jav_censored_javdb_test_code": "JUFE-487",
    }

    site_map = {
        "avdbs": SiteAvdbs,
        "dmm": SiteDmm,
        "hentaku": SiteHentaku,
        "jav321": SiteJav321,
        "javbus": SiteJavbus,
        "mgsdvd": SiteMgstageDvd,
        "javdb": SiteJavdb,
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

                current_site_settings = self.__site_settings(call)

                # 메타데이터 검색 (search2 사용)
                data = self.search2(code, call, manual=True, site_settings_override=current_site_settings)
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

    def search2(self, keyword, site, manual=False, site_settings_override=None):
        SiteClass = self.site_map.get(site, None)
        if SiteClass is None:
            return None
        sett = site_settings_override if site_settings_override is not None else self.__site_settings(site)
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
        site_list_from_setting = ModelSetting.get_list(f"{self.name}_order", ",")
        logger.debug(f"Site list from setting: {site_list_from_setting}")

        priority_sites_for_early_exit = {"dmm", "mgsdvd"} # 자동 검색 시 100점 매칭 시 조기 종료할 사이트
        early_exit_triggered = False # 조기 종료 플래그

        # 1단계: 각 사이트 검색
        for site_key_in_order in site_list_from_setting:
            if site_key_in_order not in self.site_map:
                logger.warning(f"Site '{site_key_in_order}' not in site_map. Skipping.")
                continue

            logger.debug(f"--- Searching on site: {site_key_in_order} ---")
            
            # UI 테스트(manual=True) 시에는 해당 사이트의 현재 설정을 가져와 search2에 전달
            # 자동 검색(manual=False) 시에는 search2 내부에서 __site_settings를 호출
            current_site_settings_for_test = self.__site_settings(site_key_in_order) if manual else None
            data_from_search2 = self.search2(keyword, site_key_in_order, manual=manual, site_settings_override=current_site_settings_for_test)

            if data_from_search2:
                logger.debug(f"  Got {len(data_from_search2)} result(s) from {site_key_in_order}")
                for item_result in data_from_search2:
                    if not isinstance(item_result, dict):
                        logger.error(f"  Item from {site_key_in_order} is not a dict: {item_result}")
                        continue
                    
                    # EntityAVSearch.as_dict()가 score, site_key, content_type 등을 반환한다고 가정
                    # site_key가 없다면 현재 루프의 site_key_in_order 사용
                    item_result['original_score'] = item_result.get("score", 0)
                    item_result['site_key'] = item_result.get("site_key", site_key_in_order)
                    # item_result['content_type']는 이미 item_result에 포함되어 있어야 함 (DMM의 경우)
                    item_result['hq_poster_passed'] = False 

                    all_results.append(item_result)

                    if not manual and item_result['site_key'] in priority_sites_for_early_exit and item_result['original_score'] == 100:
                        logger.info(f"Found 100-score match from priority site '{item_result['site_key']}' for '{keyword}'. Activating early exit.")
                        early_exit_triggered = True
                        break # 현재 사이트의 나머지 아이템 처리는 중단하고, 다음 단계로 (우선 사이트 종료)
            else:
                logger.debug(f"  No results from {site_key_in_order}")
            
            if early_exit_triggered: # 조기 종료 플래그가 켜졌으면 전체 사이트 검색 루프 종료
                logger.debug("  Early exit triggered. Stopping further site searches.")
                break 
        
        logger.debug(f"--- All site searches completed or exited early. Total initial results: {len(all_results)} ---")

        if not all_results:
            logger.debug("======= jav censored search END - No results found. =======")
            return []

        # 2단계: HQ 포스터 검증 (manual=False 일 때)
        if not manual and all_results:
            logger.debug("--- Starting HQ Poster check ---")
            all_results_sorted_for_hq_check = sorted(all_results, key=lambda k: k.get("original_score", 0), reverse=True)
            
            if all_results_sorted_for_hq_check: 
                top_original_score = all_results_sorted_for_hq_check[0].get("original_score", 0)
                score_threshold = 95
                
                if top_original_score >= score_threshold:
                    candidates_for_hq_check = [
                        item for item in all_results_sorted_for_hq_check 
                        if item.get("original_score", 0) >= score_threshold
                    ]
                    logger.info(f"HQ Check: {len(candidates_for_hq_check)} candidates (score >= {score_threshold}).")

                    for candidate_item_ref in candidates_for_hq_check:
                        item_in_all_results_to_update = next(
                            (
                                x for x in all_results 
                                if x.get('code') == candidate_item_ref.get('code') and 
                                   x.get('site_key') == candidate_item_ref.get('site_key')
                            ), 
                            None
                        )

                        if not item_in_all_results_to_update:
                            logger.warning(f"HQ Check: Could not find original item in all_results for candidate code {candidate_item_ref.get('code')}. Skipping HQ check for this item.")
                            continue
                        
                        code_for_hq_check = item_in_all_results_to_update.get("code")
                        site_key_for_hq_check = item_in_all_results_to_update.get("site_key")

                        if not code_for_hq_check or not site_key_for_hq_check:
                            logger.warning(f"HQ Check: Code or site_key missing for an item. Skipping HQ check.")
                            continue

                        # --- hq_poster_score_adj 초기화 및 기본 페널티 설정 ---
                        item_in_all_results_to_update['hq_poster_score_adj'] = -1

                        try:
                            info_data_for_hq_check = self.info2(code_for_hq_check, site_key_for_hq_check) 

                            if info_data_for_hq_check:
                                ps_url_hq, pl_url_hq = None, None
                                for thumb_item_hq in info_data_for_hq_check.get('thumb', []):
                                    if thumb_item_hq.get('aspect') == 'poster': ps_url_hq = thumb_item_hq.get('value')
                                    if thumb_item_hq.get('aspect') == 'landscape': pl_url_hq = thumb_item_hq.get('value')
                                    if ps_url_hq and pl_url_hq: break
                                
                                if ps_url_hq and pl_url_hq:
                                    settings_for_hq_proxy = self.__site_settings(site_key_for_hq_check)
                                    proxy_url_for_hq_util = settings_for_hq_proxy.get("proxy_url")
                                    
                                    poster_pos_result = SiteUtil.has_hq_poster(ps_url_hq, pl_url_hq, proxy_url=proxy_url_for_hq_util)
                                    if poster_pos_result:
                                        logger.info(f"HQ Check PASSED for {code_for_hq_check} on {site_key_for_hq_check}.")
                                        item_in_all_results_to_update['hq_poster_score_adj'] = 0
                                    else:
                                        logger.debug(f"HQ Check FAILED (has_hq_poster returned None) for {code_for_hq_check} on {site_key_for_hq_check}. Penalty: -1.")
                                else:
                                    logger.debug(f"HQ Check SKIPPED (ps_url or pl_url missing) for {code_for_hq_check} on {site_key_for_hq_check}. Penalty: -1.")
                            else:
                                logger.debug(f"HQ Check SKIPPED (info_data_for_hq is None) for {code_for_hq_check} on {site_key_for_hq_check}. Penalty: -1.")
                        except Exception as e_info_hq_check:
                            logger.error(f"HQ Check Exception for {code_for_hq_check} on {site_key_for_hq_check}: {e_info_hq_check}")
                            item_in_all_results_to_update['hq_poster_score_adj'] = -2 
            logger.debug("--- HQ Poster check END ---")

        # 3단계: 조정된 점수 계산
        logger.debug("--- Starting Adjusted Score calculation ---")
        for item_adj_score in all_results:
            item_adj_score['adjusted_score'] = item_adj_score.get('original_score', 0) + item_adj_score.get('hq_poster_score_adj', 0)
        logger.debug("--- Adjusted Score calculation END ---")

        # 4단계: 사용자 정의 우선순위에 따른 정렬
        logger.debug("--- Starting Custom Priority Sort ---")
        priority_string = ModelSetting.get('jav_censored_result_priority_order')
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
            adj_score = item_for_final_sort.get("adjusted_score", 0)
            prio_val = get_priority_value_for_sort(item_for_final_sort)
            return (-adj_score, prio_val)

        sorted_results_after_priority = sorted(all_results, key=get_custom_sort_key_for_final)
        logger.debug("--- Custom Priority Sort END ---")

        # 5단계: 최고 점수 동점자 페널티 및 최종 점수 할당
        logger.debug("--- Starting Tie-Breaking and Final Score Assignment ---")
        final_results_with_score_assigned = []
        if sorted_results_after_priority:
            top_adj_score_final = sorted_results_after_priority[0].get('adjusted_score', 0)
            current_penalty_level = 0
            last_priority_val_in_tie = -1
            logger.debug(f"Top adjusted score for tie-breaking: {top_adj_score_final}")
            for i, item_in_tie_break_loop in enumerate(sorted_results_after_priority):
                final_item = item_in_tie_break_loop.copy()
                current_adj_score_loop = final_item.get('adjusted_score', 0)
                calculated_final_score = current_adj_score_loop
                if current_adj_score_loop == top_adj_score_final:
                    current_prio_val_loop = get_priority_value_for_sort(final_item)
                    if current_prio_val_loop > last_priority_val_in_tie:
                        if i > 0: current_penalty_level += 1
                        last_priority_val_in_tie = current_prio_val_loop
                    calculated_final_score = top_adj_score_final - current_penalty_level
                final_item['score'] = max(0, calculated_final_score) # 최종 'score' 필드 업데이트
                final_results_with_score_assigned.append(final_item)
        logger.debug("--- Tie-Breaking and Final Score Assignment END ---")

        if final_results_with_score_assigned:
            logger.debug("Top results after final scoring:")
            for i, item_log_final_list in enumerate(final_results_with_score_assigned[:5]):
                logger.debug(f"  {i+1}. Final Score={item_log_final_list.get('score')}, AdjScore={item_log_final_list.get('adjusted_score')}, OrigScore={item_log_final_list.get('original_score')}, Site={item_log_final_list.get('site_key')}, Type={item_log_final_list.get('content_type')}, PrioValue={get_priority_value_for_sort(item_log_final_list)}, Code={item_log_final_list.get('code')}")
        
        logger.debug(f"======= jav censored search END - Returning {len(final_results_with_score_assigned)} results. =======")
        return final_results_with_score_assigned


    def info(self, code):
        if code[1] == "B":
            site = "javbus"
        elif code[1] == "D":
            site = "dmm"
        elif code[1] == "T":
            site = "jav321"
        elif code[1] == "M":
            site = "mgsdvd"
        elif code[1] == "J":
            site = "javdb"
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

        # === 디스코드 프록시 URL 치환 로직 ===
        try:
            current_image_mode = ModelSetting.get("jav_censored_image_mode")
            use_custom_proxy_server = ModelSetting.get_bool("jav_censored_use_discord_proxy_server")
            custom_proxy_url_base = ModelSetting.get("jav_censored_discord_proxy_server_url").strip().rstrip('/')

            if (current_image_mode == '3' or current_image_mode == '5') and \
                use_custom_proxy_server and custom_proxy_url_base:
                
                logger.debug(f"Applying custom Discord proxy server: {custom_proxy_url_base} for code {code}")
                
                data_to_modify = ret # entity.as_dict()의 결과가 담긴 dict
                if data_to_modify is None: # 방어 코드
                    logger.warning("data_to_modify is None before URL rewrite. Skipping rewrite.")
                    return ret # 또는 다른 적절한 처리

                # DiscordUtil.isurlattachment 함수를 가져오거나, 유사한 로직 사용
                # from lib_metadata.discord import DiscordUtil # 상단에 임포트 필요

                def rewrite_discord_url(url_string):
                    if isinstance(url_string, str) and DiscordUtil.isurlattachment(url_string):
                        try:
                            # URL 파싱하여 경로 및 쿼리 유지
                            parsed_url = urlparse(url_string)
                            new_url = f"{custom_proxy_url_base}{parsed_url.path}"
                            if parsed_url.query:
                                new_url += f"?{parsed_url.query}"
                            logger.debug(f"  Rewriting Discord URL: '{url_string}' -> '{new_url}'")
                            return new_url
                        except Exception as e_parse_rewrite:
                            logger.error(f"  Error parsing/rewriting URL '{url_string}': {e_parse_rewrite}")
                            return url_string
                    return url_string

                # entity.thumb 수정 (poster, landscape)
                if data_to_modify.get('thumb') and isinstance(data_to_modify['thumb'], list):
                    for thumb_item in data_to_modify['thumb']:
                        if isinstance(thumb_item, dict) and 'value' in thumb_item:
                            thumb_item['value'] = rewrite_discord_url(thumb_item['value'])
                        # EntityThumb에 thumb 필드가 있다면 그것도 처리 (지금은 value만)

                # entity.fanart 수정 (리스트 내 URL들)
                if data_to_modify.get('fanart') and isinstance(data_to_modify['fanart'], list):
                    data_to_modify['fanart'] = [rewrite_discord_url(fanart_url) for fanart_url in data_to_modify['fanart']]
                
                # entity.actor의 thumb 수정 (만약 배우 이미지도 디스코드 프록시를 사용한다면)
                if data_to_modify.get('actor') and isinstance(data_to_modify['actor'], list):
                    for actor_item in data_to_modify['actor']:
                        if isinstance(actor_item, dict) and 'thumb' in actor_item:
                            actor_item['thumb'] = rewrite_discord_url(actor_item['thumb'])
                
                # entity.extras의 thumb 수정 (트레일러 썸네일 등)
                if data_to_modify.get('extras') and isinstance(data_to_modify['extras'], list):
                    for extra_item in data_to_modify['extras']:
                        if isinstance(extra_item, dict) and 'thumb' in extra_item:
                            extra_item['thumb'] = rewrite_discord_url(extra_item['thumb'])

            else:
                if current_image_mode == '3' or current_image_mode == '5':
                    if not use_custom_proxy_server:
                        logger.debug(f"Custom Discord proxy server is NOT enabled for code {code}.")
                    if not custom_proxy_url_base:
                        logger.debug(f"Custom Discord proxy server URL is empty for code {code}.")

        except Exception as e_rewrite:
            logger.exception(f"Error during Discord URL rewrite for code {code}: {e_rewrite}")
        # === URL 치환 로직 끝 ===

        return ret

    def info2(self, code, site):
        SiteClass = self.site_map.get(site, None)
        if SiteClass is None:
            logger.warning(f"info2: site '{site}'에 해당하는 SiteClass를 찾을 수 없습니다.")
            return None

        sett = self.__info_settings(site, code)
        sett['url_prefix_segment'] = 'jav/cen'

        logger.debug(f"info2: 사이트 '{site}'에서 코드 '{code}' 정보 조회 시작...")
        data = None
        try:
            data = SiteClass.info(code, **sett)
        except Exception as e_info:
            logger.exception(f"info2: 사이트 '{site}'에서 코드 '{code}' 정보 조회 중 오류 발생: {e_info}")
            return None

        if data and data.get("ret") == "success" and data.get("data"):
            ret = data["data"]
            logger.info(f"info2: 사이트 '{site}'에서 코드 '{code}' 정보 조회 성공.")

            return ret
        else:
            response_ret = data.get('ret') if data else 'No response'
            has_data_field = bool(data.get('data')) if data and data.get('ret') == 'success' else False
            logger.warning(f"info2: 사이트 '{site}'에서 코드 '{code}' 정보 조회 실패. Response ret='{response_ret}', Has data field='{has_data_field}'")
            return None

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
        if not originalname: 
            logger.warning("process_actor2: originalname이 없어 배우 정보를 처리할 수 없습니다.")
            return False

        SiteClass = self.site_map.get(site, None)
        if SiteClass is None:
            logger.warning(f"process_actor2: site '{site}'에 해당하는 SiteClass를 찾을 수 없습니다.")
            return False

        logger.debug(f"process_actor2: '{originalname}' 정보를 사이트 '{site}'에서 조회 시작...")
        sett = self.__site_settings(site)
        if is_avdbs:
            sett['use_local_db'] = ModelSetting.get_bool('jav_censored_avdbs_use_local_db')
            sett['local_db_path'] = ModelSetting.get('jav_censored_avdbs_local_db_path')
            sett['db_image_base_url'] = ModelSetting.get('jav_actor_img_url_prefix')

        get_info_success = False
        try:
            get_info_success = SiteClass.get_actor_info(entity_actor, **sett)
        except Exception as e_getinfo:
            logger.exception(f"process_actor2: 사이트 '{site}'에서 배우 '{originalname}' 정보 조회 중 오류 발생: {e_getinfo}")
            get_info_success = False

        if get_info_success:
            updated_name = entity_actor.get("name", None)
            updated_thumb = entity_actor.get("thumb", None)
            logger.info(f"process_actor2: 사이트 '{site}'에서 '{originalname}' 정보 조회 성공. Name: {updated_name}, Thumb: {bool(updated_thumb)}")
            
            return True
        else:
            logger.info(f"process_actor2: 사이트 '{site}'에서 '{originalname}' 정보 조회 실패 또는 정보 없음.")
            return False

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
