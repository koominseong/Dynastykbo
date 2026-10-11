from flask import Flask, render_template, request, redirect, url_for, flash, session
from datetime import datetime, timedelta
import random
import json
import os
import re
import math
import uuid
import sqlite3
from contextlib import contextmanager
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me-change-in-render")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
AUTH_DB = os.path.join(DATA_DIR, "dynasty.db")

@contextmanager
def db_connect():
    conn = sqlite3.connect(AUTH_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_auth_db():
    with db_connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS user_games (user_id INTEGER PRIMARY KEY, game_id TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id))")
        conn.execute("CREATE TABLE IF NOT EXISTS auth_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT NOT NULL, event TEXT NOT NULL, result TEXT NOT NULL, occurred_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_logs_user_time ON auth_logs(user_id, occurred_at DESC)")

def log_auth_event(username, event, result, user_id=None):
    """Persist account activity in SQLite; never store passwords or password hashes here."""
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO auth_logs(user_id, username, event, result, occurred_at) VALUES(?,?,?,?,?)",
                (user_id, (username or "")[:80], event[:40], result[:40], datetime.now().isoformat(timespec="seconds")),
            )
    except sqlite3.Error:
        app.logger.exception("Could not persist authentication event")

def user_game_id(user_id):
    if not user_id:
        return None
    with db_connect() as conn:
        row = conn.execute("SELECT game_id FROM user_games WHERE user_id=?", (user_id,)).fetchone()
        return row["game_id"] if row else None

def set_user_game(user_id, game_id):
    with db_connect() as conn:
        conn.execute("INSERT INTO user_games(user_id, game_id, updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET game_id=excluded.game_id, updated_at=excluded.updated_at", (user_id, game_id, datetime.now().isoformat(timespec="seconds")))

init_auth_db()

PUBLIC_ENDPOINTS = {"index", "login", "register", "logout", "static"}

@app.before_request
def require_login_for_game():
    if request.endpoint and request.endpoint not in PUBLIC_ENDPOINTS and not session.get("user_id"):
        return redirect(url_for("login", next=request.path))

@app.context_processor
def inject_auth_user():
    return {"auth_username": session.get("username"), "auth_user_id": session.get("user_id")}

RULES = [
    {"id":"R-01","name":"장기 등록 외국인 선수 슬롯 면제","category":"외국인 선수","change":"동일 구단에서 5시즌 이상 등록한 외국인 선수 1명을 외국인 선수 보유 한도 산정에서 제외한다.","trigger":"외국인 선수 제도의 안정성 논의 또는 장기 등록 선수 비중 증가.","prob":45,"effective":"통과 다음 시즌부터. 자격은 시즌 종료 시점 누적 등록 시즌으로 판정.","transition":"기준 충족 선수 중 구단당 최대 1명.","impact":"장기 재계약에 유리하나 외국인 선수 발굴력이 좋은 구단에 추가 이점.","implementation":"선수별 구단 등록 시즌 이력과 면제 슬롯 상태를 저장한다."},
    {"id":"R-02","name":"신인 드래프트 로터리픽 도입","category":"신인 드래프트","change":"최종 순위 하위 3개 구단의 1라운드 지명 순서를 추첨으로 정한다. 기본 추첨 확률은 동일하다.","trigger":"상·하위권 전력 격차 확대 또는 최하위 구단의 장기 침체.","prob":50,"effective":"통과 다음 드래프트부터.","transition":"이미 확정된 지명권에는 적용하지 않고 2라운드 이후는 기존 순서를 따른다.","impact":"최하위 팀의 지명권 독점은 줄지만 1순위 지명을 보장하지 않는다.","implementation":"추첨 대상, 확률, 결과 기록을 저장한다."},
    {"id":"R-03","name":"연고 지역 신인 우선 지명 부활","category":"신인 드래프트","change":"구단별 지정 연고 지역 출신 선수 1명을 일반 드래프트 전에 우선 지명할 수 있다.","trigger":"지역 팬덤 강화 또는 연고지 유망주 유출 논의.","prob":40,"effective":"통과 다음 드래프트부터.","transition":"연고 지역 판정은 대상자 공개 전에 고정. 포기 시 권리 소멸을 기본으로 한다.","impact":"지역 정체성이 강화되나 지역별 유망주 풀 차이가 격차를 만들 수 있다.","implementation":"선수 출신 지역과 구단 연고지 매핑이 필요하다."},
    {"id":"R-04","name":"지역 연고 선수 추가 지명 라운드","category":"신인 드래프트","change":"일반 드래프트 8~11라운드에 지역 연고 선수 전용 추가 지명 라운드를 둔다. 일반 지명권과 별도다.","trigger":"지역 선수 육성 정책 또는 퓨처스 뎁스 강화 요구.","prob":40,"effective":"통과 다음 드래프트부터.","transition":"이미 지명된 선수는 지역 라운드에서 제외한다.","impact":"지역 유망주 발굴에 도움이 되나 선수단·육성 비용 증가.","implementation":"지역 라운드 자격과 미사용 지명권 처리 규칙이 필요하다."},
    {"id":"R-05","name":"샐러리캡 하한 도입·조정","category":"재정·샐러리캡","change":"산정 대상 총연봉이 샐러리캡 상한의 80% 미만이면 개선 계획 제출 의무를 둔다. 1시즌 유예를 기본으로 한다.","trigger":"지나친 저비용 운영 또는 선수 투자 격차 확대.","prob":45,"effective":"통과 후 1시즌 유예를 거쳐 시행.","transition":"기존 장기 계약은 유지하고 해당 시즌 산정 기준을 따른다.","impact":"선수단 투자를 유도하지만 리빌딩 구단의 재정 유연성 감소.","implementation":"하한 비율, 유예 기간, 미달 조치를 설정값으로 둔다."},
    {"id":"R-06","name":"1군 등록 인원 확대","category":"선수단 등록","change":"게임의 기존 등록 한도에서 1군 등록 가능 인원을 2명 확대한다. 경기 출전 가능 인원은 별도 설정할 수 있다.","trigger":"피로·부상 부담 증가 또는 일정 과밀.","prob":60,"effective":"통과 다음 시즌 개막일부터.","transition":"시즌 중 소급 확대하지 않는다.","impact":"불펜·대타·백업 운영이 유연해지지만 선수층이 두꺼운 팀에 유리할 수 있다.","implementation":"등록 한도와 경기 출전 한도를 분리한다."},
    {"id":"R-07","name":"FA 자격 취득 기준 변경","category":"FA·계약","change":"FA 자격 취득에 필요한 누적 등록 기준을 1시즌 단축한다. FA 보상 규정은 별도 안건이다.","trigger":"선수 권리 확대 또는 FA 시장 진입 시점 논쟁.","prob":40,"effective":"통과 후 자격을 새로 쌓는 선수부터 적용하는 것을 기본으로 한다.","transition":"시행 직전 자격 취득 예정 선수의 소급 적용 여부를 명시한다.","impact":"선수의 FA 진입이 빨라지고 구단의 장기 계약 결정을 앞당긴다.","implementation":"자격 기준 버전과 선수별 누적 등록 기록을 보존한다."},
    {"id":"R-08","name":"연장전·비디오 판독 규정 변경","category":"경기 규칙","change":"연장전 최대 이닝, 무승부 처리, 비디오 판독 횟수·대상 중 한 항목을 선택해 변경한다.","trigger":"경기 시간 증가, 판정 논란 또는 무승부 빈도 변화.","prob":50,"effective":"통과 다음 시즌부터.","transition":"정규시즌·포스트시즌 적용 범위를 명시한다.","impact":"경기 결과, 감독 작전, 불펜 소모 및 경기 시간 변화.","implementation":"연장전과 비디오 판독 설정을 분리 관리한다."},
    {"id":"R-09","name":"포스트시즌 진출 구단 확대","category":"포스트시즌","change":"포스트시즌 진출 팀을 5개에서 6개로 확대하고 추가 팀은 와일드카드 라운드에 참가한다.","trigger":"시즌 후반 중위권 경쟁 조기 소멸 또는 흥행 확대 요구.","prob":55,"effective":"통과 다음 시즌부터.","transition":"진출 팀 수와 대진표·홈 어드밴티지·경기 수를 함께 확정한다.","impact":"더 많은 팀이 시즌 후반까지 경쟁하나 순위별 이점 재설계 필요.","implementation":"포스트시즌 진출 조건과 대진 생성기를 변경한다."},
    {"id":"R-10","name":"연장전 승부치기 도입","category":"경기 규칙","change":"정규이닝 종료 후 연장 10회부터 2루 주자를 둔 상태로 공격을 시작한다.","trigger":"경기 시간과 투수 소모의 지속적 증가.","prob":55,"effective":"통과 다음 시즌부터. 정규시즌 우선 적용.","transition":"포스트시즌 적용 여부를 별도로 설정한다.","impact":"경기 시간·득점 가능성 증가, 희생번트·대타 작전 가치 변화.","implementation":"주자 배치, 득점 기록, 투수 자책점 처리 기준을 정의한다."},
    {"id":"R-11","name":"투구 시간 제한 도입","category":"경기 규칙","change":"투수의 투구 간격과 타자의 준비 시간에 제한을 둔다. 위반 시 규정된 볼·스트라이크 페널티를 적용한다.","trigger":"경기 평균 시간이 목표치를 지속 초과할 때.","prob":60,"effective":"통과 다음 시즌부터. 첫 시즌은 적응 기간.","transition":"적응 기간에는 경고 중심, 이후 정식 페널티 적용.","impact":"경기 템포 개선 가능. 적응이 낮은 투수에게 제구·운영 불이익 가능.","implementation":"타이머, 위반 조건, 위반 이력, 페널티를 관리한다."},
    {"id":"R-12","name":"아시아 쿼터 제도 변경","category":"외국인 선수","change":"아시아 쿼터 보유 한도를 1명 확대하거나 계약·교체 조건을 완화한다. 한 안건에서는 한 종류만 변경한다.","trigger":"아시아권 선수 시장 확대 또는 외국인 선수 제도 논의.","prob":45,"effective":"통과 다음 시즌부터.","transition":"기존 계약 유지, 신규 등록·재계약부터 적용.","impact":"스카우팅 선택지 확대와 인건비·선수단 구성 변화.","implementation":"일반 외국인 슬롯과 아시아 쿼터 슬롯을 분리 관리한다."},
    {"id":"R-13","name":"부상자 명단 및 대체 등록","category":"선수단 등록·부상","change":"장기 부상 선수를 별도 부상자 명단에 올리고 대체 선수 1명을 임시 등록한다. 최소 등재 기간 기본값은 15일.","trigger":"장기 부상으로 인한 엔트리 손실 또는 선수 보호 요구 증가.","prob":65,"effective":"통과 다음 시즌부터.","transition":"시행 전 부상은 새 명단으로 소급 전환하지 않는다.","impact":"부상으로 인한 엔트리 손실 완화. 선수층이 두꺼운 팀에 유리할 수 있음.","implementation":"등재일, 최소 기간, 복귀 제한, 대체 선수 상태를 추적한다."},
    {"id":"R-14","name":"신인·육성 선수 등록 규정 변경","category":"신인·육성","change":"육성 선수 정식 전환 절차 또는 신인의 1군 등록 제한 중 하나를 조정한다.","trigger":"유망주 성장 기회 부족 또는 육성 제도 형평성 논란.","prob":50,"effective":"통과 다음 시즌부터.","transition":"기존 계약·등록 상태 유지, 시행일 이후 신청부터 적용.","impact":"콜업·퓨처스 운영 유연성 증가, 1군 등록 경쟁 변화.","implementation":"선수 신분, 전환 조건, 등록 가능 시점과 제한을 정의한다."},
    {"id":"R-15","name":"FA 등급 및 보상 규정 변경","category":"FA·계약","change":"FA 등급 산정 또는 원소속 구단 보상 기준 중 하나를 변경한다. 기본 후보는 보호 선수 범위 또는 보상금 기준 조정.","trigger":"FA 시장 과열 또는 보상 부담 논쟁.","prob":45,"effective":"통과 다음 FA 시장부터.","transition":"이미 협상이 시작된 FA 계약에는 소급 적용하지 않는다.","impact":"FA 영입 비용, 보호 선수 전략, 선수 이동 가능성 변화.","implementation":"등급 산식과 보상 테이블을 규정 버전별로 보관한다."},
    {"id":"R-16","name":"샐러리캡 상한·초과 제재 변경","category":"재정·샐러리캡","change":"샐러리캡 상한을 5% 인상·인하하거나 초과 제재를 한 단계 강화·완화한다. 한 번에 하나만 변경한다.","trigger":"고액 연봉 지출 집중, 재정 격차 또는 제도의 실효성 문제.","prob":50,"effective":"통과 다음 시즌부터.","transition":"기존 다년 계약은 유지하고 새 규정 시행 시점에 제재 적용.","impact":"FA·고액 선수 영입, 연봉 협상과 장기 예산에 영향.","implementation":"상한, 산입 대상, 제재 단계를 분리해 버전 관리한다."},
    {"id":"R-17","name":"최저 연봉 인상","category":"선수 보수","change":"선수 최저 연봉을 기존 기준에서 10% 인상한다.","trigger":"선수 처우 개선 요구 또는 리그 수익 증가.","prob":60,"effective":"통과 다음 시즌부터.","transition":"기존 계약이 새 최저 기준보다 낮으면 시행 시즌부터 기준액까지 보정한다.","impact":"저연봉 선수 소득 증가, 선수단 규모가 큰 구단의 인건비 부담 증가.","implementation":"최저 기준과 계약 연도, 보정 비용을 재정 계산에 반영한다."},
    {"id":"R-18","name":"저연봉 선수 기용 인센티브","category":"경쟁 균형·선수 기용","change":"저연봉 선수 또는 신인이 출전 기준을 충족하면 구단에 육성 보너스 또는 리그 지원금을 지급한다. 감독에게 출전을 강제하지 않는다.","trigger":"신인 출전 비중 감소 또는 고액 연봉자 중심 기용.","prob":35,"effective":"통과 다음 시즌부터.","transition":"대상과 출전 기준을 시즌 시작 전에 공지하며 단순 등록만으로 보상하지 않는다.","impact":"신인 육성 유인 증가. 기준 맞추기용 형식적 출전 위험.","implementation":"연봉 구간, 신인 자격, 출전 기준, 보상 상한과 지급 주체를 정의한다."}
]

# ==============================
# 연도별 KBO 규정 데이터 (규칙서 18. 규정은 코드에 하드코딩하지 않고 데이터로 관리)
# 값은 2026년 KBO 규정 공개 자료 기준이며, 실제 공식 규정집과 대조 검증이 필요하다(규칙서 '검증 필요').
# 시즌별 항목이 없으면 가장 가까운 이전 연도의 값을 사용한다. 게임 내 가상 개정안(R-xx)은
# 이 기준값 위에 효과를 덧씌우는 방식으로 적용한다.
# ==============================
KBO_RULE_DATA = {
    2026: {
        "roster_total": 68,            # 소속선수 정원
        "active_register": 29,         # 1군 등록 인원
        "active_play": 28,             # 1군 경기 출전 가능 인원
        "expanded_from_month": 9,      # 확대 엔트리 시작 월 (9월 1일)
        "expanded_register_bonus": 5,  # 확대 엔트리 추가 인원 (검증 필요)
        "doubleheader_bonus": 2,       # 더블헤더 특별 엔트리
        "foreign_general": 3,          # 일반 외국인 보유
        "asia_quota": 1,               # 아시아쿼터 보유
        "min_salary": 3000,            # 최저 연봉(만원)
        "salary_cap": 1372874,         # 경쟁균형세 상한(만원), 상위 40명 연봉 합산 (검증 필요)
        "salary_cap_top_n": 40,
        "cap_penalty_rates": [0.5, 1.0, 1.5],  # 1회/2회 연속/3회 연속 초과 시 초과분 대비 벌금 비율
        "fa_service_years": 8,         # FA 자격 누적 등록 시즌 (고졸 기준, 대졸 7)
        "fa_window_days": 21,          # 게임 내 FA 시장 기간(기본안)
        "draft_rounds": 11,            # 신인 드래프트 라운드
        "draft_pool_size": 150,
        "rookie_contract_years": 3,    # 게임 내 신인 계약 기간(기본안)
        "postseason_teams": 5,
        "regular_games": 144,
        "games_per_opponent": 16,
        "rule_event_probability": 0.30,  # 오프시즌 규정 개정안 등장 확률(규칙서 15.2 초기 밸런스)
        "rain_out_probability": 0.012,   # 경기당 우천 취소 확률(기본안)
        "doubleheader_allowed_months": (4, 5),  # 더블헤더 편성 가능 월 (2026: 4월 12일~5월 31일)
    }
}

def rule_data(season, key):
    """해당 시즌 규정값. 없으면 가장 가까운 이전 연도 값을 쓴다."""
    season = int(season)
    years = sorted(KBO_RULE_DATA)
    chosen = years[0]
    for y in years:
        if y <= season:
            chosen = y
    return KBO_RULE_DATA[chosen][key]

# 규칙서 2. '선수 이름은 실제 선수와 구별되도록 유사한 이름으로 변형' (확정).
# True로 바꾸면 실명을 그대로 사용한다. 기본값은 규칙서를 따른다.
USE_REAL_PLAYER_NAMES = False

STATE_DEFAULT = {
    "season": 2027,
    "event_history": [],
    "active_rules": [],
    "next_event_id": 1,
    "last_event_season": None
}

def load_state():
    # Rule proposals/history are scoped to the active user's game save, not shared globally.
    game = get_game() if session.get("user_id") else None
    if game and isinstance(game.get("rule_state"), dict):
        state = game["rule_state"]
        for key, value in STATE_DEFAULT.items():
            state.setdefault(key, value.copy() if isinstance(value, list) else value)
        state["season"] = game.get("season", state.get("season", 2027))
        return state
    if not os.path.exists(STATE_FILE):
        save_state(STATE_DEFAULT.copy())
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        state = STATE_DEFAULT.copy()
        save_state(state)
    for key, value in STATE_DEFAULT.items():
        state.setdefault(key, value.copy() if isinstance(value, list) else value)
    return state

def save_state(state):
    game = get_game() if session.get("user_id") else None
    if game:
        game["rule_state"] = state
        game["rule_state"]["season"] = game.get("season", state.get("season", 2027))
        save_game(game)
        return
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp, STATE_FILE)

def rule_by_id(rule_id):
    return next((r for r in RULES if r["id"] == rule_id), None)

def eligible_rules(state):
    active_ids = {r["rule_id"] for r in state["active_rules"]}
    recently_proposed = set()
    for event in state["event_history"][-3:]:
        for item in event.get("proposals", []):
            recently_proposed.add(item["rule_id"])
    return [r for r in RULES if r["id"] not in active_ids and r["id"] not in recently_proposed]

def calc_pass_probability(rule, stance, state):
    p = rule["prob"]
    if stance == "찬성":
        p += 8
    elif stance == "반대":
        p -= 8
    # Bound within agreed 25–75% range.
    return max(25, min(75, p))


# ==============================
# Dynasty game engine (playable MVP)
# ==============================
TEAM_INFO = {
    "LG 트윈스": {"abbr":"LG", "strength":78, "budget":525000, "fans":88, "color":"#c8102e"},
    "KT 위즈": {"abbr":"KT", "strength":74, "budget":450000, "fans":68, "color":"#111111"},
    "SSG 랜더스": {"abbr":"SSG", "strength":76, "budget":490000, "fans":75, "color":"#ce0e2d"},
    "NC 다이노스": {"abbr":"NC", "strength":72, "budget":410000, "fans":63, "color":"#1d467d"},
    "두산 베어스": {"abbr":"두산", "strength":75, "budget":450000, "fans":78, "color":"#131230"},
    "KIA 타이거즈": {"abbr":"KIA", "strength":79, "budget":510000, "fans":90, "color":"#ea0029"},
    "롯데 자이언츠": {"abbr":"롯데", "strength":70, "budget":430000, "fans":86, "color":"#041e42"},
    "삼성 라이온즈": {"abbr":"삼성", "strength":73, "budget":460000, "fans":80, "color":"#074ca1"},
    "한화 이글스": {"abbr":"한화", "strength":68, "budget":400000, "fans":77, "color":"#ff6600"},
    "키움 히어로즈": {"abbr":"키움", "strength":66, "budget":360000, "fans":55, "color":"#820024"},
}
POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]

# 2026 KBO 선수 실명 기반 초기 로스터.
# 첫 28명은 실명 선수, 29~36번째 육성/신인 슬롯만 가상 선수로 생성한다.
# 실제 소속은 2026 KBO 공시를 기준으로 정기 점검이 필요하다.
REAL_ROSTERS = {
    "LG 트윈스": [
        ("임찬규","SP"),("손주영","SP"),("치리노스","SP"),("톨허스트","SP"),("김윤식","SP"),
        ("송승기","SP"),("유영찬","CP"),("김진성","RP"),("이민호","RP"),("이정용","RP"),("김영우","RP"),("함덕주","RP"),
        ("박동원","C"),("이주헌","C"),("오지환","SS"),("신민재","2B"),("문보경","3B"),("오스틴","1B"),
        ("홍창기","RF"),("박해민","CF"),("문성주","LF"),("송찬의","DH"),("구본혁","2B"),("문정빈","1B"),
        ("이재원","RF"),("천성호","2B"),("김현수","DH"),("고우석","RP")],
    "KT 위즈": [
        ("고영표","SP"),("소형준","SP"),("배제성","SP"),("오원석","SP"),("보쉴리","SP"),
        ("대니엘","SP"),("박영현","CP"),("주권","RP"),("김민수","RP"),("우규민","RP"),("로건","RP"),("사우어","RP"),
        ("장성우","C"),("한승택","C"),("황재균","3B"),("허경민","3B"),("김상수","SS"),("오윤석","2B"),
        ("안현민","RF"),("배정대","CF"),("김민혁","LF"),("장진혁","CF"),("힐리어드","1B"),("문상철","DH"),
        ("권동진","2B"),("장준원","SS"),("류현인","3B"),("조대현","C")],
    "SSG 랜더스": [
        ("화이트","SP"),("아빌라","SP"),("문승원","SP"),("노경은","SP"),("김민준","SP"),
        ("해치","SP"),("조병현","CP"),("서진용","RP"),("김택형","RP"),("이로운","RP"),("장지훈","RP"),("최민준","RP"),
        ("조형우","C"),("이지영","C"),("최정","3B"),("박성한","SS"),("정준재","2B"),("고명준","1B"),
        ("최지훈","CF"),("에레디아","LF"),("한유섬","RF"),("추신수","DH"),("김성현","2B"),("박지환","SS"),
        ("오태곤","1B"),("김민식","C"),("류효승","LF"),("한두솔","RP")],
    "NC 다이노스": [
        ("이재학","SP"),("구창모","SP"),("신민혁","SP"),("라일리","SP"),("테일러","SP"),
        ("토다","SP"),("이용찬","CP"),("김영규","RP"),("임정호","RP"),("류진욱","RP"),("송명기","RP"),("하준영","RP"),
        ("김형준","C"),("안중열","C"),("박민우","2B"),("김주원","SS"),("서호철","3B"),("김휘집","3B"),
        ("박건우","RF"),("권희동","LF"),("천재환","CF"),("데이비슨","1B"),("최정원","2B"),("도태훈","1B"),
        ("김정호","C"),("김태경","RP"),("신영우","SP"),("허윤","SS")],
    "두산 베어스": [
        ("곽빈","SP"),("최원준","SP"),("잭로그","SP"),("벤자민","SP"),("플렉센","SP"),
        ("최민석","SP"),("김택연","CP"),("김유성","RP"),("이영하","RP"),("최승용","RP"),("박치국","RP"),("김명신","RP"),
        ("양의지","C"),("김기연","C"),("강승호","2B"),("박계범","SS"),("안재석","SS"),("양석환","1B"),
        ("정수빈","CF"),("조수행","LF"),("김민석","RF"),("카메론","DH"),("오명진","2B"),("박준순","3B"),
        ("김인태","RF"),("이유찬","2B"),("김정우","RP"),("세베리노","1B")],
    "KIA 타이거즈": [
        ("양현종","SP"),("이의리","SP"),("네일","SP"),("올러","SP"),("시라카와","SP"),
        ("이태양","SP"),("정해영","CP"),("전상현","RP"),("곽도규","RP"),("성영탁","RP"),("황동하","RP"),("김범수","RP"),
        ("김태군","C"),("한준수","C"),("김선빈","2B"),("김도영","3B"),("박찬호","SS"),("이호연","1B"),
        ("나성범","RF"),("김호령","CF"),("카스트로","LF"),("아데를린","DH"),("박재현","CF"),("박상준","1B"),
        ("이창진","LF"),("박정우","RF"),("한승연","DH"),("김민규","2B")],
    "롯데 자이언츠": [
        ("박세웅","SP"),("나균안","SP"),("김진욱","SP"),("로드리게스","SP"),("비슬리","SP"),
        ("정현수","SP"),("김원중","CP"),("최준용","RP"),("구승민","RP"),("이승헌","RP"),("홍민기","RP"),("이진하","RP"),
        ("유강남","C"),("손성빈","C"),("한동희","3B"),("전민재","SS"),("고승민","2B"),("나승엽","1B"),
        ("전준우","LF"),("황성빈","CF"),("레이예스","RF"),("손호영","DH"),("박승욱","2B"),("한태양","SS"),
        ("박찬형","3B"),("김강현","RP"),("박정민","SP"),("박재엽","C")],
    "삼성 라이온즈": [
        ("원태인","SP"),("백정현","SP"),("후라도","SP"),("최원태","SP"),("페덱","SP"),
        ("오러클린","SP"),("김재윤","CP"),("최지광","RP"),("이승현","RP"),("양창섭","RP"),("배찬승","RP"),("장찬희","RP"),
        ("강민호","C"),("김재성","C"),("이재현","SS"),("류지혁","2B"),("김영웅","3B"),("디아즈","1B"),
        ("구자욱","LF"),("김지찬","CF"),("김성윤","RF"),("최형우","DH"),("김현준","CF"),("박승규","RF"),
        ("전병우","1B"),("이재희","SP"),("이병헌","C"),("김태훈","RP")],
    "한화 이글스": [
        ("류현진","SP"),("문동주","SP"),("엄상백","SP"),("왕옌청","SP"),("에르난데스","SP"),
        ("쿠싱","SP"),("주현상","CP"),("박상원","RP"),("강재민","RP"),("김서현","RP"),("이민우","RP"),("윤산흠","RP"),
        ("최재훈","C"),("허인서","C"),("노시환","3B"),("심우준","SS"),("정은원","2B"),("채은성","1B"),
        ("문현빈","CF"),("페라자","RF"),("김태연","LF"),("강백호","DH"),("황영묵","2B"),("최인호","LF"),
        ("이진영","RF"),("장규현","C"),("유민","CF"),("황준서","SP")],
    "키움 히어로즈": [
        ("안우진","SP"),("정현우","SP"),("김윤하","SP"),("알칸타라","SP"),("와일스","SP"),
        ("브룩스","SP"),("주승우","CP"),("김성민","RP"),("김재웅","RP"),("원종현","RP"),("김인범","RP"),("정찬헌","RP"),
        ("김건희","C"),("김동헌","C"),("안치홍","2B"),("송성문","3B"),("김웅빈","1B"),("최주환","DH"),
        ("이주형","CF"),("이형종","RF"),("이용규","LF"),("임지열","DH"),("최재영","2B"),("어준서","SS"),
        ("박수종","CF"),("주성원","RF"),("박한결","LF"),("김지석","SS")]
}
NAME_A = ["강", "김", "박", "이", "최", "정", "조", "윤", "장", "임", "한", "오", "신", "서", "권", "황", "안", "송", "전", "문", "배", "유", "남", "백", "고", "차", "노", "주", "하", "민"]
NAME_B = ["도윤", "시우", "준서", "하준", "지호", "예준", "현우", "우진", "건우", "서준", "민재", "지훈", "태윤", "성민", "준혁", "승현", "정우", "동현", "재윤", "유찬", "민규", "승우", "지환", "태민", "현준", "수현", "은찬", "시온", "주원", "정민"]
GAME_DIR = os.path.join(DATA_DIR, "games")
os.makedirs(GAME_DIR, exist_ok=True)

