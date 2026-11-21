# v0.33 — 구글 계정 전용 로그인, Supabase 저장, Render 배포 구성 (2026-11-21)

게임 규칙과 시뮬레이션 로직은 v0.32와 같다. 바뀐 것은 로그인과 저장 위치다.

## 로그인
- 아이디·비밀번호 회원가입과 로그인을 삭제했다(`/register` 경로, 비밀번호 해시 코드, SQLite `users` 테이블).
- 구글 계정 로그인만 남겼다. Supabase Auth의 구글 로그인(PKCE 방식)을 쓴다. 경로: `/login`(화면) → `/login/google`(구글로 이동) → `/auth/callback`(돌아와서 사용자 확인).
- 서버 세션에는 Supabase 사용자 ID(UUID), 표시 이름, 이메일만 저장한다. Supabase 토큰은 보관하지 않는다.
- 구글이 아닌 수단으로 만들어진 Supabase 계정은 콜백에서 거부한다.
- 로컬 개발용 임시 로그인(`DEV_LOGIN=1`)을 추가했다. Supabase 환경변수가 설정된 서버에서는 동작하지 않는다.

## 저장
- `storage.py` 추가. 환경변수 `SUPABASE_URL`, `SUPABASE_SECRET_KEY`가 있으면 Supabase Postgres에, 없으면 로컬 `data/` 폴더에 저장한다.
- 테이블 3개: `profiles`(계정), `game_saves`(계정당 세이브 1개, 게임 전체를 jsonb 1행으로), `auth_logs`(로그인 기록). 정의는 `supabase_schema.sql`.
- 한 요청 안에서 세이브를 한 번만 읽도록 했다. 이전에는 화면 하나를 그릴 때 세이브 파일을 여러 번 읽었다.
- 저장소 읽기에 실패하면 503 오류 화면을 보여준다. 이전에는 읽기 실패를 "세이브 없음"으로 처리해서, 그 상태에서 새 게임을 시작하면 기존 세이브가 덮어써질 수 있었다.

## 배포
- `render.yaml`, `DEPLOY.md` 추가. `requirements.txt`에 `requests` 추가.
- Render 프록시 뒤에서 https 주소를 정확히 만들도록 `ProxyFix` 적용. Supabase가 설정된 서버에서는 세션 쿠키에 Secure 속성을 기본으로 켠다.
- Supabase가 설정된 서버에서 `SECRET_KEY`가 없으면 서버가 시작되지 않는다(기본 서명 키로 운영되는 것을 막기 위해).
- 사용하지 않는 `templates/landing - 복사본.html`과 `__pycache__` 삭제.

## 검증
- Supabase HTTP 응답을 흉내 낸 테스트로 확인: 비로그인 차단, 구글 로그인 주소 생성, 잘못된 코드·재사용·구글 외 수단 거부, 세이브 저장과 재로그인, 계정 간 세이브 분리, 저장소 장애 시 503.
- 로컬 저장 모드로 한 시즌 전체(28주 → 포스트시즌 → FA 21일 → 드래프트 → 다음 시즌 시작) 진행, 시스템 점검 오류 0건.
- 실제 Supabase 프로젝트와 실제 구글 계정으로는 테스트하지 않았다. 배포 후 `DEPLOY.md` 4번 순서로 확인이 필요하다.
