import os
import time
import json
import requests
import traceback
from urllib.parse import unquote_plus
from support_site import SupportWavve, SiteUtilAv, SiteUtil, SiteAvBase
from flask import request, send_file, redirect, abort, Response, send_from_directory, current_app
from io import BytesIO
from PIL import Image, UnidentifiedImageError

from .setup import *


class ModuleRoute(PluginModuleBase):

    def __init__(self, P):
        super(ModuleRoute, self).__init__(P, name='route')


    def plugin_load(self):
        try:
            app = F.app 
            
            rule_exists = False
            for rule in app.url_map.iter_rules():
                if rule.rule == '/images/<path:filename>':
                    rule_exists = True
                    break
            
            if not rule_exists:
                def serve_global_images(filename):
                    try:
                        image_root_dir = (
                            P.ModelSetting.get('jav_censored_image_server_local_path') or
                            P.ModelSetting.get('western_image_server_local_path') or
                            os.path.join(path_data, 'images')
                        )
                        abs_target_path = os.path.abspath(os.path.join(image_root_dir, filename))
                        
                        if os.path.commonpath([image_root_dir, abs_target_path]) == image_root_dir:
                            if os.path.exists(abs_target_path):
                                return send_from_directory(image_root_dir, filename)
                            else:
                                abort(404)
                        else:
                            abort(403)
                    except Exception as e:
                        logger.error(f"Global Image Route Error: {e}")
                        abort(500)
                
                app.add_url_rule('/images/<path:filename>', 'serve_global_images', serve_global_images, methods=['GET'])
                logger.debug("[Metadata] Successfully injected global route '/images' into Flask Core.")
            else:
                logger.debug("[Metadata] Global route '/images' already exists in Flask Core. Injection skipped.")

            person_api_rule = f'/{P.package_name}/person_api'
            person_api_endpoint = f'{P.package_name}_person_api'
            person_api_exists = False
            for rule in app.url_map.iter_rules():
                if rule.rule == person_api_rule:
                    person_api_exists = True
                    break

            if not person_api_exists:
                def person_api():
                    try:
                        from .mod_meta_db import ModuleMetaDb

                        command = request.form.get('command')
                        search_domain = request.form.get('search_domain')

                        # 목록 조회 명령 또는 인물 카테고리 요청 처리
                        if command in ['web_list', 'list', 'person_web_list'] or search_domain is not None or request.form.get('category') == 'PERSON':
                            default_domain = search_domain or 'ALL'
                            return current_app.response_class(
                                response=json.dumps(ModuleMetaDb.person_web_list(request, default_domain=default_domain), ensure_ascii=False),
                                status=200,
                                mimetype='application/json'
                            )

                        # 인물 및 메타 DB 관련 모든 커맨드 동적 위임
                        meta_module = P.get_module('meta_db')
                        if meta_module is None:
                            return current_app.response_class(
                                response=json.dumps({'ret': 'error', 'msg': 'meta_db module not available'}, ensure_ascii=False),
                                status=500,
                                mimetype='application/json'
                            )

                        res = meta_module.process_command(
                            command,
                            request.form.get('arg1'),
                            request.form.get('arg2'),
                            request.form.get('arg3'),
                            request
                        )
                        if res is not None:
                            return res

                        return current_app.response_class(
                            response=json.dumps({'ret': 'error', 'msg': f'Unknown person command: {command}'}, ensure_ascii=False),
                            status=400,
                            mimetype='application/json'
                        )
                    except Exception as e:
                        logger.error(f"[Metadata Person API] Error: {e}")
                        logger.error(traceback.format_exc())
                        return current_app.response_class(
                            response=json.dumps({'ret': 'error', 'msg': str(e)}, ensure_ascii=False),
                            status=500,
                            mimetype='application/json'
                        )

                app.add_url_rule(person_api_rule, person_api_endpoint, person_api, methods=['POST'])
                logger.debug(f"[Metadata] Successfully injected route '{person_api_rule}'.")

            else:
                logger.debug(f"[Metadata] Route '{person_api_rule}' already exists. Injection skipped.")

            meta_api_rule = f'/{P.package_name}/meta_api'
            meta_api_endpoint = f'{P.package_name}_meta_api'
            meta_api_exists = any(rule.rule == meta_api_rule for rule in app.url_map.iter_rules())

            if not meta_api_exists:
                def meta_api():
                    try:
                        from .mod_meta_db import ModuleMetaDb
                        meta_module = P.get_module('meta_db')
                        command = request.form.get('command') or ''

                        # 목록 조회 처리
                        if command in ['web_list', 'list'] or not command:
                            return current_app.response_class(
                                response=json.dumps(
                                    ModuleMetaDb.web_list(
                                        request,
                                        category=request.form.get('category') or 'JAV_CEN'
                                    ),
                                    ensure_ascii=False
                                ),
                                status=200,
                                mimetype='application/json'
                            )

                        # 목록 조회가 아닌 모든 커맨드는 meta_db 모듈의 process_command로 동적 위임
                        if meta_module:
                            res = meta_module.process_command(
                                command,
                                request.form.get('arg1'),
                                request.form.get('arg2'),
                                request.form.get('arg3'),
                                request
                            )
                            if res is not None:
                                return res

                        return current_app.response_class(
                            response=json.dumps({'ret': 'error', 'msg': f'알 수 없는 명령: {command}'}, ensure_ascii=False),
                            status=400,
                            mimetype='application/json'
                        )
                    except Exception as e:
                        logger.error(f"[Metadata Meta API] Error: {e}")
                        logger.error(traceback.format_exc())
                        return current_app.response_class(
                            response=json.dumps({'ret': 'error', 'success': False, 'msg': str(e)}, ensure_ascii=False),
                            status=500,
                            mimetype='application/json'
                        )

                app.add_url_rule(meta_api_rule, meta_api_endpoint, meta_api, methods=['POST'])
                logger.debug(f"[Metadata] Successfully injected route '{meta_api_rule}'.")
            else:
                logger.debug(f"[Metadata] Route '{meta_api_rule}' already exists. Injection skipped.")
                
        except Exception as e_inject:
            logger.error(f"[Metadata] Failed to inject global image route: {e_inject}")
            logger.error(traceback.format_exc())


    def process_normal(self, sub, req):
        from werkzeug.exceptions import HTTPException
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
                        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]',
                        'quiet': True,
                        'skip_download': True,
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
            
            elif sub == "image_proxy":
                image_url = unquote_plus(request.args.get("url"))
                proxy_url = request.args.get("proxy_url")
                if proxy_url is not None:
                    proxy_url = unquote_plus(proxy_url)

                # image open
                res = SiteUtil.get_response(image_url, proxy_url=proxy_url, verify=False)

                if res is None:
                    P.logger.error(f"image_proxy: SiteUtil.get_response returned None for URL: {image_url}")
                    abort(404)
                    return
                
                if res.status_code != 200:
                    P.logger.error(f"image_proxy: Received status code {res.status_code} for URL: {image_url}. Content: {res.text[:200]}")
                    abort(res.status_code if res.status_code >= 400 else 500)
                    return

                content_type_header = res.headers.get('Content-Type', '').lower()
                if not content_type_header.startswith('image/'):
                    P.logger.error(f"image_proxy: Expected image Content-Type, but got '{content_type_header}' for URL: {image_url}. Content: {res.text[:200]}")
                    abort(400)
                    return

                try:
                    bytes_im = BytesIO(res.content)
                    im = Image.open(bytes_im)
                    imformat = im.format
                    if imformat is None:
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
                    if mimetype is None:
                        if imformat == 'JPEG': mimetype = 'image/jpeg'
                        elif imformat == 'PNG': mimetype = 'image/png'
                        elif imformat == 'WEBP': mimetype = 'image/webp'
                        elif imformat == 'GIF': mimetype = 'image/gif'
                        else:
                            P.logger.error(f"image_proxy: Could not determine mimetype for inferred format '{imformat}'. URL: {image_url}")
                            abort(400)
                            return

                except UnidentifiedImageError as e:
                    P.logger.error(f"image_proxy: PIL.UnidentifiedImageError for URL: {image_url}. Response Content-Type: {content_type_header}")
                    P.logger.error(f"image_proxy: Error details: {e}")
                    
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

            elif sub in ["jav_image", "jav_image_un"]:
                image_url = unquote_plus(request.args.get("url")) if request.args.get("url") else None
                mode = unquote_plus(request.args.get("mode")) if request.args.get("mode") else None
                site = request.args.get("site")
                path = request.args.get("path")
                
                target_site_cls = self._get_site_class(site)
                return target_site_cls.jav_image(url=image_url, mode=mode, site=site, path=path)

            elif sub in ["jav_video", "jav_video_un"]:
                video_url = unquote_plus(request.args.get("url")) if request.args.get("url") else None
                site = request.args.get("site")
                mode = request.args.get("mode")

                # 프리뷰 클립 로컬 파일 스트리밍 서빙
                if mode == "preview_local":
                    local_filepath = unquote_plus(request.args.get("path") or "")
                    if local_filepath and os.path.exists(local_filepath):
                        return send_file(local_filepath, mimetype='video/mp4')
                    abort(404)

                # 프리뷰 클립 구글 드라이브 스트리밍 서빙
                if mode == "preview_gdrive":
                    file_id = request.args.get("fileid")
                    category = request.args.get("cat") or "JAV_CEN"
                    if file_id:
                        from .util_preview import MetaPreviewUtil
                        rclone_conf = MetaPreviewUtil.get_setting("preview_rclone_conf", category, default="/root/.config/rclone/rclone.conf")
                        
                        remote_name = (
                            MetaPreviewUtil.get_setting("preview_rclone_playback_remote", category, default="") or
                            MetaPreviewUtil.get_setting("preview_rclone_remote", category, default="my_gdrive")
                        )

                        access_token = MetaPreviewUtil.get_gdrive_access_token(rclone_conf, remote_name)
                        if access_token:
                            gdrive_api_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
                            req_headers = {
                                "Authorization": f"Bearer {access_token}",
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                            }

                            # 브라우저의 비디오 Range 요청(탐색/시크) 헤더 전달
                            range_header = request.headers.get('Range')
                            if range_header:
                                req_headers['Range'] = range_header

                            try:
                                g_res = requests.get(gdrive_api_url, headers=req_headers, stream=True, timeout=(10, 120))
                                if g_res.status_code in [200, 206]:
                                    def generate_stream():
                                        for chunk in g_res.iter_content(chunk_size=65536):
                                            if chunk:
                                                yield chunk

                                    resp_headers = {
                                        'Content-Type': g_res.headers.get('Content-Type', 'video/mp4'),
                                        'Accept-Ranges': 'bytes'
                                    }
                                    if 'Content-Length' in g_res.headers:
                                        resp_headers['Content-Length'] = g_res.headers['Content-Length']
                                    if 'Content-Range' in g_res.headers:
                                        resp_headers['Content-Range'] = g_res.headers['Content-Range']

                                    return Response(generate_stream(), status=g_res.status_code, headers=resp_headers)
                                else:
                                    logger.error(f"[MetaPreview] 구글 드라이브 API 스트리밍 실패 (HTTP {g_res.status_code}): {g_res.text[:200]}")
                            except Exception as e_stream:
                                logger.error(f"[MetaPreview] 구글 드라이브 스트리밍 중계 예외: {e_stream}")
                        else:
                            logger.error(f"[MetaPreview] 구글 드라이브 스트리밍 토큰 발급 실패 (FileID: {file_id})")
                    abort(404)

                target_site_cls = self._get_site_class(site)
                return target_site_cls.jav_video(video_url)

        except HTTPException:
            raise
        except Exception as e: 
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())


    def _get_site_class(self, site):
        if not site or site == 'system':
            return SiteAvBase

        mod_western = P.get_module("western")
        if mod_western and site in getattr(mod_western, 'site_map', {}):
            return mod_western.site_map[site]

        mod_cen = P.get_module("jav_censored")
        if mod_cen and site in getattr(mod_cen, 'site_map', {}):
            return mod_cen.site_map[site]

        mod_uncen = P.get_module("jav_uncensored")
        if mod_uncen and site in getattr(mod_uncen, 'site_map', {}):
            site_entry = mod_uncen.site_map[site]
            return site_entry.get('instance') if isinstance(site_entry, dict) else site_entry

        return SiteAvBase
