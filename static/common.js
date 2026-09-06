// static/common.js

// 다중 이미지 갤러리 라이트박스 전역 상태
window.enlarge_image_gallery = [];
window.enlarge_image_idx = 0;

// 이미지 갤러리 열기 및 렌더링 함수
window.openImageEnlargeModal = function(images, startIdx) {
    if (!images) return;
    if (typeof images === 'string') {
        window.enlarge_image_gallery = [{ url: images, type: '', is_final: false }];
        window.enlarge_image_idx = 0;
    } else if (Array.isArray(images)) {
        window.enlarge_image_gallery = images.map(function(item) {
            if (typeof item === 'string') return { url: item, type: '', is_final: false };
            return {
                url: item.url || item.src || '',
                type: item.type || '',
                is_final: Boolean(item.is_final)
            };
        }).filter(function(it) { return Boolean(it.url); });
        window.enlarge_image_idx = Math.max(0, Math.min(startIdx || 0, window.enlarge_image_gallery.length - 1));
    }

    if (window.enlarge_image_gallery.length === 0) return;

    renderEnlargeModalCurrent();
    $('#imageEnlargeModal').modal('show');
};

function renderEnlargeModalCurrent() {
    if (window.enlarge_image_gallery.length === 0) return;
    var cur = window.enlarge_image_gallery[window.enlarge_image_idx];
    var total = window.enlarge_image_gallery.length;

    var $img = $('#enlarged_image');
    var $spinner = $('#enlarge_image_spinner');

    if ($img.attr('src') === cur.url && $img[0] && $img[0].complete && $img[0].naturalWidth > 0) {
        $spinner.hide();
        $img.css('opacity', '1');
    } else {
        $spinner.stop(true, true).fadeIn(120);
        $img.css('opacity', '0.55');

        $img.off('load.enlarge error.enlarge').on('load.enlarge error.enlarge', function() {
            $spinner.stop(true, true).fadeOut(140);
            $img.css('opacity', '1');
        });

        $img.attr('src', cur.url);

        if ($img[0] && $img[0].complete && $img[0].naturalWidth > 0) {
            $spinner.hide();
            $img.css('opacity', '1');
        }
    }

    // Poster, Landscape, Fanart 등 명확한 속성이 있을 때만 뱃지 표시
    var imgType = (cur.type || '').trim();
    if (imgType && imgType.toLowerCase() !== 'image') {
        var $enlargeBadge = $('#enlarge_img_type');
        $enlargeBadge.text(imgType).show();

        if (cur.is_final) {
            $enlargeBadge.attr('style', 'background: rgba(0, 123, 255, 0.75); color: #ffffff; font-size: 0.82rem; border: 1px solid rgba(255, 255, 255, 0.3); backdrop-filter: blur(4px);');
        } else {
            $enlargeBadge.attr('style', 'background: rgba(0, 0, 0, 0.55); color: #e0e6ed; font-size: 0.82rem; border: 1px solid rgba(255, 255, 255, 0.12); backdrop-filter: blur(4px);');
        }
    } else {
        $('#enlarge_img_type').text('').hide();
    }

    // 이미지가 2개 이상일 때만 카운터 및 좌우 내비게이션 바 노출
    if (total > 1) {
        $('#enlarge_counter').text((window.enlarge_image_idx + 1) + ' / ' + total).show();
        $('.enlarge-nav-wrapper').removeClass('nav-hidden').attr('style', 'display: flex !important;');
    } else {
        $('#enlarge_counter').text('').hide();
        $('.enlarge-nav-wrapper').addClass('nav-hidden').attr('style', 'display: none !important;');
    }

    // 뱃지와 카운터가 모두 없으면 상단 헤더 영역 숨김
    if ((!imgType || imgType.toLowerCase() === 'image') && total <= 1) {
        $('#enlarge_header_bar').hide();
    } else {
        $('#enlarge_header_bar').show();
    }
}

