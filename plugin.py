import os
import time
from io import BytesIO

# third-party
import requests
from flask import Blueprint, Response, redirect, request, send_file, abort
from PIL import Image

# sjva 공용
from framework import app, check_api, path_data, py_urllib
from framework.logger import get_logger
from lib_metadata import SiteUtil, SiteNaverMovie
from tool_base import ToolUtil

from plugin import Logic, default_route, get_model_setting

# 패키지
#########################################################


class P:
    package_name = __name__.split(".", maxsplit=1)[0]
    logger = get_logger(package_name)
    blueprint = Blueprint(
        package_name,
        package_name,
        url_prefix=f"/{package_name}",
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    )
    menu = {
        "main": [package_name, "메타데이터"],
        "sub": [
            ["movie", "영화"],
            ["ktv", "국내TV"],
            ["ftv", "외국TV"],
            ["music_normal", "음악 일반"],
            ["jav_censored", "JavCensored"],
            ["jav_uncensored", "JavUnCensored"],
            ["jav_fc2", "JavFc2"],
            ["book", "책"],
            ["log", "로그"],
        ],
        "category": "tool",
        "sub2": {
            "ktv": [
                ["setting", "설정"],
                ["test", "테스트"],  # ['daum', 'Daum'], ['wavve', '웨이브'], ['tving', '티빙'],
            ],
            "movie": [
                ["setting", "설정"],
                [
                    "test",
                    "테스트",
                ],  # ['naver', '네이버'], ['daum', 'Daum'], ['tmdb', 'TMDB'], ['watcha', '왓챠'],  ['tmdb', 'TMDB'], ['wavve', '웨이브'], ['tving', '티빙'],
            ],
            "ftv": [
                ["setting", "설정"],
                ["test", "테스트"],
            ],
            "music_normal": [
                ["setting", "설정"],
                ["test", "테스트"],
            ],
            "jav_censored": [
                ["setting", "설정"],
                ["dmm", "DMM"],
                ["mgs", "MGS"],
                ["jav321", "Jav321"],
                ["javdb", "JavDB"],
                ["javbus", "Javbus"],
            ],
            "jav_uncensored": [
                ["setting", "설정"],
                ["1pondo", "1Pondo"],
                ["10musume", "10Musume"],
                ["heyzo", "HEYZO"],
                ["carib", "Caribbeancom"],
            ],
            "jav_fc2": [
                ["setting", "설정"],
                ["setting_site", "사이트별 설정"],
                ["test_all", "통합 테스트"],
            ],
            "book": [
                ["naver", "네이버 책 검색 API"],
            ],
        },
    }

    plugin_info = {
        "version": "0.2.0.4",
        "name": package_name,
        "category": "tool",
        "icon": "",
        "developer": "by275: mod by saibi",
        "description": "Metadata",
        "home": "https://github.com/golmog/" + package_name,
        "more": "",
        "dependency": [
            {
                "name": "lib_metadata",
                "home": "https://github.com/golmog/lib_metadata",
            }
        ],
    }
    ModelSetting = get_model_setting(package_name, logger)
    logic = None
    module_list = None
    home_module = "setting"


def initialize():
    try:
        app.config["SQLALCHEMY_BINDS"][P.package_name] = "sqlite:///" + os.path.join(
            path_data, "db", f"{P.package_name}.db"
        )
        ToolUtil.save_dict(P.plugin_info, os.path.join(os.path.dirname(__file__), "info.json"))

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

        P.module_list = [
            LogicKtv(P),
            LogicJavCensored(P),
            LogicJavCensoredAma(P),
            LogicJavUncensored(P),
            LogicJavFc2(P),
            LogicOttShow(P),
            LogicMovie(P),
            LogicFtv(P),
            LogicLyric(P),
            LogicBook(P),
            LogicVideoStation(P),
            LogicMusic(P),
            LogicMusicNormal(P),
        ]
        P.logic = Logic(P)
        default_route(P)
    except Exception:
        P.logger.exception("플러그인 초기 구성 중 예외:")


logger = P.logger

initialize()


