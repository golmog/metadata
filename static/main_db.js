// static/main_db.js

var current_data = null;
var current_person_data = [];
var transfer_timer = null;
var current_search_xhr = null;
var is_initial_search_done = false;

var current_edit_actors = [];
var modal_preview_images = [];
var modal_preview_idx = 0;
var p_modal_preview_images = [];
var p_modal_preview_idx = 0;
var cropperInstance = null;
var custom_upload_payload = null;
var modal_pl_url = '';
var modal_p_url = '';
var active_source_type = 'pl';
var current_crop_target_type = 'meta';

var isVideoAudioRestoring = false;
var modalDropdownHoverTimer = null;

// 모달 내부 드래그/크롭 조작 중 바깥 영역에서 마우스를 놓았을 때 의도치 않게 모달이 닫히는 현상 방지
var isModalDragActive = false;
$(document).on('mousedown', '.modal-dialog, .cropper-container', function() {
    isModalDragActive = true;
});
$(document).on('mouseup', function() {
    setTimeout(function() { isModalDragActive = false; }, 80);
});
$(document).on('click', '.modal', function(e) {
    if (e.target === this && isModalDragActive) {
        e.stopImmediatePropagation();
        e.preventDefault();
        return false;
    }
});

// 유저가 설정한 이미지 서버 주소와 비교하여 순수 로컬 미디어 여부 판정
function isLocalServerMediaUrl(url) {
    if (!url || typeof url !== 'string') return false;
    var clean = url.trim();
    if (!clean) return false;

    if (clean.indexOf('/metadata/normal/') !== -1) {
        return false;
    }

    if (clean.indexOf('/') === 0) {
        return true;
    }

    var serverUrl = (typeof window.image_server_url !== 'undefined' && window.image_server_url)
        ? window.image_server_url.trim().replace(/\/+$/, '')
        : '';
    var ddns = (typeof window.meta_db_ddns !== 'undefined' && window.meta_db_ddns)
        ? window.meta_db_ddns.trim().replace(/\/+$/, '')
        : '';

    if (serverUrl && clean.indexOf(serverUrl) === 0) {
        return true;
    }
    if (ddns && clean.indexOf(ddns) === 0) {
        return true;
    }

    return false;
}

// 프록시 URL 여부와 무관하게 순수 원본 URL을 추출하는 정규화 헬퍼
function getCleanSourceUrl(url) {
    if (!url || typeof url !== 'string') return '';
    var clean = url.trim();
    while ((clean.indexOf('/metadata/normal/jav_image') !== -1 || clean.indexOf('/metadata/normal/jav_image_un') !== -1) && clean.indexOf('url=') !== -1) {
        try {
            var match = clean.match(/[?&]url=([^&]+)/);
            if (match && match[1]) {
                clean = decodeURIComponent(match[1]).trim();
            } else {
                break;
            }
        } catch (e) {
            break;
        }
    }
    return clean;
}

// 브라우저 화면 표시(UI 렌더링) 시 필요한 경우에만 FF 프록시 주소로 감싸는 헬퍼
function getDisplayMediaUrl(url, site, mediaType) {
    if (!url || typeof url !== 'string') return '';
    var clean = url.trim();
    if (!clean) return '';

    if (isLocalServerMediaUrl(clean)) {
        return clean;
    }
    if (clean.indexOf('/metadata/normal/') !== -1) {
        var existingProxyHost = (typeof window.meta_db_ddns === 'string') ? window.meta_db_ddns.replace(/\/+$/, '') : '';
        if (existingProxyHost && clean.charAt(0) === '/') return existingProxyHost + clean;
        return clean;
    }

    if (clean.indexOf('drive.google.com') !== -1 || clean.indexOf('googleusercontent.com') !== -1) {
        return clean;
    }

    var isProxyActive = (typeof window.meta_db_use_ff_proxy !== 'undefined')
        ? window.meta_db_use_ff_proxy
        : false;

    if (isProxyActive && (clean.indexOf('http://') === 0 || clean.indexOf('https://') === 0)) {
        var isUncen = (window.location.pathname.indexOf('jav_uncensored') !== -1);
        var routePath = (mediaType === 'video') ? (isUncen ? 'jav_video_un' : 'jav_video') : (isUncen ? 'jav_image_un' : 'jav_image');
        var siteParam = site ? encodeURIComponent(site) : '';
        var proxyUrl = '/' + package_name + '/normal/' + routePath + '?site=' + siteParam + '&url=' + encodeURIComponent(clean);
        var ddnsHost = (typeof window.meta_db_ddns === 'string') ? window.meta_db_ddns.replace(/\/+$/, '') : '';
        return ddnsHost ? ddnsHost + proxyUrl : proxyUrl;
    }

    return clean;
}

// 크롭 에디터 로드 헬퍼 (중첩 프록시를 완전히 벗겨낸 후 순수 원본 URL에 대해 프록시 생성)
function getSameOriginProxyUrl(url, site) {
    if (!url) return '';
    var rawUrl = getCleanSourceUrl(String(url));
    if (isLocalServerMediaUrl(rawUrl)) {
        return rawUrl;
    }
    if (rawUrl.indexOf('http://') === 0 || rawUrl.indexOf('https://') === 0) {
        var isUncen = (window.location.pathname.indexOf('jav_uncensored') !== -1);
        var routePath = isUncen ? 'jav_image_un' : 'jav_image';
        var siteParam = site ? ('&site=' + encodeURIComponent(site)) : '';
        return '/' + package_name + '/normal/' + routePath + '?url=' + encodeURIComponent(rawUrl) + siteParam + '&mode=';
    }
    return rawUrl;
}

// FlaskFarm AJAX 명령 단일 라우팅 제어 함수
window.globalSendCommand = function(command, arg1, arg2, arg3, callback, options) {
    var pathname = window.location.pathname;
    var current_module_name = (typeof sub !== 'undefined' && sub) ? sub : get_current_module_sub();
    var is_person_page = get_list_context().type === 'person';

    var is_meta_db_system_cmd = (pathname.indexOf('/meta_db/') !== -1) ||
        (typeof command === 'string' && (
            command.indexOf('db_') === 0 ||
            command.indexOf('preview_clip') !== -1 ||
            command === 'get_meta_by_code'
        ));

    var target_module = current_module_name;
    if (pathname.indexOf('/meta_db/') !== -1 || is_meta_db_system_cmd) {
        target_module = 'meta_db';
    }

    var is_person_req = is_person_page || (typeof command === 'string' && command.indexOf('person_') === 0) || (options && options.category === 'PERSON');

    var request_url = is_person_req
        ? ('/' + package_name + '/person_api')
        : (is_meta_db_system_cmd
            ? ('/' + package_name + '/meta_api')
            : ('/' + package_name + '/ajax/' + target_module));

    var postData = {
        sub: target_module,
        command: command || '',
        arg1: (arg1 !== undefined && arg1 !== null) ? arg1 : '',
        arg2: (arg2 !== undefined && arg2 !== null) ? arg2 : '',
        arg3: (arg3 !== undefined && arg3 !== null) ? arg3 : ''
    };
    postData.list_type = is_person_req ? 'person' : 'meta';
    if (options) $.extend(postData, options);

    $.ajax({
        url: request_url,
        type: "POST",
        cache: false,
        data: postData,
        dataType: "json",
        success: function (data) { if (callback) callback(data); },
        error: function (request, status, error) {
            console.warn('[AJAX Error]:', command, status, error);
            if ((command === 'db_delete_preview' || command === 'db_delete_confirm') && callback) {
                callback({ret:'error', uncertain:command === 'db_delete_confirm',
                    msg:command === 'db_delete_confirm' ? '삭제 응답을 받지 못했습니다. 처리가 진행 중이거나 완료되었을 수 있으므로 목록을 새로 고쳐 확인하세요.' : '관련 파일 목록을 불러오지 못했습니다.'});
            }
        }
    });
};

// 현재 활성화된 모듈 서브명 판별 헬퍼
function get_current_module_sub() {
    var pathname = window.location.pathname;
    if (pathname.indexOf('jav_censored') !== -1) return 'jav_censored';
    if (pathname.indexOf('jav_uncensored') !== -1) return 'jav_uncensored';
    if (pathname.indexOf('western') !== -1) return 'western';
    if (pathname.indexOf('meta_db') !== -1) return 'meta_db';
    return typeof sub !== 'undefined' ? sub : 'jav_censored';
}

function get_list_context() {
    var $list = $('#list_div');
    var current_sub = get_current_module_sub();
    var list_type = $list.attr('data-list-type');
    var is_person_page = list_type === 'person' ||
        (list_type !== 'meta' && ($('#search_domain').length > 0 || window.location.pathname.indexOf('person_list') !== -1));

    var sub_to_cat = {
        'jav_censored': 'JAV_CEN',
        'jav_uncensored': 'JAV_UNCEN',
        'western': 'WESTERN'
    };

    var resolved_category = 'JAV_CEN';
    if (is_person_page) {
        resolved_category = 'PERSON';
    } else if ($('#search_category').length > 0 && $('#search_category').val()) {
        resolved_category = $('#search_category').val();
    } else if (current_sub && sub_to_cat[current_sub]) {
        resolved_category = sub_to_cat[current_sub];
    } else {
        resolved_category = $list.attr('data-list-category') || 'JAV_CEN';
    }

    return {
        type: is_person_page ? 'person' : 'meta',
        category: resolved_category,
        domain: $list.attr('data-list-domain') || (current_sub === 'western' ? 'WESTERN' : 'JAV')
    };
}

function get_storage_prefix() {
    var current_sub = get_current_module_sub();
    return get_list_context().type === 'person'
        ? ('person_' + current_sub + '_')
        : (current_sub + '_dblist_');
}

function updateSiteOptionsByCategory(cat, preserveSiteVal) {
    var $siteSelect = $('#search_site');
    if ($siteSelect.length === 0 || $('#search_category').length === 0) return;

    var siteMapByCat = {
        'JAV_CEN': [
            { val: 'all', text: '전체 사이트' },
            { val: 'dmm', text: 'DMM' },
            { val: 'mgstage', text: 'MGStage' },
            { val: 'jav321', text: 'Jav321' },
            { val: 'javbus', text: 'Javbus' },
            { val: 'javdb', text: 'JavDB' }
        ],
        'JAV_UNCEN': [
            { val: 'all', text: '전체 사이트' },
            { val: '1pondo', text: '1Pondo' },
            { val: '10musume', text: '10Musume' },
            { val: 'carib', text: 'Caribbean' },
            { val: 'heyzo', text: 'Heyzo' },
            { val: 'paco', text: 'Pacopacomama' },
            { val: 'fc2com', text: 'FC2' }
        ],
        'WESTERN': [
            { val: 'all', text: '전체 사이트' },
            { val: 'stashdb', text: 'StashDB' },
            { val: 'tpdb', text: 'ThePornDB' }
        ],
        'AV_ALL': [
            { val: 'all', text: '전체 사이트' },
            { val: 'dmm', text: 'DMM' },
            { val: 'mgstage', text: 'MGStage' },
            { val: 'jav321', text: 'Jav321' },
            { val: 'javbus', text: 'Javbus' },
            { val: 'javdb', text: 'JavDB' },
            { val: '1pondo', text: '1Pondo' },
            { val: '10musume', text: '10Musume' },
            { val: 'carib', text: 'Caribbean' },
            { val: 'heyzo', text: 'Heyzo' },
            { val: 'paco', text: 'Pacopacomama' },
            { val: 'fc2com', text: 'FC2' },
            { val: 'stashdb', text: 'StashDB' },
            { val: 'tpdb', text: 'ThePornDB' }
        ]
    };

    var targetCat = (cat || 'AV_ALL').toUpperCase();
    var optionsList = siteMapByCat[targetCat] || siteMapByCat['AV_ALL'];
    var currentVal = preserveSiteVal || $siteSelect.val() || 'all';

    var html = '';
    var isValidVal = false;
    for (var i = 0; i < optionsList.length; i++) {
        var opt = optionsList[i];
        var isSelected = (opt.val === currentVal);
        if (isSelected) isValidVal = true;
        html += '<option value="' + opt.val + '"' + (isSelected ? ' selected' : '') + '>' + opt.text + '</option>';
    }

    $siteSelect.html(html);
    if (!isValidVal) {
        $siteSelect.val('all');
    }
}

function triggerInitialSearchOnce() {
    if (is_initial_search_done) return;
    is_initial_search_done = true;

    var current_sub = get_current_module_sub();
    var list_context = get_list_context();
    var is_person_page = list_context.type === 'person';
    var storage_pfx = get_storage_prefix();

    if ($('#search_category').length > 0) {
        var saved_cat = localStorage.getItem(storage_pfx + 'search_category') || 'AV_ALL';
        $("#search_category").val(saved_cat);
        $('#list_div').attr('data-list-category', saved_cat);
    }

    var saved_word = localStorage.getItem(storage_pfx + 'search_word') || '';
    $("#search_word").val(saved_word);

    var saved_size = localStorage.getItem(storage_pfx + 'page_size') || (is_person_page ? '30' : '10');
    $("#page_size").val(saved_size);

    var saved_status = localStorage.getItem(storage_pfx + 'search_status') || 'all';
    $("#search_status").val(saved_status);

    var saved_order = localStorage.getItem(storage_pfx + 'search_order') || 'desc';
    $("#search_order").val(saved_order);

    if (!is_person_page) {
        var saved_site = localStorage.getItem(storage_pfx + 'search_site') || 'all';
        if ($('#search_category').length > 0) {
            updateSiteOptionsByCategory($("#search_category").val(), saved_site);
        } else {
            $("#search_site").val(saved_site);
        }
    } else {
        var default_dom = 'ALL';
        var saved_domain = localStorage.getItem(storage_pfx + 'search_domain') || default_dom;
        $("#search_domain").val(saved_domain);
    }

    var saved_page = localStorage.getItem(storage_pfx + 'current_page') || '1';
    window.globalRequestSearch(saved_page, false);
}

function render_pagination(paging) {
    var p1 = $('#page1');
    var p2 = $('#page2');
    if (!paging || !paging.total_page || paging.total_page <= 1) {
        p1.html('');
        p2.html('');
        return;
    }

    var cur_page = parseInt(paging.current_page || paging.page || 1, 10);
    var str = '<div class="btn-toolbar justify-content-center my-2" role="toolbar">';
    str += '<div class="btn-group btn-group-sm" role="group">';

    if (paging.prev_page && paging.prev_page > 0) {
        str += '<button type="button" class="btn btn-secondary db-page-btn" data-page="1" title="첫 페이지 (1페이지)">1</button>';
        str += '<button type="button" class="btn btn-secondary db-page-btn" data-page="' + paging.prev_page + '" title="이전 10페이지">&lt;</button>';
    }

    for (var i = paging.start_page; i <= paging.end_page; i++) {
        if (i === cur_page) {
            str += '<button type="button" class="btn btn-primary active font-weight-bold db-page-btn" data-page="' + i + '">' + i + '</button>';
        } else {
            str += '<button type="button" class="btn btn-secondary db-page-btn" data-page="' + i + '">' + i + '</button>';
        }
    }

    if (paging.next_page && paging.next_page > 0) {
        str += '<button type="button" class="btn btn-secondary db-page-btn" data-page="' + paging.next_page + '" title="다음 10페이지">&gt;</button>';
        str += '<button type="button" class="btn btn-secondary db-page-btn" data-page="' + paging.total_page + '" title="마지막 페이지 (' + paging.total_page + '페이지)">' + paging.total_page + '</button>';
    }

    str += '</div></div>';
    p1.html(str);
    p2.html(str);
}

// 플랫폼 표준 검색/페이징 단일 함수
window.globalRequestSearch = function(page, preserveScroll) {
    var current_sub = get_current_module_sub();
    var pathname = window.location.pathname;
    var list_context = get_list_context();
    var is_person_page = list_context.type === 'person';
    var storage_pfx = get_storage_prefix();

    var page_val = (page !== undefined && page !== null && page !== '') 
        ? page.toString() 
        : (localStorage.getItem(storage_pfx + 'current_page') || '1');

    var search_word = $("#search_word").val() || '';
    var page_size = $("#page_size").val() || (is_person_page ? '30' : '10');

    localStorage.setItem(storage_pfx + 'search_word', search_word);
    localStorage.setItem(storage_pfx + 'page_size', page_size);
    localStorage.setItem(storage_pfx + 'current_page', page_val);

    if ($('#search_category').length > 0) {
        var search_category = $("#search_category").val() || 'AV_ALL';
        localStorage.setItem(storage_pfx + 'search_category', search_category);
        $('#list_div').attr('data-list-category', search_category);
    }

    var savedScrollTop = (preserveScroll === true) ? window.scrollY : 0;

    var post_options = {
        page: page_val,
        search_word: search_word,
        page_size: page_size
    };

    if (is_person_page) {
        var search_domain = $("#search_domain").val() || (current_sub === 'western' ? 'WESTERN' : 'JAV');
        var search_status = $("#search_status").val() || 'all';
        var search_order = $("#search_order").val() || 'desc';

        localStorage.setItem(storage_pfx + 'search_domain', search_domain);
        localStorage.setItem(storage_pfx + 'search_status', search_status);
        localStorage.setItem(storage_pfx + 'search_order', search_order);

        post_options.search_domain = search_domain;
        post_options.search_status = search_status;
        post_options.search_order = search_order;
        post_options.category = 'PERSON';
    } else {
        var search_site = $("#search_site").val() || 'all';
        var search_status = $("#search_status").val() || 'all';
        var search_order = $("#search_order").val() || 'desc';
        localStorage.setItem(storage_pfx + 'search_site', search_site);
        localStorage.setItem(storage_pfx + 'search_status', search_status);
        localStorage.setItem(storage_pfx + 'search_order', search_order);

        var target_cat = list_context.category;

        post_options.search_site = search_site;
        post_options.search_status = search_status;
        post_options.search_order = search_order;
        post_options.category = target_cat;
    }

    var list_command = 'web_list';
    var target_module = ((pathname.indexOf('/meta_db/') !== -1) || is_person_page || list_context.type === 'meta')
        ? 'meta_db'
        : sub;
    var postData = {
        sub: target_module,
        command: list_command,
        arg1: '',
        arg2: '',
        arg3: '',
        list_type: list_context.type,
        _ts: Date.now().toString()
    };
    $.extend(postData, post_options);

    $('#page1').html('');
    $('#page2').html('');

    var skeletonCount = Math.min(parseInt(page_size, 10) || 10, 10);
    var skeletonHtml = '';
    for (var sk = 0; sk < skeletonCount; sk++) {
        skeletonHtml += '<div class="row align-items-start py-3 px-1 border-bottom border-secondary" style="opacity: 0.7;">';
        skeletonHtml += '  <div class="col-sm-2 pl-2 pr-1 d-flex"><div class="skeleton-box mr-2" style="width: 20px; height: 20px; border-radius: 3px;"></div><div class="skeleton-box flex-grow-1" style="height: 140px;"></div></div>';
        skeletonHtml += '  <div class="col-sm-8 pl-1 pr-2">';
        skeletonHtml += '    <div class="skeleton-box mb-2" style="height: 22px; width: 65%;"></div>';
        skeletonHtml += '    <div class="skeleton-box mb-2" style="height: 16px; width: 35%;"></div>';
        skeletonHtml += '    <div class="skeleton-box mb-1" style="height: 14px; width: 95%;"></div>';
        skeletonHtml += '    <div class="skeleton-box" style="height: 14px; width: 85%;"></div>';
        skeletonHtml += '  </div>';
        skeletonHtml += '  <div class="col-sm-2 pl-1 pr-2"><div class="skeleton-box mb-2" style="height: 28px;"></div><div class="skeleton-box mb-2" style="height: 28px;"></div><div class="skeleton-box" style="height: 28px;"></div></div>';
        skeletonHtml += '</div>';
    }
    $('#list_div').html(skeletonHtml);

    if (current_search_xhr && typeof current_search_xhr.abort === 'function') {
        current_search_xhr.abort();
        current_search_xhr = null;
    }

    var request_url = is_person_page
        ? ('/' + package_name + '/person_api')
        : (get_list_context().type === 'meta'
            ? ('/' + package_name + '/meta_api')
            : ('/' + package_name + '/ajax/' + target_module));

    current_search_xhr = $.ajax({
        url: request_url,
        type: 'POST',
        cache: false,
        data: postData,
        dataType: 'json',
        success: function(ret){
            current_search_xhr = null;
            if (ret && ret.success) {
                if (typeof ret.meta_db_use_ff_proxy !== 'undefined') {
                    window.meta_db_use_ff_proxy = Boolean(ret.meta_db_use_ff_proxy);
                }
                if (typeof ret.meta_db_ddns !== 'undefined') {
                    window.meta_db_ddns = ret.meta_db_ddns || '';
                }
                if (ret.image_server_url) {
                    window.image_server_url = ret.image_server_url;
                }

                var listData = ret.list || [];
                if (typeof listData === 'string') {
                    try { listData = JSON.parse(listData); } catch (e) { listData = []; }
                }

                try {
                    make_list(listData);
                } catch (renderErr) {
                    $('#list_div').html('<div class="col-12 text-center p-4 text-danger font-weight-bold">렌더링 에러: ' + renderErr.message + '</div>');
                    return;
                }

                if (ret.paging) {
                    render_pagination(ret.paging);
                }

                if (preserveScroll && savedScrollTop > 0) {
                    setTimeout(function() {
                        window.scrollTo({ top: savedScrollTop, behavior: 'instant' });
                    }, 10);
                }
                return;
            }

            var listEl = document.getElementById("list_div");
            if (listEl) {
                listEl.innerHTML = '<div class="col-12 text-center p-4 text-warning">목록을 불러오지 못했습니다.</div>';
            }
            if (typeof notify === 'function') {
                var msg = (ret && (ret.msg || ret.message)) ? (ret.msg || ret.message) : '목록 조회 응답이 올바르지 않습니다.';
                notify(msg, 'warning');
            }
        },
        error: function(request, status, error){
            if (status === 'abort') return;
            current_search_xhr = null;
            var listEl = document.getElementById("list_div");
            if (listEl) {
                listEl.innerHTML = '<div class="col-12 text-center p-4 text-danger">목록 요청 실패: ' + (error || status || '서버 응답 없음') + '</div>';
            }
            if (typeof notify === 'function') {
                notify('목록 요청 실패: ' + (error || status || 'unknown'), 'warning');
            }
        }
    });
};

window.__mainDbGlobalRequestSearch = window.globalRequestSearch;
window.request_search = window.globalRequestSearch;
window.request_db_search = window.globalRequestSearch;
window.request_person_search = window.globalRequestSearch;