// FlaskFarm 내장 함수 덮어쓰기 (options 폼 데이터 완전 전송 보장)
window.globalSendCommand = function(command, arg1, arg2, arg3, callback, options) {
    var postData = {
        command: command || '',
        arg1: (arg1 !== undefined && arg1 !== null) ? arg1 : '',
        arg2: (arg2 !== undefined && arg2 !== null) ? arg2 : '',
        arg3: (arg3 !== undefined && arg3 !== null) ? arg3 : ''
    };
    if (options) $.extend(postData, options);
    $.ajax({
        url: '/' + package_name + '/ajax/' + sub,
        type: "POST",
        cache: false,
        data: postData,
        dataType: "json",
        success: function (data) { if (callback) callback(data); },
        error: function (request, status, error) { console.warn('[AJAX Error]:', command, status, error); }
    });
};

$(document).ready(function(){
    // 좌우 클릭 영역이 현재 이미지 높이에 정확히 맞춰지는 갤러리 모달 주입
    if ($('#imageEnlargeModal').length === 0) {
        var modalHtml = `
        <div class="modal fade" id="imageEnlargeModal" tabindex="-1" role="dialog" aria-hidden="true" style="user-select: none;">
          <div class="modal-dialog modal-dialog-centered justify-content-center" role="document" style="max-width: fit-content; margin: auto;">
            <div class="modal-content bg-transparent border-0 text-center position-relative">
              
              <!-- 본문: 이미지 높이에 정확히 맞춰지는 [좌측 바] - [중앙 이미지] - [우측 바] -->
              <div class="modal-body p-0 d-flex align-items-stretch justify-content-center position-relative" style="max-width: 96vw; max-height: 92vh;">
                <!-- 좌측 사이드 바 -->
                <div class="d-flex align-items-stretch mr-2 mr-md-3 enlarge-nav-wrapper">
                  <button type="button" class="btn border-0 enlarge-side-btn" id="btn_enlarge_prev" title="이전 이미지 (좌측 방향키 또는 클릭)">
                    <span class="enlarge-side-icon">&lsaquo;</span>
                  </button>
                </div>

                <!-- 중앙 이미지 래퍼 -->
                <div class="d-flex flex-column align-items-center justify-content-center position-relative" style="min-width: 140px; min-height: 140px;">
                  <!-- 상단 정보 뱃지 및 카운터 -->
                  <div id="enlarge_header_bar" class="d-flex justify-content-between align-items-center position-absolute w-100 px-3" style="top: 12px; left: 0; z-index: 20; pointer-events: none;">
                    <span class="badge shadow-sm px-3 py-1 font-weight-bold" id="enlarge_img_type" style="background: rgba(0, 0, 0, 0.55); color: #e0e6ed; font-size: 0.82rem; border: 1px solid rgba(255, 255, 255, 0.12); backdrop-filter: blur(4px);">Poster</span>
                    <span class="badge shadow-sm px-3 py-1 font-weight-bold" id="enlarge_counter" style="background: rgba(0, 0, 0, 0.55); color: #e0e6ed; font-size: 0.82rem; border: 1px solid rgba(255, 255, 255, 0.12); backdrop-filter: blur(4px);">1 / 1</span>
                  </div>

                  <div id="enlarge_image_spinner" class="spinner-border text-info position-absolute modal-image-spinner" role="status" style="width: 2.8rem; height: 2.8rem; z-index: 10; display: none;">
                    <span class="sr-only">Loading...</span>
                  </div>

                  <img id="enlarged_image" src="" class="rounded shadow-lg" style="max-height: 84vh; max-width: calc(90vw - 140px); width: auto; height: auto; object-fit: contain; background: #08090a; display: block; cursor: pointer;" alt="Enlarged Image" data-dismiss="modal" title="클릭하면 닫힙니다">
                </div>

                <!-- 우측 사이드 바 -->
                <div class="d-flex align-items-stretch ml-2 mr-md-3 enlarge-nav-wrapper">
                  <button type="button" class="btn border-0 enlarge-side-btn" id="btn_enlarge_next" title="다음 이미지 (우측 방향키 또는 클릭)">
                    <span class="enlarge-side-icon">&rsaquo;</span>
                  </button>
                </div>
              </div>

            </div>
          </div>
        </div>`;
        $('body').append(modalHtml);
    }
});

// 이미지 갤러리 이전/다음 내비게이션 클릭 핸들러
$(document).on('click', '#btn_enlarge_prev', function(e) {
    e.preventDefault();
    e.stopPropagation();
    if (window.enlarge_image_gallery.length <= 1) return;
    window.enlarge_image_idx = (window.enlarge_image_idx - 1 + window.enlarge_image_gallery.length) % window.enlarge_image_gallery.length;
    renderEnlargeModalCurrent();
});

