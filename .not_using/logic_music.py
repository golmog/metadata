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

# 패키지
from support_site import SiteUtil, SiteVibe

from .plugin import P

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting

from support_site import MetadataServerUtil

#########################################################

class LogicMusic(LogicModuleBase):
    db_default = {
        'music_db_version' : '1',
        'music_test_artist_search' : '',
        'music_test_artist_info' : '',

        'music_test_album_search' : '',
        'music_test_album_info' : '',

    }

    def __init__(self, P):
        super(LogicMusic, self).__init__(P, 'test')
        self.name = 'music'

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        arg['sub'] = self.name
        arg['sub2'] = sub
        try:
            return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)
        except:
            return render_template('sample.html', title=f'{P.package_name}/{self.name}/{sub}')

    def process_ajax(self, sub, req):
        try:
            if sub == 'command':
                command = req.form['command'].strip()
                if command == 'test':
                    arg1 = req.form['arg1'].strip()
                    keyword = req.form['arg2'].strip()
                    logger.debug(f"[{command}] [{arg1}] [{keyword}]")
                    what, site, mode, return_format = arg1.split('|')
                    ret = {'ret':'success', 'modal':None}
                    if what == 'artist':
                        if mode == 'search':
                            ModelSetting.set('music_test_artist_search', keyword)
                            ret['json'] = self.search_artist(keyword, return_format)
                        elif mode == 'info':
                            ModelSetting.set('music_test_artist_info', keyword)
                            ret['json'] = self.info_artist(keyword, return_format)
                    elif what == 'album':
                        if mode == 'search':
                            ModelSetting.set('music_test_album_search', keyword)
                            tmp = keyword.split('|')
                            if len(tmp) == 1:
                                ret['json'] = self.album_search(None, keyword, return_format)
                            elif len(tmp) == 2:
                                ret['json'] = self.album_search(tmp[0], tmp[1], return_format)
                        elif mode == 'info':
                            ModelSetting.set('music_test_album_info', keyword)
                            ret['json'] = self.album_info(keyword, json_mode)
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
            data = self.info(req.args.get('code'), req.args.get('title'))
            if call == 'kodi':
                data = SiteUtil.info_to_kodi(data)
            return jsonify(data)
        elif sub == 'episode_info':
            return jsonify(self.episode_info(req.args.get('code')))
            #return jsonify(self.episode_info(req.args.get('code'), req.args.get('no'), req.args.get('premiered'), req.args.get('param')))
            #return jsonify(self.episode_info(req.args.get('code'), req.args.get('no'), py_urllib.unquote(req.args.get('param'))))

    #########################################################

    def search_artist(self, keyword, return_format):
        data = SiteVibe.search_artist(keyword, return_format)
        return data
    
    def info_artist(self, keyword, return_format):
        data = SiteVibe.info_artist(keyword, return_format)
        return data

    


    def album_search(self, artist, album, mode):
        data = SiteVibe.search_artist(keyword, mode)
        return data



    def info(self, code, title):
        try:
            show = None
            if code[1] == 'D':
                tmp = SiteDaumTv.info(code, title)
                if tmp['ret'] == 'success':
                    show = tmp['data']

                if 'kakao_id' in show['extra_info'] and show['extra_info']['kakao_id'] is not None and ModelSetting.get_bool('ktv_use_kakaotv'):
                    show['extras'] = SiteDaumTv.get_kakao_video(show['extra_info']['kakao_id'])

                if ModelSetting.get_bool('ktv_use_tmdb'):
                    from support_site import SiteTmdbTv
                    tmdb_id = SiteTmdbTv.search(show['title'], show['premiered'])
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
                if ModelSetting.get_bool('ktv_use_theme'):
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

            
            return show

        except Exception as e: 
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())

    
    
    def episode_info(self, code):
        try:
            if code[1] == 'D':
                from support_site import SiteDaumTv
                data = SiteDaumTv.episode_info(code, include_kakao=ModelSetting.get_bool('ktv_use_kakaotv_episode'))
                if data['ret'] == 'success':
                    return data['data']

        except Exception as e: 
            P.logger.error(f"Exception:{str(e)}")
            P.logger.error(traceback.format_exc())