#########################################################
# API - 외부
#########################################################
@P.blueprint.route("/api/<sub>", methods=["GET", "POST"])
@check_api
def baseapi(sub):
    try:
        if sub == "image":
            # 2020-06-02 proxy 사용시 포스터처리
            image_url = request.args.get("url")
            logger.debug(image_url)
            method = P.ModelSetting.get("javdb_landscape_poster")
            if method == "0":
                if FileProcess.Vars.proxies is None:
                    return redirect(image_url)
                im = Image.open(requests.get(image_url, stream=True, proxies=FileProcess.Vars.proxies, timeout=30).raw)
                filename = os.path.join(path_data, "tmp", "rotate.jpg")
                im.save(filename)
                return send_file(filename, mimetype="image/jpeg")

            im = Image.open(requests.get(image_url, stream=True, proxies=FileProcess.Vars.proxies, timeout=30).raw)
            width, height = im.size
            logger.debug(width)
            logger.debug(height)
            if height > width * 1.5:
                return redirect(image_url)
            if method == "1":
                if width > height:
                    im = im.rotate(-90, expand=True)
            elif method == "2":
                if width > height:
                    im = im.rotate(90, expand=True)
            elif method == "3":
                new_height = int(width * 1.5)
                new_im = Image.new("RGB", (width, new_height))
                new_im.paste(im, (0, int((new_height - height) / 2)))
                im = new_im

            filename = os.path.join(path_data, "tmp", "rotate.jpg")
            im.save(filename)
            return send_file(filename, mimetype="image/jpeg")

        elif sub == "image_proxy":
            image_url = py_urllib.unquote_plus(request.args.get("url"))
            proxy_url = request.args.get("proxy_url")
            if proxy_url is not None:
                proxy_url = py_urllib.unquote_plus(proxy_url)

            # image open
            res = SiteUtil.get_response(image_url, proxy_url=proxy_url)
            bytes_im = BytesIO(res.content)
            im = Image.open(bytes_im)
            imformat = im.format
            mimetype = im.get_format_mimetype()

            # apply crop - quality loss
            crop_mode = request.args.get("crop_mode")
            if crop_mode is not None:
                crop_mode = py_urllib.unquote_plus(crop_mode)
                im = SiteUtil.imcrop(im, position=crop_mode)
                with BytesIO() as buf:
                    im.save(buf, format=imformat, quality=95)
                    return Response(buf.getvalue(), mimetype=mimetype)
            bytes_im.seek(0)
            return send_file(bytes_im, mimetype=mimetype)
        elif sub == "discord_proxy":
            image_url = py_urllib.unquote_plus(request.args.get("url"))
            proxy_url = request.args.get("proxy_url")
            if proxy_url is not None:
                proxy_url = py_urllib.unquote_plus(proxy_url)
            crop_mode = request.args.get("crop_mode")
            if crop_mode is not None:
                crop_mode = py_urllib.unquote_plus(crop_mode)
            ret = SiteUtil.discord_proxy_image(image_url, proxy_url=proxy_url, crop_mode=crop_mode)
            return redirect(ret)

        elif sub == "video":
            site = request.args.get("site")
            param = request.args.get("param")
            if site == "naver":
                ret = SiteNaverMovie.get_video_url(param)
            elif site == "youtube":
                command = [
                    "youtube-dl",
                    "-f",
                    "best",
                    "-g",
                    "https://www.youtube.com/watch?v=%s" % request.args.get("param"),
                ]
                from system.logic_command import SystemLogicCommand

                ret = SystemLogicCommand.execute_command_return(command).strip()
            elif site == "kakao":
                url = "https://tv.kakao.com/katz/v2/ft/cliplink/{}/readyNplay?player=monet_html5&profile=HIGH&service=kakao_tv&section=channel&fields=seekUrl,abrVideoLocationList&startPosition=0&tid=&dteType=PC&continuousPlay=false&contentType=&{}".format(
                    param, int(time.time())
                )

                data = requests.get(url, timeout=30).json()
                # logger.debug(json.dumps(data, indent=4))
                ret = data["videoLocation"]["url"]
                logger.debug(ret)
            return redirect(ret)
    except Exception:
        logger.exception("API 요청 처리 중 예외:")
        abort(404)
        return


@P.blueprint.route("/normal/<sub>", methods=["GET", "POST"])
def basenormal(sub):
    try:
        if sub == "image_process.jpg":
            mode = request.args.get("mode")
            if mode == "landscape_to_poster":
                image_url = py_urllib.unquote_plus(request.args.get("url"))
                im = Image.open(requests.get(image_url, stream=True, timeout=30).raw)
                width, height = im.size
                left = width / 1.895734597
                top = 0
                right = width
                bottom = height
                filename = os.path.join(path_data, "tmp", "proxy_%s.jpg" % str(time.time()))
                poster = im.crop((left, top, right, bottom))
                poster.save(filename)
                return send_file(filename, mimetype="image/jpeg")
        elif sub == "stream":
            mode = request.args.get("mode")
            param = request.args.get("param")
            if mode == "naver":
                ret = SiteNaverMovie.get_video_url(param)
            elif mode == "youtube":
                try:
                    import yt_dlp
                except ImportError:
                    try:
                        os.system("{} install yt-dlp".format(app.config["config"]["pip"]))
                    except Exception:
                        pass
                ydl_opts = {
                    "quiet": True,
                }
                ydl = yt_dlp.YoutubeDL(ydl_opts)
                target_url = f"https://www.youtube.com/watch?v={request.args.get('param')}"
                result = ydl.extract_info(target_url, download=False)
                if "formats" in result:
                    for item in reversed(result["formats"]):
                        if (
                            item["ext"] == "mp4"
                            and item["acodec"].startswith("mp4a")
                            and item["vcodec"].startswith("avc1")
                        ):
                            ret = item["url"]
                            break
            elif mode == "kakao":
                url = "https://tv.kakao.com/katz/v2/ft/cliplink/{}/readyNplay?player=monet_html5&profile=HIGH&service=kakao_tv&section=channel&fields=seekUrl,abrVideoLocationList&startPosition=0&tid=&dteType=PC&continuousPlay=false&contentType=&{}".format(
                    param, int(time.time())
                )
                data = requests.get(url, timeout=30).json()
                # logger.debug(json.dumps(data, indent=4))
                ret = data["videoLocation"]["url"]
            elif mode == "tving_movie":
                from support.site.tving import SupportTving

                data = SupportTving.ins.get_info(param, "stream50")
                ret = data["url"]
            elif mode == "tving":
                from support.site.tving import SupportTving

                data = SupportTving.ins.get_info(param, "stream50")
                ret = data["url"]
            elif mode == "wavve_movie":
                import framework.wavve.api as Wavve

                data = {"wavve_url": Wavve.streaming2("movie", param, "FHD", return_url=True)}
                ret = data["wavve_url"]
            elif mode == "wavve":
                import framework.wavve.api as Wavve

                data = Wavve.streaming("vod", param, "1080p", return_url=True)
                ret = data

            return redirect(ret)
    except Exception:
        logger.exception("API 요청 처리 중 예외:")
        abort(404)
        return
