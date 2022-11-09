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

import lxml
# third-party
import requests
from flask import jsonify, redirect, render_template, request
# sjva
from framework import (SystemModelSetting, app, db, path_data, scheduler,
                       socketio)
from support_site import MetadataServerUtil
from tool_base import ToolBaseNotify, ToolUtil

from plugin import LogicModuleBase

#########################################################
from .plugin import P

module_name = 'jav_fc2'
logger = P.logger
ModelSetting = P.ModelSetting
package_name = P.package_name

class LogicJavFc2(LogicModuleBase):
    db_default = {
        f'{module_name}_db_version' : '1',
        f'{module_name}_use_sjva' : 'False',
        f'{module_name}_order' : 'fc2com, msin, bp4x, fc2cm, fc2hub, 7mmtv',
        f'{module_name}_title_format' : '[{title}] {tagline}',
        f'{module_name}_use_extras' : 'False',
        f'{module_name}_image_mode' : '3',

        f'{module_name}_fc2com_use_proxy' : 'False',
        f'{module_name}_fc2com_proxy_url' : '',
    
        f'{module_name}_7mmtv_use_proxy' : 'False',
        f'{module_name}_7mmtv_proxy_url' : '',
        f'{module_name}_7mmtv_url' : 'https://bb9711.com',

        f'{module_name}_fc2com_code' : 'FC2-2313436',
        f'{module_name}_msin_code' : 'FC2-2906321',
        f'{module_name}_7mmtv_code' : 'FC2-1032322',
        f'{module_name}_bp4x_code' : 'FC2-2313436',
        f'{module_name}_fc2cm_code' : 'FC2-2313436',
        f'{module_name}_fc2hub_code' : 'FC2-2313436',
        f'{module_name}_total_code' : 'FC2-2313436' 

    }

    def __init__(self, P):
        super(LogicJavFc2, self).__init__(P, 'setting')
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
            from support_site import (Site7mmTv, SiteBp4x, SiteFc2Cm,
                                      SiteFc2Com, SiteFc2Hub, SiteMsin)
            if sub == 'test':
                code = req.form['code']
                call = req.form['call']
                ModelSetting.set('jav_fc2_%s_code' % (call), code)
                ret = {}

                if re.search('FC2[-_\\s]?(?:PPV)?[-_\\s]?(\\d{6,7})-?(?:cd)?(\\d)?', code, re.I):
                    code = re.search('FC2[-_\\s]?(?:PPV)?[-_\\s]?(\\d{6,7})-?(?:cd)?(\\d)?', code, re.I).group(1).lstrip('0')

                if call == 'total':
                    ret = self.search(code, manual=True)

                else:
                    module_map = {'fc2com':SiteFc2Com, 'msin':SiteMsin, 'bp4x':SiteBp4x, 'fc2cm':SiteFc2Cm, 'fc2hub':SiteFc2Hub, '7mmtv':Site7mmTv}
                    SiteClass = module_map[call]
                    ret['search'] = SiteClass.search(code, 
                        proxy_url=ModelSetting.get('jav_fc2_%s_proxy_url' % call) if ModelSetting.get_bool('jav_fc2_%s_use_proxy' % call) else None, 
                        image_mode=ModelSetting.get('jav_fc2_image_mode'), manual = False)
                    if ret['search']['ret'] == 'success':
                        if len(ret['search']['data']) > 0:
                            ret['info'] = self.info(ret['search']['data'][0]['code'])
                
                return jsonify(ret)

            # if sub == 'test2': # 접속테스트 for javdb쿠키
            #     javdb_cookie = req.form['javdb_cookie']
            #     ret = {}
            #     res = SiteJavdb.test_cookie()
            #     if res is True:
            #         ret['result'] = 'success'
            #     else:
            #         ret['result'] = 'failed'
            #     return ret


        except Exception as e:
            logger.error('Exception:%s', e)
            logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})

    
    def process_api(self, sub, req):
        try:
            if sub == 'search':
                call = req.args.get('call')
                if call == 'plex':
                    manual = (req.args.get('manual') == 'True')

                    return jsonify(self.search(req.args.get('keyword'), manual=manual))

            elif sub == 'info':
                call = req.args.get('call')
                data = self.info(req.args.get('code'))

                return jsonify(data)
        
        except Exception as e: 
            logger.error('Exception:%s', e)
            logger.error(traceback.format_exc())


    ################################################################

    def search(self, keyword, manual=False):
        if re.fullmatch('\\d{5,7}', keyword):
            fc2_code = keyword
        elif re.search('FC2[-_\\s]?(?:PPV)?[-_\\s]?(\\d{6,7})-?(?:cd)?(\\d)?', keyword, re.I):
            fc2_code = re.search('FC2[-_\\s]?(?:PPV)?[-_\\s]?(\\d{6,7})-?(?:cd)?(\\d)?', keyword, re.I).group(1).lstrip('0')
        else:
            logger.debug(f'wrong search keyword: {keyword}')
            return []

        # fc2_code = re.search('FC2[-_\\s]?(?:PPV)?[-_\\s]?(\\d{6,7})-?(?:cd)?(\\d)?', keyword, re.I).group(1).lstrip('0')
        logger.debug('fc2 search - keyword:[%s] code:[%s] manual:[%s]', keyword, fc2_code, manual)
        do_trans = manual
        ret = []
        site_list = ModelSetting.get_list('jav_fc2_order', ',')
        for idx, site in enumerate(site_list):
            if site == 'fc2com':
                from support_site import SiteFc2Com as SiteClass
            elif site == 'msin':
                from support_site import SiteMsin as SiteClass
            elif site == 'bp4x':
                from support_site import SiteBp4x as SiteClass
            elif site == 'fc2cm':
                from support_site import SiteFc2Cm as SiteClass
            elif site == 'fc2hub':
                from support_site import SiteFc2Hub as SiteClass
            elif site == '7mmtv':
                from support_site import Site7mmTv as SiteClass

            data = SiteClass.search(
                fc2_code, 
                do_trans=do_trans,
                proxy_url=ModelSetting.get('jav_fc2_{site_name}_proxy_url'.format(site_name=SiteClass.site_name)) if ModelSetting.get_bool('jav_fc2_{site_name}_use_proxy'.format(site_name=SiteClass.site_name)) else None, 
                image_mode=ModelSetting.get('jav_fc2_{site_name}_image_mode'.format(site_name=SiteClass.site_name)),manual=manual)
            
            if data['ret'] == 'success' and len(data['data']) > 0:
                if idx != 0:
                    for item in data['data']:
                        item['score'] += -1
                ret += data['data']
                ret = sorted(ret, key=lambda k: k['score'], reverse=True)  
            
            if manual:
                continue
            else:
                # if site == 'javdb':
                #     logger.debug(f'javdb delay {ModelSetting.get("jav_fc2_javdb_delay")} seconds....')
                #     time.sleep(int(ModelSetting.get('jav_fc2_javdb_delay')))
                if len(ret) > 0 and ret[0]['score'] > 95:
                    break


        return ret


    def info(self, code):
        ret = None
        if ModelSetting.get_bool('jav_fc2_use_sjva'):
            ret = MetadataServerUtil.get_metadata(code)
            if ret is not None:
                logger.debug('Get meda info by server : %s', code)
        if ret is None:
            if code[1] == 'F':
                from support_site import SiteFc2Com
                ret = self.info2(code, SiteFc2Com)
            elif code[1] == 'N':
                from support_site import SiteMsin
                ret = self.info2(code, SiteMsin)
            elif code[1] == 'B':
                from support_site import SiteBp4x
                ret = self.info2(code, SiteBp4x)
            elif code[1] == 'M':
                from support_site import SiteFc2Cm
                ret = self.info2(code, SiteFc2Cm)
            elif code[1] == 'H':
                from support_site import SiteFc2Hub
                ret = self.info2(code, SiteFc2Hub)
            elif code[1] == '7':
                from support_site import Site7mmTv
                ret = self.info2(code, Site7mmTv)
        
        if ret is not None:
            ret['title'] = ModelSetting.get('jav_fc2_title_format').format(
                originaltitle=ret['originaltitle'], 
                plot=ret['plot'],
                title=ret['title'],
                sorttitle=ret['sorttitle'],
                country=ret['country'],
                premiered=ret['premiered'],
                year=ret['year'],
                tagline=ret['tagline']
            )

            if ModelSetting.get_bool('jav_fc2_use_extras') == False:
                ret['extras'] = []

            return ret

    def info2(self, code, SiteClass):
        image_mode = ModelSetting.get(f'jav_fc2_image_mode')
        data = SiteClass.info(
            code,
            proxy_url=ModelSetting.get(f'jav_fc2_{SiteClass.site_name}_proxy_url') if ModelSetting.get_bool(f'jav_fc2_{SiteClass.site_name}_use_proxy') else None, 
            image_mode=image_mode)


        if data['ret'] == 'success':
            ret = data['data']
            logger.debug(ret)
            if ModelSetting.get_bool('jav_fc2_use_sjva') and image_mode == '3' and (SystemModelSetting.get('trans_type') == '4' or (SystemModelSetting.get('trans_type') == '1' and SystemModelSetting.get('trans_google_api_key') != '')):
                MetadataServerUtil.set_metadata_jav_uncensored(code, ret, ret['title'].lower())
        else:
            logger.debug('info2 fail..')
            ret = None
        return ret
