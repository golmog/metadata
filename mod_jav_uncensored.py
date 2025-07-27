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
        
        always_all_set = [
            "jav_censored_use_extras",
            "jav_censored_art_count", 
            #"jav_censored_image_server_url",
            #"jav_censored_image_server_local_path",
            #"jav_censored_use_discord_proxy_server",
            #"jav_censored_discord_proxy_server_url"
        ]
        # 굳이???? 
        for tmp in always_all_set:
            if tmp in change_list:
                ins_list = list(self.site_map.values())
                break
            if ins_list:
                break
        
        if True:
            for key in change_list:
                if key.endswith("_test_code"):
                    continue
                for site, ins in self.site_map.items():
                    if site in key:
                        if ins not in ins_list:
                            ins_list.append(ins)
                            break
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
                call = arg1 # '1pondo', '10musume', 'heyzo', 'carib'
                db_prefix = f"{self.name}_{call}"
                P.ModelSetting.set(f"{db_prefix}_test_code", code)

                search_results = self.search2(code, call, manual=True)

                if not search_results:
                    ret['ret'] = "warning"
                    ret['msg'] = f"no results for '{code}'"
                    return jsonify(ret)

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


    # endregion PluginModuleBase 메서드 오버라이드
    ################################################     


    ################################################
    # region SEARCH

    def search(self, keyword, manual=False):
        logger.debug('uncensored search - keyword:[%s] manual:[%s]', keyword, manual)
        do_trans = manual
        
        for site in self.site_map.values():
            if any(k in keyword.lower() for k in site['keyword']):
                instance = site['instance']
                match = re.search(site['regex'], keyword.lower())
                data = None
                if match:
                    code = match.group('code')
                    if code:
                        data = instance.search(code, do_trans=do_trans, manual=manual)
                if data == None:
                    data = instance.search(keyword.lower(), do_trans=do_trans, manual=manual)
                if data['ret'] == 'success' and len(data['data']) > 0:
                    ret = data['data']
                    ret = sorted(ret, key=lambda k: k['score'], reverse=True)
                    return ret
        return

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
        for site in self.site_map.values():
            if site['instance'].site_char == code[1]:
                instance = site['instance']
                ret = instance.info(code)
                if ret['ret'] == 'success':
                    ret = ret['data']
                break

        if ret != None:
            if ret.get('actor') is not None:
                for item in ret['actor']:
                    # self.get_actor_from_server(item) # actor 정보, avdbs 차단 때문에 직접 메타서버로 요청
                    censored_module.process_actor(item)

            ret['title'] = P.ModelSetting.get('jav_censored_title_format').format(
                originaltitle=ret.get('originaltitle', ''),
                plot=ret.get('plot', ''),
                title=ret.get('title', ''),
                sorttitle=ret.get('sorttitle', ''),
                country=ret.get('country', ''),
                premiered=ret.get('premiered', ''),
                year=ret.get('year', ''),
                actor=ret['actor'][0]['name'] if ret.get('actor') is not None and len(ret['actor']) > 0 else '',
                tagline=ret.get('tagline', '')
            )

            if P.ModelSetting.get_bool('jav_censored_use_extras') == False:
                ret['extras'] = []

            return ret

    # endregion INFO
    ################################################
