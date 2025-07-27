import requests
from urllib.parse import unquote_plus
from support_site import SupportWavve, SiteUtilAv, SiteUtil
from flask import request, send_file, redirect, abort, Response
from io import BytesIO
from PIL import Image, UnidentifiedImageError

from .setup import *


class ModuleRoute(PluginModuleBase):

    def __init__(self, P):
        super(ModuleRoute, self).__init__(P, name='route')
    
    
    def process_normal(self, sub, req):
        try:
            if sub == 'image_process.jpg':
                mode = request.args.get('mode')
                if mode == 'landscape_to_poster':
                    image_url = unquote_plus(request.args.get('url'))
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
                        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]',  # mp4 비디오와 m4a 오디오 형식 선택
                        'quiet': True,  # 불필요한 출력 억제
                        'skip_download': True,  # 실제로 파일을 다운로드하지 않음
                        "username": "oauth2",
                        "password": ""
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        target_url = f"https://www.youtube.com/watch?v={request.args.get('param')}"
                        result = ydl.extract_info(target_url)
                        if 'formats' in result:
                            for item in reversed(result['formats']):
                                if item['ext'] == 'mp4' and item['vcodec'].startswith('avc1'):# and item['acodec'].startswith('mp4a') :
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
            # AV 처리용 sjva metadata에서 이동
            elif sub == "image_proxy":
                image_url = unquote_plus(request.args.get("url"))
                proxy_url = request.args.get("proxy_url")
                if proxy_url is not None:
                    proxy_url = unquote_plus(proxy_url)

                # image open
                res = SiteUtil.get_response(image_url, proxy_url=proxy_url, verify=False)  # SSL 인증서 검증 비활성화 (필요시)

                # --- 응답 검증 추가 ---
                if res is None:
                    P.logger.error(f"image_proxy: SiteUtil.get_response returned None for URL: {image_url}")
                    abort(404) # 또는 적절한 에러 응답
                    return # 함수 종료
                
                if res.status_code != 200:
                    P.logger.error(f"image_proxy: Received status code {res.status_code} for URL: {image_url}. Content: {res.text[:200]}")
                    abort(res.status_code if res.status_code >= 400 else 500)
                    return

                content_type_header = res.headers.get('Content-Type', '').lower()
                if not content_type_header.startswith('image/'):
                    P.logger.error(f"image_proxy: Expected image Content-Type, but got '{content_type_header}' for URL: {image_url}. Content: {res.text[:200]}")
                    abort(400) # 잘못된 요청 또는 서버 응답 오류
                    return
                # --- 응답 검증 끝 ---

                try:
                    bytes_im = BytesIO(res.content)
                    im = Image.open(bytes_im)
                    imformat = im.format
                    if imformat is None: # Pillow가 포맷을 감지 못하는 경우 (드물지만 발생 가능)
                        P.logger.warning(f"image_proxy: Pillow could not determine format for image from URL: {image_url}. Attempting to infer from Content-Type.")
                        if 'jpeg' in content_type_header or 'jpg' in content_type_header:
                            imformat = 'JPEG'
                        elif 'png' in content_type_header:
                            imformat = 'PNG'
                        elif 'webp' in content_type_header:
                            imformat = 'WEBP'
                        elif 'gif' in content_type_header:
                            imformat = 'GIF'
                        else:
                            P.logger.error(f"image_proxy: Could not infer image format from Content-Type '{content_type_header}'. URL: {image_url}")
                            abort(400)
                            return
                    mimetype = im.get_format_mimetype()
                    if mimetype is None: # 위에서 imformat을 강제로 설정한 경우 mimetype도 설정
                        if imformat == 'JPEG': mimetype = 'image/jpeg'
                        elif imformat == 'PNG': mimetype = 'image/png'
                        elif imformat == 'WEBP': mimetype = 'image/webp'
                        elif imformat == 'GIF': mimetype = 'image/gif'
                        else:
                            P.logger.error(f"image_proxy: Could not determine mimetype for inferred format '{imformat}'. URL: {image_url}")
                            abort(400)
                            return

                except UnidentifiedImageError as e: # PIL.UnidentifiedImageError 명시적 임포트 필요
                    P.logger.error(f"image_proxy: PIL.UnidentifiedImageError for URL: {image_url}. Response Content-Type: {content_type_header}")
                    P.logger.error(f"image_proxy: Error details: {e}")
                    # 디버깅을 위해 실패한 이미지 데이터 일부 저장 (선택적)
                    try:
                        failed_image_path = os.path.join(path_data, "tmp", f"failed_image_{time.time()}.bin")
                        with open(failed_image_path, 'wb') as f:
                            f.write(res.content)
                        P.logger.info(f"image_proxy: Content of failed image saved to: {failed_image_path}")
                    except Exception as save_err:
                        P.logger.error(f"image_proxy: Could not save failed image content: {save_err}")
                    abort(400) # 잘못된 이미지 파일
                    return
                except Exception as e_pil:
                    P.logger.error(f"image_proxy: General PIL error for URL: {image_url}: {e_pil}")
                    P.logger.error(traceback.format_exc())
                    abort(500)
                    return

                # apply crop - quality loss
                crop_mode = request.args.get("crop_mode")
                if crop_mode is not None:
                    crop_mode = unquote_plus(crop_mode)
                    im = SiteUtilAv.imcrop(im, position=crop_mode)
                    with BytesIO() as buf:
                        im.save(buf, format=imformat, quality=95)
                        return Response(buf.getvalue(), mimetype=mimetype)
                bytes_im.seek(0)
                return send_file(bytes_im, mimetype=mimetype)

            elif sub == "discord_proxy":
                image_url = unquote_plus(request.args.get("url"))
                proxy_url = request.args.get("proxy_url")
                if proxy_url is not None:
                    proxy_url = unquote_plus(proxy_url)
                crop_mode = request.args.get("crop_mode")
                if crop_mode is not None:
                    crop_mode = unquote_plus(crop_mode)
                ret = SiteUtilAv.discord_proxy_image(image_url, proxy_url=proxy_url, crop_mode=crop_mode)
                return redirect(ret)    

            elif sub == "jav_image":
                image_url = unquote_plus(request.args.get("url"))
                mode = request.args.get("mode")
                site = request.args.get("site")
                if mode: 
                    mode = unquote_plus(mode)
                return P.get_module("jav_censored").site_map[site].jav_image(image_url, mode=mode)
            elif sub == "jav_video":
                image_url = unquote_plus(request.args.get("url"))
                site = request.args.get("site")
                return P.get_module("jav_censored").site_map[site].jav_video(image_url)
            elif sub == "jav_image_un":
                image_url = unquote_plus(request.args.get("url"))
                mode = request.args.get("mode")
                site = request.args.get("site")
                if mode: 
                    mode = unquote_plus(mode)
                return P.get_module("jav_uncensored").site_map[site]['instance'].jav_image(image_url, mode=mode)
            elif sub == "jav_video_un":
                image_url = unquote_plus(request.args.get("url"))
                site = request.args.get("site")
                return P.get_module("jav_uncensored").site_map[site]['instance'].jav_video(image_url)

        except Exception as e: 
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())
