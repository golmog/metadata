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

from support_site import (MetadataServerUtil, SiteDmm, SiteJavbus,
                          SiteMgstageDvd, SiteUtil)

#########################################################

class LogicJavCensored(LogicModuleBase):
    db_default = {
        'jav_censored_db_version' : '1',
        'jav_censored_use_sjva' : 'False',
        'jav_censored_order' : 'dmm, javbus',
        'jav_censored_title_format' : '[{title}] {tagline}',
        'jav_censored_art_count' : '0',
        'jav_censored_tag_option' : '2',
        #'jav_censored_plex_is_proxy_preview' : 'True',
        #'jav_censored_plex_landscape_to_art' : 'True',
        
        'jav_censored_actor_order' : 'avdbs, hentaku',

        'jav_censored_avdbs_use_proxy' : 'False',
        'jav_censored_avdbs_proxy_url' : '',

        'jav_censored_javbus_code' : 'ssni-900',
        'jav_censored_javbus_use_proxy' : 'False',
        'jav_censored_javbus_proxy_url' : '',
        'jav_censored_javbus_image_mode' : '0',

        'jav_censored_dmm_code' : 'ssni-900',
        'jav_censored_dmm_use_proxy' : 'False',
        'jav_censored_dmm_proxy_url' : '',
        'jav_censored_dmm_image_mode' : '0',
        'jav_censored_actor_test_name' : '',
        'jav_censored_dmm_small_image_to_poster' : '',

        'jav_censored_mgs_code' : 'abw-073',
        'jav_censored_mgs_use_proxy' : 'False',
        'jav_censored_mgs_proxy_url' : '',
        'jav_censored_mgs_image_mode' : '0',
        'jav_censored_mgs_attach_number' : '',

        'jav_censored_use_extras' : 'True',

    }
    
    module_map = {'javbus':SiteJavbus, 'dmm':SiteDmm, 'mgs':SiteMgstageDvd}


    def __init__(self, P):
        super(LogicJavCensored, self).__init__(P, 'setting')
        self.name = 'jav_censored'

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        arg['sub'] = self.name
        #if sub in ['setting']:
        try:
            return render_template('{package_name}_{module_name}_{sub}.html'.format(package_name=P.package_name, module_name=self.name, sub=sub), arg=arg)
        except Exception as e: 
            P.logger.error('Exception:%s', e)
            P.logger.error(traceback.format_exc())
            return render_template('sample.html', title='%s - %s' % (P.package_name, sub))

    def process_ajax(self, sub, req):
        try:
            if sub == 'test':
                code = req.form['code']
                call = req.form['call']
                ModelSetting.set('jav_censored_%s_code' % (call), code)

                SiteClass = self.module_map[call]
                ret = {}
                ret['search'] = SiteClass.search(code, 
                    proxy_url=ModelSetting.get('jav_censored_%s_proxy_url' % call) if ModelSetting.get_bool('jav_censored_%s_use_proxy' % call) else None, 
                    image_mode=ModelSetting.get('jav_censored_%s_image_mode' % call), manual=False)
                if ret['search']['ret'] == 'success':
                    if len(ret['search']['data']) > 0:
                        ret['info'] = self.info(ret['search']['data'][0]['code'])
                return jsonify(ret)
            elif sub == 'actor_test':
                ModelSetting.set('jav_censored_actor_test_name', req.form['name'])
                entity_actor = {'originalname' : req.form['name']}
                call = req.form['call']
                if call == 'avdbs':
                    from support_site import SiteAvdbs
                    self.process_actor2(entity_actor, SiteAvdbs, ModelSetting.get('jav_censored_avdbs_proxy_url') if ModelSetting.get_bool('jav_censored_avdbs_use_proxy') else None)
                elif call == 'hentaku':
                    from support_site import SiteHentaku
                    self.process_actor2(entity_actor, SiteHentaku, None)
                return jsonify(entity_actor)
            
        except Exception as e: 
            P.logger.error('Exception:%s', e)
            P.logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})

    def process_api(self, sub, req):
        if sub == 'search':
            call = req.args.get('call')
            if call == 'plex' or call == 'kodi':
                manual = (req.args.get('manual') == 'True')
                #manual = bool(req.args.get('manual'))
                return jsonify(self.search(req.args.get('keyword').rstrip('-').strip(), manual=manual))
        elif sub == 'info':
            call = req.args.get('call')
            data = self.info(req.args.get('code'))
            if call == 'kodi':
                data = SiteUtil.info_to_kodi(data)
            return jsonify(data)

    def process_normal(self, sub, req):
        if sub == 'nfo_download':
            #code = req.form['code']
            code = req.args.get('code')
            #call = req.form['call']
            call = req.args.get('call')
            if call == 'dmm':
                from support_site import SiteDmm
                ModelSetting.set('jav_censored_dmm_code', code)
                ret = {}
                ret['search'] = SiteDmm.search(code, proxy_url=ModelSetting.get('jav_censored_dmm_proxy_url') if ModelSetting.get_bool('jav_censored_dmm_use_proxy') else None, image_mode=ModelSetting.get('jav_censored_dmm_image_mode'))
                if ret['search']['ret'] == 'success':
                    if len(ret['search']['data']) > 0:
                        ret['info'] = self.info(ret['search']['data'][0]['code'])
                        from support_site import UtilNfo
                        return UtilNfo.make_nfo_movie(ret['info'], output='file', filename='%s.nfo' % ret['info']['originaltitle'].upper())

    #########################################################


    def search(self, keyword, manual=False):
        logger.debug('dvd search - keyword:[%s] manual:[%s]', keyword, manual)
        do_trans = manual
        ret = []
        site_list = ModelSetting.get_list('jav_censored_order', ',')
        for idx, site in enumerate(site_list):
            if site == 'javbus':
                from support_site import SiteJavbus as SiteClass
            elif site == 'dmm':
                from support_site import SiteDmm as SiteClass

            data = SiteClass.search(
                keyword, 
                do_trans=do_trans,
                proxy_url=ModelSetting.get('jav_censored_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_censored_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
                image_mode=ModelSetting.get('jav_censored_{site_name}_image_mode'.format(site_name=SiteClass.site_name)),manual=manual)
            if data['ret'] == 'success' and len(data['data']) > 0:
                if idx != 0:
                    for item in data['data']:
                        item['score'] += -1
                ret += data['data']
                ret = sorted(ret, key=lambda k: k['score'], reverse=True)  
            if manual:
                continue
            else:
                if len(ret) > 0 and ret[0]['score'] > 95:
                    break
        return ret
    

    def info(self, code):
        ret = None
        if ModelSetting.get_bool('jav_censored_use_sjva'):
            ret = MetadataServerUtil.get_metadata(code)
            if ret is not None:
                logger.debug('Get meda info by server : %s', code)
        if ret is None:
            if code[1] == 'B':
                from support_site import SiteJavbus
                ret = self.info2(code, SiteJavbus)
            elif code[1] == 'D':
                from support_site import SiteDmm
                ret = self.info2(code, SiteDmm)
        
        if ret is not None:
            ret['plex_is_proxy_preview'] = True #ModelSetting.get_bool('jav_censored_plex_is_proxy_preview')
            ret['plex_is_landscape_to_art'] = True #ModelSetting.get_bool('jav_censored_plex_landscape_to_art')
            ret['plex_art_count'] = ModelSetting.get_int('jav_censored_art_count')
            art_count = ModelSetting.get_int('jav_censored_art_count')
            ret['fanart'] = ret['fanart'][:art_count]
            
            if ret['actor'] is not None:
                for item in ret['actor']:
                    self.process_actor(item)

            ret['title'] = ModelSetting.get('jav_censored_title_format').format(
                originaltitle=ret['originaltitle'], 
                plot=ret['plot'],
                title=ret['title'],
                sorttitle=ret['sorttitle'],
                runtime=ret['runtime'],
                country=ret['country'],
                premiered=ret['premiered'],
                year=ret['year'],
                actor=ret['actor'][0]['name'] if ret['actor'] is not None and len(ret['actor']) > 0 else '',
                tagline=ret['tagline']
            )

            if 'tag' in ret:
                tag_option = ModelSetting.get('jav_censored_tag_option')
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

            if ModelSetting.get_bool('jav_censored_use_extras') == False:
                ret['extras'] = []
            return ret

    def info2(self, code, SiteClass):
        image_mode = ModelSetting.get('jav_censored_{site_name}_image_mode'.format(site_name=SiteClass.site_name))
        if SiteClass.site_name == 'dmm':
            data = SiteClass.info(
                code,
                proxy_url=ModelSetting.get('jav_censored_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_censored_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
                image_mode=image_mode, small_image_to_poster_list=ModelSetting.get_list('jav_censored_dmm_small_image_to_poster', ','))
            
        else:
            data = SiteClass.info(
            code,
            proxy_url=ModelSetting.get('jav_censored_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_censored_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
            image_mode=image_mode)

        if data['ret'] == 'success':
            ret = data['data']
            if ModelSetting.get_bool('jav_censored_use_sjva') and image_mode == '3' and SystemModelSetting.get('trans_type') in ['1', '3'] and SystemModelSetting.get('trans_google_api_key') != '':
                MetadataServerUtil.set_metadata_jav_censored(code, ret, ret['title'].lower())
        else:
            logger.debug('info2 fail..')
            ret = None
        return ret

    def process_actor(self, entity_actor):
        actor_site_list = ModelSetting.get_list('jav_censored_actor_order', ',')
        #logger.debug('actor_site_list : %s', actor_site_list)
        for site in actor_site_list:
            if site == 'hentaku':
                from support_site import SiteHentaku
                self.process_actor2(entity_actor, SiteHentaku, None)
            elif site == 'avdbs':
                from support_site import SiteAvdbs
                self.process_actor2(entity_actor, SiteAvdbs, ModelSetting.get('jav_censored_avdbs_proxy_url') if ModelSetting.get_bool('jav_censored_avdbs_use_proxy') else None)
            if entity_actor['name'] is not None and entity_actor['name'] != '':
                return
        if entity_actor['name'] is None or entity_actor['name'] == '':
            entity_actor['name'] = entity_actor['originalname'] 


    def process_actor2(self, entity_actor, SiteClass, proxy_url):
        
        if ModelSetting.get_bool('jav_censored_use_sjva'):
            #logger.debug('A' + SiteClass.site_char + entity_actor['originalname'])
            code = u'A%s%s' % (SiteClass.site_char, entity_actor['originalname'])
            data = MetadataServerUtil.get_metadata(code)
            if data is not None and data['name'] is not None and data['name'] != '' and data['name'] != data['originalname'] and data['thumb'] is not None and data['thumb'].find('discordapp.net') != -1:
                logger.info('Get actor info by server : %s %s', entity_actor['originalname'], SiteClass)
                entity_actor['name'] = data['name']
                entity_actor['name2'] = data['name2']
                entity_actor['thumb'] = data['thumb']
                entity_actor['site'] = data['site']
                return
        #logger.debug('Get actor... :%s', SiteClass)
        SiteClass.get_actor_info(entity_actor, proxy_url=proxy_url)
        #logger.debug(entity_actor)
        if ModelSetting.get_bool('jav_censored_use_sjva'):
            if 'name' in entity_actor and entity_actor['name'] is not None and entity_actor['name'] != '' and 'thumb' in entity_actor and entity_actor['thumb'] is not None and entity_actor['thumb'].find('.discordapp.') != -1:
                MetadataServerUtil.set_metadata('A'+ SiteClass.site_char + entity_actor['originalname'], entity_actor, entity_actor['originalname'])
                return
        

