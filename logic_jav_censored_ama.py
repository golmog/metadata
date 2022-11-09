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
from flask import jsonify, redirect, render_template, request
# sjva 공용
from framework import (SystemModelSetting, app, db, path_data, scheduler,
                       socketio)
from framework.common.util import headers
from framework.util import Util
from lxml import etree as ET
from sqlalchemy import and_, desc, func, not_, or_

from plugin import LogicModuleBase, default_route_socketio

# 패키지
from .plugin import P

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting

from support_site import MetadataServerUtil

#########################################################

class LogicJavCensoredAma(LogicModuleBase):
    db_default = {
        'jav_censored_ama_db_version' : '1',
        
        'jav_censored_ama_jav321_code' : 'ara-464',
        'jav_censored_ama_jav321_use_proxy' : 'False',
        'jav_censored_ama_jav321_proxy_url' : '',
        'jav_censored_ama_jav321_image_mode' : '3',

        'jav_censored_ama_order' : 'mgstage, jav321, r18',
        'jav_censored_ama_title_format' : '[{title}] {tagline}',
        'jav_censored_ama_tag_option' : '2',
        'jav_censored_ama_use_extras' : 'True',
    }

    def __init__(self, P):
        super(LogicJavCensoredAma, self).__init__(P, 'setting')
        self.name = 'jav_censored_ama'

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        arg['sub'] = self.name
        return render_template('{package_name}_{module_name}_{sub}.html'.format(package_name=P.package_name, module_name=self.name, sub=sub), arg=arg)
       
    def process_ajax(self, sub, req):
        try:
            if sub == 'test':
                code = req.form['code']
                call = req.form['call']
                if call == 'jav321':
                    from support_site import SiteJav321
                    ModelSetting.set('jav_censored_ama_jav321_code', code)
                    ret = {}
                    ret['search'] = SiteJav321.search(code, proxy_url=ModelSetting.get('jav_censored_ama_jav321_proxy_url') if ModelSetting.get_bool('jav_censored_ama_jav321_use_proxy') else None, image_mode=ModelSetting.get('jav_censored_ama_jav321_image_mode'), manual=False)
                    if ret['search']['ret'] == 'success':
                        if len(ret['search']['ret']) > 0:
                            ret['info'] = self.info(ret['search']['data'][0]['code'])
                return jsonify(ret)
        except Exception as e: 
            P.logger.error('Exception:%s', e)
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})

    def process_api(self, sub, req):
        if sub == 'search':
            return jsonify(self.search(req.args.get('keyword'), bool(req.args.get('manual'))))
        elif sub == 'info':
            return jsonify(self.info(req.args.get('code')))

    
    #########################################################


    def search(self, keyword, manual=False):
        ret = []
        site_list = ModelSetting.get_list('jav_censored_ama_order', ',')
        for idx, site in enumerate(site_list):
            #logger.debug(site)
            SiteClass = None
            if site == 'jav321':
                from support_site import SiteJav321 as SiteClass

            if SiteClass is None:
                continue

            data = SiteClass.search(
                keyword, 
                proxy_url=ModelSetting.get('jav_censored_ama_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_censored_ama_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
                image_mode=ModelSetting.get('jav_censored_ama_{site_name}_image_mode'.format(site_name=SiteClass.site_name)))
            if data['ret'] == 'success':
                for item in data['data']:
                    item['score'] -= idx
                ret += data['data']
                ret = sorted(ret, key=lambda k: k['score'], reverse=True)  
            if manual:
                continue
            else:
                if len(ret) > 0 and ret[0]['score'] > 95:
                    break
        return ret
    

    def info(self, code):
        #logger.debug(code)
        ret = None
        if ModelSetting.get_bool('jav_censored_use_sjva'):
            ret = MetadataServerUtil.get_metadata(code)
        if ret is None:
            if code[1] == 'T':
                from support_site import SiteJav321
                ret = self.info2(code, SiteJav321)
            elif code[1] == 'D':
                from support_site import SiteDmm
                ret = self.info2(code, SiteDmm)
        
        if ret is not None:
            ret['plex_is_proxy_preview'] = True #ModelSetting.get_bool('jav_censored_plex_is_proxy_preview')
            ret['plex_is_landscape_to_art'] = True #ModelSetting.get_bool('jav_censored_plex_landscape_to_art')
            ret['plex_art_count'] = ModelSetting.get_int('jav_censored_art_count')
            if ret['plex_art_count'] == 0 and len(ret['thumb']) == 1:
                ret['plex_art_count'] = 1
            art_count = ModelSetting.get_int('jav_censored_art_count')                
            ret['fanart'] = ret['fanart'][:art_count]
            if ret['actor'] is not None:
                for item in ret['actor']:
                    #self.process_actor(item)
                    item['name'] = item['originalname']
               
            ret['title'] = ModelSetting.get('jav_censored_ama_title_format').format(
                originaltitle=ret['originaltitle'], 
                plot=ret['plot'],
                title=ret['title'],
                sorttitle=ret['sorttitle'],
                runtime=ret['runtime'],
                country=ret['country'],
                premiered=ret['premiered'],
                year=ret['year'],
                actor=ret['actor'][0]['name'] if ret['actor'] is not None and len(ret['actor']) > 0 else '',
                tagline=ret['tagline'] if ret['tagline'] is not None else '',
            )
            if 'tag' in ret:
                tag_option = ModelSetting.get('jav_censored_ama_tag_option')
                if tag_option == '0':
                    ret['tag'] = []
                elif tag_option == '1':
                    ret['tag'] = [ret['originaltitle'].split('-')[0]]
                elif tag_option == '3':
                    tmp = []
                    if ret['tag'] is not None:
                        for _ in ret['tag']:
                            if _ != ret['originaltitle'].split('-')[0]:
                                tmp.append(_)
                    ret['tag'] = tmp
            if ModelSetting.get_bool('jav_censored_ama_use_extras') == False:
                ret['extras'] = []
            return ret

    def info2(self, code, SiteClass):
        ret = None
        #logger.debug('ama info2:%s %s', code, SiteClass)
        image_mode = ModelSetting.get('jav_censored_ama_{site_name}_image_mode'.format(site_name=SiteClass.site_name))
        data = SiteClass.info(
            code,
            proxy_url=ModelSetting.get('jav_censored_ama_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_censored_ama_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
            image_mode=image_mode)
        #logger.debug(json.dumps(data, indent=4))

        if data['ret'] == 'success':
            ret = data['data']
            if ModelSetting.get_bool('jav_censored_use_sjva') and image_mode == '3' and SystemModelSetting.get('trans_type') in ['1', '3'] and SystemModelSetting.get('trans_google_api_key') != '':
                MetadataServerUtil.set_metadata_jav_censored(code, ret, ret['title'].lower())
        return ret

