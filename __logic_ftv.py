# -*- coding: utf-8 -*-
#########################################################
# python
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime

import lxml.html
# third-party
import requests
# third-party
from flask import (Response, jsonify, redirect, render_template, request,
                   send_file)
# sjva 공용
from framework import (SystemModelSetting, app, db, path_data, py_urllib,
                       scheduler, socketio)
from framework.common.util import headers
from framework.util import Util
from lxml import etree as ET
from plugin import LogicModuleBase, default_route_socketio
from sqlalchemy import and_, desc, func, not_, or_
from system import SystemLogicTrans

# 패키지
from support_site import (SiteDaumTv, SiteTmdbFtv, SiteTmdbTv, SiteTvdbTv,
                          SiteUtil, SiteWatchaTv)

from .plugin import P

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting

from support_site import MetadataServerUtil

#########################################################

class LogicFtv(LogicModuleBase):
    db_default = {
        'ftv_db_version' : '1',
        'ftv_total_test_search' : '',
        'ftv_total_test_info' : '',

        'ftv_tvdb_test_search' : '',
        'ftv_tvdb_test_info' : '',

        'ftv_tmdb_test_search' : '',
        'ftv_tmdb_test_info' : '',
        
        'ftv_daum_test_search' : '',
        'ftv_daum_test_info' : '',

        'ftv_watcha_test_search' : '',
        'ftv_watcha_test_info' : '',

        
    }

    module_map = {'daum':SiteDaumTv, 'tvdb':SiteTvdbTv, 'tmdb':SiteTmdbTv, 'watcha':SiteWatchaTv, 'tmdb':SiteTmdbFtv}

    def __init__(self, P):
        super(LogicFtv, self).__init__(P, 'setting')
        self.name = 'ftv'

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        arg['sub'] = self.name

        try:
            return render_template('{package_name}_{module_name}_{sub}.html'.format(package_name=P.package_name, module_name=self.name, sub=sub), arg=arg)
        except:
            return render_template('sample.html', title='%s - %s' % (P.package_name, sub))

    def process_ajax(self, sub, req):
        try:
            if sub == 'test':
                keyword = req.form['keyword'].strip()
                call = req.form['call']
                mode = req.form['mode']
                ModelSetting.set('ftv_%s_test_%s' % (call, mode), keyword)
                tmps = keyword.split('|')
                year = None
                if len(tmps) == 2:
                    keyword = tmps[0].strip()
                    try: year = int(tmps[1].strip())
                    except: year = None

                if call == 'total':
                    if mode == 'search':
                        manual = (req.form['manual'] == 'manual')
                        ret = self.search(keyword, year=year, manual=manual)
                    elif mode == 'info':
                        ret = self.info(keyword)
                else:
                    SiteClass = self.module_map[call]
                    if mode == 'search':
                        ret = SiteClass.search(keyword, year=year)
                    elif mode == 'info':
                        ret = SiteClass.info(keyword)
                    elif mode == 'search_api':
                        ret = SiteClass.search_api(keyword)
                    elif mode == 'info_api':
                        ret = SiteClass.info_api(keyword)
                return jsonify(ret)
        except Exception as e: 
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})
        


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
        elif sub == 'episode_info':
            return jsonify(self.episode_info(req.args.get('code')))
            #return jsonify(self.episode_info(req.args.get('code'), req.args.get('no'), req.args.get('premiered'), req.args.get('param')))
            #return jsonify(self.episode_info(req.args.get('code'), req.args.get('no'), py_urllib.unquote(req.args.get('param'))))

    #########################################################

    def search(self, keyword, year=None, manual=False):
        keyword = keyword.split(u'시즌')[0]
        logger.debug('FTV [%s] [%s] [%s]', keyword, year, manual)
        en_keyword = None   
        if SiteUtil.is_hangul(keyword):
            watcha_ret = SiteWatchaTv.search(keyword, year=year)
            if watcha_ret['ret'] == 'success':
                #logger.debug(json.dumps(watcha_ret, indent=4))
                en_keyword = watcha_ret['data'][0]['title_en']
                if en_keyword is None:
                    
                    en_keyword = SiteUtil.trans(keyword, source='ko', target='en')
                
        for key in [en_keyword, keyword]:
            if key is None:
                continue
            ret = SiteTvdbTv.search(key, year=year)
            #logger.debug(ret)
            if ret['ret'] == 'success' and ret['data'][0]['score'] >= 95:
                return ret['data']
            return ret['data']
        
        """
        ret = SiteTvdbTv.search(keyword)
        logger.debug(ret)
        if ret['ret'] == 'success':
            return ret['data']
        
        else:
            if SiteUtil.is_hangul(keyword) == False:
                logger.debug('keyword is not hangul..')
                return []
            watcha_ret = SiteWatchaTv.search(keyword)
            if watcha_ret['ret'] == 'success':
                new_keyword = watcha_ret['data'][0]['title_en']
                if new_keyword != '':
                    logger.debug('new keyword is %s', new_keyword)
                    ret = SiteTvdbTv.search(new_keyword)
                    if ret['ret'] == 'success':
                        return ret['data']
        """



    def info(self, code):
        try:
            tvdb_info = SiteTvdbTv.info(code)
            logger.debug('TVDB [%s]', tvdb_info['title'])
            tmp = re.sub(r'\(\d{4}\)', '', tvdb_info['title']).strip()
            watcha_search = SiteWatchaTv.search(tmp, year=tvdb_info['year'], season_count=len(tvdb_info['seasons'].keys()))
            if watcha_search['ret'] != 'success':
                return tvdb_info

            #logger.debug(watcha_ret['data'][0]['title'])
            #logger.debug(json.dumps(watcha_ret['data'][0]))
            
            title = watcha_search['data'][0]['title'] if tvdb_info['season_count'] == 1 else u'%s 시즌 1' % watcha_search['data'][0]['title']
            daum_search = SiteDaumTv.search(title, year=watcha_search['data'][0]['year'])
            if daum_search['ret'] == 'success':
                if True or tvdb_info['studio'].lower().find(daum_search['data']['studio'].lower()) != -1:
                    tvdb_info['title'] = watcha_search['data'][0]['title']
                    #logger.debug(tvdb_info['season_count'])
                    daum_actor_list = {}
                    for season_no, season in enumerate(daum_search['data']['series'][:tvdb_info['season_count']]):
                        season_no += 1
                        season_info = SiteDaumTv.info(season['code'], season['title'])
                        if season_info['ret'] == 'success':
                            season_info = season_info['data']
                        #season_info = P.logic.get_module('ktv').info(season['code'], season['title'])
                        if season_no == 1:
                            tvdb_info['plot'] = season_info['plot']
                            for item in season_info['director']:
                                tvdb_info['director'].append(item['name'])
                            for item in season_info['credits']:
                                tvdb_info['writer'].append(item['name']) 
                            tvdb_info['art'] += season_info['thumb']

                        for actor in season_info['actor']:
                            if actor['name'] not in daum_actor_list:
                                daum_actor_list[actor['name']] = actor
                        season['title'] = season_info['title']
                        tvdb_info['extras'] += season_info['extras']
                        #logger.debug(json.dumps(season_info, indent=4))
                        if 'episodes' in season_info['extra_info']:
                            if len(tvdb_info['seasons'][season_no]['episodes'].keys()) != len(season_info['extra_info']['episodes'].keys()):
                                continue

                            for epi_no in season_info['extra_info']['episodes'].keys():
                                if 'wavve' in season_info['extra_info']['episodes'][epi_no]:
                                    tvdb_info['seasons'][season_no]['episodes'][epi_no]['title'] = season_info['extra_info']['episodes'][epi_no]['wavve']['title']
                                    tvdb_info['seasons'][season_no]['episodes'][epi_no]['plot'] = season_info['extra_info']['episodes'][epi_no]['wavve']['plot']
                                    tvdb_info['seasons'][season_no]['episodes'][epi_no]['art'].append(season_info['extra_info']['episodes'][epi_no]['wavve']['thumb'])
                                    tvdb_info['seasons'][season_no]['episodes'][epi_no]['is_title_kor'] = True
                                    tvdb_info['seasons'][season_no]['episodes'][epi_no]['is_plot_kor'] = True
                                elif 'tving' in season_info['extra_info']['episodes'][epi_no]:
                                    tvdb_info['seasons'][season_no]['episodes'][epi_no]['plot'] = season_info['extra_info']['episodes'][epi_no]['tving']['plot']
                                    tvdb_info['seasons'][season_no]['episodes'][epi_no]['art'].append(season_info['extra_info']['episodes'][epi_no]['tving']['thumb'])
                                    tvdb_info['seasons'][season_no]['episodes'][epi_no]['is_plot_kor'] = True
                                elif 'daum' in season_info['extra_info']['episodes'][epi_no]:
                                    episode_info = SiteDaumTv.episode_info(season_info['extra_info']['episodes'][epi_no]['daum']['code'], is_ktv=False)
                                    if episode_info['data']['title'] != '':
                                        tvdb_info['seasons'][season_no]['episodes'][epi_no]['title'] = episode_info['data']['title']
                                        tvdb_info['seasons'][season_no]['episodes'][epi_no]['is_title_kor'] = True
                                    if episode_info['data']['plot'] != '':
                                        tvdb_info['seasons'][season_no]['episodes'][epi_no]['plot'] = episode_info['data']['plot']
                                        tvdb_info['seasons'][season_no]['episodes'][epi_no]['is_plot_kor'] = True
                    # end of each season
                           

                    for key, value in daum_actor_list.items():
                        tmp = SiteDaumTv.get_actor_eng_name(key)
                        if tmp is not None:
                            value['eng_name'] = tmp 
                            logger.debug('[%s] [%s]', key, value['eng_name'])
                        else:
                            value['eng_name'] = None
                            #del daum_actor_list[key]
                            pass
                        

                    for actor in tvdb_info['actor']:
                        actor['is_kor_name'] = False
                        for key, value in daum_actor_list.items():
                            if value['eng_name'] is None:
                                continue
                            for tmp_name in value['eng_name']:
                                if actor['name'].lower().replace(' ', '') == tmp_name.lower().replace(' ', ''):
                                    actor['name'] = value['name']
                                    actor['role'] = value['role']
                                    actor['is_kor_name']= True
                                    del daum_actor_list[key]
                                    break
                            if actor['is_kor_name']:
                                break
                        actor['role'] = actor['role'].replace('&#39;', '\"')
                    for key, value in daum_actor_list.items():
                        logger.debug('22 [%s] [%s]', key, value['eng_name'])
                        value['image'] = value['thumb']
                        del value['eng_name']
                        del value['thumb']
                        tvdb_info['actor'].append(value)
            else:
                try:
                    first = watcha_search['data'][0]
                    if 'seasons' in first and len(first['seasons']) > 0:
                        code = first['seasons'][0]['info']['code']
                    else:
                        code = first['code']
                    data = SiteWatchaTv.info(code)
                    #logger.debug(json.dumps(data, indent=4))
                    if data['ret'] == 'success':
                        tvdb_info['title'] = first['title']
                        watcha_info = data['data']
                        tvdb_info['plot'] = watcha_info['plot']
                except Exception as e: 
                    P.logger.error(f"Exception:{str(e)}")
                    P.logger.error(traceback.format_exc())


            logger.debug(u'FTV 리턴')
            return tvdb_info

            

        except Exception as e: 
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())

    
    
    def episode_info(self, code):
        try:
            logger.debug('code : %s', code)
            if code[1] == 'D':
                from support_site import SiteDaumTv
                data = SiteDaumTv.episode_info(code, include_kakao=ModelSetting.get_bool('ktv_use_kakaotv_episode'))
                if data['ret'] == 'success':
                    return data['data']

        except Exception as e: 
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())
