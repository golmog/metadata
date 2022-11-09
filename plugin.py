# python
import json
import os
import time
import traceback

# third-party
import requests
from flask import Blueprint, jsonify, redirect, request, send_file
# sjva 공용
from framework import SystemModelSetting, app, check_api, path_data, py_urllib
from framework.logger import get_logger
from framework.util import Util
# 패키지
#########################################################
from support.base import d

from plugin import Logic, default_route, get_model_setting


class P(object):
    package_name = __name__.split('.')[0]
    logger = get_logger(package_name)
    blueprint = Blueprint(package_name, package_name, url_prefix='/%s' %  package_name, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
    menu = {
        'main' : [package_name, '메타데이터'],
        'sub' : [
            ['movie', '영화'], ['ktv', '국내TV'],  ['ftv', '외국TV'], ['music_normal', '음악 일반'], ['jav_censored', 'JavCensored'], ['jav_censored_ama', 'JavCensored AMA'], ['jav_uncensored', 'JavUnCensored'], ['jav_fc2', 'JavFc2'], ['book', '책'], ['log', '로그']
        ], 
        'category' : 'tool',
        'sub2' : {
            'ktv' : [
                ['setting', '설정'], ['test', '테스트'], #['daum', 'Daum'], ['wavve', '웨이브'], ['tving', '티빙'], 
            ],
            'movie' : [
                ['setting', '설정'], ['test', '테스트'], #['naver', '네이버'], ['daum', 'Daum'], ['tmdb', 'TMDB'], ['watcha', '왓챠'],  ['tmdb', 'TMDB'], ['wavve', '웨이브'], ['tving', '티빙'], 
            ],
            'ftv' : [
                ['setting', '설정'], ['test', '테스트'],
            ],
            'music_normal' : [
                ['setting', '설정'], ['test', '테스트'],
            ],
            'jav_censored' : [
                ['setting', '설정'], ['dmm', 'DMM'], ['mgs', 'MGS'], ['javbus', 'Javbus'],
            ],
            'jav_censored_ama' : [
                ['setting', '설정'], ['jav321', 'Jav321'], 
            ],
            'jav_uncensored' : [
                ['setting', '설정'], ['1pondo', '1Pondo'], ['10musume', '10Musume'], ['heyzo', 'HEYZO'], ['carib', 'Caribbeancom'], 
            ],
            'jav_fc2' : [
                ['setting', '설정'], ['setting_site', '사이트별 설정'], ['test_all', '통합 테스트'], 
            ],
            'book' : [
                ['naver', '네이버 책 검색 API'],
            ],
        }
    }  

    plugin_info = {
        'version' : '0.2.0.0',
        'name' : package_name,
        'category' : 'tool',
        'icon' : '',
        'developer' : 'soju6jan',
        'description' : 'Metadata',
        'home' : 'https://github.com/soju6jan/%s' % package_name,
        'more' : '',
        'dependency' : [
            {   
                'name' : 'support_site',
                'home' : 'https://github.com/soju6jan/support_site',
            }
        ]
    }
    ModelSetting = get_model_setting(package_name, logger)
    logic = None
    module_list = None
    home_module = 'setting'

    
def initialize():
    try:
        app.config['SQLALCHEMY_BINDS'][P.package_name] = 'sqlite:///%s' % (os.path.join(path_data, 'db', '{package_name}.db'.format(package_name=P.package_name)))
        from tool_base import ToolUtil
        ToolUtil.save_dict(P.plugin_info, os.path.join(os.path.dirname(__file__), 'info.json'))

        from .logic_book import LogicBook
        from .logic_ftv import LogicFtv
        from .logic_jav_censored import LogicJavCensored
        from .logic_jav_censored_ama import LogicJavCensoredAma
        from .logic_jav_fc2 import LogicJavFc2
        from .logic_jav_uncensored import LogicJavUncensored
        from .logic_ktv import LogicKtv
        from .logic_lyric import LogicLyric
        from .logic_movie import LogicMovie
        from .logic_music import LogicMusic
        from .logic_music_normal import LogicMusicNormal
        from .logic_ott_show import LogicOttShow
        from .logic_videostation import LogicVideoStation
        P.module_list = [LogicKtv(P), LogicJavCensored(P), LogicJavCensoredAma(P), LogicJavUncensored(P), LogicJavFc2(P), LogicOttShow(P), LogicMovie(P), LogicFtv(P), LogicLyric(P), LogicBook(P), LogicVideoStation(P), LogicMusic(P), LogicMusicNormal(P)]
        P.logic = Logic(P)
        default_route(P)
    except Exception as e: 
        P.logger.error('Exception:%s', e)
        P.logger.error(traceback.format_exc())

logger = P.logger

initialize()





#########################################################
# API - 외부
#########################################################
@P.blueprint.route('/api/<sub>', methods=['GET', 'POST'])
@check_api
def baseapi(sub):
    try:
        if sub == 'image':
            from PIL import Image

            # 2020-06-02 proxy 사용시 포스터처리
            image_url = request.args.get('url')
            logger.debug(image_url)
            method = ModelSetting.get('javdb_landscape_poster')
            if method == '0':
                if FileProcess.Vars.proxies is None:
                    return redirect(image_url)
                else:
                    im = Image.open(requests.get(image_url, stream=True, proxies=FileProcess.Vars.proxies).raw)
                    filename = os.path.join(path_data, 'tmp', 'rotate.jpg')
                    im.save(filename)
                    return send_file(filename, mimetype='image/jpeg')
            
            im = Image.open(requests.get(image_url, stream=True, proxies=FileProcess.Vars.proxies).raw)
            width,height = im.size
            logger.debug(width)
            logger.debug(height)
            if height > width * 1.5:
                return redirect(image_url)
            if method == '1':
                if width > height:
                    im = im.rotate(-90, expand=True)
            elif method == '2':
                if width > height:
                    im = im.rotate(90, expand=True)
            elif method == '3':
                new_height = int(width * 1.5)
                new_im = Image.new('RGB', (width, new_height))
                new_im.paste(im, (0, int((new_height-height)/2)))
                im = new_im

            filename = os.path.join(path_data, 'tmp', 'rotate.jpg')
            im.save(filename)
            return send_file(filename, mimetype='image/jpeg')

        elif sub == 'image_proxy':
            from PIL import Image
            image_url = py_urllib.unquote_plus(request.args.get('url'))
            proxy_url = request.args.get('proxy_url')
            if proxy_url is not None:
                proxy_url = py_urllib.unquote_plus(proxy_url)

            logger.debug('image_url : %s', image_url)
            #2020-09-21 핸드쉐이크 에러
            from system.logic_command import SystemLogicCommand
            filename = os.path.join(path_data, 'tmp', 'proxy_%s.jpg' % str(time.time()) )

            #im = Image.open(requests.get(image_url, stream=True, verify=False, proxies=FileProcess.Vars.proxies).raw)
            #im.save(filename)
            if proxy_url is not None and proxy_url != '':
                # 알파인 도커 wget 에 -e 옵션 안먹음
                #tmp = image_url.split('//')
                #if len(tmp) == 2:
                #    image_url = tmp[1]
                #command = ['wget', '-O', filename, image_url, '-e', 'use_proxy=yes', '-e', 'http_proxy=%s' % ModelSetting.get('proxy_url').replace('https://', '').replace('http://', '')]
                #command = ['curl', '-o', filename, image_url, '-x', proxy_url.replace('https://', '').replace('http://', '')]
                command = ['curl', '-o', filename, image_url, '-x', proxy_url]
                logger.debug(' '.join(command))
                ret = SystemLogicCommand.execute_command_return(command)
            else:
                #tmp = image_url.split('//')
                #if len(tmp) == 2:
                #    image_url = tmp[1]
                ret = SystemLogicCommand.execute_command_return(['curl', '-o', filename, image_url])
            
            return send_file(filename, mimetype='image/jpeg')
        elif sub == 'discord_proxy':
            from tool_expand import ToolExpandDiscord
            image_url = py_urllib.unquote_plus(request.args.get('url'))
            ret = ToolExpandDiscord.discord_proxy_image(image_url)
            #logger.debug(ret)
            return redirect(ret)
            from PIL import Image
            
            im = Image.open(requests.get(ret, stream=True, verify=False).raw)
            filename = os.path.join(path_data, 'tmp', 'proxy.jpg')
            im.save(filename)
            return send_file(filename, mimetype='image/jpeg')
        
        #elif sub == 'youtube':
        #    command = ['youtube-dl', '-f', 'best', '-g', 'https://www.youtube.com/watch?v=%s' % request.args.get('youtube_id')]
        #    from system.logic_command import SystemLogicCommand
        #    ret = SystemLogicCommand.execute_command_return(command).strip()
        #    return jsonify({'ret':'success', 'url':ret})

        elif sub == 'video':
            site = request.args.get('site')
            param = request.args.get('param')
            if site == 'naver':
                from support_site import SiteNaverMovie
                ret = SiteNaverMovie.get_video_url(param)
            elif site == 'youtube':
                command = ['youtube-dl', '-f', 'best', '-g', 'https://www.youtube.com/watch?v=%s' % request.args.get('param')]
                from system.logic_command import SystemLogicCommand
                ret = SystemLogicCommand.execute_command_return(command).strip()
            elif site == 'kakao':
                url = 'https://tv.kakao.com/katz/v2/ft/cliplink/{}/readyNplay?player=monet_html5&profile=HIGH&service=kakao_tv&section=channel&fields=seekUrl,abrVideoLocationList&startPosition=0&tid=&dteType=PC&continuousPlay=false&contentType=&{}'.format(param, int(time.time()))
                
                data = requests.get(url).json()
                #logger.debug(json.dumps(data, indent=4))
                ret = data['videoLocation']['url']
                logger.debug(ret)
            return redirect(ret)

        """
        elif sub == 'image_process':
            mode = request.args.get('mode')
            if mode == 'landscape_to_poster':
                from PIL import Image
                image_url = py_urllib.unquote_plus(request.args.get('url'))
                im = Image.open(requests.get(image_url, stream=True).raw)
                width, height = im.size
                left = width/1.895734597
                top = 0
                right = width
                bottom = height
                filename = os.path.join(path_data, 'tmp', 'proxy_%s.jpg' % str(time.time()) )
                poster = im.crop((left, top, right, bottom))
                poster.save(filename)
                return send_file(filename, mimetype='image/jpeg')
        """

    except Exception as e:
        logger.debug('Exception:%s', e)
        logger.debug(traceback.format_exc())


@P.blueprint.route('/normal/<sub>', methods=['GET', 'POST'])
def basenormal(sub):
    try:
        if sub == 'image_process.jpg':
            mode = request.args.get('mode')
            if mode == 'landscape_to_poster':
                from PIL import Image
                image_url = py_urllib.unquote_plus(request.args.get('url'))
                im = Image.open(requests.get(image_url, stream=True).raw)
                width, height = im.size
                left = width/1.895734597
                top = 0
                right = width
                bottom = height
                filename = os.path.join(path_data, 'tmp', 'proxy_%s.jpg' % str(time.time()) )
                poster = im.crop((left, top, right, bottom))
                poster.save(filename)
                return send_file(filename, mimetype='image/jpeg')
        elif sub == 'stream':
            mode = request.args.get('mode')
            param = request.args.get('param')
            if mode == 'naver':
                from support_site import SiteNaverMovie
                ret = SiteNaverMovie.get_video_url(param)
            elif mode == 'youtube':
                try:
                    import yt_dlp
                except:
                    try: os.system("{} install yt-dlp".format(app.config['config']['pip']))
                    except: pass
                ydl_opts = {
                    "quiet": True,
                }
                ydl = yt_dlp.YoutubeDL(ydl_opts)
                target_url = f"https://www.youtube.com/watch?v={request.args.get('param')}"
                result = ydl.extract_info(target_url, download=False)
                if 'formats' in result:
                    for item in reversed(result['formats']):
                        if item['ext'] == 'mp4' and item['acodec'].startswith('mp4a') and item['vcodec'].startswith('avc1'):
                            ret = item['url']
                            break
            elif mode == 'kakao':
                url = 'https://tv.kakao.com/katz/v2/ft/cliplink/{}/readyNplay?player=monet_html5&profile=HIGH&service=kakao_tv&section=channel&fields=seekUrl,abrVideoLocationList&startPosition=0&tid=&dteType=PC&continuousPlay=false&contentType=&{}'.format(param, int(time.time()))
                data = requests.get(url).json()
                #logger.debug(json.dumps(data, indent=4))
                ret = data['videoLocation']['url']
            elif mode == 'tving_movie':
                from support_site import SupportTving
                data = SupportTving.get_info(param, 'stream50')
                ret = data['url']
            elif mode == 'tving':
                from support_site import SupportTving
                data = SupportTving.get_info(param, 'stream50')
                ret = data['url']
            elif mode == 'wavve_movie':
                import framework.wavve.api as Wavve
                data = {'wavve_url':Wavve.streaming2('movie', param, 'FHD', return_url=True)}
                ret = data['wavve_url']
            elif mode == 'wavve':
                import framework.wavve.api as Wavve
                data = Wavve.streaming('vod', param, '1080p', return_url=True)
                ret = data
           
            logger.warning(f"mode : {mode}")
            logger.warning(f"param : {param}")
            logger.warning(f"ret : {ret}")
            return redirect(ret)             
    except Exception as e:
        logger.debug('Exception:%s', e)
        logger.debug(traceback.format_exc())