// 6개 표준 모달 DOM 동적 주입
function injectDbModals() {
    if ($('#dbEditModal').length === 0) {
        $('body').append(`
        <div class="modal fade" id="dbEditModal" tabindex="-1" role="dialog" aria-hidden="true">
          <div class="modal-dialog modal-xl" role="document">
            <div class="modal-content db-modal-content" style="min-width: 880px; min-height: 580px; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden;">
              <div class="modal-header py-2 bg-dark text-white" style="cursor: move; user-select: none; flex-shrink: 0;">
                <h5 class="modal-title font-weight-bold text-info" id="db_edit_modal_title">메타데이터 편집</h5>
                <button type="button" class="close text-white" data-dismiss="modal" aria-label="Close"><span aria-hidden="true">&times;</span></button>
              </div>
              <div class="modal-body py-3" style="overflow-y: auto; flex: 1 1 auto;">
                <input type="hidden" id="edit_code">
                <input type="hidden" id="edit_idx">
                <div class="row mb-2">
                  <div class="col-md-4 d-flex flex-column p-2 rounded justify-content-between" style="background: #14171a; height: 370px;">
                    <div class="d-flex align-items-center justify-content-center shadow-sm rounded overflow-hidden" style="width: 100%; height: 310px; background: #0a0c0e; position: relative;">
                      <span class="badge shadow-sm font-weight-bold" id="preview_img_type" style="position: absolute; top: 10px; left: 10px; z-index: 5; font-size: 0.78rem; padding: 4px 8px; background: rgba(0, 123, 255, 0.55); color: #ffffff; border: 1px solid rgba(255, 255, 255, 0.3); backdrop-filter: blur(4px);">Poster</span>
                      <div id="preview_modal_spinner" class="spinner-border text-info position-absolute modal-image-spinner" role="status" style="width: 2.2rem; height: 2.2rem; z-index: 4; display: none;">
                        <span class="sr-only">Loading...</span>
                      </div>
                      <img id="preview_modal_img" src="" class="enlarge-img" style="max-width: 100%; max-height: 100%; width: auto; height: auto; object-fit: contain; display: none; cursor: zoom-in;" alt="Preview" title="클릭하여 원본 크게 보기">
                      <div id="preview_no_img" class="text-muted small">No Image</div>
                    </div>
                    <div class="d-flex align-items-center justify-content-between w-100 pt-2 px-1" style="height: 44px;">
                      <button type="button" class="btn btn-sm btn-secondary font-weight-bold px-3 shadow-sm" id="btn_preview_prev" title="이전 이미지">&lt; 이전</button>
                      <span class="small font-weight-bold text-info" id="preview_counter">0 / 0</span>
                      <button type="button" class="btn btn-sm btn-secondary font-weight-bold px-3 shadow-sm" id="btn_preview_next" title="다음 이미지">다음 &gt;</button>
                    </div>
                  </div>

                  <div class="col-md-8 pl-3 pr-2">
                    <div class="form-row mb-2">
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">고유 식별코드 (Code)</label><input type="text" class="form-control form-control-sm bg-light" id="edit_code_view" readonly placeholder="고유 식별코드 없음"></div>
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">표시 품번 (UI Code)</label><input type="text" class="form-control form-control-sm" id="edit_ui_code" placeholder="표시할 품번을 입력하세요"></div>
                    </div>
                    <div class="form-row mb-2">
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">출처 사이트 (Site)</label><input type="text" class="form-control form-control-sm bg-light" id="edit_site" readonly placeholder="출처 사이트 없음"></div>
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">평점 (0.0 ~ 5.0)</label><input type="number" step="0.1" class="form-control form-control-sm" id="edit_rating" placeholder="평점 미등록 (0.0 ~ 5.0)"></div>
                    </div>
                    <div class="form-row mb-2">
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">출시일 (YYYY-MM-DD)</label><input type="text" class="form-control form-control-sm" id="edit_premiered" placeholder="예: 2026-10-16"></div>
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">출시년도 (Year)</label><input type="number" class="form-control form-control-sm" id="edit_year" placeholder="예: 2026"></div>
                    </div>
                    <div class="form-row mb-2">
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">제작사 / 스튜디오</label><input type="text" class="form-control form-control-sm" id="edit_studio" placeholder="제작사 / 스튜디오 이름"></div>
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">시리즈</label><input type="text" class="form-control form-control-sm" id="edit_series" placeholder="시리즈 미등록"></div>
                    </div>
                    <div class="form-row mb-2">
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">감독</label><input type="text" class="form-control form-control-sm" id="edit_director" placeholder="감독 이름"></div>
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">재생시간 (분)</label><input type="number" class="form-control form-control-sm" id="edit_runtime" placeholder="재생시간 (분)"></div>
                    </div>
                    <div class="form-row mb-2">
                      <div class="col-md-12" id="div_edit_genres"><label class="small font-weight-bold mb-1">장르 목록 <span class="text-muted font-weight-normal">(쉼표 구분)</span></label><input type="text" class="form-control form-control-sm" id="edit_genres" placeholder="장르를 쉼표로 구분해 입력하세요"></div>
                      <div class="col-md-6" id="div_edit_mpaa" style="display: none;"><label class="small font-weight-bold mb-1">관람 등급 (MPAA)</label><input type="text" class="form-control form-control-sm" id="edit_mpaa" placeholder="예: 청소년 관람불가"></div>
                    </div>
                  </div>
                </div>

                <div class="form-group mb-2"><label class="small font-weight-bold mb-1">최종 제목 (Title)</label><input type="text" class="form-control form-control-sm" id="edit_title" placeholder="제목을 입력하세요"></div>
                <div class="form-group mb-2"><label class="small font-weight-bold mb-1">원문/번역 부제 (Tagline)</label><input type="text" class="form-control form-control-sm" id="edit_tagline" placeholder="부제 미등록"></div>
                <div class="form-group mb-2"><label class="small font-weight-bold mb-1">줄거리 (Plot)</label><textarea class="form-control form-control-sm" id="edit_plot" rows="3" placeholder="줄거리 미등록"></textarea></div>

                <div class="form-group mb-2 p-2 border rounded bg-light">
                  <div class="d-flex justify-content-between align-items-center mb-1">
                    <label class="small font-weight-bold mb-0 text-dark">출연 배우</label>
                    <button type="button" class="btn btn-sm btn-outline-primary font-weight-bold py-1 px-2" id="btn_open_actor_search_modal">+ 배우 추가</button>
                  </div>
                  <div id="edit_actors_badges" class="d-flex flex-wrap align-items-center"></div>
                </div>

                <div class="form-row mb-2">
                  <div class="col-md-6">
                    <label class="small font-weight-bold mb-1">대표 포스터 URL (Poster)</label>
                    <input type="text" class="form-control form-control-sm" id="edit_poster_url" placeholder="포스터 원본 URL 없음 (https://...)">
                    <input type="text" class="form-control form-control-sm bg-light mt-1" id="edit_poster_relative_path" readonly placeholder="저장된 포스터 상대 경로 없음">
                    <input type="text" class="form-control form-control-sm bg-light mt-1" id="edit_poster_url_final" readonly placeholder="포스터 최종 URL 없음">
                  </div>
                  <div class="col-md-6">
                    <label class="small font-weight-bold mb-1">랜드스케이프 커버 URL (Landscape)</label>
                    <input type="text" class="form-control form-control-sm" id="edit_landscape_url" placeholder="가로 커버 원본 URL 없음 (https://...)">
                    <input type="text" class="form-control form-control-sm bg-light mt-1" id="edit_landscape_relative_path" readonly placeholder="저장된 가로 커버 상대 경로 없음">
                    <input type="text" class="form-control form-control-sm bg-light mt-1" id="edit_landscape_url_final" readonly placeholder="가로 커버 최종 URL 없음">
                  </div>
                </div>

                <div class="form-group mb-2"><label class="small font-weight-bold mb-1">팬아트 이미지 URLs <span class="text-muted font-weight-normal">(줄바꿈으로 구분)</span></label><textarea class="form-control form-control-sm" id="edit_fanarts" rows="2" placeholder="https://... (엔터로 여러 개)"></textarea></div>

                <!-- 예고편 및 프리뷰 클립 분리 관리 영역 -->
                <div class="form-group mb-2 p-2 border rounded" style="background: rgba(255, 255, 255, 0.02); border-color: #343a40 !important;">
                  <!-- 공식 예고편 영역 -->
                  <div class="d-flex justify-content-between align-items-center mb-1">
                    <label class="small font-weight-bold mb-0 text-white">공식 예고편 (Official Trailer)</label>
                    <span class="badge badge-dark border border-secondary text-info btn-play-trailer-modal" id="btn_modal_play_trailer" style="cursor: pointer; font-size: 0.82rem; padding: 3px 8px; display: none;" title="공식 예고편 재생">🎬 공식 트레일러 재생</span>
                  </div>
                  <input type="text" class="form-control form-control-sm" id="edit_trailer_url" placeholder="공식 예고편 스트림 URL이 없습니다. (수동 입력 가능)">

                  <div class="d-flex justify-content-between align-items-center mt-2 pt-2 border-top border-secondary">
                    <span class="small font-weight-bold text-muted">개인 저장소 <span id="badge_preview_status" class="badge badge-secondary">미저장</span></span>
                    <div>
                      <button type="button" class="btn btn-sm btn-outline-success" id="btn_modal_play_saved_trailer" style="display:none">저장된 트레일러 재생</button>
                      <button type="button" class="btn btn-sm btn-outline-success" id="btn_modal_play_preview" style="display:none">프리뷰 재생</button>
                      <button type="button" class="btn btn-sm btn-outline-primary" id="btn_modal_create_preview">프리뷰 수동 생성</button>
                    </div>
                  </div>
                  <div id="div_saved_trailer_box" class="input-group input-group-sm mt-1" style="display:none">
                    <div class="input-group-prepend"><span class="input-group-text">트레일러</span></div>
                    <input class="form-control bg-light" id="edit_saved_trailer_path" readonly placeholder="트레일러 상대 경로 미기록">
                    <input class="form-control bg-light" id="edit_saved_trailer_url" readonly placeholder="트레일러 FF proxy URL 없음">
                    <div class="input-group-append"><button type="button" class="btn btn-outline-danger btn-delete-stored-video" data-video-type="trailer">삭제</button></div>
                  </div>
                  <div id="div_preview_clip_box" class="input-group input-group-sm mt-1" style="display:none">
                    <div class="input-group-prepend"><span class="input-group-text">프리뷰</span></div>
                    <input class="form-control bg-light" id="edit_preview_path" readonly placeholder="프리뷰 상대 경로 미기록">
                    <input class="form-control bg-light" id="edit_preview_url" readonly placeholder="프리뷰 FF proxy URL 없음">
                    <div class="input-group-append"><button type="button" class="btn btn-outline-danger btn-delete-stored-video" data-video-type="preview">삭제</button></div>
                  </div>
                  <div id="div_preview_source_bar" class="p-2 my-2 rounded bg-dark border border-secondary shadow-sm" style="display: none;">
                    <div class="d-flex align-items-center">
                      <span class="small font-weight-bold text-info mr-2 text-nowrap">🎥 원본 파일:</span>
                      <input type="text" class="form-control form-control-sm mr-2" id="input_preview_source_path" placeholder="/mnt/nas/video/ABC-123.mp4 (전체 절대 경로)">
                      <button type="button" class="btn btn-sm btn-secondary mr-1 text-nowrap font-weight-bold py-1 px-2" id="btn_cancel_preview_source">취소</button>
                      <button type="button" class="btn btn-sm btn-primary text-nowrap font-weight-bold py-1 px-3" id="btn_confirm_create_preview">생성 실행</button>
                    </div>
                  </div>

                </div>

                <div class="form-group mb-2">
                  <label class="small font-weight-bold mb-1">정보 출처 URL</label>
                  <div class="input-group input-group-sm">
                    <input type="text" class="form-control bg-light" id="edit_info_url" readonly placeholder="정보 출처 링크가 없습니다.">
                    <div class="input-group-append">
                      <button class="btn btn-outline-info font-weight-bold" type="button" id="btn_open_meta_info_url" title="새 탭에서 정보 출처 페이지 열기">🔗 열기</button>
                    </div>
                  </div>
                </div>
              </div>
              <div class="modal-footer py-2 d-flex justify-content-between flex-wrap" style="flex-shrink: 0;">
                <div class="d-flex align-items-center flex-wrap mb-1 mb-md-0">
                  <button type="button" class="btn btn-sm btn-outline-info font-weight-bold mr-2" id="btn_view_db_json">JSON 보기</button>

                  <div class="dropdown mr-2">
                    <button class="btn btn-sm btn-outline-primary custom-dropdown-toggle font-weight-bold py-1" type="button">
                      이미지 관리
                    </button>
                    <div class="dropdown-menu shadow">
                      <a class="dropdown-item btn_modal_crop text-light font-weight-bold" href="#">✏️ 포스터 크롭 에디터</a>
                      <a class="dropdown-item btn_modal_refresh_image_only text-primary font-weight-bold" href="#" data-media-action="reuse">🔄 이미지/미디어 동기화</a>
                      <a class="dropdown-item btn_modal_refresh_image_only text-warning font-weight-bold" href="#" data-media-action="replace">🔁 이미지/미디어 재처리</a>
                    </div>
                  </div>

                  <div class="dropdown">
                    <button class="btn btn-sm btn-outline-success custom-dropdown-toggle font-weight-bold py-1" type="button">
                      메타 갱신
                    </button>
                    <div class="dropdown-menu shadow">
                      <a class="dropdown-item btn_modal_refresh_missing_in_place text-info font-weight-bold" href="#">🩹 현재 사이트 누락 정보 업데이트</a>
                      <a class="dropdown-item btn_modal_refresh_in_place text-light font-weight-bold" href="#">📌 현재 사이트 제자리 갱신</a>
                      <a class="dropdown-item btn_modal_refresh_auto_search text-success font-weight-bold" href="#">🔍 전체 우선순위 자동 재검색</a>
                    </div>
                  </div>
                </div>

                <div>
                  <button type="button" class="btn btn-secondary" data-dismiss="modal">취소</button>
                  <button type="button" class="btn btn-primary font-weight-bold ml-1" id="btn_save_db_edit">DB에 저장</button>
                </div>
              </div>
            </div>
          </div>
        </div>`);
    }

    if ($('#dbJsonViewModal').length === 0) {
        $('body').append(`
        <div class="modal fade" id="dbJsonViewModal" tabindex="-1" role="dialog" aria-hidden="true">
          <div class="modal-dialog modal-lg" role="document">
            <div class="modal-content border border-secondary shadow-lg" style="min-width: 680px; max-height: 85vh;">
              <div class="modal-header py-2 bg-dark text-white">
                <h6 class="modal-title font-weight-bold text-info" id="db_json_modal_title">메타데이터 JSON 원본 뷰어</h6>
                <button type="button" class="close text-white" data-dismiss="modal" aria-label="Close"><span aria-hidden="true">&times;</span></button>
              </div>
              <div class="modal-body p-2" style="background: #0d1117;">
                <textarea id="db_json_modal_textarea" class="form-control form-control-sm text-light font-monospace" style="background: #0d1117; color: #58a6ff; border: 1px solid #30363d; font-family: monospace; font-size: 0.82rem; height: 60vh; resize: vertical;" readonly></textarea>
              </div>
              <div class="modal-footer py-2 d-flex justify-content-between">
                <button type="button" class="btn btn-sm btn-outline-success font-weight-bold" id="btn_copy_db_json">클립보드 복사</button>
                <button type="button" class="btn btn-sm btn-secondary" data-dismiss="modal">닫기</button>
              </div>
            </div>
          </div>
        </div>`);
    }

    if ($('#videoPreviewModal').length === 0) {
        $('body').append(`
        <div class="modal fade" id="videoPreviewModal" tabindex="-1" role="dialog" aria-hidden="true">
          <div class="modal-dialog modal-dialog-centered" role="document" style="max-width: none; width: auto; margin: auto;">
            <div class="modal-content bg-dark border border-secondary shadow-lg overflow-hidden" style="min-width: 380px; min-height: 260px; max-width: 96vw; max-height: 94vh; width: 920px; height: 560px; resize: both; display: flex; flex-direction: column;">
              <div class="modal-header py-2 bg-dark text-white d-flex justify-content-between align-items-center" style="cursor: move; user-select: none; flex-shrink: 0;">
                <h6 class="modal-title font-weight-bold text-info" id="video_preview_modal_title">예고편 비디오 미리보기</h6>
                <button type="button" class="close text-white" data-dismiss="modal" aria-label="Close"><span aria-hidden="true">&times;</span></button>
              </div>
              <div class="modal-body p-0 d-flex justify-content-center align-items-center flex-grow-1" style="background: #000; overflow: hidden; min-height: 0;">
                <video id="video_preview_player" controls autoplay playsinline style="width: 100%; height: 100%; max-width: 100%; max-height: 100%; object-fit: contain; background: #000; display: block;" src=""></video>
                <div id="video_key_feedback" role="status" aria-live="polite" aria-atomic="true"></div>
              </div>
            </div>
          </div>
        </div>`);
    }

    if ($('#actorSearchModal').length === 0) {
        $('body').append(`
        <div class="modal fade" id="actorSearchModal" tabindex="-1" role="dialog" aria-hidden="true">
          <div class="modal-dialog modal-lg" role="document">
            <div class="modal-content border border-secondary shadow-lg d-flex flex-column" style="min-width: 620px; min-height: 500px; height: 75vh; overflow: hidden;">
              <div class="modal-header py-2 bg-dark text-white" style="cursor: move; user-select: none; flex-shrink: 0;">
                <h6 class="modal-title font-weight-bold text-info">출연 배우 검색 및 추가</h6>
                <button type="button" class="close text-white" data-dismiss="modal" aria-label="Close"><span aria-hidden="true">&times;</span></button>
              </div>
              <div class="modal-body p-3 d-flex flex-column flex-grow-1" style="min-height: 0; overflow-y: auto;">
                <div class="d-flex justify-content-between align-items-center mb-2 flex-wrap">
                  <div class="input-group input-group-sm mr-2 mb-1" style="flex: 1; min-width: 220px;">
                    <input type="text" class="form-control" id="actor_search_kw" placeholder="배우 이름, 일본어/영문 이름, 식별코드(PA106, PS..., PT... 등) 검색">
                    <div class="input-group-append"><button class="btn btn-primary font-weight-bold" id="btn_perform_actor_search">검색</button></div>
                  </div>
                  <div class="d-flex align-items-center mb-1">
                    <div class="custom-control custom-checkbox mr-3">
                      <input type="checkbox" class="custom-control-input" id="actor_search_include_aliases" checked>
                      <label class="custom-control-label font-weight-bold text-muted" for="actor_search_include_aliases" style="cursor: pointer; font-size: 0.92rem;">별칭 포함</label>
                    </div>
                    <select id="actor_search_sort" class="form-control form-control-sm" style="width: 170px;">
                      <option value="match" selected>정확도/일치순</option>
                      <option value="id_asc">코드/ID 번호순 (1-9)</option>
                      <option value="id_desc">코드/ID 번호순 (9-1)</option>
                      <option value="name_asc">이름순 (ㄱ-ㅎ/A-Z)</option>
                      <option value="name_desc">이름 역순 (ㅎ-ㄱ/Z-A)</option>
                    </select>
                  </div>
                </div>
                <div class="table-responsive border rounded flex-grow-1" style="height: 100%; min-height: 220px; overflow-y: auto; background: #1a1d21;">
                  <table class="table table-sm table-hover mb-0 text-white">
                    <thead class="thead-dark small"><tr><th class="text-center" style="width: 80px;">사진</th><th style="width: 180px;">배우 이름</th><th>원문 / 영문 / 별칭</th><th class="text-center" style="width: 90px;">추가</th></tr></thead>
                    <tbody id="actor_search_results_tbody"><tr><td colspan="4" class="text-center text-muted py-4">배우 이름을 입력하여 검색하세요.</td></tr></tbody>
                  </table>
                </div>
              </div>
              <div class="modal-footer py-2" style="flex-shrink: 0;"><button type="button" class="btn btn-sm btn-secondary" data-dismiss="modal">닫기</button></div>
            </div>
          </div>
        </div>`);
    }

    if ($('#imageCropModal').length === 0) {
        $('body').append(`
        <div class="modal fade" id="imageCropModal" tabindex="-1" role="dialog" aria-hidden="true">
          <div class="modal-dialog modal-dialog-centered" role="document" style="max-width: 950px; width: 95%;">
            <div class="modal-content border border-secondary shadow-lg">
              <div class="modal-header d-flex justify-content-between align-items-center py-2 border-bottom border-secondary bg-dark text-white">
                <h5 class="modal-title font-weight-bold text-info" id="crop_modal_title" style="font-size: 1.05em;">크롭 에디터</h5>
                <button type="button" class="close text-white" data-dismiss="modal" aria-label="Close"><span aria-hidden="true">&times;</span></button>
              </div>
              <div class="modal-body p-2">
                <input type="hidden" id="crop_target_code">
                <input type="hidden" id="crop_target_category">
                <input type="hidden" id="crop_person_id">
                <input type="hidden" id="crop_person_domain" value="JAV">
                <input type="hidden" id="crop_has_user_poster">

                <div class="d-flex justify-content-between align-items-center mb-2 px-2 py-1 rounded bg-dark border border-secondary flex-wrap">
                  <div class="d-flex align-items-center flex-nowrap mb-1 mb-md-0">
                    <div class="btn-group btn-group-sm mr-2" role="group" id="crop_meta_sources">
                      <button type="button" class="btn btn-primary font-weight-bold" id="btn_source_pl" title="가로 커버(PL)를 소스로 불러옵니다.">PL</button>
                      <button type="button" class="btn btn-outline-light" id="btn_source_p" title="세로 포스터(P)를 소스로 불러옵니다.">P</button>
                    </div>

                    <div class="btn-group btn-group-sm mr-2" role="group">
                      <button type="button" class="btn btn-info font-weight-bold" id="btn_crop_ratio_lock" title="표준 AV 비율 (1:1.4225)">1.42</button>
                      <button type="button" class="btn btn-outline-light font-weight-bold" id="btn_crop_ratio_portrait" title="3:4 프로필 비율">3:4</button>
                      <button type="button" class="btn btn-outline-light font-weight-bold" id="btn_crop_ratio_square" title="1:1 정사각형 비율">1:1</button>
                      <button type="button" class="btn btn-outline-light font-weight-bold" id="btn_crop_ratio_free" title="자유 비율">자유</button>
                    </div>

                    <div class="btn-group btn-group-sm" role="group">
                      <button type="button" class="btn btn-outline-light" id="btn_crop_rotate_left" title="좌측 90도 회전">좌 90°</button>
                      <button type="button" class="btn btn-outline-light" id="btn_crop_rotate_right" title="우측 90도 회전">우 90°</button>
                      <button type="button" class="btn btn-outline-warning" id="btn_crop_reset">리셋</button>
                    </div>
                  </div>

                  <div class="d-flex align-items-center flex-nowrap">
                    <button type="button" class="btn btn-sm btn-outline-info font-weight-bold py-1 mr-1" id="btn_toggle_crop_url" title="이미지 웹 주소 입력">🔗 URL 로드</button>
                    <button type="button" class="btn btn-sm btn-warning font-weight-bold py-1 mr-1" id="btn_direct_save_user" style="display: none;" title="현재 로드된 이미지를 크롭 없이 직접 저장합니다."><span id="text_direct_save_user">💾 _pl_user 저장</span></button>
                    <label class="btn btn-sm btn-outline-success mb-0 font-weight-bold py-1 mr-1" id="lbl_upload_pl" style="cursor: pointer;" title="가로 커버 업로드"><span>📁 가로(PL)</span><input type="file" id="input_upload_pl" accept="image/*" style="display: none;"></label>
                    <label class="btn btn-sm btn-success mb-0 font-weight-bold py-1" id="lbl_upload_p" style="cursor: pointer;" title="세로 포스터 업로드"><span>🖼️ 세로(P)</span><input type="file" id="input_upload_p" accept="image/*" style="display: none;"></label>
                    <label class="btn btn-sm btn-success mb-0 font-weight-bold py-1" id="lbl_upload_person" style="cursor: pointer; display: none;" title="프로필 사진 업로드"><span>🖼️ 사진 업로드</span><input type="file" id="input_upload_person" accept="image/*" style="display: none;"></label>
                  </div>
                </div>

                <div id="crop_url_bar" class="p-2 mb-2 rounded bg-dark border border-secondary shadow-sm" style="display: none;">
                  <div class="d-flex align-items-center">
                    <span class="small font-weight-bold text-info mr-2 text-nowrap">🔗 URL:</span>
                    <input type="text" class="form-control form-control-sm mr-2" id="input_crop_url" placeholder="https://... 이미지 주소 붙여넣기">
                    <button class="btn btn-sm btn-secondary mr-1 text-nowrap font-weight-bold py-1 px-2" type="button" id="btn_cancel_crop_url">취소</button>
                    <button class="btn btn-sm btn-primary text-nowrap font-weight-bold py-1 px-3" type="button" id="btn_apply_crop_url">로드</button>
                  </div>
                </div>

                <div class="crop-view-wrapper" style="width: 100%; height: 540px; background: #0a0a0a; border-radius: 4px; border: 1px solid #333; overflow: hidden;">
                  <img id="cropper_image" src="" style="display: block; max-width: 100%;" alt="Crop Source">
                </div>
              </div>
              <div class="modal-footer py-2 d-flex justify-content-between border-top border-secondary">
                <span class="text-muted small" id="crop_footer_notice">※ 저장 시 <code>_user.jpg</code> 파일로 생성되어 대표 이미지로 적용됩니다.</span>
                <div>
                  <button type="button" class="btn btn-secondary" data-dismiss="modal">취소</button>
                  <button type="button" class="btn btn-primary font-weight-bold" id="btn_save_crop_result">이미지 저장 (_user)</button>
                </div>
              </div>
            </div>
          </div>
        </div>`);
    }

    if ($('#personEditModal').length === 0) {
        $('body').append(`
        <div class="modal fade" id="personEditModal" tabindex="-1" role="dialog" aria-hidden="true">
          <div class="modal-dialog modal-xl" role="document">
            <div class="modal-content person-modal-content" style="min-width: 880px; min-height: 580px; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden;">
              <div class="modal-header py-2 bg-dark text-white" style="cursor: move; user-select: none; flex-shrink: 0;">
                <h6 class="modal-title font-weight-bold text-info" id="person_modal_title">인물 상세 정보 편집</h6>
                <button type="button" class="close text-white" data-dismiss="modal" aria-label="Close"><span aria-hidden="true">&times;</span></button>
              </div>
              <div class="modal-body py-3" style="overflow-y: auto; flex: 1 1 auto;">
                <input type="hidden" id="p_edit_id">
                <input type="hidden" id="p_edit_thumb">
                <input type="hidden" id="p_edit_selected_primary_url">
                <div class="row mb-2">
                  <div class="col-md-4 d-flex flex-column p-2 rounded justify-content-between" style="background: #14171a; height: 440px;">
                    <div class="d-flex align-items-center justify-content-center shadow-sm rounded overflow-hidden" style="width: 100%; height: 380px; background: #0a0c0e; position: relative;">
                      <span class="badge shadow-sm font-weight-bold" id="p_preview_img_type" style="position: absolute; top: 10px; left: 10px; z-index: 5; font-size: 0.78rem; padding: 4px 8px; background: rgba(0, 0, 0, 0.55); color: #e0e6ed; border: 1px solid rgba(255, 255, 255, 0.15); backdrop-filter: blur(4px);">SERVER</span>
                      <div id="p_modal_spinner" class="spinner-border text-info position-absolute modal-image-spinner" role="status" style="width: 2.2rem; height: 2.2rem; z-index: 4; display: none;">
                        <span class="sr-only">Loading...</span>
                      </div>
                      <img id="p_modal_preview_img" src="" class="enlarge-person-photo" style="max-width: 100%; max-height: 100%; width: auto; height: auto; object-fit: contain; display: none; cursor: zoom-in;" alt="Preview" title="클릭하여 원본 사진 크게 보기">
                      <div id="p_modal_no_img" class="text-muted small">No Photo</div>
                    </div>
                    <div class="d-flex align-items-center justify-content-between w-100 pt-2 px-1" style="height: 44px;">
                      <button type="button" class="btn btn-sm btn-secondary font-weight-bold px-2 shadow-sm" id="btn_p_preview_prev" title="이전 이미지">&lt; 이전</button>
                      <button type="button" class="btn btn-xs btn-outline-info font-weight-bold py-1 px-2" id="btn_set_primary_person_photo" title="현재 보이는 사진을 대표 사진으로 설정">★ 대표 지정</button>
                      <span class="small font-weight-bold text-info" id="p_preview_counter">0 / 0</span>
                      <button type="button" class="btn btn-sm btn-secondary font-weight-bold px-2 shadow-sm" id="btn_p_preview_next" title="다음 이미지">다음 &gt;</button>
                    </div>
                  </div>

                  <div class="col-md-8 pl-3 pr-2">
                    <div class="form-row mb-2">
                      <div class="col-md-4">
                        <label class="small font-weight-bold mb-1">도메인 (Domain)</label>
                        <select class="form-control form-control-sm" id="p_edit_domain">
                          <option value="JAV" selected>JAV</option>
                          <option value="WESTERN">WESTERN</option>
                          <option value="GENERAL">GENERAL</option>
                        </select>
                      </div>
                      <div class="col-md-4">
                        <label class="small font-weight-bold mb-1">성별 (Gender)</label>
                        <select class="form-control form-control-sm" id="p_edit_gender">
                          <option value="female" selected>여성 (Female ♀)</option>
                          <option value="male">남성 (Male ♂)</option>
                          <option value="trans">트랜스 (Trans)</option>
                        </select>
                      </div>
                      <div class="col-md-4">
                        <label class="small font-weight-bold mb-1">고유 식별코드 (Code/ID)</label>
                        <input type="text" class="form-control form-control-sm bg-light" id="p_edit_idx" readonly placeholder="고유 식별코드">
                      </div>
                    </div>

                    <div class="form-row mb-2">
                      <div class="col-md-4"><label class="small font-weight-bold mb-1">원문 이름 (Name ORG)</label><input type="text" class="form-control form-control-sm" id="p_edit_name_org" placeholder="원문 표기 (한자/가나/영문)"></div>
                      <div class="col-md-4"><label class="small font-weight-bold mb-1">한국어 표기 (Name KO)</label><input type="text" class="form-control form-control-sm" id="p_edit_name_ko" placeholder="번역/한국어 표기명"></div>
                      <div class="col-md-4"><label class="small font-weight-bold mb-1">영문 이름 (Name EN)</label><input type="text" class="form-control form-control-sm" id="p_edit_name_en" placeholder="English Name"></div>
                    </div>

                    <div class="form-row mb-2">
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">소속사 / 에이전시</label><input type="text" class="form-control form-control-sm" id="p_edit_agency" placeholder="소속사명"></div>
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">생년월일 (Birth)</label><input type="text" class="form-control form-control-sm" id="p_edit_birth" placeholder="YYYY-MM-DD"></div>
                    </div>

                    <div class="form-row mb-2">
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">신장 (Height, cm)</label><input type="number" class="form-control form-control-sm" id="p_edit_height" placeholder="165"></div>
                      <div class="col-md-6"><label class="small font-weight-bold mb-1">혈액형 (Blood)</label><input type="text" class="form-control form-control-sm" id="p_edit_blood" placeholder="A형 / B형 / O형 ..."></div>
                    </div>

                    <div class="form-group mb-2">
                      <label class="small font-weight-bold mb-1">취미 / 특기</label>
                      <input type="text" class="form-control form-control-sm" id="p_edit_hobby" placeholder="취미 및 특기 사항">
                    </div>

                    <div class="form-group mb-2">
                      <label class="small font-weight-bold mb-1">별칭 / 예명 목록 <span class="text-muted font-weight-normal">(쉼표 구분)</span></label>
                      <input type="text" class="form-control form-control-sm" id="p_edit_aliases" placeholder="예명 또는 별칭">
                    </div>

                    <div id="p_edit_av_spec_div" class="form-row mb-2 p-2 rounded" style="background: rgba(255,255,255,0.03); border: 1px dashed rgba(255,255,255,0.1);">
                      <div class="col-md-5"><label class="small font-weight-bold text-info mb-1">신체 사이즈 (B-W-H)</label><input type="text" class="form-control form-control-sm" id="p_edit_body_size" placeholder="예: B85-W58-H86"></div>
                      <div class="col-md-3"><label class="small font-weight-bold text-info mb-1">브라 컵 (Cup)</label><input type="text" class="form-control form-control-sm" id="p_edit_bra_size" placeholder="예: E컵"></div>
                      <div class="col-md-4"><label class="small font-weight-bold text-info mb-1">데뷔일 (Debut)</label><input type="text" class="form-control form-control-sm" id="p_edit_debut" placeholder="YYYY-MM-DD"></div>
                    </div>
                  </div>
                </div>

                <div class="form-row mb-2" id="p_gds_image_fields">
                  <div class="col-md-6"><label class="small font-weight-bold text-muted" id="lbl_p_edit_local_path">파일 상대 경로 (GDS)</label><input class="form-control form-control-sm" id="p_edit_local_img_path" placeholder="GDS 파일 상대 경로 없음"></div>
                  <div class="col-md-6"><label class="small font-weight-bold text-muted" id="lbl_p_gds_thumb_url">GDS 썸네일 URL</label><input class="form-control form-control-sm bg-light" id="p_edit_gdrive_thumb_url" readonly placeholder="GDS 썸네일 URL 없음"></div>
                </div>
                <input type="hidden" id="p_edit_google_fileid">
                <div class="form-row mb-2">
                  <div class="col-md-6"><label class="small text-muted" id="lbl_p_local_path">파일 상대 경로 (로컬)</label><input class="form-control form-control-sm bg-light" id="p_edit_storage_local_path" readonly placeholder="로컬 이미지 상대 경로 없음"></div>
                  <div class="col-md-6"><label class="small text-muted" id="lbl_p_official_local_url">이미지 서버 URL</label><input class="form-control form-control-sm bg-light" id="p_edit_official_local_url" readonly placeholder="이미지 서버 URL 없음"></div>
                </div>
                <div class="form-row mb-2">
                  <div class="col-md-6"><label class="small text-muted" id="lbl_p_remote_path">파일 상대 경로 (구글 드라이브)</label><input class="form-control form-control-sm bg-light" id="p_edit_storage_remote_path" readonly placeholder="구글 드라이브 이미지 상대 경로 없음"></div>
                  <div class="col-md-6"><label class="small text-muted" id="lbl_p_official_gdrive_url">구글 드라이브 썸네일 URL</label><input class="form-control form-control-sm bg-light" id="p_edit_official_gdrive_url" readonly placeholder="구글 드라이브 썸네일 URL 없음"></div>
                </div>
                <input type="hidden" id="p_edit_custom_user_google_fileid">

                <div class="form-group mb-2">
                  <label class="small font-weight-bold mb-1">프로필 이미지 URLs <span class="text-muted font-weight-normal">(줄바꿈으로 구분)</span></label>
                  <textarea class="form-control form-control-sm" id="p_edit_site_img_urls" rows="2" placeholder="https://... (엔터로 여러 개)"></textarea>
                </div>

                <div class="form-group mb-2">
                  <label class="small font-weight-bold mb-1">정보 출처 URL</label>
                  <div class="input-group input-group-sm">
                    <input type="text" class="form-control bg-light" id="p_edit_info_url" readonly placeholder="정보 출처 링크가 없습니다.">
                    <div class="input-group-append">
                      <button class="btn btn-outline-info font-weight-bold" type="button" id="btn_open_person_info_url" title="새 탭에서 정보 출처 페이지 열기">🔗 열기</button>
                    </div>
                  </div>
                </div>

                <div id="p_merged_sub_actors_wrapper" class="form-group mb-2 p-2 rounded" style="background: rgba(0, 123, 255, 0.05); border: 1px solid rgba(0, 123, 255, 0.2); display: none;">
                  <div class="d-flex justify-content-between align-items-center mb-2">
                    <div>
                      <span class="badge badge-primary font-weight-bold mr-1">통합 인물</span>
                      <span class="small font-weight-bold text-info" id="p_merged_sub_count_text">총 0개 ID 병합됨</span>
                    </div>
                    <button type="button" class="btn btn-sm btn-outline-secondary font-weight-bold py-0 px-2" id="btn_toggle_sub_actors" style="font-size: 0.78rem;">상세 접기/펼치기</button>
                  </div>
                  <div id="p_merged_sub_badges_container" class="d-flex flex-wrap align-items-center mb-2"></div>
                  <div id="p_merged_sub_table_collapse" class="table-responsive border rounded" style="background: #16181b;">
                    <table class="table table-sm table-dark table-hover mb-0" style="font-size: 0.84rem;">
                      <thead>
                        <tr>
                          <th class="text-center" style="width: 50px;">사진</th>
                          <th style="width: 110px;">식별 ID</th>
                          <th>원문명 / 한국어명</th>
                          <th class="text-center" style="width: 90px;">상세</th>
                          <th class="text-center" style="width: 180px;">관리</th>
                        </tr>
                      </thead>
                      <tbody id="p_merged_sub_tbody"></tbody>
                    </table>
                  </div>
                </div>

                <div class="form-group mb-0 p-2 rounded" style="background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.08);">
                  <div class="d-flex justify-content-between align-items-center mb-1">
                    <label class="small font-weight-bold text-white mb-0">소장 출연작 목록 <span class="badge badge-success small ml-1" id="p_modal_works_count">0편</span></label>
                    <div>
                      <button type="button" class="btn btn-xs btn-outline-primary font-weight-bold py-1 px-2 mr-1" id="btn_sync_actor_works" title="현재 등록된 소장 출연작을 실제 소장 메타와 대조하여 오매칭을 정제하고 최신 제목으로 갱신합니다.">🔄 출연작 검증/갱신</button>
                      <button type="button" class="btn btn-xs btn-outline-success font-weight-bold py-1 px-2" id="btn_search_actor_works" title="새 탭에서 이 배우의 작품 목록 검색 열기">🔍 작품 목록에서 검색 (새 탭)</button>
                    </div>
                  </div>
                  <div id="p_modal_works_container" class="d-flex flex-column align-items-stretch w-100" style="min-height: 32px;">
                    <span class="text-muted small py-1">등록된 소장 출연작이 없습니다. (작품 메타데이터 등록 시 자동 연계)</span>
                  </div>
                </div>
              </div>

              <div class="modal-footer py-2 d-flex justify-content-between flex-wrap" style="flex-shrink: 0;">
                <div class="d-flex align-items-center flex-wrap mb-1 mb-md-0">
                  <button type="button" class="btn btn-sm btn-outline-info font-weight-bold mr-2" id="btn_view_person_json">JSON 보기</button>
                  <button type="button" class="btn btn-sm btn-outline-success font-weight-bold" id="btn_open_person_crop_modal">사진 편집</button>
                  <button type="button" class="btn btn-sm btn-outline-primary font-weight-bold mr-2" id="btn_person_refresh_site" style="display: none;" title="원본 사이트(StashDB, TPDB)에서 최신 정보를 다시 조회하여 갱신합니다.">🔄 사이트 정보 갱신</button>
                  <div class="btn-group ml-2">
                    <button type="button" class="btn btn-sm btn-outline-warning dropdown-toggle font-weight-bold" data-toggle="dropdown">사용자 데이터 초기화</button>
                    <div class="dropdown-menu dropdown-menu-right">
                      <button type="button" class="dropdown-item btn_person_reset_user" data-reset-type="text">텍스트 편집 초기화</button>
                      <button type="button" class="dropdown-item btn_person_reset_user" data-reset-type="image">사용자 이미지 초기화</button>
                      <div class="dropdown-divider"></div>
                      <button type="button" class="dropdown-item text-danger btn_person_reset_user" data-reset-type="all">전체 사용자 데이터 초기화</button>
                    </div>
                  </div>
                </div>
                <div>
                  <button type="button" class="btn btn-sm btn-secondary" data-dismiss="modal">취소</button>
                  <button type="button" class="btn btn-sm btn-primary font-weight-bold ml-1" id="btn_person_save">저장</button>
                </div>
              </div>

            </div>
          </div>
        </div>`);
    }
}

function getOrCreateModal(baseModalId) {
    var $base = $(baseModalId);
    if (!$base.hasClass('show')) {
        return $base;
    }

    var cloneId = baseModalId.replace('#', '') + '_layer_' + Date.now();
    var isPerson = baseModalId.indexOf('person') !== -1;
    var contentClass = isPerson ? 'person-modal-content' : 'db-modal-content';

    var $clone = $base.clone(false).attr('id', cloneId).addClass('dynamic-modal-clone');
    $clone.removeClass('show in').css('display', 'none').removeAttr('aria-modal').attr('aria-hidden', 'true');
    $clone.find('.modal-content').addClass(contentClass);

    var $baseDialog = $base.find('.modal-dialog');
    var $cloneDialog = $clone.find('.modal-dialog');
    var $baseContent = $base.find('.modal-content');
    var $cloneContent = $clone.find('.modal-content');

    if ($baseDialog.length && $baseDialog[0].style.position === 'fixed') {
        var rect = $baseDialog[0].getBoundingClientRect();
        var newLeft = Math.min(window.innerWidth - 300, rect.left + 25);
        var newTop = Math.min(window.innerHeight - 200, rect.top + 25);
        $cloneDialog.css({
            position: 'fixed',
            margin: '0',
            left: newLeft + 'px',
            top: newTop + 'px',
            width: $baseContent.outerWidth() + 'px',
            maxWidth: 'none',
            transform: 'none'
        });
        $cloneContent.css({
            width: $baseContent.outerWidth() + 'px',
            height: $baseContent.outerHeight() + 'px'
        });
    }

    $clone.on('click', '[data-dismiss="modal"]', function(e){
        e.preventDefault();
        e.stopPropagation();
        $clone.modal('hide');
    });

    $('body').append($clone);
    $clone.on('hidden.bs.modal', function(){
        $(this).remove();
    });
    return $clone;
}

function update_modal_image_preview($modal) {
    $modal = $modal || $('#dbEditModal');
    var images = $modal.data('preview_images') || modal_preview_images || [];
    var idx = ($modal.data('preview_idx') !== undefined) ? parseInt($modal.data('preview_idx'), 10) : 0;

    if (idx < 0) idx = 0;
    if (images.length > 0 && idx >= images.length) idx = images.length - 1;

    modal_preview_images = images;
    modal_preview_idx = idx;
    $modal.data('preview_idx', idx);

    var $img = $modal.find('#preview_modal_img');
    var $noImg = $modal.find('#preview_no_img');
    var $spinner = $modal.find('#preview_modal_spinner');
    var $counter = $modal.find('#preview_counter');
    var $badge = $modal.find('#preview_img_type');
    var $btnPrev = $modal.find('#btn_preview_prev');
    var $btnNext = $modal.find('#btn_preview_next');

    if (images.length === 0) {
        $spinner.hide();
        $img.attr('src', '').attr('data-src', '').removeData('src').hide();
        $noImg.show();
        $counter.text('0 / 0');
        renderImageOverlayBadge($badge, {type: 'No Image'});
        $btnPrev.prop('disabled', true);
        $btnNext.prop('disabled', true);
    } else {
        var cur = images[idx];
        $noImg.hide();

        if ($img.attr('src') === cur.url && $img[0] && $img[0].complete && $img[0].naturalWidth > 0) {
            $spinner.hide();
            $img.css('opacity', '1').show();
        } else {
            $spinner.stop(true, true).fadeIn(120);
            $img.css('opacity', '0.55').show();

            $img.off('load.prev_img error.prev_img').on('load.prev_img', function() {
                $spinner.stop(true, true).fadeOut(140);
                $noImg.hide();
                $img.css('opacity', '1').show();
            }).on('error.prev_img', function() {
                $spinner.stop(true, true).hide();
                $img.hide();
                $noImg.show();
            });

            $img.attr('src', cur.url).attr('data-src', cur.url).data('src', cur.url);

            if ($img[0] && $img[0].complete && $img[0].naturalWidth > 0) {
                $spinner.hide();
                $img.css('opacity', '1');
            }
        }

        if (typeof preloadAdjacentModalImages === 'function') preloadAdjacentModalImages(images, idx);

        $counter.text((idx + 1) + ' / ' + images.length);
        renderImageOverlayBadge($badge, cur, 'Poster');

        $btnPrev.prop('disabled', images.length <= 1);
        $btnNext.prop('disabled', images.length <= 1);
    }
}

function update_p_modal_image_preview($modal) {
    $modal = $modal || $('#personEditModal');
    var images = $modal.data('p_preview_images') || p_modal_preview_images || [];
    var idx = ($modal.data('p_preview_idx') !== undefined) ? parseInt($modal.data('p_preview_idx'), 10) : 0;

    if (idx < 0) idx = 0;
    if (images.length > 0 && idx >= images.length) idx = images.length - 1;

    p_modal_preview_images = images;
    p_modal_preview_idx = idx;
    $modal.data('p_preview_idx', idx);

    var $img = $modal.find('#p_modal_preview_img');
    var $noImg = $modal.find('#p_modal_no_img');
    var $spinner = $modal.find('#p_modal_spinner');
    var $counter = $modal.find('#p_preview_counter');
    var $pBadge = $modal.find('#p_preview_img_type');
    var $btnPrev = $modal.find('#btn_p_preview_prev');
    var $btnNext = $modal.find('#btn_p_preview_next');

    if (images.length === 0) {
        $spinner.hide();
        $img.attr('src', '').attr('data-src', '').removeData('src').hide();
        $noImg.show();
        $counter.text('0 / 0');
        renderImageOverlayBadge($pBadge, {type: 'No Photo'});
        $btnPrev.prop('disabled', true);
        $btnNext.prop('disabled', true);
    } else {
        var cur = images[idx];
        $noImg.hide();

        if ($img.attr('src') === cur.url && $img[0] && $img[0].complete && $img[0].naturalWidth > 0) {
            $spinner.hide();
            $img.css('opacity', '1').show();
        } else {
            $spinner.stop(true, true).fadeIn(120);
            $img.css('opacity', '0.55').show();

            $img.off('load.p_prev_img error.p_prev_img').on('load.p_prev_img error.p_prev_img', function() {
                $spinner.stop(true, true).fadeOut(140);
                $img.css('opacity', '1');
            });

            $img.attr('src', cur.url).attr('data-src', cur.url).data('src', cur.url);

            if ($img[0] && $img[0].complete && $img[0].naturalWidth > 0) {
                $spinner.hide();
                $img.css('opacity', '1');
            }
        }

        if (typeof preloadAdjacentModalImages === 'function') preloadAdjacentModalImages(images, idx);

        $counter.text((idx + 1) + ' / ' + images.length);
        renderImageOverlayBadge($pBadge, cur, 'Photo');

        $btnPrev.prop('disabled', images.length <= 1);
        $btnNext.prop('disabled', images.length <= 1);
    }
}

function render_actor_badges($modal) {
    $modal = $modal || $('#dbEditModal');
    var actors = $modal.data('edit_actors') || current_edit_actors || [];
    current_edit_actors = actors;
    var str = '';
    if (actors.length === 0) {
        str = '<span class="text-muted small align-self-center mr-2">등록된 출연 배우가 없습니다.</span>';
    } else {
        for (var i = 0; i < actors.length; i++) {
            var a = actors[i];
            var display_name = (typeof a === 'object') ? (a.name_ko || a.name_org || '') : a;
            var sub_name = (typeof a === 'object' && a.name_org && a.name_org !== display_name) ? ' (' + a.name_org + ')' : '';
            var actor_idx = (typeof a === 'object') ? (a.actor_idx || a.person_idx || '') : '';

            var gender = (typeof a === 'object') ? (a.gender || (a.extra_info && a.extra_info.gender) || '') : '';
            var g_lower = String(gender).toLowerCase().trim();
            var gender_icon = '';

            if (g_lower === 'female' || g_lower === 'f' || g_lower === '여' || g_lower === '여성') {
                gender_icon = '<span class="font-weight-bold mr-1" style="color: #ff6b81; font-size: 1.1em; line-height: 1;" title="여성">♀</span>';
            } else if (g_lower === 'male' || g_lower === 'm' || g_lower === '남' || g_lower === '남성') {
                gender_icon = '<span class="font-weight-bold mr-1" style="color: #45aaf2; font-size: 1.1em; line-height: 1;" title="남성">♂</span>';
            }

            str += '<span class="badge badge-secondary mr-2 mb-1 p-1 font-weight-normal d-inline-flex align-items-center" style="font-size: 0.9em; background-color: #2b3035; border: 1px solid #444c56;">';
            str += gender_icon;
            str += '  <span class="actor-badge-clickable" data-actor-name="' + (a.name_org || display_name) + '" data-actor-idx="' + actor_idx + '" title="클릭하여 배우 상세 정보 열기">' + display_name + sub_name + '</span>';
            str += '  <a href="#" class="text-white ml-2 btn_remove_actor font-weight-bold" data-idx="' + i + '" style="text-decoration: none; padding: 0 4px;" title="삭제">&times;</a>';
            str += '</span>';
        }
    }
    $modal.find('#edit_actors_badges').html(str);
}

function formatActorLocalUrl(local_path, domain) {
    if (!local_path || typeof local_path !== 'string') return '';
    var clean = local_path.trim();
    if (!clean || clean.toLowerCase() === 'null' || clean.toLowerCase() === 'none') return '';
    if (clean.startsWith('http://') || clean.startsWith('https://')) return clean;

    var parts = clean.replace(/\\/g, '/').split('/').filter(Boolean);
    var subRel = (parts.length >= 2) ? (parts[parts.length - 2] + '/' + parts[parts.length - 1]) : parts.join('/');

    var dom = String(domain || 'JAV').toUpperCase();
    var actorFolder = (dom === 'WESTERN') ? 'western/actors' : 'jav/actors';
    return '/images/' + actorFolder + '/' + subRel;
}

function normalizeDbEditPayload(row, srcJson, $modal) {
    var payload = srcJson && typeof srcJson === 'object' ? JSON.parse(JSON.stringify(srcJson)) : {};
    var $m = $modal || $('#dbEditModal');
    var code = ($m.find('#edit_code').val() || row.code || '').trim();

    payload.extra_info = (srcJson && srcJson.extra_info) ? JSON.parse(JSON.stringify(srcJson.extra_info)) : (row.extra_info || {});
    delete payload.extra_info.actor_cache;

    payload.spec_data = (srcJson && srcJson.spec_data) ? JSON.parse(JSON.stringify(srcJson.spec_data)) : (row.spec_data || {});
    payload.original = (srcJson && srcJson.original) ? JSON.parse(JSON.stringify(srcJson.original)) : (row.original || {});
    if (!payload.original.thumb) payload.original.thumb = {};

    var edited_poster = $m.find('#edit_poster_url').val().trim();
    var edited_landscape = $m.find('#edit_landscape_url').val().trim();
    var edited_trailer = $m.find('#edit_trailer_url').val().trim();
    var fanarts_text = $m.find('#edit_fanarts').val().split(/\r?\n/).map(function(v){ return v.trim(); }).filter(Boolean);

    var final_p = $m.find('#edit_poster_url_final').val().trim();
    var final_pl = $m.find('#edit_landscape_url_final').val().trim();

    payload.code = code;
    payload.category = row.category || payload.category || get_list_context().category || 'JAV_CEN';
    payload.ui_code = $m.find('#edit_ui_code').val().trim() || code;
    payload.site = $m.find('#edit_site').val().trim() || (row.site || '');
    payload.title = $m.find('#edit_title').val().trim() || row.title || code;
    payload.tagline = $m.find('#edit_tagline').val().trim();
    payload.originaltitle = row.originaltitle || payload.originaltitle || code;
    payload.plot = $m.find('#edit_plot').val().trim();
    payload.mpaa = $m.find('#edit_mpaa').val().trim();
    payload.director = $m.find('#edit_director').val().trim();
    payload.studio = $m.find('#edit_studio').val().trim();
    payload.series = $m.find('#edit_series').val().trim();
    payload.premiered = $m.find('#edit_premiered').val().trim();

    var ratingInputVal = parseFloat($m.find('#edit_rating').val());
    payload.rating = isNaN(ratingInputVal) ? 0.0 : ratingInputVal;
    payload.rating_votes = (srcJson && srcJson.rating_votes) ? srcJson.rating_votes : (row.rating_votes || 0);
    payload.ratings = payload.rating > 0 ? [{
        name: payload.site || 'dmm',
        value: payload.rating,
        votes: payload.rating_votes,
        max: 5
    }] : [];

    var yearVal = parseInt($m.find('#edit_year').val(), 10);
    payload.year = isNaN(yearVal) ? 0 : yearVal;

    var runtimeVal = parseInt($m.find('#edit_runtime').val(), 10);
    payload.runtime = isNaN(runtimeVal) ? 0 : runtimeVal;

    payload.thumb = [];
    if (final_pl) payload.thumb.push({ aspect: 'landscape', value: final_pl, site: payload.site });
    if (final_p) payload.thumb.push({ aspect: 'poster', value: final_p, site: payload.site });

    if (edited_poster && !isLocalServerMediaUrl(edited_poster) && edited_poster.indexOf('/metadata/normal/') === -1) {
        payload.original.thumb.poster = edited_poster;
    } else if (!edited_poster) {
        payload.original.thumb.poster = '';
    }

    if (edited_landscape && !isLocalServerMediaUrl(edited_landscape) && edited_landscape.indexOf('/metadata/normal/') === -1) {
        payload.original.thumb.landscape = edited_landscape;
    } else if (!edited_landscape) {
        payload.original.thumb.landscape = '';
    }

    if (edited_trailer) {
        payload.original.extras = [{
            content_url: edited_trailer,
            content_type: 'trailer'
        }];
        payload.extras = [{
            mode: 'mp4',
            title: payload.title || payload.tagline,
            content_url: getDisplayMediaUrl(edited_trailer, payload.site, 'video'),
            content_type: 'trailer'
        }];
    } else {
        payload.original.extras = [];
        payload.extras = [];
    }

    payload.original.fanart = fanarts_text;

    // 공식 원본 예고편은 원문 백업(original.extras)에 보존
    if (edited_trailer) {
        payload.original.extras = [{
            content_url: edited_trailer,
            content_type: 'trailer'
        }];
    } else {
        payload.original.extras = [];
    }

    // 최종 재생용 extras 구성: 저장된 비디오 스트림 주소(프리뷰 또는 저장된 트레일러)를 최우선 적용
    var activeStreamUrl = $m.find('#edit_preview_url').val() ? $m.find('#edit_preview_url').val().trim() : '';

    if (activeStreamUrl) {
        var videoTitlePrefix = (payload.extra_info && payload.extra_info.preview_clip) ? '[Preview] ' : '';
        payload.extras = [{
            mode: 'mp4',
            title: videoTitlePrefix + (payload.title || payload.tagline || payload.originaltitle),
            content_url: activeStreamUrl,
            content_type: 'trailer'
        }];

        if (payload.extra_info && payload.extra_info.preview_clip) {
            payload.extra_info.preview_clip.stream_url = activeStreamUrl;
        }
        if (payload.extra_info && payload.extra_info.trailer_video) {
            payload.extra_info.trailer_video.stream_url = activeStreamUrl;
        }
    } else if (edited_trailer) {
        // 저장된 영상이 없는 경우에만 원본 트레일러 URL을 폴백으로 적용
        payload.extras = [{
            mode: 'mp4',
            title: payload.title || payload.tagline || payload.originaltitle,
            content_url: getDisplayMediaUrl(edited_trailer, payload.site, 'video'),
            content_type: 'trailer'
        }];
    } else {
        payload.extras = [];
    }

    var preservedInfoUrl = (srcJson && srcJson.extra_info && srcJson.extra_info.info_url) ||
                           (row && row.info_url) ||
                           $m.find('#edit_info_url').val().trim() || '';
    payload.extra_info.info_url = preservedInfoUrl;

    var genreText = $m.find('#edit_genres').val().trim();
    if (genreText) {
        payload.genre = genreText.split(',').map(function(g){ return g.trim(); }).filter(Boolean);
    } else {
        payload.genre = [];
    }

    payload.tag = Array.isArray(srcJson.tag) ? srcJson.tag.slice() : (Array.isArray(row.tag) ? row.tag.slice() : []);

    var modalActors = $m.data('edit_actors') || current_edit_actors || [];
    payload.actor = modalActors.map(function(a){
        if (typeof a === 'string') return { name: a, name_org: a, name_ko: '', name_en: '', gender: '', role: '출연' };
        var dName = a.name || a.name_ko || a.name_org || '';
        return {
            name: dName,
            name_org: a.name_org || '',
            name_ko: a.name_ko || '',
            name_en: a.name_en || '',
            thumb: a.thumb || '',
            actor_idx: a.actor_idx || a.person_idx || '',
            gender: a.gender || '',
            role: a.role || '출연'
        };
    });

    return payload;
}

