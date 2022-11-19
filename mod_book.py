from support_site import SiteNaverBook, SiteUtil

from .setup import *


class ModuleBook(PluginModuleBase):
    
    def __init__(self, P):
        super(ModuleBook, self).__init__(P, name='book', first_menu='naver')
        self.db_default = {
            f'{self.name}_db_version' : '1',
            f'{self.name}_naver_titl' : '',
            f'{self.name}_naver_auth' : '',
            f'{self.name}_naver_cont' : '',
            f'{self.name}_naver_isbn' : '',
            f'{self.name}_naver_publ' : '',
            f'{self.name}_naver_code' : '',
        }

    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            ret = {}
            command = req.form['command']
            if command == 'search_naver':
                tmp = arg1.split('|')
                P.ModelSetting.set(f'{self.name}_naver_titl', tmp[0])
                P.ModelSetting.set(f'{self.name}_naver_auth', tmp[1])
                P.ModelSetting.set(f'{self.name}_naver_cont', tmp[2])
                P.ModelSetting.set(f'{self.name}_naver_isbn', tmp[3])
                P.ModelSetting.set(f'{self.name}_naver_publ', tmp[4])
                mode = arg2
                if mode == 'api':
                    data = SiteNaverBook.search_api(*tmp)
                else:
                    data = SiteNaverBook.search(*tmp)
                ret['modal'] = d(data)
            elif command == 'info_naver':
                code = arg1
                P.ModelSetting.set(f'{self.name}_naver_code', code)
                data = SiteNaverBook.info(code)
                ret['modal'] = d(data)
            return jsonify(ret)
        except Exception as e: 
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})

    def process_api(self, sub, req):
        if sub == 'search':
            call = req.args.get('call')
            manual = bool(req.args.get('manual'))
            if call == 'plex' or call == 'kodi':
                return jsonify(self.search(req.args.get('keyword'), manual=manual))
        elif sub == 'info':
            call = req.args.get('call')
            data = SiteNaverBook.info(req.args.get('code'))
            if call == 'plex':
                try:
                    data['poster'] = SiteUtil.process_image_book(data['poster'])
                except:
                    pass
            return jsonify(data)
        elif sub == 'top_image':
            url = req.args.get('url')
            ret = SiteUtil.process_image_book(url)
            return jsonify(ret)


    def search(self, keyword, manual=False):
        ret = {}
        tmp = keyword.split('|')
        logger.debug(tmp)
        if len(tmp) == 2:
            data = SiteNaverBook.search(tmp[0], tmp[1], '', '', '')
        elif len(tmp) == 1:
            data = SiteNaverBook.search(tmp[0], '', '', '', '')
        if data['ret'] == 'success':
            return data['data']
