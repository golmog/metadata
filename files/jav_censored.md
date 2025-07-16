
### 디스코드 Proxy

- 참고자료: [https://github.com/useapi/discord-cdn-proxy](https://github.com/useapi/discord-cdn-proxy)

- 동작하지 않는 URL: [https://cdn.discordapp.com/attachments/1239264794394234985/1239266735992078447/vault_boy.png?ex=66424c96&is=6640fb16&hm=0b3d3210b4ea0916d5c8c0b2d998a4f4b64f5b95b79cdb9b58ff96b8287dace4&](https://cdn.discordapp.com/attachments/1239264794394234985/1239266735992078447/vault_boy.png?ex=66424c96&is=6640fb16&hm=0b3d3210b4ea0916d5c8c0b2d998a4f4b64f5b95b79cdb9b58ff96b8287dace4&)

- 이치로님의 Discord Proxy 플러그인 설치 : [https://github.com/by275/discord_proxy](https://github.com/by275/discord_proxy)

    - 본인의 토큰의 값 설정.
    - 위의 동작하지 않는 URL에서 `https://cdn.discordapp.com` 값을 본인 FF DDNS로 변경하면 정상적으로 로딩 함.

- 본인의 웹훅을 사용하면 설정한 채널에 이미지가 올라오며 Discord Proxy 플러그인에서 proxy 처리여부 설정 가능.


### 이미지 서버

- FF는 기본적으로 본인 FF DDNS/images 예)`https://ff.sample.com/images` URL로 `/data/images' 폴더를 라우팅한다.

- 파일명: 
    - `{code}_p.jpg`: 포스터
    - `{code}_pl.jpg`: 아트
    - `{code}_art_{num}.jpg`: 팬아트

- _p_user.jpg / _pl_user.jpg 파일이 있으면 우선적으로 선택

- 고퀄 포스터를 설정할 때 유용.

    <img src="https://i.imgur.com/d2Vinpu.png" width="30%" height="30%" />