$(document).ready(function(){
    injectDbModals();
    var current_sub = get_current_module_sub();
    var is_person_page = get_list_context().type === 'person';
    var storage_pfx = is_person_page ? ('person_' + current_sub + '_') : (current_sub + '_dblist_');

    // DB 기본 설정 패널 접힘 상태 복원
    if ($('#setting_collapse_box').length > 0) {
        var saved_collapse = localStorage.getItem(storage_pfx + 'setting_collapse');
        if (saved_collapse === 'show') {
            $('#setting_collapse_box').addClass('show');
            $('#btn_toggle_setting').removeClass('collapsed').attr('aria-expanded', 'true');
        } else {
            $('#setting_collapse_box').removeClass('show');
            $('#btn_toggle_setting').addClass('collapsed').attr('aria-expanded', 'false');
        }
    }

    // 상단 배치 제어 패널 접힘 상태 및 설정값 복원
    if ($('#batch_collapse_box').length > 0) {
        var saved_batch_collapse = localStorage.getItem(storage_pfx + 'batch_collapse');
        if (saved_batch_collapse === 'show') {
            $('#batch_collapse_box').addClass('show');
            $('#btn_toggle_batch').removeClass('collapsed').attr('aria-expanded', 'true');
        } else {
            $('#batch_collapse_box').removeClass('show');
            $('#btn_toggle_batch').addClass('collapsed').attr('aria-expanded', 'false');
        }

        // 배치 작업 폼 설정값 복원
        var batchFields = ['batch_target_scope', 'batch_mode', 'batch_delay'];
        for (var b = 0; b < batchFields.length; b++) {
            var bFid = batchFields[b];
            var savedBVal = localStorage.getItem(storage_pfx + bFid);
            if (savedBVal !== null) {
                var $batchField = $('#' + bFid);
                if ($batchField.is('select') && $batchField.find('option[value="' + savedBVal.replace(/"/g, '\\"') + '"]').length === 0) {
                    localStorage.removeItem(storage_pfx + bFid);
                } else {
                    $batchField.val(savedBVal);
                }
            }
        }

        var savedAutoScroll = localStorage.getItem(storage_pfx + 'batch_log_autoscroll');
        if (savedAutoScroll !== null) {
            $('#batch_log_autoscroll').prop('checked', savedAutoScroll === 'true');
        }

        // 페이지 진입 즉시 이전 배치 상태 동기화 및 SSE 연결
        syncBatchStatus();
        connectBatchSSE();
    }

    // 엔진 설정 뷰 또는 초기 검색 실행
    if ($('#meta_db_engine_sqlite_div').length > 0) {
        $('#btn_transfer_stop').hide();
        $('#btn_db_import_stop').hide();

        var current_engine = $('input[type=radio][name="meta_db_engine_type"]:checked').val() || 'sqlite';
        set_db_engine_view(current_engine);

        $('input[type=radio][name="meta_db_engine_type"]').change(function(){
            set_db_engine_view(this.value);
        });
    }
    else if ($('#list_div').length > 0) {
        if (typeof window.__mainDbGlobalRequestSearch === 'function') {
            window.globalRequestSearch = window.__mainDbGlobalRequestSearch;
        }
        triggerInitialSearchOnce();
    }

    // 저장된 모드 적용 이후 현재 선택값과 설명/옵션 패널을 항상 동기화
    if ($('#batch_mode').length > 0) {
        updateBatchDelayInputState();
        renderBatchDynamicOptions();
    }
});

// DB 기본 설정 패널 펼침/접힘 토글 시 상태 저장
$(document).on('click', '#btn_toggle_setting', function(){
    var current_sub = get_current_module_sub();
    var is_person_page = get_list_context().type === 'person';
    var storage_pfx = is_person_page ? ('person_' + current_sub + '_') : (current_sub + '_dblist_');
    var willExpand = $(this).hasClass('collapsed');
    localStorage.setItem(storage_pfx + 'setting_collapse', willExpand ? 'show' : 'hide');
});

// 상단 배치 제어 패널 부트스트랩 이벤트 기반 상태 저장
$(document).on('shown.bs.collapse', '#batch_collapse_box', function () {
    var storage_pfx = get_storage_prefix();
    localStorage.setItem(storage_pfx + 'batch_collapse', 'show');
    $('#btn_toggle_batch').removeClass('collapsed').attr('aria-expanded', 'true');
});

$(document).on('hidden.bs.collapse', '#batch_collapse_box', function () {
    var storage_pfx = get_storage_prefix();
    localStorage.setItem(storage_pfx + 'batch_collapse', 'hide');
    $('#btn_toggle_batch').addClass('collapsed').attr('aria-expanded', 'false');
});

// 배치 작업 폼 설정값 변경 시 저장
$(document).on('change input', '#batch_target_scope, #batch_mode, #batch_delay', function(){
    var storage_pfx = get_storage_prefix();
    localStorage.setItem(storage_pfx + $(this).attr('id'), $(this).val());
});

// 모드 변경 시 딜레이 입력창 동적 활성/비활성화 처리
function updateBatchDelayInputState() {
    var mode = $('#batch_mode').val();
    var $delayInput = $('#batch_delay');
    if (mode === 'local_sync' || mode === 'works_sync') {
        $delayInput.prop('disabled', true).attr('title', '순수 로컬 DB 전용 모드는 딜레이 없이 초고속으로 처리됩니다.');
    } else {
        $delayInput.prop('disabled', false).removeAttr('title');
    }
}

// 모드별 설명 및 특화 동적 옵션 렌더러
function renderBatchDynamicOptions() {
    var $panel = $('#batch_dynamic_options_panel');
    if ($panel.length === 0) return;

    var bType = get_batch_type();
    var mode = $('#batch_mode').val();

    var descText = '';
    var optionsHtml = '';

    if (bType === 'person') {
        if (mode === 'person_image_sync') {
            descText = '💡 배우 프로필 사진을 개인 저장소(구글 드라이브 / 로컬 이미지 서버)로 다운로드하여 영구 보관용 썸네일로 동기화합니다.';
            optionsHtml += '<div class="custom-control custom-checkbox mr-3 mb-1">';
            optionsHtml += '  <input type="checkbox" class="custom-control-input" id="opt_person_skip_existing" data-opt-key="person_skip_existing" checked>';
            optionsHtml += '  <label class="custom-control-label small font-weight-bold text-light" for="opt_person_skip_existing" style="cursor: pointer;">기존 이미지 존재 시 처리 생략 (구글 드라이브는 File ID만 갱신)</label>';
            optionsHtml += '</div>';
        } else if (mode === 'works_sync') {
            descText = '💡 소장 중인 전체 작품(JAV 및 Western)의 출연진을 스캔하여 인물 DB와의 연계 누락(works) 및 통계(works_count)를 일괄 재구축합니다.';
            optionsHtml += '<div class="custom-control custom-checkbox mr-3 mb-1">';
            optionsHtml += '  <input type="checkbox" class="custom-control-input" id="opt_works_sync_clean_orphan" data-opt-key="works_sync_clean_orphan" checked>';
            optionsHtml += '  <label class="custom-control-label small font-weight-bold text-light" for="opt_works_sync_clean_orphan" style="cursor: pointer;">오매칭 출연작 자동 정제 포함 (출연진에서 빠진 작품은 works에서 자동 제거)</label>';
            optionsHtml += '</div>';
        } else if (mode === 'western_person_refresh') {
            descText = '💡 StashDB / ThePornDB 원본 사이트에 접속하여 서양 인물의 최신 프로필(신체 사이즈, 생년월일, 국적, 별칭, 사진 목록 등)을 일괄 갱신합니다.';
            optionsHtml += '<div class="custom-control custom-checkbox mr-3 mb-1">';
            optionsHtml += '  <input type="checkbox" class="custom-control-input" id="opt_western_protect_manual" data-opt-key="western_protect_manual" checked>';
            optionsHtml += '  <label class="custom-control-label small font-weight-bold text-light" for="opt_western_protect_manual" style="cursor: pointer;">수동 편집 데이터 보호 (사용자가 직접 수정한 필드는 덮어쓰지 않음)</label>';
            optionsHtml += '</div>';
            optionsHtml += '<div class="custom-control custom-checkbox mr-3 mb-1">';
            optionsHtml += '  <input type="checkbox" class="custom-control-input" id="opt_western_skip_existing" data-opt-key="western_skip_existing" checked>';
            optionsHtml += '  <label class="custom-control-label small font-weight-bold text-light" for="opt_western_skip_existing" style="cursor: pointer;">기존 이미지 존재 시 이미지 처리 생략 (구글 드라이브는 File ID만 갱신)</label>';
            optionsHtml += '</div>';
        }
    } else {
        if (mode === 'local_sync') {
            descText = '💡 메타 사이트 재검색 없이 로컬 DB와 개인 저장소의 파일을 기준으로 메타 DB를 동기화합니다.';
        } else if (mode === 'missing') {
            descText = '💡 출처 사이트에 접속하여 누락된 줄거리, 인물, 출시일, 제작사 등의 정보만 선별적으로 보완합니다.';
            optionsHtml += '<div class="custom-control custom-checkbox mr-3 mb-1">';
            optionsHtml += '  <input type="checkbox" class="custom-control-input" id="opt_missing_include_media" data-opt-key="missing_include_media">';
            optionsHtml += '  <label class="custom-control-label small font-weight-bold text-light" for="opt_missing_include_media" style="cursor: pointer;">이미지/미디어 동기화 포함 (기존 미디어 존재 시 재처리 스킵)</label>';
            optionsHtml += '</div>';
        } else if (mode === 'media_sync') {
            descText = '💡 모달의 이미지/미디어 동기화와 동일하게 출처 사이트에서 이미지 정보를 다시 가져옵니다. 기존 미디어는 재사용하고, 없는 이미지는 탐색·크롭·저장합니다. 사용자 이미지와 기존 프리뷰는 보호합니다.';
        } else if (mode === 'media_reprocess') {
            descText = '💡 모달의 이미지/미디어 재처리와 동일하게 출처 사이트에서 이미지 정보를 다시 가져와 기존 일반 이미지와 공식 트레일러를 다시 처리합니다. 사용자 이미지와 기존 프리뷰는 보호하며, 저장 방식과 이미지 처리 설정을 따릅니다.';
        } else if (mode === 'in_place') {
            descText = '💡 현재 출처 사이트의 최신 웹페이지 정보로 메타데이터 전체를 새로고침합니다.';
            optionsHtml += '<div class="custom-control custom-checkbox mr-3 mb-1">';
            optionsHtml += '  <input type="checkbox" class="custom-control-input" id="opt_in_place_skip_media" data-opt-key="in_place_skip_media">';
            optionsHtml += '  <label class="custom-control-label small font-weight-bold text-light" for="opt_in_place_skip_media" style="cursor: pointer;">이미지/미디어 처리 생략 (텍스트/인물 정보만 고속 갱신)</label>';
            optionsHtml += '</div>';
        } else if (mode === 'auto_search') {
            descText = '💡 사이트 우선순위에 따라 전체 사이트를 다시 검색하여 더 온전한 사이트의 메타데이터로 전면 교체합니다.';
            optionsHtml += '<div class="custom-control custom-checkbox mr-3 mb-1">';
            optionsHtml += '  <input type="checkbox" class="custom-control-input" id="opt_auto_search_under_95" data-opt-key="auto_search_under_95" checked>';
            optionsHtml += '  <label class="custom-control-label small font-weight-bold text-light" for="opt_auto_search_under_95" style="cursor: pointer;">현재 점수 95점 미만 항목만 대상 (고득점 항목 유지)</label>';
            optionsHtml += '</div>';
        }
    }

    $('#batch_mode_desc_text').text(descText);
    $('#batch_mode_options_container').html(optionsHtml);
}

$(document).on('change', '#batch_mode', function(){
    updateBatchDelayInputState();
    renderBatchDynamicOptions();
});

// 페이지 로드 시 모드에 따른 초기 딜레이 상태 및 동적 옵션 반영
$(document).ready(function(){
    if ($('#batch_mode').length > 0) {
        updateBatchDelayInputState();
        renderBatchDynamicOptions();
    }
});

$(document).on('change', '#batch_log_autoscroll', function(){
    var storage_pfx = get_storage_prefix();
    localStorage.setItem(storage_pfx + 'batch_log_autoscroll', $(this).is(':checked') ? 'true' : 'false');
});

// 현재 페이지에 맞는 배치 트랙('item' vs 'person') 자동 판별 헬퍼
function get_batch_type() {
    var ctx = (typeof get_list_context === 'function') ? get_list_context() : null;
    if (ctx) return ctx.type === 'person' ? 'person' : 'item';
    return $('#batch_mode').val() === 'person_image_sync' ? 'person' : 'item';
}

// 하단 체크박스 선택 변경 시 배치 대상 수량 실시간 표시 및 작업 트리거
function triggerBatchRefresh(actionType) {
    var bType = get_batch_type();
    var bLabel = (bType === 'person') ? '인물' : '영상 메타';

    var checkedCodes = [];
    $('.meta-item-chk:checked').each(function(){
        var v = $(this).val();
        var category = ($(this).attr('data-category') || '').toUpperCase();
        var uiCode = $(this).attr('data-ui-code') || v;
        if (v) {
            checkedCodes.push({
                code: v,
                ui_code: uiCode,
                category: category
            });
        }
    });

    var targetScope = $('#batch_target_scope').val() || 'auto';
    var refreshMode = $('#batch_mode').val() || (bType === 'person' ? 'person_image_sync' : 'missing');
    var delayVal = parseFloat($('#batch_delay').val()) || (bType === 'person' ? 0.5 : 2.0);

    // 하단 검색 폼의 현재 필터 상태를 추출
    var currentContext = get_list_context();
    var searchCategory = ($('#search_category').length > 0 ? $('#search_category').val() : currentContext.category) || 'AV_ALL';
    var searchDomain = $('#search_domain').val() || 'ALL';
    var searchSite = $('#search_site').val() || 'all';
    var searchStatus = $('#search_status').val() || 'all';
    var searchWord = $('#search_word').val() || '';
    var searchOrder = $('#search_order').val() || 'desc';

    if (actionType === 'restart') {
        var isTargetChecked = (targetScope === 'selected' || (targetScope === 'auto' && checkedCodes.length > 0));
        var confirmMsg = isTargetChecked
            ? "선택한 " + checkedCodes.length + "건의 항목을 대상으로 " + bLabel + " 배치 작업을 시작하시겠습니까?"
            : "하단 검색 조건 일치 항목을 대상으로 " + bLabel + " 배치 작업을 시작하시겠습니까?";
        if (!confirm(confirmMsg)) return;
    }

    $('#batch_terminal_logs').html('<div style="color: #58a6ff;">[시스템] ' + bLabel + ' 배치 작업을 시작합니다. 서버 세션 연결 및 대상 항목 수집 중...</div>');
    $('#batch_badge_status').removeClass('badge-secondary badge-success badge-warning').addClass('badge-info').text('시작 중');
    $('#batch_current_code_text').text('대상 수집 중...');

    $('#btn_batch_start').hide();
    $('#batch_resume_btn_group').hide();
    $('#btn_batch_stop').show();

    connectBatchSSE();

    // 동적 패널에 렌더링된 모드별 옵션값 수집
    var batchOptions = {};
    $('#batch_mode_options_container input[type="checkbox"]').each(function(){
        var optKey = $(this).attr('data-opt-key') || $(this).attr('id');
        if (optKey) {
            batchOptions[optKey] = $(this).is(':checked');
        }
    });

    var searchPayload = {
        action: actionType,
        batch_type: bType,
        target_scope: targetScope,
        codes: checkedCodes,
        mode: refreshMode,
        delay: delayVal,
        batch_options: batchOptions,
        search_category: searchCategory,
        search_domain: searchDomain,
        search_site: searchSite,
        search_status: searchStatus,
        search_word: searchWord,
        search_order: searchOrder
    };

    globalSendCommand('db_batch_refresh_start', actionType, refreshMode, JSON.stringify(searchPayload), function(ret){
        if (typeof notify === 'function') notify(ret.msg, ret.ret === 'success' ? 'success' : 'warning');
    }, searchPayload);
}

// 시작 버튼: 2중 실행 방지를 위한 즉시 전파 차단 적용
$(document).off('click', '#btn_batch_start').on('click', '#btn_batch_start', function(e){
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    triggerBatchRefresh('restart');
});

// 재개 버튼: 중단된 인덱스부터 이어서 시작
$(document).off('click', '#btn_batch_resume').on('click', '#btn_batch_resume', function(e){
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    triggerBatchRefresh('resume');
});

// 재시작 버튼: 처음 항목부터 새로 시작
$(document).off('click', '#btn_batch_restart').on('click', '#btn_batch_restart', function(e){
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    triggerBatchRefresh('restart');
});

// 패널 상태 및 큐 초기화 버튼 핸들러 (item/person 트랙별 독립 리셋)
$(document).off('click', '#btn_batch_reset_panel').on('click', '#btn_batch_reset_panel', function(e){
    e.preventDefault();
    e.stopPropagation();

    var bType = get_batch_type();
    var bLabel = (bType === 'person') ? '인물' : '영상 메타';

    if (!confirm(bLabel + " 배치 작업 설정 및 큐 상태를 초기화하시겠습니까?")) return;

    globalSendCommand('db_batch_refresh_reset', bType, null, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg || '패널이 초기화되었습니다.', 'success');

        // 상단 패널 입력 요소 초기화
        if (bType === 'person') {
            $('#batch_target_scope').val('search_filter');
            $('#batch_mode').val('person_image_sync');
            $('#batch_delay').val('0.5');
        } else {
            $('#batch_target_scope').val('auto');
            $('#batch_mode').val('missing');
            $('#batch_delay').val('2.0');
        }
        updateBatchDelayInputState();
        renderBatchDynamicOptions();
        $('#opt_batch_selected').text('체크박스 선택 항목 (0개 선택됨)');
        $('#check_all_meta, .meta-item-chk').prop('checked', false);

        // 버튼 상태를 초기 시작 버튼 단독 상태로 리셋
        $('#batch_resume_btn_group').hide();
        $('#btn_batch_stop').hide();
        $('#btn_batch_start').show();

        // 프로그레스 바 및 상태 텍스트 초기화
        $('#batch_badge_status').removeClass('badge-info badge-warning badge-success').addClass('badge-secondary').text('대기 중');
        $('#batch_current_code_text').text('대기 중');
        $('#batch_stats_text').text('진행: 0 / 0 (성공: 0 | 실패: 0 | 건너뜀: 0)');
        $('#batch_progress_bar').css('width', '0%').text('0%').removeClass('progress-bar-animated bg-info bg-warning bg-success bg-danger');

        // 터미널 안내문 초기화
        $('#batch_terminal_logs').html('<div style="color: #58a6ff;">[시스템] ' + bLabel + ' 배치 작업 대기 상태입니다. 상단에서 조건을 설정하고 시작 버튼을 누르세요.</div>');

        // 스토리지 저장값 리셋
        var storage_pfx = get_storage_prefix();
        localStorage.removeItem(storage_pfx + 'batch_target_scope');
        localStorage.removeItem(storage_pfx + 'batch_mode');
        localStorage.setItem(storage_pfx + 'batch_delay', bType === 'person' ? '0.5' : '2.0');
    }, { batch_type: bType });
});

// 딜레이 값 변경 시 로컬 저장
$(document).off('input change', '#batch_delay').on('input change', '#batch_delay', function(){
    var storage_pfx = get_storage_prefix();
    localStorage.setItem(storage_pfx + 'batch_delay', $(this).val());
});

// 페이징 및 필터 변경 이벤트
$(document).on('click', '#page, #gloablSearchPageBtn, a[onclick*="request_search"], button[data-page]', function(e){
    e.preventDefault();
    e.stopImmediatePropagation();
    var targetPage = $(this).data('page') || $(this).text().trim();
    if(targetPage && !isNaN(targetPage)) {
        window.globalRequestSearch(targetPage.toString());
    }
});

$(document).off('click', '.db-page-btn').on('click', '.db-page-btn', function(e){
    e.preventDefault();
    e.stopPropagation();
    var targetPage = $(this).attr('data-page') || $(this).data('page');
    if (targetPage && !isNaN(targetPage)) {
        window.globalRequestSearch(targetPage.toString(), false);
    }
});

$(document).on('change', '#search_site, #search_status, #search_order, #page_size, #search_domain', function(e){
    window.globalRequestSearch('1', false);
});

$(document).on('change', '#search_category', function(e){
    var selCat = $(this).val() || 'AV_ALL';
    $('#list_div').attr('data-list-category', selCat);
    var storage_pfx = get_storage_prefix();
    localStorage.setItem(storage_pfx + 'search_category', selCat);
    updateSiteOptionsByCategory(selCat);
    localStorage.setItem(storage_pfx + 'search_site', $('#search_site').val());
    window.globalRequestSearch('1', false);
});

$(document).on('click', '#search, #btn_person_search_submit', function(e) {
    e.preventDefault();
    e.stopPropagation();
    window.globalRequestSearch('1', false);
});

$(document).on('keydown', '#search_word', function(e) {
    if (e.key === 'Enter' || e.keyCode === 13) {
        e.preventDefault();
        e.stopPropagation();
        window.globalRequestSearch('1', false);
    }
});

$(document).on('submit', '#form_search, #form_search_person', function(e) {
    e.preventDefault();
    e.stopPropagation();
    window.globalRequestSearch('1', false);
});

// 검색 조건 리셋 버튼
$(document).on('click', '#reset_btn, #btn_person_search_reset', function(e) {
    e.preventDefault();
    e.stopPropagation();
    $("#search_word").val('');

    var storage_pfx = get_storage_prefix();
    var current_sub = get_current_module_sub();
    var is_person_page = get_list_context().type === 'person';

    var preserved_page_size = $("#page_size").val() || (is_person_page ? '30' : '10');

    var keysToRemove = [
        storage_pfx + 'search_word',
        storage_pfx + 'current_page',
        storage_pfx + 'search_status',
        storage_pfx + 'search_order',
        storage_pfx + 'search_site',
        storage_pfx + 'search_domain'
    ];
    if ($('#search_category').length > 0) {
        keysToRemove.push(storage_pfx + 'search_category');
        $("#search_category").val('AV_ALL');
        $('#list_div').attr('data-list-category', 'AV_ALL');
        updateSiteOptionsByCategory('AV_ALL', 'all');
    }
    for (var k = 0; k < keysToRemove.length; k++) {
        localStorage.removeItem(keysToRemove[k]);
    }

    $("#search_status").val('all');
    $("#search_order").val('desc');
    $("#page_size").val(preserved_page_size);

    if (!is_person_page) {
        $("#search_site").val('all');
    } else {
        var default_dom = current_sub === 'western' ? 'WESTERN' : (
            current_sub === 'meta_db' ? 'ALL' : 'JAV'
        );
        $("#search_domain").val(default_dom);
    }

    window.globalRequestSearch('1', false);
});

function make_list(data) {
    if (typeof data === 'string') {
        try {
            data = JSON.parse(data);
        } catch (e) {
            data = [];
        }
    }
    if ($('#search_domain').length > 0 || window.location.pathname.indexOf('person_list') !== -1) {
        make_person_list(data);
    } else {
        make_item_list(data);
    }
}

function make_item_list(data) {
    current_data = data || [];
    var str = '';
    if (!data || data.length === 0) {
        str += '<div class="row"><div class="col-sm-12 text-center p-4 text-muted">저장된 메타데이터가 없습니다.</div></div>';
    } else {
        for (var i = 0; i < data.length; i++) {
            var row = data[i];
            var jd = row.json_data;
            if (typeof jd === 'string') { try { jd = JSON.parse(jd); } catch(e) { jd = {}; } }
            if (!jd) jd = {};

            var raw_title = row.title || row.originaltitle || jd.title || row.code || '';
            // 개행문자(\n, \r, \t) 및 줄바꿈 태그를 단일 공백으로 치환
            raw_title = raw_title.replace(/<\/?(br|p|div)[^>]*>/gi, ' ').replace(/[\r\n\t]+/g, ' ').replace(/\s+/g, ' ').trim();
            // 제목 끝에 스페이스 없이 붙은 (YYYY) 분리 보정
            raw_title = raw_title.replace(/(?<=[^\s])\((\d{4})\)$/, ' ($1)');

            var year_val = jd.year || (jd.premiered ? jd.premiered.substring(0, 4) : '');
            var year_html = (year_val && year_val != 0 && year_val != 1900 && raw_title.indexOf('(' + year_val + ')') === -1) 
                ? '&nbsp;<span class="text-muted small font-weight-normal">(' + year_val + ')</span>' : '';

            var current_cat = (row.category || get_list_context().category || '').toUpperCase();
            var is_jav = (current_cat === 'JAV_CEN' || current_cat === 'JAV_UNCEN' || current_cat === 'AV_ALL' || sub === 'jav_censored' || sub === 'jav_uncensored' || sub === 'meta_db');
            var actor_html = '';

            if (is_jav && jd.actor && Array.isArray(jd.actor) && jd.actor.length > 0) {
                var actor_items = [];
                var seen_actor_names = new Set();
                for (var a_idx = 0; a_idx < jd.actor.length; a_idx++) {
                    var a_item = jd.actor[a_idx];
                    var a_name = '';
                    var a_target = '';
                    var a_registered = false;
                    if (typeof a_item === 'object' && a_item !== null) {
                        a_name = (a_item.name_ko || a_item.name_org || a_item.name || '').trim();
                        a_registered = a_item.is_registered === true && Number(a_item.db_person_id) > 0;
                        a_target = a_registered ? String(a_item.db_person_id) : '';
                    } else if (typeof a_item === 'string') {
                        a_name = a_item.trim();
                        a_target = a_name;
                    }
                    if (a_name && !seen_actor_names.has(a_name)) {
                        seen_actor_names.add(a_name);
                        actor_items.push({ name: a_name, target: a_target, registered: a_registered });
                    }
                }

                if (actor_items.length > 0) {
                    var actor_dom = (current_cat === 'WESTERN') ? 'WESTERN' : 'JAV';
                    var top_items = actor_items.slice(0, 3);
                    var actor_links = [];
                    for (var t_idx = 0; t_idx < top_items.length; t_idx++) {
                        var it = top_items[t_idx];
                        var safe_name = it.name.replace(/[&<>"']/g, function(c) {
                            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
                        });
                        if (it.registered) {
                            actor_links.push('<span class="actor-title-link-clickable text-info font-weight-normal" data-db-person-id="' + it.target + '" data-domain="' + actor_dom + '" style="cursor: pointer;" title="[' + safe_name + '] 인물 정보 열기">' + safe_name + '</span>');
                        } else {
                            actor_links.push('<span class="font-weight-normal" title="인물 DB 미등록" style="cursor: default;">' + safe_name + '</span>');
                        }
                    }
                    var top_actors_html = actor_links.join(', ');
                    if (actor_items.length > 3) {
                        top_actors_html += ' 외';
                    }
                    actor_html = '&nbsp;<span class="text-muted small font-weight-normal">- ' + top_actors_html + '</span>';
                }
            }

            var trailer_url = '';
            if (jd.extras && Array.isArray(jd.extras)) {
                var tr_ex = jd.extras.find(function(ex){ return ex && ex.content_type === 'trailer' && ex.content_url; });
                if (tr_ex) trailer_url = tr_ex.content_url;
            }
            if (!trailer_url && jd.original && jd.original.extras && Array.isArray(jd.original.extras)) {
                var orig_tr = jd.original.extras.find(function(ex){ return ex && ex.content_type === 'trailer' && ex.content_url; });
                if (orig_tr) trailer_url = orig_tr.content_url;
            }

            var trailer_badge_html = '';
            if (trailer_url) {
                var safe_title_attr = encodeURIComponent(raw_title);
                trailer_badge_html = '<span class="badge badge-dark border border-secondary text-info mr-2 btn-play-trailer-modal" data-url="' + trailer_url + '" data-title="' + safe_title_attr + '" style="cursor: pointer; font-size: 0.9em; padding: 3px 7px;" title="예고편 비디오 바로 재생">🎬 트레일러 재생</span>';
            }

            var item_gallery = [];
            var added_raw_keys = new Set();

            var list_p_url = '';
            var list_pl_url = '';

            if (jd.thumb && Array.isArray(jd.thumb)) {
                var p_item = jd.thumb.find(function(t){ return t && t.aspect === 'poster'; });
                if (p_item && p_item.value) list_p_url = p_item.value;
                var pl_item = jd.thumb.find(function(t){ return t && t.aspect === 'landscape'; });
                if (pl_item && pl_item.value) list_pl_url = pl_item.value;
            }
            if (!list_p_url && row.poster_url) {
                list_p_url = row.poster_url;
            }

            var orig_thumb = (jd.original && jd.original.thumb) ? jd.original.thumb : {};
            var raw_site_p = Array.isArray(orig_thumb.poster) ? (orig_thumb.poster[0] || '') : (orig_thumb.poster || '');
            var raw_site_pl = Array.isArray(orig_thumb.landscape) ? (orig_thumb.landscape[0] || '') : (orig_thumb.landscape || '');
            var site_photo_urls = [];
            if (Array.isArray(orig_thumb.poster)) site_photo_urls = site_photo_urls.concat(orig_thumb.poster);
            if (Array.isArray(orig_thumb.landscape)) site_photo_urls = site_photo_urls.concat(orig_thumb.landscape);
            [
                jd.photo_urls,
                jd.site_img_urls,
                jd.extra_info && jd.extra_info.photo_urls,
                jd.extra_info && jd.extra_info.site_img_urls,
                jd.original && jd.original.photo_urls,
                jd.original && jd.original.site_img_urls
            ].forEach(function(photo_list) {
                if (!Array.isArray(photo_list)) return;
                photo_list.forEach(function(photo_url) {
                    if (typeof photo_url === 'string' && photo_url.trim()) {
                        site_photo_urls.push(photo_url.trim());
                    }
                });
            });

            if (list_p_url) {
                var cleanKeyP = getCleanSourceUrl(list_p_url);
                var isLocalP = isLocalServerMediaUrl(list_p_url);
                var isGdriveP = list_p_url.indexOf('drive.google.com') !== -1;
                if (cleanKeyP && !added_raw_keys.has(cleanKeyP)) {
                    item_gallery.push({
                        url: getDisplayMediaUrl(list_p_url, row.site),
                        type: isGdriveP ? 'Poster (GDrive)' : (isLocalP ? 'Poster' : 'Poster (Site)'),
                        is_final: isLocalP || isGdriveP
                    });
                    added_raw_keys.add(cleanKeyP);
                }
            }

            if (list_pl_url) {
                var cleanKeyPl = getCleanSourceUrl(list_pl_url);
                var isLocalPl = isLocalServerMediaUrl(list_pl_url);
                var isGdrivePl = list_pl_url.indexOf('drive.google.com') !== -1;
                if (cleanKeyPl && !added_raw_keys.has(cleanKeyPl)) {
                    item_gallery.push({
                        url: getDisplayMediaUrl(list_pl_url, row.site),
                        type: isGdrivePl ? 'Landscape (GDrive)' : (isLocalPl ? 'Landscape' : 'Landscape (Site)'),
                        is_final: isLocalPl || isGdrivePl
                    });
                    added_raw_keys.add(cleanKeyPl);
                }
            }

            var localFanarts = (jd.fanart && Array.isArray(jd.fanart)) ? jd.fanart : [];
            localFanarts.forEach(function(fa_url, fa_i){
                if (!fa_url) return;
                var cleanKeyFa = getCleanSourceUrl(fa_url);
                var isLocalFa = isLocalServerMediaUrl(fa_url);
                var isGdriveFa = fa_url.indexOf('drive.google.com') !== -1 || fa_url.indexOf('googleusercontent.com') !== -1;
                if (cleanKeyFa && !added_raw_keys.has(cleanKeyFa)) {
                    var typeLabel = isGdriveFa ? ('Art (GDrive) #' + (fa_i + 1)) : (isLocalFa ? ('Local Art #' + (fa_i + 1)) : ('Art #' + (fa_i + 1)));
                    item_gallery.push({
                        url: getDisplayMediaUrl(fa_url, row.site),
                        type: typeLabel,
                        is_final: isLocalFa || isGdriveFa
                    });
                    added_raw_keys.add(cleanKeyFa);
                }
            });

            if (raw_site_p) {
                var cleanSiteP = getCleanSourceUrl(raw_site_p);
                if (cleanSiteP && !added_raw_keys.has(cleanSiteP) && !isLocalServerMediaUrl(cleanSiteP)) {
                    item_gallery.push({
                        url: getDisplayMediaUrl(raw_site_p, row.site),
                        type: 'Poster (Site)',
                        is_final: false
                    });
                    added_raw_keys.add(cleanSiteP);
                }
            }

            if (raw_site_pl) {
                var cleanSitePl = getCleanSourceUrl(raw_site_pl);
                if (cleanSitePl && !added_raw_keys.has(cleanSitePl) && !isLocalServerMediaUrl(cleanSitePl)) {
                    item_gallery.push({
                        url: getDisplayMediaUrl(raw_site_pl, row.site),
                        type: 'Landscape (Site)',
                        is_final: false
                    });
                    added_raw_keys.add(cleanSitePl);
                }
            }

            site_photo_urls.forEach(function(photo_url, photo_idx) {
                var cleanSitePhoto = getCleanSourceUrl(photo_url);
                if (cleanSitePhoto && !added_raw_keys.has(cleanSitePhoto) && !isLocalServerMediaUrl(photo_url)) {
                    item_gallery.push({
                        url: getDisplayMediaUrl(photo_url, row.site),
                        type: 'Site Photo #' + (photo_idx + 1),
                        is_final: false
                    });
                    added_raw_keys.add(cleanSitePhoto);
                }
            });

            var origFanarts = (jd.original && Array.isArray(jd.original.fanart)) ? jd.original.fanart : [];
            origFanarts.forEach(function(ofa_url, ofa_i){
                if (!ofa_url) return;
                var cleanSiteOfa = getCleanSourceUrl(ofa_url);
                if (cleanSiteOfa && !added_raw_keys.has(cleanSiteOfa) && !isLocalServerMediaUrl(cleanSiteOfa)) {
                    item_gallery.push({
                        url: getDisplayMediaUrl(ofa_url, row.site),
                        type: 'Site Art #' + (ofa_i + 1),
                        is_final: false
                    });
                    added_raw_keys.add(cleanSiteOfa);
                }
            });

            var gallery_attr = encodeURIComponent(JSON.stringify(item_gallery));

            var img_html = '';
            var listImageUrl = getDisplayMediaUrl(list_p_url || list_pl_url || row.poster_url || jd.landscape_url || '', row.site);
            if (listImageUrl) {
                img_html = '<img src="' + listImageUrl + '" class="enlarge-img shadow-sm" loading="lazy" data-gallery="' + gallery_attr + '" data-src="' + listImageUrl + '" title="클릭하여 갤러리 크게 보기">';
            } else {
                img_html = '<div class="d-flex align-items-center justify-content-center rounded text-muted no-image-box w-100">No Image</div>';
            }

            var plot_text = jd.plot || "줄거리 정보가 없습니다.";
            if (plot_text.length > 320) plot_text = plot_text.substring(0, 320) + "...";

            var chk_html = '<div class="mr-2 d-flex align-items-center justify-content-center" style="flex-shrink: 0; min-width: 26px;">' +
                           '  <input type="checkbox" class="meta-item-chk" value="' + (row.code || '') + '" data-ui-code="' + (row.ui_code || row.code || '') + '" data-category="' + (row.category || '') + '" style="cursor: pointer; transform: scale(1.4);" title="항목 선택">' +
                           '</div>';

            str += '<div class="row align-items-start py-3 px-1" style="border-bottom: 1px solid rgba(128, 128, 128, 0.15);">';
            str += '<div class="col-sm-2 d-flex align-items-center justify-content-start pl-2 pr-1">' + chk_html + '<div class="flex-grow-1 text-center overflow-hidden">' + img_html + '</div></div>';
            
            str += '<div class="col-sm-8 pl-1 pr-2">';
            str += '  <div style="line-height: 1.4;">';
            str += '    <div class="mb-1" style="word-break: break-word;">';
            str += '      <strong class="text-primary item-title-clickable font-weight-bold mr-1" data-idx="' + i + '" style="font-size: 1.05em; cursor: pointer;" title="클릭하여 메타데이터 편집">' + raw_title + '</strong>' + year_html + actor_html;
            str += '    </div>';
            str += '    <div class="d-flex align-items-center flex-wrap text-muted mb-2" style="font-size: 0.82em;">';
            str += '      <span class="badge badge-info mr-2 badge-copy-code" data-code="' + (row.code || '') + '" style="cursor: pointer;" title="클릭하여 코드 복사">' + (row.code || '') + '</span>';
            str += '      <span class="badge badge-secondary mr-2">' + (row.site || '').toUpperCase() + '</span>';
            str += trailer_badge_html;
            str += '      <span>생성: ' + (row.created_time || '') + '</span>';
            if (row.updated_time && row.updated_time !== row.created_time) {
                str += '   <span class="ml-3 text-info">갱신: ' + row.updated_time + '</span>';
            }
            str += '    </div>';
            str += '    <div class="text-muted" style="font-size: 0.9em; line-height: 1.45; word-break: break-word;">' + plot_text + '</div>';
            str += '  </div>';
            str += '</div>';

            str += '<div class="col-sm-2 pl-1 pr-2">';
            str += '  <div class="d-flex flex-column align-items-center justify-content-start pt-1 w-100" style="max-width: 120px; margin: 0 auto;">';

            str += '    <div class="dropdown mb-2 w-100">';
            str += '      <button class="btn btn-sm btn-outline-primary btn-block custom-dropdown-toggle font-weight-bold py-1 px-1" type="button" style="font-size: 0.82rem;">';
            str += '        이미지 관리';
            str += '      </button>';
            str += '      <div class="dropdown-menu dropdown-menu-right shadow">';
            str += '        <a class="dropdown-item btn_crop_modal" href="#" data-idx="' + i + '" data-category="' + (row.category || '') + '">✏️ 포스터 크롭 에디터</a>';
            str += '        <a class="dropdown-item btn_refresh_image_only text-primary font-weight-bold" href="#" data-media-action="reuse" data-code="' + row.code + '" data-category="' + (row.category || '') + '">🔄 이미지/미디어 동기화</a>';
            str += '        <a class="dropdown-item btn_refresh_image_only text-warning font-weight-bold" href="#" data-media-action="replace" data-code="' + row.code + '" data-category="' + (row.category || '') + '">🔁 이미지/미디어 재처리</a>';
            str += '      </div>';
            str += '    </div>';

            str += '    <button class="btn btn-sm btn-outline-info btn-block btn_edit_db mb-2 font-weight-bold py-1 px-1" type="button" data-idx="' + i + '" data-category="' + (row.category || '') + '" style="font-size: 0.82rem;">DB 편집</button>';

            str += '    <div class="dropdown mb-2 w-100">';
            str += '      <button class="btn btn-sm btn-outline-success btn-block custom-dropdown-toggle font-weight-bold py-1 px-1" type="button" style="font-size: 0.82rem;">';
            str += '        메타 갱신';
            str += '      </button>';
            str += '      <div class="dropdown-menu dropdown-menu-right shadow">';
            str += '        <a class="dropdown-item btn_refresh_missing_in_place text-info font-weight-bold" href="#" data-code="' + row.code + '" data-category="' + (row.category || '') + '">🩹 현재 사이트 누락 정보 업데이트</a>';
            str += '        <a class="dropdown-item btn_refresh_in_place font-weight-bold" href="#" data-code="' + row.code + '" data-category="' + (row.category || '') + '">📌 현재 사이트 제자리 갱신</a>';
            str += '        <a class="dropdown-item btn_refresh_auto_search text-success font-weight-bold" href="#" data-code="' + row.code + '" data-category="' + (row.category || '') + '">🔍 전체 우선순위 자동 재검색</a>';
            str += '      </div>';
            str += '    </div>';

            str += '    <button class="btn btn-sm btn-outline-danger btn-block btn_delete_db font-weight-bold py-1 px-1" data-code="' + row.code + '" data-category="' + (row.category || '') + '" style="font-size: 0.82rem;">데이터 삭제</button>';
            str += '  </div>';
            str += '</div>';

            str += '</div>';
        }
    }
    var listEl = document.getElementById("list_div");
    if (listEl) listEl.innerHTML = str;

    $('#check_all_meta').prop('checked', false);
}

