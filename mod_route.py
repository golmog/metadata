import os
import re
import time
import json
import requests
import traceback
from urllib.parse import unquote_plus
from support_site import SupportWavve, SiteUtilAv, SiteUtil, SiteAvBase
from support_site.ssrf_guard import fetch as guarded_fetch, public_scope, SSRFRejected, validate, current_image_proxies
from flask import request, send_file, redirect, abort, Response, send_from_directory, current_app
from io import BytesIO
from PIL import Image, UnidentifiedImageError

from .setup import *
from .util_runtime import storage_logger

logger = storage_logger(logger)


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
                    from werkzeug.exceptions import HTTPException
                    try:
                        image_root_dir = (
                            P.ModelSetting.get('jav_censored_image_server_local_path') or
                            P.ModelSetting.get('western_image_server_local_path') or
                            os.path.join(path_data, 'images')
                        )
                        image_root_dir = os.path.abspath(image_root_dir)
                        abs_target_path = os.path.abspath(os.path.join(image_root_dir, filename))
                        
                        if os.path.commonpath([image_root_dir, abs_target_path]) == image_root_dir:
                            if os.path.exists(abs_target_path):
                                return send_from_directory(image_root_dir, filename)
                            else:
                                abort(404)
                        else:
                            abort(403)
                    except HTTPException as e:
                        return e.get_response(request.environ)
                    except Exception as e:
                        logger.error(f"Global Image Route Error: {e}")
                        abort(500)
                
                app.add_url_rule('/images/<path:filename>', 'serve_global_images', serve_global_images, methods=['GET'])
                # logger.debug("[Metadata] Successfully injected global route '/images' into Flask Core.")
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
                @F.check_api
                def person_api():
                    try:
                        from .mod_meta_db import ModuleMetaDb
                        from .util_metadata import MetaUtil
                        if not ModuleMetaDb.is_effectively_enabled():
                            return ModuleMetaDb.disabled_response()

                        command = request.form.get('command')
                        search_domain = request.form.get('search_domain')

                        # 목록 조회 명령 또는 인물 카테고리 요청 처리
                        if command in ['web_list', 'list', 'person_web_list'] or search_domain is not None or request.form.get('category') == 'PERSON':
                            default_domain = search_domain or 'ALL'
                            listing = ModuleMetaDb.person_web_list(request, default_domain=default_domain)
                            failed = isinstance(listing, dict) and listing.get('success') is False
                            if failed:
                                listing = dict(listing, ret='error', error_code='lookup_failed', error_origin='ff',
                                               msg=listing.get('msg') or '인물 DB 목록 조회에 실패했습니다.')
                            return current_app.response_class(
                                response=json.dumps(listing, ensure_ascii=False),
                                status=500 if failed else 200,
                                mimetype='application/json'
                            )

                        # 인물 및 메타 DB 관련 모든 커맨드 동적 위임
                        meta_module = P.get_module('meta_db')
                        if meta_module is None:
                            return current_app.response_class(
                                response=json.dumps({'ret': 'error', 'error_code': 'ff_internal_error', 'error_origin': 'ff', 'msg': '메타 DB 모듈을 사용할 수 없습니다.'}, ensure_ascii=False),
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
                            return MetaUtil.api_command_response(res)

                        return current_app.response_class(
                            response=json.dumps({'ret': 'error', 'error_code': 'unsupported_command', 'error_origin': 'ff', 'msg': '지원하지 않는 인물 명령입니다.'}, ensure_ascii=False),
                            status=400,
                            mimetype='application/json'
                        )
                    except Exception as e:
                        logger.exception('[Metadata Person API] 요청 처리 실패')
                        return current_app.response_class(
                            response=json.dumps({'ret': 'error', 'error_code': 'ff_internal_error', 'error_origin': 'ff', 'msg': '인물 API 요청 처리에 실패했습니다.'}, ensure_ascii=False),
                            status=500,
                            mimetype='application/json'
                        )

                app.add_url_rule(person_api_rule, person_api_endpoint, person_api, methods=['POST'])
                # logger.debug(f"[Metadata] Successfully injected route '{person_api_rule}'.")

            else:
                logger.debug(f"[Metadata] Route '{person_api_rule}' already exists. Injection skipped.")

            meta_api_rule = f'/{P.package_name}/meta_api'
            meta_api_endpoint = f'{P.package_name}_meta_api'
            meta_api_exists = any(rule.rule == meta_api_rule for rule in app.url_map.iter_rules())

            if not meta_api_exists:
                @F.check_api
                def meta_api():
                    try:
                        from .mod_meta_db import ModuleMetaDb
                        from .util_metadata import MetaUtil
                        if not ModuleMetaDb.is_effectively_enabled():
                            return ModuleMetaDb.disabled_response()
                        meta_module = P.get_module('meta_db')
                        command = request.form.get('command') or ''

                        # 목록 조회 처리
                        if command in ['web_list', 'list'] or not command:
                            listing = ModuleMetaDb.web_list(request, category=request.form.get('category') or 'JAV_CEN')
                            failed = isinstance(listing, dict) and listing.get('success') is False
                            if failed:
                                listing = dict(listing, ret='error', error_code='lookup_failed', error_origin='ff',
                                               msg=listing.get('msg') or '메타 DB 목록 조회에 실패했습니다.')
                            return current_app.response_class(
                                response=json.dumps(listing, ensure_ascii=False),
                                status=500 if failed else 200,
                                mimetype='application/json'
                            )

                        # 목록 조회가 아닌 모든 커맨드는 meta_db 모듈의 process_command로 동적 위임
                        if meta_module is None:
                            return current_app.response_class(
                                response=json.dumps({'ret': 'error', 'error_code': 'ff_internal_error', 'error_origin': 'ff', 'msg': '메타 DB 모듈을 사용할 수 없습니다.'}, ensure_ascii=False),
                                status=500, mimetype='application/json')
                        if meta_module:
                            res = meta_module.process_command(
                                command,
                                request.form.get('arg1'),
                                request.form.get('arg2'),
                                request.form.get('arg3'),
                                request
                            )
                            if res is not None:
                                return MetaUtil.api_command_response(res)

                        return current_app.response_class(
                            response=json.dumps({'ret': 'error', 'error_code': 'unsupported_command', 'error_origin': 'ff', 'msg': '지원하지 않는 메타 명령입니다.'}, ensure_ascii=False),
                            status=400,
                            mimetype='application/json'
                        )
                    except Exception as e:
                        logger.exception('[Metadata Meta API] 요청 처리 실패')
                        return current_app.response_class(
                            response=json.dumps({'ret': 'error', 'error_code': 'ff_internal_error', 'error_origin': 'ff', 'success': False, 'msg': '메타 API 요청 처리에 실패했습니다.'}, ensure_ascii=False),
                            status=500,
                            mimetype='application/json'
                        )

                app.add_url_rule(meta_api_rule, meta_api_endpoint, meta_api, methods=['POST'])
                # logger.debug(f"[Metadata] Successfully injected route '{meta_api_rule}'.")
            else:
                logger.debug(f"[Metadata] Route '{meta_api_rule}' already exists. Injection skipped.")

            settings_api_rule = f'/{P.package_name}/settings_api'
            settings_api_endpoint = f'{P.package_name}_settings_api'
            settings_api_exists = any(rule.rule == settings_api_rule for rule in app.url_map.iter_rules())

            if not settings_api_exists:
                @F.check_api
                def settings_api():
                    try:
                        data = request.get_json(silent=True)
                        category = data.get('category') if isinstance(data, dict) else request.form.get('category')
                        if not isinstance(category, str) or category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
                            return current_app.response_class(
                                response=json.dumps({'ret': 'error', 'error_code': 'invalid_request', 'msg': '지원하지 않는 카테고리입니다.'}, ensure_ascii=False),
                                status=400, mimetype='application/json'
                            )

                        from .util_preview import MetaPreviewUtil
                        return current_app.response_class(
                            response=json.dumps({
                                'ret': 'success',
                                'category': category,
                                'media': {
                                    'enabled': bool(MetaPreviewUtil.is_enabled(category)),
                                    'auto_download_trailer': bool(MetaPreviewUtil.is_trailer_download_enabled(category)),
                                    'auto_create_preview': bool(MetaPreviewUtil.is_auto_create_enabled(category)),
                                },
                            }, ensure_ascii=False),
                            status=200, mimetype='application/json'
                        )
                    except Exception:
                        logger.error('[Metadata Settings API] 설정 상태 조회 실패')
                        return current_app.response_class(
                            response=json.dumps({'ret': 'error', 'error_code': 'ff_internal_error', 'error_origin': 'ff', 'msg': '설정 상태 조회에 실패했습니다.'}, ensure_ascii=False),
                            status=500, mimetype='application/json'
                        )

                app.add_url_rule(settings_api_rule, settings_api_endpoint, settings_api, methods=['POST'])
                
        except Exception as e_inject:
            logger.error(f"[Metadata] Failed to inject global image route: {e_inject}")
            logger.error(traceback.format_exc())


    def process_normal(self, sub, req):
        from werkzeug.exceptions import HTTPException
        try:
            if sub == 'image_process.jpg':
                mode = request.args.get('mode')
                if mode == 'landscape_to_poster':
                    image_url = request.args.get('url')
                    res = SiteUtil.get_image_response(image_url, timeout=30, ssrf_safe=True)
                    if res.status_code != 200:
                        return Response(status=502)
                    with Image.open(BytesIO(res.content)) as im:
                        width, height = im.size
                        with im.crop((width/1.895734597, 0, width, height)) as poster:
                            buf = BytesIO()
                            poster.save(buf, format='JPEG')
                    buf.seek(0)
                    return send_file(buf, mimetype='image/jpeg')
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
                image_url = request.args.get("url")
                proxy_url = self._resolve_image_proxy(request.args)

                # image open
                res = SiteUtil.get_image_response(image_url, proxy_url=proxy_url, verify=False, ssrf_safe=True)

                if res is None:
                    P.logger.error(f"image_proxy: SiteUtil.get_response returned None for URL: [remote image]")
                    abort(404)
                    return
                
                if res.status_code != 200:
                    P.logger.error(f"image_proxy: Received status code {res.status_code} for URL: [remote image]. Content: [omitted]")
                    abort(res.status_code if res.status_code >= 400 else 500)
                    return

                content_type_header = res.headers.get('Content-Type', '').lower()
                if not content_type_header.startswith('image/'):
                    P.logger.error(f"image_proxy: Expected image Content-Type, but got '{content_type_header}' for URL: [remote image]. Content: [omitted]")
                    abort(400)
                    return

                try:
                    bytes_im = BytesIO(res.content)
                    im = Image.open(bytes_im)
                    imformat = im.format
                    if imformat is None:
                        P.logger.warning(f"image_proxy: Pillow could not determine format for image from URL: [remote image]. Attempting to infer from Content-Type.")
                        if 'jpeg' in content_type_header or 'jpg' in content_type_header:
                            imformat = 'JPEG'
                        elif 'png' in content_type_header:
                            imformat = 'PNG'
                        elif 'webp' in content_type_header:
                            imformat = 'WEBP'
                        elif 'gif' in content_type_header:
                            imformat = 'GIF'
                        else:
                            P.logger.error(f"image_proxy: Could not infer image format from Content-Type '{content_type_header}'. URL: [remote image]")
                            abort(400)
                            return
                    mimetype = im.get_format_mimetype()
                    if mimetype is None:
                        if imformat == 'JPEG': mimetype = 'image/jpeg'
                        elif imformat == 'PNG': mimetype = 'image/png'
                        elif imformat == 'WEBP': mimetype = 'image/webp'
                        elif imformat == 'GIF': mimetype = 'image/gif'
                        else:
                            P.logger.error(f"image_proxy: Could not determine mimetype for inferred format '{imformat}'. URL: [remote image]")
                            abort(400)
                            return

                except UnidentifiedImageError as e:
                    logger.error('image_proxy: image decode failed, type=%s', content_type_header)
                    logger.exception('image_proxy: PIL.UnidentifiedImageError')
                    
                    abort(400) # 잘못된 이미지 파일
                    return
                except Exception as e_pil:
                    logger.exception('image_proxy: General PIL error for remote image')
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
                image_url = request.args.get("url")
                crop_mode = request.args.get("crop_mode")
                if crop_mode is not None:
                    crop_mode = unquote_plus(crop_mode)
                ret = SiteUtilAv.discord_proxy_image(image_url, crop_mode=crop_mode, ssrf_safe=True)
                validate(ret)
                return redirect(ret)    

            elif sub in ["jav_image", "jav_image_un"]:
                # Flask already decoded the outer query; preserve signed URL bytes.
                image_url = request.args.get("url")
                mode = request.args.get("mode")
                site = request.args.get("site")
                
                target_site_cls = self._get_site_class(site)
                with public_scope():
                    return target_site_cls.jav_image(url=image_url, mode=mode, site=site)

            elif sub in ["jav_video", "jav_video_un"]:
                video_url = request.args.get("url")
                site = request.args.get("site")
                mode = request.args.get("mode")

                # 프리뷰 클립 로컬 파일 스트리밍 서빙
                if mode == "preview_local":
                    from .util_preview import MetaPreviewUtil
                    # Flask already decoded quote_plus(local_path); '+' and '%' are filenames.
                    local_filepath = request.args.get("path") or ""
                    if not local_filepath:
                        abort(404)
                    if '\x00' in local_filepath or '\\' in local_filepath:
                        abort(400)
                    local_root = MetaPreviewUtil.get_setting("preview_local_path", "JAV_CEN", default="/data/previews")
                    if not isinstance(local_root, str) or not local_root.strip() or not os.path.isabs(local_root):
                        abort(403)
                    local_root = os.path.abspath(local_root)
                    if local_root == os.path.sep or not os.path.isabs(local_filepath) or '..' in local_filepath.split('/'):
                        abort(403)
                    local_filepath = os.path.abspath(local_filepath)
                    if os.path.commonpath([local_root, local_filepath]) != local_root:
                        abort(403)
                    # Lexical containment permits operator-created symlinks by policy.
                    if not os.path.isdir(local_root) or not os.path.isfile(local_filepath):
                        abort(404)
                    return send_file(local_filepath, mimetype='video/mp4')

                # 프리뷰 클립 구글 드라이브 스트리밍 서빙
                if mode == "preview_gdrive":
                    file_id = request.args.get("fileid")
                    category = request.args.get("cat", "JAV_CEN")
                    if not file_id or category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
                        return Response(status=404)
                    try:
                        from sqlalchemy.orm import sessionmaker
                        from .mod_meta_db import ModuleMetaDb, MetaItem, MetaMedia, DOMAIN_MAP
                        if not ModuleMetaDb.is_effectively_enabled() or ModuleMetaDb._engine_config_dirty:
                            return Response(status=404)
                        # Borrow only the selected, already initialized store; never initialize DBs here.
                        store = 'postgres' if ModuleMetaDb._is_postgres else DOMAIN_MAP[category][0]
                        registry = ModuleMetaDb._sessions.get(store)
                        if registry is None:
                            return Response(status=404)
                        with sessionmaker(bind=registry.get_bind())() as read:
                            registered = read.query(MetaMedia.id).join(
                                MetaItem, MetaMedia.item_id == MetaItem.id
                            ).filter(
                                MetaItem.category == category,
                                MetaMedia.gdrive_fileid == file_id,
                                MetaMedia.media_type.in_(('preview', 'trailer'))
                            ).first() is not None
                        if not registered:
                            return Response(status=404)
                    except Exception:
                        # Fail closed without exposing IDs or storage failure details.
                        return Response(status=404)
                    if file_id:
                        from .util_preview import MetaPreviewUtil
                        rclone_conf = MetaPreviewUtil.get_setting("preview_rclone_conf", category, default="/root/.config/rclone/rclone.conf")
                        
                        remote_name = (
                            MetaPreviewUtil.get_setting("preview_rclone_playback_remote", category, default="") or
                            MetaPreviewUtil.get_setting("preview_rclone_remote", category, default="my_gdrive")
                        )

                        access_token = MetaPreviewUtil.get_gdrive_access_token(rclone_conf, remote_name, file_id=file_id)
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
                    return Response(status=404)

                target_site_cls = self._get_site_class(site)
                with public_scope():
                    validate(video_url)
                    return target_site_cls.jav_video(video_url)

        except SSRFRejected:
            return Response('Remote media request denied', status=403, mimetype='text/plain')
        except HTTPException as e:
            if sub in ('image_proxy', 'discord_proxy', 'image_process.jpg') or (
                    sub in ('jav_video', 'jav_video_un') and
                    req.args.get('mode') not in ('preview_local', 'preview_gdrive')):
                return Response(status=e.code)
            if sub in ('jav_image', 'jav_image_un') or (
                    sub in ('jav_video', 'jav_video_un') and req.args.get('mode') == 'preview_local'):
                # FF's outer plugin route catches raised exceptions without a response.
                return e.get_response(req.environ)
            raise
        except Exception as e:
            if sub in ('image_proxy', 'discord_proxy', 'image_process.jpg', 'jav_image', 'jav_image_un') or (
                    sub in ('jav_video', 'jav_video_un') and
                    req.args.get('mode') not in ('preview_local', 'preview_gdrive')):
                mode = req.args.get('mode')
                site = req.args.get('site')
                safe_mode = mode if mode in ('poster', 'landscape', 'original', 'preview_local', 'preview_gdrive') else '기타'
                safe_site = site if site in ('dmm', 'mgstage', '1pondo', '10musume', 'paco', 'heyzo', 'carib', 'fc2com', 'stashdb', 'tpdb') else '미지정/기타'
                logger.exception('[미디어] 원격 요청 처리 실패: sub=%s mode=%s site=%s',
                    sub, safe_mode, safe_site)
                return Response(status=502)
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())
            if sub in ('jav_image', 'jav_image_un') or (
                    sub in ('jav_video', 'jav_video_un') and req.args.get('mode') == 'preview_local'):
                return Response(status=500)


    def _resolve_image_proxy(self, args):
        """Ignore legacy proxy_url/proxy_ref completely, including duplicates.

        A site is a public selector within registered operator settings, never
        authorization. Historical links without a site use a proxy only when
        there is exactly one distinct enabled URL; ambiguous links go direct.
        """
        current = current_image_proxies(P)
        if 'proxy_site' in args:
            if hasattr(args, 'getlist') and len(args.getlist('proxy_site')) != 1:
                raise SSRFRejected()
            site = args.get('proxy_site')
            if not isinstance(site, str) or site not in current:
                raise SSRFRejected()
            return current[site]
        enabled = set(value for value in current.values() if value)
        return next(iter(enabled)) if len(enabled) == 1 else None


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
