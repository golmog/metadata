from urllib.parse import urlparse
from flask import send_from_directory
from support_site import (
    Site1PondoTv,
    Site10Musume,
    SiteCarib,
    SiteHeyzo,
    SiteAvdbs,
    SiteUtil,
    SiteFc2ppvdb
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
                "regex": r"1pon-(?P<code>\d{6}_\d{2,3})",
            },
            "10musume": {
                "instance": Site10Musume,
                "keyword": ["10mu"],
                "regex": r"10mu-(?P<code>\d{6}_\d{2})",
            },
            "heyzo": {
                "instance": SiteHeyzo,
                "keyword": ["heyzo"],
                "regex": r"(?P<code>heyzo-\d{4})",
            },
            "carib": {
                "instance": SiteCarib,
                "keyword": ["carib", "caribbeancom"],
                "regex": r"(carib|caribbeancom)-(?P<code>\d{6}-\d{3})",
            },
            "fc2": {
                "instance": SiteFc2ppvdb,
                "keyword": ["fc2", "fc2-ppv"],
                "regex": r"(fc2|fc2-ppv)-(?P<code>\d{6,7})",
            },
        }

        self.db_default = {
            f"{self.name}_db_version": "1",
            f"{self.name}_image_server_save_format": "/jav/uncen/{label}",

            f'{self.name}_1pondo_use_proxy' : 'False',
            f'{self.name}_1pondo_proxy_url' : '',
            f'{self.name}_1pondo_test_code' : '092121_001',
            f'{self.name}_10musume_use_proxy' : 'False',
            f'{self.name}_10musume_proxy_url' : '',
            f'{self.name}_10musume_test_code' : '010620_01',
            f'{self.name}_heyzo_use_proxy' : 'False',
            f'{self.name}_heyzo_proxy_url' : '',
            f'{self.name}_heyzo_test_code' : 'HEYZO-2681',
            f'{self.name}_carib_use_proxy' : 'False',
            f'{self.name}_carib_proxy_url' : '',
            f'{self.name}_carib_test_code' : '062015-904',
            f'{self.name}_fc2_use_proxy' : 'False',
            f'{self.name}_fc2_proxy_url' : '',
            f'{self.name}_fc2_test_code' : '3669846',
        }


    ################################################
    # region PluginModuleBase 메서드 오버라이드

    def plugin_load(self):
        self.__set_site_setting()

    def plugin_load_celery(self):
        self.__set_site_setting()

    # 사이트 설정값이 바뀌면 config
    def setting_save_after(self, change_list):
        ins_list = []

        # 공통 설정(jav_censored_)이 변경된 경우, 모든 Uncensored 사이트도 다시 로드
        common_settings = [
            'jav_censored_image_mode', 'jav_censored_trans_option', 
            'jav_censored_use_extras', 'jav_censored_art_count',
            'jav_censored_title_format' 
        ]
        if any(setting in change_list for setting in common_settings):
            ins_list = [v['instance'] for v in self.site_map.values()]
        else:
            # 기존 로직을 유지하되, Uncensored 관련 설정만 처리하도록 명확화
            for key in change_list:
                if key.endswith("_test_code"):
                    continue
                if key.startswith(self.name): # jav_uncensored_ 로 시작하는 키만 확인
                    for site, site_info in self.site_map.items():
                        if site in key:
                            instance = site_info['instance']
                            if instance not in ins_list:
                                ins_list.append(instance)

        if ins_list:
            self.__set_site_setting(ins_list)


    def __set_site_setting(self, ins_list=None):
        if ins_list is None:
            ins_list = self.site_map.values()
        for ins in ins_list:
            try:
                P.logger.debug(f"set_config site {ins['instance'].__name__} with settings.")
                ins['instance'].set_config(P.ModelSetting)
            except Exception as e:
                P.logger.error(f"Error initializing site {ins}: {str(e)}")
                #P.logger.error(traceback.format_exc())


    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            ret = {'ret': 'success'}
            if command == "test":
                code = arg2
                call = arg1 # '1pondo', '10musume', 'heyzo', 'carib', 'fc2'
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_code", code)

                # 1. 해당 사이트의 인스턴스를 가져온다.
                site_instance = self.site_map.get(call, {}).get('instance')
                if not site_instance:
                    ret['ret'] = 'error'
                    ret['msg'] = f"Site '{call}' not found."
                    return jsonify(ret)

                # manual=True는 번역을 하지 않고, 프록시 이미지 URL을 생성하는 등의 역할을 한다.
                search_result_dict = site_instance.search(code, manual=True)

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


    # endregion PluginModuleBase 메서드 오버라이드
    ################################################     


    ################################################
    # region SEARCH

    def search(self, keyword, manual=False):
        logger.debug('uncensored search - keyword:[%s] manual:[%s]', keyword, manual)

        for site_name, site_info in self.site_map.items():
            if any(k in keyword.lower() for k in site_info['keyword']):
                instance = site_info['instance']
                match = re.search(site_info['regex'], keyword.lower())

                search_code = keyword
                if match:
                    try:
                        code = match.group('code')
                        if code: search_code = code
                    except IndexError: pass

                data = instance.search(search_code, manual=manual)

                if data and data.get('ret') == 'success' and data.get('data'):
                    return sorted(data['data'], key=lambda k: k.get('score', 0), reverse=True)

        return []


    def search2(self, keyword, site, manual=False):
        SiteClass = self.site_map.get(site, None)['instance']
        if SiteClass is None:
            return None

        try:
            data = SiteClass.search(keyword, do_trans=manual, manual=manual) 

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


    ################################################
    # region INFO

    def info(self, code):
        censored_module = P.get_module('jav_censored')
        ret = None

        target_instance = None
        for site_info in self.site_map.values():
            instance = site_info['instance']
            if instance.site_char == code[1]:
                target_instance = instance
                break

        if target_instance:
            res = target_instance.info(code)
            if res and res['ret'] == 'success':
                ret = res['data']
        else:
            logger.error(f"No site found for site_char '{code[1]}' in code '{code}'")
            return None # 해당 사이트 없음

        if ret is not None:
            if ret.get('actor'):
                for item in ret['actor']:
                    censored_module.process_actor(item)

            try:
                # 타이틀 포맷
                title_format = P.ModelSetting.get('jav_censored_title_format')
                ret['title'] = title_format.format(
                    originaltitle=ret.get('originaltitle', ''),
                    plot=ret.get('plot', ''),
                    title=ret.get('title', ''),
                    sorttitle=ret.get('sorttitle', ''),
                    country=', '.join(ret.get('country', [])),
                    premiered=ret.get('premiered', ''),
                    year=ret.get('year', ''),
                    actor=ret['actor'][0]['name'] if ret.get('actor') and ret['actor'][0].get('name') else '',
                    tagline=ret.get('tagline', '')
                )
            except Exception as e:
                logger.error(f"Title formatting error in uncensored info: {e}")

            # 부가 영상 사용 여부
            if not P.ModelSetting.get_bool('jav_censored_use_extras'):
                ret['extras'] = []

            return ret
        return None


    # endregion INFO
    ################################################