$(document).on('click', '#btn_enlarge_next', function(e) {
    e.preventDefault();
    e.stopPropagation();
    if (window.enlarge_image_gallery.length <= 1) return;
    window.enlarge_image_idx = (window.enlarge_image_idx + 1) % window.enlarge_image_gallery.length;
    renderEnlargeModalCurrent();
});

// 이미지 클릭 시 확대 팝업 이벤트 (DB 편집 및 배우 편집 모달의 전체 갤러리 목록 연동)
$("body").on('click', '.enlarge-img, .enlarge-person-photo', function(){
    // DB 편집 모달의 미리보기 이미지 클릭 시 전체 갤러리 연동
    if ($(this).attr('id') === 'preview_modal_img' && typeof modal_preview_images !== 'undefined' && modal_preview_images.length > 0) {
        window.openImageEnlargeModal(modal_preview_images, typeof modal_preview_idx !== 'undefined' ? modal_preview_idx : 0);
        return;
    }

    // 배우 편집 모달의 미리보기 이미지 클릭 시 다중 이미지(LOCAL/GOOGLE/AVDBS) 갤러리 연동
    if ($(this).attr('id') === 'p_modal_preview_img' && typeof p_modal_preview_images !== 'undefined' && p_modal_preview_images.length > 0) {
        window.openImageEnlargeModal(p_modal_preview_images, typeof p_modal_preview_idx !== 'undefined' ? p_modal_preview_idx : 0);
        return;
    }

    var img_src = $(this).attr('data-src') || $(this).attr('src');
    if (img_src) {
        window.openImageEnlargeModal(img_src);
    }
});

// 다중 모달 활성 스택 (실제 화면에 열린 순서를 추적하는 LIFO 스택)
var activeModalStack = [];

// 모달이 열릴 때 스택 최상단에 푸시하고 모달 및 어두운 배경막(Backdrop)의 z-index를 동적 상향
$(document).on('show.bs.modal', '.modal', function () {
    var modalEl = this;
    activeModalStack = activeModalStack.filter(function(el) { return el !== modalEl; });
    activeModalStack.push(modalEl);

    var stackIndex = activeModalStack.length - 1;
    var baseZ = 1040;
    var modalZ = baseZ + 10 + (stackIndex * 20);
    var backdropZ = modalZ - 5;

    $(modalEl).css('z-index', modalZ);

    setTimeout(function () {
        var $backdrops = $('.modal-backdrop');
        if ($backdrops.length > 0) {
            $backdrops.last().css('z-index', backdropZ).addClass('modal-stack');
        }
    }, 10);
});

// 모달이 완전히 닫힐 때 스택에서 제거, z-index 초기화 및 하위 모달 상태/포커스 복원
$(document).on('hidden.bs.modal', '.modal', function () {
    var modalEl = this;
    activeModalStack = activeModalStack.filter(function(el) { return el !== modalEl; });

    $(modalEl).css('z-index', '');

    if (activeModalStack.length > 0) {
        $('body').addClass('modal-open');
        var topModalEl = activeModalStack[activeModalStack.length - 1];
        $(topModalEl).trigger('focus');
    }
});

// 키보드 이벤트: ESC로 최상위 모달 닫기 및 갤러리 열림 시 좌우 방향키로 슬라이드 탐색
document.addEventListener('keydown', function (e) {
    // 갤러리 모달이 최상단에 열려 있을 때 좌우 방향키로 이미지 넘기기
    if ($('#imageEnlargeModal').hasClass('show') && window.enlarge_image_gallery.length > 1) {
        if (e.key === 'ArrowLeft') {
            e.preventDefault();
            $('#btn_enlarge_prev').trigger('click');
            return;
        } else if (e.key === 'ArrowRight') {
            e.preventDefault();
            $('#btn_enlarge_next').trigger('click');
            return;
        }
    }

    // 최상위 모달부터 ESC로 닫기
    if (e.key === 'Escape' || e.keyCode === 27 || e.which === 27) {
        activeModalStack = activeModalStack.filter(function(el) {
            return $(el).hasClass('show');
        });

        if (activeModalStack.length > 0) {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();

            var topModalEl = activeModalStack[activeModalStack.length - 1];
            $(topModalEl).modal('hide');
        }
    }
}, true);
