import traceback

from urllib.parse import urlparse
from flask import send_from_directory
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
from .setup import *
from support import d

class ModuleJavUncensored(PluginModuleBase):

    def __init__(self, P):
        super(ModuleJavUncensored, self).__init__(P, name='jav_uncensored', first_menu='setting')
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

            f'{self.name}_fc2com_use_proxy' : 'False',
            f'{self.name}_fc2com_proxy_url' : '',
            f'{self.name}_fc2com_test_code' : '3669846',
        }

        try:
            self.keyword_cache = F.get_cache(f"{P.package_name}_{self.name}_keyword_cache")
        except Exception as e:
            self.keyword_cache = {}


    ################################################
    # region PluginModuleBase 메서드 오버라이드

    def plugin_load(self):
        self._set_site_setting()

    def plugin_load_celery(self):
        self._set_site_setting()

    # 사이트 설정값이 바뀌면 config
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

        for ins in ins_list:
            try:
                P.logger.debug(f"set_config site {ins.__name__} with settings.")
                # 읽어온 규칙을 set_config에 전달
                ins.set_config(P.ModelSetting)
            except Exception as e:
                P.logger.error(f"Error initializing site {ins.__name__}: {str(e)}")
                P.logger.error(traceback.format_exc())


    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            ret = {'ret': 'success'}
            if command == "test":
                code = arg2
                call = arg1 # '1pondo', '10musume', 'heyzo', 'carib', 'fc2'
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_code", code)

                # 1. 해당 사이트의 정보를 가져온다.
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

                # 테스트 시 숫자만 입력된 경우 접두어 자동 추가 (UI 로그 개선용)
                search_code = code
                if site_info.get('keyword'):
                    prefix = site_info['keyword'][0]
                    # 입력값에 해당 사이트의 키워드가 포함되어 있지 않으면 접두어 추가
                    if not any(k in code.lower() for k in site_info['keyword']):
                        search_code = f"{prefix}-{code}"

                # manual=True는 번역을 하지 않고, 프록시 이미지 URL을 생성하는 등의 역할을 한다.
                search_result_dict = site_instance.search(search_code, manual=True)

                if not search_result_dict or search_result_dict['ret'] != 'success' or not search_result_dict.get('data'):
                    ret['ret'] = "warning"
                    ret['msg'] = f"no results for '{code}' from site '{call}'"
                    return jsonify(ret)

                search_results = search_result_dict['data']

                # 3. 새로운 시그니처에 맞게 info 함수를 호출한다.
                # info 함수는 이제 code만 인자로 받는다.
                info_data = self.info(search_results[0]['code'])

                ret['json'] = {
                    "search": search_results,
                    "info": info_data if info_data else {}
                }
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
        return None


    # endregion PluginModuleBase 메서드 오버라이드
    ################################################     


    ################################################
    # region SEARCH

    def search(self, keyword, manual=False):
        logger.info('======= jav uncensored search START - keyword:[%s] manual:[%s] =======', keyword, manual)

        for site_name, site_info in self.site_map.items():
            if any(k in keyword.lower() for k in site_info['keyword']):
                instance = site_info['instance']
                match = re.search(site_info['regex'], keyword.lower())

                search_code = keyword

                data = instance.search(search_code, manual=manual)

                if data and data.get('ret') == 'success' and data.get('data'):
                    return sorted(data['data'], key=lambda k: k.get('score', 0), reverse=True)

        logger.info(f'======= jav uncensored search END - NOT FOUND: {keyword} =======')
        return []


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
                logger.debug(f"search2: Modified keyword for test '{keyword}' -> '{search_keyword}'")

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

    def info(self, code, fp_meta_mode=False):
        censored_module = P.get_module('jav_censored')
        ret = None

        target_instance = None
        for site_info in self.site_map.values():
            instance = site_info['instance']
            if instance.site_char == code[1]:
                target_instance = instance
                break

        if target_instance:
            res = target_instance.info(code, fp_meta_mode=fp_meta_mode)
            if res and res['ret'] == 'success':
                ret = res['data']
        else:
            logger.error(f"No site found for site_char '{code[1]}' in code '{code}'")
            return None

        if ret is None:
            return None

        ret["plex_is_proxy_preview"] = True
        ret["plex_is_landscape_to_art"] = True
        ret["plex_art_count"] = len(ret.get("fanart", []))

        actor_names_for_log = []
        if not fp_meta_mode:
            if ret.get('actor'):
                for item in ret['actor']:
                    censored_module.process_actor(item)
                    actor_names_for_log.append(item.get("name", item.get("originalname", "?")))

        original_calculated_title = ret.get("title", "")
        try:
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
            ret["title"] = title_format.format(**format_dict)
        except KeyError as e:
            logger.error(f"타이틀 포맷팅 오류: 키 '{e}' 없음. 포맷: '{title_format}', 데이터: {format_dict}")
            ret["title"] = original_calculated_title
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

        return ret


    # endregion INFO
    ################################################
