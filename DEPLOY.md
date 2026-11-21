# 배포 안내 — GitHub → Render, 저장은 Supabase, 로그인은 구글 계정 전용

## 전체 구조
- 코드: GitHub 저장소. Render가 이 저장소를 받아 `gunicorn app:app`으로 실행한다.
- 로그인: Supabase Auth의 구글 로그인. 아이디·비밀번호 가입은 없다.
- 저장: Supabase Postgres의 테이블 3개(`profiles`, `game_saves`, `auth_logs`). Render 서버 디스크에는 아무것도 남기지 않는다.

## 1. Supabase 준비
1. Supabase에서 프로젝트를 만든다.
2. SQL Editor에 `supabase_schema.sql` 내용을 붙여 넣고 실행한다. 테이블 3개가 만들어진다.
3. 프로젝트의 API 키 화면에서 다음 세 값을 확인한다.
   - 프로젝트 URL (`https://<프로젝트 ID>.supabase.co`)
   - publishable 키 (`sb_publishable_...`, 기존 프로젝트는 anon 키)
   - secret 키 (`sb_secret_...`, 기존 프로젝트는 service_role 키). 이 키는 모든 테이블을 읽고 쓸 수 있으므로 GitHub에 올리지 않는다.

## 2. 구글 로그인 준비
1. Google Cloud 콘솔에서 OAuth 클라이언트 ID를 만든다. 애플리케이션 유형은 "웹 애플리케이션".
2. "승인된 리디렉션 URI"에 Supabase가 안내하는 콜백 주소를 넣는다. 이 주소는 Supabase 대시보드의 Authentication 설정 중 Google 제공자 항목에 표시되어 있고, 형식은 `https://<프로젝트 ID>.supabase.co/auth/v1/callback` 이다.
3. Supabase 대시보드의 Google 제공자 항목을 켜고, 구글에서 발급받은 클라이언트 ID와 클라이언트 시크릿을 입력한다.
4. Supabase 대시보드 Authentication 설정의 URL 설정에서
   - Site URL: Render 배포 주소 (예: `https://dynastykbo.onrender.com`)
   - Redirect URLs: `https://<Render 배포 주소>/auth/callback` 을 추가한다. 이 주소가 등록되어 있지 않으면 로그인 후 게임으로 돌아오지 못한다.

## 3. Render 배포
1. Render에서 GitHub 저장소를 연결해 Web Service를 만든다. 저장소의 `render.yaml`을 쓰면 빌드·실행 명령이 자동으로 들어간다.
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 2 --timeout 120`
2. 환경변수를 입력한다.

| 이름 | 값 | 필수 |
|---|---|---|
| `SUPABASE_URL` | Supabase 프로젝트 URL | 필수 |
| `SUPABASE_PUBLISHABLE_KEY` | publishable 키 (또는 anon 키) | 필수 |
| `SUPABASE_SECRET_KEY` | secret 키 (또는 service_role 키) | 필수 |
| `SECRET_KEY` | 임의의 긴 문자열. 세션 쿠키 서명에 쓴다. 바꾸면 모든 사용자가 다시 로그인해야 한다 | 필수 (없으면 서버가 시작되지 않음) |
| `PUBLIC_BASE_URL` | 배포 주소를 직접 지정할 때만. 예: `https://dynastykbo.onrender.com` | 선택 |

`DATA_DIR`과 Render 영구 디스크는 더 이상 필요 없다.

## 4. 배포 후 확인 순서
1. 배포 주소 첫 화면에서 "구글 계정으로 시작하기" → 구글 계정 선택 → 구단 선택 화면으로 돌아오는지.
2. 구단을 고른 뒤 Supabase의 `game_saves` 테이블에 행이 1개 생겼는지.
3. Render에서 서비스를 재시작(또는 재배포)한 뒤 다시 로그인했을 때 같은 세이브가 열리는지.
4. 상단 "로그인 기록" 메뉴에 방금 로그인이 보이는지.

## 로컬에서 실행할 때
Supabase 환경변수 없이 실행하면 저장은 `data/` 폴더의 파일로 하고, 구글 로그인은 꺼진다.
```bash
pip install -r requirements.txt
DEV_LOGIN=1 python app.py
```
`DEV_LOGIN=1`이면 로그인 화면에 임시 계정 버튼이 나온다. Supabase 환경변수가 설정된 서버에서는 `DEV_LOGIN=1`을 넣어도 이 버튼과 경로가 동작하지 않는다.

## 알아둘 점
- 계정당 세이브는 1개다. 새 게임을 시작하면 `game_saves`의 같은 행을 덮어쓴다.
- 세이브 1개의 크기는 개막 시점 약 0.6MB이고 한 시즌을 마치면 약 1.1MB다(재정 거래 내역이 매주 쌓이기 때문). 화면을 열 때마다 1번 읽고, 진행·계약 같은 동작마다 전체를 1번 다시 쓴다.
- v0.32까지의 아이디·비밀번호 계정과 서버 디스크의 세이브 파일은 새 구조로 옮기지 않는다. 구글 계정으로 처음 로그인하면 세이브가 없는 상태로 시작한다.
