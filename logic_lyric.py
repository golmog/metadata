# -*- coding: utf-8 -*-
#########################################################
# python
import os, sys, traceback, re, json, threading, time, shutil
from datetime import datetime
# third-party
import requests
# third-party
from flask import request, render_template, jsonify, redirect, Response, send_file
from sqlalchemy import or_, and_, func, not_, desc
import lxml.html
from lxml import etree as ET


# sjva 공용
from framework import db, scheduler, path_data, socketio, SystemModelSetting, app, py_urllib
from framework.util import Util
from framework.common.util import headers
from plugin import LogicModuleBase, default_route_socketio
from system import SystemLogicTrans
from tool_base import d

from .plugin import P
logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting
name = 'lyric'
#########################################################

class LogicLyric(LogicModuleBase):
    db_default = {
        f'{name}_db_version' : '1',
    }

    def __init__(self, P):
        super(LogicLyric, self).__init__(P, 'test')
        self.name = name

    """
    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        arg['sub'] = self.name
        try:
            return render_template(f'{package_name}_{self.name}_{sub}.html', arg=arg)
        except Exception as exception: 
            logger.error(f'Exception : {exception}')
            logger.error(traceback.format_exc()) 
            return render_template('sample.html', title=f'{P.package_nam}/{self.name}/{sub}')


    def process_ajax(self, sub, req):
        try:
            ret = {}
            if sub == 'test':
                return jsonify(ret)
        except Exception as e: 
            P.logger.error('Exception:%s', e)
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})
    """
    def process_api(self, sub, req):
        if sub == 'get_lyric':
            ret = self.get_lyric(req.args.get('mode'), req.args.get('artist'), req.args.get('track'), req.args.get('filename'))
            return jsonify(ret)
            
    #########################################################

    def get_lyric(self, mode, artist, track_title, filename):
        try:
            logger.warning(f"{artist} - {track_title} - {filename}")
            ret = {}
            url = f"https://apis.naver.com/vibeWeb/musicapiweb/v4/searchall?query={py_urllib.quote(artist.replace('&', ','))}%20{py_urllib.quote(track_title)}"
            data = requests.get(url, headers={'accept' : 'application/json'}).json()
            #logger.warning(url)
            tracks = data['response']['result']['trackResult']['tracks']
            logger.warning(self.dump(tracks))
            track = None
            for item in tracks:

                if item['trackTitle'].replace(' ', '').strip() == track_title.replace(' ', '').strip():
                    track = item
                    break
            if track is None:
                logger.warning('정확히 일치하는 제목이 없음')
                track = track[0]
            #logger.warning(d(track))
            if track['hasLyric']:
                url = f"https://apis.naver.com/vibeWeb/musicapiweb/track/{track['trackId']}/info"
                tmp = requests.get(url, headers={'accept' : 'application/json'}).json()
                #logger.warning(self.dump(tmp))
                
                track_data = tmp['response']['result']['trackInformation']
                info= [
                    f"작사  {','.join([x['lyricWriterName'] for x in track_data['lyricWriters']])}",
                    f"작곡  {','.join([x['composerName'] for x in track_data['composers']])}",
                    f"편곡  {','.join([x['arrangerName'] for x in track_data['arrangers']])}", ""]
                if mode == 'lrc' and track_data['hasSyncLyric'] == 'Y':
                    ret['ret'] = 'success'
                    ret['data'] = '\n\n'.join([f"[00:00:01]{x}" for x in info]) + "\n\n" + self.change_to_lrc(track_data['syncLyric'])
                elif mode == 'txt' and track_data['hasLyric'] == 'Y':
                    ret['ret'] = 'success'
                    ret['data'] = '\n'.join([x for x in info]) + "\n" + track_data['lyric']
                else:
                    ret['ret'] = 'fail'
                    ret['log'] = '가사가 없습니다.'
            else:
                logger.warning("track['hasLyric'] is false!!")
                ret['ret'] = 'fail'
                ret['log'] = '가사가 없습니다.'
            logger.debug(f"get_lyric return is {ret['ret']}")
        except Exception as exception:
            logger.debug('Exception:%s', exception)
            logger.debug(traceback.format_exc())
            ret['ret'] = 'fail'
            if mode == 'lrc':
                ret['log'] = f'[00:00:01]에러가 발생했습니다.\n[00:00:02]{exception}'
            else:
                ret['log'] = f'에러가 발생했습니다.\n{exception}'
        return ret

    def change_to_lrc(self, data):
        def tt(t1):
            tmps = t1.split('.')
            t1 = int(tmps[0])
            t3 = '00' if len(tmps) == 1 else tmps[1].zfill(2)
            return f"[{str(int(t1/60)).zfill(2)}:{str(t1%60).zfill(2)}:{t3}]"

        ret = ''
        for line in data.split('#'):
            tmp = line.split('|')
            if len(tmp) == 2 and not tmp[0].startswith('@'):
                ret += f"{tt(tmp[0])}{tmp[1]}\n\n"

        return ret
