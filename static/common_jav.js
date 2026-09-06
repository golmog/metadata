// static/common_jav.js (perform_actor_search 부분 확인 및 갱신)

function perform_actor_search() {
    var kw = $('#actor_search_kw').val().trim();
    if (!kw) { notify('검색할 배우 이름을 입력하세요.', 'warning'); return; }

    var domain = (sub === 'western') ? 'WESTERN' : 'JAV';
    $('#actor_search_results_tbody').html('<tr><td colspan="4" class="text-center text-info py-3">검색 중...</td></tr>');

    // person_search 명령은 person 또는 meta_db 모듈로 전송
    globalSendCommand('person_search', kw, domain, null, function(ret){
        if (ret.ret === 'success') {
            render_actor_search_results(ret.data);
        } else {
            $('#actor_search_results_tbody').html('<tr><td colspan="4" class="text-center text-danger py-3">검색 실패: ' + ret.msg + '</td></tr>');
        }
    });
}
