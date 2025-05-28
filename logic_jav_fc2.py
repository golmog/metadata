# -*- coding: utf-8 -*-
# python
import os, sys, traceback, re, json, datetime, time
# third-party
import requests
from flask import request, render_template, jsonify, redirect
# sjva
from framework import app, scheduler, db, socketio, path_data, SystemModelSetting
from plugin import LogicModuleBase
from tool_base import ToolUtil, ToolBaseNotify

# lib_metadata
from lib_metadata.server_util import MetadataServerUtil
from lib_metadata import SiteAvdbs, SiteHentaku
from lib_metadata.site_fc2.site_fc2com import SiteFc2Com
from lib_metadata.site_fc2.site_fc2ppvdb import SiteFc2ppvdb

from .plugin import P

module_name = 'jav_fc2'
logger = P.logger
ModelSetting = P.ModelSetting
package_name = P.package_name

class LogicJavFc2(LogicModuleBase):
    db_default = {
        f'{module_name}_db_version' : '1',
        f'{module_name}_order' : 'fc2ppvdb, fc2com',
        f'{module_name}_title_format' : '[{title}] {tagline}',
        f'{module_name}_use_extras' : 'False',

        # fc2ppvdb 사이트 설정
        f'{module_name}_fc2ppvdb_use_proxy' : 'False',
        f'{module_name}_fc2ppvdb_proxy_url' : '',
        f'{module_name}_fc2ppvdb_use_review' : 'False',
        f'{module_name}_fc2ppvdb_not_found_delay' : '0',

        # fc2com 사이트 설정
        f'{module_name}_fc2com_use_proxy' : 'False',
        f'{module_name}_fc2com_proxy_url' : '',
        f'{module_name}_fc2com_art_count' : '0',

        # 테스트용 코드
        f'{module_name}_fc2com_code' : 'FC2-4690907',
        f'{module_name}_fc2ppvdb_code' : 'FC2-4690907',
        f'{module_name}_total_code' : 'FC2-4690907',
    }

    site_map = {
        'avdbs': SiteAvdbs,
        'hentaku': SiteHentaku,
        'fc2com': SiteFc2Com,
        'fc2ppvdb': SiteFc2ppvdb,
    }


    def __init__(self, P):
        super(LogicJavFc2, self).__init__(P, 'setting')
        self.name = module_name

    def process_menu(self, sub, req):
        arg = ModelSetting.to_dict()
        arg['sub'] = self.name
        try:
            return render_template(f"{package_name}_{self.name}_{sub}.html", arg=arg)
        except Exception as exception:
            logger.error('Exception:%s', exception)
            logger.error(traceback.format_exc())
            return render_template('sample.html', title=f"{package_name} - {self.name} - {sub}")

    def process_ajax(self, sub, req):
        try:
            if sub == 'test':
                code = req.form['code']
                call = req.form['call']

                ModelSetting.set(f'{self.name}_{call}_code', code)

                ret = {}

                match = re.search(r'FC2(?:-PPV)?(?:-|_|\s)?(\d{6,8})(?:-cd\d)?', code.upper())
                if match:
                    processed_code = match.group(1).lstrip('0')
                else:
                    processed_code = code 
                    logger.warning(f"FC2 코드 정규식 매칭 실패: {code}. 원본 코드 사용.")

                if call == 'total':
                    ret['search_results'] = self.search(processed_code, manual=True)
                    if ret['search_results'] and len(ret['search_results']) > 0:
                        first_result_code = ret['search_results'][0].get('code')
                        if first_result_code:
                            ret['first_item_info'] = self.info(first_result_code)

                elif call in self.site_map:
                    SiteClass = self.site_map[call]
                    site_key = SiteClass.site_name

                    # 개별 사이트 테스트를 위한 설정 가져오기
                    current_site_settings = self.__site_settings(site_key)
                    current_site_settings['manual'] = True

                    # 1. 해당 사이트의 search 함수 호출
                    search_data = SiteClass.search(processed_code, **current_site_settings)
                    ret['search'] = search_data

                    # 2. search 결과가 있고, 첫 번째 아이템이 있다면 해당 아이템의 info 함수 호출
                    if search_data and search_data.get('ret') == 'success' and search_data.get('data'):
                        first_search_item = search_data['data'][0]
                        item_code_for_info = first_search_item.get('code')
                        if item_code_for_info:
                            ret['info'] = self.info(item_code_for_info)
                else:
                    ret['ret'] = 'error'
                    ret['log'] = f"알 수 없는 테스트 호출: {call}"

                return jsonify(ret)

        except Exception as e:
            logger.error('Exception:%s', e)
            logger.error(traceback.format_exc())
            return jsonify({'ret':'exception', 'log':str(e)})

    def process_api(self, sub, req):
        try:
            if sub == 'search':
                keyword = req.args.get('keyword')
                manual = (req.args.get('manual') == 'True')

                match = re.search(r'FC2(?:-PPV)?(?:-|_|\s)?(\d{5,8})(?:-cd\d)?', keyword.upper())
                if match:
                    processed_keyword = match.group(1).lstrip('0')
                else:
                    processed_keyword = keyword
                    logger.warning(f"API 검색: FC2 코드 정규식 매칭 실패: {keyword}. 원본 코드 사용.")

                search_results = self.search(processed_keyword, manual=manual)
                return jsonify(search_results)

            elif sub == 'info':
                code = req.args.get('code')

                info_data = self.info(code)
                return jsonify(info_data)

        except Exception as e: 
            logger.error('Exception:%s', e)
            logger.error(traceback.format_exc())
            return jsonify({'ret': 'exception', 'log': str(e)})

    ################################################################
    # 설정 헬퍼 함수들
    ################################################################
    def __site_settings(self, site_name_key):
        settings = {}

        # 프록시 설정
        settings['proxy_url'] = None
        if ModelSetting.get_bool(f'{self.name}_{site_name_key}_use_proxy'):
            settings['proxy_url'] = ModelSetting.get(f'{self.name}_{site_name_key}_proxy_url')

        # 이미지 관련 설정 (JavCensored 설정 따름)
        settings['image_mode'] = ModelSetting.get('jav_censored_image_mode')
        settings['use_image_server'] = ModelSetting.get_bool('jav_censored_use_image_server')
        settings['image_server_url'] = ModelSetting.get('jav_censored_image_server_url')
        settings['image_server_local_path'] = ModelSetting.get('jav_censored_image_server_local_path')

        delay_key = f'{self.name}_{site_name_key}_not_found_delay' 

        # 페이지 없음 딜레이 설정
        if ModelSetting.has_key(delay_key) or delay_key in self.db_default:
            delay_value_str = ModelSetting.get(delay_key)
            if delay_value_str is not None and delay_value_str.strip().isdigit():
                settings['not_found_delay_seconds'] = int(delay_value_str)
            else:
                default_delay_str = self.db_default.get(delay_key, '0') 
                settings['not_found_delay_seconds'] = int(default_delay_str) if default_delay_str.strip().isdigit() else 0
                if delay_value_str is not None:
                    logger.warning(f"'{delay_key}' 값이 유효하지 않음 ('{delay_value_str}'). 기본값 {settings['not_found_delay_seconds']}초 사용.")

        return settings

    def __info_settings(self, site_name_key, code_for_site=None):
        settings = self.__site_settings(site_name_key)

        settings['url_prefix_segment'] = 'jav/fc2'

        art_count_key = f'{self.name}_{site_name_key}_art_count'
        if art_count_key in self.db_default:
            art_count_str = ModelSetting.get(art_count_key)
            if art_count_str is not None and art_count_str.strip().isdigit():
                settings['max_arts'] = int(art_count_str.strip())
            else:
                try:
                    settings['max_arts'] = int(self.db_default[art_count_key])
                except (ValueError, TypeError):
                    settings['max_arts'] = 0
                    logger.warning(f"Value for key '{art_count_key}' ('{art_count_str}') and its db_default ('{self.db_default.get(art_count_key)}') are not valid digits. Defaulting max_arts to 0.")
        else:
            settings['max_arts'] = 0
            logger.debug(f"Key '{art_count_key}' not defined in db_default for {site_name_key}. Defaulting max_arts to 0.")

        use_extras_key = f'{self.name}_{site_name_key}_use_extras'
        if use_extras_key in self.db_default:
            use_extras_str = ModelSetting.get(use_extras_key)
            if use_extras_str is not None:
                settings['use_extras'] = (use_extras_str.lower() == 'true')
            else:
                settings['use_extras'] = (str(self.db_default[use_extras_key]).lower() == 'true')
        else:
            common_use_extras_str = ModelSetting.get(f'{self.name}_use_extras')
            if common_use_extras_str is not None:
                settings['use_extras'] = (common_use_extras_str.lower() == 'true')
            else:
                settings['use_extras'] = False
            logger.debug(f"Key '{use_extras_key}' not defined in db_default for {site_name_key}. Using common/default use_extras: {settings['use_extras']}.")

        use_review_key = f'{self.name}_{site_name_key}_use_review'
        if use_review_key in self.db_default:
            settings['use_review'] = ModelSetting.get_bool(use_review_key)
            # if settings['use_review']:
            #    logger.debug(f"__info_settings for '{site_name_key}': use_review is enabled ({settings['use_review']}) from key '{use_review_key}'.")

        # logger.debug(f"__info_settings for '{site_name_key}': max_arts={settings.get('max_arts')}, use_extras={settings.get('use_extras')}")

        return settings

    ################################################################
    # 핵심 로직: 검색(search) 및 정보조회(info, info2)
    ################################################################
    def search(self, keyword_num_part, manual=False):
        logger.debug(f"FC2 Search 시작 - Keyword(num_part):[{keyword_num_part}] Manual:[{manual}]")

        all_results = []
        # 설정에서 정의된 사이트 검색 순서 (예: 'fc2ppvdb,fc2com')
        site_order_list = ModelSetting.get_list(f'{self.name}_order', ',') 

        for site_key_in_order in site_order_list:
            if site_key_in_order not in self.site_map:
                logger.warning(f"'{site_key_in_order}'는 site_map에 정의되지 않은 사이트입니다. 건너뜁니다.")
                continue

            SiteClass = self.site_map[site_key_in_order]
            logger.debug(f"--- '{SiteClass.site_name}' 사이트에서 검색 시작 ---")

            current_site_settings = self.__site_settings(site_key_in_order)
            current_site_settings['do_trans'] = manual
            current_site_settings['manual'] = manual

            try:
                # SiteClass.search는 {'ret': 'success', 'data': [item_dict, ...]} 형태 반환 가정
                site_search_result = SiteClass.search(keyword_num_part, **current_site_settings)

                if site_search_result and site_search_result.get('ret') == 'success':
                    site_data = site_search_result.get('data', [])
                    if site_data:
                        logger.debug(f"'{SiteClass.site_name}'에서 {len(site_data)}개의 결과 찾음.")
                        # 점수 재조정 또는 사이트별 가중치 적용 등은 여기서 가능
                        # 예: if site_key_in_order != site_order_list[0]: # 첫번째 사이트가 아니면 점수 약간 차감
                        #         for item in site_data: item['score'] = item.get('score', 0) - 1 
                        all_results.extend(site_data)
                    else:
                        logger.debug(f"'{SiteClass.site_name}'에서 결과를 찾지 못했습니다.")
                else:
                    logger.warning(f"'{SiteClass.site_name}' 검색 실패 또는 반환 형식 오류. 응답: {site_search_result}")
            except Exception as e_search:
                logger.error(f"'{SiteClass.site_name}' 검색 중 예외 발생: {e_search}")
                logger.error(traceback.format_exc())

            if not manual and all_results and all_results[0].get('score', 0) > 95 and site_key_in_order == site_order_list[0]:
                logger.debug(f"최우선 사이트 '{site_key_in_order}'에서 고득점({all_results[0].get('score')}) 결과 찾음. 검색 조기 종료.")
                break

        if all_results:
            all_results = sorted(all_results, key=lambda k: k.get('score', 0), reverse=True)

        logger.debug(f"FC2 Search 종료. 총 {len(all_results)}개의 결과 반환.")
        return all_results


    def info(self, code_module_site_id):
        logger.debug(f"FC2 Info 시작 - Code:[{code_module_site_id}]")
        final_info_data = None

        # 1. lib_metadata의 사이트 클래스를 통해 정보 조회
        site_char_from_code = code_module_site_id[1]
        target_site_key = None
        for key, site_class_obj in self.site_map.items():
            if key.startswith('fc2') and hasattr(site_class_obj, 'site_char') and site_class_obj.site_char == site_char_from_code:
                if hasattr(site_class_obj, 'module_char') and site_class_obj.module_char == code_module_site_id[0]:
                    target_site_key = key
                    break

        if target_site_key and target_site_key in self.site_map:
            SiteClass = self.site_map[target_site_key]
            if SiteClass is not None:
                logger.debug(f"'{SiteClass.site_name}'의 info2 메소드 호출 예정. Code: {code_module_site_id}")
                final_info_data = self.info2(code_module_site_id, SiteClass) # self.info2 호출
                if final_info_data:
                    logger.debug(f"'{SiteClass.site_name}'에서 정보 조회 성공 (info2 반환).")
                else:
                    logger.warning(f"'{SiteClass.site_name}'의 info2에서 정보를 반환하지 못함 (None).")
            else:
                logger.error(f"site_map에서 '{target_site_key}'에 해당하는 클래스가 None입니다.")

        else:
            logger.error(f"코드로 적합한 FC2 작품 사이트 클래스를 찾을 수 없음: {code_module_site_id} (site_char: {site_char_from_code})")

        # 2. 배우 정보 처리 (final_info_data가 성공적으로 채워졌을 경우에만 실행)
        if final_info_data and isinstance(final_info_data, dict) and 'actor' in final_info_data and final_info_data['actor']:
            logger.debug(f"FC2 Info: 배우 정보 처리 시작. 총 {len(final_info_data['actor'])}명")
            for actor_entity_dict in final_info_data['actor']:
                if isinstance(actor_entity_dict, dict) and actor_entity_dict.get('originalname'):
                    self.process_fc2_actor_info(actor_entity_dict)
                else:
                    logger.warning(f"FC2 Info: 유효하지 않은 배우 정보 객체 또는 originalname 없음: {actor_entity_dict}")
            logger.debug("FC2 Info: 배우 정보 처리 완료.")

        # 3. 최종 정보가 있으면 타이틀 포맷팅 및 후처리
        if final_info_data and isinstance(final_info_data, dict):
            try:
                title_format_str = ModelSetting.get(f'{self.name}_title_format')
                actor_name_for_format = ""
                if final_info_data.get('actor') and isinstance(final_info_data.get('actor'), list) and final_info_data['actor']:
                    if final_info_data['actor'][0].get('name'):
                        actor_name_for_format = final_info_data['actor'][0]['name']

                format_dict = {
                    'originaltitle': final_info_data.get("originaltitle", ""),
                    'plot': final_info_data.get("plot", ""),
                    'title': final_info_data.get("title", ""), 
                    'sorttitle': final_info_data.get("sorttitle", ""),
                    'country': ', '.join(final_info_data.get("country", []) if isinstance(final_info_data.get("country"), list) else []),
                    'premiered': final_info_data.get("premiered", ""),
                    'year': str(final_info_data.get("year", "")),
                    'tagline': final_info_data.get("tagline", ""),
                    'actor': actor_name_for_format,
                }
                final_info_data['title'] = title_format_str.format(**format_dict)
            except KeyError as e_key:
                logger.error(f"FC2 타이틀 포맷팅 오류: 키 '{e_key}' 없음. 포맷 문자열: '{title_format_str}', 사용 가능 키: {list(final_info_data.keys())}")
            except Exception as e_fmt:
                logger.exception(f"FC2 타이틀 포맷팅 중 일반 예외 발생: {e_fmt}")

            if not ModelSetting.get_bool(f'{self.name}_use_extras'):
                final_info_data['extras'] = []

            # logger.debug(f"FC2 Info 종료. 최종 데이터 반환.")
            return final_info_data

        elif final_info_data is None:
            logger.debug(f"FC2 Info: 최종적으로 반환할 정보가 없습니다 (모든 소스 확인 후). Code: {code_module_site_id}")
            return None
        else: 
            logger.error(f"FC2 Info: 최종 정보가 dict 형태가 아닙니다 (모든 소스 확인 후). Type: {type(final_info_data)}, Value: {final_info_data}, Code: {code_module_site_id}")
            return None


    def info2(self, code_module_site_id, SiteClass_obj):
        site_name = SiteClass_obj.site_name

        current_info_settings = self.__info_settings(site_name, code_module_site_id)
        logger.debug(f"info2: Code={code_module_site_id}, SiteClass={site_name}, Settings={current_info_settings}")

        site_info_response = None
        try:
            site_info_response = SiteClass_obj.info(code_module_site_id, **current_info_settings)
        except Exception as e_site_info:
            logger.error(f"'{site_name}'의 info 메소드 호출 중 예외 발생: {e_site_info}")
            logger.error(traceback.format_exc())
            return None

        # logger.debug(f"'{site_name}'.info 반환 결과: {json.dumps(site_info_response, indent=2)}")

        if site_info_response and site_info_response.get('ret') == 'success':
            entity_data_dict = site_info_response.get('data')
            if entity_data_dict and isinstance(entity_data_dict, dict):
                logger.debug(f"'{site_name}'에서 정보 조회 성공. Data 일부: title='{entity_data_dict.get('title')}', tagline='{entity_data_dict.get('tagline')}'")

                return entity_data_dict
            else:
                logger.warning(f"info2: '{site_name}'.info 반환값의 'data' 필드가 없거나 dict가 아님. Data: {entity_data_dict}")
                return None
        else:
            log_msg = f"info2: '{site_name}'.info 호출 실패 또는 결과 없음."
            if site_info_response:
                log_msg += f" Response ret='{site_info_response.get('ret')}', Response data='{site_info_response.get('data')}'"
            else:
                log_msg += " Response is None."
            logger.warning(log_msg)
            return None


    def _get_actor_site_settings(self, actor_site_key):
        settings = {}
        db_prefix_for_actor_site = f"jav_censored_{actor_site_key}" # JavCensored 설정 네임스페이스 사용

        # 프록시 설정
        settings['proxy_url'] = None
        if ModelSetting.get_bool(f"{db_prefix_for_actor_site}_use_proxy"):
            settings['proxy_url'] = ModelSetting.get(f"{db_prefix_for_actor_site}_proxy_url")

        if actor_site_key == 'avdbs':
            settings['use_local_db'] = ModelSetting.get_bool('jav_censored_avdbs_use_local_db')
            settings['local_db_path'] = ModelSetting.get('jav_censored_avdbs_local_db_path')
            settings['site_name_for_db_query'] = actor_site_key
            settings['db_image_base_url'] = ModelSetting.get('jav_actor_img_url_prefix')
        elif actor_site_key == 'hentaku':
            settings['image_mode'] = ModelSetting.get('jav_censored_image_mode')
            pass

        return settings


    def process_fc2_actor_info(self, actor_entity_dict):
        if not actor_entity_dict or not actor_entity_dict.get('originalname'):
            logger.warning("process_fc2_actor_info: 배우 정보가 없거나 originalname이 없어 처리 중단.")
            return

        original_name_ja = actor_entity_dict.get('originalname')
        logger.debug(f"Processing actor: {original_name_ja}")

        actor_site_order = ModelSetting.get_list("jav_censored_actor_order", ",")

        updated_by_db = False
        for actor_site_key in actor_site_order:
            if actor_site_key not in self.site_map:
                logger.warning(f"배우 정보 사이트 '{actor_site_key}'가 self.site_map에 정의되지 않았습니다.")
                continue

            ActorSiteClass = self.site_map[actor_site_key]
            if ActorSiteClass is None:
                logger.warning(f"self.site_map에서 '{actor_site_key}'에 해당하는 클래스가 None입니다.")
                continue

            actor_site_settings = self._get_actor_site_settings(actor_site_key)
            logger.debug(f"'{original_name_ja}' 배우 정보 조회 시도: 사이트='{actor_site_key}', 설정={actor_site_settings}")

            try:
                if hasattr(ActorSiteClass, 'get_actor_info'):
                    get_info_success = ActorSiteClass.get_actor_info(actor_entity_dict, **actor_site_settings)
                    if get_info_success:
                        logger.info(f"'{original_name_ja}' 배우 정보 업데이트 성공 by '{actor_site_key}'. 이름: {actor_entity_dict.get('name')}, 사진: {bool(actor_entity_dict.get('thumb'))}")
                        updated_by_db = True
                        break 
                    else:
                        logger.debug(f"'{actor_site_key}' 사이트에서 '{original_name_ja}' 배우 정보를 찾지 못하거나 업데이트 실패.")
                else:
                    logger.warning(f"'{actor_site_key}' 사이트 클래스 ({ActorSiteClass.__name__})에 get_actor_info 메소드가 없습니다.")

            except Exception as e_actor_info:
                logger.error(f"'{actor_site_key}' 사이트에서 배우 '{original_name_ja}' 정보 조회 중 예외: {e_actor_info}")
                logger.error(traceback.format_exc())

        if not updated_by_db:
            logger.debug(f"'{original_name_ja}' 배우 정보를 DB에서 업데이트하지 못했습니다.")
