
from collections import OrderedDict

from support_site import (MetadataServerUtil, SiteDaumTv, SiteTmdbFtv,
                          SiteTmdbTv, SiteTvdbTv, SiteTvingTv, SiteUtil,
                          SiteWatchaTv, SiteWavveTv)

from .setup import *


class ModuleFtv(PluginModuleBase):
    db_default = {
        'ftv_use_extra_match' : 'True',
        'ftv_option_actor' : 'tmdb', # [['tmdb, 'tmdb 정보 우선. 매칭된 배우만 한글적용'], ['change_daum', '국내 사이트 정보로 전부 대체'], ['tmdb_match', 'tmdb 정보 우선. 매칭된 배우만 한글적용 후 앞으로 배치'], ['tmdb_match_append_daum', 'tmdb 정보 우선. 매칭된 배우만 한글적용 후 앞으로 배치. 남은 국내정보 추가']] 
        'ftv_use_extra_season' : 'True',
        'ftv_use_extra_video' : 'True',
        'ftv_use_meta_server' : 'True',
        'ftv_season_order' : 'wavve, tving, daum',
        'ftv_translate_option' : 'text',
        'ftv_use_theme' : 'True',

        'ftv_total_test_search' : '',
        'ftv_total_test_info' : '',
        'ftv_tvdb_test_search' : '',
        'ftv_tvdb_test_info' : '',
        'ftv_tmdb_test_search' : '',
        'ftv_tmdb_test_info' : '',
        'ftv_tmdb_test_info_season' : '',
        'ftv_daum_test_search' : '',
        'ftv_daum_test_info' : '',
        'ftv_watcha_test_search' : '',
        'ftv_watcha_test_info' : '',
    }
    memory_cache = {'my':{}, 'server':{}}
    module_map = {'daum':SiteDaumTv, 'tvdb':SiteTvdbTv, 'tmdb':SiteTmdbTv, 'watcha':SiteWatchaTv, 'tmdb':SiteTmdbFtv}

    def __init__(self, P):
        super(ModuleFtv, self).__init__(P, name='ftv', first_menu='setting')


    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        if sub == 'setting':
            arg['cache_info'] = self.get_cache_info()
        return render_template('{package_name}_{module_name}_{sub}.html'.format(package_name=P.package_name, module_name=self.name, sub=sub), arg=arg)
        

    def process_command(self, command, arg1, arg2, arg3, req):
        ret = {'ret':'success', 'json':None}
        if command == 'reset_cache':
            self.reset_cache()
            ret['msg'] = '리셋하였습니다'
            ret['data'] = self.get_cache_info()
        else:
            keyword = arg2.strip()
            call = command
            mode = arg1
            P.ModelSetting.set(f'ftv_{call}_test_{mode}', keyword)
            if mode == 'search':
                tmps = keyword.split('|')
                year = None
                if len(tmps) == 2:
                    keyword = tmps[0].strip()
                    try: year = int(tmps[1].strip())
                    except: year = None
            if call == 'total':
                if mode == 'search':
                    manual = (arg3 == 'manual')
                    ret['json'] = self.search(keyword, year=year, manual=manual)
                elif mode == 'info':
                    ret['json'] = self.info(keyword)
            else:
                SiteClass = self.module_map[call]
                if mode == 'search':
                    ret['json'] = SiteClass.search(keyword, year=year)
                elif mode == 'info':
                    if SiteClass == SiteDaumTv:
                        tmps = keyword.split('|')
                        if len(tmps) != 2:
                            ret['ret'] = 'warning'
                            ret['msg'] = "포맷 확인 필요"
                        else:
                            ret['json'] = SiteClass.info(tmps[0], tmps[1])
                    else:
                        ret['json'] = SiteClass.info(keyword)
                elif mode == 'search_api':
                    ret['json'] = SiteClass.search_api(keyword)
                elif mode == 'info_api':
                    ret['json'] = SiteClass.info_api(keyword)
                elif mode == 'info_season':
                    ret['json'] = SiteClass.info_season(keyword)
                elif mode == 'info_season_api':
                    ret['json'] = SiteClass.info_season_api(keyword)
            return jsonify(ret)


    def process_api(self, sub, req):
        if sub == 'search':
            call = req.args.get('call')
            try: year = int(req.args.get('year'))
            except: year = None
            manual = bool(req.args.get('manual'))
            if call == 'plex' or call == 'kodi':
                return jsonify(self.search(req.args.get('keyword'), manual=manual, year=year))
        elif sub == 'info':
            call = req.args.get('call')
            data = self.info(req.args.get('code'))
            if call == 'kodi':
                data = SiteUtil.info_to_kodi(data)
            return jsonify(data)

    #########################################################

    def search(self, keyword, year=None, manual=False):
        keyword = keyword.split(u'시즌')[0]
        logger.debug('FTV search keyword:[%s] year:[%s] manual:[%s]', keyword, year, manual)
        tmdb_ret = SiteTmdbFtv.search(keyword, year=year)
        ret = []
        if tmdb_ret['ret'] == 'success':
            if tmdb_ret['data'][0]['score'] >= 95:
                return tmdb_ret['data']
            else:
                ret += tmdb_ret['data']

        if SiteUtil.is_include_hangul(keyword):
            watcha_ret = SiteWatchaTv.search(keyword, year=year)
            if watcha_ret['ret'] == 'success':
                en_keyword = watcha_ret['data'][0]['title_en']
                logger.debug(en_keyword)
                if en_keyword is not None:
                    tmdb_ret = SiteTmdbFtv.search(en_keyword, year=year)
                    
                    if tmdb_ret['ret'] == 'success': #and tmdb_ret['data'][0]['score'] >= 95:
                        ret += tmdb_ret['data']
        return ret


    def info(self, code):
        logger.debug('FTV info [%s]', code)
        try:
            tmp = code.split('_')
            if len(tmp) == 1:
                if code[1] == 'T':
                    tmdb_info = SiteTmdbFtv.info(code)
                    if tmdb_info['ret'] != 'success':
                        return
                    data = tmdb_info['data']
                    if P.ModelSetting.get_bool('ftv_use_extra_match'):
                        self.info_extra_match(data)
                    
                    data['use_theme'] = P.ModelSetting.get_bool('ftv_use_theme')
                    self.process_trans('show', data)
                    self.set_cache('my', code, data)
                    return data
            elif len(tmp) == 2:
                if code[1] == 'T':
                    tmdb_info = SiteTmdbFtv.info_season(code)
                    if tmdb_info['ret'] != 'success':
                        return
                    data = tmdb_info['data']
                    if P.ModelSetting.get_bool('ftv_use_extra_match') and P.ModelSetting.get_bool('ftv_use_extra_season'):
                        self.info_extra_season(data)
                    self.process_trans('season', data)
                    return data
        except Exception as e: 
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())


    def process_trans(self, data_type, data):
        mode = P.ModelSetting.get('ftv_translate_option')
        if mode == 'none':
            return

        if data_type == 'show':
            if data['is_plot_kor'] == False:
                data['is_plot_kor'] = True
                data['plot'] = SiteUtil.trans(data['plot'], source='en')
            if mode == 'all':
                for actor in data['actor']:
                    #if 'is_kor_name' in actor and actor['is_kor_name'] == False:
                    #    actor['name'] = SiteUtil.trans(actor['name_original'], source='en')
                    #    actor['role'] = SiteUtil.trans(actor['role'], source='en')
                    if SiteUtil.is_include_hangul(actor['name']) == False:
                        actor['name'] = SiteUtil.trans(actor['name'], source='en')
                    if SiteUtil.is_include_hangul(actor['role']) == False:
                        actor['role'] = SiteUtil.trans(actor['role'], source='en')
                lists = [data['director'], data['producer'], data['writer']]
                for _list in lists:
                    new = []
                    for name in _list:
                        if SiteUtil.is_include_hangul(name) == False:
                            new.append(SiteUtil.trans(name, source='en'))
                        else:
                            new.append(name)
                    _list = new

        if data_type == 'season':
            for key, tmdb_epi in data['episodes'].items():
                try:
                    if tmdb_epi['is_title_kor'] == False:
                        tmdb_epi['title'] = SiteUtil.trans(tmdb_epi['title'], source='en')
                    if tmdb_epi['is_plot_kor'] == False:
                        tmdb_epi['plot'] = SiteUtil.trans(tmdb_epi['plot'], source='en')
                except:
                    pass
        return data



    # 시즌정보 
    def info_extra_season(self, data):
        try:
            trash, series_info = self.get_cache('my', data['parent_code'])
            meta_server_season_dict = None
            if P.ModelSetting.get_bool('ftv_use_meta_server'):
                extra = self.get_meta_extra(series_info['code'])
                if extra is not None:
                    if 'seasons' in extra:
                        if str(data['season_no']) in extra['seasons']:
                            meta_server_season_dict = extra['seasons'][str(data['season_no'])]

                if meta_server_season_dict:
                    logger.debug(meta_server_season_dict)
                    for site in P.ModelSetting.get_list('ftv_season_order', ','):
                        if site in meta_server_season_dict:
                            value = meta_server_season_dict[site]
                            if site == 'daum':
                                tmp = value.split('|')
                                daum_season_info = SiteDaumTv.info('KD' + tmp[0], tmp[1])
                                if daum_season_info is not None and daum_season_info['ret'] == 'success':
                                    daum_season_info = daum_season_info['data']
                                    if len(daum_season_info['extra_info']['episodes'].keys()) > 0:
                                        self.apply_season_info_by_daum(data, daum_season_info)
                                        return
                            elif site in ['wavve', 'tving']:
                                if self.apply_season_info(data, value, site):
                                    return
            tmp = self.get_daum_search(series_info)
            if tmp is None:
                return data
            title = tmp[0]
            daum_search_data = tmp[1]['data']
            if True or data['studio'].lower().find(daum_search_data['studio'].lower()) != -1 or daum_search_data['studio'].lower().find(data['studio'].lower()) != -1:
                daum_season_info = None
                for season_no, season in enumerate(daum_search_data['series']):
                    season_no += 1
                    if data['season_no'] == season_no:
                        logger.debug('daum on ftv code:[%s], title:[%s]', season['code'], season['title'])
                        daum_season_info = SiteDaumTv.info(season['code'], season['title'])
                        if daum_season_info is not None and daum_season_info['ret'] == 'success':
                            daum_season_info = daum_season_info['data']
                        else:
                            logger.debug('Daum fail : %s', title)
                        break
                if daum_season_info:
                    self.apply_season_info_by_daum(data, daum_season_info)
        except Exception as e: 
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())
        return data


    def apply_season_info(self, tmdb_info, code, site):
        try:
            if site == 'wavve':
                tmp = SiteWavveTv.info('XX'+code)
            elif site == 'tving':
                tmp = SiteTvingTv.info('XX'+code)
            if tmp['ret'] == 'success':
                source_episodes = tmp['data']['extra_info']['episodes']
                for key, tmdb_epi in tmdb_info['episodes'].items():
                    if int(key) in source_episodes:
                        src_epi = source_episodes[int(key)][site]
                        if src_epi['title'] != '':
                            tmdb_epi['title'] = src_epi['title']
                            tmdb_epi['is_title_kor'] = True
                        if src_epi['plot'] != '':
                            tmdb_epi['plot'] = src_epi['plot']
                            tmdb_epi['is_plot_kor'] = True
                        if src_epi['thumb'] != '':
                            tmdb_epi['art'].append(src_epi['thumb'])
                return True
        except Exception as e: 
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())
        return False       


    def apply_season_info_by_daum(self, tmdb_info, daum_info):
        daum_episodes = daum_info['extra_info']['episodes']
        for key, tmdb_epi in tmdb_info['episodes'].items():
            if int(key) in daum_episodes:
                daum_epi = SiteDaumTv.episode_info(daum_episodes[int(key)]['daum']['code'], is_ktv=False)['data']
                tmdb_epi['title'] = daum_epi['title'] if daum_epi['title'] != '' else tmdb_epi['title']
                if daum_epi['title'] != '':
                    tmdb_epi['title'] = daum_epi['title']
                    tmdb_epi['is_title_kor'] = True

                if daum_epi['plot'] != '':
                    tmdb_epi['plot'] = daum_epi['plot'].replace(daum_epi['title'], '').strip()
                    tmdb_epi['is_plot_kor'] = True


    def info_extra_match(self, data):
        try:
            daum_list = []
            if P.ModelSetting.get_bool('ftv_use_meta_server'):
                extra = self.get_meta_extra(data['code'])
                if extra is not None:
                    if 'themes' in extra:
                        data['extra_info']['themes'] = extra['themes']
                    if 'seasons' in extra:
                        keys = extra['seasons'].keys()
                        keys = [int(x) for x in keys]
                        keys = sorted(keys)
                        for key in keys:
                            if 'daum' in extra['seasons'][str(key)]:
                                tmp = extra['seasons'][str(key)]['daum'].split('|')
                                daum_list.append(['KD'+tmp[0], tmp[1]])
            if len(daum_list) == 0:
                tmp = self.get_daum_search(data)
                if tmp is not None:
                    title = tmp[0]
                    daum_search_data = tmp[1]['data']
                    if SiteUtil.is_include_hangul(data['title']) == False:
                        data['title'] = title.split(u'시즌')[0].split(u'1기')[0].strip()
                    for season_no, season in enumerate(daum_search_data['series'][:len(data['seasons'])]):        
                        daum_list.append([season['code'], season['title']])
            logger.debug('탐색 : %s', daum_list)
            daum_actor_list = OrderedDict()
            for daum_one_of_list in daum_list:
                daum_season_info = SiteDaumTv.info(daum_one_of_list[0], daum_one_of_list[1])
                if daum_season_info['ret'] == 'success':
                    daum_season_info = daum_season_info['data']
                if data['is_plot_kor'] == False: #화이트퀸
                    data['plot'] = daum_season_info['plot']
                    data['is_plot_kor'] = True
                for actor in daum_season_info['actor']:
                    if actor['name'] not in daum_actor_list:
                        daum_actor_list[actor['name']] = actor
                if P.ModelSetting.get_bool('ftv_use_extra_video'):
                    data['extras'] += daum_season_info['extras']
                if len(daum_actor_list.keys()) > 30:
                    break
            option_actor = P.ModelSetting.get('ftv_option_actor')
            if option_actor == 'change_daum':
                data['actor'] = []
                for key, value in daum_actor_list.items():
                    data['actor'].append({'name':value['name'], 'role':value['role'], 'image':value['thumb']})
            else:
                for key, value in daum_actor_list.items():
                    tmp = SiteDaumTv.get_actor_eng_name(key)
                    if tmp is not None:
                        value['eng_name'] = tmp 
                        logger.debug('[%s] [%s]', key, value['eng_name'])
                    else:
                        value['eng_name'] = None

                for actor in data['actor']:
                    actor['is_kor_name'] = False
                    for key, value in daum_actor_list.items():
                        # tmdb에 한글이름 누군가 등록한 상태. 이 이름과 daum이름이 같으면 롤도 업데이트
                        # 예 : 바이킹스 구스타프 스가스가드
                        if actor['name_ko'] != '' and actor['name_ko'].replace(' ', '') == value['name'].replace(' ', ''):
                            actor['name'] = actor['name_ko']
                            actor['role'] = value['role']
                            actor['is_kor_name']= True
                            del daum_actor_list[key]
                            break
                        if value['eng_name'] is None:
                            continue
                        for tmp_name in value['eng_name']:
                            if (actor['name_original'].lower().replace(' ', '') == tmp_name.lower().replace(' ', '')):
                                actor['name'] = actor['name_ko'] = value['name']
                                actor['role'] = value['role']
                                actor['is_kor_name']= True
                                del daum_actor_list[key]
                                break
                        if actor['is_kor_name']:
                            break
                    actor['role'] = actor['role'].replace('&#39;', '\"')
                if option_actor == 'tmdb_match' or option_actor == 'tmdb_match_append_daum':
                    tmp1 = []
                    tmp2 = []
                    for actor in data['actor']:
                        if actor['is_kor_name']:
                            tmp1.append(actor)
                        else:
                            tmp2.append(actor)
                    data['actor'] = tmp1 + tmp2
                if option_actor == 'tmdb_match_append_daum':
                    for key, value in daum_actor_list.items():
                        value['image'] = value['thumb']
                        del value['eng_name']
                        del value['thumb']
                        data['actor'].append(value)
        except Exception as e: 
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())


    #####################################################################################
    # 매칭 유틸

    def __get_daum_search(self, tmdb_title, series_year, season_count):
        logger.debug('get_daum_search title:[%s], year:[%s], season_count:[%s]', tmdb_title, series_year, season_count)
        title = re.sub(r'\(\d{4}\)', '', tmdb_title).strip()
        
        search_title = []
        if SiteUtil.is_include_hangul(title):
            if season_count == 1:
                search_title = [title]
            else:
                search_title = [title, u'%s 시즌 1' % title, u'%s 1기' % title]
        else:
            watcha_search = SiteWatchaTv.search(title, year=series_year, season_count=season_count)
            if watcha_search['ret'] != 'success':
                logger.debug('watcha search fail : %s %s %s', title, series_year, season_count)
                return
            tmp = watcha_search['data'][0]['title'] 
            if season_count == 1:
                search_title = [tmp]
            else:
                search_title = [u'%s 시즌 1' % tmp, u'%s 1기' % tmp]
        for title in search_title:
            daum_search = SiteDaumTv.search(title, year=series_year)
            if daum_search['ret'] == 'success':
                logger.debug('title : %s', title)
                return [title, daum_search]


    #####################################################################################
    # 캐시 활용
    def get_daum_search(self, series_info):
        unique = series_info['code']+'_daum'
        cache_ret = self.get_cache('my', unique)
        if cache_ret[0]:
            data = cache_ret[1]
        else:
            data = self.__get_daum_search(series_info['title'], series_info['year'], len(series_info['seasons']))
            self.set_cache('server', unique, data)
        return data


    def get_meta_extra(self, code):
        cache_ret = self.get_cache('server', code)
        if cache_ret[0]:
            extra = cache_ret[1]
        else:
            extra = MetadataServerUtil.get_meta_extra(code)
            self.set_cache('server', code, extra)
        return extra


    #####################################################################################
    #  캐시 유틸
    def get_cache(self, mode, code):
        if code in self.memory_cache[mode]:
            return [True, self.memory_cache[mode][code]]
        else:
            return [False]

    def set_cache(self, mode, code, data):
        if len(self.memory_cache[mode].keys()) > 100:
            self.memory_cache[mode] = {}
        self.memory_cache[mode][code] = data

    def reset_cache(self):
        self.memory_cache = {'my':{}, 'server':{}}
    
    def get_cache_info(self):
        return 'my : %s / server : %s' % (len(self.memory_cache['my'].keys()), len(self.memory_cache['server'].keys()) )