function make_person_list(data) {
    if (typeof data === 'string') {
        try {
            data = JSON.parse(data);
        } catch (e) {
            data = [];
        }
    }
    if (!Array.isArray(data)) {
        data = [];
    }

    current_person_data = data;
    var str = '';
    var valid_count = 0;

    var no_photo_svg = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMTAiIGhlaWdodD0iMTQ1Ij48cmVjdCB3aWR0aD0iMTAwJSIgaGVpZ2h0PSIxMDAlIiBmaWxsPSIjMTYxODFiIi8+PHRleHQgeD0iNTAlIiB5PSI1MCUiIGZpbGw9IiM2Yzc1N2QiIGZvbnQtc2l6ZT0iMTIiIGZvbnQtZmFtaWx5PSJzYW5zLXNlcmlmIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBkeT0iLjNlbSI+Tm8gUGhvdG88L3RleHQ+PC9zdmc+';

    if (current_person_data.length === 0) {
        str = '<div class="col-12 text-center p-5 text-muted">등록된 인물 데이터가 없습니다.</div>';
    } else {
        for (var i = 0; i < current_person_data.length; i++) {
            var p = current_person_data[i];
            if (!p || typeof p !== 'object' || Array.isArray(p)) {
                continue;
            }
            var looks_like_person = ('name_org' in p) || ('name_ko' in p) || ('name_en' in p) || ('name' in p) || ('person_idx' in p) || ('aliases' in p) || ('other_names' in p);
            if (!looks_like_person) {
                continue;
            }
            
            var p_gallery = buildPersonImageGallery(p).images;

            var active_thumb_url = p.thumb_url || p.thumb || '';
            if (active_thumb_url && isLocalServerMediaUrl(active_thumb_url)) {
                active_thumb_url = active_thumb_url + (active_thumb_url.indexOf('?') === -1 ? '?' : '&') + '_t=' + Date.now();
            }

            var p_gallery_attr = encodeURIComponent(JSON.stringify(p_gallery));

            var img_tag = active_thumb_url 
                ? '<img src="' + active_thumb_url + '" class="enlarge-person-photo shadow-sm" data-gallery="' + p_gallery_attr + '" data-src="' + active_thumb_url + '" style="width: 110px; height: 145px; object-fit: cover; cursor: zoom-in;" title="클릭하여 갤러리 크게 보기" onerror="this.onerror=null; this.src=\'' + no_photo_svg + '\';">' 
                : '<div class="d-flex align-items-center justify-content-center text-muted small" style="width: 110px; height: 145px; background: #16181b;">No Photo</div>';

            var name_org = (p.name_org || p.name || '').trim();
            var name_ko = (p.name_ko || '').trim();
            var name_en = (p.name_en || '').trim();

            var title_name = name_ko || name_org || name_en || '이름 없음';
            var show_orig = (name_org && name_org.toLowerCase() !== title_name.toLowerCase()) ? name_org : '';
            var show_en = (name_en && name_en.toLowerCase() !== title_name.toLowerCase() && name_en.toLowerCase() !== show_orig.toLowerCase()) ? name_en : '';
            var display_id = p.person_idx || '';

            var extra_d = p.extra_info || {};
            if (typeof extra_d === 'string') { try { extra_d = JSON.parse(extra_d); } catch(e){} }
            var sub_count = (p.merged_count && p.merged_count > 1) ? p.merged_count : (
                (extra_d.merged_sub_actors && Array.isArray(extra_d.merged_sub_actors)) ? extra_d.merged_sub_actors.length : (
                    (extra_d.alt_actor_indices && Array.isArray(extra_d.alt_actor_indices)) ? extra_d.alt_actor_indices.length : 0
                )
            );
            var merged_badge = (sub_count > 1) ? (' <span class="badge badge-primary font-weight-bold" style="font-size: 0.72em;" title="' + sub_count + '개 ID 통합됨">통합 (' + sub_count + ')</span>') : '';

            // 소장작품 수 및 신체 스펙 뱃지 생성 (코드 뒤쪽 배치용)
            var works_count_val = (p.works_count !== undefined && p.works_count !== null) ? p.works_count : (
                p.works ? Object.keys(p.works).reduce(function(acc, k){ return acc + (Array.isArray(p.works[k]) ? p.works[k].length : 0); }, 0) : 0
            );
            var works_badge = (works_count_val > 0) ? ('<span class="badge badge-success font-weight-normal mr-1 mb-1" style="font-size: 0.75em;" title="소장 출연작 ' + works_count_val + '편">소장 ' + works_count_val + '편</span>') : '';

            var spec_text = '';
            if (p.height && parseInt(p.height, 10) > 0) spec_text += p.height + 'cm';
            if (p.cup_size) {
                var clean_cup = String(p.cup_size).trim();
                if (spec_text) spec_text += ' ';
                spec_text += clean_cup + (clean_cup.endsWith('컵') ? '' : '컵');
            }
            var spec_badge = spec_text ? ('<span class="badge badge-dark border border-secondary font-weight-normal mr-1 mb-1" style="font-size: 0.75em;">' + spec_text + '</span>') : '';

            str += '<div class="col-lg-4 col-md-6 mb-3">';
            str += '  <div class="card h-100 border border-secondary shadow-sm overflow-hidden" style="background: #23272d; border-radius: 6px;">';
            str += '    <div class="d-flex h-100">';
            str += '      <div class="flex-shrink-0" style="width: 110px; height: 145px; background: #16181b;">' + img_tag + '</div>';
            str += '      <div class="p-2 d-flex flex-column justify-content-between flex-grow-1" style="min-width: 0;">';
            str += '        <div style="line-height: 1.35;">';
            str += '          <div class="mb-1"><strong class="text-info text-truncate font-weight-bold person-name-clickable" data-idx="' + i + '" style="font-size: 1.1em; cursor: pointer;" title="클릭하여 인물 정보 수정">' + title_name + '</strong>' + merged_badge + '</div>';
            if (show_orig) {
                str += '          <div class="text-white text-truncate font-weight-normal" style="font-size: 0.92em;" title="' + show_orig + '">' + show_orig + '</div>';
            }
            if (show_en) {
                str += '          <div class="text-muted text-truncate" style="font-size: 0.88em;" title="' + show_en + '">' + show_en + '</div>';
            }

            // 식별코드 바로 뒤쪽으로 소장 편수 및 신체 스펙 뱃지 나란히 배치
            str += '          <div class="mt-1 d-flex align-items-center flex-wrap">';
            if (display_id) {
                str += '            <span class="badge badge-secondary badge-copy-code mr-1 mb-1 text-wrap" data-code="' + display_id + '" style="cursor: pointer; font-size: 0.78em;" title="클릭하여 식별코드 복사">' + display_id + '</span>';
            }
            str += works_badge;
            str += spec_badge;
            str += '          </div>';
            str += '        </div>';

            // 하단 라인은 도메인 뱃지와 수정/삭제 버튼만 배치하여 1줄 레이아웃 유지
            str += '        <div class="d-flex justify-content-between align-items-center mt-2 pt-1">';
            str += '          <span class="badge badge-secondary font-weight-normal" style="font-size: 0.75em;">' + (p.domain || 'JAV') + '</span>';
            str += '          <div class="text-nowrap">';
            str += '            <button class="btn btn-sm btn-outline-info py-0 px-2 mr-1 btn_edit_person" data-idx="' + i + '">수정</button>';
            str += '            <button class="btn btn-sm btn-outline-danger py-0 px-2 btn_delete_person" data-id="' + p.id + '">삭제</button>';
            str += '          </div>';
            str += '        </div>';

            str += '      </div>';
            str += '    </div>';
            str += '  </div>';
            str += '</div>';
            valid_count += 1;
        }
    }
    if (valid_count === 0) {
        str = '<div class="col-12 text-center p-5 text-muted">등록된 인물 데이터가 없습니다.</div>';
    }
    var listEl = document.getElementById("list_div");
    if (listEl) listEl.innerHTML = str;
}

// 통합 단일 모달 로더 및 렌더러
// The list lightbox and person editor share source labels, assets and primary ordering.
function buildPersonImageGallery(p, mediaPayload) {
    var media = mediaPayload || p.media_src || {};
    if (typeof media === 'string') { try { media = JSON.parse(media); } catch (err) { media = {}; } }
    var official_media = media.official || media;
    var custom_media = media.custom || {};
    var dom = (p.domain || 'JAV').toUpperCase();
    var local_val = dom === 'JAV' ? (official_media.local_img_path || '').trim() : '';
    var google_val = dom === 'JAV' ? (official_media.google_fileid || '').trim() : '';
    var cached_local_val = (official_media.cached_local_img_path || '').trim();
    var cached_google_val = (official_media.official_cached_google_fileid || (!media.official ? media.user_google_fileid : '') || p.official_cached_gdrive_fileid || '').trim();
    var custom_local_val = (custom_media.local_img_path || '').trim();
    var custom_local_url = custom_local_val ? formatActorLocalUrl(custom_local_val, dom) : '';
    var user_google_val = String(Object.prototype.hasOwnProperty.call(custom_media, 'custom_user_google_fileid')
        ? custom_media.custom_user_google_fileid || '' : (media.custom_user_google_fileid || p.custom_gdrive_fileid || '')).trim();
    var customGdriveThumbUrl = user_google_val ? 'https://drive.google.com/thumbnail?id=' + user_google_val + '&sz=s0' : '';
    var site_val = (official_media.site_img_url || '').trim();
    var raw_site_photos = p.photo_urls && p.photo_urls.length > 0 ? p.photo_urls : (official_media.site_img_urls || []);
    var site_photos = Array.isArray(raw_site_photos) ? raw_site_photos.filter(function(url) {
        return url && typeof url === 'string' && url.trim().startsWith('http') && !isLocalServerMediaUrl(url);
    }) : [];
    if (site_val && site_val.startsWith('http') && !isLocalServerMediaUrl(site_val) && site_photos.indexOf(site_val) === -1) site_photos.unshift(site_val);
    var is_invalid = function(val) {
        if (!val) return true;
        var low = val.toLowerCase();
        return low === 'null' || low === 'none' || low === '403' || low === '404' || low === 'deprecated' || low === 'unavailable';
    };

    var sources_map = {};
    if (dom === 'JAV' && !is_invalid(local_val)) {
        sources_map['local_img_path'] = {url: formatActorLocalUrl(local_val, dom), type: 'GDS LOCAL', is_final: true};
    }
    if (dom === 'JAV' && !is_invalid(google_val)) {
        sources_map['google_fileid'] = {url: 'https://drive.google.com/thumbnail?id=' + google_val + '&sz=s0', type: 'GDS', is_final: true};
    }
    if (!is_invalid(cached_local_val)) {
        sources_map['cached_local_img_path'] = {url: formatActorLocalUrl(cached_local_val, dom), type: 'CACHE SERVER', is_final: true};
    }
    if (!is_invalid(cached_google_val)) {
        sources_map['official_cached_google_fileid'] = {url: 'https://drive.google.com/thumbnail?id=' + cached_google_val + '&sz=s0', type: 'CACHE GDRIVE', is_final: true};
    }
    if (!is_invalid(custom_local_val)) {
        sources_map['custom_local_img_path'] = {url: custom_local_url, type: 'USER SERVER', is_final: true};
    }
    if (!is_invalid(user_google_val)) {
        sources_map['custom_user_google_fileid'] = {url: customGdriveThumbUrl, type: 'USER GDRIVE', is_final: true};
    }

    if (site_photos.length > 0) {
        var type_name = (dom === 'WESTERN' ? 'SITE' : (dom === 'GENERAL' ? 'WEB' : 'AVDBS'));
        site_photos.forEach(function(s_url, s_idx){
            if (!is_invalid(s_url) && s_url.startsWith('http')) {
                var label_str = site_photos.length > 1 ? (type_name + ' #' + (s_idx + 1)) : type_name;
                sources_map['site_img_url_' + s_idx] = { url: s_url, type: label_str, key_type: 'site_img_url', is_final: false };
            }
        });
    }

    var active_primary_source_key = null;
    var current_thumb = (p.thumb_url || p.thumb || '').trim();
    var currentMatch = current_thumb.match(/[?&]id=([^&]+)/);
    Object.keys(sources_map).some(function(key) {
        var sourceUrl = sources_map[key].url || '';
        var sourceMatch = sourceUrl.match(/[?&]id=([^&]+)/);
        if (current_thumb === sourceUrl || (currentMatch && sourceMatch && currentMatch[1] === sourceMatch[1])) {
            active_primary_source_key = key;
            return true;
        }
        return false;
    });
    if (!active_primary_source_key && current_thumb.startsWith('http')) {
        sources_map['active_thumb'] = {url: current_thumb, type: 'ACTIVE', is_final: true};
        active_primary_source_key = 'active_thumb';
    }
    if (!active_primary_source_key) active_primary_source_key = Object.keys(sources_map)[0];

    var preview_images = [];
    if (active_primary_source_key && sources_map[active_primary_source_key]) {
        preview_images.push(sources_map[active_primary_source_key]);
    }
    Object.keys(sources_map).forEach(function(k) {
        if (k !== active_primary_source_key) {
            preview_images.push(sources_map[k]);
        }
    });

    return {sources: sources_map, primaryKey: active_primary_source_key, images: preview_images};
}

