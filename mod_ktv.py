from support_site import (MetadataServerUtil, SiteDaumTv, SiteTmdbTv,
                          SiteTvingTv, SiteUtil, SiteWavveTv, SupportTving,
                          SupportWavve)

from .setup import *


class ModuleKtv(PluginModuleBase):
    db_default = {
        'ktv_use_tmdb' : 'True',
        'ktv_use_kakaotv' : 'False',
        'ktv_use_kakaotv_episode' : 'False',
        'ktv_use_theme' : 'True',
        'ktv_change_actor_name_rule' : '',
        # Daum 에피소드 줄거리에서 날짜, 제목 제거.
        'ktv_summary_duplicate_remove' : 'False',

        'ktv_total_test_search' : '',
        'ktv_daum_test_search' : '',
        'ktv_daum_test_episode' : '',
        'ktv_wavve_test_search' : '',
        'ktv_wavve_test_info' : '',
        'ktv_tving_test_search' : '',
        'ktv_tving_test_info' : '',
        'ktv_total_test_info' : '',
    }

    module_map = {'daum':SiteDaumTv, 'tving':SiteTvingTv, 'wavve':SiteWavveTv, 'tmdb':SiteTmdbTv}

    def __init__(self, P):
        super(ModuleKtv, self).__init__(P, name='ktv', first_menu='setting')
    
    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            call = command
            mode = arg1
            keyword = arg2.strip()
            manual = (arg3 == 'manual')
            P.ModelSetting.set(f"ktv_{call}_test_{mode}", keyword)
            ret = {'ret':'success', 'json':{}}
            if call == 'total':
                if mode == 'search':
                    ret['json']['search'] = self.search(keyword, manual=manual)
                    if 'daum' in ret['json']['search']:
                        ret['json']['info'] = self.info(ret['json']['search']['daum']['code'], ret['json']['search']['daum']['title'])
                    elif 'tving' in ret['json']['search']:
                        ret['json']['info'] = self.info(ret['json']['search']['tving'][0]['code'], '')
                    elif 'wavve' in ret['json']['search']:
                        ret['json']['info'] = self.info(ret['json']['search']['wavve'][0]['code'], '')
                elif mode == 'info':
                    code = keyword
                    title = ''
                    tmps = keyword.split('|')
                    if len(tmps) == 2:
                        code = tmps[0]
                        title = tmps[1]
                    ret['json']['info'] = self.info(code, title)
            elif call == 'daum':
                if mode == 'search':
                    ret['json']['search'] = SiteDaumTv.search(keyword)
                    if ret['json']['search']['ret'] == 'success':
                        ret['json']['info'] = self.info(ret['json']['search']['data']['code'], ret['json']['search']['data']['title'])
                elif mode == 'episode':
                    ret['json']['episode'] = self.episode_info(keyword)
            elif call == 'wavve':
                if mode == 'search':
                    ret['json'] = SupportWavve.search_tv(keyword)
                elif mode == 'info':
                    ret['json']['program'] = SupportWavve.vod_programs_programid(keyword)
                    ret['json']['episodes'] = []
                    page = 1
                    while True:
                        episode_data = SupportWavve.vod_program_contents_programid(keyword, page=page)
                        ret['json']['episodes'] += episode_data['list']
                        page += 1
                        if episode_data['pagecount'] == episode_data['count']:# or page == 6:
                            break
            elif call == 'tving':
                if mode == 'search':
                    ret['json'] = SupportTving.search(keyword)
                elif mode == 'info':
                    ret['json']['program'] = SupportTving.get_program_programid(keyword)
                    ret['json']['episodes'] = []
                    page = 1
                    while True:
                        episode_data = SupportTving.get_frequency_programid(keyword, page=page)
                        for epi in episode_data['result']:
                            ret['json']['episodes'].append(epi['episode'])
                        page += 1
                        if episode_data['has_more'] == 'N':
                            break
            return jsonify(ret)
        except Exception as e: 
            P.logger.error('Exception:%s', e)
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
            data = self.info(req.args.get('code'), req.args.get('title'))
            if call == 'kodi':
                data = SiteUtil.info_to_kodi(data)
            return jsonify(data)
        elif sub == 'episode_info':
            return jsonify(self.episode_info(req.args.get('code')))

    #########################################################

    def search(self, keyword, manual=False):
        ret = {}
        site_list = ['daum', 'tving', 'wavve']
        for idx, site in enumerate(site_list):
            site_data = self.module_map[site].search(keyword)
            if site_data['ret'] == 'success':
                ret[site] = site_data['data']
                #logger.info(u'KTV 검색어 : %s site : %s 매칭', keyword, site)
                if manual:
                    continue
                return ret
        return ret

    def info(self, code, title):
        try:
            show = None
            if code[1] == 'D':
                tmp = SiteDaumTv.info(code, title)
                if tmp['ret'] == 'success':
                    show = tmp['data']

                if 'kakao_id' in show['extra_info'] and show['extra_info']['kakao_id'] is not None and P.ModelSetting.get_bool('ktv_use_kakaotv'):
                    show['extras'] = SiteDaumTv.get_kakao_video(show['extra_info']['kakao_id'])

                if P.ModelSetting.get_bool('ktv_use_tmdb'):
                    tmdb_id = SiteTmdbTv.search_tv(show['title'], show['premiered'])
                    show['extra_info']['tmdb_id'] = tmdb_id
                    if tmdb_id is not None:
                        show['tmdb'] = {}
                        show['tmdb']['tmdb_id'] = tmdb_id
                        SiteTmdbTv.apply(tmdb_id, show, apply_image=True, apply_actor_image=True)

                if 'tving_episode_id' in show['extra_info']:
                    SiteTvingTv.apply_tv_by_episode_code(show, show['extra_info']['tving_episode_id'], apply_plot=True, apply_image=True )
                else: #use_tving 정도
                    SiteTvingTv.apply_tv_by_search(show, apply_plot=True, apply_image=True)

                SiteWavveTv.apply_tv_by_search(show)
                #extra
                if P.ModelSetting.get_bool('ktv_use_theme'):
                    extra = MetadataServerUtil.get_meta_extra(code)
                    if extra is not None:
                        if 'themes' in extra:
                            show['extra_info']['themes'] = extra['themes']

            elif code[1] == 'V': 
                tmp = SiteTvingTv.info(code)
                if tmp['ret'] == 'success':
                    show = tmp['data']
            elif code[1] == 'W': 
                tmp = SiteWavveTv.info(code)
                if tmp['ret'] == 'success':
                    show = tmp['data']

            logger.info('KTV info title:%s code:%s tving:%s wavve:%s', title, code, show['extra_info']['tving_id'] if 'tving_id' in show['extra_info'] else None, show['extra_info']['wavve_id'] if 'wavve_id' in show['extra_info'] else None)

            if show is not None:
                try:
                    rules = P.ModelSetting.get_list('ktv_change_actor_name_rule', '\n')
                    for rule in rules:
                        tmps = rule.split('|')
                        if len(tmps) != 3:
                            continue
                        if tmps[0] == show['title']:
                            for actor in show['actor']:
                                if actor['name'] == tmps[1]:
                                    actor['name'] = tmps[2]
                                    break
                except Exception as e: 
                    P.logger.error('Exception:%s', e)
                    P.logger.error(traceback.format_exc())

                return show

        except Exception as e: 
            P.logger.error('Exception:%s', e)
            P.logger.error(traceback.format_exc())

    
    
    def episode_info(self, code):
        try:
            if code[1] == 'D':
                from support_site import SiteDaumTv
                data = SiteDaumTv.episode_info(code, include_kakao=P.ModelSetting.get_bool('ktv_use_kakaotv_episode'), summary_duplicate_remove=P.ModelSetting.get_bool('ktv_summary_duplicate_remove'))
                if data['ret'] == 'success':
                    return data['data']

        except Exception as e: 
            P.logger.error('Exception:%s', e)
            P.logger.error(traceback.format_exc())
