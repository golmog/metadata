setting = {
    'filepath' : __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': 'movie',
    'menu': {
        'uri': __package__,
        'name': '메타데이터',
        'list': [
            {
                'uri': 'movie',
                'name': '영화',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'test', 'name': '테스트'},
                ]
            },
            {
                'uri': 'ktv',
                'name': '국내TV',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'test', 'name': '테스트'},
                ]
            },
            {
                'uri': 'ftv',
                'name': '외국TV',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'test', 'name': '테스트'},
                ] 
            },
            {
                'uri': 'music_normal',
                'name': '음악 (일반)',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'test', 'name': '테스트'},
                ]
            },
            {
                'uri': 'book',
                'name': '책',
                'list': [
                    {'uri': 'naver', 'name': '네이버 책 검색 API'},
                ]
            },
            {
                'uri': 'jav_censored',
                'name': 'JAV Censored',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                    {'uri': 'avdbs', 'name': 'Avdbs'},
                    #{'uri': 'hentaku', 'name': 'Hentaku'},
                    {'uri': 'dmm', 'name': 'DMM'},
                    {'uri': 'mgstage', 'name': 'MGStage'},
                    {'uri': 'jav321', 'name': 'Jav321'},
                    {'uri': 'javbus', 'name': 'Javbus'},
                    {'uri': 'javdb', 'name': 'JavDB'},
                    {'uri': 'manual/files/jav_censored.md', 'name': '매뉴얼'},
                ]
            },
            {
                'uri': 'jav_uncensored',
                'name': 'JAV Uncensored',
                'list': [
                    {'uri': 'setting', 'name': '설정'},
                ]
            },
            {
                'uri': 'manual',
                'name': '매뉴얼',
                'list': [
                    {'uri':'README.md', 'name':'README.md'}
                ]
            },
            {
                'uri': 'log',
                'name': '로그',
            },
        ]
    },
    'setting_menu': None,
    'default_route': 'normal',
}


from plugin import *

P = create_plugin_instance(setting)
PLUGIN_ROOT = os.path.dirname(__file__)
try:
    from .mod_book import ModuleBook
    from .mod_ftv import ModuleFtv
    from .mod_ktv import ModuleKtv
    from .mod_movie import ModuleMovie
    from .mod_music_normal import ModuleMusicNormal
    from .mod_route import ModuleRoute
    from .mod_jav_censored import ModuleJavCensored
    from .mod_jav_uncensored import ModuleJavUncensored
    P.set_module_list([ModuleRoute, ModuleKtv, ModuleMovie, ModuleFtv, ModuleMusicNormal, ModuleBook, ModuleJavCensored,ModuleJavUncensored])
except Exception as e:
    P.logger.error(f'Exception:{str(e)}')
    P.logger.error(traceback.format_exc())

logger = P.logger

