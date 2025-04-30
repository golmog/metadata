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
        # "jav_censored_use_sjva": "False",
        "jav_censored_order": "dmm, mgsdvd, javbus",
        #'jav_censored_plex_is_proxy_preview' : 'True',
        #'jav_censored_plex_landscape_to_art' : 'True',
        "jav_censored_actor_order": "avdbs, hentaku",
        # avdbs
        "jav_censored_avdbs_use_sjva": "False",
        "jav_censored_avdbs_use_proxy": "False",
        "jav_censored_avdbs_proxy_url": "",
        "jav_censored_avdbs_image_mode": "0",
        "jav_censored_avdbs_test_name": "",
        # hentaku
        "jav_censored_hentaku_use_sjva": "False",
        "jav_censored_hentaku_use_proxy": "False",
        "jav_censored_hentaku_proxy_url": "",
        "jav_censored_hentaku_image_mode": "0",
        "jav_censored_hentaku_test_name": "",
        # dmm
        "jav_censored_dmm_use_sjva": "False",
        "jav_censored_dmm_use_proxy": "False",
        "jav_censored_dmm_proxy_url": "",
        "jav_censored_dmm_image_mode": "0",
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
        "jav_censored_mgsdvd_image_mode": "0",
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
        "jav_censored_javbus_image_mode": "0",
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
                call = req.form["call"]
                ModelSetting.set(f"{self.name}_{call}_test_code", code)

                data = self.search2(code, call)
                if data is None:
                    return jsonify({"ret": "no_match", "log": f"no results for '{code}' by '{call}'"})
                return jsonify({"search": data, "info": self.info(data[0]["code"])})
            if sub == "actor_test":
                name = req.form["name"]
                call = req.form["call"]
                ModelSetting.set(f"{self.name}_{call}_test_name", name)

                entity_actor = {"originalname": name}
                self.process_actor2(entity_actor, call)
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
                ModelSetting.set(f"{self.name}_{call}_code", code)
                data = self.search2(code, call)
                if data:
                    info = self.info(data[0]["code"])
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

        # 설정된 사이트 순서대로 검색 결과 수집
        for idx, site in enumerate(site_list):
            if site == 'jav321': continue # jav321은 ama 모듈에서 처리
            if site not in self.site_map: continue

            logger.debug(f"Searching on site: {site} (priority: {idx})")
            data = self.search2(keyword, site, manual=manual)
            if data:
                for item in data:
                    item["score"] = item.get("score", 0) - idx
                    item["site_key"] = site # 사이트 정보 추가
                all_results.extend(data)

            # 수동 검색 시에는 모든 사이트 결과 필요
            if manual:
                continue
            # 자동 검색 시 break 조건은 제거 (점수 조정으로 대체)

        if not all_results:
            return []

        # 점수 기준으로 최종 정렬
        all_results = sorted(all_results, key=lambda k: k.get("score", 0), reverse=True)

        # --- 자동 매칭 시 동일 최상위 점수 항목 포스터 확인 로직 ---
        if not manual and all_results:
            top_score = all_results[0].get("score", 0)
            score_threshold = 95 # 동일 점수 비교 시작 임계값
            if top_score >= score_threshold:
                # 최상위 점수와 동일한 점수를 가진 후보들 필터링
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
                            # 상세 정보 조회 (info 메소드 사용)
                            info_data = self.info(code) # 상세 정보 가져오기

                            if info_data:
                                ps_url = info_data.get('thumb')
                                fanart_list = info_data.get('fanart', [])
                                pl_url = fanart_list[0] if fanart_list else None
                                # 필요시 사이트별 pl_url 추출 로직 추가

                                if ps_url and pl_url:
                                    site_settings = self.__site_settings(site_key)
                                    proxy_url = site_settings.get("proxy_url")

                                    # SiteUtil.has_hq_poster 호출
                                    poster_pos = SiteUtil.has_hq_poster(ps_url, pl_url, proxy_url=proxy_url)

                                    if poster_pos:
                                        logger.info(f"후보 {code}: 포스터 확인 성공 (위치: {poster_pos}). 이 항목 선택.")
                                        best_candidate = candidate
                                        break # 첫 성공 시 종료
                                    else:
                                        logger.debug(f"후보 {code}: 포스터 확인 실패 (유사 영역 없음).")
                                else:
                                    logger.debug(f"후보 {code}: 상세 정보에서 포스터 URL(ps/pl) 부족.")
                            else:
                                logger.debug(f"후보 {code}: 상세 정보 조회 실패.")

                        except Exception as e_info:
                            logger.error(f"후보 {code}: 상세 정보 조회 또는 포스터 확인 중 오류: {e_info}")
                            continue

                    # 루프 종료 후 처리
                    if best_candidate:
                        logger.debug(f"최종 선택 (포스터 확인됨): {best_candidate['code']}")
                        all_results.remove(best_candidate)
                        all_results.insert(0, best_candidate)
                    else:
                        logger.info("모든 후보 포스터 확인 실패 또는 해당 없음. 첫 번째 후보 유지.")

        # 최종 결과 반환
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
            return ret

        db_prefix = f"{self.db_prefix.get(site, self.name)}_{site}"

        # lib_metadata로부터 받은 데이터를 가공
        ret["plex_is_proxy_preview"] = True  # ModelSetting.get_bool('jav_censored_plex_is_proxy_preview')
        ret["plex_is_landscape_to_art"] = True  # ModelSetting.get_bool('jav_censored_plex_landscape_to_art')
        ret["plex_art_count"] = len(ret["fanart"])

        actors = ret["actor"] or []
        for item in actors:
            self.process_actor(item)

            try:
                name_ja, name_ko = item["originalname"], item["name"]
                if name_ja and name_ko:
                    name_trans = SiteUtil.trans(name_ja)
                    if name_trans != name_ko:
                        ret["plot"] = ret["plot"].replace(name_trans, name_ko)
                        ret["tagline"] = ret["tagline"].replace(name_trans, name_ko)
                        for extra in ret["extras"] or []:
                            extra["title"] = extra["title"].replace(name_trans, name_ko)
            except Exception:
                logger.exception("오역된 배우 이름이 들어간 항목 수정 중 예외:")

        ret["title"] = ModelSetting.get(f"{db_prefix}_title_format").format(
            originaltitle=ret["originaltitle"],
            plot=ret["plot"],
            title=ret["title"],
            sorttitle=ret["sorttitle"],
            runtime=ret["runtime"],
            country=ret["country"],
            premiered=ret["premiered"],
            year=ret["year"],
            actor=actors[0].get("name", "") if actors else "",
            tagline=ret["tagline"] or "",
        )

        if "tag" in ret:
            tag_option = ModelSetting.get(f"{db_prefix}_tag_option")
            if tag_option == "0":
                ret["tag"] = []
            elif tag_option == "1":
                ret["tag"] = [ret["originaltitle"].split("-")[0]]
            elif tag_option == "3":
                tmp = []
                for _ in ret.get("tag", []):
                    if _ != ret["originaltitle"].split("-")[0]:
                        tmp.append(_)
                ret["tag"] = tmp

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
        data = SiteClass.info(code, **sett)

        if data["ret"] != "success":
            return None

        ret = data["data"]
        trans_ok = (
            SystemModelSetting.get("trans_type") == "1" and SystemModelSetting.get("trans_google_api_key").strip() != ""
        ) or SystemModelSetting.get("trans_type") in ["3", "4"]
        if use_sjva and sett["image_mode"] == "3" and trans_ok:
            MetadataServerUtil.set_metadata_jav_censored(code, ret, ret["title"].lower())
        return ret

    def process_actor(self, entity_actor):
        actor_site_list = ModelSetting.get_list(f"{self.name}_actor_order", ",")
        # logger.debug("actor_site_list : %s", actor_site_list)
        for site in actor_site_list:
            if self.process_actor2(entity_actor, site):
                return
        if not entity_actor.get("name", None):
            entity_actor["name"] = entity_actor["originalname"]

    def process_actor2(self, entity_actor, site) -> bool:
        originalname = entity_actor["originalname"]

        SiteClass = self.site_map.get(site, None)
        if SiteClass is None:
            return False

        code = "A" + SiteClass.site_char + originalname

        db_prefix = f"{self.db_prefix.get(site, self.name)}_{site}"
        use_sjva = ModelSetting.get_bool(f"{db_prefix}_use_sjva")
        if use_sjva:
            data = MetadataServerUtil.get_metadata(code) or entity_actor
            name = data.get("name", None)
            thumb = data.get("thumb", "")
            if name and name != data["originalname"] and ".discordapp." in thumb:
                logger.info("서버로부터 가져온 배우 정보를 사용: %s %s", originalname, code)
                entity_actor["name"] = name
                entity_actor["name2"] = data["name2"]
                entity_actor["thumb"] = thumb
                entity_actor["site"] = data["site"]
                return True

        sett = self.__site_settings(site)
        SiteClass.get_actor_info(entity_actor, **sett)

        name = entity_actor.get("name", None)
        if not name:
            return False

        # 서버에 저장
        thumb = entity_actor.get("thumb", "")
        if use_sjva and sett["image_mode"] == "3" and name and ".discordapp." in thumb:
            MetadataServerUtil.set_metadata(code, entity_actor, originalname)
        return True

    def __site_settings(self, site: str):
        db_prefix = f"{self.db_prefix.get(site, self.name)}_{site}"
        proxy_url = None
        if ModelSetting.get_bool(f"{db_prefix}_use_proxy"):
            proxy_url = ModelSetting.get(f"{db_prefix}_proxy_url")
        return {
            "proxy_url": proxy_url,
            "image_mode": ModelSetting.get(f"{db_prefix}_image_mode"),
        }

    def __info_settings(self, site: str, code: str):
        db_prefix = f"{self.db_prefix.get(site, self.name)}_{site}"
        sett = self.__site_settings(site)
        sett["max_arts"] = ModelSetting.get_int(f"{db_prefix}_art_count")
        sett["use_extras"] = ModelSetting.get_bool(f"{db_prefix}_use_extras")

        ps_to_poster = False
        for tmp in ModelSetting.get_list(f"{db_prefix}_small_image_to_poster", ","):
            if tmp in code:
                ps_to_poster = True
                break
        sett["ps_to_poster"] = ps_to_poster

        crop_mode = None
        for tmp in ModelSetting.get(f"{db_prefix}_crop_mode").splitlines():
            tmp = list(map(str.strip, tmp.split(":")))
            if len(tmp) != 2:
                continue
            if tmp[0] in code and tmp[1] in ["r", "l", "c"]:
                crop_mode = tmp[1]
                break
        sett["crop_mode"] = crop_mode

        return sett
