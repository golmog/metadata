# -*- coding: utf-8 -*-
#########################################################
# python
import os, sys, traceback, re, json, threading, time, shutil
from datetime import datetime
# third-party
import requests
from html import unescape
from flask import request, render_template, jsonify, redirect, Response, send_file

# sjva 공용
from framework import db, scheduler, path_data, socketio, SystemModelSetting, app, py_urllib
from plugin import LogicModuleBase
from tool_base import d


# 패키지
from .plugin import P
logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting
#########################################################

class LogicVideoStation(LogicModuleBase):
    db_default = None

    cache = {}

    def __init__(self, P):
        super(LogicVideoStation, self).__init__(P, '')
        self.name = 'vs'

    def process_api(self, sub, req):
        if sub == 'info':
            args = {}
            args['input'] = json.loads(req.form['input'])
            args['lang'] = req.form['lang']
            args['type'] = req.form['type']
            args['limit'] = req.form['limit']
            args['allowguess'] = req.form['allowguess']

            return jsonify(self.info(args))

    def get_show(self, title):
        if title in self.cache:
            data = self.cache[title]
        else:
            module = self.P.logic.get_module('ktv')
            search = module.search(title, False)
            #logger.debug(d(search))
            data = module.info(search['daum']['code'], search['daum']['title'])
            vs = self.change_format_for_vs(data, 'tvshow')
            data['vs'] = vs
            self.cache[title] = data
        return data

    def info(self, args):
        if args['type'] == 'movie':
            module = self.P.logic.get_module('movie')
            search = module.search(args['input']['title'], 1900, False)
            #logger.debug(d(search))
            ret = []
            for item in search:
                if item['score'] >= 90:
                    data = module.info(item['code'])
                    vs = self.change_format_for_vs(data, 'movie')
                    ret.append(vs)
                else:
                    break
            if len(ret) == 0:
                module = self.P.logic.get_module('jav_censored')
                search = module.search(args['input']['title'])
                #logger.debug(d(search))
                for item in search:
                    if item['score'] >= 90:
                        data = module.info(item['code'])
                        logger.warning(d(data))
                        vs = self.change_format_for_vs(data, 'jav')
                        ret.append(vs)

            return ret
        elif args['type'] == 'tvshow':
            data = self.get_show(args['input']['title'])
            return [data['vs']]
        elif args['type'] == 'tvshow_episode':
            data = self.get_show(args['input']['title'])
            episode_no = int(args['input']['episode'])
            try:
                logger.warning(data['extra_info']['episodes'].keys())
                episode = data['extra_info']['episodes'][episode_no]
            except:
                logger.warning(f"not found episode : {episode_no}")
                return []

            if 'daum' in episode:
                epi = self.P.logic.get_module('ktv').episode_info(episode['daum']['code'])
                logger.warning(epi)
                vs = self.change_format_for_vs(epi, 'tvshow_episode', episode_no, data)
            elif 'wavve' in episode:
                vs = self.change_format_for_vs(episode['wavve'], 'tvshow_episode', episode_no, data)
            elif 'tving' in episode:
                vs = self.change_format_for_vs(episode['tving'], 'tvshow_episode', episode_no, data)
            #logger.error(d(vs))
            return [vs]


    def change_format_for_vs(self, data, content_type, episode=1, show_data=None):
        #logger.warning(d(data))
        if content_type == 'movie':
            vs = {
                'title' : data['title'],
                'tagline' : data['tagline'],
                'original_available' : data['premiered'],
                'summary' : data['plot'],
                'certificate' : data['mpaa'],
                'genre' : data['genre'],
                'actor' : [x['name'] for x in data['actor']],
                'director' : data['director'],
                'writer' : data['credits'],
                'extra' : {
                    'sina': {
                        'rating' : 0,
                        'poster' : [],
                        'backdrop' : [],
                    }
                }
            }
            

            if data['ratings']:
                vs['extra']['sina']['rating'] = data['ratings'][0]['value']

            for item in data['art']:
                if item['aspect'] == 'poster':
                    vs['extra']['sina']['poster'].append(item['value'])
                elif item['aspect'] == 'landscape':
                    vs['extra']['sina']['backdrop'].append(item['value'])
            
            return vs
        elif content_type == 'jav':
            vs = {
                'title' : data['title'],
                'tagline' : '',
                'original_available' : data['premiered'],
                'summary' : data['plot'],
                'certificate' : data['mpaa'],
                'genre' : data['genre'],
                'actor' : [x['name'] for x in data['actor']],
                'director' : [data['director']],
                'writer' : ['--'],
                'extra' : {
                    'sina': {
                        'rating' : 0,
                        'poster' : [],
                        'backdrop' : [],
                    }
                }
            }

            if data['ratings']:
                vs['extra']['sina']['rating'] = data['ratings'][0]['value']*2

            for item in data['thumb']:
                if item['aspect'] == 'poster':
                    vs['extra']['sina']['poster'].append(item['value'])
                elif item['aspect'] == 'landscape':
                    vs['extra']['sina']['backdrop'].append(item['value'])

            vs['summary'] = unescape(vs['summary'])
            vs['title'] = unescape(vs['title'])
            return vs
        elif content_type == 'tvshow':
            vs = {
                'title' : data['title'],
                'original_available' : data['premiered'],
                'original_title' : data['title'],
                'summary' : data['plot'],
                'extra' : {
                    'sina': {
                        'poster' : [],
                        'backdrop' : [],
                    }
                }
            }
            for item in data['thumb']:
                if item['aspect'] == 'poster':
                    vs['extra']['sina']['poster'].append(item['value'])
                elif item['aspect'] == 'landscape':
                    vs['extra']['sina']['backdrop'].append(item['value'])
            return vs
        elif content_type == 'tvshow_episode':
            vs = {
                'title' : show_data['title'],
                'tagline' : data['title'],
                'original_available' : data['premiered'],
                'summary' : data['plot'],
                'genre' : show_data['genre'],
                'actor' : [x['name'] for x in show_data['actor']],
                'director' : [x['name'] for x in show_data['director']],
                'writer' : [x['name'] for x in show_data['credits']],
                'extra' : {
                    'sina': {
                        'tvshow' : show_data['vs'],
                        'poster' : [],
                    }
                },
                "season": 1,
		        "episode": episode,
            }
            
            if vs['tagline'] == '':
                vs['tagline'] = f"Episode : {episode}"
            
            for item in data['thumb']:
                vs['extra']['sina']['poster'].append(item['value'])

            
            return vs
            

