# -*- coding: utf-8 -*-
#########################################################
# python
import datetime
import json
import os
import re
import sys
import time
import traceback

# third-party
import requests
from flask import jsonify, redirect, render_template, request
# sjva
from framework import (SystemModelSetting, app, db, path_data, scheduler,
                       socketio)
from support_site import (Site1PondoTv, Site10Musume, SiteCarib, SiteHeyzo,
                          SiteUtil)
from support_site.server_util import MetadataServerUtil
from tool_base import ToolBaseNotify, ToolUtil

from plugin import LogicModuleBase

#########################################################
from .plugin import P

module_name = 'jav_uncensored'
logger = P.logger
ModelSetting = P.ModelSetting
package_name = P.package_name

class LogicJavUncensored(LogicModuleBase):
    db_default = {
        f'{module_name}_db_version' : '1',
        f'{module_name}_use_sjva' : 'False',
        f'{module_name}_image_mode' : '0',
        f'{module_name}_title_format' : '[{title}] {tagline}',
        f'{module_name}_use_extras' : 'False',

        f'{module_name}_1pondo_use_proxy' : 'False',
        f'{module_name}_1pondo_proxy_url' : '',
        f'{module_name}_1pondo_code' : '092121_001',
        f'{module_name}_10musume_use_proxy' : 'False',
        f'{module_name}_10musume_proxy_url' : '',
        f'{module_name}_10musume_code' : '010620_01',
        f'{module_name}_heyzo_use_proxy' : 'False',
        f'{module_name}_heyzo_proxy_url' : '',
        f'{module_name}_heyzo_code' : 'HEYZO-2681',
        f'{module_name}_carib_use_proxy' : 'False',
        f'{module_name}_carib_proxy_url' : '',
        f'{module_name}_carib_code' : '062015-904',

    }

    module_map = {'1pondo': Site1PondoTv, '10musume': Site10Musume, 'heyzo': SiteHeyzo, 'carib': SiteCarib}

    def __init__(self, P):
        super(LogicJavUncensored, self).__init__(P, 'setting')
        self.name = module_name
        
    def process_menu(self, sub, req):
        arg = ModelSetting.to_dict()
        arg['sub'] = self.name
        try:
            return render_template(f"{package_name}_{module_name}_{sub}.html", arg=arg)
        except Exception as exception:
            logger.error('Exception:%s', exception)
            logger.error(traceback.format_exc())
            return render_template('sample.html', title=f"{package_name} - {module_name} - {sub}")


    def process_ajax(self, sub, req):
        try:
            if sub == 'test':
                code = req.form['code']
                call = req.form['call']
                ModelSetting.set('jav_uncensored_%s_code' % (call), code)

                SiteClass = self.module_map[call]
                ret = {}
                ret['search'] = SiteClass.search(code, 
                    proxy_url=ModelSetting.get('jav_uncensored_%s_proxy_url' % call) if ModelSetting.get_bool('jav_uncensored_%s_use_proxy' % call) else None, 
                    image_mode=ModelSetting.get('jav_uncensored_image_mode'), manual=False)
                if ret['search']['ret'] == 'success':
                    if len(ret['search']['data']) > 0:
                        ret['info'] = self.info(ret['search']['data'][0]['code'])
                return jsonify(ret)

        except Exception as e:
            logger.error('Exception:%s', e)
            logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})

    
    def process_api(self, sub, req):
        try:
            if sub == 'search':
                call = req.args.get('call')
                if call == 'plex' or call == 'kodi':
                    manual = (req.args.get('manual') == 'True')
                    #manual = bool(req.args.get('manual'))
                    return jsonify(self.search(req.args.get('keyword'), manual=manual))
            elif sub == 'info':
                call = req.args.get('call')
                data = self.info(req.args.get('code'))
                if call == 'kodi':
                    data = SiteUtil.info_to_kodi(data)
                return jsonify(data)
        
        except Exception as e: 
            logger.error('Exception:%s', e)
            logger.error(traceback.format_exc())


    ################################################################

    def search(self, keyword, manual=False):
        logger.debug('uncensored search - keyword:[%s] manual:[%s]', keyword, manual)
        do_trans = manual
        ret = []
        site_list = list(self.module_map)

        # 키워드에서 제작사 판별, 판별 불가시 전체 검색
        # 각 site_ 에서는 품번만 추출해냄, 여기에서 제작사 판별 필요
        
        if '1pon' in keyword.lower():
            from support_site import \
                Site1PondoTv as SiteClass

            data = SiteClass.search(
                keyword, 
                do_trans=do_trans,
                proxy_url=ModelSetting.get('jav_uncensored_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_uncensored_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
                image_mode=ModelSetting.get('jav_uncensored_image_mode'),manual=manual)

            if data['ret'] == 'success' and len(data['data']) > 0:
                ret += data['data']

        elif '10mu' in keyword.lower():
            from support_site import \
                Site10Musume as SiteClass

            data = SiteClass.search(
                keyword, 
                do_trans=do_trans,
                proxy_url=ModelSetting.get('jav_uncensored_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_uncensored_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
                image_mode=ModelSetting.get('jav_uncensored_image_mode'),manual=manual)

            if data['ret'] == 'success' and len(data['data']) > 0:
                ret += data['data']

        elif 'heyzo' in keyword.lower():
            from support_site import \
                SiteHeyzo as SiteClass

            data = SiteClass.search(
                keyword, 
                do_trans=do_trans,
                proxy_url=ModelSetting.get('jav_uncensored_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_uncensored_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
                image_mode=ModelSetting.get('jav_uncensored_image_mode'),manual=manual)

            if data['ret'] == 'success' and len(data['data']) > 0:
                ret += data['data']

        elif 'carib' in keyword.lower():
            from support_site import \
                SiteCarib as SiteClass

            data = SiteClass.search(
                keyword, 
                do_trans=do_trans,
                proxy_url=ModelSetting.get('jav_uncensored_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_uncensored_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
                image_mode=ModelSetting.get('jav_uncensored_image_mode'),manual=manual)

            if data['ret'] == 'success' and len(data['data']) > 0:
                ret += data['data']
                

        else:
            for idx, site in enumerate(site_list):
                if site == '1pondo':
                    from support_site import \
                        Site1PondoTv as SiteClass
                elif site == '10musume':
                    from support_site import \
                        Site10Musume as SiteClass
                elif site == 'heyzo':
                    from support_site import \
                        SiteHeyzo as SiteClass
                elif site == 'carib':
                    from support_site import \
                        SiteCarib as SiteClass

                data = SiteClass.search(
                keyword, 
                do_trans=do_trans,
                proxy_url=ModelSetting.get('jav_uncensored_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_uncensored_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
                image_mode=ModelSetting.get('jav_uncensored_image_mode'),manual=manual)

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
        if ModelSetting.get_bool('jav_uncensored_use_sjva'):
            ret = MetadataServerUtil.get_metadata(code)
            if ret is not None:
                logger.debug('Get meda info by server : %s', code)
        if ret is None:
            if code[1] == 'D':
                from support_site import Site1PondoTv
                ret = self.info2(code, Site1PondoTv)
            elif code[1] == 'M':
                from support_site import Site10Musume
                ret = self.info2(code, Site10Musume)
            elif code[1] == 'H':
                from support_site import SiteHeyzo
                ret = self.info2(code, SiteHeyzo)
            elif code[1] == 'C':
                from support_site import SiteCarib
                ret = self.info2(code, SiteCarib)
            
        
        if ret is not None:

            if ret['actor'] is not None:
                for item in ret['actor']:
                    # self.get_actor_from_server(item) # actor 정보, avdbs 차단 때문에 직접 메타서버로 요청
                    self.process_actor(item)

            ret['title'] = ModelSetting.get('jav_uncensored_title_format').format(
                originaltitle=ret['originaltitle'], 
                plot=ret['plot'],
                title=ret['title'],
                sorttitle=ret['sorttitle'],
                country=ret['country'],
                premiered=ret['premiered'],
                year=ret['year'],
                actor=ret['actor'][0]['name'] if ret['actor'] is not None and len(ret['actor']) > 0 else '',
                tagline=ret['tagline']
            )

            if ModelSetting.get_bool('jav_uncensored_use_extras') == False:
                ret['extras'] = []

            return ret

    def info2(self, code, SiteClass):
        image_mode = ModelSetting.get('jav_uncensored_image_mode')
        data = SiteClass.info(
            code,
            proxy_url=ModelSetting.get(f'jav_uncensored_{SiteClass.site_name}_proxy_url') if ModelSetting.get_bool(f'jav_uncensored_{SiteClass.site_name}_use_proxy') else None, 
            image_mode=image_mode)


        if data['ret'] == 'success':
            ret = data['data']
            logger.debug(ret)
            if ModelSetting.get_bool('jav_uncensored_use_sjva') and image_mode == '3' and (SystemModelSetting.get('trans_type') == '4' or (SystemModelSetting.get('trans_type') == '1' and SystemModelSetting.get('trans_google_api_key') != '')):
                MetadataServerUtil.set_metadata_jav_uncensored(code, ret, ret['title'].lower())
        else:
            logger.debug('info2 fail..')
            ret = None
        return ret

    def get_actor_from_server(self, entity_actor):
        for site_char in ['A', 'H']:
            code = f'A{site_char}{entity_actor["originalname"]}'
            data = MetadataServerUtil.get_metadata(code)
            logger.debug(data)
            if data is not None and data['name'] is not None and data['name'] != '' and data['name'] != data['originalname'] and data['thumb'] is not None and data['thumb'].find('discordapp.net') != -1:
                logger.info('Get actor info by server : %s %s', entity_actor['originalname'], site_char)
                entity_actor['name'] = data['name']
                entity_actor['name2'] = data['name2']
                entity_actor['thumb'] = data['thumb']
                entity_actor['site'] = data['site']
                return

        if entity_actor['name'] is None or entity_actor['name'] == '':
            entity_actor['name'] = entity_actor['originalname'] 
    
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