function renderPersonModalContent(p, $modal) {
    if (!p) return;
    $modal = $modal || $('#personEditModal');
    $modal.data('person_data', p);

    var media = p.media_src || {};
    var extra = p.extra_info || {};

    if (typeof extra === 'string') { try { extra = JSON.parse(extra); } catch (e) { extra = {}; } }
    if (typeof media === 'string') { try { media = JSON.parse(media); } catch (e) { media = {}; } }

    $modal.find('#p_edit_id').val(p.id || '');
    $modal.find('#p_edit_domain').val(p.domain || 'JAV');
    var current_gender = String(p.gender || (extra && extra.gender) || 'female').toLowerCase().trim();
    if (current_gender === 'm' || current_gender === '남' || current_gender === '남성') current_gender = 'male';
    else if (current_gender === 'f' || current_gender === '여' || current_gender === '여성') current_gender = 'female';
    $modal.find('#p_edit_gender').val(current_gender || 'female');

    $modal.find('#p_edit_idx').val(p.person_idx || '');
    $modal.find('#p_edit_name_org').val(p.name_org || p.name || '');
    $modal.find('#p_edit_name_ko').val(p.name_ko || '');
    $modal.find('#p_edit_name_en').val(p.name_en || '');
    $modal.find('#p_edit_aliases').val((p.aliases && p.aliases.length > 0) ? p.aliases.join(', ') : (p.other_names || ''));
    $modal.find('#p_edit_thumb').val(p.thumb_url || p.thumb || '');
    $modal.find('#p_edit_selected_primary_url').val('');

    $modal.find('#p_edit_birth').val(p.birth_date || (extra && extra.birth) || '');
    var display_height = (p.height && parseInt(p.height, 10) > 0) ? String(p.height) : (
        (extra && extra.height && parseInt(extra.height, 10) > 0) ? String(extra.height) : ''
    );
    $modal.find('#p_edit_height').val(display_height);
    $modal.find('#p_edit_agency').val(p.agency || (extra && extra.agency) || '');
    $modal.find('#p_edit_blood').val(p.blood_type || (extra && extra.blood) || '');
    $modal.find('#p_edit_hobby').val((extra && (extra.hobby || extra.specialty)) || '');

    $modal.find('#p_edit_body_size').val(p.body_size || (extra && extra.body_size) || '');
    $modal.find('#p_edit_bra_size').val(p.cup_size || (extra && (extra.bra_size || extra.cup_size)) || '');
    $modal.find('#p_edit_debut').val(p.debut_date || (extra && extra.debut) || '');

    var official_media = media.official || media;
    var custom_media = media.custom || {};
    var dom = (p.domain || 'JAV').toUpperCase();
    $modal.find('#btn_person_refresh_site').toggle(dom !== 'JAV');
    var local_val = dom === 'JAV' ? (official_media.local_img_path || '').trim() : '';
    var cached_local_val = (official_media.cached_local_img_path || '').trim();
    var google_val = dom === 'JAV' ? (official_media.google_fileid || '').trim() : '';
    var cached_google_val = (official_media.official_cached_google_fileid || (!media.official ? media.user_google_fileid : '') || p.official_cached_gdrive_fileid || '').trim();
    var site_val = (official_media.site_img_url || '').trim();

    var user_google_val = String(Object.prototype.hasOwnProperty.call(custom_media, 'custom_user_google_fileid')
        ? custom_media.custom_user_google_fileid || ''
        : (media.custom_user_google_fileid || p.custom_gdrive_fileid || '')).trim();
    $modal.find('#p_gds_image_fields').toggle(dom === 'JAV');
    $modal.find('#p_edit_local_img_path').val(local_val);
    $modal.find('#p_edit_google_fileid').val(google_val);
    $modal.find('#p_edit_gdrive_thumb_url').val(google_val ? 'https://drive.google.com/thumbnail?id=' + google_val + '&sz=s0' : '');
    var custom_local_val = (custom_media.local_img_path || '').trim();
    var custom_local_url = custom_local_val ? formatActorLocalUrl(custom_local_val, dom) : '';
    var customGdriveThumbUrl = user_google_val ? 'https://drive.google.com/thumbnail?id=' + user_google_val + '&sz=s0' : '';
    // Keep official and custom assets separate; choose a complete pair per storage.
    $modal.find('#p_edit_custom_user_google_fileid').val(user_google_val).data('managed-google-fileid', false);
    var officialRemotePath = (official_media.remote_img_path || '').trim();
    var customRemotePath = (custom_media.remote_img_path || '').trim();
    var usesCustomLocal = !!custom_local_val;
    var usesCustomRemote = !!(customRemotePath || user_google_val);
    $modal.find('#p_edit_storage_local_path').val(usesCustomLocal ? custom_local_val : cached_local_val);
    $modal.find('#p_edit_official_local_url').val(usesCustomLocal ? custom_local_url :
        (cached_local_val ? formatActorLocalUrl(cached_local_val, dom) : ''));
    $modal.find('#p_edit_storage_remote_path').val(usesCustomRemote ? customRemotePath : officialRemotePath);
    $modal.find('#p_edit_official_gdrive_url').val(usesCustomRemote ? customGdriveThumbUrl :
        (cached_google_val ? 'https://drive.google.com/thumbnail?id=' + cached_google_val + '&sz=s0' : ''));
    $modal.find('#lbl_p_local_path').text('파일 상대 경로 (로컬)' + (usesCustomLocal ? ' · 사용자 이미지' : ''));
    $modal.find('#lbl_p_official_local_url').text('이미지 서버 URL' + (usesCustomLocal ? ' · 사용자 이미지' : ''));
    $modal.find('#lbl_p_remote_path').text('파일 상대 경로 (구글 드라이브)' + (usesCustomRemote ? ' · 사용자 이미지' : ''));
    $modal.find('#lbl_p_official_gdrive_url').text('구글 드라이브 썸네일 URL' + (usesCustomRemote ? ' · 사용자 이미지' : ''));

    var raw_site_photos = (p.photo_urls && p.photo_urls.length > 0) ? p.photo_urls : (official_media.site_img_urls || []);
    var site_photos = Array.isArray(raw_site_photos) ? raw_site_photos.filter(function(u){
        return u && typeof u === 'string' && u.trim().startsWith('http') && !isLocalServerMediaUrl(u);
    }) : [];

    if (site_val && site_val.startsWith('http') && !isLocalServerMediaUrl(site_val) && site_photos.indexOf(site_val) === -1) {
        site_photos.unshift(site_val);
    }
    $modal.find('#p_edit_site_img_urls').val(site_photos.join('\n'));

    var info_url_val = (p.info_url || (extra && extra.info_url) || '').trim();
    $modal.find('#p_edit_info_url').val(info_url_val);

    if (dom === 'JAV' || dom === 'WESTERN') {
        $modal.find('#p_edit_av_spec_div').show();
    } else {
        $modal.find('#p_edit_av_spec_div').hide();
    }

    var person_gallery = buildPersonImageGallery(p, media);
    var sources_map = person_gallery.sources;
    var active_primary_source_key = person_gallery.primaryKey;
    var preview_images = person_gallery.images;

    $modal.find('.badge-primary-source').remove();
    var primaryLabel = {local_img_path: 'lbl_p_edit_local_path', google_fileid: 'lbl_p_gds_thumb_url',
        cached_local_img_path: usesCustomLocal ? null : 'lbl_p_official_local_url',
        official_cached_google_fileid: usesCustomRemote ? null : 'lbl_p_official_gdrive_url',
        custom_local_img_path: usesCustomLocal ? 'lbl_p_official_local_url' : null,
        custom_user_google_fileid: usesCustomRemote ? 'lbl_p_official_gdrive_url' : null}[active_primary_source_key];
    if (!primaryLabel && String(active_primary_source_key || '').indexOf('site_img_url_') === 0) primaryLabel = 'lbl_p_edit_site_url';
    if (primaryLabel) $modal.find('#' + primaryLabel).append(' <span class="badge badge-success badge-primary-source">대표</span>');


    $modal.data('p_preview_images', preview_images);
    $modal.data('p_preview_idx', 0);
    update_p_modal_image_preview($modal);

    var mergedSubList = (p.merged_details && Array.isArray(p.merged_details) && p.merged_details.length > 0) ? p.merged_details : (
        (extra && extra.merged_sub_actors && Array.isArray(extra.merged_sub_actors)) ? extra.merged_sub_actors : []
    );
    if (mergedSubList.length === 0 && p.alt_indices && Array.isArray(p.alt_indices) && p.alt_indices.length > 1) {
        mergedSubList = p.alt_indices.map(function(altId){
            return { actor_id: altId, name_org: p.name_org || '', name_ko: p.name_ko || '', site_img_url: p.thumb || '', info_url: p.info_url || '' };
        });
    } else if (mergedSubList.length === 0 && extra && extra.alt_actor_indices && Array.isArray(extra.alt_actor_indices) && extra.alt_actor_indices.length > 1) {
        mergedSubList = extra.alt_actor_indices.map(function(altId){
            return { actor_id: altId, name_org: p.name_org || '', name_ko: p.name_ko || '', site_img_url: p.thumb || '', info_url: '' };
        });
    }

    var $subWrapper = $modal.find('#p_merged_sub_actors_wrapper');
    var currentMasterIdx = (p.person_idx || '').trim();

    if (mergedSubList.length > 1) {
        $subWrapper.show();
        $modal.find('#p_merged_sub_count_text').text('총 ' + mergedSubList.length + '개 ID 병합됨');

        var badgesHtml = '';
        var tableHtml = '';

        for (var s = 0; s < mergedSubList.length; s++) {
            var subItem = mergedSubList[s];
            var sId = subItem.actor_id || '-';
            var isMaster = (sId === currentMasterIdx);
            var badgeClass = isMaster ? 'badge-primary font-weight-bold shadow-sm' : 'badge-secondary';

            badgesHtml += '<span class="badge ' + badgeClass + ' mr-1 mb-1 p-1" style="font-size: 0.78rem;">' + sId + (isMaster ? ' [대표]' : '') + '</span>';

            var subImgSrc = subItem.site_img_url || '';
            var subImgTag = subImgSrc ? ('<img src="' + subImgSrc + '" style="width: 32px; height: 32px; object-fit: cover; border-radius: 3px;">') : '<span class="text-muted">-</span>';
            var subNameOrg = subItem.name_org || '-';
            var subNameKo = subItem.name_ko ? (' (' + subItem.name_ko + ')') : '';
            var subItemJsonAttr = encodeURIComponent(JSON.stringify(subItem));

            tableHtml += '<tr>';
            tableHtml += '  <td class="align-middle text-center">' + subImgTag + '</td>';
            tableHtml += '  <td class="align-middle font-weight-bold ' + (isMaster ? 'text-primary' : 'text-white') + '">' + sId + (isMaster ? ' <span class="badge badge-primary px-1" style="font-size: 0.72rem;">대표</span>' : '') + '</td>';
            tableHtml += '  <td class="align-middle text-light">' + subNameOrg + subNameKo + '</td>';
            tableHtml += '  <td class="align-middle text-center">';
            if (!isMaster) {
                tableHtml += '<button type="button" class="btn btn-sm btn-outline-info font-weight-bold py-1 px-2 btn_open_sub_person_modal" data-sub-actor="' + subItemJsonAttr + '" style="font-size: 0.75rem; white-space: nowrap;">🔗 상세</button>';
            }
            tableHtml += '</td>';
            tableHtml += '  <td class="align-middle text-center" style="white-space: nowrap;">';
            if (!isMaster) {
                tableHtml += '    <button type="button" class="btn btn-sm btn-outline-primary font-weight-bold py-1 px-2 mr-1 btn_person_sub_set_master" data-person-id="' + p.id + '" data-sub-id="' + sId + '" style="font-size: 0.75rem;">★ 대표 지정</button>';
                tableHtml += '    <button type="button" class="btn btn-sm btn-outline-danger font-weight-bold py-1 px-2 btn_person_sub_split" data-person-id="' + p.id + '" data-sub-id="' + sId + '" style="font-size: 0.75rem;">✂ 그룹 분리</button>';
            } else {
                tableHtml += '    <span class="text-muted small font-weight-bold">현재 대표 인물</span>';
            }
            tableHtml += '  </td>';
            tableHtml += '</tr>';
        }

        $modal.find('#p_merged_sub_badges_container').html(badgesHtml);
        $modal.find('#p_merged_sub_tbody').html(tableHtml);
    } else {
        $subWrapper.hide();
    }

    // 소장 출연작 뱃지 렌더링
    var worksMap = (p.works_detailed && Object.keys(p.works_detailed).length > 0) ? p.works_detailed : (p.works || {});
    var totalWorks = 0;
    var worksHtml = '';

    for (var cat in worksMap) {
        var rawList = worksMap[cat];
        if (Array.isArray(rawList) && rawList.length > 0) {
            // 원본 배열 사본을 만들어 제목순(A-Z / ㄱ-ㅎ)으로 정렬
            var sortedList = rawList.slice().sort(function(a, b){
                var titleA = (typeof a === 'object' && a) ? (a.title || a.ui_code || a.code || '') : String(a);
                var titleB = (typeof b === 'object' && b) ? (b.title || b.ui_code || b.code || '') : String(b);
                return titleA.localeCompare(titleB, 'ko');
            });

            totalWorks += sortedList.length;

            for (var w = 0; w < sortedList.length; w++) {
                var it = sortedList[w];
                var wCode = '';
                var wUiCode = '';
                var wTitle = '';
                var wYear = '';

                if (typeof it === 'object' && it !== null) {
                    wCode = it.code || '';
                    wUiCode = it.ui_code || wCode;
                    wTitle = it.title || '';
                    wYear = (it.year && it.year !== '0' && it.year !== 1900) ? String(it.year) : '';
                } else {
                    wCode = String(it);
                    wUiCode = wCode;
                    wTitle = '';
                    wYear = '';
                }

                var displayCode = wUiCode || wCode;
                var displayTitle = wTitle ? (' ' + wTitle) : '';
                var yearBadge = wYear ? (' <span class="text-muted font-weight-normal ml-1">(' + wYear + ')</span>') : '';
                var tooltipText = '[' + cat + '] [' + displayCode + ']' + displayTitle + (wYear ? ' (' + wYear + ')' : '') + '\n(클릭하여 작품 메타 편집창 열기)';

                worksHtml += '<div class="badge badge-dark mb-1 p-1 work-badge-clickable border border-secondary d-flex align-items-center w-100 text-left" data-code="' + wCode + '" data-cat="' + cat + '" title="' + tooltipText.replace(/"/g, '&quot;') + '" style="font-size: 0.82rem; cursor: pointer;">';
                worksHtml += '  <span class="badge badge-info mr-1 flex-shrink-0" style="font-size: 0.76rem;">' + cat + '</span>';
                worksHtml += '  <span class="badge badge-secondary mr-2 flex-shrink-0" style="font-size: 0.76rem;">' + displayCode + '</span>';
                if (wTitle) {
                    worksHtml += '  <span class="text-light text-truncate font-weight-normal flex-grow-1 work-badge-title" style="min-width: 0;">' + wTitle + yearBadge + '</span>';
                } else {
                    worksHtml += '  <span class="text-muted text-truncate font-weight-normal flex-grow-1 work-badge-title" style="min-width: 0;">(제목 정보 없음)' + yearBadge + '</span>';
                }
                worksHtml += '</div>';
            }
        }
    }

    $modal.find('#p_modal_works_count').text(totalWorks + '편');
    if (totalWorks === 0) {
        worksHtml = '<span class="text-muted small py-1">등록된 소장 출연작이 없습니다. (작품 메타데이터 등록 시 자동 연계)</span>';
    }
    $modal.find('#p_modal_works_container').html(worksHtml);

    var modal_display_name = p.name_ko || p.name_org || p.name || p.name_en || '인물';

    if (!p.id) {
        $modal.find('#person_modal_title').text('[' + modal_display_name + '] 인물 상세 정보 (DB 미등록 - 조회 전용)');
        $modal.find('#btn_person_save')
            .prop('disabled', true)
            .addClass('disabled')
            .text('저장 불가 (DB 미등록)')
            .attr('title', '크롤링 DB에 등록되지 않은 배우는 수동 추가 및 저장이 제한됩니다.');
        $modal.find('#btn_open_person_crop_modal')
            .prop('disabled', true)
            .addClass('disabled')
            .attr('title', 'DB에 등록되지 않은 배우는 사진 편집을 사용할 수 없습니다.');
        $modal.find('.btn_person_reset_user').prop('disabled', true).addClass('disabled');
        if (totalWorks === 0) {
            $modal.find('#p_modal_works_container').html('<span class="text-warning small py-1">⚠️ 크롤링 DB에 등록되지 않은 배우입니다. (신규 수동 추가 및 저장 불가)</span>');
        }
    } else {
        $modal.find('#person_modal_title').text('[' + modal_display_name + '] 인물 상세 정보 편집');
        $modal.find('#btn_person_save')
            .prop('disabled', false)
            .removeClass('disabled')
            .text('저장')
            .attr('title', '인물 정보 저장');
        $modal.find('#btn_open_person_crop_modal')
            .prop('disabled', false)
            .removeClass('disabled')
            .removeAttr('title');
        $modal.find('.btn_person_reset_user').prop('disabled', false).removeClass('disabled');
    }
}

// 인물 모달 단일 공용 로더
function renderDbEditModalContent(row, $modal) {
    if (!row) return;
    $modal = $modal || $('#dbEditModal');

    var jd = row.json_data;
    if (typeof jd === 'string') { try { jd = JSON.parse(jd); } catch (err) { jd = {}; } }
    if (!jd || typeof jd !== 'object') jd = {};

    var rawActors = jd.actor || (jd.extra_info && (jd.extra_info._actors || jd.extra_info.actor_cache)) || [];
    var actorsList = Array.isArray(rawActors) ? rawActors.map(function(actor){
        if (typeof actor === 'string') return { name_org: actor, name_ko: '', name_en: '', role: '출연' };
        if (!actor || typeof actor !== 'object') return null;
        return {
            name_org: actor.name_org || actor.name || '',
            name_ko: actor.name_ko || '',
            name_en: actor.name_en || '',
            thumb: actor.thumb || '',
            actor_idx: actor.actor_idx || actor.person_idx || '',
            gender: actor.gender || (actor.extra_info && actor.extra_info.gender) || '',
            role: actor.role || '출연'
        };
    }).filter(Boolean) : [];
    
    $modal.data('edit_actors', actorsList);
    render_actor_badges($modal);

    var info_url_val = (jd.extra_info && jd.extra_info.info_url) || (jd.spec_data && jd.spec_data.info_url) || jd.info_url || row.info_url || '';
    $modal.find('#edit_info_url').val(info_url_val);

    var orig_data = jd.original || {};
    var orig_thumb = orig_data.thumb || {};
    var orig_extras = orig_data.extras || [];
    var raw_p_url = orig_thumb.poster || '';
    var raw_pl_url = orig_thumb.landscape || '';
    var raw_fanarts = Array.isArray(orig_data.fanart) ? orig_data.fanart : [];

    if (isLocalServerMediaUrl(raw_p_url)) raw_p_url = '';
    if (isLocalServerMediaUrl(raw_pl_url)) raw_pl_url = '';

    var raw_trailer_url = '';
    if (Array.isArray(orig_extras) && orig_extras.length > 0) {
        raw_trailer_url = orig_extras[0].content_url || '';
    } else if (typeof orig_data.trailer === 'string') {
        raw_trailer_url = orig_data.trailer;
    }

    var storedImages = jd.stored_images;
    var final_p_url = storedImages ? (storedImages.poster_url || '') : (jd.poster_url || row.poster_url || '');
    var final_pl_url = storedImages ? (storedImages.landscape_url || '') : (jd.landscape_url || '');
    var final_trailer_url = '';

    if (jd.thumb && Array.isArray(jd.thumb)) {
        var p_th = jd.thumb.find(function(t){ return t && t.aspect === 'poster'; });
        if (!storedImages && p_th && p_th.value) final_p_url = p_th.value;
        var pl_th = jd.thumb.find(function(t){ return t && t.aspect === 'landscape'; });
        if (!storedImages && pl_th && pl_th.value) final_pl_url = pl_th.value;
    }

    if (jd.extras && Array.isArray(jd.extras)) {
        var ex_tr = jd.extras.find(function(ex){ return ex && ex.content_type === 'trailer' && ex.content_url; });
        if (ex_tr) final_trailer_url = ex_tr.content_url;
    }

    var previewList = [];
    var addedRawKeys = new Set();

    if (final_p_url) {
        var cleanFinalP = getCleanSourceUrl(final_p_url);
        var isLocalP = isLocalServerMediaUrl(final_p_url);
        var isGdriveP = final_p_url.indexOf('drive.google.com') !== -1;
        if (cleanFinalP && !addedRawKeys.has(cleanFinalP)) {
            previewList.push({
                url: getDisplayMediaUrl(final_p_url, row.site),
                type: isGdriveP ? 'Poster (GDrive)' : (isLocalP ? 'Poster' : 'Poster (Site)'),
                is_final: isLocalP || isGdriveP
            });
            addedRawKeys.add(cleanFinalP);
        }
    }

    if (final_pl_url) {
        var cleanFinalPl = getCleanSourceUrl(final_pl_url);
        var isLocalPl = isLocalServerMediaUrl(final_pl_url);
        var isGdrivePl = final_pl_url.indexOf('drive.google.com') !== -1;
        if (cleanFinalPl && !addedRawKeys.has(cleanFinalPl)) {
            previewList.push({
                url: getDisplayMediaUrl(final_pl_url, row.site),
                type: isGdrivePl ? 'Landscape (GDrive)' : (isLocalPl ? 'Landscape' : 'Landscape (Site)'),
                is_final: isLocalPl || isGdrivePl
            });
            addedRawKeys.add(cleanFinalPl);
        }
    }

    if (jd.fanart && Array.isArray(jd.fanart)) {
        jd.fanart.forEach(function(f_url, a_i){
            if (!f_url) return;
            var cleanFa = getCleanSourceUrl(f_url);
            var isLocalFa = isLocalServerMediaUrl(f_url);
            var isGdriveFa = f_url.indexOf('drive.google.com') !== -1 || f_url.indexOf('googleusercontent.com') !== -1;
            if (cleanFa && !addedRawKeys.has(cleanFa)) {
                var typeLabel = isGdriveFa ? ('Art (GDrive) #' + (a_i + 1)) : (isLocalFa ? ('Local Art #' + (a_i + 1)) : ('Art #' + (a_i + 1)));
                previewList.push({
                    url: getDisplayMediaUrl(f_url, row.site),
                    type: typeLabel,
                    is_final: isLocalFa || isGdriveFa
                });
                addedRawKeys.add(cleanFa);
            }
        });
    }

    if (raw_p_url) {
        var cleanSiteP = getCleanSourceUrl(raw_p_url);
        if (cleanSiteP && !addedRawKeys.has(cleanSiteP) && !isLocalServerMediaUrl(cleanSiteP)) {
            previewList.push({
                url: getDisplayMediaUrl(raw_p_url, row.site),
                type: 'Poster (Site)',
                is_final: false
            });
            addedRawKeys.add(cleanSiteP);
        }
    }

    if (raw_pl_url) {
        var cleanSitePl = getCleanSourceUrl(raw_pl_url);
        if (cleanSitePl && !addedRawKeys.has(cleanSitePl) && !isLocalServerMediaUrl(cleanSitePl)) {
            previewList.push({
                url: getDisplayMediaUrl(raw_pl_url, row.site),
                type: 'Landscape (Site)',
                is_final: false
            });
            addedRawKeys.add(cleanSitePl);
        }
    }

    if (raw_fanarts.length > 0) {
        raw_fanarts.forEach(function(rf_url, rf_i){
            if (!rf_url) return;
            var cleanSiteOfa = getCleanSourceUrl(rf_url);
            if (cleanSiteOfa && !addedRawKeys.has(cleanSiteOfa) && !isLocalServerMediaUrl(cleanSiteOfa)) {
                previewList.push({
                    url: getDisplayMediaUrl(rf_url, row.site),
                    type: 'Site Art #' + (rf_i + 1),
                    is_final: false
                });
                addedRawKeys.add(cleanSiteOfa);
            }
        });
    }

    $modal.data('preview_images', previewList);
    $modal.data('preview_idx', 0);
    update_modal_image_preview($modal);

    $modal.data('row_data', row);

    $modal.find('#edit_code').val(row.code || '');
    $modal.find('#edit_code_view').val(row.code || '');
    $modal.find('#edit_ui_code').val(row.ui_code || jd.ui_code || row.code || '');
    $modal.find('#edit_site').val(row.site || jd.site || '');

    var ratingVal = '';
    if (jd.rating !== undefined && jd.rating !== null && jd.rating !== '' && jd.rating !== 0) {
        ratingVal = jd.rating;
    } else if (Array.isArray(jd.ratings) && jd.ratings.length > 0 && jd.ratings[0].value !== undefined) {
        ratingVal = jd.ratings[0].value;
    } else if (row.rating !== undefined && row.rating !== null && row.rating !== 0) {
        ratingVal = row.rating;
    }
    $modal.find('#edit_rating').val(ratingVal);

    $modal.find('#edit_premiered').val(jd.premiered || row.premiered || '');
    $modal.find('#edit_year').val(jd.year || row.year || '');
    $modal.find('#edit_runtime').val(jd.runtime || row.runtime || '');
    $modal.find('#edit_studio').val(jd.studio || row.studio || '');
    $modal.find('#edit_series').val(jd.series || row.series || '');
    $modal.find('#edit_director').val(jd.director || row.director || '');

    var currentCat = (row.category || get_list_context().category || '').toUpperCase();
    if (currentCat === 'GENERAL' || currentCat === 'MOVIE' || currentCat === 'KTV' || currentCat === 'FTV') {
        $modal.find('#div_edit_genres').removeClass('col-md-12').addClass('col-md-6');
        $modal.find('#div_edit_mpaa').show();
        $modal.find('#edit_mpaa').val(jd.mpaa || row.mpaa || '');
    } else {
        $modal.find('#div_edit_genres').removeClass('col-md-6').addClass('col-md-12');
        $modal.find('#div_edit_mpaa').hide();
        $modal.find('#edit_mpaa').val(jd.mpaa || row.mpaa || '');
    }

    $modal.find('#edit_title').val(row.title || jd.title || '');
    $modal.find('#edit_tagline').val(jd.tagline || row.tagline || '');

    var genreDisplayStr = '';
    if (Array.isArray(jd.genre) && jd.genre.length > 0) {
        genreDisplayStr = jd.genre.join(', ');
    } else if (Array.isArray(row.genre) && row.genre.length > 0) {
        genreDisplayStr = row.genre.join(', ');
    } else if (typeof row.genres === 'string' && row.genres.trim()) {
        genreDisplayStr = row.genres.trim();
    }
    $modal.find('#edit_genres').val(genreDisplayStr);

    $modal.find('#edit_poster_url').val(raw_p_url);
    $modal.find('#edit_poster_url_final').val(final_p_url).css('cursor', final_p_url ? 'pointer' : 'default').attr('title', final_p_url ? '클릭하여 이미지 크게 보기' : '');

    $modal.find('#edit_landscape_url').val(raw_pl_url);
    $modal.find('#edit_landscape_url_final').val(final_pl_url).css('cursor', final_pl_url ? 'pointer' : 'default').attr('title', final_pl_url ? '클릭하여 이미지 크게 보기' : '');

    var storedMedia = (Array.isArray(jd.media) ? jd.media : []).concat(
        Array.isArray(row.media) ? row.media : [],
        jd.extra_info && Array.isArray(jd.extra_info._storage_media) ? jd.extra_info._storage_media : []
    );
    function imageFileId(value) {
        var clean = typeof getCleanSourceUrl === 'function' ? getCleanSourceUrl(value || '') : String(value || '');
        if (!/^https?:\/\/(?:drive\.google\.com|(?:[^/]+\.)?googleusercontent\.com)\//i.test(clean)) return '';
        var match = clean.match(/[?&]id=([^&#]+)/) || clean.match(/\/d\/([^/?#]+)/);
        if (!match) return '';
        try { return decodeURIComponent(match[1]); } catch (err) { return match[1]; }
    }
    ['poster', 'landscape'].forEach(function(kind) {
        var url = kind === 'poster' ? final_p_url : final_pl_url;
        var id = imageFileId(url);
        var candidates = storedMedia.filter(function(asset) {
            return asset && asset.media_type === kind && url && (asset.url === url ||
                (id && (asset.gdrive_fileid || imageFileId(asset.url)) === id));
        });
        var matched = candidates.find(function(asset) { return Boolean(asset.relative_path); }) || candidates[0];
        $modal.find('#edit_' + kind + '_relative_path').val(matched ? matched.relative_path || '' : '')
            .attr('placeholder', url ? '저장 경로 미기록 (미디어 재동기화 시 등록)' : '저장된 이미지 경로 없음');
        $modal.find('#edit_' + kind + '_url_final').attr('placeholder', /drive\.google\.com|googleusercontent\.com/.test(url || '') ? '구글 썸네일 URL 없음' : '이미지 서버 / 최종 URL 없음');
    });

    // 프리뷰 클립을 배제한 순수 공식 예고편 URL 추출
    var official_trailer_url = raw_trailer_url || '';
    if (!official_trailer_url && final_trailer_url && final_trailer_url.indexOf('mode=preview_') === -1) {
        official_trailer_url = final_trailer_url;
    }

    $modal.find('#edit_trailer_url').val(official_trailer_url);

    // 공식 예고편이 실제로 존재할 때만 트레일러 재생 버튼 노출
    if (official_trailer_url && official_trailer_url.trim() !== '') {
        $modal.find('#btn_modal_play_trailer').show();
    } else {
        $modal.find('#btn_modal_play_trailer').hide();
    }

    // 아카이빙된 트레일러 및 프리뷰 클립 상태 바인딩 (프리뷰 클립 우선)
    var extraData = (jd.extra_info && typeof jd.extra_info === 'object') ? jd.extra_info : (row.extra_info || {});
    var previewClip = extraData.preview_clip;
    var trailerVideo = extraData.trailer_video;

    var isUncen = (window.location.pathname.indexOf('jav_uncensored') !== -1) || (row.category === 'JAV_UNCEN');
    var videoEndpoint = isUncen ? 'jav_video_un' : 'jav_video';
    var hostOrigin = window.location.origin;

    var getVideoUrl = function(clipObj) {
        if (!clipObj) return '';
        if (clipObj.storage_type === 'gdrive' && clipObj.google_fileid) {
            return hostOrigin + '/' + package_name + '/normal/' + videoEndpoint + '?mode=preview_gdrive&fileid=' + clipObj.google_fileid + '&cat=' + (row.category || get_list_context().category);
        } else if (clipObj.local_path) {
            return hostOrigin + '/' + package_name + '/normal/' + videoEndpoint + '?mode=preview_local&path=' + encodeURIComponent(clipObj.local_path);
        }
        return '';
    };

    var previewStreamUrl = getVideoUrl(previewClip);
    var trailerStreamUrl = getVideoUrl(trailerVideo);

    var hasPreview = Boolean(previewClip && (previewClip.google_fileid || previewClip.local_path));
    var hasSavedTrailer = Boolean(trailerVideo && (trailerVideo.google_fileid || trailerVideo.local_path));
    $modal.find('#edit_preview_url').val(previewStreamUrl);
    $modal.find('#edit_saved_trailer_url').val(trailerStreamUrl);
    $modal.find('#edit_preview_path').val(previewClip ? previewClip.relative_path || '' : '');
    $modal.find('#edit_saved_trailer_path').val(trailerVideo ? trailerVideo.relative_path || '' : '');
    $modal.find('#div_preview_clip_box, #btn_modal_play_preview').toggle(hasPreview);
    $modal.find('#div_saved_trailer_box, #btn_modal_play_saved_trailer').toggle(hasSavedTrailer);
    $modal.find('#btn_modal_create_preview').text(hasPreview ? '프리뷰 재생성' : '프리뷰 수동 생성');
    $modal.find('#badge_preview_status').text(hasPreview && hasSavedTrailer ? '트레일러 · 프리뷰 저장됨' : hasPreview ? '프리뷰 저장됨' : hasSavedTrailer ? '트레일러 저장됨' : '미저장');

    $modal.find('#div_preview_source_bar').hide();
    $modal.find('#input_preview_source_path').val(extraData.source_video_path || '');

    $modal.find('#edit_fanarts').val(raw_fanarts.join('\n'));
    $modal.find('#edit_plot').val(jd.plot || row.plot || '');

    var rawInfoUrl = (jd.extra_info && jd.extra_info.info_url) || (jd.spec_data && jd.spec_data.info_url) || row.info_url || '';
    $modal.find('#edit_info_url').val(rawInfoUrl);

    $modal.find('#db_edit_modal_title').text('[' + (row.code || '') + '] 메타데이터 편집');
}

// 작품 모달 단일 공용 로더
var isMovieModalLoading = false;

function loadAndOpenMovieModal(code, category, fallbackData, $targetModal) {
    if (!code) return;
    if (isMovieModalLoading) return;
    isMovieModalLoading = true;

    var $modal = $targetModal || getOrCreateModal('#dbEditModal');
    var targetCat = category || (fallbackData && fallbackData.category) || get_list_context().category || 'JAV_CEN';

    // 반응 속도를 위해 캐시된 기본 데이터로 먼저 모달 오픈
    if (fallbackData) {
        renderDbEditModalContent(fallbackData, $modal);
        $modal.modal('show');
    }

    // 백엔드에서 힐링(Self-Healing) 및 관계 복원이 완료된 최신 메타데이터 수신
    globalSendCommand('get_meta_by_code', String(code), String(targetCat), null, function(ret){
        isMovieModalLoading = false;
        if (ret && ret.ret === 'success' && ret.data) {
            renderDbEditModalContent(ret.data, $modal);
            $modal.modal('show');
        } else if (!fallbackData) {
            if (typeof notify === 'function') notify('[' + code + '] 작품 데이터를 조회하지 못했습니다.', 'warning');
        }
    });
}

// 모달 내부 갱신 작업 시 오버레이 스피너 및 푸터 버튼 상태 제어 헬퍼
function setModalLoadingState($modal, isLoading, actionText, $btn) {
    if (!$modal || !$modal.length) return;
    var $body = $modal.find('.modal-body');

    if (isLoading) {
        $body.css('position', 'relative');
        if ($body.find('.modal-body-overlay').length === 0) {
            var overlayHtml = '<div class="modal-body-overlay d-flex flex-column align-items-center justify-content-center position-absolute w-100 h-100" style="top:0; left:0; background: rgba(13, 17, 23, 0.75); z-index: 1050; backdrop-filter: blur(2px); border-radius: 4px;">' +
                '<div class="spinner-border text-info mb-3" role="status" style="width: 2.8rem; height: 2.8rem;"></div>' +
                '<span class="text-white font-weight-bold" id="modal_refresh_status_text" style="font-size: 0.95rem;">' + (actionText || '데이터 갱신 중...') + '</span>' +
                '</div>';
            $body.append(overlayHtml);
        } else {
            $body.find('#modal_refresh_status_text').text(actionText || '데이터 갱신 중...');
        }

        if ($btn && $btn.length) {
            $btn.data('orig-html', $btn.html());
            $btn.prop('disabled', true).html('<span class="spinner-border spinner-border-sm mr-1" role="status"></span>처리 중...');
        }
        $modal.find('#btn_save_db_edit').prop('disabled', true);
    } else {
        $body.find('.modal-body-overlay').fadeOut(120, function(){ $(this).remove(); });
        if ($btn && $btn.length && $btn.data('orig-html')) {
            $btn.prop('disabled', false).html($btn.data('orig-html'));
        }
        $modal.find('#btn_save_db_edit').prop('disabled', false);
    }
}

function reloadDbEditModalData(code, category, $modal, callback) {
    if (!code) {
        if (typeof callback === 'function') callback();
        return;
    }
    $modal = $modal || $('#dbEditModal');
    var row = $modal.data('row_data') || {};
    var target_cat = category || row.category || get_list_context().category || 'JAV_CEN';

    globalSendCommand('get_meta_by_code', String(code), String(target_cat), null, function(ret){
        if (ret && ret.ret === 'success' && ret.data) {
            renderDbEditModalContent(ret.data, $modal);
            window.globalRequestSearch(null, true);
        }
        if (typeof callback === 'function') callback();
    });
}

var isPersonModalLoading = false;

function loadAndOpenPersonModal(targetIdentifier, targetDomain, $targetModal) {
    if (!targetIdentifier) return;
    if (isPersonModalLoading) return;
    isPersonModalLoading = true;

    var $modal = $targetModal || getOrCreateModal('#personEditModal');
    var domain = targetDomain || (get_current_module_sub() === 'western' ? 'WESTERN' : 'JAV');

    $modal.find('#p_modal_works_container').html('<div class="d-flex align-items-center py-2 text-info small"><span class="spinner-border spinner-border-sm mr-2" role="status"></span>실시간 소장 출연작 정보 조회 중...</div>');
    $modal.modal('show');

    globalSendCommand('person_get_detailed', String(targetIdentifier), String(domain), null, function(ret){
        isPersonModalLoading = false;
        if (ret && ret.ret === 'success' && ret.data) {
            renderPersonModalContent(ret.data, $modal);
        } else {
            globalSendCommand('person_search', String(targetIdentifier), String(domain), JSON.stringify({ include_aliases: true }), function(sRet){
                if (sRet && sRet.ret === 'success' && sRet.data && sRet.data.length > 0) {
                    renderPersonModalContent(sRet.data[0], $modal);
                } else {
                    var dummyUnregPerson = {
                        id: null,
                        domain: domain,
                        person_idx: '',
                        name_org: targetIdentifier,
                        name_ko: targetIdentifier,
                        name_en: '',
                        thumb: '',
                        media_src: {},
                        works: {},
                        works_detailed: {},
                        extra_info: {}
                    };
                    renderPersonModalContent(dummyUnregPerson, $modal);
                    if (typeof notify === 'function') notify('[' + targetIdentifier + '] DB에 등록되지 않은 배우입니다. (수동 추가 불가)', 'warning');
                }
            });
        }
    });
}

$(document).on('click', '.btn_edit_person', function(e){
    e.preventDefault();
    var idx = $(this).data('idx');
    var p = current_person_data[idx];
    if (p) {
        loadAndOpenPersonModal(p.person_idx || p.name_org || p.name_ko || p.id, p.domain || 'JAV');
    }
});

$(document).on('click', '.person-name-clickable', function(e){
    e.preventDefault();
    e.stopPropagation();
    var idx = parseInt($(this).data('idx'), 10);
    var p = (current_person_data && !isNaN(idx) && idx >= 0 && idx < current_person_data.length) ? current_person_data[idx] : null;
    if (p) {
        loadAndOpenPersonModal(p.person_idx || p.name_org || p.name_ko || p.id, p.domain || 'JAV');
    }
});

$(document).on('click', '.actor-title-link-clickable', function(e){
    e.preventDefault();
    e.stopPropagation();

    var target = Number($(this).attr('data-db-person-id'));
    var domain = $(this).attr('data-domain') || $(this).data('domain') || 'JAV';
    if (!Number.isInteger(target) || target <= 0) return;

    loadAndOpenPersonModal(String(target), domain, null);
});

var isActorBadgeRequesting = false;

$(document).on('click', '.actor-badge-clickable', function(e){
    e.preventDefault();
    e.stopPropagation();

    if (isActorBadgeRequesting) return;

    var $badgeText = $(this);
    var $badgeContainer = $badgeText.closest('.badge');
    var actor_name = $badgeText.data('actor-name');
    var actor_idx = $badgeText.data('actor-idx');
    if (!actor_name && !actor_idx) return;

    var current_sub = get_current_module_sub();
    var domain = (current_sub === 'western') ? 'WESTERN' : 'JAV';

    isActorBadgeRequesting = true;
    var originalBadgeHtml = $badgeContainer.html();
    $badgeContainer.css({ 'pointer-events': 'none', 'opacity': '0.7' });
    $badgeText.append(' <span class="spinner-border spinner-border-sm text-info ml-1" role="status" style="width: 0.75rem; height: 0.75rem; vertical-align: middle;"></span>');

    loadAndOpenPersonModal(actor_idx || actor_name, domain, null);

    setTimeout(function(){
        isActorBadgeRequesting = false;
        $badgeContainer.css({ 'pointer-events': '', 'opacity': '' });
        $badgeContainer.html(originalBadgeHtml);
    }, 400);
});

var isWorkBadgeRequesting = false;

$(document).on('click', '.work-badge-clickable', function(e){
    e.preventDefault();
    e.stopPropagation();

    if (isWorkBadgeRequesting) return;

    var $badge = $(this);
    var code = $badge.attr('data-code') || $badge.data('code');
    var cat = $badge.attr('data-cat') || $badge.data('cat') || 'JAV_CEN';

    if (!code) return;

    isWorkBadgeRequesting = true;
    var originalBadgeContent = $badge.html();
    $badge.css({ 'pointer-events': 'none', 'opacity': '0.7' });
    $badge.append(' <span class="spinner-border spinner-border-sm text-info ml-1" role="status" style="width: 0.7rem; height: 0.7rem; vertical-align: middle;"></span>');

    loadAndOpenMovieModal(code, cat, null);

    setTimeout(function(){
        isWorkBadgeRequesting = false;
        $badge.css({ 'pointer-events': '', 'opacity': '' });
        $badge.html(originalBadgeContent);
    }, 400);
});

$(document).on('click', '#btn_search_actor_works', function(e){
    e.preventDefault();
    e.stopPropagation();
    var $modal = $(this).closest('.modal');
    var actor_name = $modal.find('#p_edit_name_ko').val() || $modal.find('#p_edit_name_org').val() || $modal.find('#p_edit_name_en').val();
    if (!actor_name) return;

    var domain = ($modal.find('#p_edit_domain').val() || 'JAV').toUpperCase();
    var target_sub = (domain === 'WESTERN') ? 'western' : 'jav_censored';

    var target_storage_pfx = target_sub + '_dblist_';
    localStorage.setItem(target_storage_pfx + 'search_word', actor_name);
    localStorage.setItem(target_storage_pfx + 'current_page', '1');

    var target_url = '/' + package_name + '/' + target_sub + '/meta_list';
    window.open(target_url, '_blank');
});

$(document).on('click', '#btn_sync_actor_works', function(e){
    e.preventDefault();
    e.stopPropagation();

    var $modal = $(this).closest('.modal');
    var pData = $modal.data('person_data') || {};
    var targetId = $modal.find('#p_edit_idx').val() || pData.person_idx || $modal.find('#p_edit_id').val() || pData.id;
    var domain = $modal.find('#p_edit_domain').val() || pData.domain || 'JAV';

    if (!targetId) {
        if (typeof notify === 'function') notify('검증할 인물 식별자가 없습니다.', 'warning');
        return;
    }

    var $btn = $(this);
    var origHtml = $btn.html();
    $btn.prop('disabled', true).html('<span class="spinner-border spinner-border-sm mr-1" role="status"></span>검증 중...');

    globalSendCommand('person_verify_works', String(targetId), String(domain), null, function(ret){
        $btn.prop('disabled', false).html(origHtml);
        if (ret && ret.ret === 'success') {
            var successMsg = ret.msg || '출연작 검증 및 동기화가 완료되었습니다.';
            if (typeof notify === 'function') notify(successMsg, 'success');
            if (ret.works_detailed) {
                pData.works_detailed = ret.works_detailed;
                pData.works = ret.works_detailed;
                $modal.data('person_data', pData);
                renderPersonModalContent(pData, $modal);
            }
        } else {
            var errMsg = (ret && (ret.msg || ret.message)) ? (ret.msg || ret.message) : '출연작 검증에 실패했습니다.';
            if (typeof notify === 'function') notify(errMsg, 'warning');
        }
    });
});

$(document).on('click', '#btn_person_refresh_site', function(e){
    e.preventDefault();
    e.stopPropagation();

    var $modal = $(this).closest('.modal');
    var pData = $modal.data('person_data') || {};
    var targetId = $modal.find('#p_edit_idx').val() || pData.person_idx || $modal.find('#p_edit_id').val() || pData.id;
    var domain = $modal.find('#p_edit_domain').val() || pData.domain || 'WESTERN';

    if (!targetId) {
        if (typeof notify === 'function') notify('조회할 인물 식별자가 없습니다.', 'warning');
        return;
    }

    var $btn = $(this);
    var origHtml = $btn.html();
    $btn.prop('disabled', true).html('<span class="spinner-border spinner-border-sm mr-1" role="status"></span>갱신 중...');

    globalSendCommand('person_refresh_from_site', String(targetId), String(domain), null, function(ret){
        $btn.prop('disabled', false).html(origHtml);
        if (ret && ret.ret === 'success') {
            if (typeof notify === 'function') notify(ret.msg, 'success');
            if (ret.data) {
                renderPersonModalContent(ret.data, $modal);
            }
            window.globalRequestSearch(null, true);
        } else {
            var errMsg = (ret && ret.msg) ? ret.msg : '정보 갱신에 실패했습니다.';
            if (typeof notify === 'function') notify(errMsg, 'warning');
        }
    });
});

$(document).on('click', '#btn_open_person_info_url', function(e) {
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var url = $modal.find('#p_edit_info_url').val().trim();

    if (!url) {
        var pIdx = ($modal.find('#p_edit_idx').val() || '').trim();
        if (pIdx.startsWith('PS')) {
            url = 'https://stashdb.org/performers/' + pIdx.substring(2);
        } else if (pIdx.startsWith('PP') || pIdx.startsWith('PT')) {
            url = 'https://theporndb.net/performers/' + pIdx.substring(2);
        } else if (pIdx.startsWith('PA')) {
            var rawNum = pIdx.replace(/\D/g, '');
            if (rawNum) url = 'https://www.avdbs.com/menu/actor.php?actor_idx=' + rawNum;
        }
    }

    if (url && url.startsWith('http')) {
        window.open(url, '_blank');
    } else {
        if (typeof notify === 'function') notify('유효한 상세 정보 링크가 없습니다.', 'warning');
    }
});

$(document).on('click', '#btn_p_preview_prev', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var images = $modal.data('p_preview_images') || p_modal_preview_images || [];
    var idx = ($modal.data('p_preview_idx') !== undefined) ? parseInt($modal.data('p_preview_idx'), 10) : 0;
    if (images.length <= 1) return;
    idx = (idx - 1 + images.length) % images.length;
    $modal.data('p_preview_idx', idx);
    update_p_modal_image_preview($modal);
});

$(document).on('click', '#btn_p_preview_next', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var images = $modal.data('p_preview_images') || p_modal_preview_images || [];
    var idx = ($modal.data('p_preview_idx') !== undefined) ? parseInt($modal.data('p_preview_idx'), 10) : 0;
    if (images.length <= 1) return;
    idx = (idx + 1) % images.length;
    $modal.data('p_preview_idx', idx);
    update_p_modal_image_preview($modal);
});

$(document).on('click', '#btn_set_primary_person_photo', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var images = $modal.data('p_preview_images') || [];
    var idx = $modal.data('p_preview_idx') || 0;
    if (images.length === 0) return;

    var cur = images[idx];
    $modal.find('#p_edit_thumb').val(cur.url);
    $modal.find('#p_edit_selected_primary_url').val(cur.url);

    images.forEach(function(item, i) {
        item.is_primary = (i === idx);
    });
    $modal.data('p_preview_images', images);

    update_p_modal_image_preview($modal);

    if (typeof notify === 'function') {
        notify('[' + cur.type + '] 이미지가 대표 프로필 사진으로 지정되었습니다.', 'success');
    }
});

$(document).on('input change', '#p_edit_thumb', function(){
    var $modal = $(this).closest('.modal');
    var url = $(this).val().trim();
    if (url) {
        $modal.find('#p_modal_preview_img').attr('src', url).attr('data-src', url).show();
        $modal.find('#p_modal_no_img').hide();
    } else {
        $modal.find('#p_modal_preview_img').attr('src', '').attr('data-src', '').hide();
        $modal.find('#p_modal_no_img').show();
    }
});

$(document).on('click', '#btn_person_save, #btn_save_person', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var personId = $modal.find('#p_edit_id').val();
    if (!personId) {
        if (typeof notify === 'function') notify('크롤링 DB에 등록되지 않은 배우는 수동으로 추가하거나 저장할 수 없습니다.', 'warning');
        return;
    }
    var heightVal = parseInt($modal.find('#p_edit_height').val(), 10);
    var site_img_urls_text = $modal.find('#p_edit_site_img_urls').val().split(/\r?\n/).map(function(v){ return v.trim(); }).filter(Boolean);

    var payload = {
        id: $modal.find('#p_edit_id').val() ? parseInt($modal.find('#p_edit_id').val(), 10) : null,
        domain: $modal.find('#p_edit_domain').val(),
        gender: $modal.find('#p_edit_gender').val() || 'female',
        person_idx: $modal.find('#p_edit_idx').val().trim(),
        name_org: $modal.find('#p_edit_name_org').val().trim(),
        name_ko: $modal.find('#p_edit_name_ko').val().trim(),
        name_en: $modal.find('#p_edit_name_en').val().trim(),
        thumb: $modal.find('#p_edit_thumb').val().trim(),
        selected_primary_url: $modal.find('#p_edit_selected_primary_url').val().trim(),
        aliases: $modal.find('#p_edit_aliases').val().trim(),
        birth: $modal.find('#p_edit_birth').val().trim(),
        height: (heightVal > 0) ? heightVal : null,
        debut: $modal.find('#p_edit_debut').val().trim(),
        body_size: $modal.find('#p_edit_body_size').val().trim(),
        bra_size: $modal.find('#p_edit_bra_size').val().trim(),
        info_url: $modal.find('#p_edit_info_url').val().trim(),
        agency: $modal.find('#p_edit_agency').val().trim(),
        blood: $modal.find('#p_edit_blood').val().trim(),
        hobby: $modal.find('#p_edit_hobby').val().trim(),
        local_img_path: $modal.find('#p_edit_local_img_path').val().trim(),
        google_fileid: $modal.find('#p_edit_google_fileid').val().trim(),
        custom_user_google_fileid: $modal.find('#p_edit_custom_user_google_fileid').data('managed-google-fileid')
            ? ''
            : (($modal.find('#p_edit_custom_user_google_fileid').val() || '').trim()),
        site_img_url: ($modal.find('#p_edit_site_img_url').val() || '').trim(),
        site_img_urls: site_img_urls_text,
        person_type: 'actor'
    };

    if (payload.domain === 'WESTERN') { delete payload.local_img_path; delete payload.google_fileid; }

    if (payload.domain === 'JAV' && !payload.name_ko) {
        if (typeof notify === 'function') notify('JAV 인물은 한국어 표기명(name_ko)이 필수입니다.', 'warning');
        return;
    }

    if (!payload.name_org && !payload.name_ko) {
        if (typeof notify === 'function') notify('원문 이름 또는 한국어 표기명을 입력하세요.', 'warning');
        return;
    }

    globalSendCommand('person_save', JSON.stringify(payload), null, null, function(ret){
        if (ret.ret === 'success') {
            if (typeof notify === 'function') notify(ret.msg, 'success');
            $modal.modal('hide');
            window.globalRequestSearch(null, true);
        } else {
            if (typeof notify === 'function') notify('저장 실패: ' + ret.msg, 'warning');
        }
    });

});

$(document).on('click', '.btn_person_reset_user', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var personId = $modal.find('#p_edit_id').val();
    var resetType = $(this).data('reset-type');
    if (!personId) return;

    var labels = {
        text: '수동 텍스트 편집',
        image: '사용자 이미지 파일',
        all: '모든 사용자 데이터와 사용자 이미지 파일'
    };
    var label = labels[resetType] || '사용자 데이터';
    if (!confirm('[' + label + ']을 초기화합니다.\n사용자 이미지 초기화가 포함된 경우 실제 파일도 삭제됩니다.\n계속하시겠습니까?')) return;

    var $button = $(this);
    $button.prop('disabled', true);
    globalSendCommand('person_reset_user_data', String(personId), JSON.stringify({reset_type: resetType}), function(ret){
        $button.prop('disabled', false);
        if (ret && ret.ret === 'success') {
            notify(ret.msg || '초기화 완료', 'success');
            $modal.modal('hide');
            window.globalRequestSearch(null, true);
        } else {
            notify('초기화 실패: ' + ((ret && ret.msg) || '알 수 없는 오류'), 'warning');
        }
    });
});

$(document).on('click', '.btn_delete_person', function(e){
    e.preventDefault();
    var pid = $(this).data('id');
    openMetadataDeleteModal([{kind:'person', id:pid}]);
});

$(document).on('click', '#btn_person_db_clear', function(e){
    e.preventDefault();
    var current_sub = get_current_module_sub();
    var sel_domain = $("#search_domain").val() || (current_sub === 'western' ? 'WESTERN' : 'JAV');
    var domain_label = (sel_domain === 'all') ? '전체' : sel_domain;

    if (!confirm("⚠️ 주의!\n[" + domain_label + "] 도메인의 모든 인물 데이터가 삭제됩니다.\n계속 진행하시겠습니까?")) {
        return;
    }

    globalSendCommand('person_clear', sel_domain, null, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg, ret.ret === 'success' ? 'success' : 'warning');
        if (ret.ret === 'success') {
            window.globalRequestSearch(null, true);
        }
    });
});

$(document).on('click', '#btn_db_clear', function(e){
    e.preventDefault();
    var current_sub = get_current_module_sub();
    var list_ctx = get_list_context();
    var target_label = list_ctx.category || ((current_sub === 'western') ? 'WESTERN' : ((current_sub === 'jav_uncensored') ? 'JAV_UNCEN' : 'JAV_CEN'));

    var confirm_label = (target_label === 'AV_ALL') ? '전체 AV (통합)' : target_label;
    if (!confirm("⚠️ 주의!\n['" + confirm_label + "'] 카테고리의 메타 DB를 초기화합니다.\n계속 진행하시겠습니까?")) return;

    globalSendCommand('db_clear', target_label, null, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg, ret.ret === 'success' ? 'success' : 'warning');
        if (ret.ret === 'success') {
            window.globalRequestSearch('1');
        }
    });
});

$(document).on('click', '#globalSettingSaveBtn', function(e){
    e.preventDefault();
    var form = $(this).closest('form');
    if (form.length > 0 && typeof form[0].checkValidity === 'function') {
        if (!form[0].checkValidity()) {
            form[0].reportValidity();
            return;
        }
    }
    if (typeof save_setting === 'function') {
        save_setting(e);
        return;
    }
    if (typeof saveSettings === 'function') {
        saveSettings(e);
        return;
    }
    if (typeof form !== 'undefined' && form.length > 0) {
        form.trigger('submit');
    }
});

$(document).off('click', '.custom-dropdown-toggle').on('click', '.custom-dropdown-toggle', function(e){
    e.preventDefault();
    e.stopPropagation();
    var $parent = $(this).closest('.dropdown');
    var isAlreadyOpen = $parent.hasClass('show');
    
    $('.dropdown').removeClass('show').find('.dropdown-menu').removeClass('show');
    
    if (!isAlreadyOpen) {
        $parent.addClass('show').find('.dropdown-menu').addClass('show');
    }
});

$(document).off('mouseenter', '.modal-footer .dropdown').on('mouseenter', '.modal-footer .dropdown', function(){
    var $dropdown = $(this);
    if (modalDropdownHoverTimer) {
        clearTimeout(modalDropdownHoverTimer);
        modalDropdownHoverTimer = null;
    }
    $('.modal-footer .dropdown').not($dropdown).removeClass('show').find('.dropdown-menu').removeClass('show');
    $dropdown.addClass('show').find('.dropdown-menu').addClass('show');
});

$(document).off('mouseleave', '.modal-footer .dropdown').on('mouseleave', '.modal-footer .dropdown', function(){
    var $dropdown = $(this);
    modalDropdownHoverTimer = setTimeout(function(){
        $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');
    }, 180);
});

$(document).on('click', function(e){
    if (!$(e.target).closest('.dropdown').length) {
        if (modalDropdownHoverTimer) {
            clearTimeout(modalDropdownHoverTimer);
            modalDropdownHoverTimer = null;
        }
        $('.dropdown').removeClass('show').find('.dropdown-menu').removeClass('show');
    }
});

$(document).off('click', '.btn_edit_db').on('click', '.btn_edit_db', function(e){
    e.preventDefault();
    e.stopPropagation();
    var idx = parseInt($(this).data('idx'), 10);
    var row = (current_data && !isNaN(idx) && idx >= 0 && idx < current_data.length) ? current_data[idx] : null;
    if (row) {
        loadAndOpenMovieModal(row.code, row.category, row);
    } else {
        if (typeof notify === 'function') notify('선택한 행의 데이터를 찾을 수 없습니다.', 'warning');
    }
});

$(document).on('click', '#btn_save_db_edit', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var row = $modal.data('row_data') || (current_data && current_data.find(function(it){ return (it.code || '') === ($modal.find('#edit_code').val() || ''); })) || {};
    var jd = row.json_data;
    if (typeof jd === 'string') { try { jd = JSON.parse(jd); } catch (err) { jd = {}; } }
    if (!jd || typeof jd !== 'object') jd = {};

    var payload = normalizeDbEditPayload(row, jd, $modal);
    if (!payload.code) {
        if (typeof notify === 'function') notify('저장할 코드가 비어 있습니다.', 'warning');
        return;
    }

    globalSendCommand('db_edit_save', payload.code, JSON.stringify(payload), null, function(ret){
        if (typeof notify === 'function') notify(ret.msg || 'DB 저장 결과', ret.ret === 'success' ? 'success' : 'warning');
        if (ret.ret === 'success') {
            $modal.modal('hide');
            window.globalRequestSearch(null, true);
        }
    });
});

