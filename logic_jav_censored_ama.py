# third-party
from flask import jsonify, render_template

# sjva 공용
from framework import SystemModelSetting
from lib_metadata import MetadataServerUtil, SiteDmm, SiteJav321, SiteJavbus, SiteMgstageAma, SiteUtil

from plugin import LogicModuleBase

# 패키지
from .plugin import P

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting

#########################################################


class LogicJavCensoredAma(LogicModuleBase):
    db_default = {
        "jav_censored_ama_db_version": "1",
        "jav_censored_ama_order": "mgsama, jav321",
        "jav_censored_ama_default_title_format": "[{title}] {tagline}",
        "jav_censored_ama_default_tag_option": "2",
        "jav_censored_ama_default_use_extras": "True",

        # mgsama
        "jav_censored_ama_mgsama_use_sjva": "False",
        "jav_censored_ama_mgsama_use_proxy": "False",
        "jav_censored_ama_mgsama_proxy_url": "",
        "jav_censored_ama_mgsama_small_image_to_poster": "",
        "jav_censored_ama_mgsama_crop_mode": "",
        "jav_censored_ama_mgsama_title_format": "[{title}] {tagline}",
        "jav_censored_ama_mgsama_art_count": "0",
        "jav_censored_ama_mgsama_tag_option": "0",
        "jav_censored_ama_mgsama_use_extras": "True",
        "jav_censored_ama_mgsama_test_code": "siro-5107",

        # jav321
        "jav_censored_ama_jav321_use_sjva": "False",
        "jav_censored_ama_jav321_use_proxy": "False",
        "jav_censored_ama_jav321_proxy_url": "",
        "jav_censored_ama_jav321_small_image_to_poster": "",
        "jav_censored_ama_jav321_crop_mode": "",
        "jav_censored_ama_jav321_title_format": "[{title}] {tagline}",
        "jav_censored_ama_jav321_art_count": "0",
        "jav_censored_ama_jav321_tag_option": "0",
        "jav_censored_ama_jav321_use_extras": "True",
        "jav_censored_ama_jav321_test_code": "ara-464",
    }

    site_map = {
        "dmm": SiteDmm,
        "jav321": SiteJav321,
        "javbus": SiteJavbus,
        "mgsama": SiteMgstageAma,
    }

    db_prefix = {
        "dmm": "jav_censored",
        "javbus": "jav_censored",
    }

    def __init__(self, PM):
        super().__init__(PM, "setting")
        self.name = "jav_censored_ama"

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
                site_call_db_prefix = f"{self.name}_{call}"
                ModelSetting.set(f"{self.name}_{call}_test_code", code)

                data = self.search2(code, call, manual=True)
                if data is None or not data:
                    return jsonify({"ret": "no_match", "log": f"no results for '{code}' by '{call}'"})

                info_data = self.info(data[0]["code"])
                return jsonify({"search": data, "info": info_data if info_data else {}})
        except Exception as e:
            logger.exception(f"({self.name}) AJAX 요청 처리 중 예외: sub={sub}")
            return jsonify({"ret": "exception", "log": str(e)})

    def process_api(self, sub, req):
        if sub == "search":
            keyword = req.args.get("keyword")
            manual = req.args.get("manual") == "True"
            return jsonify(self.search(keyword, manual=manual))
        if sub == "info":
            return jsonify(self.info(req.args.get("code")))
        return None

    #########################################################

    def search2(self, keyword, site, manual=False):
        SiteClass = self.site_map.get(site, None)
        if SiteClass is None: return None

        sett = self.__site_settings(site)
        data = SiteClass.search(keyword, do_trans=manual, manual=manual, **sett)
        if data["ret"] == "success" and len(data["data"]) > 0:
            return data["data"]
        return None

    def search(self, keyword, manual=False):
        ret = []
        site_list = ModelSetting.get_list(f"{self.name}_order", ",")
        for idx, site in enumerate(site_list):
            data = self.search2(keyword, site, manual=manual)
            if data is not None:
                for item in data:
                    item["score"] -= idx
                ret += data
                ret = sorted(ret, key=lambda k: k["score"], reverse=True)
            if manual:
                continue
            if len(ret) > 0 and ret[0]["score"] > 95:
                break
        return ret

    def info(self, code):
        if code[1] == "T": site = "jav321"
        elif code[1] == "D": site = "dmm"
        elif code[1] == "B": site = "javbus"
        elif code[1] == "M": site = "mgsama"
        else:
            logger.error(f"({self.name}) 처리할 수 없는 코드: code={code}")
            return None

        ret = self.info2(code, site)
        if ret is None: return ret

        current_plugin_db_prefix = self.db_prefix.get(site, self.name)
        site_specific_db_prefix = f"{current_plugin_db_prefix}_{site}"

        ret["plex_is_proxy_preview"] = True
        ret["plex_is_landscape_to_art"] = True
        if isinstance(ret.get("fanart"), list): # fanart가 리스트인지 확인
            ret["plex_art_count"] = len(ret.get("fanart", []))
        else: ret["plex_art_count"] = 0


        actors = ret.get("actor") or []
        if actors:
            jav_censored_logic = self.P.logic.get_module("jav_censored")
            if jav_censored_logic:
                for item in actors:
                    jav_censored_logic.process_actor(item) # 전역 배우 처리 로직 호출
                    try:
                        name_ja, name_ko = item.get("originalname"), item.get("name")
                        if name_ja and name_ko:
                            name_trans = SiteUtil.trans(name_ja)
                            if name_trans != name_ko and ret.get("plot"):
                                ret["plot"] = ret["plot"].replace(name_trans, name_ko)
                            if name_trans != name_ko and ret.get("tagline"):
                                ret["tagline"] = ret["tagline"].replace(name_trans, name_ko)
                            if ret.get("extras"):
                                for extra in ret.get("extras", []):
                                    if extra.get("title") and name_trans != name_ko :
                                        extra["title"] = extra["title"].replace(name_trans, name_ko)
                    except Exception: logger.exception("오역된 배우 이름이 들어간 항목 수정 중 예외:")
            else: logger.warning("jav_censored 모듈을 찾을 수 없어 배우 처리를 건너뜁니다.")

        # 타이틀 포맷 (사이트별 설정 우선, 없으면 Ama 기본 설정 사용)
        title_format_key = f"{site_specific_db_prefix}_title_format"
        title_format = ModelSetting.get(title_format_key)
        if not title_format: # 사이트별 설정이 없으면 Ama 기본 포맷 사용
            title_format = ModelSetting.get(f"{self.name}_default_title_format")

        # .format() 실패 방지 위해 키 존재 확인 또는 기본값 사용
        format_dict = {
            'originaltitle': ret.get("originaltitle", ""), 'plot': ret.get("plot", ""),
            'title': ret.get("title", ""), 'sorttitle': ret.get("sorttitle", ""),
            'runtime': ret.get("runtime", ""), 'country': ret.get("country", []), # country는 리스트일 수 있음
            'premiered': ret.get("premiered", ""), 'year': ret.get("year", ""),
            'actor': actors[0].get("name", "") if actors else "", # actors 빈 리스트일 경우 대비
            'tagline': ret.get("tagline", "")
        }
        try:
            # country가 리스트일 경우 join 처리
            format_dict['country'] = ', '.join(format_dict['country']) if isinstance(format_dict['country'], list) else format_dict['country']
            ret["title"] = title_format.format(**format_dict)
        except KeyError as e: logger.error(f"({self.name}) 타이틀 포맷팅 오류: 키 {e} 없음. 포맷: '{title_format}'")
        except Exception as e_fmt: logger.exception(f"({self.name}) 타이틀 포맷팅 중 예외: {e_fmt}")


        # 태그 옵션 (사이트별 설정 우선, 없으면 Ama 기본 설정 사용)
        if "tag" in ret: # tag 키가 존재할 때만 처리
            tag_option_key = f"{site_specific_db_prefix}_tag_option"
            tag_option = ModelSetting.get(tag_option_key)
            if tag_option is None or tag_option == "": # 사이트별 설정 없으면 Ama 기본 옵션
                tag_option = ModelSetting.get(f"{self.name}_default_tag_option")

            if tag_option == "0": ret["tag"] = []
            elif tag_option == "1":
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                ret["tag"] = [label] if label else []
            elif tag_option == "3":
                label = ret.get("originaltitle", "").split("-")[0] if ret.get("originaltitle") else None
                ret["tag"] = [t for t in ret.get("tag", []) if label is None or t != label]
        return ret

    def info2(self, code, site):
        current_plugin_db_prefix = self.db_prefix.get(site, self.name)
        site_specific_db_prefix = f"{current_plugin_db_prefix}_{site}"

        use_sjva = ModelSetting.get_bool(f"{site_specific_db_prefix}_use_sjva")
        if use_sjva:
            ret = MetadataServerUtil.get_metadata(code)
            if ret is not None:
                logger.debug("서버로부터 메타 정보 가져옴: %s", code)
                return ret

        SiteClass = self.site_map.get(site, None)
        if SiteClass is None: return None

        sett = self.__info_settings(site, code)
        # 전역 이미지 설정
        sett['image_mode'] = ModelSetting.get("jav_censored_image_mode") # 전역 image_mode
        sett['use_image_server'] = ModelSetting.get_bool("jav_censored_use_image_server")
        sett['image_server_url'] = ModelSetting.get("jav_censored_image_server_url")
        sett['image_server_local_path'] = ModelSetting.get("jav_censored_image_server_local_path")
        sett['url_prefix_segment'] = 'jav/ama'

        logger.debug(f"({self.name}) Calling {SiteClass.__name__}.info for code '{code}' with settings: {sett}")
        data = SiteClass.info(code, **sett)

        if data["ret"] != "success": return None
        ret = data["data"]

        # SJVA 서버 저장 조건 (sett의 image_mode는 이제 전역 image_mode)
        trans_ok = (SystemModelSetting.get("trans_type") == "1" and SystemModelSetting.get("trans_google_api_key","").strip()) or SystemModelSetting.get("trans_type") in ["3", "4"]
        if use_sjva and sett.get("image_mode") == "3" and trans_ok: # image_mode '3'은 디스코드 프록시
            MetadataServerUtil.set_metadata_jav_censored(code, ret, ret.get("title", "").lower())
        return ret

    def __site_settings(self, site: str):
        current_plugin_db_prefix = self.db_prefix.get(site, self.name)
        site_specific_db_prefix = f"{current_plugin_db_prefix}_{site}"

        proxy_url = None
        if ModelSetting.get_bool(f"{site_specific_db_prefix}_use_proxy"):
            proxy_url = ModelSetting.get(f"{site_specific_db_prefix}_proxy_url")

        return {
            "proxy_url": proxy_url,
            "image_mode": ModelSetting.get("jav_censored_image_mode"),
            "use_image_server": ModelSetting.get_bool("jav_censored_use_image_server"),
            "image_server_url": ModelSetting.get("jav_censored_image_server_url"),
            "image_server_local_path": ModelSetting.get("jav_censored_image_server_local_path"),
            "url_prefix_segment": "jav/ama" # Ama용 경로 세그먼트
        }

    def __info_settings(self, site: str, code: str):
        current_plugin_db_prefix = self.db_prefix.get(site, self.name)
        site_specific_db_prefix = f"{current_plugin_db_prefix}_{site}"

        sett = self.__site_settings(site) # 프록시 및 ★전역 이미지 설정★ 이미 포함

        # 사이트별 상세 설정 추가
        sett["max_arts"] = ModelSetting.get_int(f"{site_specific_db_prefix}_art_count")
        
        # use_extras는 사이트별 또는 Ama 기본값 사용
        use_extras_key = f"{site_specific_db_prefix}_use_extras"
        use_extras_value = ModelSetting.get_bool(use_extras_key)
        sett["use_extras"] = ModelSetting.get_bool(use_extras_key)


        ps_to_poster = False
        for tmp in ModelSetting.get_list(f"{site_specific_db_prefix}_small_image_to_poster", ","):
            if tmp and tmp in code: ps_to_poster = True; break
        sett["ps_to_poster"] = ps_to_poster

        crop_mode = None
        for tmp_line in ModelSetting.get(f"{site_specific_db_prefix}_crop_mode").splitlines():
            if not tmp_line.strip(): continue
            tmp_parts = list(map(str.strip, tmp_line.split(":", 1)))
            if len(tmp_parts) != 2: continue
            if tmp_parts[0] and tmp_parts[0] in code and tmp_parts[1] in ["r", "l", "c"]:
                crop_mode = tmp_parts[1]; break
        sett["crop_mode"] = crop_mode

        return sett
