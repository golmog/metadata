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
        "jav_censored_avdbs_local_db_path": "/app/data/db/avdbs.db",
        "jav_censored_avdbs_use_image_transform": "False",
        "jav_censored_avdbs_image_transform_source": "",
        "jav_censored_avdbs_image_transform_target": "",

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
                data = self.search2(code, call) # manual=False 가 기본값
                if data is None or not data: # 검색 결과 없는 경우 처리
                    return jsonify({"ret": "no_match", "log": f"no results for '{code}' by '{call}'"})

                # 첫 번째 검색 결과의 코드로 상세 정보 조회 (info 사용)
                # info 함수는 필요한 kwargs를 내부적으로 __info_settings 에서 가져옴
                info_data = self.info(data[0]["code"])

                # 결과를 modal에 맞게 가공 (기존 방식 유지 또는 필요시 수정)
                # search 결과와 info 결과를 함께 반환
                return jsonify({"search": data, "info": info_data if info_data else {}}) # info 실패 시 빈 dict 반환

            if sub == "actor_test":
                name = req.form["name"]
                call = req.form["call"] # 'avdbs' 또는 'hentaku'
                db_prefix = f"{self.db_prefix.get(call, self.name)}_{call}"
                ModelSetting.set(f"{db_prefix}_test_name", name)

                entity_actor = {"originalname": name}

                # --- get_actor_info 호출 위한 설정(sett) 구성 ---
                # site_settings()로 기본 설정(proxy, image_mode 등) 가져오기
                sett = self.__site_settings(call)
                # Avdbs 특정 설정 추가
                if call == 'avdbs':
                    sett['use_local_db'] = ModelSetting.get_bool('jav_censored_avdbs_use_local_db')
                    sett['local_db_path'] = ModelSetting.get('jav_censored_avdbs_local_db_path')
                    sett['use_image_transform'] = ModelSetting.get_bool('jav_censored_avdbs_use_image_transform')
                    sett['image_transform_source'] = ModelSetting.get('jav_censored_avdbs_image_transform_source')
                    sett['image_transform_target'] = ModelSetting.get('jav_censored_avdbs_image_transform_target')
                    # DB 이미지 기본 URL 설정 추가 (만약 설정값이 있다면)
                    # sett['db_image_base_url'] = ModelSetting.get('some_setting_for_base_url')
                    # 현재는 db_image_base_url 설정이 없으므로 전달 안 함
                # Hentaku 특정 설정 추가 (필요하다면)
                # elif call == 'hentaku':
                #    pass

                # lib_metadata의 SiteClass 가져오기
                SiteClass = self.site_map.get(call)
                if SiteClass:
                    logger.debug(f"Actor Test: Calling {SiteClass.__name__}.get_actor_info with sett: {sett}")
                    # 수정된 sett를 **kwargs로 전달하여 get_actor_info 호출
                    SiteClass.get_actor_info(entity_actor, **sett)
                else:
                    logger.error(f"Actor Test: Cannot find SiteClass for call='{call}'")
                    entity_actor['error'] = f"Site class for '{call}' not found."
                # --- 설정 구성 및 호출 끝 ---

                # 테스트 결과 반환
                return jsonify(entity_actor)
            if sub == "rcache_clear":
                # ... (기존 캐시 클리어 로직) ...
                pass # 변경 없음
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
        data = SiteClass.search(keyword, do_trans=manual, manual=manual, **sett)
        if data["ret"] == "success" and len(data["data"]) > 0:
            return data["data"]
        return None

    def search(self, keyword, manual=False):
        logger.debug(f"jav censored search - keyword:[{keyword}] manual:[{manual}]")
        all_results = []
        site_list = ModelSetting.get_list(f"{self.name}_order", ",")

        for idx, site in enumerate(site_list):
            if site not in self.site_map: continue

            logger.debug(f"Searching on site: {site} (priority: {idx})")
            data = self.search2(keyword, site, manual=manual)
            if data:
                for item in data:
                    item["score"] = item.get("score", 0) - idx
                    item["site_key"] = site
                all_results.extend(data)

            if manual:
                continue

        if not all_results:
            return []

        all_results = sorted(all_results, key=lambda k: k.get("score", 0), reverse=True)

        if not manual and all_results:
            top_score = all_results[0].get("score", 0)
            score_threshold = 95
            if top_score >= score_threshold:
                top_candidates = [item for item in all_results if item.get("score", 0) == top_score]

                if len(top_candidates) > 1:
                    logger.info(f"자동 매칭: 동일 최상위 점수({top_score}) 후보 {len(top_candidates)}개. 포스터 확인 시도...")
                    best_candidate = None

                    for candidate in top_candidates:
                        code = candidate.get("code")
                        site_key = candidate.get("site_key")
                        if not code or not site_key: continue

                        logger.debug(f"후보 {code} ({site_key}): 상세 정보 조회 및 포스터 확인 시도...")
                        try:
                            info_data = self.info(code)

                            if info_data:
                                ps_url = info_data.get('thumb')
                                fanart_list = info_data.get('fanart', [])
                                pl_url = fanart_list[0] if fanart_list else None

                                if ps_url and pl_url:
                                    site_settings = self.__site_settings(site_key)
                                    proxy_url = site_settings.get("proxy_url")
                                    poster_pos = SiteUtil.has_hq_poster(ps_url, pl_url, proxy_url=proxy_url)

                                    if poster_pos:
                                        logger.info(f"후보 {code}: 포스터 확인 성공 (위치: {poster_pos}). 이 항목 선택.")
                                        best_candidate = candidate
                                        break
                                    else:
                                        logger.debug(f"후보 {code}: 포스터 확인 실패 (유사 영역 없음).")
                                else:
                                    logger.debug(f"후보 {code}: 상세 정보에서 포스터 URL(ps/pl) 부족.")
                            else:
                                logger.debug(f"후보 {code}: 상세 정보 조회 실패.")

                        except Exception as e_info:
                            logger.error(f"후보 {code}: 상세 정보 조회 또는 포스터 확인 중 오류: {e_info}")
                            continue

                    if best_candidate:
                        logger.debug(f"최종 선택 (포스터 확인됨): {best_candidate['code']}")
                        all_results.remove(best_candidate)
                        all_results.insert(0, best_candidate)
                    else:
                        logger.info("모든 후보 포스터 확인 실패 또는 해당 없음. 첫 번째 후보 유지.")

        return all_results

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
        # 배우 이름 처리 (로그 및 타이틀 포맷팅 용)
        actor_names_for_log = []
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
            # 주요 필드 순서대로 출력
            log_order = [
                'title', 'originaltitle', 'sorttitle', 'tagline', 'plot',
                'premiered', 'year', 'runtime', 'country', 'studio',
                'genre', 'tag', 'actor', 'director', 'ratings', 'extras',
                'thumb', 'fanart', 'mpaa', 'plex_is_proxy_preview',
                'plex_is_landscape_to_art', 'plex_art_count', 'site', 'code', 'score', 'ui_code' # 기타 정보
            ]
            logged_keys = set()
            for key in log_order:
                if key in ret:
                    value = ret[key]
                    logged_keys.add(key)
                    # 값 유형에 따른 출력 형식 조정
                    if isinstance(value, list):
                        if key == 'actor': # 배우는 처리된 이름 리스트 사용
                            log_value = ', '.join(actor_names_for_log) if actor_names_for_log else '[]'
                        elif key in ['thumb', 'art', 'fanart', 'extras', 'ratings']: # 객체 리스트는 길이 또는 요약 정보
                            log_value = f"[{len(value)} items]"
                            # 상세 내용 필요 시 주석 해제
                            # log_value = "\n".join([f"  - {str(item)}" for item in value]) if value else '[]'
                        else: # 일반 리스트는 join
                            log_value = ', '.join(map(str, value)) if value else '[]'
                    elif isinstance(value, dict):
                        log_value = str(value) # 간단히 문자열로
                    elif isinstance(value, str) and len(value) > 200 and key == 'plot': # 줄거리는 길이 제한
                        log_value = value[:200] + "..."
                    else:
                        log_value = str(value)

                    logger.debug(f"  {key}: {log_value}")

            # log_order에 없는 나머지 키 출력
            remaining_keys = set(ret.keys()) - logged_keys
            if remaining_keys:
                logger.debug("  --- (Other fields) ---")
                for key in sorted(list(remaining_keys)):
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
            use_local_db_value = ModelSetting.get_bool('jav_censored_avdbs_use_local_db')
            logger.debug(f"##### Debug: ModelSetting.get_bool('jav_censored_avdbs_use_local_db') returned: {use_local_db_value} (Type: {type(use_local_db_value)})")
            sett['use_local_db'] = ModelSetting.get_bool('jav_censored_avdbs_use_local_db')
            sett['local_db_path'] = ModelSetting.get('jav_censored_avdbs_local_db_path')
            sett['use_image_transform'] = ModelSetting.get_bool('jav_censored_avdbs_use_image_transform')
            sett['image_transform_source'] = ModelSetting.get('jav_censored_avdbs_image_transform_source')
            sett['image_transform_target'] = ModelSetting.get('jav_censored_avdbs_image_transform_target')

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