$(document).on('click', '#btn_view_db_json', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var code = $modal.find('#edit_code').val();
    var row = $modal.data('row_data') || (current_data && current_data.find(function(it){ return (it.code || '') === code; })) || {};

    var jd = row.json_data;
    if (typeof jd === 'string') { try { jd = JSON.parse(jd); } catch (err) { jd = {}; } }
    if (!jd || typeof jd !== 'object') jd = {};

    var payload = normalizeDbEditPayload(row, jd, $modal);
    var jsonOutput = JSON.parse(JSON.stringify(payload));
    if (!(jsonOutput.thumb || []).some(function(t) { return t.aspect === 'poster' && t.value; })) {
        var displayLandscape = (jsonOutput.thumb || []).find(function(t) { return t.aspect === 'landscape' && t.value; });
        if (displayLandscape) {
            jsonOutput.thumb = (jsonOutput.thumb || []).concat([Object.assign({}, displayLandscape, {aspect: 'poster'})]);
            jsonOutput.poster_url = displayLandscape.value;
        }
    }
    delete jsonOutput.stored_images;
    var isWestern = (row.category === 'WESTERN' || (typeof sub !== 'undefined' && sub === 'western'));
    var includeMale = (typeof window.western_json_include_male !== 'undefined') ? window.western_json_include_male : false;

    if (isWestern && !includeMale && Array.isArray(jsonOutput.actor)) {
        var femalesOnly = jsonOutput.actor.filter(function(a){
            var g = String(a.gender || '').toLowerCase().trim();
            return g === 'female' || g === 'f' || g === '여' || g === '여성';
        });
        if (femalesOnly.length > 0) {
            jsonOutput.actor = femalesOnly;
        }
    }

    if (Array.isArray(jsonOutput.actor)) {
        jsonOutput.actor.forEach(function(a){
            delete a.gender;
        });
    }

    var formattedJson = JSON.stringify(jsonOutput, null, 2);

    $('#db_json_modal_title').text('[' + (code || 'JSON') + '] 메타데이터 JSON 원본');
    $('#db_json_modal_textarea').val(formattedJson);
    $('#dbJsonViewModal').modal('show');
});

$(document).on('click', '#btn_copy_db_json', function(e){
    e.preventDefault();
    var text = $('#db_json_modal_textarea').val();
    if (!text) return;
    copyTextToClipboard(text, 'JSON 데이터가 클립보드에 복사되었습니다.');
});

function copyTextToClipboard(text, successMsg) {
    if (!text) return;
    var msg = successMsg || '클립보드에 복사되었습니다.';
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function(){
            if (typeof notify === 'function') notify(msg, 'success');
        }).catch(function(){
            fallbackExecCopy(text, msg);
        });
    } else {
        fallbackExecCopy(text, msg);
    }
}

function fallbackExecCopy(text, msg) {
    var tempTextarea = document.createElement('textarea');
    tempTextarea.value = text;
    tempTextarea.style.position = 'fixed';
    tempTextarea.style.left = '-9999px';
    tempTextarea.style.top = '0';
    document.body.appendChild(tempTextarea);
    tempTextarea.focus();
    tempTextarea.select();
    try {
        var successful = document.execCommand('copy');
        if (successful && typeof notify === 'function') {
            notify(msg, 'success');
        }
    } catch (err) {
        if (typeof notify === 'function') notify('클립보드 복사에 실패했습니다.', 'warning');
    }
    document.body.removeChild(tempTextarea);
}

$(document).on('click', '.item-title-clickable', function(e){
    e.preventDefault();
    e.stopPropagation();
    var idx = parseInt($(this).data('idx'), 10);
    var row = (current_data && !isNaN(idx) && idx >= 0 && idx < current_data.length) ? current_data[idx] : null;
    if (row) {
        loadAndOpenMovieModal(row.code, row.category, row);
    }
});

$(document).on('click', '.badge-copy-code', function(e){
    e.preventDefault();
    e.stopPropagation();
    var code = $(this).attr('data-code') || $(this).text().trim();
    if (!code) return;
    copyTextToClipboard(code, '[' + code + '] 코드가 클립보드에 복사되었습니다.');
});

$(document).off('click', '#edit_poster_url_final, #edit_landscape_url_final').on('click', '#edit_poster_url_final, #edit_landscape_url_final', function(e){
    e.preventDefault();
    var targetUrl = $(this).val().trim();
    if (targetUrl) {
        window.openImageEnlargeModal(targetUrl);
    }
});

// 트레일러 URL 입력값 변경 시 공식 재생 버튼 노출 여부 동적 갱신
$(document).on('input change', '#edit_trailer_url', function(){
    var val = $(this).val().trim();
    var $btn = $(this).closest('.modal').find('#btn_modal_play_trailer');
    if (val && val.indexOf('mode=preview_') === -1) {
        $btn.show();
    } else {
        $btn.hide();
    }
});

$(document).off('click', '#btn_open_meta_info_url').on('click', '#btn_open_meta_info_url', function(e) {
    e.preventDefault();
    var url = $(this).closest('.modal').find('#edit_info_url').val().trim();
    if (url && url.startsWith('http')) {
        window.open(url, '_blank');
    } else {
        if (typeof notify === 'function') notify('유효한 정보 출처 링크가 없습니다.', 'warning');
    }
});

function openVideoPlayerModal(targetUrl, title) {
    if (!targetUrl) return;
    resetVideoPointerInput();
    clearVideoKeyFeedback();
    var player = document.getElementById('video_preview_player');
    var $modal = $('#videoPreviewModal');
    $('#video_preview_modal_title').text('[' + (title || '예고편 비디오') + '] 예고편 비디오');

    if (player) {
        isVideoAudioRestoring = true;

        var savedVolRaw = localStorage.getItem('video_preview_volume');
        var savedMutedRaw = localStorage.getItem('video_preview_muted');

        var targetVolume = (savedVolRaw !== null && !isNaN(parseFloat(savedVolRaw)))
            ? Math.max(0, Math.min(1, parseFloat(savedVolRaw)))
            : 0.5;
        var targetMuted = (savedMutedRaw === 'true');

        var applyAudioSettings = function() {
            try {
                player.volume = targetVolume;
                player.muted = targetMuted;
            } catch (err) {}
        };

        player.src = targetUrl;
        applyAudioSettings();

        var onMediaReady = function() {
            applyAudioSettings();
            setTimeout(function() {
                isVideoAudioRestoring = false;
            }, 300);
        };

        $(player).off('loadedmetadata.audio_state canplay.audio_state playing.audio_state')
                 .one('loadedmetadata.audio_state canplay.audio_state playing.audio_state', onMediaReady);

        player.load();
        var playPromise = player.play();
        if (playPromise !== undefined) {
            playPromise.then(function() {
                applyAudioSettings();
                setTimeout(function() { isVideoAudioRestoring = false; }, 300);
            }).catch(function() {
                setTimeout(function() { isVideoAudioRestoring = false; }, 300);
            });
        }
    }
    $modal.modal('show');
}

$(document).off('click', '#btn_modal_play_trailer').on('click', '#btn_modal_play_trailer', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var rawUrl = $modal.find('#edit_trailer_url').val().trim();
    if (!rawUrl) {
        if (typeof notify === 'function') notify('재생할 예고편 비디오 URL이 없습니다.', 'warning');
        return;
    }
    var currentTitle = $modal.find('#edit_title').val() || '예고편 비디오';
    var site = $modal.find('#edit_site').val() || '';
    var playUrl = getDisplayMediaUrl(rawUrl, site, 'video');
    openVideoPlayerModal(playUrl, currentTitle);
});

$(document).off('click', '#btn_modal_create_preview').on('click', '#btn_modal_create_preview', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var $sourceBar = $modal.find('#div_preview_source_bar');

    $sourceBar.slideToggle(120, function(){
        if ($(this).is(':visible')) {
            $modal.find('#input_preview_source_path').trigger('focus').select();
        }
    });
});

$(document).off('click', '#btn_cancel_preview_source').on('click', '#btn_cancel_preview_source', function(e){
    e.preventDefault();
    $(this).closest('#div_preview_source_bar').slideUp(120);
});

$(document).off('click', '#btn_confirm_create_preview').on('click', '#btn_confirm_create_preview', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var code = $modal.find('#edit_code').val();
    var row = $modal.data('row_data') || {};
    var inputVideoPath = $modal.find('#input_preview_source_path').val().trim();

    if (!inputVideoPath) {
        if (typeof notify === 'function') notify('프리뷰를 추출할 원본 동영상 파일의 전체 경로를 입력하세요.', 'warning');
        $modal.find('#input_preview_source_path').trigger('focus');
        return;
    }

    var $btn = $(this);
    var origText = $btn.text();
    $btn.prop('disabled', true).text('생성 중...');

    if (typeof notify === 'function') notify('[' + code + '] 프리뷰 클립 생성을 시작합니다...', 'info');

    var currentCat = row.category || get_list_context().category;
    var payload = { video_path: inputVideoPath };

    globalSendCommand('make_preview_clip', code, currentCat, JSON.stringify(payload), function(ret){
        $btn.prop('disabled', false).text(origText);
        if (ret && ret.ret === 'success') {
            $modal.find('#div_preview_source_bar').slideUp(120);
            var successMsg = ret.msg || ret.message || '프리뷰 클립이 성공적으로 생성되었습니다.';
            if (typeof notify === 'function') notify(successMsg, 'success');
            reloadDbEditModalData(code, currentCat, $modal);
        } else {
            var failMsg = (ret && (ret.msg || ret.message || ret.data || ret.log)) || '프리뷰 클립 생성 실패 (응답 없음)';
            if (typeof notify === 'function') notify(failMsg, 'warning');
        }
    });
});

// 프리뷰 클립 스트림 주소 복사 핸들러
$(document).on('click', '#btn_copy_preview_url', function(e){
    e.preventDefault();
    var url = $(this).closest('.modal').find('#edit_preview_url').val().trim();
    if (!url) {
        if (typeof notify === 'function') notify('복사할 프리뷰 스트림 주소가 없습니다.', 'warning');
        return;
    }
    copyTextToClipboard(url, '프리뷰 재생 주소가 클립보드에 복사되었습니다.');
});

$(document).off('click', '#btn_modal_play_preview').on('click', '#btn_modal_play_preview', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var streamUrl = $modal.find('#edit_preview_url').val().trim();

    if (!streamUrl) {
        if (typeof notify === 'function') notify('등록된 프리뷰 클립 주소가 없습니다.', 'warning');
        return;
    }

    var currentTitle = $modal.find('#edit_title').val() || '프리뷰 영상';
    openVideoPlayerModal(streamUrl, '[프리뷰] ' + currentTitle);
});

$(document).on('click', '#btn_modal_play_saved_trailer', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var url = $modal.find('#edit_saved_trailer_url').val();
    if (url) openVideoPlayerModal(url, '저장된 트레일러');
});

$(document).off('click', '.btn-delete-stored-video').on('click', '.btn-delete-stored-video', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var code = $modal.find('#edit_code').val();
    var row = $modal.data('row_data') || {};

    var videoType = $(this).data('video-type');
    if (!confirm('선택한 ' + (videoType === 'trailer' ? '트레일러' : '프리뷰') + ' 영상 파일과 메타 정보를 삭제하시겠습니까?')) {
        return;
    }

    var currentCat = row.category || get_list_context().category;
    globalSendCommand('delete_preview_clip', code, currentCat, videoType, function(ret){
        if (ret && ret.ret === 'success') {
            if (typeof notify === 'function') notify(ret.msg, 'success');
            reloadDbEditModalData(code, currentCat, $modal);
        } else {
            if (typeof notify === 'function') notify(ret ? ret.msg : '삭제 실패', 'warning');
        }
    });
});

$(document).on('click', '.btn-play-trailer-modal', function(e){
    e.preventDefault();
    e.stopPropagation();
    var targetUrl = $(this).attr('data-url');
    var rawTitle = $(this).attr('data-title');
    var title = rawTitle ? decodeURIComponent(rawTitle) : '예고편 비디오 미리보기';
    openVideoPlayerModal(targetUrl, title);
});

document.addEventListener('volumechange', function(e){
    if (isVideoAudioRestoring) return;
    var player = e.target;
    if (!player || player.id !== 'video_preview_player') return;
    if (player.readyState === 0) return;
    if (!$('#videoPreviewModal').hasClass('show')) return;

    try {
        localStorage.setItem('video_preview_volume', player.volume.toString());
        localStorage.setItem('video_preview_muted', player.muted ? 'true' : 'false');
    } catch(err){}
}, true);

// 타임라인 슬라이더나 뮤트 클릭/드래그 후 네이티브 컨트롤 포커스를 해제하고 모달로 포커스 복원
$(document).on('mouseup pointerup touchend', '#videoPreviewModal video, #videoPreviewModal .modal-body', function(){
    setTimeout(function(){
        if (videoControlsTimer !== null || (videoPointerDrag && videoPointerDrag.active)) return;
        var player = document.getElementById('video_preview_player');
        if (player && document.activeElement === player) {
            player.blur();
        }
        $('#videoPreviewModal').trigger('focus');
    }, 60);
});

// 탐색(seek) 완료 시 포커스를 모달로 복귀시켜 ESC 및 단축키 동작 보장
document.addEventListener('seeked', function(e){
    if (e.target && e.target.id === 'video_preview_player') {
        setTimeout(function(){
            if (videoControlsTimer !== null || (videoPointerDrag && videoPointerDrag.active)) return;
            if (document.activeElement === e.target) {
                e.target.blur();
            }
            $('#videoPreviewModal').trigger('focus');
        }, 60);
    }
}, true);

var videoKeyFeedbackTimer = null;
var videoControlsTimer = null;

function clearVideoKeyFeedback() {
    clearTimeout(videoKeyFeedbackTimer);
    videoKeyFeedbackTimer = null;
    clearTimeout(videoControlsTimer);
    videoControlsTimer = null;
    var feedback = document.getElementById('video_key_feedback');
    if (feedback) {
        feedback.classList.remove('visible');
        feedback.textContent = '';
    }
}

function revealVideoControls(player) {
    clearTimeout(videoControlsTimer);
    // Native controls reveal themselves on focus. Refresh focus for repeated
    // keys so the browser also restarts its controls auto-hide timer.
    if (document.activeElement === player) player.blur();
    player.focus({preventScroll: true});
    videoControlsTimer = setTimeout(function() {
        videoControlsTimer = null;
        if (videoPointerDrag && videoPointerDrag.active) return;
        if (document.activeElement === player && $('#videoPreviewModal').hasClass('show')) {
            player.blur();
            $('#videoPreviewModal').trigger('focus');
        }
    }, 2500);
}

function showVideoKeyFeedback(message) {
    var feedback = document.getElementById('video_key_feedback');
    if (!feedback || !$('#videoPreviewModal').hasClass('show')) return;
    clearTimeout(videoKeyFeedbackTimer);
    feedback.textContent = message;
    feedback.classList.add('visible');
    videoKeyFeedbackTimer = setTimeout(function() {
        feedback.classList.remove('visible');
        videoKeyFeedbackTimer = null;
    }, 900);
}

// Mouse-only fine controls; native controls keep the bottom 70px of the video.
var videoPointerDrag = null;
var videoWheelInput = {axis: null, amount: 0, timer: null, last: 0};
var videoDragClickUntil = 0;

function isTopVideoPlayer(player) {
    var modal = document.getElementById('videoPreviewModal');
    return player && player.id === 'video_preview_player' && modal && modal.contains(player) &&
        $(modal).hasClass('show') && (typeof activeModalStack === 'undefined' || !activeModalStack.length ||
        activeModalStack[activeModalStack.length - 1] === modal);
}

function isVideoPicturePoint(player, e) {
    if (!isTopVideoPlayer(player) || e.target !== player || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return false;
    var rect = player.getBoundingClientRect();
    if (!(player.videoWidth > 0 && player.videoHeight > 0)) return false;
    var scale = Math.min(rect.width / player.videoWidth, rect.height / player.videoHeight);
    var width = player.videoWidth * scale, height = player.videoHeight * scale;
    var left = rect.left + (rect.width - width) / 2, top = rect.top + (rect.height - height) / 2;
    return e.clientX >= left && e.clientX <= left + width && e.clientY >= top &&
        e.clientY < Math.min(top + height, rect.bottom - 70);
}

function canSeekVideo(player) {
    return Number.isFinite(player.duration) && player.duration > 0 && Number.isFinite(player.currentTime);
}

function showVideoPointerSeek(target, before) {
    var ticks = Math.round(target * 10);
    var label = String(Math.floor(ticks / 600) % 60).padStart(2, '0') + ':' +
        ((ticks % 600) / 10).toFixed(1).padStart(4, '0');
    if (ticks >= 36000) label = String(Math.floor(ticks / 36000)).padStart(2, '0') + ':' + label;
    var delta = target - before;
    showVideoKeyFeedback(label + ' (' + (delta >= 0 ? '+' : '') + delta.toFixed(1) + '초)');
}

function resetVideoWheelInput() {
    clearTimeout(videoWheelInput.timer);
    videoWheelInput = {axis: null, amount: 0, timer: null, last: 0};
}

function resetVideoPointerInput() {
    var drag = videoPointerDrag;
    videoPointerDrag = null;
    videoDragClickUntil = 0;
    resetVideoWheelInput();
    if (drag) {
        clearTimeout(drag.timer);
        if (drag.player.hasPointerCapture(drag.id)) drag.player.releasePointerCapture(drag.id);
        clearVideoKeyFeedback();
    }
}

document.addEventListener('wheel', function(e) {
    var player = e.target;
    if (!isVideoPicturePoint(player, e) || videoPointerDrag) return;
    var axis = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? 'x' : 'y';
    var delta = axis === 'x' ? e.deltaX : e.deltaY;
    if (!Number.isFinite(delta) || delta === 0 || (axis === 'x' && !canSeekVideo(player))) return;
    if (axis === 'y' && ((delta > 0 && player.volume <= 0) ||
            (delta < 0 && player.volume >= 1 && !player.muted))) return;
    if (axis === 'x' && ((delta > 0 && player.currentTime <= 0) ||
            (delta < 0 && player.currentTime >= player.duration))) return;
    var now = Date.now();
    if (videoWheelInput.axis !== axis || now - videoWheelInput.last > 180) resetVideoWheelInput();
    videoWheelInput.axis = axis;
    videoWheelInput.last = now;
    // One detent: 100 pixels, 3 lines, or one page. Cap a burst at 3 units.
    var pixels = delta * (e.deltaMode === 1 ? 100 / 3 : e.deltaMode === 2 ? 100 : 1);
    videoWheelInput.amount = Math.max(-300, Math.min(300, videoWheelInput.amount + pixels));
    e.preventDefault();
    if (videoWheelInput.timer !== null || Math.abs(videoWheelInput.amount) < 100) return;
    videoWheelInput.timer = setTimeout(function() {
        videoWheelInput.timer = null;
        var steps = Math.trunc(videoWheelInput.amount / 100);
        videoWheelInput.amount -= steps * 100;
        if (!isTopVideoPlayer(player) || document.hidden || !steps) return;
        revealVideoControls(player);
        if (axis === 'x') {
            if (!canSeekVideo(player)) return;
            var before = player.currentTime;
            player.currentTime = Math.max(0, Math.min(player.duration, before - steps));
            showVideoPointerSeek(player.currentTime, before);
        } else {
            player.volume = Math.max(0, Math.min(1, Math.round((player.volume - steps * 0.01) * 100) / 100));
            if (steps < 0 && player.volume > 0) player.muted = false;
            showVideoKeyFeedback('볼륨 ' + Math.round(player.volume * 100) + '%' + (player.muted ? ' (음소거)' : ''));
        }
    }, 80);
}, {capture: true, passive: false});

document.addEventListener('pointerdown', function(e) {
    var player = e.target;
    if (e.pointerType !== 'mouse' || e.button !== 0 || !isVideoPicturePoint(player, e) || !canSeekVideo(player)) return;
    resetVideoPointerInput();
    videoPointerDrag = {player: player, id: e.pointerId, x: e.clientX, y: e.clientY,
        before: player.currentTime, target: player.currentTime, active: false, timer: null};
}, true);

document.addEventListener('pointermove', function(e) {
    var drag = videoPointerDrag;
    if (!drag || e.pointerId !== drag.id) return;
    if (!(e.buttons & 1) || !isTopVideoPlayer(drag.player) || !canSeekVideo(drag.player) ||
            e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) { resetVideoPointerInput(); return; }
    var dx = e.clientX - drag.x;
    if (!drag.active) {
        if (Math.abs(dx) < 8 || Math.abs(dx) <= Math.abs(e.clientY - drag.y)) return;
        drag.active = true;
        drag.player.setPointerCapture(drag.id);
        revealVideoControls(drag.player);
    }
    e.preventDefault();
    drag.target = Math.max(0, Math.min(drag.player.duration, drag.before + dx / 10));
    showVideoPointerSeek(drag.target, drag.before);
    clearTimeout(videoKeyFeedbackTimer);
    videoKeyFeedbackTimer = null;
    if (drag.timer === null) {
        drag.timer = setTimeout(function() {
            drag.timer = null;
            if (videoPointerDrag !== drag) return;
            if (!isTopVideoPlayer(drag.player) || !canSeekVideo(drag.player)) {
                resetVideoPointerInput(); return;
            }
            var target = Math.max(0, Math.min(drag.player.duration, drag.target));
            if (drag.player.currentTime !== target) drag.player.currentTime = target;
        }, 60);
    }
}, true);

document.addEventListener('pointerup', function(e) {
    var drag = videoPointerDrag;
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.active || e.button !== 0 || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey ||
            !isTopVideoPlayer(drag.player) || !canSeekVideo(drag.player)) {
        resetVideoPointerInput(); return;
    }
    var target = Math.max(0, Math.min(drag.player.duration, drag.before + (e.clientX - drag.x) / 10));
    clearTimeout(drag.timer);
    videoPointerDrag = null;
    if (drag.player.hasPointerCapture(drag.id)) drag.player.releasePointerCapture(drag.id);
    videoDragClickUntil = Date.now() + 400;
    e.preventDefault();
    if (drag.player.currentTime !== target) drag.player.currentTime = target;
    revealVideoControls(drag.player);
    showVideoPointerSeek(target, drag.before);
}, true);

document.addEventListener('click', function(e) {
    if (e.target && e.target.id === 'video_preview_player' && Date.now() < videoDragClickUntil) {
        videoDragClickUntil = 0;
        e.preventDefault();
        e.stopImmediatePropagation();
    }
}, true);

['pointercancel', 'lostpointercapture'].forEach(function(name) {
    document.addEventListener(name, function(e) {
        if (videoPointerDrag && e.pointerId === videoPointerDrag.id) resetVideoPointerInput();
    }, true);
});
document.addEventListener('loadstart', function(e) {
    if (e.target && e.target.id === 'video_preview_player') resetVideoPointerInput();
}, true);
document.addEventListener('visibilitychange', function() {
    if (document.hidden) resetVideoPointerInput();
});
window.addEventListener('blur', resetVideoPointerInput);
$(document).on('hide.bs.modal', '#videoPreviewModal', resetVideoPointerInput);

// 재생 모달 단축키: 스페이스 재생/정지, 좌우 5초 탐색, 상하 볼륨 5% 조절
// Capture before native video controls consume arrows and apply their own steps.
document.addEventListener('keydown', function(e){
    var modal = document.getElementById('videoPreviewModal');
    if (!modal || !modal.contains(e.target) || !$(modal).hasClass('show') || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (typeof activeModalStack !== 'undefined' && activeModalStack.length && activeModalStack[activeModalStack.length - 1] !== modal) return;
    var active = document.activeElement;
    if (active && (active.isContentEditable || /^(input|textarea|select)$/i.test(active.tagName))) return;
    var key = e.key || ({32: ' ', 37: 'ArrowLeft', 38: 'ArrowUp', 39: 'ArrowRight', 40: 'ArrowDown'})[e.keyCode];
    if ([' ', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].indexOf(key) === -1) return;
    var player = document.getElementById('video_preview_player');
    if (!player) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    revealVideoControls(player);
    if (key === ' ') {
        if (player.paused) {
            var playAttempt = player.play();
            if (playAttempt && typeof playAttempt.then === 'function') {
                playAttempt.then(function() {
                    if (!player.paused) showVideoKeyFeedback('재생');
                }).catch(function(err) {
                    if (err.name !== 'AbortError') showVideoKeyFeedback('재생할 수 없습니다');
                });
            } else {
                showVideoKeyFeedback('재생');
            }
        } else {
            player.pause();
            showVideoKeyFeedback('일시정지');
        }
    } else if (key === 'ArrowLeft' || key === 'ArrowRight') {
        if (Number.isFinite(player.duration) && player.duration > 0 && Number.isFinite(player.currentTime)) {
            var beforeSeek = player.currentTime;
            player.currentTime = Math.max(0, Math.min(player.duration, player.currentTime + (key === 'ArrowRight' ? 5 : -5)));
            var seekDelta = Math.round((player.currentTime - beforeSeek) * 10) / 10;
            var positionSeconds = Math.floor(player.currentTime);
            var positionLabel = String(Math.floor(positionSeconds / 60) % 60).padStart(2, '0') + ':' + String(positionSeconds % 60).padStart(2, '0');
            if (positionSeconds >= 3600) {
                positionLabel = String(Math.floor(positionSeconds / 3600)).padStart(2, '0') + ':' + positionLabel;
            }
            showVideoKeyFeedback(positionLabel + ' (' + (seekDelta >= 0 ? '+' : '') + seekDelta + ')');
        } else {
            showVideoKeyFeedback('영상 탐색 준비 중');
        }
    } else {
        player.volume = Math.max(0, Math.min(1, Math.round((player.volume + (key === 'ArrowUp' ? 0.05 : -0.05)) * 100) / 100));
        if (key === 'ArrowUp' && player.volume > 0) player.muted = false;
        showVideoKeyFeedback('볼륨 ' + Math.round(player.volume * 100) + '%' + (player.muted ? ' (음소거)' : ''));
    }
}, true);

$(document).on('hidden.bs.modal', '#videoPreviewModal', function(){
    resetVideoPointerInput();
    clearVideoKeyFeedback();
    var player = document.getElementById('video_preview_player');
    if (player) {
        isVideoAudioRestoring = true;
        player.pause();
        player.removeAttribute('src');
        player.load();
        setTimeout(function() {
            isVideoAudioRestoring = false;
        }, 100);
    }
});

$(document).off('click', '#btn_preview_prev').on('click', '#btn_preview_prev', function(e){
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    var $modal = $(this).closest('.modal');
    var images = $modal.data('preview_images') || [];
    var idx = ($modal.data('preview_idx') !== undefined) ? parseInt($modal.data('preview_idx'), 10) : 0;
    if (images.length <= 1) return;
    idx = (idx - 1 + images.length) % images.length;
    $modal.data('preview_idx', idx);
    update_modal_image_preview($modal);
});

$(document).off('click', '#btn_preview_next').on('click', '#btn_preview_next', function(e){
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    var $modal = $(this).closest('.modal');
    var images = $modal.data('preview_images') || [];
    var idx = ($modal.data('preview_idx') !== undefined) ? parseInt($modal.data('preview_idx'), 10) : 0;
    if (images.length <= 1) return;
    idx = (idx + 1) % images.length;
    $modal.data('preview_idx', idx);
    update_modal_image_preview($modal);
});

$(document).on('click', '#btn_open_actor_search_modal', function(e){
    e.preventDefault();
    var $callingModal = $(this).closest('.modal');
    $('#actorSearchModal').data('calling_modal', $callingModal);

    $('#actor_search_kw').val('');
    $('#actor_search_results_tbody').html('<tr><td colspan="4" class="text-center text-muted py-4">배우 이름이나 품번을 입력하여 검색하세요.</td></tr>');
    $('#actorSearchModal').modal('show');
});

$(document).on('shown.bs.modal', '#actorSearchModal', function() {
    $('#actor_search_kw').trigger('focus').select();
});

$(document).on('mouseenter', '.actor-search-thumb-hover', function() {
    var src = $(this).attr('data-src') || $(this).attr('src');
    if (!src) return;

    var $preview = $('#actor_hover_preview');
    if ($preview.length === 0) {
        $('body').append('<div id="actor_hover_preview"><img src="" alt="Actor Preview"></div>');
        $preview = $('#actor_hover_preview');
    }

    $preview.find('img').attr('src', src);

    var rect = this.getBoundingClientRect();
    var previewW = 180;
    var previewH = 240;
    var left = rect.right + 12;
    if (left + previewW > window.innerWidth - 12) {
        left = rect.left - previewW - 12;
    }

    var top = rect.top - 20;
    top = Math.max(12, Math.min(window.innerHeight - previewH - 12, top));

    $preview.css({
        top: top + 'px',
        left: left + 'px'
    }).stop(true, true).fadeIn(120);
});

$(document).on('mouseleave', '.actor-search-thumb-hover', function() {
    $('#actor_hover_preview').stop(true, true).fadeOut(100);
});

$(document).on('scroll', '#actorSearchModal .table-responsive', function() {
    $('#actor_hover_preview').hide();
});
$(document).on('hidden.bs.modal', '#actorSearchModal', function() {
    $('#actor_hover_preview').hide();
});

$(document).on('keydown', '#actor_search_kw', function(e){
    if (e.key === 'Enter') {
        e.preventDefault();
        $('#btn_perform_actor_search').trigger('click');
    }
});

var current_searched_actors_raw = [];
var current_search_keyword = '';

function getActorMatchScore(actor, kw) {
    var target = kw.toLowerCase().trim();
    if (!target) return 0;

    var name_org = (actor.name_org || '').toLowerCase().trim();
    var name_ko = (actor.name_ko || '').toLowerCase().trim();
    var name_en = (actor.name_en || '').toLowerCase().trim();
    var other = (actor.other_names || '').toLowerCase().trim();
    var idx = (actor.person_idx || '').toLowerCase().trim();

    var raw_target_num = target.replace(/^(pa|ps|pt)/i, '');
    var raw_idx_num = idx.replace(/^(pa|ps|pt)/i, '');

    if (idx === target || (raw_target_num && raw_target_num === raw_idx_num && raw_target_num.length >= 2)) {
        return 200;
    }
    if (name_org === target || name_ko === target) return 100;
    if (name_en === target) return 90;
    if (idx && (idx.indexOf(target) === 0 || (raw_target_num && raw_idx_num.indexOf(raw_target_num) === 0))) {
        var lenDiff = Math.abs(idx.length - target.length);
        return Math.max(110, 150 - (lenDiff * 8));
    }
    if (name_org.indexOf(target) === 0 || name_ko.indexOf(target) === 0) return 80;
    if (name_org.indexOf(target) !== -1 || name_ko.indexOf(target) !== -1) return 60;
    if (idx && idx.indexOf(target) !== -1) return 50;
    if (name_en.indexOf(target) !== -1 || other.indexOf(target) !== -1) return 40;

    return 10;
}

function render_sorted_actor_search_results() {
    if (!current_searched_actors_raw || current_searched_actors_raw.length === 0) {
        $('#actor_search_results_tbody').html('<tr><td colspan="4" class="text-center text-muted py-4">검색 결과가 없습니다.</td></tr>');
        return;
    }

    var sort_mode = $('#actor_search_sort').val() || 'match';
    var list = current_searched_actors_raw.slice();

    if (sort_mode === 'match') {
        list.sort(function(a, b){
            var scoreA = getActorMatchScore(a, current_search_keyword);
            var scoreB = getActorMatchScore(b, current_search_keyword);
            if (scoreB !== scoreA) {
                return scoreB - scoreA;
            }
            var numA = parseInt((a.person_idx || '').replace(/[^0-9]/g, ''), 10) || 0;
            var numB = parseInt((b.person_idx || '').replace(/[^0-9]/g, ''), 10) || 0;
            if (numA !== numB) {
                return numA - numB;
            }
            return (a.name_ko || a.name_org || '').localeCompare(b.name_ko || b.name_org || '');
        });
    } else if (sort_mode === 'id_asc') {
        list.sort(function(a, b){
            var numA = parseInt((a.person_idx || '').replace(/[^0-9]/g, ''), 10) || 0;
            var numB = parseInt((b.person_idx || '').replace(/[^0-9]/g, ''), 10) || 0;
            return numA - numB;
        });
    } else if (sort_mode === 'id_desc') {
        list.sort(function(a, b){
            var numA = parseInt((a.person_idx || '').replace(/[^0-9]/g, ''), 10) || 0;
            var numB = parseInt((b.person_idx || '').replace(/[^0-9]/g, ''), 10) || 0;
            return numB - numA;
        });
    } else if (sort_mode === 'name_asc') {
        list.sort(function(a, b){ return (a.name_ko || a.name_org || '').localeCompare(b.name_ko || b.name_org || ''); });
    } else if (sort_mode === 'name_desc') {
        list.sort(function(a, b){ return (b.name_ko || b.name_org || '').localeCompare(a.name_ko || a.name_org || ''); });
    }

    var str = '';
    for (var i = 0; i < list.length; i++) {
        var a = list[i];
        var img_tag = a.thumb
            ? '<img src="' + a.thumb + '" class="enlarge-person-photo shadow-sm rounded actor-search-thumb-hover" data-src="' + a.thumb + '" style="height: 60px; width: 60px; object-fit: cover; cursor: zoom-in;" title="클릭하여 크게 보기">'
            : '<div class="d-flex align-items-center justify-content-center rounded text-muted small" style="height: 60px; width: 60px; background: #2b3035;">No Img</div>';

        var code_badge = a.person_idx ? '<span class="badge badge-info mr-1">' + a.person_idx + '</span>' : '';
        var display_name = a.name_ko || a.name_org || '-';
        var sub_info = (a.name_org && a.name_org !== display_name) ? a.name_org : '';
        if (a.name_en && a.name_en !== display_name && a.name_en !== a.name_org) {
            sub_info += (sub_info ? ' (' + a.name_en + ')' : a.name_en);
        }
        if (!sub_info) sub_info = '-';
        if (a.other_names) sub_info += '<div class="text-muted small mt-1">별칭: ' + a.other_names + '</div>';

        str += '<tr>';
        str += '  <td class="text-center align-middle" style="width: 80px;">' + img_tag + '</td>';
        str += '  <td class="align-middle">' + code_badge + '<strong style="font-size: 1.05em;">' + display_name + '</strong></td>';
        str += '  <td class="align-middle text-muted">' + sub_info + '</td>';
        str += '  <td class="text-center align-middle" style="width: 90px;"><button type="button" class="btn btn-sm btn-outline-success font-weight-bold btn_select_actor" data-actor=\'' + JSON.stringify(a).replace(/'/g, "&apos;") + '\'>선택</button></td>';
        str += '</tr>';
    }
    $('#actorSearchResultsTbody, #actor_search_results_tbody').html(str);
}

$(document).on('change', '#actor_search_sort', function(){
    render_sorted_actor_search_results();
});

$(document).on('click', '#btn_perform_actor_search', function(e){
    e.preventDefault();
    var kw = $('#actor_search_kw').val().trim();
    if (!kw) { if (typeof notify === 'function') notify('검색할 배우 이름을 입력하세요.', 'warning'); return; }
    
    current_search_keyword = kw;
    var current_sub = get_current_module_sub();
    var domain = (current_sub === 'western') ? 'WESTERN' : 'JAV';
    
    var search_options = {
        include_aliases: $('#actor_search_include_aliases').is(':checked')
    };

    $('#actor_search_results_tbody').html('<tr><td colspan="4" class="text-center text-info py-4">검색 중...</td></tr>');
    
    globalSendCommand('person_search', kw, domain, JSON.stringify(search_options), function(ret){
        if (ret.ret === 'success') {
            current_searched_actors_raw = ret.data || [];
            render_sorted_actor_search_results();
        } else {
            $('#actor_search_results_tbody').html('<tr><td colspan="4" class="text-center text-danger py-4">검색 실패: ' + (ret.msg || 'unknown') + '</td></tr>');
        }
    });
});

$(document).on('click', '.btn_select_actor', function(e){
    e.preventDefault();
    var actor_data = $(this).data('actor');
    if (typeof actor_data === 'string') {
        try { actor_data = JSON.parse(actor_data); } catch (err) {}
    }
    if (!actor_data) return;

    var $callingModal = $('#actorSearchModal').data('calling_modal') || $('#dbEditModal');
    var actorsList = $callingModal.data('edit_actors') || [];

    var exists = actorsList.some(function(a){
        return (a.name_org && a.name_org === actor_data.name_org) || 
               (a.name_ko && a.name_ko === actor_data.name_ko) ||
               (a.actor_idx && a.actor_idx === actor_data.person_idx);
    });
    if (exists) { if (typeof notify === 'function') notify('이미 추가된 배우입니다.', 'warning'); return; }

    actorsList.push({
        name_org: actor_data.name_org || '',
        name_ko: actor_data.name_ko || '',
        name_en: actor_data.name_en || '',
        thumb: actor_data.thumb || '',
        actor_idx: actor_data.person_idx || '',
        gender: (actor_data.extra_info && actor_data.extra_info.gender) || actor_data.gender || '',
        role: '출연'
    });
    $callingModal.data('edit_actors', actorsList);
    render_actor_badges($callingModal);

    var added_name_log = actor_data.name_ko || actor_data.name_org || actor_data.name_en;
    if (typeof notify === 'function') notify('[' + added_name_log + '] 배우가 추가되었습니다.', 'success');
    $('#actorSearchModal').modal('hide');
});

$(document).on('click', '.btn_remove_actor', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var actorsList = $modal.data('edit_actors') || [];
    var idx = parseInt($(this).data('idx'), 10);
    if (!isNaN(idx) && idx >= 0 && idx < actorsList.length) {
        actorsList.splice(idx, 1);
        $modal.data('edit_actors', actorsList);
        render_actor_badges($modal);
    }
});

// Explicit menu actions apply only to this request and never change shared settings.
function requestImageMediaRefresh(code, category, action, callback) {
    globalSendCommand('db_refresh_image_only', String(code), category, action === 'replace' ? 'replace' : 'reuse', function(ret) {
        callback(ret || {ret: 'error', msg: '동기화 응답을 받지 못했습니다.'});
    });
}

$(document).off('click', '.btn_refresh_image_only').on('click', '.btn_refresh_image_only', function(e){
    e.preventDefault();
    var $dropdown = $(this).closest('.dropdown');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var code = $(this).attr('data-code') || $(this).data('code');
    if (!code) return;
    var cat = $(this).attr('data-category') || $(this).data('category') || '';
    var mediaAction = $(this).attr('data-media-action') === 'replace' ? 'replace' : 'reuse';
    var operationLabel = mediaAction === 'replace' ? '재처리' : '동기화';
    if (typeof notify === 'function') notify('[' + code + '] 이미지/미디어 ' + operationLabel + '를 요청합니다...', 'info');

    requestImageMediaRefresh(code, cat, mediaAction, function(ret){
        if (typeof notify === 'function') notify(ret.msg || operationLabel + ' 완료', ret.ret === 'success' ? 'success' : 'warning');
        if (ret.ret === 'success') {
            window.globalRequestSearch(null, true);
        }
    });
});

$(document).on('click', '.btn_modal_refresh_image_only', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var $dropdown = $(this).closest('.dropdown');
    var $toggleBtn = $dropdown.find('.custom-dropdown-toggle');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var code = $modal.find('#edit_code').val();
    if (!code) return;
    var row = $modal.data('row_data') || {};
    var cat = row.category || get_list_context().category || '';
    var mediaAction = $(this).attr('data-media-action') === 'replace' ? 'replace' : 'reuse';
    var operationLabel = mediaAction === 'replace' ? '재처리' : '동기화';
    if (typeof notify === 'function') notify('[' + code + '] 이미지/미디어 ' + operationLabel + '를 요청합니다...', 'info');

    setModalLoadingState($modal, true, '이미지 및 미디어 ' + operationLabel + ' 중...', $toggleBtn);

    requestImageMediaRefresh(code, cat, mediaAction, function(ret){
        if (typeof notify === 'function') notify(ret.msg || operationLabel + ' 완료', ret.ret === 'success' ? 'success' : 'warning');
        if (ret && ret.ret === 'success') {
            reloadDbEditModalData(code, cat, $modal, function(){
                setModalLoadingState($modal, false, '', $toggleBtn);
            });
        } else {
            setModalLoadingState($modal, false, '', $toggleBtn);
        }
    });
});

