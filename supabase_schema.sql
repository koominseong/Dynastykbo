-- 니가 해봐 단장 Dynasty — Supabase 테이블 정의
-- Supabase 대시보드의 SQL Editor에 그대로 붙여 넣고 한 번 실행한다.
-- 세 테이블 모두 Row Level Security를 켜고 정책을 만들지 않는다.
-- 그러면 브라우저용 publishable(anon) 키로는 읽기·쓰기가 전부 막히고,
-- Render 서버가 가진 secret 키(service_role 권한)로만 접근할 수 있다.

-- 1) 계정 정보: 구글 로그인으로 만들어진 Supabase Auth 사용자 1명당 1행
create table if not exists public.profiles (
  user_id       uuid primary key references auth.users(id) on delete cascade,
  email         text,
  display_name  text,
  created_at    timestamptz not null default now(),
  last_login_at timestamptz
);

-- 2) 게임 세이브: 계정당 1개. 새 게임을 시작하면 같은 행을 덮어쓴다.
--    data 컬럼에 게임 전체 상태(선수단·일정·재정·기록)가 JSON으로 들어간다.
create table if not exists public.game_saves (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  game_id    text not null,
  team       text,
  season     integer,
  data       jsonb not null,
  updated_at timestamptz not null default now()
);

-- 3) 로그인 기록: 로그인 성공·실패, 로그아웃
create table if not exists public.auth_logs (
  id          bigint generated always as identity primary key,
  user_id     uuid,
  email       text,
  event       text not null,
  result      text not null,
  occurred_at timestamptz not null default now()
);
create index if not exists idx_auth_logs_user_time on public.auth_logs (user_id, occurred_at desc);

alter table public.profiles   enable row level security;
alter table public.game_saves enable row level security;
alter table public.auth_logs  enable row level security;

-- 서버 키(service_role)에 테이블 권한을 명시적으로 준다.
grant all on table public.profiles, public.game_saves, public.auth_logs to service_role;
grant usage, select on all sequences in schema public to service_role;