def game_path(game_id):
    return os.path.join(GAME_DIR, f"{game_id}.json")

def save_game(game):
    path = game_path(game["game_id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(game, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def get_game():
    # The database, not a browser-supplied session value, determines save ownership.
    game_id = user_game_id(session.get("user_id"))
    if not game_id:
        session.pop("game_id", None)
        return None
    session["game_id"] = game_id
    try:
        with open(game_path(game_id), encoding="utf-8") as f:
            game = json.load(f)
        # Existing saves: replace old generated names with the corresponding real roster names
        # while preserving player IDs, ratings, contracts, stats, injuries, and history.
        if not game.get("real_names_migrated_v1"):
            changed = False
            for team, roster in game.get("rosters", {}).items():
                real_list = REAL_ROSTERS.get(team, [])
                for idx, player in enumerate(roster):
                    if idx < len(real_list) and idx < 28:
                        real_name, real_pos = real_list[idx]
                        shown = real_name if USE_REAL_PLAYER_NAMES else display_name_for(real_name)
                        if player.get("name") != shown:
                            player["name"] = shown
                            changed = True
                        # Keep the actual listed position aligned with the roster source.
                        player["position"] = real_pos
            game["real_names_migrated_v1"] = True
            if changed:
                save_game(game)
        # One-time migration only. Never auto-refill a roster after the user releases players.
        if not game.get("roster_capacity_migrated_v1"):
            expanded = False
            prior_releases = any(p.get("release_reason") for p in game.get("free_agents", []))
            for club, roster in game.get("rosters", {}).items():
                while len(roster) < 68 and not prior_releases:
                    idx = len(roster)
                    roster.append(new_player(club, idx, TEAM_INFO.get(club, {}).get("strength", 68), future=True))
                    expanded = True
            game["roster_capacity_migrated_v1"] = True
            if expanded:
                for club, roster in game.get("rosters", {}).items():
                    if club in game.get("finances", {}):
                        game["finances"][club]["payroll"] = sum(int(p.get("salary", 0)) for p in roster)
                game.setdefault("reports", []).insert(0, "선수단 규정 업데이트: 구단별 소속선수 정원을 최대 68명으로 설정했습니다.")
            save_game(game)
        if not game.get("migrated_v031"):
            migrate_v031(game)
            save_game(game)
        return game
    except (OSError, json.JSONDecodeError):
        return None

def migrate_v031(game):
    """v0.31 세이브 보정: 선수 이름 변형, 누적 등록 시즌 추정, 누락 필드 기본값."""
    real_names = {name for roster in REAL_ROSTERS.values() for name, _ in roster}
    for club, roster in game.get("rosters", {}).items():
        for p in roster:
            if not USE_REAL_PLAYER_NAMES and p.get("name") in real_names:
                p["name"] = display_name_for(p["name"])
            if not p.get("seasons_registered") and p.get("status") == "1군":
                p["seasons_registered"] = max(0, int(p.get("age", 25)) - 20 - random.randint(0, 2))
            p.setdefault("stats", {"G":0,"AB":0,"H":0,"HR":0,"RBI":0,"W":0,"IP":0.0,"ER":0,"SO":0})
    for p in game.get("free_agents", []):
        if not USE_REAL_PLAYER_NAMES and p.get("name") in real_names:
            p["name"] = display_name_for(p["name"])
    if game.get("fan_interest", 0) <= 35 and game.get("team") in TEAM_INFO:
        game["fan_interest"] = TEAM_INFO[game["team"]]["fans"]
    for club in game.get("teams", []):
        fin = game.setdefault("finances", {}).setdefault(club, {})
        fin.setdefault("ledger", {})
        fin.setdefault("budget", TEAM_INFO.get(club, {}).get("budget", 400000))
        fin.setdefault("transactions", [])
        diff = int(fin.get("cash", 0)) - sum(int(t.get("amount", 0)) for t in fin["transactions"])
        if diff:
            # 구버전 세이브는 일부 거래가 장부에 없었다. 보정 거래를 넣어 장부로 현금 잔액을 재현할 수 있게 한다 (규칙서 10.1).
            fin["transactions"].append({"date": f"{game.get('season', 2027)}-01-01", "type": "장부 보정", "amount": diff, "note": "v0.31 업데이트: 구버전 미기록 거래 합계 보정"})
        fin["payroll"] = sum(int(p.get("salary", 0)) for p in game.get("rosters", {}).get(club, []))
    for s_ in list(game.get("staff", {}).values()) + list(game.get("staff_pool", [])):
        if int(s_.get("salary", 0)) < 1500:
            s_["salary"] = int(s_.get("salary", 200)) * 20   # 구버전 소액 단위 → 만원 단위 재조정
    game.setdefault("released_players", [])
    game.setdefault("retired_players", [])
    game.setdefault("hall_of_fame", [])
    game.setdefault("career_records", {})
    game.setdefault("postponed_games", [])
    game.setdefault("cap_violations", {})
    game["migrated_v031"] = True

def potential_gap(age):
    """잠재력 = 현재 능력 + 여유분. 대부분은 작고 소수만 크다(지수 분포). 나이가 많을수록 여유분이 작다."""
    gap = int(random.expovariate(1/5.0))
    if age >= 27: gap = min(gap, 6)
    if age >= 30: gap = min(gap, 2)
    return min(18, gap)

def new_player(team, idx, strength, future=False):
    seed = random.randint(1000, 9999)
    real_list = REAL_ROSTERS.get(team, [])
    is_real = idx < len(real_list) and not future
    if is_real:
        name, pos = real_list[idx]
        if not USE_REAL_PLAYER_NAMES:
            name = display_name_for(name)
    else:
        # 신규 육성 선수·신인은 실제 KBO 선수와 겹치지 않는 가상 이름으로 생성
        name = make_fictional_name(team, seed, idx)
        if idx < 12:
            pos = "SP" if idx < 5 else ("CP" if idx == 5 else "RP")
        else:
            pos = POSITIONS[(idx - 12) % len(POSITIONS)]
    pitcher = pos in ("SP", "RP", "CP")
    age = random.randint(19, 23) if future else (random.randint(20, 35) if is_real else random.randint(22, 36))
    base = strength + random.randint(-17, 15) - (5 if age < 21 else 0) - (max(0, age-32)*2)
    # 퓨처스는 1군보다 평균적으로 낮게: 신인/육성 선수의 과도한 OVR 방지
    if future:
        base -= 13
    ovr = max(35 if future else 42, min(91, base + random.randint(-5, 5)))
    skill_names = (["구위", "제구", "구종", "체력", "위기관리"] if pitcher
                   else ["컨택", "장타", "선구", "주루", "수비", "송구"])
    skills = {key: max(0, min(100, ovr + random.randint(-16, 16))) for key in skill_names}
    min_salary = rule_data(2026, "min_salary")
    # 1군 실명 기반 선수는 나이에 따라 누적 등록 시즌을 추정한다 (FA 자격 판정에 사용).
    seasons_registered = 0 if future else max(0, age - 20 - random.randint(0, 2))
    player = {
        "id": f"P{seed}{idx}{random.randint(10,99)}", "name":name, "team":team, "nationality":"대한민국", "is_foreign":False, "foreign_type":None,
        "position":pos, "age":age, "ovr":ovr, "skills":skills,
        "potential":max(ovr, min(96, ovr+potential_gap(age))),
        "development_report":"스카우트 평가 필요", "seasons_registered":seasons_registered,
        "salary":random.randint(min_salary, 9000) if age < 29 else random.randint(7000, 28000),
        "status":"퓨처스" if future else "1군", "condition":random.randint(65,100), "fatigue":random.randint(0,20),
        "injury":None, "injury_days":0, "rehab_score":0, "morale":random.randint(55,85),
        "contract_years":random.randint(1,4), "contract":{"years":None,"salary_by_year":[],"guaranteed":True,"bonuses":[],"team_option":False,"player_option":False},
        "stats":{"G":0,"AB":0,"H":0,"HR":0,"RBI":0,"W":0,"IP":0.0,"ER":0,"SO":0}
    }
    return player

FICTIONAL_GIVEN_NAMES = ["도윤","시우","준서","하준","지호","예준","현우","우진","건우","서준","민재","지훈","태윤","성민","준혁","승현","정우","동현","재윤","유찬","민규","승우","지환","태민","현준","수현","은찬","시온","주원","정민","재민","윤호","승민","찬우","민호","지후"]
FICTIONAL_FAMILY_NAMES = ["강","김","박","이","최","정","조","윤","장","임","한","오","신","서","권","황","안","송","전","문","배","유","남","백","고","차","노","주","하","민"]

KOREAN_FAMILY_NAMES = set(FICTIONAL_FAMILY_NAMES) | {"류","양","원","구","허","곽","손","나","염","홍","천","도","엄","채","심","변","설","표","진","탁","추","함","성","육","봉"}
# 모음 치환표(자연스러운 음절만 나오도록 흔한 모음끼리 짝지음): ㅏ↔ㅓ, ㅗ↔ㅜ, ㅐ↔ㅔ, ㅑ↔ㅕ, ㅛ↔ㅠ, ㅡ→ㅜ, ㅣ→ㅔ 등
_VOWEL_SHIFT = {0:4, 4:0, 8:13, 13:8, 18:13, 20:5, 1:5, 5:1, 2:6, 6:2, 12:17, 17:12, 3:7, 7:3, 9:0, 10:5, 11:8, 14:4, 15:5, 16:20, 19:20}

def _shift_syllable_vowel(ch):
    code = ord(ch)
    if not (0xAC00 <= code <= 0xD7A3):
        return ch
    base = code - 0xAC00
    cho, jung, jong = base // 588, (base % 588) // 28, base % 28
    jung = _VOWEL_SHIFT.get(jung, (jung + 1) % 21)
    return chr(0xAC00 + cho * 588 + jung * 28 + jong)

def display_name_for(real_name):
    """규칙서 2(확정): 실명을 그대로 쓰지 않고 구별 가능한 유사 이름으로 변형한다.
    한국 이름은 성(姓)을 바꾸고, 외국인 이름은 첫 음절 모음을 바꾼다. 같은 실명은 항상 같은 변형 이름이 된다."""
    import hashlib
    taken = {name for roster in REAL_ROSTERS.values() for name, _ in roster}
    digest = int(hashlib.md5(real_name.encode("utf-8")).hexdigest(), 16)
    if 2 <= len(real_name) <= 3 and real_name[0] in KOREAN_FAMILY_NAMES:
        families = [f for f in FICTIONAL_FAMILY_NAMES if f != real_name[0] and f != real_name[1]]
        for k in range(len(families)):
            candidate = families[(digest + k) % len(families)] + real_name[1:]
            if candidate not in taken:
                return candidate
    # 외국인 이름: 2음절은 끝 음절, 3음절 이상은 첫 음절의 모음을 바꾼다.
    if len(real_name) <= 2:
        candidate = real_name[:-1] + _shift_syllable_vowel(real_name[-1])
    else:
        candidate = _shift_syllable_vowel(real_name[0]) + real_name[1:]
    if candidate in taken or candidate == real_name:
        candidate = real_name[0] + _shift_syllable_vowel(real_name[1]) + real_name[2:] if len(real_name) > 1 else candidate + "리"
    return candidate

def make_fictional_name(team, seed, idx):
    # Deterministic within the generated player ID, and avoid using the current KBO roster names.
    taken = {name for roster in REAL_ROSTERS.values() for name, _ in roster}
    rng = random.Random(f"{team}-{seed}-{idx}")
    for _ in range(200):
        candidate = rng.choice(FICTIONAL_FAMILY_NAMES) + rng.choice(FICTIONAL_GIVEN_NAMES)
        if candidate not in taken:
            return candidate
    return f"신인{seed}"

def build_schedule(teams, season=2027):
    """규칙서 4(확정): 구단당 144경기, 상대별 16경기(홈 8·원정 8), 홈·원정 시리즈, 월요일 휴식.
    KBO 편성 원칙을 따라 9개 상대와 3연전 4회 + 2연전 2회(= 16경기)를 치른다.
    화~목 / 금~일 3연전이 기본 틀이며, 2연전 뒤에는 이동·휴식일을 둔다."""
    wheel = list(teams)
    base_rounds = []
    for rnd in range(9):
        pairings = []
        for i in range(5):
            a, b = wheel[i], wheel[9-i]
            home, away = (a, b) if (rnd+i) % 2 == 0 else (b, a)
            pairings.append((home, away))
        base_rounds.append(pairings)
        wheel = [wheel[0], wheel[-1], *wheel[1:-1]]

    # 6 cycles over the 9 pairing rounds: cycles 0-3 are 3-game series, cycles 4-5 are 2-game series.
    # Home/away flips every cycle, so each pairing gets home series of 3+3+2 and away series of 3+3+2.
    rng = random.Random(season)
    series_plan = []
    for cycle in range(6):
        order = list(range(9))
        rng.shuffle(order)
        for rnd in order:
            series_plan.append((rnd, 3 if cycle < 4 else 2, cycle % 2 == 1))

    schedule = []
    # 개막은 3월 마지막 주 금요일 전후로 맞춘다 (실제 개막일은 연도별 검증 필요).
    current_date = datetime(season, 3, 26)
    while current_date.weekday() != 4:
        current_date += timedelta(days=1)
    for series_no, (rnd, length, flipped) in enumerate(series_plan, start=1):
        for game_no in range(1, length + 1):
            while current_date.weekday() == 0:  # Monday league-wide rest day
                current_date += timedelta(days=1)
            fixtures = []
            for home, away in base_rounds[rnd]:
                if flipped:
                    home, away = away, home
                fixtures.append({"home": home, "away": away, "played": False,
                                 "series_no": series_no, "game_no": game_no})
            schedule.append({"date": current_date.strftime("%Y-%m-%d"),
                             "series_no": series_no, "game_no": game_no, "games": fixtures})
            current_date += timedelta(days=1)
        if length == 2:
            current_date += timedelta(days=1)  # travel/rest day keeps the Tue-Thu / Fri-Sun rhythm
    return schedule

def team_games_played(game, team):
    st = game["standings"].get(team, {})
    return int(st.get("W", 0)) + int(st.get("L", 0)) + int(st.get("D", 0))

def regular_season_complete(game):
    """모든 구단이 정규시즌 경기 수를 채웠는지 (연기 경기 포함)."""
    target = rule_data(game.get("season", 2027), "regular_games")
    return game.get("day_index", 0) >= len(game.get("schedule", [])) and all(team_games_played(game, t) >= target for t in game.get("teams", []))

def create_game(team):
    game_id=uuid.uuid4().hex[:20]
    teams=list(TEAM_INFO)
    rosters={}
    standings={}
    finances={}
    for name,info in TEAM_INFO.items():
        roster=[]
        for i in range(68):
            # KBO 2026 affiliated-player ceiling: 68 players per club.
            # Publicly sourced real names are used where verified; remaining development slots are game-generated.
            roster.append(new_player(name, i, info["strength"], future=(i>=len(REAL_ROSTERS.get(name, [])))))
        rosters[name]=roster
        standings[name]={"W":0,"L":0,"D":0,"RS":0,"RA":0,"streak":"-"}
        finances[name]={"budget":info["budget"],"cash":info["budget"],"payroll":sum(x["salary"] for x in roster),"ledger":{},"transactions":[{"date":"2027-01-01","type":"기초 예산","amount":info["budget"],"note":"시즌 시작 운영 자금"}]}
    schedule=build_schedule(teams, 2027)
    staff = {}
    for role, base_skill, salary in STAFF_ROLE_BASELINE:
        staff[role] = {"id": uuid.uuid4().hex[:12], "role":role, "name":random.choice(REAL_STAFF_BY_ROLE.get(role, ["김용희","김민호","정경배","조인성"])), "grade":staff_grade(base_skill), "skills":{"전문성":base_skill,"리더십":max(40,min(95,base_skill+random.randint(-10,10))),"협업":random.randint(45,90)}, "salary":salary, "contract_years":random.randint(1,3), "age":random.randint(35,60), "health":random.randint(75,100), "career_seasons":random.randint(2,20)}
    game={"schema_version":3,"ruleset_version":"Dynasty-2026-v0.5","game_id":game_id,"team":team,"season":2027,"week":0,"day_index":0,"teams":teams,"rosters":rosters,"standings":standings,"finances":finances,"schedule":schedule,"game_log":[],"season_history":[],"phase":"정규시즌","fa_days_left":0,"trade_offers":[],"next_trade_id":1,"reports":["2027시즌 단장 업무가 시작되었습니다. 선수단과 예산을 확인하세요."],"free_agents":[],"fa_offers":[],"fa_market_generated":False,"staff":staff,"rule_state":{"season":2027,"event_history":[],"active_rules":[],"next_event_id":1,"last_event_season":None},"processed_week_ids":[],"ledger_version":2,"event_ids_processed":[],"weekly_reports":[],"career_records":{},"club_history":[],"fan_interest":TEAM_INFO[team]["fans"],"team_morale":65,"manager_rating":70,"owner_trust":65,"owner_bad_reviews":0,"manager_name":"초대 단장","manager_start_season":2027,"manager_history":[],"manager_fired":False,"injury_list":[],"draft_pool":[],"postseason":None,"season_budget_snapshot":{},"foreign_pool":[],"foreign_market_generated":False,"foreign_signings":[],"retired_players":[],"hall_of_fame":[],"postponed_games":[],"cap_violations":{},"migrated_v031":True,"real_names_migrated_v1":True,"roster_capacity_migrated_v1":True,"released_players":[]}
    # 구단별 초기 1군 등록 인원을 규정 한도에 맞춘다.
    for club in teams:
        ai_manage_roster(game, club, fill_only=True)
    # FA 시장은 기존 선수 계약 만료로만 구성한다. 임의 생성 FA는 만들지 않는다.
    # 단장 단톡방에 게임 시작 시 타 구단의 초기 트레이드 제안을 생성한다.
    create_initial_trade_offers(game)
    save_game(game)
    return game

def find_player(game, team, player_id):
    return next((p for p in game["rosters"].get(team, []) if p["id"] == player_id), None)

def add_trade_message(offer, speaker, message):
    offer.setdefault("thread", []).append({"speaker": speaker, "message": message,
        "time": datetime.now().strftime("%m-%d %H:%M")})

def create_initial_trade_offers(game):
    user_team = game["team"]
    rivals = [t for t in game["teams"] if t != user_team]
    random.shuffle(rivals)
    for rival in rivals[:4]:
        ours = [p for p in game["rosters"][user_team] if p["status"] == "1군" and not p.get("injury")]
        theirs = [p for p in game["rosters"][rival] if p["status"] == "1군" and not p.get("injury")]
        if not ours or not theirs:
            continue
        # Similar-value offers feel more plausible than random superstar swaps.
        their_player = random.choice(theirs)
        candidates = sorted(ours, key=lambda p: abs(p["ovr"] - their_player["ovr"]))[:min(5, len(ours))]
        our_player = random.choice(candidates)
        offer = {"id": game["next_trade_id"], "from_team": rival, "to_team": user_team,
            "give_player_ids": [their_player["id"]], "want_player_ids": [our_player["id"]],
            "give_player_names": [their_player["name"]], "want_player_names": [our_player["name"]],
            "status": "pending", "kind": "incoming", "created_week": 0,
            "thread": []}
        game["next_trade_id"] += 1
        add_trade_message(offer, rival + " 단장", f"{their_player['name']} 선수와 {our_player['name']} 선수의 1:1 트레이드를 제안합니다. 전력 보강 차원에서 검토 부탁드립니다.")
        game["trade_offers"].append(offer)

def trade_players(game, offer):
    from_team, to_team = offer["from_team"], offer["to_team"]
    giving = [find_player(game, from_team, pid) for pid in offer["give_player_ids"]]
    receiving = [find_player(game, to_team, pid) for pid in offer["want_player_ids"]]
    if not giving or not receiving or any(p is None for p in giving + receiving):
        return False, "트레이드 대상 선수를 찾을 수 없어 거래를 취소했습니다."
    for player in giving:
        game["rosters"][from_team].remove(player)
        game["rosters"][to_team].append(player)
        player["team"] = to_team
    for player in receiving:
        game["rosters"][to_team].remove(player)
        game["rosters"][from_team].append(player)
        player["team"] = from_team
    total_limit = roster_total_limit(game)
    if len(game["rosters"][from_team]) > total_limit or len(game["rosters"][to_team]) > total_limit:
        # 되돌리기
        for player in giving:
            game["rosters"][to_team].remove(player); game["rosters"][from_team].append(player); player["team"] = from_team
        for player in receiving:
            game["rosters"][from_team].remove(player); game["rosters"][to_team].append(player); player["team"] = to_team
        return False, f"소속선수 정원({total_limit}명)을 초과해 거래를 취소했습니다."
    recalc_payroll(game, from_team); recalc_payroll(game, to_team)
    names_out = ", ".join(p["name"] for p in giving)
    names_in = ", ".join(p["name"] for p in receiving)
    for team, note in ((from_team, f"트레이드: {names_in} 영입 / {names_out} 이적"), (to_team, f"트레이드: {names_out} 영입 / {names_in} 이적")):
        record_transaction(game, team, "트레이드", 0, note, None, "트레이드")
        enforce_active_limit(game, team, announce=(team == game.get("team")))
    game.setdefault("transfer_log", []).append({"season": game["season"], "week": game.get("week", 0), "from": from_team, "to": to_team, "out": [p["id"] for p in giving], "in": [p["id"] for p in receiving]})
    game["reports"].insert(0, f"트레이드 성사: {from_team} ↔ {to_team} · {names_out} / {names_in}")
    return True, f"트레이드 성사: {names_out} ↔ {names_in}"

def active_rule_ids(game):
    """Return rules whose effective season has arrived; keep effects data-driven."""
    state = game.get("rule_state", {})
    season = int(game.get("season", 2027))
    return {r.get("rule_id") for r in state.get("active_rules", [])
            if r.get("status") == "시행 중" and int(r.get("effective_season", 9999)) <= season}

def rule_is_active(game, rule_id):
    return rule_id in active_rule_ids(game)

def current_game_date(game):
    sched = game.get("schedule") or []
    idx = min(int(game.get("day_index", 0)), max(0, len(sched) - 1))
    if sched:
        try:
            return datetime.strptime(sched[idx]["date"], "%Y-%m-%d")
        except (KeyError, ValueError):
            pass
    return datetime(int(game.get("season", 2027)), 3, 1)

def roster_limit(game):
    """1군 등록 한도. 연도별 KBO 규정 데이터 + 확대 엔트리 + 가상 개정안(R-06) 효과."""
    season = game.get("season", 2027)
    limit = int(rule_data(season, "active_register"))
    if current_game_date(game).month >= int(rule_data(season, "expanded_from_month")):
        limit += int(rule_data(season, "expanded_register_bonus"))
    if rule_is_active(game, "R-06"):
        limit += 2
    return limit

def roster_total_limit(game):
    return int(rule_data(game.get("season", 2027), "roster_total"))

def foreign_limits(game):
    season = game.get("season", 2027)
    general = int(rule_data(season, "foreign_general"))
    asia = int(rule_data(season, "asia_quota")) + (1 if rule_is_active(game, "R-12") else 0)
    return general, asia

def min_salary(game):
    base = int(rule_data(game.get("season", 2027), "min_salary"))
    return int(base * 1.10) if rule_is_active(game, "R-17") else base

def fa_service_requirement(game):
    base = int(rule_data(game.get("season", 2027), "fa_service_years"))
    return max(1, base - 1) if rule_is_active(game, "R-07") else base

def draft_round_count(game):
    return int(rule_data(game.get("season", 2027), "draft_rounds"))

def postseason_team_count(game):
    return 6 if rule_is_active(game, "R-09") else int(rule_data(game.get("season", 2027), "postseason_teams"))

def salary_cap(game, team=None):
    """경쟁균형세(총연봉) 상한. 리그 공통 규정값이며 R-16은 ±5% 조정."""
    base = int(rule_data(game.get("season", 2027), "salary_cap"))
    if rule_is_active(game, "R-16"):
        return int(base * 1.05)
    return base

def cap_payroll(game, team):
    """총연봉 한도 산정 대상: 소속선수 중 연봉 상위 N명 합계 (외국인 포함, 신인 제외는 미반영)."""
    top_n = int(rule_data(game.get("season", 2027), "salary_cap_top_n"))
    salaries = sorted((int(p.get("salary", 0)) for p in game["rosters"].get(team, [])), reverse=True)
    return sum(salaries[:top_n])

def active_roster(game, team):
    return [p for p in game["rosters"][team] if p["status"]=="1군"]

def active_count_for_limit(game, team):
    """등록 한도 계산용 1군 인원. R-13(부상자 명단) 시행 시 부상 선수는 한도에서 제외."""
    players = active_roster(game, team)
    if rule_is_active(game, "R-13"):
        return sum(1 for p in players if not p.get("injury"))
    return len(players)

def ai_manage_roster(game, team, fill_only=False):
    """감독·코치가 1군 엔트리를 구성한다 (규칙서 3 스프링캠프·13 감독 자율성).
    AI 구단은 매주, 사용자 구단은 시즌 개막 시에만 호출된다(이후 콜업·말소는 단장 결정)."""
    roster = game["rosters"].get(team, [])
    limit = roster_limit(game)
    if not fill_only:
        for p in roster:
            if p.get("status") == "1군" and p.get("injury") and int(p.get("injury_days", 0)) > 10 and not rule_is_active(game, "R-13"):
                p["status"] = "퓨처스"
    def active(): return [p for p in roster if p.get("status") == "1군"]
    while active_count_for_limit(game, team) < limit:
        pool = [p for p in roster if p.get("status") == "퓨처스" and not p.get("injury")]
        if not pool: break
        cur = active()
        sp = sum(1 for p in cur if p["position"] == "SP"); pit = sum(1 for p in cur if p["position"] in ("SP","RP","CP")); c = sum(1 for p in cur if p["position"] == "C")
        if sp < 5 and any(p["position"] == "SP" for p in pool): pick = max((p for p in pool if p["position"] == "SP"), key=lambda p: p["ovr"])
        elif c < 2 and any(p["position"] == "C" for p in pool): pick = max((p for p in pool if p["position"] == "C"), key=lambda p: p["ovr"])
        elif pit < 13 and any(p["position"] in ("RP","CP","SP") for p in pool): pick = max((p for p in pool if p["position"] in ("RP","CP","SP")), key=lambda p: p["ovr"])
        else: pick = max(pool, key=lambda p: p["ovr"])
        pick["status"] = "1군"
    enforce_active_limit(game, team, announce=False)

def enforce_active_limit(game, team, announce=True):
    """엔트리 위반 시 OVR이 낮은 선수부터 퓨처스로 내린다 (규칙서 7: 위반 차단·경고)."""
    limit = roster_limit(game)
    demoted = []
    while active_count_for_limit(game, team) > limit:
        candidates = sorted([p for p in active_roster(game, team) if not (rule_is_active(game, "R-13") and p.get("injury"))], key=lambda p: int(p.get("ovr", 0)))
        if not candidates:
            break
        candidates[0]["status"] = "퓨처스"
        demoted.append(candidates[0]["name"])
    if demoted and announce:
        game.setdefault("reports", []).insert(0, f"엔트리 규정 적용({team}): 1군 등록 한도 {limit}명 초과로 {', '.join(demoted)} 선수를 퓨처스로 내렸습니다.")
    return demoted

def record_transaction(game, team, tx_type, amount, note, date=None, category=None):
    """규칙서 10.1·18: 모든 수입·지출은 거래 내역으로 저장하고 시즌별 장부에 분류한다."""
    fin = game["finances"][team]
    amount = int(amount)
    fin["cash"] = int(fin.get("cash", 0)) + amount
    if date is None:
        date = current_game_date(game).strftime("%Y-%m-%d")
    fin.setdefault("transactions", []).append({"date": date, "type": tx_type, "amount": amount, "note": note})
    season_key = str(game.get("season", 2027))
    ledger = fin.setdefault("ledger", {}).setdefault(season_key, {})
    cat = category or tx_type
    ledger[cat] = int(ledger.get(cat, 0)) + amount
    return amount

def recalc_payroll(game, team):
    fin = game["finances"][team]
    fin["payroll"] = sum(int(p.get("salary", 0)) for p in game["rosters"].get(team, []))
    return fin["payroll"]

def player_contract_years(p):
    return int(p.get("contract_years", 1) or 1)

def player_ovr(player): return int(player.get("ovr",50))

def staff_effect(game, team, role, key="전문성"):
    """스태프 능력 효과 (0 기준, -1.0~+1.0 범위로 정규화). 사용자 구단만 실제 스태프 데이터를 가진다."""
    if team != game.get("team"):
        return 0.0
    staff = game.get("staff", {}).get(role)
    if not staff:
        return -0.3
    value = int(staff.get("skills", {}).get(key, 60))
    return max(-1.0, min(1.0, (value - 65) / 30))

def team_strength(game, team):
    """선수단 능력(타격·투수) + 컨디션·피로·사기·부상 + 감독 전술 보정. 보정은 상한을 둬 전력 차이를 뒤집지 못한다 (규칙서 4.1·13)."""
    active = [p for p in active_roster(game, team) if not p.get("injury")]
    if not active:
        return 40.0, {"bat": 40, "pit": 40, "fatigue": 0, "morale": 65}
    batting = [p["ovr"] for p in active if p["position"] not in ("SP","RP","CP")]
    pitching = [p["ovr"] for p in active if p["position"] in ("SP","RP","CP")]
    bat = sum(batting)/max(1,len(batting)); pit = sum(pitching)/max(1,len(pitching))
    fatigue = sum(p.get("fatigue",0) for p in active)/len(active)
    condition = sum(p.get("condition",80) for p in active)/len(active)
    morale = game.get("team_morale_by_team", {}).get(team, 65)
    manager = staff_effect(game, team, "감독") * 1.5   # 감독 전술 효과 상한 ±1.5
    strength = bat*.48 + pit*.52 - fatigue*.06 + (condition-80)*.05 + (morale-65)*.035 + manager
    return strength, {"bat": round(bat,1), "pit": round(pit,1), "fatigue": round(fatigue,1), "morale": morale}

def explain_result(home, away, hs, as_, hinfo, ainfo, hrun, arun, extra):
    """규칙서 4.1: 경기 결과 로그에 주요 원인을 남긴다."""
    winner, loser = (home, away) if hrun > arun else (away, home)
    winfo, linfo = (hinfo, ainfo) if hrun > arun else (ainfo, hinfo)
    reasons = []
    if abs(hrun - arun) >= 5:
        reasons.append("타선 폭발")
    if winfo["pit"] - linfo["pit"] >= 4:
        reasons.append("선발·불펜 우위")
    if linfo["fatigue"] - winfo["fatigue"] >= 8:
        reasons.append(f"{loser} 피로 누적")
    if winfo["morale"] - linfo["morale"] >= 8:
        reasons.append(f"{winner} 팀 분위기")
    if extra:
        reasons.append("승부치기")
    if abs(hrun - arun) <= 1:
        reasons.append("접전 · 경기별 변동성")
    if not reasons:
        reasons.append("전력 차이 반영")
    return ", ".join(reasons)

def reschedule_postponed(game, fixture, rain_date):
    """우천 취소 경기 재편성 (규칙서 4 확정): 양 팀이 모두 쉬는 휴식일에 우선 편성하고,
    없으면 더블헤더(허용 기간 내) 또는 시즌 말 추가 편성일을 둔다."""
    season = int(game.get("season", 2027))
    home, away = fixture["home"], fixture["away"]
    schedule = game["schedule"]
    start = int(game.get("day_index", 0)) + 1
    makeup = {"home": home, "away": away, "played": False, "series_no": fixture.get("series_no"), "game_no": fixture.get("game_no"), "makeup": True, "postponed_from": rain_date}
    by_date = {day["date"]: i for i, day in enumerate(schedule)}
    rain_dt = datetime.strptime(rain_date, "%Y-%m-%d")
    last_date = datetime.strptime(schedule[-1]["date"], "%Y-%m-%d")
    dh_months = rule_data(season, "doubleheader_allowed_months")
    # KBO 2026 더블헤더 조건: 허용 기간 내 토요일 경기 취소 시 일요일 더블헤더
    if rain_dt.weekday() == 5 and dh_months[0] <= rain_dt.month <= dh_months[1]:
        sunday = (rain_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        idx = by_date.get(sunday)
        if idx is not None and idx >= start and any({g["home"], g["away"]} == {home, away} for g in schedule[idx]["games"]):
            makeup["doubleheader"] = True; makeup["kind"] = "더블헤더"
            schedule[idx]["games"].append(makeup)
            return makeup
    # 휴식일 우선 재편성: 양 팀이 모두 쉬는 가장 빠른 날 (월요일 제외)
    cursor = rain_dt + timedelta(days=1)
    while cursor <= last_date + timedelta(days=21):
        key = cursor.strftime("%Y-%m-%d")
        if cursor.weekday() != 0:
            if key in by_date:
                idx = by_date[key]
                if idx >= start:
                    busy = {t for g in schedule[idx]["games"] for t in (g["home"], g["away"])}
                    if home not in busy and away not in busy:
                        schedule[idx]["games"].append(makeup); makeup["kind"] = "휴식일 편성"
                        return makeup
            else:
                new_day = {"date": key, "series_no": fixture.get("series_no"), "game_no": fixture.get("game_no"), "games": [makeup], "makeup_day": True}
                insert_at = next((i for i, d in enumerate(schedule) if d["date"] > key), len(schedule))
                if insert_at >= start:
                    schedule.insert(insert_at, new_day); makeup["kind"] = "휴식일 편성" if cursor <= last_date else "시즌 말 편성"
                    return makeup
        cursor += timedelta(days=1)
    new_day = {"date": (last_date + timedelta(days=1)).strftime("%Y-%m-%d"), "series_no": None, "game_no": None, "games": [makeup], "makeup_day": True}
    schedule.append(new_day); makeup["kind"] = "시즌 말 편성"
    return makeup

def simulate_one(game, fixture):
    home,away=fixture["home"],fixture["away"]
    day_date = game["schedule"][game["day_index"]]["date"]
    hs,hinfo=team_strength(game,home); as_,ainfo=team_strength(game,away)
    hs += random.uniform(-3.0,3.0); as_ += random.uniform(-3.0,3.0)
    hrun=max(0,round(random.gauss(4.4+(hs-as_)*.045,2.1)))
    arun=max(0,round(random.gauss(4.15+(as_-hs)*.045,2.1)))
    extra=False
    if hrun==arun:
        # 연장전: R-10 승부치기 시행 시 무승부가 줄고, 아니면 일부 경기가 무승부로 끝난다.
        tie_prob = 0.03 if rule_is_active(game, "R-10") else 0.08
        if random.random() >= tie_prob:
            extra = rule_is_active(game, "R-10")
            if random.random()<.55: hrun+=1
            else: arun+=1
    for team,rs,ra in ((home,hrun,arun),(away,arun,hrun)):
        st=game["standings"][team]; st["RS"]+=rs; st["RA"]+=ra
        if rs>ra: st["W"]+=1; st["streak"]="W"
        elif rs<ra: st["L"]+=1; st["streak"]="L"
        else: st["D"]+=1; st["streak"]="D"
        roster=[p for p in active_roster(game,team) if not p.get("injury")]
        batters=[p for p in roster if p["position"] not in ("SP","RP","CP")]
        for p in batters:
            if random.random()<.88:
                p["stats"]["G"]+=1; game_ab=random.randint(2,5); p["stats"]["AB"]+=game_ab
                hit_rate=max(0.16,min(0.34,0.22+(p["ovr"]-50)*0.0016))
                hits=sum(1 for _ in range(game_ab) if random.random()<hit_rate)
                p["stats"]["H"]+=hits
                if random.random() < max(0.004,min(0.07,0.012+(p["ovr"]-50)*0.00045)):
                    p["stats"]["HR"]+=1
                if random.random()<min(0.45,rs*0.075):
                    p["stats"]["RBI"]+=random.randint(1,2)
                p["morale"]=max(30,min(95,int(p.get("morale",65))+(1 if hits>=2 else 0)))
        pitchers=[p for p in roster if p["position"] in ("SP","RP","CP")]
        starters=sorted([p for p in pitchers if p["position"]=="SP"],key=lambda x:x["ovr"],reverse=True)[:5]
        if starters:
            sp=starters[game["day_index"]%len(starters)]
            sp["stats"]["G"]+=1; sp["stats"]["IP"]+=round(random.uniform(4.0,6.2),1); sp["stats"]["ER"]+=max(0,ra-random.randint(0,2)); sp["stats"]["SO"]+=random.randint(2,8)
            sp["fatigue"]=min(100,int(sp.get("fatigue",0))+random.randint(10,18))
            if rs>ra and random.random()<0.62: sp["stats"]["W"]+=1
        for p in active_roster(game,team):
            if p["position"]=="SP" and p in starters and p is not starters[game["day_index"]%len(starters)]:
                p["fatigue"]=max(0,int(p.get("fatigue",0))-random.randint(2,4))
            else:
                p["fatigue"]=min(100,max(0,int(p.get("fatigue",0))+random.randint(1,4)-2))
            p["condition"]=max(35,min(100,int(p.get("condition",80))+random.randint(-5,4)))
            if p.get("injury"):
                p["injury_days"] = max(0, int(p.get("injury_days", 1))-1)
                p["rehab_score"] = min(100, int(p.get("rehab_score", 0))+random.randint(4,10))
                if p["injury_days"] == 0 and p["rehab_score"] >= 75:
                    p["injury"] = None; p["rehab_score"] = 0
                    p.setdefault("injury_history", []).append({"season":game["season"],"days":0})
                    if team == game.get("team"):
                        game["reports"].insert(0, f"재활 복귀: {p['name']} 선수의 회복 평가가 기준을 충족했습니다.")
            else:
                # 부상 위험: 나이, 누적 부하(피로), 부상 이력, 컨디션 (규칙서 6 확정)
                risk = 0.0025 + max(0, p["fatigue"]-55)*0.0005 + max(0, p["age"]-31)*0.0006 + len(p.get("injury_history", []))*0.0008 + max(0, 60-p["condition"])*0.0003
                if random.random() < risk:
                    p["injury"]=random.choice(["근육 피로 손상","햄스트링 염좌","팔꿈치 염증","손목 타박","발목 염좌"]); p["injury_days"]=random.randint(7,45); p["rehab_score"]=random.randint(0,20)
                    p.setdefault("injury_history", []).append({"season":game["season"],"days":p["injury_days"],"diagnosis":p["injury"]})
                    game.setdefault("injury_list",[]).append({"player_id":p["id"],"player_name":p["name"],"team":team,"diagnosis":p["injury"],"days":p["injury_days"],"status":"재활 중","season":game["season"]})
    fixture["played"]=True
    record={"date":day_date,"home":home,"away":away,"home_score":hrun,"away_score":arun,
            "reason":explain_result(home,away,hs,as_,hinfo,ainfo,hrun,arun,extra)}
    if fixture.get("makeup"): record["makeup"]=fixture.get("kind","재편성")
    game["game_log"].append(record)
    return record

def advance_week_game(game):
    if game.get("phase", "정규시즌") != "정규시즌":
        return []
    played=[]
    # 한 번의 진행 = 달력상 1주 (규칙서 4 확정). 저장 후 재실행 시 중복 처리를 막는다 (규칙서 17·18).
    week_id = f"{game['season']}-{game['week']+1}"
    if week_id in game.setdefault("processed_week_ids", []):
        return []
    rain_prob = float(rule_data(game["season"], "rain_out_probability"))
    for team in game["teams"]:
        if team != game["team"] or game.get("settings", {}).get("auto_roster", True):
            ai_manage_roster(game, team)
    if game["day_index"] < len(game["schedule"]):
        # 주는 화요일 시작~다음 월요일 종료로 본다 (KBO 주간 리듬).
        start_date = datetime.strptime(game["schedule"][game["day_index"]]["date"], "%Y-%m-%d")
        week_end = start_date + timedelta(days=((1 - start_date.weekday()) % 7) or 7)
    else:
        week_end = None
    while game["day_index"]<len(game["schedule"]):
        day=game["schedule"][game["day_index"]]
        if week_end and datetime.strptime(day["date"], "%Y-%m-%d") >= week_end:
            break
        for fixture in list(day["games"]):
            if fixture.get("played"):
                continue
            if random.random() < rain_prob and not fixture.get("makeup"):
                fixture["played"]=True; fixture["rained_out"]=True
                makeup=reschedule_postponed(game, fixture, day["date"])
                game.setdefault("postponed_games",[]).append({"date":day["date"],"home":fixture["home"],"away":fixture["away"],"rescheduled_to":makeup.get("kind"),"season":game["season"]})
                if game["team"] in (fixture["home"],fixture["away"]):
                    game["reports"].insert(0,f"우천 취소: {day['date']} {fixture['away']} @ {fixture['home']} 경기가 취소되어 {makeup.get('kind')}으로 재편성되었습니다.")
                continue
            played.append(simulate_one(game,fixture))
        game["day_index"]+=1
    game["week"]+=1
    game.setdefault("processed_week_ids", []).append(week_id)
    season_done = game["day_index"] >= len(game["schedule"])
    if season_done:
        game["phase"] = "시즌 종료"
        game["reports"].insert(0, f"{game['season']} 정규시즌 {rule_data(game['season'],'regular_games')}경기가 모두 끝났습니다. 포스트시즌 메뉴에서 플레이오프를 진행하세요.")
    # 성장·노화 (규칙서 6): 나이·잠재력·출전·코치·육성 환경 반영, 주간 변동은 소폭.
    for team in game["teams"]:
        hit_coach = staff_effect(game, team, "타격 코치"); pit_coach = staff_effect(game, team, "투수 코치"); farm = staff_effect(game, team, "육성 총괄")
        for p in game["rosters"][team]:
            pitcher = p["position"] in ("SP","RP","CP")
            coach = pit_coach if pitcher else hit_coach
            if not p.get("injury") and p["age"] < 29:
                base = 0.15 if p["status"]=="퓨처스" else 0.05
                if p["status"]=="퓨처스": base += farm*0.06
                base += coach*0.03
                if random.random() < max(0.02, base):
                    ceiling = int(p.get("potential", p["ovr"]))
                    if p["ovr"] < ceiling:
                        p["ovr"] = min(ceiling, p["ovr"] + 1)
                        for skill in p.get("skills", {}): p["skills"][skill] = min(100, p["skills"][skill] + (1 if random.random()<0.35 else 0))
                        p["development_report"] = "최근 훈련에서 소폭 성장"
            elif p["age"] >= 31 and random.random() < min(0.35, 0.04 + (p["age"]-31)*0.03):
                p["ovr"] = max(25, p["ovr"] - 1)
                # 능력별 하락 시기 차이: 주루·구위가 먼저, 선구·제구는 늦게 (규칙서 6 확정)
                skills = p.get("skills", {})
                early = [k for k in skills if k in ("주루","수비","구위","체력")]
                late = [k for k in skills if k in ("선구","제구","위기관리","컨택")]
                target = random.choice(early) if early and (p["age"] < 35 or random.random()<0.7) else (random.choice(late) if late else None)
                if target: skills[target] = max(0, skills[target]-1)
                p["development_report"] = "연령에 따른 기량 저하 징후"
            p["fatigue"]=max(0,p.get("fatigue",0)-random.randint(5,12))
            p["condition"]=min(100,p.get("condition",80)+random.randint(4,12))
    # 재정 (규칙서 10.1): 수입원별·지출원별 거래 내역 저장
    week_games = {t: {"home":0, "games":0} for t in game["teams"]}
    for rec in played:
        week_games[rec["home"]]["home"] += 1; week_games[rec["home"]]["games"] += 1; week_games[rec["away"]]["games"] += 1
    week_date = game["schedule"][max(0,game["day_index"]-1)]["date"]
    for team in game["teams"]:
        fin=game["finances"][team]
        st=game["standings"][team]; total=st["W"]+st["L"]+st["D"]; pct=st["W"]/max(1,total)
        fans=game.setdefault("fan_interest_by_team",{}).setdefault(team, TEAM_INFO[team]["fans"])
        homes=week_games[team]["home"]
        pr_boost = 1 + staff_effect(game, team, "홍보팀 팀장")*0.08
        gate=int(homes*(1200+fans*30+pct*1500)*pr_boost)
        merch=int(homes*(150+fans*6)*pr_boost)
        sponsor=int((2500+fans*20)*pr_boost)
        broadcast=2500
        staff_cost=int(sum(int(s.get("salary",0)) for s in game.get("staff",{}).values())/24) if team==game["team"] else 1800
        payroll_cost=int(fin["payroll"]/24)
        operations=int(4200+homes*250)
        if homes: record_transaction(game, team, "입장권 수입", gate, f"홈 {homes}경기 입장권·시즌권", week_date, "입장권")
        if homes: record_transaction(game, team, "상품 판매", merch, f"홈 {homes}경기 상품 판매", week_date, "상품")
        record_transaction(game, team, "스폰서·광고", sponsor, "주간 스폰서십·광고 수입", week_date, "스폰서")
        record_transaction(game, team, "중계·리그 배분금", broadcast, "주간 중계권·리그 배분", week_date, "중계")
        record_transaction(game, team, "선수 연봉", -payroll_cost, f"연봉 총액 {fin['payroll']:,}만원의 주간 분할 지급", week_date, "선수 연봉")
        record_transaction(game, team, "스태프 연봉", -staff_cost, "프런트·코칭스태프 주간 분할 지급", week_date, "스태프 연봉")
        record_transaction(game, team, "운영비", -operations, f"구장 운영·이동·훈련 ({week_games[team]['games']}경기)", week_date, "운영비")
    # 사기·팀 분위기·팬 관심 (규칙서 12·13): 연승·연패, 성적, 리더십 반영, 영향 상한
    for team in game["teams"]:
        st=game["standings"][team]
        total=st["W"]+st["L"]+st["D"]
        pct=st["W"]/max(1,total)
        game.setdefault("team_morale_by_team",{}).setdefault(team,65)
        current=game["team_morale_by_team"][team]
        leadership = staff_effect(game, team, "감독", "리더십")
        delta=(1 if pct>=.55 else -1 if pct<.40 else 0)+(1 if st.get("streak")=="W" and random.random()<0.3 else 0)-(1 if st.get("streak")=="L" and random.random()<0.3 else 0)
        if leadership>0.3 and delta<0 and random.random()<0.5: delta=0
        current=max(30,min(90,current+delta))
        game["team_morale_by_team"][team]=current
        game.setdefault("fan_interest_by_team",{}).setdefault(team, TEAM_INFO[team]["fans"])
        game["fan_interest_by_team"][team]=max(20,min(100,game["fan_interest_by_team"][team]+(1 if pct>=.55 else -1 if pct<.35 else 0)))
    own=game["team"]; own_st=game["standings"][own]
    game["team_morale"]=game["team_morale_by_team"].get(own,65)
    game["fan_interest"]=game["fan_interest_by_team"].get(own,TEAM_INFO[own]["fans"])
    own_players=game["rosters"][own]
    injured=[p for p in own_players if p.get("injury")]
    prospects=sorted([p for p in own_players if p.get("status")=="퓨처스"],key=lambda p:p.get("ovr",0),reverse=True)[:3]
    # 콜업 추천: 퓨처스 선수 중 1군 같은 포지션 최저 OVR보다 높은 선수 (규칙서 7)
    callups=[]
    active=active_roster(game,own)
    for p in prospects:
        same=[a for a in active if a["position"]==p["position"]]
        if same and p["ovr"] > min(a["ovr"] for a in same) and not p.get("injury"):
            callups.append(f"{p['name']}({p['position']}, OVR {p['ovr']}) 콜업 검토: 1군 동일 포지션 최저 OVR {min(a['ovr'] for a in same)}")
    pending_trades=[o for o in game.get("trade_offers",[]) if o.get("status")=="pending" and o.get("to_team")==own]
    week_tx=[t for t in game["finances"][own]["transactions"] if t.get("date")==week_date]
    income=sum(t["amount"] for t in week_tx if t["amount"]>0); expense=-sum(t["amount"] for t in week_tx if t["amount"]<0)
    ranking=sorted(game["teams"],key=lambda t:(game["standings"][t]["W"]/max(1,sum(game["standings"][t][k] for k in ("W","L","D"))),game["standings"][t]["W"]),reverse=True)
    top_batters=sorted([p for p in active if p["position"] not in ("SP","RP","CP") and p["stats"]["AB"]>0],key=lambda p:p["stats"]["H"]/max(1,p["stats"]["AB"]),reverse=True)[:3]
    report={"id":f"WR-{game['season']}-{game['week']}","season":game["season"],"week":game["week"],"team":own,
      "record":{"W":own_st["W"],"L":own_st["L"],"D":own_st["D"]},"rank":ranking.index(own)+1,"games_processed":len([r for r in played if own in (r["home"],r["away"])]),
      "injuries":[{"name":p["name"],"diagnosis":p.get("injury"),"days":p.get("injury_days",0),"rehab":p.get("rehab_score",0)} for p in injured],
      "prospects":[{"name":p["name"],"ovr":p["ovr"],"report":p.get("development_report","관찰 중")} for p in prospects],
      "top_batters":[{"name":p["name"],"avg":round(p["stats"]["H"]/max(1,p["stats"]["AB"]),3),"hr":p["stats"]["HR"]} for p in top_batters],
      "income":income,"expense":expense,"pending_trades":len(pending_trades),
      "cash":game["finances"][own]["cash"],"payroll":game["finances"][own]["payroll"],
      "morale":game.get("team_morale_by_team",{}).get(own,65),"fan_interest":game.get("fan_interest_by_team",{}).get(own,60),
      "recommendations":[]}
    if injured: report["recommendations"].append(f"부상 선수 {len(injured)}명: 재활 평가와 퓨처스 대체 자원을 확인하세요.")
    if game["finances"][own]["cash"] < 0: report["recommendations"].append("현금 잔액이 음수입니다. 신규 계약과 선택적 지출을 재검토하세요. (근거: 주간 지출이 수입을 초과)")
    if expense > income: report["recommendations"].append(f"이번 주 적자 {expense-income:,}만원: 연봉 비중이 높습니다. 트레이드·방출로 연봉 조정을 검토하세요.")
    report["recommendations"].extend(callups)
    if pending_trades: report["recommendations"].append(f"미응답 트레이드 제안 {len(pending_trades)}건이 단장 단톡에 있습니다.")
    if prospects and not callups: report["recommendations"].append(f"퓨처스 유망주 {prospects[0]['name']}({prospects[0]['ovr']} OVR)의 성장 추이를 관찰하세요.")
    game.setdefault("weekly_reports",[]).append(report)
    game["weekly_reports"]=game["weekly_reports"][-60:]
    game["reports"].insert(0,f"{game['season']}시즌 {game['week']}주차 완료: 리그 {len(played)}경기 처리. 성적·부상/재활·퓨처스 후보·재정·팀 분위기 주간 보고를 생성했습니다.")
    game["reports"]=game["reports"][:80]
    return played

def audit_game_state(game):
    """Lightweight integrity audit; not a substitute for full season testing."""
    issues = []
    teams = game.get("teams", [])
    if len(teams) != 10:
        issues.append(f"리그 구단 수가 10개가 아닙니다: {len(teams)}")
    target = int(rule_data(game.get("season", 2027), "regular_games"))
    per_team = {t: 0 for t in teams}
    home_count = {t: 0 for t in teams}
    for day in game.get("schedule", []):
        for g in day.get("games", []):
            if g.get("rained_out") and not g.get("makeup"):
                continue
            per_team[g["home"]] = per_team.get(g["home"], 0) + 1
            per_team[g["away"]] = per_team.get(g["away"], 0) + 1
            home_count[g["home"]] = home_count.get(g["home"], 0) + 1
    for t in teams:
        if per_team.get(t) != target:
            issues.append(f"{t} 편성 경기 수가 {target}이 아닙니다: {per_team.get(t)}")
        if abs(home_count.get(t, 0) - target // 2) > 1:
            issues.append(f"{t} 홈 경기 수 불균형: {home_count.get(t)}")
    player_ids = []
    total_limit = roster_total_limit(game)
    for club, roster in game.get("rosters", {}).items():
        if len(roster) > total_limit:
            issues.append(f"{club} 소속선수 {len(roster)}명이 정원 {total_limit}명을 초과")
        active = active_count_for_limit(game, club)
        if active > roster_limit(game):
            issues.append(f"{club} 1군 등록 {active}명이 한도 {roster_limit(game)}명을 초과")
        fin = game.get("finances", {}).get(club, {})
        if fin and fin.get("payroll") != sum(int(p.get("salary", 0)) for p in roster):
            issues.append(f"{club} 연봉 총액이 선수단 연봉 합계와 다릅니다")
        if fin and fin.get("transactions") and abs(sum(int(t.get("amount", 0)) for t in fin["transactions"]) - int(fin.get("cash", 0))) > 1:
            issues.append(f"{club} 현금 잔액이 거래 내역 합계와 다릅니다 (장부 재현 불가)")
        for player in roster:
            player_ids.append(player.get("id"))
            if player.get("team") != club:
                issues.append(f"{club}/{player.get('name')} 소속 구단 필드 불일치")
            if not (0 <= int(player.get("ovr", 0)) <= 100):
                issues.append(f"{club}/{player.get('name')} 능력치 범위 오류")
            if int(player.get("contract_years", 0)) < 0:
                issues.append(f"{club}/{player.get('name')} 계약 기간 음수")
    for p in game.get("free_agents", []):
        player_ids.append(p.get("id"))
    if len(player_ids) != len(set(player_ids)):
        issues.append("선수 ID 중복 (선수단·FA 풀)")
    return issues


@app.route("/system/audit")
def system_audit():
    game = get_game()
    if not game:
        flash("게임 세이브가 없습니다.", "error")
        return redirect(url_for("start_game"))
    findings = audit_game_state(game)
    items = ([{"name":"문제 발견","description":x} for x in findings] if findings else
             [{"name":"기초 무결성 검사 통과","description":"구단 수, 구단별 144경기·홈 경기 균형, 선수 ID, 소속·1군 정원, 연봉 총액, 현금-장부 일치, 능력치 범위, 계약 기간을 검사했습니다."}])
    return render_template("module.html", game=game, title="세이브 정합성 점검",
        description="기초 데이터 무결성 검사입니다. 공식 규정 완전성이나 전체 게임 밸런스 검증을 의미하지 않습니다.", items=items)


def require_game():
    game=get_game()
    if not game:
        flash("먼저 새 게임을 시작하고 구단을 선택하세요.","error")
        return None
    if game.get("game_status") == "종료" and request.endpoint not in {"owner_review", "index", "new_game_route", "logout", "static"}:
        flash("이 커리어는 종료되었습니다. 최종 점수를 확인하거나 새 커리어를 시작하세요.", "error")
        return None
    return game


def calculate_final_career_score(game, reason="커리어 종료"):
    """종료 시 0~1000점 종합 평가. 결과는 세이브에 고정 저장한다."""
    team = game.get("team")
    st = game.get("standings", {}).get(team, {})
    wins, losses, draws = (int(st.get(k, 0)) for k in ("W", "L", "D"))
    played = wins + losses + draws
    history = game.get("season_history", [])
    if played == 0 and history:
        # 새 시즌 개막 직후 종료: 직전 완료 시즌 성적으로 평가한다.
        last = history[-1]; rec = last.get("user_team_record", {})
        wins, losses, draws = int(rec.get("W", 0)), int(rec.get("L", 0)), int(rec.get("D", 0))
        played = wins + losses + draws
        rank = int(last.get("user_team_finish", 10))
    else:
        rank = league_ranking(game).index(team) + 1 if team in game.get("teams", []) else 10
    win_pct = wins / played if played else 0.5
    seasons = max(1, len(history) + (1 if played else 0))
    championships = sum(1 for h in history if h.get("champion") == team)
    current_ps = game.get("postseason") or {}
    if current_ps.get("champion") == team and current_ps.get("season") == game.get("season") and not any(h.get("season") == game.get("season") for h in history):
        championships += 1
    playoffs = sum(1 for h in history if h.get("user_team_finish", 10) <= 5)
    if rank <= 5 and played and not any(h.get("season") == game.get("season") for h in history) and game.get("day_index", 0) >= len(game.get("schedule", [])):
        playoffs += 1
    cash = int(game.get("finances", {}).get(team, {}).get("cash", 0))
    payroll = int(game.get("finances", {}).get(team, {}).get("payroll", 0))
    # 구성 점수 합계 1,000점: 성적 400, 우승 200, 지속성 100, 재정 150, 육성/운영 100, 구단주 신뢰 50
    performance = round(win_pct * 250 + max(0, (11-rank)/10) * 150)
    trophy = min(200, championships * 100 + playoffs * 15)
    longevity = min(100, seasons * 10)
    finance = 150 if cash >= 0 else max(0, 150 + round(cash / 1000))
    if payroll > 0 and cash < 0:
        finance = max(0, finance - 15)
    development = int(min(100, max(0, int(game.get("team_morale", 65)) * 0.5 + int(game.get("fan_interest", 60)) * 0.5)))
    trust = max(0, min(50, int(game.get("owner_trust", 65)) // 2))
    total = int(max(0, min(1000, performance + trophy + longevity + finance + development + trust)))
    if total >= 850: grade = "전설적인 단장"
    elif total >= 700: grade = "명장"
    elif total >= 550: grade = "성공적인 커리어"
    elif total >= 400: grade = "평범한 커리어"
    elif total >= 250: grade = "아쉬운 커리어"
    else: grade = "재건에 실패한 커리어"
    return {
        "total_score": total, "grade": grade, "reason": reason,
        "season": int(game.get("season", 2027)), "team": team,
        "rank": rank, "wins": wins, "losses": losses, "draws": draws,
        "win_pct": round(win_pct, 3), "seasons_managed": seasons,
        "championships": championships, "playoff_appearances": playoffs,
        "components": {
            "성적": performance, "우승·포스트시즌": trophy,
            "장기 운영": longevity, "재정": finance,
            "육성·팬 관심": development, "구단주 신뢰": trust
        }
    }


def league_ranking(game, standings=None):
    standings = standings or game["standings"]
    def key(t):
        st = standings[t]; total = st.get("W",0)+st.get("L",0)+st.get("D",0)
        return (st.get("W",0)/max(1,total), st.get("W",0), st.get("RS",0)-st.get("RA",0))
    return sorted(game["teams"], key=key, reverse=True)

def compute_owner_review(game):
    """규칙서 16: 성적·재정 건전성·유망주 개발·구단 목표(사전 목표) 종합. 서버에서만 계산한다."""
    team = game["team"]; st = game["standings"][team]
    wins, losses = int(st.get("W", 0)), int(st.get("L", 0))
    played = wins + losses + int(st.get("D", 0))
    win_pct = wins / played if played else 0.5
    rank = league_ranking(game).index(team) + 1
    owner_trust = int(game.get("owner_trust", 65))
    fin = game.get("finances", {}).get(team, {})
    reasons = []
    goal = club_goal(game)
    score = 50 + round((win_pct - 0.5) * (60 if goal == "리빌딩" else 100))
    if goal == "우승 도전":
        score += 10 if rank <= 2 else (-10 if rank >= 6 else 0)
        if rank >= 6: reasons.append("우승 도전 목표 대비 하위권 성적 감점")
    elif goal == "리빌딩":
        score += 6 if rank <= 5 else 0
        prospects = [p for p in game["rosters"][team] if p.get("age", 30) <= 25 and int(p.get("ovr", 0)) >= 62]
        score += min(15, len(prospects) * 2)
        if prospects: reasons.append(f"리빌딩 목표: 유망주 {len(prospects)}명 성장 가산 (단기 순위 비중 축소)")
    else:
        score += 6 if rank <= 3 else (-8 if rank >= 8 else 0)
    score += max(-10, min(10, (owner_trust - 60) // 3))
    cash = int(fin.get("cash", 0))
    season_ledger = fin.get("ledger", {}).get(str(game.get("season")), {})
    net = sum(season_ledger.values()) if season_ledger else 0
    score += 5 if cash > 0 else -8
    if net < 0 and cash < 0: score -= 4
    rep_count = int(game.get("finance_reputation", {}).get(team, 0))
    if rep_count: score -= min(10, rep_count * 3); reasons.append(f"모기업 특별 지원 {rep_count}회 · 재정 평판 감점")
    score = max(0, min(100, score))
    if rank <= 3: reasons.append("상위권 성적 가산")
    if rank >= 8: reasons.append("하위권 성적 감점")
    if cash <= 0: reasons.append("현금흐름 위험 감점")
    if net < 0: reasons.append(f"시즌 누적 적자 {abs(net):,}만원")
    if owner_trust < 40: reasons.append("구단주 신뢰도 위험")
    verdict = "신임" if score >= 45 else "해임 경고"
    return {"score": score, "rank": rank, "wins": wins, "losses": losses, "verdict": verdict, "reasons": reasons, "owner_trust": owner_trust, "goal": goal}

def club_goal(game):
    """구단 사전 목표: 직전 시즌 순위와 전력으로 결정 (규칙서 16: 평가 기준은 구단 목표 반영)."""
    goals = game.setdefault("club_goals", {})
    key = str(game.get("season"))
    if key not in goals:
        history = game.get("season_history", [])
        last_rank = history[-1].get("user_team_finish", 5) if history else None
        strength = TEAM_INFO[game["team"]]["strength"]
        if (last_rank is not None and last_rank <= 3) or strength >= 76: goals[key] = "우승 도전"
        elif (last_rank is not None and last_rank >= 8) or strength <= 68: goals[key] = "리빌딩"
        else: goals[key] = "포스트시즌 진출"
    return goals[key]

def apply_owner_review(game, review):
    """평가 기록 저장과 해임 판정. 시즌당 1회만 반영한다."""
    reviews = game.setdefault("owner_reviews", [])
    if any(r.get("season") == game["season"] for r in reviews):
        return False
    score = int(review["score"])
    reviews.append({"season": game["season"], "score": score, "verdict": review["verdict"], "goal": review.get("goal"), "date": datetime.now().isoformat(timespec="seconds")})
    game["owner_trust"] = max(0, min(100, int(game.get("owner_trust",65)) + (5 if score >= 65 else (-8 if score < 45 else 0))))
    game.setdefault("manager_history", [])
    if score < 45:
        game["owner_bad_reviews"] = int(game.get("owner_bad_reviews", 0)) + 1
        if game["owner_bad_reviews"] == 1:
            game.setdefault("reports", []).insert(0, "구단주 경고: 평가 점수가 기준 미달입니다. 다음 시즌에도 개선이 없으면 해임됩니다.")
    else:
        game["owner_bad_reviews"] = 0
    if game["owner_bad_reviews"] >= 2 and not game.get("manager_fired"):
        game["manager_fired"] = True
        reason = "구단주 평가 2회 연속 기준 미달에 따른 해임"
        game["manager_history"].append({"name":game.get("manager_name","초대 단장"),"team":game["team"],"start_season":game.get("manager_start_season",2027),"end_season":game["season"],"reason":reason,"fired_at":datetime.now().isoformat(timespec="seconds")})
        game["game_status"] = "종료"
        game["game_end"] = {"reason": reason, "season": game["season"], "ended_at": datetime.now().isoformat(timespec="seconds")}
        game["game_end"]["score"] = calculate_final_career_score(game, reason)
        game.setdefault("reports", []).insert(0, f"게임 오버: {game.get('manager_name','초대 단장')} 단장 해임 · 최종 종합 점수 {game['game_end']['score']['total_score']}/1000점")
    return True

@app.context_processor
def inject_globals():
    state = load_state()
    game = get_game()
    return {"current_season": game["season"] if game else state["season"], "active_rule_count": len(state["active_rules"]), "nav_team": game["team"] if game else session.get("team"), "game_phase": game.get("phase", "정규시즌") if game else None}

@app.route("/")
def index():
    state = load_state()
    game = get_game() if session.get("user_id") else None
    return render_template("landing.html", state=state, selected_team=game.get("team") if game else None, saved_game=game)

@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("office"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not re.fullmatch(r"[A-Za-z0-9_가-힣]{3,20}", username):
            flash("아이디는 한글·영문·숫자·밑줄로 3~20자여야 합니다.", "error")
        elif len(password) < 8:
            flash("비밀번호는 8자 이상으로 설정해 주세요.", "error")
        elif password != confirm:
            flash("비밀번호 확인이 일치하지 않습니다.", "error")
        else:
            try:
                with db_connect() as conn:
                    cur = conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)", (username, generate_password_hash(password), datetime.now().isoformat(timespec="seconds")))
                    user_id = cur.lastrowid
                log_auth_event(username, "회원가입", "성공", user_id)
                session.clear()
                session["user_id"] = user_id
                session["username"] = username
                flash("회원가입이 완료되었습니다. 구단을 선택해 새 게임을 시작하세요.", "success")
                return redirect(url_for("start_game"))
            except sqlite3.IntegrityError:
                flash("이미 사용 중인 아이디입니다.", "error")
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("office"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db_connect() as conn:
            user = conn.execute("SELECT id, username, password_hash FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            log_auth_event(user["username"], "로그인", "성공", user["id"])
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("office") if user_game_id(user["id"]) else url_for("start_game")
            return redirect(target)
        log_auth_event(username, "로그인", "실패", user["id"] if user else None)
        flash("아이디 또는 비밀번호를 확인해 주세요.", "error")
    return render_template("auth.html", mode="login")

@app.route("/account/login-history")
def login_history_page():
    user_id = session.get("user_id")
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT username, event, result, occurred_at FROM auth_logs WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (user_id,),
        ).fetchall()
    return render_template("login_history.html", logs=rows)

@app.route("/logout", methods=["POST"])
def logout():
    log_auth_event(session.get("username", ""), "로그아웃", "성공", session.get("user_id"))
    session.clear()
    flash("로그아웃했습니다.", "info")
    return redirect(url_for("index"))

@app.route("/start", methods=["GET", "POST"])
def start_game():
    teams = list(TEAM_INFO.keys())
    existing_game = get_game()
    if request.method == "POST":
        team = request.form.get("team")
        if team not in teams:
            flash("구단을 선택해 주세요.", "error")
            return render_template("team_select.html", teams=teams, existing_game=existing_game)
        # One active save per account. Starting a new game replaces only this user's save.
        old_id = user_game_id(session.get("user_id"))
        game = create_game(team)
        set_user_game(session["user_id"], game["game_id"])
        session["team"] = team
        session["game_id"] = game["game_id"]
        if old_id and old_id != game["game_id"]:
            try: os.remove(game_path(old_id))
            except OSError: pass
        flash(f"{team} 단장으로 2027시즌을 시작합니다. 기존 계정 세이브는 새 게임으로 교체되었습니다.", "success")
        return redirect(url_for("office"))
    return render_template("team_select.html", teams=teams, existing_game=existing_game)

@app.route("/office")
def office():
    game=get_game()
    if not game:
        state=load_state()
        current_event=next((e for e in reversed(state["event_history"]) if e["status"]=="심의 중"),None)
        return render_template("index.html",state=state,rules=RULES,current_event=current_event,selected_team=session.get("team","구단 미선택"))
    team=game["team"]
    standing=league_ranking(game)
    rank=standing.index(team)+1
    st=game["standings"][team]
    recent=[g for g in game["game_log"] if team in (g["home"],g["away"])][-5:][::-1]
    target=int(rule_data(game["season"],"regular_games"))
    return render_template("office_game.html",game=game,team=team,info=TEAM_INFO[team],standing=st,rank=rank,roster=active_roster(game,team),recent=recent,played_days=game["day_index"],played_games=team_games_played(game,team),target_games=target,roster_cap=roster_limit(game),goal=club_goal(game),team_strength_now=round(club_strength_now(game,team),1),standings_order=standing)

@app.route("/modules/<slug>")
def module_page(slug):
    destinations={"roster":"roster_page","schedule":"schedule_page","contracts":"contracts_page","finance":"finance_page","staff":"staff_page","history":"history_page","settings":"settings_page"}
    if slug in destinations:
        return redirect(url_for(destinations[slug]))
    flash("해당 메뉴를 찾을 수 없습니다.", "error")
    return redirect(url_for("office"))

@app.route("/roster")
def roster_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]
    roster=game["rosters"][team]
    return render_template("roster_game.html",game=game,team=team,roster=roster,active_count=len(active_roster(game,team)),active_for_limit=active_count_for_limit(game,team),roster_cap=roster_limit(game),roster_total=roster_total_limit(game),payroll=game["finances"][team]["payroll"],cap=salary_cap(game,team),cap_now=cap_payroll(game,team))

@app.route("/roster/<player_id>/move",methods=["POST"])
def move_player(player_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; player=next((p for p in game["rosters"][team] if p["id"]==player_id),None)
    if not player: flash("선수를 찾을 수 없습니다.","error")
    else:
        target=request.form.get("status")
        active=active_roster(game,team)
        if target=="1군" and player["status"]!="1군" and active_count_for_limit(game,team)>=roster_limit(game): flash(f"현재 1군 등록 한도({roster_limit(game)}명)에 도달했습니다. 적용 규정: {game['season']}시즌 KBO 1군 등록 {rule_data(game['season'],'active_register')}명" + (" + 확대 엔트리" if roster_limit(game) > rule_data(game['season'],'active_register') + (2 if rule_is_active(game,'R-06') else 0) else "") + (" + R-06" if rule_is_active(game,'R-06') else ""),"error")
        elif target not in ("1군","퓨처스"): flash("올바르지 않은 이동입니다.","error")
        elif player.get("injury") and target=="1군": flash("부상 회복 중인 선수는 1군으로 등록할 수 없습니다.","error")
        else:
            player["status"]=target; game["reports"].insert(0,f"선수단 이동: {player['name']} → {target}"); save_game(game); flash(f"{player['name']} 선수를 {target}로 이동했습니다.","success")
    return redirect(url_for("roster_page"))

@app.route("/roster/<player_id>/release", methods=["POST"])
def release_player(player_id):
    game = require_game()
    if not game: return redirect(url_for("start_game"))
    team = game["team"]
    roster = game["rosters"].get(team, [])
    player = next((p for p in roster if p.get("id") == player_id), None)
    if not player:
        flash("방출할 선수를 찾을 수 없습니다.", "error")
    else:
        # Release is an explicit permanent transaction. Preserve the player in the FA pool.
        if player.get("status") == "1군" and len(active_roster(game, team)) <= 1:
            flash("최소 1명의 1군 선수를 남겨야 합니다.", "error")
            return redirect(url_for("roster_page"))
        compensation = release_compensation(game, player)
        if game["finances"][team].get("cash", 0) < compensation:
            flash(f"방출 시 잔여 계약 보상금 {compensation:,}만원을 지급할 현금이 부족합니다.", "error")
            return redirect(url_for("roster_page"))
        release_to_market(game, team, player, "단장 방출")
        save_game(game)
        flash(f"{player['name']} 선수를 방출했습니다. 잔여 계약 보상 {compensation:,}만원을 지급했고 웨이버(FA 풀)로 공시됩니다.", "success")
    return redirect(url_for("roster_page"))

def release_compensation(game, player):
    """규칙서 8.3: 계약 기간 중 방출 시 보장 연봉 보상. 잔여 연도 연봉의 50%를 일시 지급한다 (게임 기본안)."""
    years=max(0, player_contract_years(player)-1)
    guaranteed=int(player.get("salary",0))*years
    in_season=int(player.get("salary",0))//2 if game.get("phase")=="정규시즌" else 0
    return int(guaranteed*0.5)+in_season

def release_to_market(game, team, player, reason):
    roster=game["rosters"][team]
    compensation=release_compensation(game, player)
    roster.remove(player)
    player.update({"status":"FA","former_team":team,"previous_team":team,"team":None,"released_season":game.get("season",2027),"released_week":game.get("week",0),"release_reason":reason})
    game.setdefault("free_agents",[]).append(player)
    recalc_payroll(game, team)
    record_transaction(game, team, "선수 방출 보상", -compensation, f"{player['name']} 방출 · 잔여 계약 {max(0,player_contract_years(player)-1)}년 보상", None, "계약 보상")
    game.setdefault("reports",[]).insert(0,f"선수단 조치: {player['name']} 선수를 방출했습니다({reason}). 웨이버 공시 후 FA 풀로 이동합니다.")

@app.route("/roster/release-selected", methods=["POST"])
def release_selected_players():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]
    ids=set(request.form.getlist("player_ids"))
    roster=game["rosters"].get(team, [])
    selected=[p for p in roster if str(p.get("id")) in ids]
    if not selected:
        flash("방출할 선수를 먼저 선택하세요.", "error")
        return redirect(url_for("roster_page"))
    active=[p for p in roster if p.get("status")=="1군"]
    active_selected=sum(1 for p in selected if p.get("status")=="1군")
    if len(active)-active_selected < 1:
        flash("최소 1명의 1군 선수를 남겨야 합니다. 선택을 줄여 주세요.", "error")
        return redirect(url_for("roster_page"))
    total_comp=sum(release_compensation(game,p) for p in selected)
    if game["finances"][team].get("cash",0) < total_comp:
        flash(f"선택 선수의 잔여 계약 보상금 합계 {total_comp:,}만원을 지급할 현금이 부족합니다.", "error")
        return redirect(url_for("roster_page"))
    for player in selected:
        release_to_market(game, team, player, "일괄 방출")
    game.setdefault("reports",[]).insert(0,f"선수단 정리: {len(selected)}명을 일괄 방출했습니다. 보상 합계 {total_comp:,}만원.")
    save_game(game); flash(f"선수 {len(selected)}명을 일괄 방출했습니다. 잔여 계약 보상 {total_comp:,}만원.","success")
    return redirect(url_for("roster_page"))

@app.route("/schedule")
def schedule_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]
    upcoming=[]
    for day in game["schedule"][game["day_index"]:game["day_index"]+12]:
        for fixture in day["games"]:
            if team in (fixture["home"],fixture["away"]):
                upcoming.append({**fixture,"date":day["date"],"series_no":day.get("series_no"),"game_no":day.get("game_no")})
    calendar_days=[]
    scheduled_by_date={day.get("date"): (idx, day) for idx, day in enumerate(game["schedule"])}
    if game.get("schedule"):
        from datetime import date as _date, timedelta as _td
        first=_date.fromisoformat(game["schedule"][0]["date"]); last=_date.fromisoformat(game["schedule"][-1]["date"])
        cursor=first
        while cursor<=last:
            date_key=cursor.isoformat()
            if cursor.weekday()==0:
                calendar_days.append({"date":date_key,"day_index":None,"fixture":None,"record":None,"current":False,"rest":True,"result_class":"rest"})
            elif date_key in scheduled_by_date:
                idx, day=scheduled_by_date[date_key]
                fixture=next((f for f in day.get("games",[]) if team in (f.get("home"),f.get("away"))),None)
                record=next((g for g in game.get("game_log",[]) if g.get("date")==date_key and fixture and {g.get("home"),g.get("away")}=={fixture.get("home"),fixture.get("away")}),None)
                result_class="upcoming"
                if record:
                    if record.get("home_score")==record.get("away_score"):
                        result_class="draw"
                    else:
                        won=(record.get("home")==team and record.get("home_score",0)>record.get("away_score",0)) or (record.get("away")==team and record.get("away_score",0)>record.get("home_score",0))
                        result_class="win" if won else "loss"
                calendar_days.append({"date":date_key,"day_index":idx,"fixture":fixture,"record":record,"current":idx==game.get("day_index",0),"rest":False,"result_class":result_class})
            cursor += _td(days=1)
    target=int(rule_data(game["season"],"regular_games"))
    return render_template("schedule_game.html",game=game,team=team,upcoming=upcoming,calendar_days=calendar_days,played_games=team_games_played(game,team),target_games=target,season_over=regular_season_complete(game),postponed=[p for p in game.get("postponed_games",[]) if team in (p.get("home"),p.get("away"))],standings=league_ranking(game))

@app.route("/season/advance-week",methods=["POST"])
def advance_week():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    if game["day_index"]>=len(game["schedule"]):
        flash("정규시즌 경기를 모두 진행했습니다. 포스트시즌 메뉴로 이동하세요.","info")
        return redirect(url_for("schedule_page"))
    old=len(game["game_log"]); played=advance_week_game(game); save_game(game)
    flash(f"{game['season']}시즌 {game['week']}주차 진행 완료: 리그 {len(played)}경기 처리.","success")
    return redirect(url_for("schedule_page"))

@app.route("/finance")
def finance_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; fin=game["finances"][team]
    ledger=fin.get("ledger",{}).get(str(game["season"]),{})
    income={k:v for k,v in ledger.items() if v>0}; expense={k:-v for k,v in ledger.items() if v<0}
    future=[]
    for y in range(1,5):
        future.append({"season":game["season"]+y,"amount":sum(int(p.get("salary",0)) for p in game["rosters"][team] if player_contract_years(p)>y)})
    return render_template("finance_game.html",game=game,team=team,fin=fin,income=income,expense=expense,net=sum(ledger.values()),future=future,cap=salary_cap(game,team),cap_now=cap_payroll(game,team),ledger_by_season={k:sum(v.values()) for k,v in fin.get("ledger",{}).items()},staff_cost=sum(int(x.get("salary",0)) for x in game.get("staff",{}).values()))

def fa_player_preference(player):
    """FA 선수 성향(돈·우승·역할·충성)은 시장 진입 시 한 번만 생성한다. 협상 횟수 제한도 함께 둔다."""
    if "fa_preferences" not in player:
        player["fa_preferences"] = {
            "money": random.randint(35, 75), "winning": random.randint(5, 25),
            "role": random.randint(5, 25), "loyalty": random.randint(0, 20),
            "patience": random.randint(2, 4),   # 구단별 협상 가능 횟수
            "preferred_team": player.get("previous_team") if random.random() < 0.28 else random.choice(list(TEAM_INFO)),
        }
    return player["fa_preferences"]

def club_strength_now(game, team):
    active = active_roster(game, team)
    if not active:
        return TEAM_INFO[team]["strength"]
    return sum(int(p.get("ovr", 50)) for p in active) / len(active)

def fa_offer_score(player, offer, game):
    pref = fa_player_preference(player)
    ask = max(1, int(player.get("salary", 5000)))
    money_score = max(0, min(100, int((int(offer.get("salary", 0)) / ask) * 65)))
    team = offer.get("team")
    winning_score = max(0, min(100, int(club_strength_now(game, team) + 10)))
    role_score = {"핵심 주전": 95, "마무리/선발 핵심": 90, "주전 경쟁": 70, "백업": 35}.get(offer.get("role", "주전 경쟁"), 60)
    loyalty_score = 100 if team == player.get("previous_team") else (100 if team == pref.get("preferred_team") else 25)
    years = int(offer.get("years", 1))
    # 계약 기간 선호: 젊은 선수는 짧게, 30대는 길게
    ideal_years = 2 if int(player.get("age", 28)) < 29 else 3
    term_score = max(20, 100 - abs(years - ideal_years) * 20)
    denom = max(1, pref["money"] + pref["winning"] + pref["role"] + pref["loyalty"] + 10)
    return int((money_score * pref["money"] + winning_score * pref["winning"] + role_score * pref["role"] + loyalty_score * pref["loyalty"] + term_score * 10) / denom)

def sign_player_to_club(game, player, club, salary, years, source_note, status=None):
    """FA 계약 공통 처리: 선수단 이동, 계약 구조, 연봉 총액·거래 내역, 1군 한도."""
    player["team"] = club
    player["status"] = status or ("퓨처스" if int(player.get("ovr", 50)) < 62 else "1군")
    player["salary"] = int(salary); player["contract_years"] = int(years)
    player["contract"] = {"years": int(years), "salary_by_year": [int(salary)] * int(years), "guaranteed": True, "bonuses": [], "team_option": False, "player_option": False}
    player.pop("fa_preferences", None); player.pop("negotiations", None)
    game["rosters"][club].append(player)
    if player in game.get("free_agents", []):
        game["free_agents"].remove(player)
    recalc_payroll(game, club)
    record_transaction(game, club, "FA 계약금", -int(salary) // 2, f"{player['name']} · {years}년 · 연봉 {int(salary):,}만원 ({source_note})", f"{game['season']}-11-{random.randint(10,28):02d}", "계약금")
    enforce_active_limit(game, club, announce=(club == game.get("team")))

def generate_ai_fa_offers(game):
    """AI 구단은 FA 선수별 최대 2개 제안만 생성하고 예산·정원·팀 전략(우승 도전/리빌딩)을 확인한다 (규칙서 15.1)."""
    total_limit = roster_total_limit(game)
    ranking = league_ranking(game)
    for player in game.get("free_agents", []):
        existing = [o for o in game.get("fa_offers", []) if o.get("player_id") == player["id"] and o.get("source") == "AI"]
        if existing:
            continue
        clubs = [c for c in game["teams"] if c != game.get("team") and c != player.get("previous_team")]
        random.shuffle(clubs)
        made = 0
        for club in clubs:
            fin = game["finances"].get(club, {})
            rank = ranking.index(club) + 1
            rebuilding = rank >= 8
            # 리빌딩 구단은 32세 이상 베테랑에게 다년 계약을 제안하지 않는다.
            if rebuilding and int(player.get("age", 28)) >= 32 and random.random() < 0.7:
                continue
            if len(game["rosters"].get(club, [])) >= total_limit or int(fin.get("cash", 0)) < int(player.get("salary", 5000)) * 2:
                continue
            years = random.randint(1, 2) if rebuilding or int(player.get("age", 28)) >= 33 else random.randint(1, 4)
            offer = {"id": uuid.uuid4().hex[:10], "player_id": player["id"], "player_name": player["name"],
                     "team": club, "salary": int(player.get("salary", 5000) * random.uniform(0.90, 1.30)),
                     "years": years, "role": random.choice(["주전 경쟁", "핵심 주전", "백업"]),
                     "source": "AI", "day": game.get("fa_days_left", 21), "result": "검토 중", "season": game["season"]}
            offer["player_score"] = fa_offer_score(player, offer, game)
            game.setdefault("fa_offers", []).append(offer)
            made += 1
            if made >= 2:
                break

def resolve_ai_fa_signings(game):
    """매일 선수별 최고 제안 하나만 평가하고, 구단 정원과 재정을 확인한다. 마감 임박 시 수락 기준이 낮아진다 (규칙서 8.2)."""
    total_limit = roster_total_limit(game)
    days_left = int(game.get("fa_days_left", 21))
    for player in list(game.get("free_agents", [])):
        offers = [o for o in game.get("fa_offers", []) if o.get("player_id") == player["id"] and o.get("source") == "AI" and o.get("result") == "검토 중"]
        if not offers:
            continue
        best = max(offers, key=lambda o: o.get("player_score", 0))
        threshold = 52 - (6 if days_left <= 5 else 0)
        if best.get("player_score", 0) + random.randint(-10, 10) < threshold:
            best["result"] = "선수 거절"
            continue
        club = best["team"]
        fin = game["finances"].get(club, {})
        if len(game["rosters"].get(club, [])) >= total_limit or int(fin.get("cash", 0)) < int(best["salary"]):
            best["result"] = "구단 정원/재정 부족"
            continue
        best["result"] = "선수 수락"
        for other in game.get("fa_offers", []):
            if other.get("player_id") == player["id"] and other is not best and other.get("result") == "검토 중": other["result"] = "타 구단 계약으로 종료"
        sign_player_to_club(game, player, club, best["salary"], best["years"], "타 구단 FA 영입")
        game.setdefault("reports", []).insert(0, f"타 구단 FA 계약: {club}가 {player['name']}과 {best['years']}년 계약에 합의했습니다.")

@app.route("/contracts")
def contracts_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; game.setdefault("free_agents", []); game.setdefault("fa_offers", [])
    offers=[o for o in game["fa_offers"] if o.get("team")==team]
    counters={o["player_id"]: o for o in offers if o.get("result")=="역제안" }
    cap=salary_cap(game, team); cap_now=cap_payroll(game, team)
    future_burden=sum(int(p.get("salary",0))*max(0,player_contract_years(p)-1) for p in game["rosters"][team])
    return render_template("contracts_game.html",game=game,team=team,free_agents=game["free_agents"],fin=game["finances"][team],offers=offers,counters=counters,
        season_over=regular_season_complete(game),cap=cap,cap_now=cap_now,future_burden=future_burden,roster_total=roster_total_limit(game),fa_requirement=fa_service_requirement(game))

def gm_offer_decision(game, player, offer):
    """단장 제안 판정: 수락 / 역제안 / 거절. 협상 횟수·마감·경쟁 제안을 함께 본다 (규칙서 8.2 확정·19 기본안)."""
    pref=fa_player_preference(player)
    negotiations=player.setdefault("negotiations", {}).setdefault(game["team"], 0)
    if negotiations >= pref["patience"]:
        return "결렬", 0, None
    player["negotiations"][game["team"]] = negotiations + 1
    score=fa_offer_score(player, offer, game)
    rival_offers=[o for o in game.get("fa_offers",[]) if o.get("player_id")==player["id"] and o.get("source")=="AI" and o.get("result")=="검토 중"]
    rival_best=max([o.get("player_score",fa_offer_score(player,o,game)) for o in rival_offers],default=0)
    days_left=int(game.get("fa_days_left",21))
    probability=max(5,min(95,int(35 + score*0.55 - rival_best*0.28 + (6 if days_left<=5 else 0) + random.randint(-8,8))))
    if random.randint(1,100)<=probability:
        return "수락", probability, None
    remaining=pref["patience"]-player["negotiations"][game["team"]]
    # 결렬 직전 최종 제안 기회: 선수 성격(인내), 마감 임박, 조건 격차로 판정
    if remaining <= 0 and not (days_left <= 5 or score >= 45):
        return "결렬", probability, None
    ask=max(1,int(player.get("salary",5000)))
    gap=max(0.0, 1.0 - int(offer["salary"])/ask)
    counter_salary=int(max(int(offer["salary"])*1.05, ask*(1.0+min(0.35,gap+0.08+random.uniform(0,0.1))))//100*100)
    ideal_years=2 if int(player.get("age",28))<29 else 3
    counter={"salary":counter_salary,"years":ideal_years if abs(int(offer["years"])-ideal_years)>1 else int(offer["years"]),"role":offer["role"] if offer["role"]!="백업" else "주전 경쟁","remaining":max(0,remaining)}
    return "역제안", probability, counter

@app.route("/contracts/negotiate/<player_id>", methods=["POST"])
def negotiate_free_agent(player_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; fin=game["finances"][team]
    player=next((p for p in game.get("free_agents",[]) if p["id"]==player_id),None)
    try:
        salary=max(min_salary(game),int(request.form.get("salary",0))); years=max(1,min(6,int(request.form.get("years",1))))
        role=request.form.get("role","주전 경쟁")
    except (ValueError,TypeError):
        flash("연봉과 계약 기간을 숫자로 입력해 주세요.","error"); return redirect(url_for("contracts_page"))
    if game.get("phase")!="FA 시장": flash("FA 협상은 FA 시장 기간에만 가능합니다.","error")
    elif not player: flash("해당 선수는 이미 시장을 떠났습니다.","error")
    elif len(game["rosters"].get(team, [])) >= roster_total_limit(game): flash(f"구단 소속선수 {roster_total_limit(game)}명 정원이 가득 찼습니다. 먼저 선수를 정리하세요.","error")
    elif salary > fin["cash"]: flash("제시 연봉이 현재 구단 현금 보유액을 초과합니다.","error")
    else:
        offer={"id":uuid.uuid4().hex[:10],"player_id":player["id"],"player_name":player["name"],"team":team,"salary":salary,"years":years,"role":role,"source":"GM","season":game["season"],"result":"검토 중"}
        decision, probability, counter = gm_offer_decision(game, player, offer)
        offer["probability"]=probability
        # 미래 연도별 부담 표시 (규칙서 8.4)
        offer["future_burden"]=salary*years
        for other in [o for o in game.get("fa_offers",[]) if o.get("player_id")==player["id"] and o.get("team")==team and o.get("result")=="역제안"]:
            other["result"]="역제안 종료"
        if decision=="수락":
            offer["result"]="수락"
            game.setdefault("fa_offers",[]).insert(0,offer)
            for other in game.get("fa_offers", []):
                if other.get("player_id") == player["id"] and other is not offer and other.get("result") == "검토 중": other["result"] = "GM 계약으로 종료"
            sign_player_to_club(game, player, team, salary, years, "단장 FA 영입")
            game["reports"].insert(0,f"FA 계약 성사: {player['name']} · {years}년 · 연봉 {salary:,}만원 · 총 보장 {salary*years:,}만원")
            flash(f"{player['name']} 선수가 수락했습니다. {years}년 계약 성사! (총 보장 {salary*years:,}만원)","success")
        elif decision=="역제안":
            offer["result"]="역제안"; offer["counter"]=counter
            game.setdefault("fa_offers",[]).insert(0,offer)
            game["reports"].insert(0,f"FA 역제안: {player['name']} · 연봉 {counter['salary']:,}만원 · {counter['years']}년 · {counter['role']} (남은 협상 {counter['remaining']}회)")
            flash(f"{player['name']} 선수가 역제안했습니다: 연봉 {counter['salary']:,}만원 · {counter['years']}년 · {counter['role']}. 수락하거나 새 조건을 제시하세요.","info")
        else:
            offer["result"]="결렬"
            game.setdefault("fa_offers",[]).insert(0,offer)
            game["reports"].insert(0,f"FA 협상 결렬: {player['name']} · 제시 연봉 {salary:,}만원 · 수락 가능성 {probability}%")
            flash(f"{player['name']} 선수와의 협상이 결렬되었습니다. (협상 횟수 초과 또는 조건 미달)","error")
        save_game(game)
    return redirect(url_for("contracts_page"))

@app.route("/contracts/accept-counter/<offer_id>", methods=["POST"])
def accept_counter_offer(offer_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; fin=game["finances"][team]
    offer=next((o for o in game.get("fa_offers",[]) if o.get("id")==offer_id and o.get("team")==team and o.get("result")=="역제안"),None)
    player=next((p for p in game.get("free_agents",[]) if offer and p["id"]==offer["player_id"]),None)
    if game.get("phase")!="FA 시장": flash("FA 협상은 FA 시장 기간에만 가능합니다.","error")
    elif not offer or not player: flash("유효한 역제안이 없습니다.","error")
    elif len(game["rosters"].get(team, [])) >= roster_total_limit(game): flash("소속선수 정원이 가득 찼습니다.","error")
    elif int(offer["counter"]["salary"]) > fin["cash"]: flash("역제안 연봉이 현금 보유액을 초과합니다.","error")
    else:
        c=offer["counter"]; offer["result"]="역제안 수락"
        for other in game.get("fa_offers", []):
            if other.get("player_id") == player["id"] and other is not offer and other.get("result") == "검토 중": other["result"] = "GM 계약으로 종료"
        sign_player_to_club(game, player, team, c["salary"], c["years"], "역제안 수락")
        game["reports"].insert(0,f"FA 계약 성사(역제안 수락): {player['name']} · {c['years']}년 · 연봉 {c['salary']:,}만원")
        flash(f"{player['name']} 선수와 {c['years']}년 · 연봉 {c['salary']:,}만원에 계약했습니다.","success")
        save_game(game)
    return redirect(url_for("contracts_page"))

def renew_expiring_player(game, club, p):
    """FA 자격 미달 선수의 계약 만료 → 연봉 조정 재계약 (성적·OVR·나이 반영, 규칙서 8.1)."""
    stats=p.get("stats",{})
    performance=0
    if p["position"] in ("SP","RP","CP"):
        performance=(stats.get("W",0)*2 + stats.get("SO",0)*0.05) - (stats.get("ER",0)*0.2)
    else:
        performance=(stats.get("H",0)*0.25 + stats.get("HR",0)*1.5 + stats.get("RBI",0)*0.3)
    factor=1.0 + max(-0.30, min(0.35, (int(p.get("ovr",50))-60)*0.01 + performance*0.004 - max(0,int(p.get("age",28))-32)*0.04))
    if int(game["finances"].get(club,{}).get("cash",0)) < 0 and club != game.get("team"):
        factor=min(factor, 0.97)   # 재정 위기 구단은 연봉 동결·삭감 기조 (규칙서 10.3·15.1)
    new_salary=max(min_salary(game), int(int(p.get("salary",3000))*factor)//100*100)
    p["salary"]=new_salary; p["contract_years"]=1 if int(p.get("age",28))>=33 else random.randint(1,2)
    p["contract"]={"years":p["contract_years"],"salary_by_year":[new_salary]*p["contract_years"],"guaranteed":True,"bonuses":[],"team_option":False,"player_option":False}
    return new_salary

@app.route("/offseason/open-fa", methods=["POST"])
def open_fa_window():
    game = require_game()
    if not game: return redirect(url_for("start_game"))
    if not regular_season_complete(game):
        flash("정규시즌 경기를 모두 마친 뒤에 FA 시장을 열 수 있습니다.", "error")
    elif (game.get("postseason") or {}).get("season") != game.get("season"):
        flash("FA 시장을 열기 전에 포스트시즌을 완료하세요.", "error")
    elif game.get("phase") != "포스트시즌 종료":
        flash("FA 시장은 포스트시즌 종료 직후 한 번만 열 수 있습니다.", "error")
    else:
        game["phase"] = "FA 시장"
        game["fa_days_left"] = int(rule_data(game["season"], "fa_window_days"))
        game.setdefault("free_agents", []); game.setdefault("fa_offers", [])
        if not game.get("fa_market_generated"):
            requirement=fa_service_requirement(game)
            renewals=0; eligible=[]
            for club, roster in game["rosters"].items():
                keep=[]
                for p in roster:
                    if p.get("is_foreign"):
                        keep.append(p); continue  # 외국인 선수는 외국인 메뉴에서 1년 단위 재계약·교체
                    if player_contract_years(p) <= 1:
                        if int(p.get("seasons_registered",0)) >= requirement:
                            p["status"]="FA"; p["previous_team"]=club; p["team"]=None
                            p["salary"]=max(min_salary(game),int(p.get("salary",5000)*random.uniform(1.05,1.35)))
                            p["fa_grade"]="A" if int(p.get("ovr",0))>=75 else "B" if int(p.get("ovr",0))>=65 else "C"
                            eligible.append(p)
                        else:
                            renew_expiring_player(game, club, p); renewals+=1; keep.append(p)
                    else:
                        p["contract_years"]=player_contract_years(p)-1; keep.append(p)
                game["rosters"][club]=keep
            # 재정 위기 AI 구단의 연봉 정리: 현금이 음수면 고연봉 베테랑을 방출해 연봉 부담을 줄인다 (규칙서 10.3).
            for club in game["teams"]:
                if club == game["team"] or int(game["finances"][club].get("cash",0)) >= 0:
                    continue
                veterans=sorted([p for p in game["rosters"][club] if not p.get("is_foreign") and int(p.get("age",28))>=31],key=lambda p:int(p.get("salary",0)),reverse=True)[:3]
                for p in veterans:
                    game["rosters"][club].remove(p)
                    p.update({"status":"FA","former_team":club,"previous_team":club,"team":None,"release_reason":"재정 정리 방출","released_season":game["season"]})
                    p["salary"]=max(min_salary(game),int(int(p.get("salary",3000))*0.7))
                    eligible.append(p)
                if veterans:
                    game.setdefault("reports",[]).insert(0,f"{club} 재정 정리: 고연봉 베테랑 {len(veterans)}명을 방출했습니다.")
            # 기존 방출 선수 + 이번 FA 자격 선수. 실질 협상 가치가 있는 선수 최대 30명으로 제한하고 나머지는 원소속 1년 재계약.
            all_fa = list({p["id"]: p for p in game["free_agents"] + eligible}.values())
            ranked = sorted(all_fa, key=lambda p: (int(p.get("ovr", 0)) * 2 - max(0, int(p.get("age", 28))-30) * 2 + int(p.get("potential", 0)) * 0.25), reverse=True)
            game["free_agents"] = ranked[:30]
            selected_ids = {p["id"] for p in game["free_agents"]}
            for p in all_fa:
                if p["id"] not in selected_ids:
                    club = p.get("previous_team") or p.get("former_team")
                    if club in game["rosters"] and len(game["rosters"][club]) < roster_total_limit(game):
                        p["team"] = club; p["status"] = "퓨처스" if int(p.get("ovr", 50)) < 62 else "1군"
                        renew_expiring_player(game, club, p); game["rosters"][club].append(p)
                    else:
                        retire_player(game, p, club or "무소속", "미계약 은퇴")
            for club in game["teams"]:
                recalc_payroll(game, club); enforce_active_limit(game, club, announce=(club==game["team"]))
            game["fa_market_generated"]=True
            for p in game["free_agents"]:
                fa_player_preference(p)
            generate_ai_fa_offers(game)
            game["reports"].insert(0, f"계약 만료 처리: FA 자격({requirement}시즌 등록) 미달 선수 {renewals}명은 연봉 조정 후 재계약했습니다.")
        game["reports"].insert(0, f"스토브리그 개막: FA 자격 선수 {len(game['free_agents'])}명이 FA 시장에 진입했습니다. 등급(A/B/C)은 게임 기본안입니다.")
        save_game(game)
        flash(f"FA 시장이 열렸습니다. {game['fa_days_left']}일 동안만 계약할 수 있습니다.", "success")
    return redirect(url_for("contracts_page"))

@app.route("/offseason/advance-fa-day", methods=["POST"])
def advance_fa_day():
    game = require_game()
    if not game: return redirect(url_for("start_game"))
    if game.get("phase") != "FA 시장":
        flash("현재 FA 시장이 열려 있지 않습니다.", "error")
    else:
        game["fa_days_left"] = max(0, int(game.get("fa_days_left", 0)) - 1)
        resolve_ai_fa_signings(game)
        generate_ai_fa_offers(game)
        if game["fa_days_left"] == 0:
            game["phase"] = "스토브리그 종료"
            for o in game.get("fa_offers", []):
                if o.get("result") in ("검토 중", "역제안"): o["result"] = "마감 종료"
            game["reports"].insert(0, "FA 시장이 종료되었습니다. 이번 시장에서는 추가 계약을 할 수 없습니다. 미계약 선수는 시즌 개막 전 원소속 복귀 또는 은퇴 처리됩니다.")
            flash("FA 시장이 종료되었습니다.", "info")
        else:
            flash(f"FA 시장 하루 경과 · 남은 기간 {game['fa_days_left']}일", "success")
        save_game(game)
    return redirect(url_for("contracts_page"))

def retire_player(game, p, club, reason):
    """규칙서 14: 은퇴 기록과 통산 기록 보존, 명예의 전당 후보 판정."""
    accumulate_career_record(game, p, club)
    rec=game.get("career_records",{}).get(p["id"],{})
    entry={"id":p["id"],"name":p["name"],"team":club,"age":int(p.get("age",30)),"season":game["season"],"reason":reason,"position":p.get("position"),
           "career":{k:rec.get(k,0) for k in ("seasons","H","HR","RBI","W","SO")}}
    game.setdefault("retired_players",[]).append(entry)
    # 명예의 전당 후보: 통산 성적 기준 (게임 기본안)
    if rec.get("H",0)>=1500 or rec.get("HR",0)>=250 or rec.get("W",0)>=120 or rec.get("SO",0)>=1500:
        game.setdefault("hall_of_fame",[]).append({"name":p["name"],"team":club,"inducted_season":game["season"]+1,"basis":"통산 기록 기준 충족"})
    if p in game.get("free_agents",[]): game["free_agents"].remove(p)

def accumulate_career_record(game, p, club):
    """시즌 기록을 통산 기록에 누적한다 (규칙서 14)."""
    rec=game.setdefault("career_records",{}).setdefault(p["id"],{"id":p["id"],"name":p["name"],"position":p.get("position"),"seasons":0,"G":0,"AB":0,"H":0,"HR":0,"RBI":0,"W":0,"IP":0.0,"ER":0,"SO":0,"teams":[]})
    st=p.get("stats",{})
    if st.get("G",0)>0 or st.get("IP",0)>0:
        rec["seasons"]+=1
        for k in ("G","AB","H","HR","RBI","W","ER","SO"): rec[k]=rec.get(k,0)+int(st.get(k,0))
        rec["IP"]=round(rec.get("IP",0)+float(st.get("IP",0)),1)
    rec["name"]=p["name"]; rec["position"]=p.get("position")
    if club and club not in rec["teams"]: rec["teams"].append(club)
    rec["last_season"]=game["season"]
    return rec

def parent_company_support(game, team, rank, ps_result, history_entry):
    """규칙서 10.2: 기본 지원 + 성적 보너스 + 흥행 보너스 - 재정 페널티, 상·하한과 최소 보장."""
    info=TEAM_INFO[team]; fin=game["finances"][team]
    base=int(info["budget"]*0.65)
    rank_bonus=int(info["budget"]*max(0,(6-rank))*0.02)
    ps_bonus=int(info["budget"]*({"우승":0.10,"한국시리즈":0.06,"플레이오프":0.04,"준플레이오프":0.02,"와일드카드":0.01}.get(ps_result,0)))
    fans=game.get("fan_interest_by_team",{}).get(team,info["fans"])
    fan_bonus=int(info["budget"]*max(0,fans-60)*0.002)
    season_ledger=fin.get("ledger",{}).get(str(game["season"]),{})
    net=sum(season_ledger.values()) if season_ledger else 0
    penalty=int(min(info["budget"]*0.10, abs(net)*0.15)) if net<0 and fin.get("cash",0)<0 else 0
    total=base+rank_bonus+ps_bonus+fan_bonus-penalty
    floor=int(info["budget"]*0.50); ceiling=int(info["budget"]*1.00)
    total=max(floor,min(ceiling,total))
    note=f"기본 {base:,} + 성적 {rank_bonus:,} + 포스트시즌 {ps_bonus:,} + 흥행 {fan_bonus:,} - 페널티 {penalty:,} (하한 {floor:,}/상한 {ceiling:,})"
    return total, note

def apply_salary_cap_check(game, team):
    """규칙서 8.4: 총연봉 한도 초과 시 해당 시즌 규정에 따른 제재(벌금, 연속 초과 시 가중)."""
    cap=salary_cap(game, team); payroll=cap_payroll(game, team)
    violations=game.setdefault("cap_violations",{})
    streak=int(violations.get(team,0))
    if payroll>cap:
        rates=rule_data(game["season"],"cap_penalty_rates")
        rate=rates[min(streak,len(rates)-1)]
        fine=int((payroll-cap)*rate)
        record_transaction(game, team, "총연봉 한도 제재", -fine, f"한도 {cap:,}만원 대비 {payroll-cap:,}만원 초과 · {streak+1}회 연속 · 초과분의 {int(rate*100)}%", f"{game['season']}-01-05", "제재")
        violations[team]=streak+1
        game.setdefault("reports",[]).insert(0,f"총연봉 한도 초과 제재({team}): 벌금 {fine:,}만원" + (" · 다음 드래프트 1라운드 지명권 순위 하락" if streak+1>=2 else ""))
        if streak+1>=2:
            game.setdefault("draft_penalties",{})[team]=game["season"]
        return fine
    violations[team]=0
    return 0

@app.route("/offseason/start-next-season", methods=["POST"])
def start_next_season():
    game = require_game()
    if not game: return redirect(url_for("start_game"))
    if not regular_season_complete(game) or game.get("phase") != "스토브리그 종료":
        flash("정규시즌 종료 후 FA 시장 기간을 마쳐야 새 시즌을 시작할 수 있습니다.", "error")
        return redirect(url_for("schedule_page"))
    draft_state = game.get("draft_state", {})
    if draft_state.get("season") != int(game.get("season", 2027)) or not draft_state.get("complete"):
        flash(f"새 시즌 시작 전에 신인 드래프트 {draft_round_count(game)}라운드를 완료해야 합니다.", "error")
        return redirect(url_for("draft_page"))
    old_season = game["season"]
    # 시즌 결산 1: 구단주 평가가 기록되지 않았다면 자동 기록 (규칙서 16)
    if not any(r.get("season") == old_season for r in game.get("owner_reviews", [])):
        apply_owner_review(game, compute_owner_review(game))
        if game.get("manager_fired"):
            save_game(game)
            flash("구단주 평가 결과 해임되었습니다. 최종 점수를 확인하세요.", "error")
            return redirect(url_for("owner_review"))
    standings_snapshot = json.loads(json.dumps(game["standings"], ensure_ascii=False))
    ranked = league_ranking(game, standings_snapshot)
    ps = game.get("postseason") or {}
    user_ps = ps.get("results", {}).get(game["team"], "미진출") if ps.get("season") == old_season else "미진출"
    history_entry = {
        "season": old_season, "standings": standings_snapshot,
        "game_log": [r for r in game["game_log"] if game["team"] in (r.get("home"), r.get("away"))],
        "user_team_finish": ranked.index(game["team"])+1 if game["team"] in ranked else 10,
        "user_team_record": {k: standings_snapshot[game["team"]].get(k, 0) for k in ("W", "L", "D")},
        "champion": ps.get("champion"), "user_postseason": user_ps,
        "team": game["team"], "goal": club_goal(game)
    }
    game.setdefault("season_history", []).append(history_entry)
    # 시즌 결산 2: 통산 기록 누적, 은퇴 판정 (규칙서 14)
    retired_names = []
    for club in game["teams"]:
        keep = []
        for p in game["rosters"][club]:
            accumulate_career_record(game, p, club)
            age = int(p.get("age", 25)); ovr = int(p.get("ovr", 50))
            played = int(p.get("stats", {}).get("G", 0))
            retire = (age >= 40) or (age >= 36 and ovr < 55) or (age >= 34 and ovr < 48 and played < 20) or (age >= 38 and random.random() < 0.35)
            if retire and not p.get("is_foreign"):
                retire_player(game, p, club, "기량 하락·연령에 따른 은퇴")
                if club == game["team"]: retired_names.append(p["name"])
            else:
                keep.append(p)
        game["rosters"][club] = keep
    # 시즌 결산 3: 미계약 FA 처리 (원소속 복귀 또는 은퇴)
    for p in list(game.get("free_agents", [])):
        club = p.get("previous_team") or p.get("former_team")
        if int(p.get("age", 30)) >= 33:
            retire_player(game, p, club or "무소속", "미계약 은퇴")
        elif not club or club not in game["rosters"] or len(game["rosters"][club]) >= roster_total_limit(game):
            accumulate_career_record(game, p, club)
            game.setdefault("released_players", []).append({"id": p["id"], "name": p["name"], "team": club, "age": int(p.get("age", 25)), "season": old_season, "reason": p.get("release_reason") or "미계약 방출"})
            game["free_agents"].remove(p)
        else:
            p["team"] = club; p["status"] = "퓨처스"; p["salary"] = max(min_salary(game), int(p.get("salary", 3000) * 0.8)); p["contract_years"] = 1
            p.pop("fa_preferences", None); p.pop("negotiations", None)
            game["rosters"][club].append(p); game["free_agents"].remove(p)
    game["free_agents"] = []
    game["released_players"] = game.get("released_players", [])[-400:]
    # 시즌 결산 4: 모기업 지원금 (규칙서 10.2) — 다음 시즌 지원은 직전 시즌 평가로 결정
    for club in game["teams"]:
        rank = ranked.index(club) + 1
        ps_result = ps.get("results", {}).get(club, "미진출") if ps.get("season") == old_season else "미진출"
        amount, note = parent_company_support(game, club, rank, ps_result, history_entry)
        record_transaction(game, club, "모기업 지원금", amount, note, f"{old_season+1}-01-02", "모기업 지원")
        if game["finances"][club]["cash"] < 0:
            # 최소 운영 보장: 적자의 절반을 특별 목적 투자로 메우되 상한을 둔다. 재정 평판은 구단주 평가에서 감점된다.
            rescue = int(min(TEAM_INFO[club]["budget"] * 0.4, -game["finances"][club]["cash"] * 0.7))
            record_transaction(game, club, "모기업 특별 지원", rescue, "재정 위기 최소 운영 보장 (적자 70% 보전, 상한 예산 40%)", f"{old_season+1}-01-02", "모기업 지원")
            game.setdefault("finance_reputation", {})[club] = int(game.get("finance_reputation", {}).get(club, 0)) + 1
        game["finances"][club]["budget"] = int(TEAM_INFO[club]["budget"])
    own_support = next((t for t in reversed(game["finances"][game["team"]]["transactions"]) if t["type"] == "모기업 지원금"), None)
    game["season"] = old_season + 1
    game["fa_market_generated"] = False; game["fa_offers"] = []
    rule_state = game.setdefault("rule_state", STATE_DEFAULT.copy())
    rule_state["season"] = game["season"]
    for active_rule in rule_state.get("active_rules", []):
        if active_rule.get("status") == "시행 예정" and active_rule.get("effective_season", 9999) <= game["season"]:
            active_rule["status"] = "시행 중"
    game["week"] = 0
    game["day_index"] = 0
    game["processed_week_ids"] = []
    game["schedule"] = build_schedule(game["teams"], game["season"])
    game["standings"] = {t:{"W":0,"L":0,"D":0,"RS":0,"RA":0,"streak":"-"} for t in game["teams"]}
    game["game_log"] = []
    game["postponed_games"] = []
    game["phase"] = "정규시즌"
    game["fa_days_left"] = 0
    game["trade_offers"] = [o for o in game.get("trade_offers", []) if o.get("status") != "pending"]
    # 시즌 개막 준비: 규정 효과 적용, 연봉·정원 점검, 나이·등록 시즌 갱신
    active_ids = active_rule_ids(game)
    floor_salary = min_salary(game)
    for team in game["teams"]:
        fin = game["finances"][team]
        for p in game["rosters"][team]:
            p["team"] = team
            old_salary = int(p.get("salary", 0))
            if old_salary < floor_salary:
                p["salary"] = floor_salary
                record_transaction(game, team, "최저연봉 보정", 0, f"{p['name']} 최저연봉 {floor_salary:,}만원 적용" + (" (R-17)" if "R-17" in active_ids else ""), f"{game['season']}-01-03", "보정")
            p["stats"] = {"G":0,"AB":0,"H":0,"HR":0,"RBI":0,"W":0,"IP":0.0,"ER":0,"SO":0}
            p["fatigue"] = max(0, p.get("fatigue", 0) - 15)
            p["age"] = p.get("age", 25) + 1
            p["seasons_registered"] = p.get("seasons_registered", 0) + (1 if p.get("status") == "1군" else 0)
            p["injury"] = None; p["injury_days"] = 0; p["rehab_score"] = 0
        recalc_payroll(game, team)
        ai_manage_roster(game, team, fill_only=True)   # 스프링캠프: 감독·코치가 개막 엔트리 구성 (규칙서 3)
        enforce_active_limit(game, team, announce=(team == game["team"]))
        apply_salary_cap_check(game, team)
        if rule_is_active(game, "R-05") and cap_payroll(game, team) < salary_cap(game) * 0.8:
            game.setdefault("rule_compliance", []).append({"season":game["season"],"team":team,"rule_id":"R-05","status":"개선 계획 필요","payroll":cap_payroll(game, team),"threshold":int(salary_cap(game)*0.8)})
    # 스태프 계약·노화 (규칙서 11.4·11.5)
    advance_staff_season(game)
    game["injury_list"] = []
    game["weekly_reports"] = []
    game["foreign_market_generated"] = False
    club_goal(game)
    active_names = [r.get("name", r.get("rule_id")) for r in rule_state.get("active_rules", []) if r.get("status") == "시행 중"]
    if active_names:
        game["reports"].insert(0, "시행 중 규정: " + ", ".join(active_names))
    if retired_names:
        game["reports"].insert(0, f"은퇴: {', '.join(retired_names[:6])}" + (f" 외 {len(retired_names)-6}명" if len(retired_names) > 6 else ""))
    if own_support:
        game["reports"].insert(0, f"모기업 지원금 {own_support['amount']:,}만원 입금 · {own_support['note']}")
    game["reports"].insert(0, f"{game['season']}시즌 개막 준비 완료. 구단 목표: {club_goal(game)}. 정규시즌 일정이 새로 편성되었습니다.")
    save_game(game)
    flash(f"{game['season']}시즌을 시작했습니다. 구단 목표는 '{club_goal(game)}'입니다.", "success")
    return redirect(url_for("office"))

def trade_value(game, player, for_team=None):
    """규칙서 9.2: 능력·나이·잠재력·포지션 수요·연봉/계약·부상 위험·구단 전략을 종합한 거래 가치."""
    ovr=int(player.get("ovr",50)); age=int(player.get("age",28)); pot=int(player.get("potential",ovr))
    value=ovr*1.0 + max(0,pot-ovr)*0.35
    value += (6 if age<=25 else 3 if age<=28 else 0 if age<=31 else -4 if age<=34 else -8)
    salary=int(player.get("salary",3000)); years=player_contract_years(player)
    value -= max(0, salary-15000)/6000            # 고액 연봉 부담
    value -= 2 if years>=4 and age>=31 else 0     # 장기 계약 베테랑 위험
    value -= 3 if player.get("injury") else 0
    value -= min(4, len(player.get("injury_history",[]))*1.2)
    if for_team:
        active=active_roster(game, for_team)
        same=[p for p in active if p["position"]==player["position"]]
        if not same: value += 4                   # 희소 포지션
        elif ovr > max(p["ovr"] for p in same): value += 2
        rank=league_ranking(game).index(for_team)+1
        if rank>=8 and age<=26: value += 3        # 리빌딩 구단은 젊은 선수 선호
        if rank<=3 and ovr>=70: value += 3        # 우승 도전 구단은 즉시 전력 선호
    return round(value,1)

def ai_trade_response(game, rival, give_ours, get_theirs):
    """상대 AI 구단의 판정: 받는 가치 vs 주는 가치 + 전략·리스크. 수락 / 역제안 / 거절."""
    incoming=sum(trade_value(game,p,rival) for p in give_ours)
    outgoing=sum(trade_value(game,p,rival) for p in get_theirs)
    diff=incoming-outgoing
    if diff >= -2:
        return "accept", diff
    if diff >= -9 and random.random() < 0.6:
        return "counter", diff
    return "reject", diff

@app.route("/trade-room/activity", methods=["POST"])
def trade_room_activity():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]
    rivals=[t for t in game["teams"] if t != team]
    random.shuffle(rivals)
    created=0
    for rival in rivals:
        if created>=2: break
        theirs=[p for p in game["rosters"][rival] if p.get("status")=="1군" and not p.get("injury")]
        ours=[p for p in game["rosters"][team] if p.get("status")=="1군" and not p.get("injury")]
        if not theirs or not ours: continue
        their_player=random.choice(theirs)
        tv=trade_value(game, their_player, team)
        candidates=sorted(ours,key=lambda p:abs(trade_value(game,p,rival)-tv))[:min(6,len(ours))]
        our_player=random.choice(candidates)
        offer={"id":game.get("next_trade_id",1),"from_team":rival,"to_team":team,"give_player_ids":[their_player["id"]],"want_player_ids":[our_player["id"]],"give_player_names":[their_player["name"]],"want_player_names":[our_player["name"]],"status":"pending","kind":"incoming","created_week":game.get("week",0),"season":game["season"],"thread":[]}
        game["next_trade_id"]=offer["id"]+1
        rank=league_ranking(game).index(rival)+1
        strategy="리빌딩 중이라 젊은 자원을 찾고 있습니다." if rank>=8 else "우승 도전을 위해 즉시 전력이 필요합니다." if rank<=3 else "포지션 뎁스 보강이 목적입니다."
        add_trade_message(offer,rival+" 단장",random.choice([f"{their_player['name']} 선수와 {our_player['name']} 선수의 맞트레이드를 검토해 주십시오. {strategy}",f"팀 내 포지션 보강을 위해 {our_player['name']} 선수에 관심이 있습니다. {their_player['name']} 선수를 제안합니다. {strategy}",f"최근 전력 구성을 검토했습니다. {their_player['name']} 선수와 {our_player['name']} 선수의 교환은 어떻습니까? {strategy}"]))
        game.setdefault("trade_offers",[]).append(offer); created+=1
    headlines=[f"{rival} 단장: 트레이드 시장에서 전력 보강 검토 중" for rival in random.sample(rivals,min(3,len(rivals)))]
    game.setdefault("league_chat",[]).extend([{"speaker":"리그 단톡 알림","message":h,"time":datetime.now().strftime("%m-%d %H:%M")} for h in headlines])
    game["league_chat"]=game["league_chat"][-40:]
    game["trade_offers"]=game["trade_offers"][-60:]
    game.setdefault("reports",[]).insert(0,f"단장 단톡 업데이트: 신규 트레이드 제안 {created}건, 리그 소식 {len(headlines)}건")
    save_game(game)
    flash(f"단장 단톡이 업데이트됐습니다. 신규 제안 {created}건과 리그 소식 {len(headlines)}건.","success")
    return redirect(url_for("trade_room"))

@app.route("/trade-room")
def trade_room():
    game = require_game()
    if not game: return redirect(url_for("start_game"))
    team = game["team"]
    rivals = [t for t in game["teams"] if t != team]
    eligible_ours = [p for p in game["rosters"][team] if p["status"] == "1군" and not p.get("injury")]
    for p in eligible_ours: p["trade_value"] = trade_value(game, p)
    return render_template("trade_room.html", game=game, team=team, rivals=rivals,
        eligible_ours=eligible_ours, roster_lookup={t:game["rosters"][t] for t in rivals})

@app.route("/trade/propose", methods=["POST"])
def propose_trade():
    game = require_game()
    if not game: return redirect(url_for("start_game"))
    team = game["team"]
    rival = request.form.get("rival")
    our_id = request.form.get("our_player")
    their_id = request.form.get("their_player")
    ours = find_player(game, team, our_id)
    theirs = find_player(game, rival, their_id) if rival in game["teams"] else None
    if rival == team or not ours or not theirs or ours.get("status") != "1군" or theirs.get("status") != "1군":
        flash("트레이드 대상 구단과 1군 선수를 올바르게 선택해 주세요.", "error")
        return redirect(url_for("trade_room"))
    if game.get("phase") not in ("정규시즌", "스토브리그 종료", "FA 시장", "포스트시즌 종료", "시즌 종료"):
        flash("현재 단계에서는 트레이드를 제안할 수 없습니다.", "error")
        return redirect(url_for("trade_room"))
    offer = {"id":game["next_trade_id"], "from_team":team, "to_team":rival,
        "give_player_ids":[ours["id"]], "want_player_ids":[theirs["id"]],
        "give_player_names":[ours["name"]], "want_player_names":[theirs["name"]], "status":"pending",
        "kind":"outgoing", "created_week":game.get("week",0), "season":game["season"], "thread":[]}
    game["next_trade_id"] += 1
    add_trade_message(offer, team + " 단장", f"{ours['name']} 선수와 {theirs['name']} 선수의 1:1 트레이드를 제안합니다.")
    decision, diff = ai_trade_response(game, rival, [ours], [theirs])
    if decision == "accept":
        success, message = trade_players(game, offer)
        if success:
            offer["status"] = "accepted"
            add_trade_message(offer, rival + " 단장", f"제안을 수락합니다. {message}")
        else:
            offer["status"] = "rejected"
            add_trade_message(offer, rival + " 단장", message)
    elif decision == "counter":
        # 역제안: 가치 격차를 메울 우리 쪽 추가 선수(퓨처스 유망주)를 요구한다 (규칙서 9.2 복잡한 거래는 역제안 허용)
        need = abs(diff)
        extras = sorted([p for p in game["rosters"][team] if p["status"]=="퓨처스" and not p.get("injury") and p["id"] != ours["id"]], key=lambda p: abs(trade_value(game,p,rival)-need))
        if extras:
            extra = extras[0]
            offer["status"] = "counter"
            offer["counter_want_ids"] = [ours["id"], extra["id"]]
            offer["counter_want_names"] = [ours["name"], extra["name"]]
            add_trade_message(offer, rival + " 단장", f"가치 차이가 있습니다. {ours['name']} 선수에 더해 퓨처스 {extra['name']} 선수를 포함해 주시면 {theirs['name']} 선수를 보내겠습니다.")
        else:
            offer["status"] = "rejected"
            add_trade_message(offer, rival + " 단장", "선수 가치 차이가 있어 이번 제안은 받기 어렵겠습니다. 다른 조건을 검토해 주세요.")
    else:
        offer["status"] = "rejected"
        add_trade_message(offer, rival + " 단장", f"현재 팀 전력상 이 제안은 수용하기 어렵습니다. (가치 평가 차이 {abs(diff):.0f})")
    game["trade_offers"].append(offer)
    save_game(game)
    flash("단장 단톡방에 상대 구단의 답변이 도착했습니다.", "success")
    return redirect(url_for("trade_room"))

@app.route("/trade/<int:trade_id>/respond", methods=["POST"])
def respond_trade(trade_id):
    game = require_game()
    if not game: return redirect(url_for("start_game"))
    offer = next((o for o in game.get("trade_offers", []) if o["id"] == trade_id), None)
    decision = request.form.get("decision")
    if not offer or offer["status"] not in ("pending", "counter"):
        flash("응답할 수 있는 트레이드 제안이 아닙니다.", "error")
        return redirect(url_for("trade_room"))
    if offer["status"] == "counter" and offer.get("from_team") == game["team"]:
        # 우리가 보낸 제안에 대한 상대 역제안 수락/거절
        if decision == "accept":
            # 역제안 구조: 우리(from_team)가 counter_want 선수들을 주고 원래 요청한 상대 선수를 받는다.
            offer["give_player_ids"] = list(offer.get("counter_want_ids", offer["give_player_ids"]))
            offer["give_player_names"] = list(offer.get("counter_want_names", offer["give_player_names"]))
            ok, message = trade_players(game, offer)
            offer["status"] = "accepted" if ok else "rejected"
            add_trade_message(offer, game["team"] + " 단장", "역제안을 수락합니다." if ok else message)
            flash(message, "success" if ok else "error")
        else:
            offer["status"] = "rejected"
            add_trade_message(offer, game["team"] + " 단장", "역제안은 받아들이기 어렵습니다.")
            flash("역제안을 거절했습니다.", "info")
        save_game(game)
        return redirect(url_for("trade_room"))
    if offer["to_team"] != game["team"]:
        flash("응답할 수 있는 트레이드 제안이 아닙니다.", "error")
        return redirect(url_for("trade_room"))
    if decision == "accept":
        ok, message = trade_players(game, offer)
        offer["status"] = "accepted" if ok else "rejected"
        add_trade_message(offer, game["team"] + " 단장", "제안을 수락합니다." if ok else message)
        flash(message, "success" if ok else "error")
    else:
        offer["status"] = "rejected"
        add_trade_message(offer, game["team"] + " 단장", "이번 제안은 거절하겠습니다.")
        flash("트레이드 제안을 거절했습니다.", "info")
    save_game(game)
    return redirect(url_for("trade_room"))

# 실제 한국 프로야구에서 활동한 감독·코치·프런트 인물 이름을 후보 데이터로 사용합니다.
# 현 소속 및 직책을 보증하는 공식 실시간 인명부가 아니라, 게임 내 실명 후보 풀입니다.
REAL_STAFF_BY_ROLE = {
 "감독": ["염경엽","김경문","이범호","이강철","박진만","김태형","이숭용","이승엽","강인권","홍원기","류지현","김원형"],
 "수석 코치": ["정경배","김한수","양상문","이종범","조인성","장원진","김민호","이종운"],
 "투수 코치": ["정민태","조웅천","최일언","김상엽","박정진","정재훈","배영수","권명철","강상수"],
 "타격 코치": ["박용택","김종국","이진영","김재현","이숭용","김한수","장종훈","최경철"],
 "수비·주루 코치": ["김민재","김민호","김광림","정수성","이종욱","박재상","김동수"],
 "불펜 코치": ["가득염","조웅천","최일언","강상수","정재훈","배영수"],
 "배터리 코치": ["진갑용","조인성","김동수","강인권","최경철","박경완"],
 "육성 총괄": ["김태룡","차명석","손혁","이종열","박종훈","김용희"],
 "고교 스카우트 팀장": ["윤석환","김용희","김진섭","이종열","김태룡"],
 "외국인 스카우트 팀장": ["민훈기","이종열","손혁","차명석","김용희"],
 "홍보팀 팀장": ["김태룡","차명석","손혁","이종열"],
 "단장 보좌": ["김태룡","차명석","손혁","이종열","박종훈"],
 "전력분석 팀장": ["이종열","김용희","김진섭","윤석환"],
 "데이터 분석관": ["민훈기","윤석환","김진섭","이종열"]
}

# 규칙서 11.1 주요 보직. (보직, 기본 전문성, 기본 연봉(만원))
STAFF_ROLE_BASELINE = [("감독",78,10000),("투수 코치",74,5200),("타격 코치",73,4800),("수비·주루 코치",70,4400),("육성 총괄",76,4200),("고교 스카우트 팀장",72,3600),("외국인 스카우트 팀장",72,3800),("홍보팀 팀장",68,3400),("단장 보좌",70,4000)]
STAFF_ROLES = [r for r, _, _ in STAFF_ROLE_BASELINE]

def staff_grade(skill):
    """규칙서 11.2: D·C·B·A·S 5단계."""
    return "S" if skill>=85 else "A" if skill>=75 else "B" if skill>=62 else "C" if skill>=50 else "D"

def staff_market_salary(skill, role, game=None):
    base = next((s for r, _, s in STAFF_ROLE_BASELINE if r == role), 4000)
    salary = int(base * (0.5 + skill / 100))
    if game:
        fin = game["finances"][game["team"]]
        # 구단 재정 상황 반영: 현금이 적으면 협상 여지, 많으면 요구액 상승 (규칙서 11.4)
        salary = int(salary * (1.08 if fin.get("cash", 0) > TEAM_INFO[game["team"]]["budget"] * 0.6 else 0.95 if fin.get("cash", 0) < 0 else 1.0))
    return max(2000, salary // 100 * 100)

def make_staff_candidate(game, role=None):
    role = role or random.choice(STAFF_ROLES)
    skill = random.randint(48, 90)
    true_skills = {"전문성":skill,"리더십":random.randint(40,90),"협업":random.randint(40,90)}
    # 기본 평가(무료)는 오차가 크고, 정밀 평가 후 오차가 줄어든다 (규칙서 11.3).
    est = {k: max(20, min(99, v + random.randint(-12, 12))) for k, v in true_skills.items()}
    return {"id":uuid.uuid4().hex[:12], "role":role, "name":random.choice(REAL_STAFF_BY_ROLE.get(role, ["김용희","김민호","정경배","조인성"])),
            "grade":staff_grade(est["전문성"]), "skills":true_skills, "estimated":est, "evaluated":False,
            "salary":staff_market_salary(skill, role), "contract_years":random.randint(1,3), "age":random.randint(32,62), "health":random.randint(70,100), "career_seasons":random.randint(1,25), "status":"영입 후보"}

def ensure_staff_pool(game):
    pool = game.setdefault("staff_pool", [])
    for item in pool:
        if item.get("role") not in STAFF_ROLES:
            # 구버전 후보 보직명을 규칙서 보직으로 맞춘다.
            mapping = {"수석 스카우트":"고교 스카우트 팀장","외국인 스카우트":"외국인 스카우트 팀장","수석 코치":"수비·주루 코치","불펜 코치":"투수 코치","배터리 코치":"수비·주루 코치","데이터 분석관":"단장 보좌","전력분석 팀장":"단장 보좌"}
            item["role"] = mapping.get(item.get("role"), random.choice(STAFF_ROLES))
        item.setdefault("estimated", {k: v for k, v in item.get("skills", {}).items()})
        item.setdefault("evaluated", False)
    while len(pool) < 36:
        pool.append(make_staff_candidate(game))
    return pool

def advance_staff_season(game):
    """규칙서 11.4·11.5: 계약 기간 차감·재계약 협상, 경험 성장, 나이·건강에 따른 하락."""
    staff = game.setdefault("staff", {})
    for role, s in list(staff.items()):
        s["age"] = int(s.get("age", 45)) + 1
        s["career_seasons"] = int(s.get("career_seasons", 5)) + 1
        skills = s.setdefault("skills", {"전문성":60,"리더십":60,"협업":60})
        # 성장: 경력 초반·젊을수록, 하락: 나이·건강
        if s["age"] < 50 and random.random() < 0.45:
            skills["전문성"] = min(99, skills["전문성"] + 1)
        elif s["age"] >= 58 and random.random() < 0.5:
            skills["전문성"] = max(30, skills["전문성"] - 1)
        if int(s.get("health", 90)) < 70 and random.random() < 0.5:
            skills["전문성"] = max(30, skills["전문성"] - 1)
        s["health"] = max(40, min(100, int(s.get("health", 90)) + random.randint(-6, 3)))
        s["grade"] = staff_grade(skills["전문성"])
        s["contract_years"] = int(s.get("contract_years", 1)) - 1
        if s["contract_years"] <= 0:
            # 재계약 협상: 시장 가치로 조건 갱신 (자동 재계약, 단장은 이후 방출 가능)
            new_salary = staff_market_salary(skills["전문성"], role, game)
            s["salary"] = new_salary; s["contract_years"] = random.randint(1, 3)
            game.setdefault("reports", []).insert(0, f"스태프 재계약: {s['name']} ({role}) · 연봉 {new_salary:,}만원 · {s['contract_years']}년")
    for role, base_skill, salary in STAFF_ROLE_BASELINE:
        if role not in staff:
            game.setdefault("reports", []).insert(0, f"프런트 공석: {role} 자리가 비어 있습니다. 감독·프런트 메뉴에서 영입하세요.")

@app.route("/staff")
def staff_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    staff=list(game.get("staff", {}).values())
    real_names={name for names in REAL_STAFF_BY_ROLE.values() for name in names}
    for item in staff:
        item["skill"] = item.get("skills", {}).get("전문성", 50)
        item["grade"] = staff_grade(item["skill"])
        if item.get("name") not in real_names:
            item["name"] = random.choice(REAL_STAFF_BY_ROLE.get(item.get("role"), ["김용희","김민호","정경배","조인성"]))
    pool=ensure_staff_pool(game)
    for candidate in pool:
        if candidate.get("name") not in real_names:
            candidate["name"] = random.choice(REAL_STAFF_BY_ROLE.get(candidate.get("role"), ["김용희","김민호","정경배","조인성"]))
    vacancies=[r for r in STAFF_ROLES if r not in game.get("staff", {})]
    save_game(game)
    return render_template("staff_game.html",game=game,team=game["team"],staff=staff,staff_pool=pool,vacancies=vacancies,eval_cost=STAFF_EVAL_COST,roles=STAFF_ROLES)

STAFF_EVAL_COST = 500  # 정밀 평가 비용(만원), 게임 기본안

@app.route("/staff/evaluate/<staff_id>", methods=["POST"])
def evaluate_staff(staff_id):
    """규칙서 11.3: 기본 평가는 무료, 정밀 평가는 비용이 들고 정확도가 올라가되 오차는 남는다."""
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    pool=ensure_staff_pool(game)
    candidate=next((x for x in pool if x["id"]==staff_id),None)
    fin=game["finances"][game["team"]]
    if not candidate: flash("해당 후보가 없습니다.","error")
    elif candidate.get("evaluated"): flash("이미 정밀 평가를 마친 후보입니다.","info")
    elif fin.get("cash",0) < STAFF_EVAL_COST: flash("정밀 평가 비용을 지불할 현금이 부족합니다.","error")
    else:
        evaluator=game.get("staff",{}).get("단장 보좌",{})
        accuracy=max(2, 6 - int(evaluator.get("skills",{}).get("전문성",60))//25)
        candidate["estimated"]={k: max(20,min(99,v+random.randint(-accuracy,accuracy))) for k,v in candidate["skills"].items()}
        candidate["evaluated"]=True; candidate["grade"]=staff_grade(candidate["estimated"]["전문성"])
        record_transaction(game, game["team"], "스태프 정밀 평가", -STAFF_EVAL_COST, f"{candidate['name']} ({candidate['role']}) 정밀 평가", None, "스카우팅")
        save_game(game); flash(f"{candidate['name']} 후보 정밀 평가 완료 (오차 ±{accuracy}).","success")
    return redirect(url_for("staff_page"))

@app.route("/staff/hire/<staff_id>", methods=["POST"])
def hire_staff(staff_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    pool=ensure_staff_pool(game)
    candidate=next((x for x in pool if x["id"]==staff_id),None)
    if not candidate:
        flash("해당 직원은 영입 풀에 없습니다.","error")
    else:
        role=candidate["role"]
        fin=game["finances"][game["team"]]
        try:
            years=max(1,min(4,int(request.form.get("years",candidate.get("contract_years",2)))))
            offered=int(request.form.get("salary",candidate["salary"]))
        except (ValueError,TypeError):
            years=candidate.get("contract_years",2); offered=candidate["salary"]
        ask=staff_market_salary(candidate["skills"]["전문성"], role, game)
        if offered < int(ask*0.9):
            flash(f"{candidate['name']} 후보가 제시 연봉을 거절했습니다. 시장 가치 약 {ask:,}만원 수준을 요구합니다.","error")
            return redirect(url_for("staff_page"))
        old=game.setdefault("staff",{}).get(role)
        compensation=0
        if old:
            compensation=int(int(old.get("salary",0))*max(0,int(old.get("contract_years",0)))*0.5)
            if fin.get("cash",0) < compensation:
                flash(f"기존 {role} 중도 해임 보상금 {compensation:,}만원을 지급할 현금이 부족합니다.","error")
                return redirect(url_for("staff_page"))
            old["release_reason"]="교체"; old["status"]="영입 후보"; old["estimated"]=dict(old.get("skills",{})); old["evaluated"]=True
            pool.append(old)
            if compensation: record_transaction(game, game["team"], "스태프 해임 보상", -compensation, f"{old['name']} ({role}) 잔여 계약 보상", None, "스태프 보상")
        candidate["salary"]=offered; candidate["contract_years"]=years; candidate["status"]="재직"
        game["staff"][role]=candidate
        pool.remove(candidate)
        game.setdefault("reports",[]).insert(0,f"프런트 영입: {candidate['name']} ({role}, 연봉 {offered:,}만원 · {years}년)" + (f" · 전임자 보상 {compensation:,}만원" if compensation else ""))
        flash(f"{candidate['name']} 직원을 {role}로 영입했습니다.","success")
        save_game(game)
    return redirect(url_for("staff_page"))

@app.route("/staff/release/<staff_id>", methods=["POST"])
def release_staff(staff_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    staff=game.setdefault("staff",{})
    role=next((r for r,x in staff.items() if x.get("id")==staff_id),None)
    if not role:
        flash("방출할 직원을 찾을 수 없습니다.","error")
    else:
        person=staff[role]
        compensation=int(int(person.get("salary",0))*max(0,int(person.get("contract_years",0)))*0.5)
        fin=game["finances"][game["team"]]
        if fin.get("cash",0) < compensation:
            flash(f"중도 해임 보상금 {compensation:,}만원을 지급할 현금이 부족합니다.","error")
            return redirect(url_for("staff_page"))
        staff.pop(role)
        person["release_reason"]="단장 방출"; person["status"]="영입 후보"; person["estimated"]=dict(person.get("skills",{})); person["evaluated"]=True
        game.setdefault("staff_pool",[]).append(person)
        if compensation: record_transaction(game, game["team"], "스태프 해임 보상", -compensation, f"{person['name']} ({role}) 잔여 계약 {person.get('contract_years',0)}년 보상", None, "스태프 보상")
        game.setdefault("reports",[]).insert(0,f"프런트 인사: {person['name']} ({role}) 직원을 방출했습니다." + (f" 계약 보상 {compensation:,}만원." if compensation else ""))
        save_game(game); flash(f"{person['name']} 직원을 방출했습니다." + (f" 보상금 {compensation:,}만원을 지급했습니다." if compensation else ""),"success")
    return redirect(url_for("staff_page"))

@app.route("/weekly-report")
def weekly_report_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    reports=[r for r in game.get("weekly_reports",[]) if r.get("team")==game["team"]]
    return render_template("weekly_report.html",game=game,team=game["team"],reports=reports[::-1][:20])

@app.route("/history")
def history_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; st=game["standings"][team]
    roster=active_roster(game,team)
    batters=sorted([p for p in roster if p["position"] not in ("SP","RP","CP")],key=lambda p:(p["stats"]["H"],p["stats"]["HR"]),reverse=True)
    pitchers=sorted([p for p in roster if p["position"] in ("SP","RP","CP")],key=lambda p:p["stats"]["IP"],reverse=True)
    return render_template("history_game.html",game=game,team=team,standing=st,logs=game["game_log"][-30:][::-1],batters=batters,pitchers=pitchers)


def draft_standings_order(game):
    """직전 시즌 하위 팀부터 지명한다. 동률이면 득실차가 낮은 팀이 먼저다.
    R-02(로터리픽) 시행 시 하위 3개 구단의 1라운드 순서를 추첨한다. 총연봉 한도 2회 연속 초과 구단은 1라운드 지명권이 뒤로 밀린다."""
    base = sorted(game.get("teams", []), key=lambda t: (
        game.get("standings", {}).get(t, {}).get("W", 0) / max(1, sum(game.get("standings", {}).get(t, {}).get(k, 0) for k in ("W", "L", "D"))),
        game.get("standings", {}).get(t, {}).get("RS", 0) - game.get("standings", {}).get(t, {}).get("RA", 0)
    ))
    order = list(base)
    lottery = None
    if rule_is_active(game, "R-02"):
        bottom = order[:3]; random.shuffle(bottom); order = bottom + order[3:]
        lottery = {"candidates": base[:3], "result": bottom}
    penalized = [t for t, season in game.get("draft_penalties", {}).items() if season == game.get("season")]
    for t in penalized:
        if t in order:
            order.remove(t); order.append(t)
    return order, lottery

def ensure_draft(game):
    draft = game.setdefault("draft_state", {})
    if draft.get("season") != int(game.get("season", 2027)):
        pool=[]
        pool_size=int(rule_data(game["season"], "draft_pool_size"))
        scout = game.get("staff", {}).get("고교 스카우트 팀장", {})
        skill = int(scout.get("skills", {}).get("전문성", 55))
        leadership = int(scout.get("skills", {}).get("리더십", 55))
        accuracy = max(0.25, min(0.9, (skill * 0.65 + leadership * 0.35) / 100))
        for i in range(pool_size):
            p=new_player("신인 드래프트", i, random.randint(45,68), True)
            p["status"]="신인 드래프트"; p["age"]=random.randint(18,22); p["salary"]=min_salary(game); p["seasons_registered"]=0
            p["ovr"]=max(42,min(72,int(p.get("ovr",55))))
            p["potential"]=max(p["ovr"]+3,min(94,p["ovr"]+3+min(22,int(random.expovariate(1/8.0)))))
            # 스카우팅은 잠재력을 바꾸지 않고 정보 정확도와 숨은 유망주 발견 가능성을 높인다 (규칙서 9.3)
            error = int(round((1.0 - accuracy) * 18))
            hidden_gem = random.random() < 0.08
            if hidden_gem: p["potential"] = min(98, p["potential"] + random.randint(4, 10))
            estimate = max(35, min(80, int(p["ovr"]) + random.randint(-max(2,error), max(2,error))))
            pot_estimate = max(estimate, min(98, int(p["potential"]) + random.randint(-max(3,error+2), max(3,error+2))))
            if hidden_gem and random.random() > accuracy:
                pot_estimate = max(estimate, pot_estimate - random.randint(8, 14))  # 역량이 낮은 스카우트는 숨은 유망주를 놓친다
            p["scout_estimate"] = estimate
            p["scout_potential_estimate"] = pot_estimate
            p["scout_confidence"] = int(round(accuracy * 100))
            delta = estimate - int(p["ovr"])
            if delta >= 7: verdict = "팀장 평가가 후한 편입니다. 즉시 전력감으로 보고 있습니다."
            elif delta <= -7: verdict = "팀장 평가가 박한 편입니다. 원석의 가치를 낮게 볼 수 있습니다."
            elif estimate >= 64: verdict = "기본기가 탄탄하다는 평가입니다. 성장 속도는 지켜볼 필요가 있습니다."
            elif pot_estimate - estimate >= 15: verdict = "잠재력은 높게 보지만 현재 완성도는 낮다는 평가입니다."
            else: verdict = "무난한 후보라는 평가입니다. 포지션 적합성과 성장 환경이 중요합니다."
            p["development_report"] = f"{verdict} 예상 OVR {max(35,estimate-4)}~{min(85,estimate+4)} · 잠재력 추정 {max(estimate,pot_estimate-6)}~{min(99,pot_estimate+6)} · 평가 신뢰도 {p['scout_confidence']}%"
            p["true_ovr_hidden"] = True
            pool.append(p)
        order, lottery = draft_standings_order(game)
        draft.update({"season":int(game.get("season",2027)),"round":1,"pick_index":0,"order":order,"rounds":draft_round_count(game),"lottery":lottery,"picks":[],"complete":False})
        game["draft_pool"]=pool
        if lottery:
            game.setdefault("reports",[]).insert(0,f"로터리픽(R-02) 추첨 결과: 1~3순위 {', '.join(lottery['result'])}")
    draft.setdefault("rounds", draft_round_count(game))
    game.setdefault("draft_pool", [])
    return draft

def draft_assign(game, draft, p, club, round_no, idx):
    """지명 확정: 신인 계약(계약금은 라운드별), 정원 초과 시 퓨처스 최저 OVR 정리."""
    pool=game["draft_pool"]
    years=int(rule_data(game["season"], "rookie_contract_years"))
    roster=game["rosters"].setdefault(club,[])
    total_limit=roster_total_limit(game)
    if len(roster)>=total_limit:
        replaceable=sorted([x for x in roster if x.get("status")=="퓨처스" and not x.get("is_foreign")],key=lambda x:int(x.get("ovr",0)))
        if not replaceable:
            return False
        cut=replaceable[0]; roster.remove(cut)
        cut.update({"status":"FA","former_team":club,"previous_team":club,"team":None,"release_reason":"신인 드래프트 선수단 정리","released_season":game["season"]})
        game.setdefault("free_agents",[]).append(cut)
    pool.remove(p)
    p["team"]=club; p["status"]="퓨처스"; p["contract_years"]=years; p["draft_round"]=round_no; p["draft_season"]=game["season"]
    p["salary"]=min_salary(game)
    p["contract"]={"years":years,"salary_by_year":[p["salary"]]*years,"guaranteed":True,"bonuses":[],"team_option":False,"player_option":False}
    signing_bonus={1:30000,2:15000,3:9000,4:6000,5:4000}.get(round_no,2500)
    roster.append(p)
    recalc_payroll(game, club)
    record_transaction(game, club, "신인 계약금", -signing_bonus, f"{p['name']} {round_no}라운드 지명 계약금", f"{game['season']}-09-{min(28,10+round_no):02d}", "계약금")
    draft["picks"].append({"round":round_no,"pick":idx+1,"team":club,"player":p["name"],"ovr":p["ovr"] if club==game.get("team") else p.get("scout_estimate",p["ovr"]),"position":p.get("position")})
    return True

def auto_draft_until_user(game):
    draft=ensure_draft(game); order=draft.get("order") or draft_standings_order(game)[0]
    rounds=int(draft.get("rounds", draft_round_count(game)))
    while not draft.get("complete") and draft.get("pick_index",0) < len(order)*rounds:
        idx=draft["pick_index"]; round_no=idx//len(order)+1; pos=idx%len(order)
        club=order[pos]
        if club == game.get("team"):
            break
        pool=game.get("draft_pool",[])
        if not pool:
            draft["complete"]=True; break
        # AI는 스카우트 추정치(겉으로 보이는 정보)로 판단하며, 리빌딩 구단은 잠재력을 더 중시한다.
        rank=league_ranking(game).index(club)+1
        w_pot = 0.5 if rank>=8 else 0.35
        p=max(pool,key=lambda x:int(x.get("scout_estimate",x.get("ovr",0)))*(1-w_pot)+int(x.get("scout_potential_estimate",x.get("potential",0)))*w_pot+random.random()*5)
        if not draft_assign(game, draft, p, club, round_no, idx):
            draft["picks"].append({"round":round_no,"pick":idx+1,"team":club,"player":"지명권 패스(선수단 정원)","ovr":"-"})
        draft["pick_index"]+=1
    if draft.get("pick_index",0)>=len(order)*rounds or not game.get("draft_pool"):
        draft["complete"]=True
    return draft

@app.route("/draft")
def draft_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    if not regular_season_complete(game) and game.get("phase") not in ("시즌 종료","포스트시즌 종료","FA 시장","스토브리그 종료"):
        flash("신인 드래프트는 정규시즌 종료 후에 진행할 수 있습니다.","error")
        return redirect(url_for("schedule_page"))
    draft=ensure_draft(game); draft=auto_draft_until_user(game); save_game(game)
    order=draft.get("order") or []; rounds=int(draft.get("rounds",10))
    current_team=order[draft["pick_index"]%len(order)] if order and not draft.get("complete") and draft.get("pick_index",0)<len(order)*rounds else None
    return render_template("draft_game.html",game=game,pool=sorted(game.get("draft_pool",[]),key=lambda x:(int(x.get("scout_estimate",x.get("ovr",0)))*0.65+int(x.get("scout_potential_estimate",x.get("potential",0)))*0.35),reverse=True),team=game["team"],draft=draft,current_team=current_team,rounds=rounds,pool_size=int(rule_data(game["season"],"draft_pool_size")))

@app.route("/draft/pick/<player_id>",methods=["POST"])
def draft_pick(player_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    draft=ensure_draft(game); draft=auto_draft_until_user(game)
    order=draft.get("order") or []; rounds=int(draft.get("rounds",10))
    idx=int(draft.get("pick_index",0))
    current_team=order[idx%len(order)] if order and idx<len(order)*rounds and not draft.get("complete") else None
    pool=game.setdefault("draft_pool",[]); p=next((x for x in pool if x.get("id")==player_id),None)
    if draft.get("complete") or current_team != game.get("team"):
        flash("현재는 우리 구단의 지명 차례가 아닙니다.","error")
    elif not p:
        flash("해당 선수는 이미 지명됐습니다.","error")
    else:
        round_no=idx//len(order)+1
        if not draft_assign(game, draft, p, game["team"], round_no, idx):
            flash(f"선수단 {roster_total_limit(game)}명 정원이며 교체 가능한 퓨처스 선수가 없습니다.","error")
            return redirect(url_for("draft_page"))
        draft["pick_index"]+=1
        if draft["pick_index"]>=len(order)*rounds or not pool: draft["complete"]=True
        game.setdefault("reports",[]).insert(0,f"신인 드래프트 {round_no}라운드 지명: {p['name']} · 스카우트 예상 OVR {p.get('scout_estimate',p['ovr'])} · {rule_data(game['season'],'rookie_contract_years')}년 신인 계약")
        auto_draft_until_user(game)
        save_game(game); flash(f"{p['name']} 선수를 지명했습니다.","success")
    return redirect(url_for("draft_page"))

@app.route("/draft/pass", methods=["POST"])
def draft_pass():
    """지명권 포기 (하위 라운드에서 선수단 정원을 아끼기 위해)."""
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    draft=ensure_draft(game); draft=auto_draft_until_user(game)
    order=draft.get("order") or []; rounds=int(draft.get("rounds",10)); idx=int(draft.get("pick_index",0))
    current_team=order[idx%len(order)] if order and idx<len(order)*rounds and not draft.get("complete") else None
    if draft.get("complete") or current_team != game.get("team"):
        flash("현재는 우리 구단의 지명 차례가 아닙니다.","error")
    else:
        round_no=idx//len(order)+1
        draft["picks"].append({"round":round_no,"pick":idx+1,"team":game["team"],"player":"지명권 포기","ovr":"-"})
        draft["pick_index"]+=1
        if draft["pick_index"]>=len(order)*rounds: draft["complete"]=True
        auto_draft_until_user(game); save_game(game); flash(f"{round_no}라운드 지명권을 포기했습니다.","info")
    return redirect(url_for("draft_page"))

@app.route("/draft/remove/<player_id>", methods=["POST"])
def draft_remove_candidate(player_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    draft=ensure_draft(game)
    pool=game.setdefault("draft_pool",[])
    candidate=next((p for p in pool if p.get("id")==player_id),None)
    if candidate:
        pool.remove(candidate)
        game.setdefault("draft_removed",[]).append({"name":candidate.get("name"),"position":candidate.get("position"),"season":game.get("season")})
        save_game(game); flash(f"{candidate.get('name')} 선수를 후보 목록에서 제외했습니다.","success")
    else: flash("후보 목록에서 선수를 찾을 수 없습니다.","error")
    return redirect(url_for("draft_page"))

def ensure_foreign_market(game):
    """Generate a fresh annual foreign-player proposal pool using the foreign scout's ability."""
    pool = game.setdefault("foreign_pool", [])
    season = int(game.get("season", 2027))
    if game.get("foreign_market_season") != season:
        scout = game.get("staff", {}).get("외국인 스카우트 팀장", {})
        skills = scout.get("skills", {})
        expertise = int(skills.get("전문성", 60))
        leadership = int(skills.get("리더십", 60))
        collaboration = int(skills.get("협업", 60))
        quality = max(0, min(20, round((expertise + leadership + collaboration) / 15) - 12))
        candidates = [
            ("미국", "일반", "SP"), ("도미니카공화국", "일반", "CF"), ("미국", "일반", "1B"),
            ("쿠바", "일반", "SP"), ("베네수엘라", "일반", "RP"), ("도미니카공화국", "일반", "SS"),
            ("멕시코", "일반", "LF"), ("푸에르토리코", "일반", "C"), ("일본", "아시아", "SP"),
            ("일본", "아시아", "2B"), ("대만", "아시아", "RP"), ("호주", "아시아", "C")
        ]
        new_pool = []
        for i, (nation, kind, pos) in enumerate(candidates):
            base = random.randint(61, 79) if kind == "일반" else random.randint(57, 74)
            ovr = max(45, min(90, base + random.randint(-3, quality)))
            name = make_fictional_name("외국인", season, i + random.randint(1, 9999))
            # 아시아쿼터 신규 영입 비용 상한(2026: 총 20만 달러) → 게임 단위 약 28,000만원으로 환산 (검증 필요)
            salary = random.randint(45000, 180000) if kind == "일반" else random.randint(18000, 28000)
            new_pool.append({"id":"F"+uuid.uuid4().hex[:12], "name":name, "team":None, "position":pos,
                "age":random.randint(24, 34), "ovr":ovr, "potential":min(94, ovr+random.randint(0, 12)),
                "nationality":nation, "is_foreign":True, "foreign_type":kind, "status":"외국인 영입 후보",
                "salary":salary, "adaptation_risk":random.choice(["낮음","보통","높음"]),
                "contract_years":1, "scout_report":foreign_scout_report(expertise, leadership, ovr),
                "scout_confidence":max(35, min(95, round((expertise*0.6 + leadership*0.2 + collaboration*0.2)))),
                "skills":{}, "seasons_registered":0, "injury":None, "injury_days":0, "rehab_score":0, "fatigue":random.randint(0,15), "condition":random.randint(70,100), "morale":70,
                "stats":{"G":0,"AB":0,"H":0,"HR":0,"RBI":0,"W":0,"IP":0.0,"ER":0,"SO":0}})
        game["foreign_pool"] = new_pool
        game["foreign_market_season"] = season
        game["foreign_market_generated"] = True
        game["foreign_replacements_used"] = {}
        game.setdefault("reports", []).insert(0, f"{season}시즌 외국인 스카우팅 보고서 도착: 외국인 스카우트 팀장 역량을 반영해 신규 후보 {len(new_pool)}명을 제안했습니다.")
    return game["foreign_pool"]

def foreign_scout_report(expertise, leadership, ovr):
    noise = random.randint(-max(2, 22 - int(expertise * 0.2)), max(2, 22 - int(expertise * 0.2)))
    estimate = max(40, min(90, ovr + noise))
    if estimate >= 76:
        verdict = "즉시 전력감으로 평가. 다만 적응력과 부상 이력 추가 확인 권장."
    elif estimate >= 65:
        verdict = "주전 경쟁이 가능한 자원. 리그 적응 여부가 성패를 좌우할 수 있음."
    else:
        verdict = "저비용 영입 후보. 백업 활용 가능성을 중심으로 검토 필요."
    if leadership < 50:
        verdict += " 평가 근거가 엇갈려 영상·메디컬 재검토 권장."
    return f"스카우트 예상 OVR {max(40, estimate-5)}~{min(95, estimate+5)}. {verdict}"

def foreign_slot_counts(game, club):
    roster=game.get("rosters",{}).get(club,[])
    return sum(1 for x in roster if x.get("is_foreign") and x.get("foreign_type") != "아시아"), sum(1 for x in roster if x.get("is_foreign") and x.get("foreign_type") == "아시아")

FOREIGN_REPLACEMENTS_PER_SEASON = 2  # 시즌 중 외국인 교체 허용 횟수 (게임 기본안, 검증 필요)

@app.route("/foreign")
def foreign_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    pool=ensure_foreign_market(game)
    general,asian=foreign_slot_counts(game,game["team"])
    general_limit, asia_limit = foreign_limits(game)
    roster=[x for x in game.get("rosters",{}).get(game["team"],[]) if x.get("is_foreign")]
    used=int(game.get("foreign_replacements_used",{}).get(game["team"],0))
    save_game(game)
    return render_template("foreign_game.html",game=game,team=game["team"],pool=pool,general_count=general,asian_count=asian,general_limit=general_limit,asia_limit=asia_limit,foreign_roster=roster,scout=game.get("staff",{}).get("외국인 스카우트 팀장",{}),roster_total=roster_total_limit(game),replacements_used=used,replacements_max=FOREIGN_REPLACEMENTS_PER_SEASON)

@app.route("/foreign/sign/<player_id>",methods=["POST"])
def foreign_sign(player_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    pool=ensure_foreign_market(game); p=next((x for x in pool if x.get("id")==player_id),None); club=game["team"]
    replace_id=request.form.get("replace_id", "").strip()
    general_limit, asia_limit = foreign_limits(game)
    if not p:
        flash("해당 외국인 선수는 이번 시즌 제안 풀에 없습니다.","error")
    else:
        roster=game.get("rosters",{}).get(club,[])
        general,asian=foreign_slot_counts(game,club); kind=p.get("foreign_type","일반")
        old=next((x for x in roster if x.get("id")==replace_id and x.get("is_foreign")),None) if replace_id else None
        in_season = game.get("phase") == "정규시즌" and game.get("day_index",0) > 0
        used=int(game.get("foreign_replacements_used",{}).get(club,0))
        if len(roster)>=roster_total_limit(game) and not old:
            flash(f"소속선수 {roster_total_limit(game)}명 정원이 가득 찼습니다. 먼저 선수단을 정리하세요.","error")
        elif kind=="아시아" and asian>=asia_limit and not (old and old.get("foreign_type")=="아시아"):
            flash(f"아시아 쿼터({asia_limit}명)가 이미 차 있습니다. 기존 아시아 쿼터 선수를 교체 대상으로 선택하세요.","error")
        elif kind!="아시아" and general>=general_limit and not (old and old.get("foreign_type")!="아시아"):
            flash(f"일반 외국인 한도({general_limit}명)가 가득 찼습니다. 기존 일반 외국인 선수를 교체 대상으로 선택하세요.","error")
        elif int(game.get("finances",{}).get(club,{}).get("cash",0)) < int(p.get("salary",0)):
            flash("계약금을 감당할 현금이 부족합니다.","error")
        elif old and old.get("foreign_type") != kind:
            flash("같은 유형의 외국인 슬롯끼리만 교체할 수 있습니다.","error")
        elif old and in_season and used >= FOREIGN_REPLACEMENTS_PER_SEASON:
            flash(f"이번 시즌 외국인 교체 허용 횟수({FOREIGN_REPLACEMENTS_PER_SEASON}회)를 모두 사용했습니다.","error")
        else:
            if old:
                roster.remove(old)
                old["team"]=None; old["status"]="외국인 교체 방출"
                remaining=int(int(old.get("salary",0))*0.5)
                record_transaction(game, club, "외국인 교체 보상", -remaining, f"{old['name']} 잔여 계약 보상", None, "계약 보상")
                game.setdefault("foreign_releases",[]).append({"season":game["season"],"team":club,"player":old["name"],"replaced_by":p["name"]})
                game.setdefault("reports",[]).insert(0,f"외국인 선수 교체: {old['name']} → {p['name']}")
                if in_season:
                    game.setdefault("foreign_replacements_used",{})[club]=used+1
            p["team"]=club; p["status"]="퓨처스"; p["contract_years"]=1
            p["contract"]={"years":1,"salary_by_year":[p["salary"]],"guaranteed":True,"bonuses":[],"team_option":False,"player_option":False}
            roster.append(p); pool.remove(p)
            recalc_payroll(game, club)
            record_transaction(game, club, "외국인 계약금", -int(p.get("salary",0))//2, f"{p['name']} ({p['nationality']}, {kind}) 1년 계약", None, "계약금")
            game.setdefault("foreign_signings",[]).append({"season":game["season"],"team":club,"player":p["name"],"nationality":p["nationality"],"type":kind,"scout_confidence":p.get("scout_confidence")})
            game.setdefault("reports",[]).insert(0,f"외국인 선수 영입: {p['name']} ({p['nationality']}, {kind}) · 스카우트 신뢰도 {p.get('scout_confidence',0)}% · 적응 위험 {p.get('adaptation_risk','보통')}")
            save_game(game); flash(f"{p['name']} 선수와 1년 계약을 체결했습니다. 선수단 메뉴에서 1군 등록하세요.","success")
    return redirect(url_for("foreign_page"))

def simulate_postseason_game(game, a, b, home):
    sa,_=team_strength(game,a); sb,_=team_strength(game,b)
    sa += (1.2 if home==a else 0) + random.uniform(-4,4); sb += (1.2 if home==b else 0) + random.uniform(-4,4)
    ra=max(0,round(random.gauss(4.2+(sa-sb)*.075,1.9))); rb=max(0,round(random.gauss(4.2+(sb-sa)*.075,1.9)))
    if ra==rb:
        if random.random()<.5: ra+=1
        else: rb+=1
    return ra, rb

@app.route("/postseason")
def postseason_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    order=league_ranking(game)
    return render_template("postseason_game.html",game=game,order=order,team=game["team"],ps_teams=postseason_team_count(game),season_over=regular_season_complete(game))

@app.route("/postseason/simulate",methods=["POST"])
def postseason_simulate():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    if not regular_season_complete(game):
        flash("정규시즌 경기를 모두 마친 뒤 포스트시즌을 진행할 수 있습니다.","error")
        return redirect(url_for("postseason_page"))
    if (game.get("postseason") or {}).get("season") == game.get("season"):
        flash("이번 시즌 포스트시즌은 이미 완료되었습니다.","info")
        return redirect(url_for("postseason_page"))
    order=league_ranking(game)
    results={t:"미진출" for t in game["teams"]}
    def win_series(a,b,need,round_name,advantage_a=0):
        sa=advantage_a; sb=0; games=[]
        while sa<need and sb<need:
            home = a if len(games)%4 in (0,1) or len(games)==4 else b
            ra,rb=simulate_postseason_game(game,a,b,home)
            winner=a if ra>rb else b
            if winner==a: sa+=1
            else: sb+=1
            games.append({"game":len(games)+1,"winner":winner,"score_a":ra,"score_b":rb,"home":home})
        return {"round":round_name,"a":a,"b":b,"winner":a if sa>sb else b,"wins_a":sa,"wins_b":sb,"games":games,"format":f"{need*2-1}전 {need}선승"}
    n=postseason_team_count(game)
    qualifiers=order[:n]; rounds=[]
    for t in qualifiers: results[t]="와일드카드" if qualifiers.index(t)>=3 else "준플레이오프"
    # 와일드카드: 4위 1승 어드밴티지, 5위는 2승 필요 (KBO 2026 기준). R-09 시행 시 5위-6위 단판 후 4위와 맞붙는다.
    wc_low = qualifiers[4]
    if n >= 6:
        ra,rb=simulate_postseason_game(game,qualifiers[4],qualifiers[5],qualifiers[4])
        wc_low = qualifiers[4] if ra>rb else qualifiers[5]
        rounds.append({"round":"와일드카드 1차(R-09)","format":"단판","series":[{"a":qualifiers[4],"b":qualifiers[5],"winner":wc_low,"wins_a":1 if wc_low==qualifiers[4] else 0,"wins_b":1 if wc_low==qualifiers[5] else 0,"games":[{"game":1,"winner":wc_low,"score_a":ra,"score_b":rb,"home":qualifiers[4]}]}]})
    wc=win_series(qualifiers[3],wc_low,2,"와일드카드",advantage_a=1)
    wc["format"]="최대 2경기 · 4위 1승 어드밴티지"
    rounds.append({"round":"와일드카드","format":wc["format"],"series":[wc]})
    results[wc["winner"]]="준플레이오프"
    semi=win_series(qualifiers[2],wc["winner"],3,"준플레이오프"); rounds.append({"round":"준플레이오프","format":"5전 3선승","series":[semi]})
    results[semi["winner"]]="플레이오프"
    po=win_series(qualifiers[1],semi["winner"],3,"플레이오프"); rounds.append({"round":"플레이오프","format":"5전 3선승","series":[po]})
    results[po["winner"]]="한국시리즈"; results[qualifiers[0]]="한국시리즈"
    ks=win_series(qualifiers[0],po["winner"],4,"한국시리즈"); rounds.append({"round":"한국시리즈","format":"7전 4선승","series":[ks]})
    champ=ks["winner"]; results[champ]="우승"
    game["postseason"]={"season":game["season"],"qualifiers":qualifiers,"rounds":rounds,"champion":champ,"results":results}
    game["phase"]="포스트시즌 종료"
    # 포스트시즌 배분금 (규칙서 10.1 리그 배분금)
    share={"우승":60000,"한국시리즈":35000,"플레이오프":20000,"준플레이오프":12000,"와일드카드":6000}
    for t,res in results.items():
        if res in share:
            record_transaction(game, t, "포스트시즌 배분금", share[res], f"{game['season']} 포스트시즌 {res} 배분금", f"{game['season']}-11-05", "중계")
    game.setdefault("club_history",[]).append({"season":game["season"],"champion":champ,"qualifiers":qualifiers,"user_result":results.get(game["team"])})
    game["reports"].insert(0,f"{game['season']} 포스트시즌 종료: 한국시리즈 우승 {champ} · 우리 구단 {results.get(game['team'])}")
    save_game(game); flash(f"포스트시즌 종료! 한국시리즈 우승: {champ}","success")
    return redirect(url_for("rules_page"))

@app.route("/records")
def records_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    players=[]
    for club, roster in game.get("rosters",{}).items():
        for p in roster:
            row=dict(p)
            row["club_name"]=club
            stats=row.setdefault("stats",{})
            row["AVG"]=(stats.get("H",0)/stats.get("AB",0)) if stats.get("AB",0) else 0.0
            row["ERA"]=(stats.get("ER",0)*9/stats.get("IP",0)) if stats.get("IP",0) else 0.0
            players.append(row)
    batters=sorted([p for p in players if p.get("position") not in ("SP","RP","CP")],key=lambda p:(p["stats"].get("H",0),p["stats"].get("HR",0)),reverse=True)[:20]
    homers=sorted([p for p in players if p.get("position") not in ("SP","RP","CP")],key=lambda p:p["stats"].get("HR",0),reverse=True)[:20]
    pitchers=sorted([p for p in players if p.get("position") in ("SP","RP","CP")],key=lambda p:p["stats"].get("W",0),reverse=True)[:20]
    return render_template("records_game.html",game=game,team=game["team"],batters=batters,homers=homers,pitchers=pitchers)


@app.route("/awards")
def awards_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    awards=game.setdefault("awards_history",[])
    return render_template("awards_game.html", game=game, team=game["team"], latest=(awards[-1] if awards else None), awards=awards[::-1], season_over=regular_season_complete(game))

@app.route("/awards/decide", methods=["POST"])
def decide_awards():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    if not regular_season_complete(game):
        flash("정규시즌 종료 후에 시상식을 진행할 수 있습니다.","error"); return redirect(url_for("awards_page"))
    game.setdefault("awards_history",[])
    if any(a.get("season")==game["season"] for a in game["awards_history"]):
        flash("이번 시즌 시상식은 이미 완료되었습니다.","error"); return redirect(url_for("awards_page"))
    players=[]
    for club, roster in game.get("rosters",{}).items():
        for p in roster:
            st=p.setdefault("stats",{}); row={"id":p["id"],"name":p["name"],"team":club,"position":p.get("position",""),"G":st.get("G",0),"AB":st.get("AB",0),"H":st.get("H",0),"HR":st.get("HR",0),"RBI":st.get("RBI",0),"W":st.get("W",0),"IP":st.get("IP",0),"ER":st.get("ER",0),"SO":st.get("SO",0)}
            row["AVG"]=row["H"]/row["AB"] if row["AB"] else 0; row["ERA"]=row["ER"]*9/row["IP"] if row["IP"] else 99; players.append(row)
    bat=[p for p in players if p["position"] not in ("SP","RP","CP")]; pit=[p for p in players if p["position"] in ("SP","RP","CP")]
    def vote(candidates, key, reverse=True, voters=30):
        """규칙서 14: 기록 기반 후보 선정 후 기자단 투표. 후보 3명에게 가중 무작위 표를 배분한다."""
        pool=[c for c in candidates if c is not None]
        if not pool: return None, []
        ranked=sorted(pool,key=key,reverse=reverse)[:3]
        weights=[0.55,0.30,0.15][:len(ranked)]
        tally={c["id"]:0 for c in ranked}
        for _ in range(voters):
            pick=random.choices(ranked,weights=weights)[0]; tally[pick["id"]]+=1
        winner=max(ranked,key=lambda c:tally[c["id"]])
        return winner, [{"name":c["name"],"team":c["team"],"votes":tally[c["id"]]} for c in ranked]
    qualified_pit=[p for p in pit if p["IP"]>=60]
    mvp,mvp_votes=vote(players,lambda p:p["HR"]*2+p["RBI"]+p["H"]*.4+p["W"]*5+p["SO"]*.1)
    hr,hr_votes=vote(bat,lambda p:p["HR"])
    hit,hit_votes=vote([p for p in bat if p["AB"]>=200] or bat,lambda p:(p["AVG"],p["H"]))
    wins,wins_votes=vote(pit,lambda p:p["W"])
    so,so_votes=vote(pit,lambda p:p["SO"])
    era,era_votes=vote(qualified_pit or [p for p in pit if p["IP"]>=10],lambda p:p["ERA"],reverse=False)
    a={"season":game["season"],"mvp":mvp,"homerun":hr,"hitter":hit,"wins":wins,"strikeouts":so,"era":era,"champion":(game.get("postseason") or {}).get("champion"),
       "votes":{"mvp":mvp_votes,"homerun":hr_votes,"hitter":hit_votes,"wins":wins_votes,"strikeouts":so_votes,"era":era_votes},"voters":30}
    game["awards_history"].append(a); game.setdefault("reports",[]).insert(0,f"{game['season']}시즌 시상식 완료: MVP {mvp['name'] if mvp else '-'} (기자단 투표)")
    save_game(game); flash("시즌 시상식을 저장했습니다. (기록 기반 후보 3명 → 기자단 30표 투표)","success"); return redirect(url_for("awards_page"))

@app.route("/settings")
def settings_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    return render_template("settings_game.html",game=game,team=game["team"],settings=game.get("settings",{"auto_roster":True}),played_games=team_games_played(game,game["team"]),target_games=rule_data(game["season"],"regular_games"))

@app.route("/settings/update", methods=["POST"])
def settings_update():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    settings=game.setdefault("settings",{})
    settings["auto_roster"]=request.form.get("auto_roster")=="1"
    save_game(game); flash("설정을 저장했습니다.","success")
    return redirect(url_for("settings_page"))

@app.route("/game/new",methods=["POST"], endpoint="new_game_route")
@app.route("/game/new-career",methods=["POST"], endpoint="new_game")
def new_game_route():
    # Keep the current save until a replacement team is confirmed on /start.
    return redirect(url_for("start_game"))

@app.route("/rules")
def rules_page():
    game = get_game()
    state = load_state()
    current_event = next((e for e in reversed(state["event_history"]) if e["status"] == "심의 중"), None)
    offseason_phases = {"포스트시즌 종료", "FA 시장", "스토브리그 종료", "시즌 종료"}
    if game and game.get("phase") in offseason_phases and not current_event and state.get("last_event_season") != game.get("season"):
        pool = eligible_rules(state)
        # 규칙서 15.2: 오프시즌 개정안 등장 확률 초기값 30% (게임 밸런스용). 등장하지 않은 시즌도 기록해 중복 판정을 막는다.
        rolled = random.random() < float(rule_data(game.get("season", 2027), "rule_event_probability"))
        if not rolled:
            state["last_event_season"] = int(game.get("season", 2027))
            state.setdefault("no_event_seasons", []).append(int(game.get("season", 2027)))
            save_state(state)
            flash("이번 오프시즌에는 리그 차원의 규정 개정 안건이 발의되지 않았습니다. (등장 확률 30%) 필요하면 아래 '오프시즌 심의 시작'으로 직접 발의할 수 있습니다.", "info")
            pool = []
        if pool:
            count = random.randint(1, min(3, len(pool)))
            event_id = int(state.get("next_event_id", 1))
            proposals = []
            for rule in random.sample(pool, count):
                proposals.append({"rule_id":rule["id"],"name":rule["name"],"category":rule["category"],"change":rule["change"],"trigger":rule["trigger"],"base_probability":rule["prob"],"gm_stance":None,"final_probability":None,"result":None,"effective_season":int(game.get("season",2027))+1})
            state.setdefault("event_history",[]).append({"event_id":event_id,"season":int(game.get("season",2027)),"created_at":datetime.now().strftime("%Y-%m-%d %H:%M"),"status":"심의 중","proposals":proposals})
            state["next_event_id"] = event_id + 1
            state["last_event_season"] = int(game.get("season",2027))
            save_state(state)
            current_event = state["event_history"][-1]
            flash("오프시즌 규정 개정 안건이 자동 발의되었습니다. 찬반 의견을 제출하세요.", "success")
    return render_template("rules.html", rules=RULES, state=state, current_event=current_event, game=game)

@app.route("/rules/<rule_id>")
def rule_detail(rule_id):
    rule = rule_by_id(rule_id)
    if not rule:
        flash("해당 규정을 찾을 수 없습니다.", "error")
        return redirect(url_for("rules_page"))
    return render_template("rule_detail.html", rule=rule)

@app.route("/event/create", methods=["POST"])
def create_event():
    game = get_game()
    if game:
        if game.get("phase") not in ("시즌 종료", "포스트시즌 종료", "FA 시장", "스토브리그 종료"):
            flash("규정 개정은 정규시즌이 끝난 오프시즌에만 심의할 수 있습니다.", "error")
            return redirect(url_for("rules_page"))
        game.setdefault("rule_state", STATE_DEFAULT.copy())["season"] = game["season"]
    state = load_state()
    if any(e["status"] == "심의 중" for e in state["event_history"]):
        flash("이미 심의 중인 안건이 있습니다. 먼저 투표를 완료하세요.", "error")
        return redirect(url_for("office"))
    if game and any(e.get("season") == game.get("season") for e in state["event_history"]):
        flash("이번 시즌 규정 심의는 이미 진행했습니다. 규정 개정은 시즌당 1회입니다.", "error")
        return redirect(url_for("rules_page"))
    # In-game offseason proposal is guaranteed once per season; no silent 30% no-event result.
    force = request.form.get("force") == "1"
    pool = eligible_rules(state)
    if not pool:
        flash("현재 발의 가능한 규정이 없습니다. 시행 규정의 폐지·재개정 기능은 다음 버전에서 추가할 수 있습니다.", "info")
        return redirect(url_for("office"))
    count = random.randint(1, min(3, len(pool)))
    selected = random.sample(pool, count)
    event_id = state["next_event_id"]
    state["next_event_id"] += 1
    proposals = []
    for rule in selected:
        proposals.append({
            "rule_id": rule["id"],
            "name": rule["name"],
            "category": rule["category"],
            "change": rule["change"],
            "trigger": rule["trigger"],
            "base_probability": rule["prob"],
            "gm_stance": None,
            "final_probability": None,
            "result": None,
            "effective_season": state["season"] + 1
        })
    state["event_history"].append({
        "event_id": event_id,
        "season": state["season"],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "심의 중",
        "proposals": proposals
    })
    state["last_event_season"] = state["season"]
    save_state(state)
    flash(f"오프시즌 규정 변경 이벤트 #{event_id}이(가) 생성되었습니다.", "success")
    return redirect(url_for("rules_page"))

@app.route("/event/<int:event_id>/vote", methods=["POST"])
def vote_event(event_id):
    state = load_state()
    event = next((e for e in state["event_history"] if e["event_id"] == event_id), None)
    if not event or event["status"] != "심의 중":
        flash("투표할 수 있는 이벤트가 아닙니다.", "error")
        return redirect(url_for("office"))
    for proposal in event["proposals"]:
        stance = request.form.get(f"stance_{proposal['rule_id']}")
        if stance not in ("찬성", "반대"):
            flash("모든 안건에 찬성 또는 반대 의견을 선택하세요.", "error")
            return redirect(url_for("office"))
        rule = rule_by_id(proposal["rule_id"])
        probability = calc_pass_probability(rule, stance, state)
        proposal["gm_stance"] = stance
        proposal["final_probability"] = probability
        proposal["result"] = "통과" if random.random() < probability / 100 else "부결"
        if proposal["result"] == "통과":
            active = {
                "rule_id": proposal["rule_id"],
                "name": proposal["name"],
                "effective_season": state["season"] + 1,
                "status": "시행 예정",
                "approved_event": event_id,
                "gm_stance": stance
            }
            state["active_rules"].append(active)
    event["status"] = "심의 완료"
    event["resolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_state(state)
    flash("리그의 규정 심의 결과가 확정되었습니다. 통과 규정은 다음 시즌부터 시행됩니다.", "success")
    return redirect(url_for("rules_page"))


@app.route("/career")
def career_page():
    game = get_game()
    if not game:
        flash("먼저 커리어를 시작하세요.", "error")
        return redirect(url_for("start_game"))
    team = game.get("team")
    history = game.get("season_history", [])
    gm = {
        "seasons_managed": max(1, len(history) + (1 if game.get("game_status") != "종료" else 0)),
        "championships": sum(1 for h in history if h.get("champion") == team),
        "playoff_appearances": sum(1 for h in history if int(h.get("user_team_finish", 10)) <= 5),
        "best_finish": min([int(h.get("user_team_finish",10)) for h in history] or [10])
    }
    records = list(game.get("career_records", {}).values()) if isinstance(game.get("career_records"), dict) else game.get("career_records", [])
    records = sorted(records, key=lambda r: (r.get("H",0)+r.get("HR",0)*3+r.get("W",0)*8+r.get("SO",0)*0.3), reverse=True)
    return render_template("career_game.html", game=game, team=team, gm=gm,
        history=history, records=records[:30], retired=game.get("retired_players", [])[::-1][:40],
        hall=game.get("hall_of_fame", []))


@app.route("/career/new-from-end", methods=["POST"])
def new_career_from_end():
    return redirect(url_for("start_game"))


@app.route("/career/owner-review")
def owner_review():
    game = get_game()
    if not game: return redirect(url_for("start_game"))
    if game.get("game_status") == "종료" and game.get("game_end", {}).get("score"):
        score_data = game["game_end"]["score"]
        return render_template("owner_review.html", game=game, team=game["team"],
            rank=score_data.get("rank",10), score=0, verdict="커리어 종료",
            reasons=[game["game_end"].get("reason","커리어 종료")],
            owner_trust=game.get("owner_trust",0), rating=game.get("manager_rating",0),
            wins=score_data.get("wins",0), losses=score_data.get("losses",0),
            final_score=score_data)
    game = require_game()
    if not game: return redirect(url_for("start_game"))
    review = compute_owner_review(game)
    already = any(r.get("season") == game["season"] for r in game.get("owner_reviews", []))
    return render_template("owner_review.html", game=game, team=game["team"], rank=review["rank"], score=review["score"],
        verdict=("해임 확정" if game.get("manager_fired") else review["verdict"]), reasons=review["reasons"], owner_trust=review["owner_trust"],
        rating=int(game.get("manager_rating", 70)), wins=review["wins"], losses=review["losses"], goal=review["goal"], already=already,
        final_score=game.get("game_end", {}).get("score") if game.get("game_end") else None)

@app.route("/career/owner-review/acknowledge", methods=["POST"])
def acknowledge_owner_review():
    game = require_game()
    if not game: return redirect(url_for("start_game"))
    if game.get("day_index", 0) < len(game.get("schedule", [])):
        flash("구단주 평가는 정규시즌이 끝난 뒤 기록할 수 있습니다.", "error")
        return redirect(url_for("owner_review"))
    review = compute_owner_review(game)   # 점수는 서버에서만 계산한다 (클라이언트 값 무시)
    if not apply_owner_review(game, review):
        flash("이번 시즌 구단주 평가는 이미 기록되었습니다.", "info")
        return redirect(url_for("owner_review"))
    if game.get("manager_fired"):
        flash(f"단장 해임으로 커리어가 종료되었습니다. 최종 점수 {game['game_end']['score']['total_score']}/1000점.", "error")
    else:
        flash("구단주 평가 결과를 커리어 기록에 저장했습니다.", "success")
    save_game(game)
    return redirect(url_for("owner_review" if game.get("manager_fired") else "office"))

@app.route("/career/new-manager", methods=["POST"])
def appoint_new_manager():
    game = get_game()
    if not game:
        flash("진행 중인 커리어가 없습니다.", "error")
        return redirect(url_for("start_game"))
    if game.get("game_status") == "종료" or game.get("manager_fired"):
        flash("단장 해임은 게임 종료입니다. 후임 단장으로 이어갈 수 없습니다.", "error")
        return redirect(url_for("owner_review"))
    flash("후임 단장 기능은 사용하지 않습니다.", "error")
    return redirect(url_for("office"))


@app.route("/career/end", methods=["POST"])
@app.route("/career/retire", methods=["POST"], endpoint="end_career")
@app.route("/career/retire-gm", methods=["POST"], endpoint="retire_gm")
def end_career():
    game = get_game()
    if not game:
        flash("종료할 커리어가 없습니다.", "error")
        return redirect(url_for("index"))
    if game.get("game_status") == "종료":
        return redirect(url_for("owner_review"))
    reason = request.form.get("reason", "단장 자진 은퇴").strip()[:80] or "단장 자진 은퇴"
    game["game_status"] = "종료"
    game["game_end"] = {"reason": reason, "season": game.get("season", 2027), "ended_at": datetime.now().isoformat(timespec="seconds")}
    game["game_end"]["score"] = calculate_final_career_score(game, reason)
    game.setdefault("reports", []).insert(0, f"커리어 종료: {reason} · 최종 점수 {game['game_end']['score']['total_score']}/1000점")
    save_game(game)
    flash("커리어가 종료되었습니다. 최종 종합 점수를 확인하세요.", "success")
    return redirect(url_for("owner_review"))


@app.route("/season/advance", methods=["POST"])
def advance_season():
    if get_game():
        flash("실제 게임 세이브에서는 시즌 종료·FA 절차를 거쳐 다음 시즌을 시작해야 합니다.", "error")
        return redirect(url_for("schedule_page"))
    state = load_state()
    state["season"] += 1
    for rule in state["active_rules"]:
        if rule["status"] == "시행 예정" and rule["effective_season"] <= state["season"]:
            rule["status"] = "시행 중"
    save_state(state)
    flash(f"{state['season']}시즌으로 진행했습니다. 시행 예정 규정을 적용했습니다.", "success")
    return redirect(url_for("office"))

@app.route("/reset", methods=["POST"])
def reset():
    save_state({
        "season": 2027, "event_history": [], "active_rules": [],
        "next_event_id": 1, "last_event_season": None
    })
    flash("데모 데이터가 초기화되었습니다.", "info")
    return redirect(url_for("office"))

if __name__ == "__main__":
    app.run(debug=True)