$(document).off('click', '.btn_refresh_missing_in_place').on('click', '.btn_refresh_missing_in_place', function(e){
    e.preventDefault();
    var $dropdown = $(this).closest('.dropdown');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var code = $(this).attr('data-code') || $(this).data('code');
    if (!code) return;
    var cat = $(this).attr('data-category') || $(this).data('category') || '';
    if (typeof notify === 'function') notify('[' + code + '] 누락 정보 갱신을 요청합니다...', 'info');

    globalSendCommand('db_refresh_missing_in_place', String(code), cat, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg || '갱신 완료', ret.ret === 'success' ? 'success' : 'warning');
        if (ret && ret.ret === 'success') {
            window.globalRequestSearch(null, true);
        }
    });
});

$(document).on('click', '.btn_modal_refresh_missing_in_place', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var $dropdown = $(this).closest('.dropdown');
    var $toggleBtn = $dropdown.find('.custom-dropdown-toggle');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var code = $modal.find('#edit_code').val();
    if (!code) return;
    var row = $modal.data('row_data') || {};
    var cat = row.category || get_list_context().category || '';
    if (typeof notify === 'function') notify('[' + code + '] 누락 정보 갱신을 요청합니다...', 'info');

    setModalLoadingState($modal, true, '누락 정보 치유 갱신 중...', $toggleBtn);

    globalSendCommand('db_refresh_missing_in_place', String(code), cat, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg || '갱신 완료', ret.ret === 'success' ? 'success' : 'warning');
        if (ret && ret.ret === 'success') {
            reloadDbEditModalData(code, cat, $modal, function(){
                setModalLoadingState($modal, false, '', $toggleBtn);
            });
        } else {
            setModalLoadingState($modal, false, '', $toggleBtn);
        }
    });
});

$(document).off('click', '.btn_refresh_in_place').on('click', '.btn_refresh_in_place', function(e){
    e.preventDefault();
    var $dropdown = $(this).closest('.dropdown');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var code = $(this).attr('data-code') || $(this).data('code');
    if (!code) return;
    var cat = $(this).attr('data-category') || $(this).data('category') || '';
    if (typeof notify === 'function') notify('[' + code + '] 현재 사이트 메타 갱신을 요청합니다...', 'info');

    globalSendCommand('db_refresh_in_place', String(code), cat, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg || '갱신 완료', ret.ret === 'success' ? 'success' : 'warning');
        if (ret.ret === 'success') {
            window.globalRequestSearch(null, true);
        }
    });
});

$(document).on('click', '.btn_modal_refresh_in_place', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var $dropdown = $(this).closest('.dropdown');
    var $toggleBtn = $dropdown.find('.custom-dropdown-toggle');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var code = $modal.find('#edit_code').val();
    if (!code) return;
    var row = $modal.data('row_data') || {};
    var cat = row.category || get_list_context().category || '';
    if (typeof notify === 'function') notify('[' + code + '] 현재 사이트 메타 갱신을 요청합니다...', 'info');

    setModalLoadingState($modal, true, '현재 사이트 메타데이터 갱신 중...', $toggleBtn);

    globalSendCommand('db_refresh_in_place', String(code), cat, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg || '갱신 완료', ret.ret === 'success' ? 'success' : 'warning');
        if (ret && ret.ret === 'success') {
            reloadDbEditModalData(code, cat, $modal, function(){
                setModalLoadingState($modal, false, '', $toggleBtn);
            });
        } else {
            setModalLoadingState($modal, false, '', $toggleBtn);
        }
    });
});

$(document).off('click', '.btn_refresh_auto_search').on('click', '.btn_refresh_auto_search', function(e){
    e.preventDefault();
    var $dropdown = $(this).closest('.dropdown');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var code = $(this).attr('data-code') || $(this).data('code');
    if (!code) return;
    var cat = $(this).attr('data-category') || $(this).data('category') || '';
    if (typeof notify === 'function') notify('[' + code + '] 전체 사이트 자동 재검색 갱신을 요청합니다...', 'info');

    globalSendCommand('db_refresh_auto_search', String(code), cat, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg || '재검색 완료', ret.ret === 'success' ? 'success' : 'warning');
        if (ret.ret === 'success') {
            window.globalRequestSearch(null, true);
        }
    });
});

$(document).on('click', '.btn_modal_refresh_auto_search', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var $dropdown = $(this).closest('.dropdown');
    var $toggleBtn = $dropdown.find('.custom-dropdown-toggle');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var code = $modal.find('#edit_code').val();
    if (!code) return;
    var row = $modal.data('row_data') || {};
    var cat = row.category || get_list_context().category || '';
    if (typeof notify === 'function') notify('[' + code + '] 전체 사이트 자동 재검색 갱신을 요청합니다...', 'info');

    setModalLoadingState($modal, true, '전체 우선순위 자동 재검색 갱신 중...', $toggleBtn);

    globalSendCommand('db_refresh_auto_search', String(code), cat, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg || '재검색 완료', ret.ret === 'success' ? 'success' : 'warning');
        if (ret && ret.ret === 'success') {
            reloadDbEditModalData(code, cat, $modal, function(){
                setModalLoadingState($modal, false, '', $toggleBtn);
            });
        } else {
            setModalLoadingState($modal, false, '', $toggleBtn);
        }
    });
});

$(document).on('change', '#check_all_meta', function(){
    var isChecked = $(this).is(':checked');
    $('.meta-item-chk').prop('checked', isChecked);
});

$(document).on('change', '.meta-item-chk', function(){
    var totalCount = $('.meta-item-chk').length;
    var checkedCount = $('.meta-item-chk:checked').length;
    $('#check_all_meta').prop('checked', totalCount > 0 && totalCount === checkedCount);
});

$(document).off('click', '#btn_delete_selected').on('click', '#btn_delete_selected', function(e){
    e.preventDefault();
    var targets = [];
    $('.meta-item-chk:checked').each(function(){
        var codeVal = $(this).val();
        if (codeVal) targets.push({kind:'item', code:codeVal, category:$(this).attr('data-category') || get_list_context().category});
    });

    if (targets.length === 0) {
        if (typeof notify === 'function') notify('삭제할 항목을 먼저 선택하세요.', 'warning');
        return;
    }
    openMetadataDeleteModal(targets);
});

$(document).off('click', '.btn_delete_db').on('click', '.btn_delete_db', function(e){
    e.preventDefault();
    var code = $(this).data('code');
    if (!code) return;
    var cat = $(this).attr('data-category') || $(this).data('category') || '';
    openMetadataDeleteModal([{kind:'item', code:code, category:cat || get_list_context().category}]);
});

function openMetadataDeleteModal(targets) {
    var $existing = $('#metadataDeleteModal');
    if ($existing.length) return;
    var $modal = $('<div class="modal fade metadata-delete-modal" id="metadataDeleteModal" tabindex="-1" role="dialog" aria-labelledby="metadataDeleteTitle">' +
        '<div class="modal-dialog metadata-delete-dialog" role="document"><div class="modal-content metadata-delete-content">' +
        '<div class="modal-header"><h5 class="modal-title" id="metadataDeleteTitle">데이터 삭제</h5>' +
        '<button type="button" class="close" data-dismiss="modal" aria-label="닫기"><span aria-hidden="true">&times;</span></button></div>' +
        '<div class="modal-body"><p class="delete-summary">관련 미디어 목록을 확인하고 있습니다.</p><div class="delete-targets small mb-2"></div>' +
        '<p class="small text-muted">DB만 삭제하면 파일은 유지됩니다. 전체 삭제에는 사용자 이미지와 프리뷰도 포함됩니다. 원본 영상과 외부 사이트·GDS 이미지는 삭제하지 않습니다.</p>' +
        '<div class="delete-error text-danger mb-2" role="alert"></div>' +
        '<div class="delete-files"></div></div>' +
        '<div class="modal-footer"><button type="button" class="btn btn-secondary" data-dismiss="modal">취소</button>' +
        '<button type="button" class="btn btn-outline-danger delete-db" disabled>DB만 삭제</button>' +
        '<button type="button" class="btn btn-danger delete-all" disabled>전체 삭제</button></div></div></div></div>').appendTo('body');
    var payload = JSON.stringify(targets), plan = null, busy = false;
    $modal.on('hide.bs.modal', function(e){ if (busy) e.preventDefault(); });
    $modal.on('hidden.bs.modal', function(){
        $modal.remove();
        if ($('.modal.show').length) $('body').addClass('modal-open');
    });
    function loadPlan() {
        plan = null;
        $modal.find('.delete-db,.delete-all').prop('disabled', true);
        globalSendCommand('db_delete_preview', payload, null, null, function(ret){
            if (!$modal.parent().length) return;
            if (!ret || ret.ret !== 'success') {
                $modal.find('.delete-summary').text('삭제 목록을 불러오지 못했습니다.');
                $modal.find('.delete-error').text((ret && ret.msg) || 'DB 조회 실패');
                return;
            }
            plan = ret.data;
            $modal.find('.delete-summary').text('DB ' + plan.records.length + '건 · 관련 미디어 ' + plan.files.length + '개');
            var $targets = $modal.find('.delete-targets').empty();
            plan.records.forEach(function(record){ $('<div></div>').text(record.title + ' [' + record.code + ']').appendTo($targets); });
            var $files = $modal.find('.delete-files').empty();
            if (!plan.files.length) $('<p class="text-muted">개인 저장소에 기록된 미디어가 없습니다.</p>').appendTo($files);
            plan.files.forEach(function(file){
                var mediaLabels = {poster:'포스터', landscape:'가로 커버', fanart:'팬아트', trailer:'트레일러', preview:'프리뷰', person:'인물 이미지'};
                var label = (file.storage === 'gdrive' ? 'GDrive' : '로컬') + ' · ' + (mediaLabels[file.media_type] || file.media_type) +
                    (file.is_user ? ' · 사용자 미디어' : '') + ' · ' + file.owners.join(', ');
                var $entry = $('<div class="border rounded p-2 mb-2"></div>').appendTo($files);
                $('<div class="small font-weight-bold"></div>').text(label).appendTo($entry);
                $('<div class="small"></div>').text(file.path || file.relative_path || '경로 정보 없음').appendTo($entry);
                if (file.file_id) $('<div class="small text-muted"></div>').text('파일 ID: ' + file.file_id).appendTo($entry);
                if (file.retained || file.blocked) $('<div class="small"></div>').addClass(file.retained ? 'text-info' : 'text-danger')
                    .text(file.retained ? '유지: ' + file.retained : '삭제 불가: ' + file.blocked).appendTo($entry);
            });
            $modal.find('.delete-db').prop('disabled', false);
            $modal.find('.delete-all').prop('disabled', plan.files.some(function(f){ return f.blocked && !f.retained; }));
        });
    }
    $modal.find('.delete-db,.delete-all').on('click', function(){
        if (!plan || busy) return;
        var mode = $(this).hasClass('delete-all') ? 'all' : 'db';
        busy = true;
        $modal.find('button').prop('disabled', true);
        $modal.find('.delete-summary').text(mode === 'all' ? '관련 미디어와 DB를 삭제하고 있습니다.' : 'DB만 삭제하고 있습니다.');
        globalSendCommand('db_delete_confirm', payload, mode, plan.token, function(ret){
            busy = false;
            $modal.find('button').prop('disabled', false);
            if (ret && ret.ret === 'success') {
                $modal.modal('hide');
                notify(ret.msg, 'success');
                $('#check_all_meta').prop('checked', false);
                window.globalRequestSearch(null, true);
            } else {
                var errors = (ret && ret.files || []).map(function(f){return (f.path || f.file_id || '') + ': ' + (f.error || f.blocked || '');});
                $modal.find('.delete-error').text(((ret && ret.msg) || '삭제 처리 실패') + (errors.length ? '\n' + errors.join('\n') : ''));
                if (ret && ret.uncertain) {
                    $modal.find('.delete-db,.delete-all').prop('disabled', true);
                    $modal.find('.delete-summary').text('삭제 결과 확인이 필요합니다.');
                } else {
                    loadPlan();
                }
            }
        });
    });
    $modal.modal({backdrop:'static', keyboard:false});
    loadPlan();
}

function openCropModal(opts) {
    current_crop_target_type = opts.target || 'meta';
    custom_upload_payload = null;

    current_loaded_temp_path = null;
    $('#crop_url_bar').hide();
    $('#input_crop_url').val('');
    $('#btn_direct_save_user').hide();

    var defaultRatio = 1 / 1.4225;

    if (current_crop_target_type === 'person') {
        defaultRatio = 3 / 4;
        $('#crop_meta_sources').hide();
        $('#lbl_upload_pl').hide();
        $('#lbl_upload_p').hide();
        $('#lbl_upload_person').show();

        $('#crop_person_id').val(opts.id || opts.code || '');
        $('#crop_person_domain').val(opts.domain || 'JAV');
        $('#crop_target_code').val(opts.code || opts.id || '');

        var previewFileName = opts.filename_preview || ((opts.title || 'actor') + '_user.jpg');
        $('#crop_modal_title').text('[' + (opts.title || '인물') + '] 프로필 사진 크롭 & 등록');
        $('#crop_footer_notice').html('※ 저장 시 <code>' + previewFileName + '</code> 파일로 생성되어 대표 프로필로 적용됩니다.');
        $('#btn_save_crop_result').text('프로필 사진으로 저장 (_user)');

        $('#imageCropModal .btn-group button').removeClass('btn-info font-weight-bold').addClass('btn-outline-light');
        $('#btn_crop_ratio_portrait').removeClass('btn-outline-light').addClass('btn-info font-weight-bold');
    } else {
        defaultRatio = 1 / 1.4225;
        $('#crop_meta_sources').show();
        $('#lbl_upload_pl').show();
        $('#lbl_upload_p').show();
        $('#lbl_upload_person').hide();

        $('#crop_target_code').val(opts.code || '');
        $('#crop_target_category').val(opts.category || '');
        $('#crop_has_user_poster').val(opts.has_user ? 'true' : 'false');

        $('#crop_modal_title').text('[' + (opts.code || '') + '] ' + (opts.title || '') + ' - 포스터 크롭');
        $('#crop_footer_notice').html('※ 저장 시 <code>_p_user.jpg</code> 파일로 생성되며 기존 시스템 <code>_p.jpg</code>는 정리됩니다.');
        $('#btn_save_crop_result').text('포스터로 저장 (_p_user)');

        $('#imageCropModal .btn-group button').removeClass('btn-info font-weight-bold').addClass('btn-outline-light');
        $('#btn_crop_ratio_lock').removeClass('btn-outline-light').addClass('btn-info font-weight-bold');
    }

    modal_pl_url = opts.pl_url || '';
    modal_p_url = opts.p_url || '';
    active_source_type = modal_pl_url ? 'pl' : (modal_p_url ? 'p' : 'pl');

    $('#text_direct_save_user').text(active_source_type === 'p' ? '💾 _p_user 저장' : '💾 _pl_user 저장');
    $('#btn_source_pl').removeClass('btn-outline-light').addClass('btn-primary font-weight-bold');
    $('#btn_source_p').removeClass('btn-primary font-weight-bold').addClass('btn-outline-light');

    var initialUrl = opts.initial_url || '';

    var executeCropperInit = function() {
        var imageElement = document.getElementById('cropper_image');
        if (cropperInstance) {
            try { cropperInstance.destroy(); } catch(e){}
            cropperInstance = null;
        }

        if (!initialUrl) {
            imageElement.src = '';
            $('#crop_url_bar').slideDown(120);
            if (typeof notify === 'function') {
                notify('불러올 사진이 없습니다. 상단의 URL 입력 또는 사진 파일 업로드를 이용하세요.', 'info');
            }
            return;
        }

        var startCropperInstance = function() {
            if (cropperInstance) {
                try { cropperInstance.destroy(); } catch(e){}
                cropperInstance = null;
            }
            cropperInstance = new Cropper(imageElement, {
                aspectRatio: defaultRatio,
                viewMode: 1,
                dragMode: 'move',
                autoCropArea: 1.0,
                responsive: true,
                restore: false,
                checkCrossOrigin: false,
                zoomable: true,
                rotatable: true,
                scalable: true,
                wheelZoomRatio: 0.08,
                ready: function () {
                    try {
                        this.cropper.setCropBoxData(this.cropper.getCanvasData());
                    } catch(e){}
                }
            });
        };

        $(imageElement).off('load.crop_load error.crop_load')
            .one('load.crop_load', function() {
                startCropperInstance();
            })
            .one('error.crop_load', function() {
                if (cropperInstance) {
                    try { cropperInstance.destroy(); } catch(e){}
                    cropperInstance = null;
                }
                imageElement.src = '';
                $('#crop_url_bar').slideDown(120);
                if (typeof notify === 'function') {
                    notify('이미지를 불러오지 못했습니다. 상단의 URL 입력 또는 사진 업로드를 이용하세요.', 'warning');
                }
            });

        imageElement.src = initialUrl;
        if (imageElement.complete && imageElement.naturalWidth > 0) {
            startCropperInstance();
        }
    };

    $('#imageCropModal').modal('show');
    $('#imageCropModal').one('shown.bs.modal', function () {
        if (typeof Cropper === 'undefined') {
            if ($('link[href*="cropper"]').length === 0) {
                $('head').append('<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.5.13/cropper.min.css" />');
            }
            $.getScript('https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.5.13/cropper.min.js', function() {
                executeCropperInit();
            });
        } else {
            executeCropperInit();
        }
    });
}

$(document).off('click', '.btn_crop_modal').on('click', '.btn_crop_modal', function(e){
    e.preventDefault();
    var $dropdown = $(this).closest('.dropdown');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var idx = parseInt($(this).attr('data-idx') || $(this).data('idx'), 10);
    var row = (current_data && !isNaN(idx) && idx >= 0 && idx < current_data.length) ? current_data[idx] : null;
    if (!row) {
        if (typeof notify === 'function') notify('편집 대상 데이터를 찾을 수 없습니다.', 'warning');
        return;
    }

    var jd = row.json_data;
    if (typeof jd === 'string') { try { jd = JSON.parse(jd); } catch (err) { jd = {}; } }
    if (!jd || typeof jd !== 'object') jd = {};

    var raw_pl = '';
    if (jd.thumb && Array.isArray(jd.thumb)) {
        for (var t = 0; t < jd.thumb.length; t++) {
            if (jd.thumb[t] && jd.thumb[t].aspect === 'landscape') { raw_pl = jd.thumb[t].value; break; }
        }
    }
    if (!raw_pl && jd.fanart && jd.fanart.length > 0) raw_pl = jd.fanart[0];
    if (!raw_pl && jd.original && jd.original.thumb) raw_pl = jd.original.thumb.landscape || '';

    var raw_p = row.poster_url || (jd.original && jd.original.thumb ? jd.original.thumb.poster : '') || '';

    var plUrl = getSameOriginProxyUrl(raw_pl, row.site);
    var pUrl = getSameOriginProxyUrl(raw_p, row.site);

    openCropModal({
        target: 'meta',
        code: row.code,
        category: row.category || get_list_context().category,
        title: row.title || '',
        has_user: (row.poster_url && row.poster_url.indexOf('_p_user') !== -1),
        pl_url: plUrl,
        p_url: pUrl,
        initial_url: plUrl || pUrl
    });
});

$(document).on('click', '.btn_modal_crop', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var $dropdown = $(this).closest('.dropdown');
    $dropdown.removeClass('show').find('.dropdown-menu').removeClass('show');

    var row = $modal.data('row_data') || {};
    var jd = row.json_data;
    if (typeof jd === 'string') { try { jd = JSON.parse(jd); } catch (err) { jd = {}; } }
    if (!jd || typeof jd !== 'object') jd = {};

    var code = $modal.find('#edit_code').val() || row.code || '';
    if (!code) return;

    var raw_pl = $modal.find('#edit_landscape_url_final').val() || $modal.find('#edit_landscape_url').val() || '';
    if (!raw_pl && jd.thumb && Array.isArray(jd.thumb)) {
        for (var t = 0; t < jd.thumb.length; t++) {
            if (jd.thumb[t] && jd.thumb[t].aspect === 'landscape') { raw_pl = jd.thumb[t].value; break; }
        }
    }
    if (!raw_pl && jd.fanart && jd.fanart.length > 0) raw_pl = jd.fanart[0];
    if (!raw_pl && jd.original && jd.original.thumb) raw_pl = jd.original.thumb.landscape || '';

    var raw_p = $modal.find('#edit_poster_url_final').val() || row.poster_url || (jd.original && jd.original.thumb ? jd.original.thumb.poster : '') || '';

    var currentSite = row.site || $modal.find('#edit_site').val() || '';
    var plUrl = getSameOriginProxyUrl(raw_pl, currentSite);
    var pUrl = getSameOriginProxyUrl(raw_p, currentSite);

    openCropModal({
        target: 'meta',
        code: code,
        category: row.category || get_list_context().category,
        title: $modal.find('#edit_title').val() || '',
        has_user: (raw_p && raw_p.indexOf('_p_user') !== -1),
        pl_url: plUrl,
        p_url: pUrl,
        initial_url: plUrl || pUrl
    });
});

$(document).on('click', '#btn_open_person_crop_modal, .btn_open_person_crop_modal', function(e){
    e.preventDefault();
    var $pModal = $(this).closest('.modal');
    var pId = $pModal.find('#p_edit_id').val() || '';
    var pIdx = $pModal.find('#p_edit_idx').val() || '';
    var pDomain = ($pModal.find('#p_edit_domain').val() || 'JAV').toUpperCase();
    var pNameKo = $pModal.find('#p_edit_name_ko').val().trim();
    var pNameOrg = $pModal.find('#p_edit_name_org').val().trim();
    var pNameEn = $pModal.find('#p_edit_name_en').val().trim();
    var displayName = pNameKo || pNameOrg || pNameEn || '인물';
    var cleanKo = pNameKo.replace(/\s+/g, '_');
    var cleanOrg = pNameOrg.replace(/\s+/g, '_');
    var cleanIdx = pIdx.replace(/\s+/g, '_');
    var filenamePreview = '';

    if (pDomain === 'WESTERN') {
        var baseName = pNameEn || pNameOrg || pNameKo || 'Actor';
        var cleanBase = baseName.replace(/\s+/g, '_');
        filenamePreview = cleanBase + (cleanIdx ? '_' + cleanIdx : '') + '_user.jpg';
    } else {
        var namePart = cleanKo || cleanOrg || '인물';
        if (cleanOrg && cleanOrg !== cleanKo) {
            namePart += '_(' + cleanOrg + ')';
        }
        filenamePreview = namePart + (cleanIdx ? '_' + cleanIdx : '') + '_user.jpg';
    }

    var curThumb = $pModal.find('#p_modal_preview_img').attr('data-src') || 
                   $pModal.find('#p_modal_preview_img').attr('src') || '';

    if (!curThumb || curThumb.indexOf('data:image/svg') !== -1 || curThumb.indexOf('No Photo') !== -1) {
        curThumb = $pModal.find('#p_edit_thumb').val() || '';
    }

    if (!curThumb || curThumb.indexOf('data:image/svg') !== -1) {
        var pGallery = $pModal.data('p_preview_images') || [];
        for (var g = 0; g < pGallery.length; g++) {
            if (pGallery[g] && pGallery[g].url && pGallery[g].url.startsWith('http')) {
                curThumb = pGallery[g].url;
                break;
            }
        }
    }

    if (curThumb.indexOf('data:image/svg') !== -1) {
        curThumb = '';
    }

    var initialUrl = getSameOriginProxyUrl(curThumb);

    openCropModal({
        target: 'person',
        id: pId || pIdx,
        code: pIdx || pId,
        domain: pDomain,
        title: displayName,
        filename_preview: filenamePreview,
        initial_url: initialUrl
    });
});

$(document).on('click', '#btn_view_person_json', function(e){
    e.preventDefault();
    var $pModal = $(this).closest('.modal');
    var pData = $pModal.data('person_data') || {};

    var displayName = pData.name_ko || pData.name_org || pData.name_en || '인물';
    $('#db_json_modal_title').text('[' + displayName + '] 인물 데이터 DB JSON 원본');
    $('#db_json_modal_textarea').val(JSON.stringify(pData, null, 2));
    $('#dbJsonViewModal').modal('show');
});

$('#imageCropModal').on('hidden.bs.modal', function () {
    if (cropperInstance) {
        cropperInstance.destroy();
        cropperInstance = null;
    }
    var img = document.getElementById('cropper_image');
    if (img) img.src = '';
    $('#input_upload_pl').val('');
    $('#input_upload_p').val('');
    $('#input_upload_person').val('');
    $('#input_crop_url').val('');
    $('#crop_url_bar').hide();
    custom_upload_payload = null;
});

$(document).on('click', '#btn_crop_ratio_lock', function(){
    if (cropperInstance) cropperInstance.setAspectRatio(1 / 1.4225);
    $('#imageCropModal .btn-group button').removeClass('btn-info font-weight-bold').addClass('btn-outline-light');
    $(this).removeClass('btn-outline-light').addClass('btn-info font-weight-bold');
});

$(document).on('click', '#btn_crop_ratio_portrait', function(){
    if (cropperInstance) cropperInstance.setAspectRatio(3 / 4);
    $('#imageCropModal .btn-group button').removeClass('btn-info font-weight-bold').addClass('btn-outline-light');
    $(this).removeClass('btn-outline-light').addClass('btn-info font-weight-bold');
});

$(document).on('click', '#btn_crop_ratio_square', function(){
    if (cropperInstance) cropperInstance.setAspectRatio(1 / 1);
    $('#imageCropModal .btn-group button').removeClass('btn-info font-weight-bold').addClass('btn-outline-light');
    $(this).removeClass('btn-outline-light').addClass('btn-info font-weight-bold');
});

$(document).on('click', '#btn_crop_ratio_free', function(){
    if (cropperInstance) cropperInstance.setAspectRatio(NaN);
    $('#imageCropModal .btn-group button').removeClass('btn-info font-weight-bold').addClass('btn-outline-light');
    $(this).removeClass('btn-outline-light').addClass('btn-info font-weight-bold');
});

$(document).on('click', '#btn_crop_rotate_left', function(){
    if (cropperInstance) cropperInstance.rotate(-90);
});

$(document).on('click', '#btn_crop_rotate_right', function(){
    if (cropperInstance) cropperInstance.rotate(90);
});

$(document).on('click', '#btn_crop_reset', function(){
    if (cropperInstance) {
        cropperInstance.reset();
    }
});

$(document).on('click', '#btn_source_pl', function(){
    active_source_type = 'pl';
    $(this).removeClass('btn-outline-light').addClass('btn-primary font-weight-bold');
    $('#btn_source_p').removeClass('btn-primary font-weight-bold').addClass('btn-outline-light');
    $('#text_direct_save_user').text('💾 _pl_user 저장');
    if (modal_pl_url && cropperInstance) cropperInstance.replace(modal_pl_url);
});

$(document).on('click', '#btn_source_p', function(){
    active_source_type = 'p';
    $(this).removeClass('btn-outline-light').addClass('btn-primary font-weight-bold');
    $('#btn_source_pl').removeClass('btn-primary font-weight-bold').addClass('btn-outline-light');
    $('#text_direct_save_user').text('💾 _p_user 저장');
    if (modal_p_url && cropperInstance) cropperInstance.replace(modal_p_url);
});

$(document).on('click', '#btn_toggle_crop_url', function(e){
    e.preventDefault();
    $('#crop_url_bar').slideToggle(120, function(){
        if ($(this).is(':visible')) {
            $('#input_crop_url').trigger('focus').select();
        }
    });
});

$(document).on('click', '#btn_cancel_crop_url', function(e){
    e.preventDefault();
    $('#crop_url_bar').slideUp(120);
});

function applyDirectCropUrl() {
    var rawUrl = $('#input_crop_url').val().trim();
    if (!rawUrl) {
        if (typeof notify === 'function') notify('불러올 이미지 URL을 입력하세요.', 'warning');
        return;
    }

    var currentSite = get_list_context().type === 'person' ? '' : ($('#edit_site').val() || '');
    var proxyUrl = getSameOriginProxyUrl(rawUrl, currentSite);
    custom_upload_payload = JSON.stringify({ type: active_source_type, crop_url: rawUrl });

    var imageElement = document.getElementById('cropper_image');
    var targetRatio = (current_crop_target_type === 'person') ? (3 / 4) : (1 / 1.4225);

    if (cropperInstance) {
        cropperInstance.replace(proxyUrl);
    } else {
        imageElement.src = proxyUrl;
        cropperInstance = new Cropper(imageElement, {
            aspectRatio: targetRatio,
            viewMode: 1,
            dragMode: 'move',
            autoCropArea: 1.0,
            responsive: true,
            restore: false,
            checkCrossOrigin: false,
            zoomable: true,
            rotatable: true,
            scalable: true,
            wheelZoomRatio: 0.08
        });
    }

    $('#crop_url_bar').slideUp(120);
    if (current_crop_target_type === 'meta') {
        $('#btn_direct_save_user').show();
        var labelText = (active_source_type === 'p') ? '💾 _p_user 저장' : '💾 _pl_user 저장';
        $('#text_direct_save_user').text(labelText);
    }

    if (typeof notify === 'function') notify('URL 이미지를 성공적으로 불러왔습니다.', 'info');
}

$(document).on('click', '#btn_direct_save_user', function(e){
    e.preventDefault();
    var code = $('#crop_target_code').val();
    var targetType = active_source_type === 'p' ? 'p' : 'pl';
    var targetLabel = targetType === 'p' ? '_p_user' : '_pl_user';

    var sourceVal = $('#input_crop_url').val().trim();
    var b64Data = null;

    if (custom_upload_payload) {
        try {
            var parsedPayload = JSON.parse(custom_upload_payload);
            if (parsedPayload.data) {
                b64Data = parsedPayload.data;
            } else if (parsedPayload.crop_url) {
                sourceVal = parsedPayload.crop_url;
            }
        } catch (e_payload) {}
    }

    if (!code || (!sourceVal && !b64Data)) {
        if (typeof notify === 'function') notify('저장할 소스 이미지 정보가 없습니다.', 'warning');
        return;
    }

    if (!confirm("현재 로드된 원본 이미지를 크롭 없이 [" + targetLabel + "] 파일로 직접 저장하시겠습니까?")) {
        return;
    }

    var $btn = $(this);
    var origHtml = $btn.html();
    $btn.prop('disabled', true).html('<span>저장 중...</span>');

    var context = get_list_context();
    var targetCategory = $('#crop_target_category').val() || context.category;
    var postData = {
        code: code,
        type: targetType,
        url: sourceVal,
        data: b64Data,
        category: targetCategory
    };

    globalSendCommand('crop_direct_save', code, sourceVal, targetType, function(ret){
        $btn.prop('disabled', false).html(origHtml);
        if (ret && ret.ret === 'success') {
            var newUrl = ret.new_url || (ret.data && ret.data.new_url);
            var cacheBustedUrl = newUrl + (newUrl.indexOf('?') === -1 ? '?' : '&') + '_t=' + Date.now();

            if (typeof notify === 'function') notify(targetLabel + ' 이미지가 성공적으로 저장되었습니다.', 'success');

            if (targetType === 'pl') {
                modal_pl_url = cacheBustedUrl;
                active_source_type = 'pl';
                $('#btn_source_pl').removeClass('btn-outline-light').addClass('btn-primary font-weight-bold');
                $('#btn_source_p').removeClass('btn-primary font-weight-bold').addClass('btn-outline-light');
                $('#text_direct_save_user').text('💾 _pl_user 저장');
            } else {
                modal_p_url = cacheBustedUrl;
                active_source_type = 'p';
                $('#btn_source_p').removeClass('btn-outline-light').addClass('btn-primary font-weight-bold');
                $('#btn_source_pl').removeClass('btn-primary font-weight-bold').addClass('btn-outline-light');
                $('#text_direct_save_user').text('💾 _p_user 저장');
            }

            if (cropperInstance) {
                cropperInstance.replace(cacheBustedUrl);
            } else {
                var imageElement = document.getElementById('cropper_image');
                imageElement.src = cacheBustedUrl;
                var targetRatio = (current_crop_target_type === 'person') ? (3 / 4) : (1 / 1.4225);
                cropperInstance = new Cropper(imageElement, {
                    aspectRatio: targetRatio,
                    viewMode: 1,
                    dragMode: 'move',
                    autoCropArea: 1.0,
                    responsive: true,
                    restore: false,
                    checkCrossOrigin: false,
                    zoomable: true,
                    rotatable: true,
                    scalable: true,
                    wheelZoomRatio: 0.08
                });
            }

            $('#crop_url_bar').slideUp(120);
            $('#input_crop_url').val('');
            custom_upload_payload = null;

            var $dbModal = $('#dbEditModal');
            if ($dbModal.hasClass('show') && ($dbModal.find('#edit_code').val() === code)) {
                reloadDbEditModalData(code, null, $dbModal);
            }
            window.globalRequestSearch(null, true);

        } else {
            var errMsg = (ret && ret.msg) ? ret.msg : '저장 실패';
            if (typeof notify === 'function') notify(errMsg, 'warning');
        }
    }, postData);
});

$(document).on('click', '#btn_apply_crop_url', function(e){
    e.preventDefault();
    applyDirectCropUrl();
});

$(document).on('keydown', '#input_crop_url', function(e){
    if (e.key === 'Enter') {
        e.preventDefault();
        applyDirectCropUrl();
    } else if (e.key === 'Escape') {
        e.preventDefault();
        $('#crop_url_bar').slideUp(120);
    }
});

$(document).on('change', '#input_upload_pl', function(e){
    var files = e.target.files;
    if (files && files.length > 0) {
        var reader = new FileReader();
        reader.onload = function(evt){
            custom_upload_payload = JSON.stringify({ type: 'pl', data: evt.target.result });
            var imageElement = document.getElementById('cropper_image');
            active_source_type = 'pl';
            $('#btn_source_pl').removeClass('btn-outline-light').addClass('btn-primary font-weight-bold');
            $('#btn_source_p').removeClass('btn-primary font-weight-bold').addClass('btn-outline-light');

            if (cropperInstance) {
                cropperInstance.replace(evt.target.result);
            } else {
                imageElement.src = evt.target.result;
                cropperInstance = new Cropper(imageElement, {
                    aspectRatio: 1 / 1.4225,
                    viewMode: 1,
                    dragMode: 'move',
                    autoCropArea: 1.0,
                    responsive: true,
                    restore: false,
                    checkCrossOrigin: false,
                    zoomable: true,
                    rotatable: true,
                    scalable: true,
                    wheelZoomRatio: 0.08
                });
            }

            $('#btn_direct_save_user').show();
            $('#text_direct_save_user').text('💾 _pl_user 저장');
            $('#crop_url_bar').slideUp(120);
        };
        reader.readAsDataURL(files[0]);
    }
});

$(document).on('change', '#input_upload_p', function(e){
    var files = e.target.files;
    if (files && files.length > 0) {
        var reader = new FileReader();
        reader.onload = function(evt){
            custom_upload_payload = JSON.stringify({ type: 'p', data: evt.target.result });
            var imageElement = document.getElementById('cropper_image');
            active_source_type = 'p';
            $('#btn_source_p').removeClass('btn-outline-light').addClass('btn-primary font-weight-bold');
            $('#btn_source_pl').removeClass('btn-primary font-weight-bold').addClass('btn-outline-light');

            if (cropperInstance) {
                cropperInstance.replace(evt.target.result);
            } else {
                imageElement.src = evt.target.result;
                cropperInstance = new Cropper(imageElement, {
                    aspectRatio: 1 / 1.4225,
                    viewMode: 1,
                    dragMode: 'move',
                    autoCropArea: 1.0,
                    responsive: true,
                    restore: false,
                    checkCrossOrigin: false,
                    zoomable: true,
                    rotatable: true,
                    scalable: true,
                    wheelZoomRatio: 0.08
                });
            }

            $('#btn_direct_save_user').show();
            $('#text_direct_save_user').text('💾 _p_user 저장');
            $('#crop_url_bar').slideUp(120);
        };
        reader.readAsDataURL(files[0]);
    }
});

$(document).on('change', '#input_upload_person', function(e){
    var files = e.target.files;
    if (files && files.length > 0) {
        var reader = new FileReader();
        reader.onload = function(evt){
            custom_upload_payload = JSON.stringify({ type: 'person', data: evt.target.result });
            var imageElement = document.getElementById('cropper_image');
            if (cropperInstance) {
                cropperInstance.replace(evt.target.result);
            } else {
                imageElement.src = evt.target.result;
                cropperInstance = new Cropper(imageElement, {
                    aspectRatio: 3 / 4,
                    viewMode: 1,
                    dragMode: 'move',
                    autoCropArea: 1.0,
                    responsive: true,
                    restore: false,
                    checkCrossOrigin: false,
                    zoomable: true,
                    rotatable: true,
                    scalable: true,
                    wheelZoomRatio: 0.08
                });
            }
        };
        reader.readAsDataURL(files[0]);
    }
});

