import requests
from support_site import SupportWavve

from .setup import *


class ModuleRoute(PluginModuleBase):

    def __init__(self, P):
        super(ModuleRoute, self).__init__(P, name='route')
    
    
    def process_normal(self, sub, req):
        try:
            if sub == 'image_process.jpg':
                mode = request.args.get('mode')
                if mode == 'landscape_to_poster':
                    from PIL import Image
                    image_url = urllib.parse.unquote_plus(request.args.get('url'))
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
                        try: os.system("pip install yt-dlp")
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
                elif mode == 'tving':
                    from support_site import SupportTving
                    data = SupportTving.get_info(param[2:], 'stream50')
                    ret = data['url']
                elif mode == 'wavve_movie':
                    from support_site import SupportWavve
                    ret = SupportWavve.streaming('movie', param[2:], '1080p')['play_info']['hls']
                elif mode == 'wavve':
                    import framework.wavve.api as Wavve
                    data = Wavve.streaming('vod', param, '1080p', return_url=True)
                    ret = data
            
                logger.info(f"STREAM - mode : [{mode}] param : [{param}] ret : [{ret}]")
                return redirect(ret)             
        except Exception as e: 
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())
