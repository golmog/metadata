from support_site import (SiteDaumMovie, SiteNaverMovie, SiteTmdbMovie,
                          SiteTvingMovie, SiteUtil, SiteWatchaMovie,
                          SiteWavveMovie)

from .setup import *


class ModuleMovie(PluginModuleBase):
    db_default = {
        'movie_first_order' : 'daum, naver, tmdb',
        'movie_use_sub_tmdb' : 'except_daum', # [['all,'모두 사용'], ['except_daum', 'Daum은 사용 안함'], ['none', '사용 안함']]
        'movie_use_sub_tmdb_mode' : 'all', #[['all', '모두 사용'], ['art, 'Art만'], ['actor','배우정보']]
        'movie_use_watcha' : 'True',
        'movie_use_watcha_option' : 'all', # [['all', '모두 사용'], ['review','리뷰만'], ['collection', '컬렉션만']]
        'movie_use_watcha_collection_like_count' : '100',
        'movie_rating_score' : '70',
        'movie_translate_option' : 'text',

        'movie_total_test_search' : '',
        'movie_total_test_info' : '',
        'movie_naver_test_search' : '',
        'movie_naver_test_info' : '',
        'movie_daum_test_search' : '',
        'movie_daum_test_info' : '',
        'movie_tmdb_test_search' : '',
        'movie_tmdb_test_info' : '',
        'movie_watcha_test_search' : '',
        'movie_watcha_test_info' : '',
        'movie_wavve_test_search' : '',
        'movie_wavve_test_info' : '',
        'movie_tving_test_search' : '',
        'movie_tving_test_info' : '',
    }

    module_map = {'naver':SiteNaverMovie, 'daum':SiteDaumMovie, 'tmdb':SiteTmdbMovie, 'watcha':SiteWatchaMovie, 'wavve':SiteWavveMovie, 'tving':SiteTvingMovie}
    module_map2 = {'N':SiteNaverMovie, 'D':SiteDaumMovie, 'T':SiteTmdbMovie, 'C':SiteWatchaMovie, 'W':SiteWavveMovie, 'V':SiteTvingMovie}

    def __init__(self, P):
        super(ModuleMovie, self).__init__(P, name='movie', first_menu='setting')


    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            ret = {'ret':'success', 'json':{}}
            call = command
            mode = arg1
            keyword = arg2.strip()
            param = arg3

            tmps = keyword.split('|')
            year = 1900
            P.ModelSetting.set(f'movie_{call}_test_{mode}', keyword)
            if len(tmps) == 2:
                keyword = tmps[0].strip()
                try: year = int(tmps[1].strip())
                except: year = None
            
            
            if call == 'total':
                if mode == 'search':
                    manual = (param == 'manual')
                    ret['json'] = self.search(keyword, year=year, manual=manual)
                elif mode == 'info':
                    ret['json'] = self.info(keyword)
            else:
                SiteClass = self.module_map[call]
                if mode == 'search':
                    ret['json'] = SiteClass.search(keyword, year=year)
                elif mode == 'info':
                    ret['json'] = SiteClass.info(keyword)
                elif mode == 'search_api':
                    ret['json'] = SiteClass.search_api(keyword)
                elif mode == 'info_api':
                    ret['json'] = SiteClass.info_api(keyword)
            return jsonify(ret)
        except Exception as e: 
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'warning', 'msg':str(e)})

    def process_api(self, sub, req):
        if sub == 'search':
            call = req.args.get('call')
            manual = bool(req.args.get('manual'))
            try: year = int(req.args.get('year'))
            except: year = 1900

            logger.debug(req.args.get('year'))
            logger.debug(year)
            
            if call == 'plex' or call == 'kodi':
                return jsonify(self.search(req.args.get('keyword'), year, manual=manual))
        elif sub == 'info':
            call = req.args.get('call')
            data = self.info(req.args.get('code'))
            if call == 'kodi':
                data = SiteUtil.info_to_kodi(data)
            elif call == 'plex':
                data['movie_rating_score'] = P.ModelSetting.get_int('movie_rating_score')
            return jsonify(data)
        elif sub == 'stream':
            code = req.args.get('code')
            ret = self.stream(code)
            mode = req.args.get('mode')
            logger.debug(ret)
            logger.debug(mode)

            if mode == 'redirect':
                if 'hls' in ret:
                    return redirect(ret['hls'])
            else:
                return jsonify(ret)
            
    #########################################################

    def search(self, keyword, year, manual=False, site_list=None, site_all=False):
        try: 
            if isinstance(year, str):
                year = int(str)
        except: pass
        if keyword.startswith('M'):
            data = self.info(keyword)
            if data is not None:
                item = {}
                item['code'] = keyword
                item['score'] = 100
                item['title'] = data['title']
                item['year'] = data['year']
                item['image_url'] = data['art'][0]['value']
                item['desc'] = data['plot']
                item['site'] = data['site']
                return [item]
    
        ret = []
        if site_list == None or site_list == []:
            site_list = P.ModelSetting.get_list('movie_first_order', ',')
    
        # 한글 영문 분리
        split_index = -1
        is_include_kor = False
        for index, c in enumerate(keyword):
            if ord(u'가') <= ord(c) <= ord(u'힣'):
                is_include_kor = True
                split_index = -1
            elif ord('a') <= ord(c.lower()) <= ord('z'):
                is_include_eng = True
                if split_index == -1:
                    split_index = index
            elif ord('0') <= ord(c.lower()) <= ord('9') or ord(' '):
                pass
            else:
                split_index = -1

        if is_include_kor and split_index != -1:
            kor = keyword[:split_index].strip()
            eng = keyword[split_index:].strip()
        else:
            kor = None
            eng = None

        for key in [keyword, kor, eng]:
            logger.debug('search key : [%s] [%s]', key, year)
            if key is None:
                continue
            for idx, site in enumerate(site_list):
                if year is None:
                    year = 1900
                else:
                    try: year = int(year)
                    except: year = 1900
                site_data = self.module_map[site].search(key, year=year)
                if site_data['ret'] == 'success':
                    for item in site_data['data']:
                        item['score'] -= idx
                    ret += site_data['data']
                    if manual:
                        continue
                    else:
                        if site_all == False and (len(site_data['data']) and site_data['data'][0]['score'] > 85):
                            break
            ret = sorted(ret, key=lambda k: k['score'], reverse=True)  
            if len(ret) > 0 and ret[0]['score'] > 85:
                break
        ret = sorted(ret, key=lambda k: k['score'], reverse=True)
        for item in ret:
            if item['score'] < 10:
                item['score'] = 10
        return ret


    def info(self, code):
        try:
            info = None
            SiteClass = self.module_map2[code[1]]
            tmp = SiteClass.info(code)
            if tmp['ret'] == 'success':
                info = tmp['data']

            if info['title'] == '':
                logger.error('title empty.. change meta site....')
                return
            movie_use_sub_tmdb = P.ModelSetting.get('movie_use_sub_tmdb')
            if code[1] != 'T' and (movie_use_sub_tmdb == 'all' or (movie_use_sub_tmdb == 'except_daum' and code[1] != 'D')):
                try:
                    tmdb_info = None
                    for i  in range(2):
                        tmdb_search = None
                        if i == 0:
                            tmdb_search = SiteTmdbMovie.search(info['title'], year=info['year'])
                            if tmdb_search['ret'] == 'empty':
                                continue
                        elif i == 1:
                            if 'title_en' in info['extra_info']:
                                tmdb_search = SiteTmdbMovie.search(info['extra_info']['title_en'], year=info['year'])
                            elif info['originaltitle'] != '':
                                tmdb_search = SiteTmdbMovie.search(info['originaltitle'], year=info['year'])
                        if tmdb_search is None:
                            continue
                        if tmdb_search['ret'] == 'success' and len(tmdb_search['data']) > 0:
                            count = 0
                            for item in tmdb_search['data']:
                                if item['score'] == 100:
                                    count += 1
                                else:
                                    break
                            if count == 1:
                                tmdb_data = SiteTmdbMovie.info(tmdb_search['data'][0]['code'])
                                if tmdb_data['ret'] == 'success':
                                    tmdb_info = tmdb_data['data']
                            if count == 0:
                                if tmdb_search['data'][0]['score'] > 85 or ('title_en' in info['extra_info'] and SiteUtil.compare(info['extra_info']['title_en'], tmdb_search['data'][0]['originaltitle'])):
                                    tmdb_data = SiteTmdbMovie.info(tmdb_search['data'][0]['code'])
                                    if tmdb_data['ret'] == 'success':
                                        tmdb_info = tmdb_data['data']
                        if tmdb_info is not None:
                            break

                    if tmdb_info is not None:
                        logger.debug('tmdb :%s %s', tmdb_info['title'], tmdb_info['year'])  
                        #logger.debug(json.dumps(tmdb_info, indent=4))
                        logger.debug('tmdb_info : %s', tmdb_info['title'])
                        movie_use_sub_tmdb_mode = P.ModelSetting.get('movie_use_sub_tmdb_mode')
                        if movie_use_sub_tmdb_mode == '0':
                            info['extras'] += tmdb_info['extras']
                            self.change_tmdb_actor_info(tmdb_info['actor'], info['actor'])
                            info['actor'] = tmdb_info['actor']
                            info['art'] += tmdb_info['art']
                        elif movie_use_sub_tmdb_mode == '1':
                            info['art'] += tmdb_info['art']
                        elif movie_use_sub_tmdb_mode == '2':
                            self.change_tmdb_actor_info(tmdb_info['actor'], info['actor'])
                            info['actor'] = tmdb_info['actor']
                        info['code_list'] += tmdb_info['code_list']
                        if info['plot'] == '':
                            info['plot'] = tmdb_info['plot']
                except Exception as e: 
                    logger.error(f"Exception:{str(e)}")
                    logger.error(traceback.format_exc())
                    logger.error('tmdb search fail..')
           
            if True:
                try:
                    wavve_info = None
                    wavve_search = SiteWavveMovie.search(info['title'], year=info['year'])
                    if wavve_search['ret'] == 'success' and len(wavve_search['data']) > 0:
                        tmp = SiteWavveMovie.info(wavve_search['data'][0]['code'])
                        if tmp != None:
                            tmp = tmp['data']
                            if SiteUtil.compare(info['title'], tmp['title']) and abs(info['year'] - tmp['year']) <= 1:
                                wavve_info = tmp
                    if wavve_info is not None:
                        info['code_list'] += wavve_info['code_list']
                        info['art'] += wavve_info['art']
                        if 'wavve_stream' in wavve_info['extra_info']:
                            info['extra_info']['wavve_stream'] = wavve_info['extra_info']['wavve_stream']
                except Exception as e: 
                    logger.error(f"Exception:{str(e)}")
                    logger.error(traceback.format_exc())
                    logger.error('wavve search fail..')

            if True:
                try:
                    tving_info = None
                    tving_search = SiteTvingMovie.search(info['title'], year=info['year'])
                    if tving_search['ret'] == 'success' and len(tving_search['data']) > 0:
                        tmp = SiteTvingMovie.info(tving_search['data'][0]['code'])
                        if tmp['ret'] == 'success':
                            tmp = tmp['data']
                            if SiteUtil.compare(info['title'], tmp['title']) and abs(info['year'] - tmp['year']) <= 1:
                                tving_info = tmp
                    if tving_info is not None:
                        info['code_list'] += tving_info['code_list']
                        info['art'] += tving_info['art']
                        if 'tving_stream' in tving_info['extra_info']:
                            info['extra_info']['tving_stream'] = tving_info['extra_info']['tving_stream']
                        
                except Exception as e: 
                    logger.error(f"Exception:{str(e)}")
                    logger.error(traceback.format_exc())
                    logger.error('tving search fail..')
            
            if P.ModelSetting.get_bool('movie_use_watcha'):
                try:
                    movie_use_watcha_option = P.ModelSetting.get('movie_use_watcha_option')
                    watcha_info = None
                    watcha_search = SiteWatchaMovie.search(info['title'], year=info['year'])
                    
                    if watcha_search['ret'] == 'success' and len(watcha_search['data'])>0:
                        if watcha_search['data'][0]['score'] > 85:
                            watcha_data = SiteWatchaMovie.info(watcha_search['data'][0]['code'], like_count=P.ModelSetting.get_int('movie_use_watcha_collection_like_count'))
                            if watcha_data['ret'] == 'success':
                                watcha_info = watcha_data['data']
                    
                    if watcha_info is not None:
                        if movie_use_watcha_option in ['all', 'review']:
                            info['review'] = watcha_info['review']
                            info['code_list'] += watcha_info['code_list']
                            info['code_list'].append(['google_search', u'영화 ' + info['title']])
                            
                            for idx, review in enumerate(info['review']):
                                if idx >= len(info['code_list']):
                                    break
                                if info['code_list'][idx][0] == 'naver_id':
                                    review['source'] = '네이버'
                                    review['link'] = 'https://movie.naver.com/movie/bi/mi/basic.nhn?code=%s' % info['code_list'][idx][1]
                                elif info['code_list'][idx][0] == 'daum_id':
                                    review['source'] = '다음'
                                    review['link'] = 'https://movie.daum.net/moviedb/main?movieId=%s' % info['code_list'][idx][1]
                                elif info['code_list'][idx][0] == 'wavve_id':
                                    review['source'] = '웨이브'
                                    review['link'] = 'https://www.wavve.com/player/movie?movieid=%s' % info['code_list'][idx][1]
                                elif info['code_list'][idx][0] == 'tving_id':
                                    review['source'] = '티빙'
                                    review['link'] = 'https://www.tving.com/movie/player/%s' % info['code_list'][idx][1]
                                elif info['code_list'][idx][0] == 'tmdb_id':
                                    review['source'] = 'TMDB'
                                    review['link'] = 'https://www.themoviedb.org/movie/%s?language=ko' % info['code_list'][idx][1]
                                elif info['code_list'][idx][0] == 'imdb_id':
                                    review['source'] = 'IMDB'
                                    review['link'] = 'https://www.imdb.com/title/%s/' % info['code_list'][idx][1]
                                elif info['code_list'][idx][0] == 'watcha_id':
                                    review['source'] = '왓챠 피디아'
                                    review['link'] = 'https://pedia.watcha.com/ko-KR/contents/%s' % info['code_list'][idx][1]
                                elif info['code_list'][idx][0] == 'google_search':
                                    review['source'] = '구글 검색'
                                    review['link'] = 'https://www.google.com/search?q=%s' % info['code_list'][idx][1]
                        if movie_use_watcha_option in ['all', 'collection']: 
                            info['tag'] += watcha_info['tag']
                except Exception as e: 
                    logger.error(f"Exception:{str(e)}")
                    logger.error(traceback.format_exc())
                    logger.error('watcha search fail..')
            self.process_trans(info)
            max_poster = 0
            max_art = 0
            info['main_poster'] = None
            info['main_art'] = None
            for _ in info['art']:
                if _['aspect'] == 'poster':
                    if max_poster < _['score']:
                        info['main_poster'] = _['value']
                        max_poster = _['score']
                if _['aspect'] == 'landscape':
                    if max_art < _['score']:
                        info['main_art'] = _['value']
                        max_art = _['score']
            return info                    


        except Exception as e: 
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())

    
    def process_trans(self, data):
        # [['none', '번역하지 않음'], ['text', '제목,줄거리만'], ['all', '배우 이름,역할 포함 전부']]
        mode = P.ModelSetting.get('movie_translate_option')
        if mode == 'none':
            return
          
        if SiteUtil.is_include_hangul(data['plot']) == False:
            data['plot'] = SiteUtil.trans(data['plot'], source='en')
        if mode == 'all':
            for actor in data['actor']:
                if SiteUtil.is_include_hangul(actor['name']) == False:
                    actor['name'] = SiteUtil.trans(actor['name'], source='en')
                if SiteUtil.is_include_hangul(actor['role']) == False:
                    actor['role'] = SiteUtil.trans(actor['role'], source='en')
        return data



    def change_tmdb_actor_info(self, tmdb_info, portal_info):
        if len(portal_info) == 0:
            return
        for tmdb in tmdb_info:
            #logger.debug(tmdb['name'])
            for portal in portal_info:
                #logger.debug(portal['originalname'])
                if tmdb['name'] == portal['originalname']:
                    tmdb['name'] = portal['name']
                    tmdb['role'] = portal['role']
                    break