$(document).on('click', '#btn_save_crop_result', function(e){
    e.preventDefault();
    if (!cropperInstance) return;

    var btn = $(this);
    var origText = btn.text();
    var cropData = cropperInstance.getData(true);
    var cropJson = JSON.stringify(cropData);

    if (current_crop_target_type === 'person') {
        var personId = $('#crop_person_id').val();
        var personDomain = $('#crop_person_domain').val() || 'JAV';
        var directUrl = $('#input_crop_url').val().trim();

        btn.prop('disabled', true).text('저장 중...');

        var uploadPayload = '';
        try {
            var croppedCanvas = cropperInstance.getCroppedCanvas();
            if (croppedCanvas) {
                uploadPayload = JSON.stringify({ type: 'canvas', data: croppedCanvas.toDataURL('image/jpeg', 0.95) });
            }
        } catch (e_canvas) {
            uploadPayload = custom_upload_payload || '';
        }

        globalSendCommand('person_crop_save', personId, cropJson, uploadPayload, function(ret){
            btn.prop('disabled', false).text(origText);
            if (ret && ret.ret === 'success') {
                if (typeof notify === 'function') notify(ret.msg || '프로필 사진 저장 완료', 'success');
                $('#imageCropModal').modal('hide');
                custom_upload_payload = null;

                var $pModal = $('#personEditModal');
                if ($pModal.hasClass('show')) {
                    if (ret.person) {
                        renderPersonModalContent(ret.person, $pModal);
                    } else if (ret.new_url) {
                        var bustedUrl = ret.new_url + (ret.new_url.indexOf('?') === -1 ? '?' : '&') + '_t=' + Date.now();
                        $pModal.find('#p_edit_thumb').val(ret.new_url);
                        $pModal.find('#p_modal_preview_img').attr('src', bustedUrl).attr('data-src', bustedUrl).show();
                        $pModal.find('#p_modal_no_img').hide();
                    }
                }
                window.globalRequestSearch(null, true);
            } else {
                var errMsg = (ret && ret.msg) ? ret.msg : '저장에 실패했습니다.';
                if (typeof notify === 'function') notify(errMsg, 'warning');
            }
        }, { image_url: directUrl, domain: personDomain });
        return;
    }

    var code = $('#crop_target_code').val();
    var targetCategory = $('#crop_target_category').val() || get_list_context().category;
    var has_user = $('#crop_has_user_poster').val() === 'true';
    if (has_user && !confirm("⚠️ 이미 수동 설정된 유저 포스터(_p_user)가 존재합니다.\n새로운 이미지로 덮어쓰시겠습니까?")) return;

    btn.prop('disabled', true).text('저장 중...');
    cropData.source_type = active_source_type;
    cropJson = JSON.stringify(cropData);

    var metaPayload = {
        type: active_source_type,
        crop_url: $('#input_crop_url').val().trim(),
        category: targetCategory
    };

    if (custom_upload_payload) {
        try {
            var parsedCustom = JSON.parse(custom_upload_payload);
            if (parsedCustom.data) {
                if (active_source_type === 'p') metaPayload.p_base64 = parsedCustom.data;
                else metaPayload.pl_base64 = parsedCustom.data;
            }
            if (parsedCustom.crop_url) {
                metaPayload.crop_url = parsedCustom.crop_url;
            }
        } catch (e_p) {}
    }

    var uploadPayloadStr = JSON.stringify(metaPayload);

    globalSendCommand('crop_save', code, cropJson, uploadPayloadStr, function(ret){
        btn.prop('disabled', false).text(origText);
        if (ret && ret.ret === 'success') {
            if (typeof notify === 'function') notify(ret.msg || '포스터 저장 완료', 'success');
            $('#imageCropModal').modal('hide');
            custom_upload_payload = null;

            var $dbModal = $('#dbEditModal');
            if ($dbModal.hasClass('show') && ($dbModal.find('#edit_code').val() === code)) {
                reloadDbEditModalData(code, targetCategory, $dbModal);
            } else {
                window.globalRequestSearch(null, true);
            }
        } else {
            var errMsg = (ret && ret.msg) ? ret.msg : '저장에 실패했습니다.';
            if (typeof notify === 'function') notify(errMsg, 'warning');
        }
    }, { category: targetCategory });
});

(function() {
    var isDragging = false, offset = { x: 0, y: 0 };
    var $activeModal = null;

    function getModalStorageConfig($modal) {
        if ($modal.hasClass('metadata-delete-modal')) return null;
        var id = ($modal.attr('id') || '').toLowerCase();
        if (id.indexOf('person') !== -1) {
            return { key: 'person_edit_modal_geometry', defaultW: 880, defaultH: 580, minW: 600, minH: 400 };
        } else if (id.indexOf('actor') !== -1) {
            return { key: 'actor_search_modal_geometry', defaultW: 620, defaultH: 500, minW: 480, minH: 350 };
        } else if (id.indexOf('videopreview') !== -1) {
            return { key: 'video_preview_modal_geometry', defaultW: 920, defaultH: 560, minW: 380, minH: 260 };
        } else if (id.indexOf('crop') !== -1 || id.indexOf('imageenlarge') !== -1 || id.indexOf('dbjson') !== -1 || id.indexOf('pgadmin') !== -1) {
            return null;
        } else {
            return { key: 'db_edit_modal_geometry', defaultW: 880, defaultH: 580, minW: 600, minH: 400 };
        }
    }

    function applyModalGeometry($modal) {
        var cfg = getModalStorageConfig($modal);
        if (!cfg) return;

        var $dialog = $modal.find('.modal-dialog');
        var $content = $modal.find('.modal-content');
        if (!$dialog.length || !$content.length) return;

        var isClone = $modal.hasClass('dynamic-modal-clone');
        var saved = localStorage.getItem(cfg.key);

        if (saved) {
            try {
                var geom = JSON.parse(saved);
                var left = Math.max(10, Math.min(window.innerWidth - 200, geom.left));
                var top = Math.max(10, Math.min(window.innerHeight - 150, geom.top));

                var minW = cfg.minW || 380;
                var minH = cfg.minH || 260;
                var width = Math.max(minW, Math.min(window.innerWidth - 30, geom.width || cfg.defaultW));
                var height = Math.max(minH, Math.min(window.innerHeight - 30, geom.height || cfg.defaultH));

                if (isClone) {
                    left = Math.min(window.innerWidth - 300, left + 25);
                    top = Math.min(window.innerHeight - 200, top + 25);
                }

                $dialog.css({
                    position: 'fixed',
                    margin: '0',
                    left: left + 'px',
                    top: top + 'px',
                    width: width + 'px',
                    maxWidth: 'none',
                    transform: 'none'
                });
                $content.css({
                    width: width + 'px',
                    height: height + 'px'
                });
            } catch (e) {}
        }
    }

    $(document).on('show.bs.modal', '.modal', function() {
        applyModalGeometry($(this));
    });

    $(document).on('mouseup', '.modal-content', function() {
        var $modal = $(this).closest('.modal');
        var cfg = getModalStorageConfig($modal);
        if (!cfg) return;

        var $dialog = $modal.find('.modal-dialog');
        var $content = $(this);
        if ($dialog.length && $dialog[0].style.position === 'fixed') {
            var rect = $dialog[0].getBoundingClientRect();
            var geom = {
                left: rect.left,
                top: rect.top,
                width: $content.outerWidth(),
                height: $content.outerHeight()
            };
            try { localStorage.setItem(cfg.key, JSON.stringify(geom)); } catch(e) {}
        }
    });

    $(document).on('mousedown', '.modal-header', function(e) {
        if ($(e.target).closest('button, input, a, select, textarea').length > 0) return;
        var $modal = $(this).closest('.modal');
        if ($modal.length === 0 || $modal.attr('id') === 'imageEnlargeModal' || $modal.hasClass('metadata-delete-modal')) return;

        var $dialog = $modal.find('.modal-dialog');
        var $content = $modal.find('.modal-content');
        if ($dialog.length === 0 || $content.length === 0) return;

        isDragging = true;
        $activeModal = $modal;

        var rect = $dialog[0].getBoundingClientRect();
        offset.x = e.clientX - rect.left;
        offset.y = e.clientY - rect.top;
        $dialog.css({
            position: 'fixed',
            margin: '0',
            left: rect.left + 'px',
            top: rect.top + 'px',
            width: $content.outerWidth() + 'px',
            maxWidth: 'none',
            transform: 'none'
        });
    });

    $(document).on('mousemove', function(e){
        if (!isDragging || !$activeModal) return;
        var $dialog = $activeModal.find('.modal-dialog');
        if ($dialog.length === 0) return;
        var newLeft = e.clientX - offset.x;
        var newTop = e.clientY - offset.y;
        var dialogW = $dialog.outerWidth();
        newLeft = Math.max(0, Math.min(window.innerWidth - dialogW, newLeft));
        newTop = Math.max(0, Math.min(window.innerHeight - 60, newTop));
        $dialog.css({ left: newLeft + 'px', top: newTop + 'px' });
    }).on('mouseup', function(){
        if (isDragging && $activeModal) {
            isDragging = false;
            var cfg = getModalStorageConfig($activeModal);
            var $dialog = $activeModal.find('.modal-dialog');
            var $content = $activeModal.find('.modal-content');
            if (cfg && $dialog.length && $dialog[0].style.position === 'fixed') {
                var rect = $dialog[0].getBoundingClientRect();
                var geom = {
                    left: rect.left,
                    top: rect.top,
                    width: $content.outerWidth(),
                    height: $content.outerHeight()
                };
                try { localStorage.setItem(cfg.key, JSON.stringify(geom)); } catch(e) {}
            }
            $activeModal = null;
        }
    });
})();

$(document).on('click', '#btn_db_vacuum', function(e){
    e.preventDefault();
    var btn = $(this); var origText = btn.text(); btn.prop('disabled', true).text('최적화 중...');
    globalSendCommand('db_vacuum', null, null, null, function(ret){
        btn.prop('disabled', false).text(origText);
        if (typeof notify === 'function') notify(ret.msg, ret.ret == 'success' ? 'success' : 'warning');
    });
});

// 실시간 SSE 스트림 연결 및 배치 제어 엔진
var batchEventSource = null;
var batchStatusTimer = null;

var batchSnapshotLogs = null;
var batchSnapshotStatus = {};

function applyBatchSnapshot(status, logs) {
    var bType = get_batch_type();
    var signature = JSON.stringify([bType, logs]);
    if (signature !== batchSnapshotLogs) {
        var $terminal = $('#batch_terminal_logs');
        $terminal.empty();
        logs.forEach(function(line) {
            $terminal.append('<div>' + $('<div>').text(line).html() + '</div>');
        });
        if ($terminal.length && $('#batch_log_autoscroll').is(':checked')) {
            $terminal.scrollTop($terminal[0].scrollHeight);
        }
        batchSnapshotLogs = signature;
    }
    var previous = batchSnapshotStatus[bType];
    if (previous && status) {
        if ((status.success || 0) > (previous.success || 0)) {
            requestThrottledBatchListRefresh(false);
        }
        if (previous.is_running && status.is_running === false && status.total > 0) {
            requestThrottledBatchListRefresh(true);
        }
    }
    batchSnapshotStatus[bType] = status;
    updateBatchProgressUI(status);
}

function syncBatchStatus() {
    var bType = get_batch_type();
    globalSendCommand('db_batch_refresh_status', bType, null, null, function(ret){
        if (ret && ret.ret === 'success') {
            applyBatchSnapshot(ret.data, ret.logs || []);
        }
    }, { batch_type: bType });
}

var batchRefreshThrottleTimer = null;
var batchHasPendingRefresh = false;

function requestThrottledBatchListRefresh(immediate) {
    if (immediate) {
        if (batchRefreshThrottleTimer) {
            clearTimeout(batchRefreshThrottleTimer);
            batchRefreshThrottleTimer = null;
        }
        batchHasPendingRefresh = false;
        window.globalRequestSearch(null, true);
        return;
    }

    batchHasPendingRefresh = true;
    if (!batchRefreshThrottleTimer) {
        batchRefreshThrottleTimer = setTimeout(function() {
            batchRefreshThrottleTimer = null;
            if (batchHasPendingRefresh) {
                batchHasPendingRefresh = false;
                window.globalRequestSearch(null, true);
            }
        }, 3000);
    }
}

function appendBatchTerminalLog(message, eventType) {
    var $terminal = $('#batch_terminal_logs');
    if ($terminal.length === 0) return;

    var colorClass = '#58a6ff';
    if (eventType === 'warning') {
        colorClass = '#e3b341';
    } else if (eventType === 'error') {
        colorClass = '#f85149';
    }

    var lineHtml = '<div style="color: ' + colorClass + ';">' + $('<div>').text(message).html() + '</div>';
    $terminal.append(lineHtml);

    if ($('#batch_log_autoscroll').is(':checked')) {
        $terminal.scrollTop($terminal[0].scrollHeight);
    }
}

function updateBatchProgressUI(status) {
    if (!status) return;

    var total = status.total || 0;
    var current = status.current || 0;
    var success = status.success || 0;
    var fail = status.fail || 0;
    var skipped = status.skipped || 0;

    var percent = total > 0 ? (current / total * 100).toFixed(1) : 0;
    $('#batch_progress_bar').css('width', percent + '%').text(percent + '%');

    if (status.is_running) {
        $('#btn_batch_start').hide();
        $('#batch_resume_btn_group').hide();
        $('#btn_batch_stop').show();
        $('#batch_badge_status').removeClass('badge-secondary badge-success badge-warning').addClass('badge-info').text('작업 진행 중');
        $('#batch_current_code_text').text(status.current_code || '진행 중');
        $('#batch_progress_bar').addClass('progress-bar-animated bg-info').removeClass('bg-success bg-danger bg-warning');
    } else {
        $('#btn_batch_stop').hide();
        $('#batch_progress_bar').removeClass('progress-bar-animated bg-info');

        if (status.can_resume) {
            $('#btn_batch_start').hide();
            $('#batch_resume_btn_group').css('display', 'inline-block');
            $('#batch_badge_status').removeClass('badge-info badge-secondary badge-success').addClass('badge-warning').text('일시 정지');
            $('#batch_progress_bar').addClass('bg-warning');
        } else {
            $('#batch_resume_btn_group').hide();
            $('#btn_batch_start').show();
            if (status.status === '완료') {
                $('#batch_badge_status').removeClass('badge-info badge-secondary badge-warning').addClass('badge-success').text('완료');
                $('#batch_progress_bar').addClass('bg-success');
            } else {
                $('#batch_badge_status').removeClass('badge-info badge-success badge-warning').addClass('badge-secondary').text(status.status || '대기 중');
            }
        }
        $('#batch_current_code_text').text(status.status || '대기 중');
    }

    var statsStr = '진행: ' + current.toLocaleString() + ' / ' + total.toLocaleString() + ' (성공: ' + success.toLocaleString() + ' | 실패: ' + fail.toLocaleString() + ' | 건너뜀: ' + skipped.toLocaleString() + ')';
    var elapsedStr = status.elapsed_str || '';
    var etaStr = status.eta_str || '';

    if (status.is_running) {
        if (elapsedStr) statsStr += ' | 소요: ' + elapsedStr;
        if (etaStr && etaStr !== '00:00') statsStr += ' | 남은시간: ' + etaStr;
    } else if (status.status === '완료') {
        if (elapsedStr) statsStr += ' | 총 소요: ' + elapsedStr;
    } else if (status.can_resume) {
        if (elapsedStr) statsStr += ' | 소요: ' + elapsedStr;
    }

    $('#batch_stats_text').text(statsStr);
}

function connectBatchSSE() {
    if (batchEventSource) {
        batchEventSource.close();
        batchEventSource = null;
    }

    if (typeof EventSource === 'undefined') {
        console.warn('[SSE] 브라우저가 EventSource를 지원하지 않습니다. 폴링으로 전환합니다.');
        if (!batchStatusTimer) {
            batchStatusTimer = setInterval(syncBatchStatus, 1500);
        }
        return;
    }

    var bType = get_batch_type();
    var sseUrl = '/' + package_name + '/normal/meta_db/batch_stream?type=' + encodeURIComponent(bType);
    batchEventSource = new EventSource(sseUrl);

    batchEventSource.onmessage = function(e) {
        try {
            var data = JSON.parse(e.data);
            if (data.type === 'init' || data.type === 'sync') {
                applyBatchSnapshot(data.status, data.logs || []);
            } else {
                appendBatchTerminalLog(data.message, data.type);
                updateBatchProgressUI(data.status);

                if (data.is_updated === true) {
                    requestThrottledBatchListRefresh(false);
                }

                if (data.status && data.status.is_running === false && data.status.total > 0) {
                    requestThrottledBatchListRefresh(true);
                }
            }
        } catch (err) {}
    };

    batchEventSource.onerror = function() {
        if (batchEventSource) {
            batchEventSource.close();
            batchEventSource = null;
        }
        if (!batchStatusTimer) {
            batchStatusTimer = setInterval(syncBatchStatus, 1500);
        }
    };
}

$(document).on('click', '#btn_batch_stop', function(e){
    e.preventDefault();
    var bType = get_batch_type();
    var bLabel = (bType === 'person') ? '인물' : '영상 메타';

    if (!confirm("진행 중인 " + bLabel + " 배치 갱신 작업을 중단하시겠습니까?")) return;

    var $btn = $(this);
    $btn.prop('disabled', true).text('중단 처리 중...');

    globalSendCommand('db_batch_refresh_stop', bType, null, null, function(ret){
        if (typeof notify === 'function') notify(ret.msg || '중단 요청이 전달되었습니다.', 'warning');
        $btn.prop('disabled', false).text('작업 중단');
        syncBatchStatus();
        requestThrottledBatchListRefresh(true);
    }, { batch_type: bType });
});

$(document).on('click', '#btn_batch_clear_log', function(e){
    e.preventDefault();
    $('#batch_terminal_logs').empty();
});

// AV 통합 페이지 진입 시 SSE 스트림 자동 연결 확인
$(document).ready(function(){
    if ($('#batch_terminal_logs').length > 0) {
        $('#btn_batch_start').show();
        $('#batch_resume_btn_group').hide();
        $('#btn_batch_stop').hide();

        // 실시간 서버 상태와 즉시 동기화
        syncBatchStatus();
        connectBatchSSE();
    }
});

function set_db_engine_view(val) {
    if (val === 'postgres') {
        $('#meta_db_engine_postgres_div').collapse('show');
        $('#meta_db_engine_sqlite_div').collapse('hide');
    } else {
        $('#meta_db_engine_sqlite_div').collapse('show');
        $('#meta_db_engine_postgres_div').collapse('hide');
    }
}

$(document).on('click', '#btn_open_pg_admin_modal', function(e){
    e.preventDefault();
    var mainHost = ($('#meta_db_pg_host').val() || '').trim();
    var mainPort = ($('#meta_db_pg_port').val() || '').trim();
    var mainDb = ($('#meta_db_pg_name').val() || '').trim();
    var mainUser = ($('#meta_db_pg_user').val() || '').trim();
    var mainPass = ($('#meta_db_pg_pass').val() || '').trim();

    $('#pg_target_host').val(mainHost || 'postgres');
    $('#pg_target_port').val(mainPort || '5432');
    $('#pg_target_db').val(mainDb || 'metadata');
    $('#pg_target_user').val(mainUser || 'metadata');
    $('#pg_target_pass').val(mainPass);
    $('#pgAdminModal').modal('show');
});

$(document).on('click', '#btn_test_db_conn', function(e){
    e.preventDefault();
    var db_type = $('input[name="meta_db_engine_type"]:checked').val() || 'sqlite';
    var conn_type = $('input[name="meta_db_pg_conn_type"]:checked').val() || 'tcp';
    var host = ($('#meta_db_pg_host').val() || '').trim() || 'postgres';
    var port = ($('#meta_db_pg_port').val() || '').trim() || '5432';
    var socket_dir = ($('#meta_db_pg_socket_dir').val() || '').trim() || '/var/run/postgresql';
    var user = ($('#meta_db_pg_user').val() || '').trim();
    var password = ($('#meta_db_pg_pass').val() || '').trim();
    var dbname = ($('#meta_db_pg_name').val() || '').trim();

    var final_host = (conn_type === 'socket') ? socket_dir : host;

    var connPayload = {
        db_type: db_type,
        conn_type: conn_type,
        host: final_host,
        port: port,
        user: user,
        password: password,
        dbname: dbname
    };

    globalSendCommand('db_test_connection', db_type, JSON.stringify(connPayload), null, function(ret){
        notify(ret.msg, ret.ret === 'success' ? 'success' : 'warning');
    });
});

$(document).on('click', '#btn_pg_test_admin, #btn_pg_create_db, #btn_pg_drop_db', function(e){
    e.preventDefault();
    var btn_id = $(this).attr('id');
    var action = (btn_id === 'btn_pg_test_admin') ? 'test_admin' : ((btn_id === 'btn_pg_create_db') ? 'create_db_and_user' : 'drop_db_and_user');

    var hostInput = $('#pg_target_host').val().trim();
    var host = hostInput || $('#pg_target_host').attr('placeholder') || 'postgres';
    if (host.indexOf(' ') !== -1) host = host.split(' ')[0];
    $('#pg_target_host').val(host);

    var portInput = $('#pg_target_port').val().trim();
    var port = portInput || $('#pg_target_port').attr('placeholder') || '5432';
    $('#pg_target_port').val(port);

    var admin_user = $('#pg_admin_user').val().trim() || 'postgres';
    $('#pg_admin_user').val(admin_user);

    var admin_pass = $('#pg_admin_pass').val().trim();
    var target_db = $('#pg_target_db').val().trim() || 'metadata';
    var target_user = $('#pg_target_user').val().trim() || 'metadata';
    var target_pass = $('#pg_target_pass').val().trim();

    if (!host) {
        if (typeof notify === 'function') notify('대상 서버 주소(Host)를 입력하세요.', 'warning');
        $('#pg_target_host').trigger('focus');
        return;
    }
    if (!admin_user) {
        if (typeof notify === 'function') notify('관리자 아이디(Superuser)를 입력하세요.', 'warning');
        $('#pg_admin_user').trigger('focus');
        return;
    }

    if (action !== 'test_admin') {
        if (!target_db || !target_user) {
            if (typeof notify === 'function') notify('생성/삭제할 대상 DB명과 유저명을 입력하세요.', 'warning');
            return;
        }
    }

    var btn = $(this);
    var origText = btn.text();
    btn.prop('disabled', true).text('처리 중...');

    var adminPayload = {
        action: action,
        host: host,
        port: port,
        admin_user: admin_user,
        admin_pass: admin_pass,
        target_db: target_db,
        target_user: target_user,
        target_pass: target_pass
    };

    globalSendCommand('db_pg_admin_action', action, JSON.stringify(adminPayload), null, function(ret){
        btn.prop('disabled', false).text(origText);
        notify(ret.msg, ret.ret === 'success' ? 'success' : 'warning');

        if (ret.ret === 'success' && action === 'create_db_and_user') {
            $('#meta_db_pg_host').val(host);
            $('#meta_db_pg_port').val(port);
            $('#meta_db_pg_name').val(target_db);
            $('#meta_db_pg_user').val(target_user);
            $('#meta_db_pg_pass').val(target_pass);
            $('#pgAdminModal').modal('hide');
        }
    });
});

function start_transfer_status_timer() {
    if (transfer_timer) {
        clearInterval(transfer_timer);
        transfer_timer = null;
    }

    transfer_timer = setInterval(function(){
        $.ajax({
            url: '/' + package_name + '/meta_api',
            type: 'POST',
            cache: false,
            global: false,
            data: { command: 'db_transfer_status' },
            dataType: 'json',
            success: function(ret){
                if (!ret || !ret.data) return;
                var data = ret.data;

                var total = data.total || 0;
                var current = data.current || 0;
                var inserted = data.inserted || 0;
                var updated = data.updated || 0;
                var skipped = data.skipped || 0;
                var fail = data.fail || 0;
                var mode = data.mode || 'merge';

                var pct = total > 0 ? (current / total * 100).toFixed(1) : 0;

                $('#transfer_progress_percent').text(pct + '%');
                $('#transfer_progress_bar').css('width', pct + '%').text(pct + '%');

                var statsText = '';
                if (mode === 'missing') {
                    statsText = '진행: ' + current.toLocaleString() + ' / ' + total.toLocaleString() + ' (신규: ' + inserted.toLocaleString() + ' | 건너뜀: ' + skipped.toLocaleString() + ' | 실패: ' + fail.toLocaleString() + ')';
                } else if (mode === 'merge') {
                    statsText = '진행: ' + current.toLocaleString() + ' / ' + total.toLocaleString() + ' (신규: ' + inserted.toLocaleString() + ' | 갱신: ' + updated.toLocaleString() + ' | 건너뜀: ' + skipped.toLocaleString() + ' | 실패: ' + fail.toLocaleString() + ')';
                } else {
                    statsText = '진행: ' + current.toLocaleString() + ' / ' + total.toLocaleString() + ' (복제: ' + inserted.toLocaleString() + ' | 실패: ' + fail.toLocaleString() + ')';
                }
                $('#transfer_progress_stats').text(statsText);
                $('#transfer_progress_status_text').text(data.status || '전송 진행 중...');

                if (data.current_code) {
                    $('#transfer_progress_code').text(data.current_code);
                }

                if (data.is_running === false && total > 0) {
                    clearInterval(transfer_timer);
                    transfer_timer = null;

                    $('#btn_transfer_start').prop('disabled', false).show();
                    $('#btn_transfer_stop').hide();
                    $('#transfer_progress_bar').removeClass('progress-bar-animated bg-primary').addClass('bg-success');

                    var finishMsg = data.status || '데이터 복제가 완료되었습니다.';
                    if (typeof notify === 'function') {
                        notify(finishMsg, data.fail > 0 ? 'warning' : 'success');
                    }
                }
            }
        });
    }, 1000);
}

var import_status_timer = null;
function start_import_status_timer() {
    if (import_status_timer) {
        clearInterval(import_status_timer);
        import_status_timer = null;
    }

    import_status_timer = setInterval(function(){
        $.ajax({
            url: '/' + package_name + '/meta_api',
            type: 'POST',
            cache: false,
            global: false,
            data: { command: 'db_import_status' },
            dataType: 'json',
            success: function(ret){
                if (!ret || !ret.data) return;
                var data = ret.data;

                var total = data.total || 0;
                var current = data.current || 0;
                var inserted = data.inserted || 0;
                var updated = data.updated || 0;
                var skipped = data.skipped || 0;
                var fail = data.fail || 0;

                var pct = total > 0 ? (current / total * 100).toFixed(1) : 0;

                $('#import_progress_percent').text(pct + '%');
                $('#import_progress_bar').css('width', pct + '%').text(pct + '%');
                $('#import_progress_stats').text('신규: ' + inserted.toLocaleString() + ' | 갱신: ' + updated.toLocaleString() + ' | 건너뜀: ' + skipped.toLocaleString() + ' | 실패: ' + fail.toLocaleString());
                $('#import_progress_status_text').text(data.status || '임포트 진행 중...');

                if (data.current_code) {
                    $('#import_progress_code').text(data.current_code);
                }

                if (data.is_running === false && total > 0) {
                    clearInterval(import_status_timer);
                    import_status_timer = null;

                    $('#btn_db_import_merge, #btn_db_import_missing').prop('disabled', false).show();
                    $('#btn_db_import_stop').hide();
                    $('#import_progress_bar').removeClass('progress-bar-animated bg-info').addClass('bg-success');

                    var finishMsg = data.status || '임포트 작업이 완료되었습니다.';
                    if (typeof notify === 'function') {
                        notify(finishMsg, data.fail > 0 ? 'warning' : 'success');
                    }
                }
            }
        });
    }, 1000);
}

$(document).on('click', '#btn_transfer_start', function(e){
    e.preventDefault();
    var dir = $('#meta_db_transfer_direction').val() || 'sqlite_to_pg';
    var mode = $('input[name="meta_db_transfer_mode"]:checked').val() || 'merge';
    var src = (dir === 'sqlite_to_pg') ? 'sqlite' : 'postgres';
    var tgt = (dir === 'sqlite_to_pg') ? 'postgres' : 'sqlite';

    if (mode === 'clean') {
        var tgtLabel = (tgt === 'postgres') ? 'PostgreSQL' : 'SQLite3';
        if (!confirm("⚠️ [경고] 목적지(" + tgtLabel + ")의 모든 작품 및 인물 데이터가 완전히 삭제된 후 소스 데이터로 복제됩니다.\n정말 계속하시겠습니까?")) {
            return;
        }
    }

    $('#btn_transfer_start').prop('disabled', true).hide();
    $('#btn_transfer_stop').show();

    $('#transfer_progress_div').slideDown(150);
    $('#transfer_progress_status_text').text('데이터 복제 작업을 시작합니다...');
    $('#transfer_progress_percent').text('0.0%');
    $('#transfer_progress_bar').css('width', '0%').text('0%').removeClass('bg-success bg-danger').addClass('progress-bar-animated bg-primary');
    $('#transfer_progress_stats').text('복제 준비 중...');
    $('#transfer_progress_code').text('-');

    if (typeof notify === 'function') {
        notify('DB 데이터 복제를 백그라운드에서 시작합니다...', 'info');
    }

    start_transfer_status_timer();

    var transferPayload = {
        src: src,
        tgt: tgt,
        mode: mode
    };

    $.ajax({
        url: '/' + package_name + '/meta_api',
        type: 'POST',
        cache: false,
        global: false,
        data: {
            command: 'db_transfer_start',
            arg1: src,
            arg2: tgt,
            arg3: JSON.stringify(transferPayload)
        },
        dataType: 'json',
        success: function(ret){
            if (!ret || ret.ret !== 'success') {
                if (transfer_timer) {
                    clearInterval(transfer_timer);
                    transfer_timer = null;
                }
                $('#btn_transfer_start').prop('disabled', false).show();
                $('#btn_transfer_stop').hide();
                if (typeof notify === 'function') {
                    notify('전송 요청 실패: ' + (ret ? ret.msg : '응답 없음'), 'warning');
                }
            }
        },
        error: function(xhr, status, error){
            if (transfer_timer) {
                clearInterval(transfer_timer);
                transfer_timer = null;
            }
            $('#btn_transfer_start').prop('disabled', false).show();
            $('#btn_transfer_stop').hide();
            if (typeof notify === 'function') {
                notify('전송 통신 에러: ' + error, 'warning');
            }
        }
    });
});

$(document).on('click', '#btn_transfer_stop', function(e){
    e.preventDefault();
    if (!confirm("진행 중인 DB 전송 작업을 중단하시겠습니까?")) return;

    globalSendCommand('db_transfer_stop', null, null, null, function(ret){
        if (typeof notify === 'function') {
            notify(ret.msg || '전송 중단 요청이 전달되었습니다.', 'warning');
        }
    });
});

$(document).on('click', '#btn_db_import_stop', function(e){
    e.preventDefault();
    if (!confirm("진행 중인 임포트 작업을 중단하시겠습니까?")) return;

    globalSendCommand('db_import_stop', null, null, null, function(ret){
        if (typeof notify === 'function') {
            notify(ret.msg || '중단 요청이 전송되었습니다.', 'warning');
        }
    });
});

$(document).on('click', '#btn_db_import_merge, #btn_db_import_missing', function(e){
    e.preventDefault();
    var path = $('#meta_db_import_path').val();
    if (!path || !path.trim()) { 
        notify("경로를 입력하세요.", 'warning'); 
        return; 
    }
    
    var mode = ($(this).attr('id') === 'btn_db_import_merge') ? 'update' : 'missing';
    var mode_label = (mode === 'update') ? '스마트 병합' : '없는 것만';

    $('#btn_db_import_merge, #btn_db_import_missing').prop('disabled', true);
    $('#btn_db_import_stop').show();

    $('#import_progress_div').slideDown(150);
    $('#import_progress_status_text').text(mode_label + ' 임포트 작업을 시작합니다...');
    $('#import_progress_percent').text('0.0%');
    $('#import_progress_bar').css('width', '0%').text('0%').removeClass('bg-danger bg-success').addClass('progress-bar-animated bg-info');
    $('#import_progress_stats').text('임포트 준비 중...');
    $('#import_progress_code').text('-');

    if (typeof notify === 'function') {
        notify(mode_label + " 임포트 작업을 백그라운드에서 시작합니다...", 'info');
    }

    start_import_status_timer();

    globalSendCommand('db_import', path, mode, null, function(ret){
        if (!ret || ret.ret !== 'success') {
            if (import_status_timer) {
                clearInterval(import_status_timer);
                import_status_timer = null;
            }
            $('#btn_db_import_merge, #btn_db_import_missing').prop('disabled', false);
            $('#btn_db_import_stop').hide();
            if (typeof notify === 'function') {
                notify('임포트 요청 실패: ' + (ret ? ret.msg : '서버 응답 없음'), 'warning');
            }
        }
    });
});

$(document).on('click', '#btn_db_export_clean', function(e){
    e.preventDefault();

    var btn = $(this);
    var origText = btn.text();
    btn.prop('disabled', true).text('생성 중...');

    globalSendCommand('db_export', 'all', 'true', null, function(ret){
        btn.prop('disabled', false).text(origText);
        if (ret.ret === 'success') {
            notify(ret.msg, 'success');
        } else {
            notify('Export 실패: ' + (ret.msg || '알 수 없는 오류'), 'warning');
        }
    });
});

$(document).on('click', '.btn_open_sub_person_modal', function(e){
    e.preventDefault();
    var rawSubAttr = $(this).attr('data-sub-actor');
    if (!rawSubAttr) return;

    var subData = null;
    try {
        subData = JSON.parse(decodeURIComponent(rawSubAttr));
    } catch (err) {
        try { subData = JSON.parse(rawSubAttr); } catch (e2) {}
    }
    if (!subData) return;

    var currentDomain = $('#personEditModal').find('#p_edit_domain').val() || 'JAV';
    var subPersonObj = {
        id: null,
        domain: currentDomain,
        person_idx: subData.actor_id || '',
        name_org: subData.name_org || '',
        name_ko: subData.name_ko || '',
        name_en: subData.name_en || '',
        thumb: subData.site_img_url || '',
        media_src: {
            local_img_path: subData.local_img_path || '',
            google_fileid: subData.google_fileid || '',
            site_img_url: subData.site_img_url || '',
            site_img_urls: subData.site_img_url ? [subData.site_img_url] : []
        },
        extra_info: {
            birth: subData.birth || '',
            body_size: subData.body_size || '',
            bra_size: subData.bra_size || '',
            debut: subData.debut || '',
            info_url: subData.info_url || '',
            source_origin: 'merged_sub_view'
        },
        works: {},
        works_detailed: {}
    };

    var $subModal = getOrCreateModal('#personEditModal');
    renderPersonModalContent(subPersonObj, $subModal);
    $subModal.modal('show');
});

$(document).on('click', '#btn_toggle_sub_actors', function(e){
    e.preventDefault();
    var $tableDiv = $(this).closest('#p_merged_sub_actors_wrapper').find('#p_merged_sub_table_collapse');
    $tableDiv.slideToggle(150);
});

$(document).on('click', '.btn_person_sub_set_master', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var personId = $(this).attr('data-person-id') || $(this).data('person-id') || $modal.find('#p_edit_id').val();
    var personIdx = $(this).attr('data-person-idx') || $(this).data('person-idx') || $modal.find('#p_edit_idx').val();
    var subId = $(this).attr('data-sub-id') || $(this).data('sub-id');

    if (!subId) {
        if (typeof notify === 'function') notify('대상 서브 ID가 없습니다.', 'warning');
        return;
    }

    if (!confirm('[' + subId + '] 인물을 이 레코드의 새로운 대표 ID 및 프로필로 지정하시겠습니까?')) return;

    var targetIdentifier = personId || personIdx || subId;
    globalSendCommand('person_sub_set_master', String(targetIdentifier), String(subId), null, function(ret){
        if (ret && ret.ret === 'success') {
            if (typeof notify === 'function') notify(ret.msg || '대표 지정 성공', 'success');
            $modal.modal('hide');
            window.globalRequestSearch(null, true);
        } else {
            var errMsg = (ret && (ret.msg || ret.message || ret.data)) ? (ret.msg || ret.message || ret.data) : (typeof ret === 'string' ? ret : '서버 응답 없음');
            if (typeof notify === 'function') notify('대표 지정 실패: ' + errMsg, 'warning');
        }
    });
});

$(document).on('click', '.btn_person_sub_split', function(e){
    e.preventDefault();
    var $modal = $(this).closest('.modal');
    var personId = $(this).attr('data-person-id') || $(this).data('person-id') || $modal.find('#p_edit_id').val();
    var personIdx = $(this).attr('data-person-idx') || $(this).data('person-idx') || $modal.find('#p_edit_idx').val();
    var subId = $(this).attr('data-sub-id') || $(this).data('sub-id');

    if (!subId) {
        if (typeof notify === 'function') notify('대상 서브 ID가 없습니다.', 'warning');
        return;
    }

    if (!confirm('[' + subId + '] 인물을 현재 그룹에서 분리하여 독립된 별도의 인물 레코드로 만드시겠습니까?\n(분리 후 재동기화 시에도 다시 합쳐지지 않습니다)')) return;

    var targetIdentifier = personId || personIdx || subId;
    globalSendCommand('person_sub_split', String(targetIdentifier), String(subId), null, function(ret){
        if (ret && ret.ret === 'success') {
            if (typeof notify === 'function') notify(ret.msg || '그룹 분리 성공', 'success');
            $modal.modal('hide');
            window.globalRequestSearch(null, true);
        } else {
            var errMsg = (ret && (ret.msg || ret.message || ret.data)) ? (ret.msg || ret.message || ret.data) : (typeof ret === 'string' ? ret : '서버 응답 없음');
            if (typeof notify === 'function') notify('그룹 분리 실패: ' + errMsg, 'warning');
        }
    });
});
