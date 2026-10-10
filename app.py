from flask import Flask, render_template, request, redirect, url_for, flash, session
from datetime import datetime, timedelta
import random
import json
import os
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

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
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

STATE_DEFAULT = {
    "season": 2027,
    "event_history": [],
    "active_rules": [],
    "next_event_id": 1,
    "last_event_season": None
}

def load_state():
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

# 2026 KBO 소속 선수 이름을 우선 사용한다. 각 팀의 1군 기본 명단은 실제 선수,
# 추가 육성 선수와 이후 생성되는 신인은 가상 이름으로 만든다.
# 이름·구단 기준: KBO 2026 소속 선수 등록 명단 및 2026 시즌 선수단 자료.
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
        ("안현민","RF"),("배정대","CF"),("김민혁","LF"),("최원준","CF"),("힐리어드","1B"),("김현수","DH"),
        ("권동진","2B"),("장준원","SS"),("류현인","3B"),("조대현","C")],
    "SSG 랜더스": [
        ("화이트","SP"),("아빌라","SP"),("문승원","SP"),("노경은","SP"),("김민준","SP"),
        ("해치","SP"),("조병현","CP"),("서진용","RP"),("김택형","RP"),("이로운","RP"),("장지훈","RP"),("최민준","RP"),
        ("조형우","C"),("이지영","C"),("최정","3B"),("박성한","SS"),("정준재","2B"),("고명준","1B"),
        ("최지훈","CF"),("에레디아","LF"),("한유섬","RF"),("김재환","DH"),("김성현","2B"),("박지환","SS"),
        ("오태곤","1B"),("김민식","C"),("류효승","LF"),("한두솔","RP")],
    "NC 다이노스": [
        ("이재학","SP"),("구창모","SP"),("신민혁","SP"),("라일리","SP"),("테일러","SP"),
        ("토다","SP"),("이용찬","CP"),("김영규","RP"),("임정호","RP"),("류진욱","RP"),("송명기","RP"),("하준영","RP"),
        ("김형준","C"),("안중열","C"),("박민우","2B"),("김주원","SS"),("서호철","3B"),("김휘집","3B"),
        ("박건우","RF"),("권희동","LF"),("천재환","CF"),("데이비슨","1B"),("최정원","2B"),("도태훈","1B"),
        ("김정호","C"),("김태경","RP"),("신영우","SP"),("허윤","SS")],
    "두산 베어스": [
        ("곽빈","SP"),("최원준","SP"),("잭로그","SP"),("벤자민","SP"),("플렉센","SP"),
        ("최민석","SP"),("김택연","CP"),("이용찬","RP"),("이영하","RP"),("최승용","RP"),("박치국","RP"),("김명신","RP"),
        ("양의지","C"),("김기연","C"),("강승호","2B"),("박찬호","SS"),("안재석","SS"),("양석환","1B"),
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
        ("류현진","SP"),("문동주","SP"),("화이트","SP"),("왕옌청","SP"),("에르난데스","SP"),
        ("쿠싱","SP"),("주현상","CP"),("박상원","RP"),("강재민","RP"),("김서현","RP"),("이민우","RP"),("윤산흠","RP"),
        ("최재훈","C"),("허인서","C"),("노시환","3B"),("심우준","SS"),("정은원","2B"),("채은성","1B"),
        ("문현빈","CF"),("페라자","RF"),("김태연","LF"),("강백호","DH"),("황영묵","2B"),("최인호","LF"),
        ("이진영","RF"),("장규현","C"),("유민","CF"),("황준서","SP")],
    "키움 히어로즈": [
        ("안우진","SP"),("정현우","SP"),("김윤하","SP"),("알칸타라","SP"),("와일스","SP"),
        ("브룩스","SP"),("주승우","CP"),("김성민","RP"),("김재웅","RP"),("원종현","RP"),("김인범","RP"),("정찬헌","RP"),
        ("김건희","C"),("김동헌","C"),("안치홍","2B"),("송성문","3B"),("김웅빈","1B"),("데이비슨","DH"),
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
        return game
    except (OSError, json.JSONDecodeError):
        return None

def new_player(team, idx, strength, future=False):
    seed = random.randint(1000, 9999)
    real_list = REAL_ROSTERS.get(team, [])
    is_real = idx < len(real_list) and not future
    if is_real:
        name, pos = real_list[idx]
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
    ovr = max(42, min(91, base + random.randint(-5, 5)))
    player = {
        "id": f"P{seed}{idx}{random.randint(10,99)}", "name":name, "team":team,
        "position":pos, "age":age, "ovr":ovr, "potential":max(ovr, min(98, ovr+random.randint(0,18))),
        "salary":random.randint(2500, 9000) if age < 29 else random.randint(7000, 28000),
        "status":"퓨처스" if future else "1군", "condition":random.randint(65,100), "fatigue":random.randint(0,20),
        "injury":None, "contract_years":random.randint(1,4),
        "stats":{"G":0,"AB":0,"H":0,"HR":0,"RBI":0,"W":0,"IP":0.0,"ER":0,"SO":0}
    }
    return player

FICTIONAL_GIVEN_NAMES = ["도윤","시우","준서","하준","지호","예준","현우","우진","건우","서준","민재","지훈","태윤","성민","준혁","승현","정우","동현","재윤","유찬","민규","승우","지환","태민","현준","수현","은찬","시온","주원","정민","재민","윤호","승민","찬우","민호","지후"]
FICTIONAL_FAMILY_NAMES = ["강","김","박","이","최","정","조","윤","장","임","한","오","신","서","권","황","안","송","전","문","배","유","남","백","고","차","노","주","하","민"]

def make_fictional_name(team, seed, idx):
    # Deterministic within the generated player ID, and avoid using the current KBO roster names.
    taken = {name for roster in REAL_ROSTERS.values() for name, _ in roster}
    rng = random.Random(f"{team}-{seed}-{idx}")
    for _ in range(200):
        candidate = rng.choice(FICTIONAL_FAMILY_NAMES) + rng.choice(FICTIONAL_GIVEN_NAMES)
        if candidate not in taken:
            return candidate
    return f"신인{seed}"

def build_schedule(teams):
    # Circle-method round robin, repeated 16 times: 144 games per club.
    wheel = list(teams)
    rounds = []
    for rnd in range(9):
        games=[]
        for i in range(5):
            a,b=wheel[i],wheel[9-i]
            home,away=(a,b) if (rnd+i)%2==0 else (b,a)
            games.append({"home":home,"away":away,"played":False})
        rounds.append(games)
        wheel=[wheel[0],wheel[-1],*wheel[1:-1]]
    schedule=[]
    for cycle in range(16):
        for rnd,games in enumerate(rounds):
            day_games=[]
            for g in games:
                home,away=g["home"],g["away"]
                if cycle%2: home,away=away,home
                day_games.append({"home":home,"away":away,"played":False})
            schedule.append({"date":(datetime(2027,3,27)+timedelta(days=len(schedule))).strftime("%Y-%m-%d"),"games":day_games})
    return schedule

def create_game(team):
    game_id=uuid.uuid4().hex[:20]
    teams=list(TEAM_INFO)
    rosters={}
    standings={}
    finances={}
    for name,info in TEAM_INFO.items():
        roster=[]
        for i in range(36):
            # Keep the selected club near its profile strength; other clubs retain distinct power levels.
            roster.append(new_player(name, i, info["strength"], future=(i>=28)))
        rosters[name]=roster
        standings[name]={"W":0,"L":0,"D":0,"RS":0,"RA":0,"streak":"-"}
        finances[name]={"budget":info["budget"],"cash":info["budget"],"payroll":sum(x["salary"] for x in roster),"transactions":[{"date":"2027-01-01","type":"기초 예산","amount":info["budget"],"note":"시즌 시작 운영 자금"}]}
    schedule=build_schedule(teams)
    game={"game_id":game_id,"team":team,"season":2027,"week":0,"day_index":0,"teams":teams,"rosters":rosters,"standings":standings,"finances":finances,"schedule":schedule,"game_log":[],"reports":["2027시즌 단장 업무가 시작되었습니다. 선수단과 예산을 확인하세요."],"free_agents":[]}
    for i in range(12):
        p=new_player("FA 시장",i,random.randint(62,78),False); p["status"]="FA"; p["salary"]=random.randint(5000,22000); p["contract_years"]=1
        game["free_agents"].append(p)
    save_game(game)
    return game

def active_roster(game, team):
    return [p for p in game["rosters"][team] if p["status"]=="1군"]

def player_ovr(player): return int(player.get("ovr",50))

def simulate_one(game, fixture):
    home,away=fixture["home"],fixture["away"]
    def team_strength(team):
        active=active_roster(game,team)
        if not active: return 48
        batting=[p["ovr"] for p in active if p["position"] not in ("SP","RP","CP")]
        pitching=[p["ovr"] for p in active if p["position"] in ("SP","RP","CP")]
        bat=sum(batting)/max(1,len(batting)); pit=sum(pitching)/max(1,len(pitching))
        fatigue=sum(p.get("fatigue",0) for p in active)/len(active)
        return bat*.48+pit*.52-fatigue*.09+random.uniform(-3.0,3.0)
    hs,as_=team_strength(home),team_strength(away)
    hrun=max(0,round(random.gauss(4.4+(hs-as_)*.075,2.0)))
    arun=max(0,round(random.gauss(4.15+(as_-hs)*.075,2.0)))
    if hrun==arun: # regular-season ties are possible, rare
        if random.random()<.55: hrun+=1
        else: arun+=1
    for team,rs,ra in ((home,hrun,arun),(away,arun,hrun)):
        st=game["standings"][team]; st["RS"]+=rs; st["RA"]+=ra
        if rs>ra: st["W"]+=1; st["streak"]="W"
        elif rs<ra: st["L"]+=1; st["streak"]="L"
        else: st["D"]+=1; st["streak"]="D"
        roster=active_roster(game,team)
        batters=[p for p in roster if p["position"] not in ("SP","RP","CP")]
        for p in batters:
            if random.random()<.88:
                p["stats"]["G"]+=1; game_ab=random.randint(2,5); p["stats"]["AB"]+=game_ab
                ab=p["stats"]["AB"]
                hit_rate=max(0.16,min(0.34,0.22+(p["ovr"]-50)*0.0016))
                hits=sum(1 for _ in range(game_ab) if random.random()<hit_rate)
                p["stats"]["H"]+=hits
                if random.random() < max(0.004,min(0.07,0.012+(p["ovr"]-50)*0.00045)):
                    p["stats"]["HR"]+=1
                if random.random()<min(0.45,rs*0.075):
                    p["stats"]["RBI"]+=random.randint(1,2)
        pitchers=[p for p in roster if p["position"] in ("SP","RP","CP")]
        starters=sorted([p for p in pitchers if p["position"]=="SP"],key=lambda x:x["ovr"],reverse=True)
        if starters:
            sp=starters[(game["day_index"]//1)%len(starters)]
            sp["stats"]["G"]+=1; sp["stats"]["IP"]+=round(random.uniform(4.0,6.2),1); sp["stats"]["ER"]+=max(0,ra-random.randint(0,2)); sp["stats"]["SO"]+=random.randint(2,8)
            if rs>ra and random.random()<0.62: sp["stats"]["W"]+=1
        for p in roster:
            p["fatigue"]=min(100,max(0,p.get("fatigue",0)+random.randint(1,5)-2))
            p["condition"]=max(35,min(100,p.get("condition",80)+random.randint(-5,4)))
            if p.get("injury") and random.random()<.22: p["injury"]=None
            elif not p.get("injury") and p["fatigue"]>65 and random.random()<.025: p["injury"]="부상 회복 중"
    fixture["played"]=True
    record={"date":game["schedule"][game["day_index"]]["date"],"home":home,"away":away,"home_score":hrun,"away_score":arun}
    game["game_log"].append(record)
    return record

def advance_week_game(game):
    played=[]
    # 6 game days per in-game week; each team has a game on each day.
    stop=min(len(game["schedule"]),game["day_index"]+6)
    while game["day_index"]<stop:
        day=game["schedule"][game["day_index"]]
        for fixture in day["games"]:
            played.append(simulate_one(game,fixture))
        game["day_index"]+=1
    game["week"]+=1
    for team in game["teams"]:
        for p in game["rosters"][team]:
            if p["status"]=="퓨처스" and not p.get("injury") and p["age"]<28 and random.random()<.18:
                p["ovr"]=min(p["potential"],p["ovr"]+1)
            p["fatigue"]=max(0,p.get("fatigue",0)-random.randint(5,12))
            p["condition"]=min(100,p.get("condition",80)+random.randint(4,12))
    # Gate and sponsor income, payroll cost are recorded as cash transactions.
    for team in game["teams"]:
        fin=game["finances"][team]
        income=int(TEAM_INFO[team]["fans"]*90+game["standings"][team]["W"]*25+6500)
        cost=int(fin["payroll"]/24+5500)
        fin["cash"]+=income-cost
        fin["transactions"].append({"date":game["schedule"][max(0,game["day_index"]-1)]["date"],"type":"주간 결산","amount":income-cost,"note":f"수입 {income:,}만원 · 운영비/연봉 {cost:,}만원"})
    game["reports"].insert(0,f"{game['season']}시즌 {game['week']}주차 완료: {len(played)}경기일을 처리했습니다. 구단별 컨디션과 재정을 확인하세요.")
    return played

def require_game():
    game=get_game()
    if not game:
        flash("먼저 새 게임을 시작하고 구단을 선택하세요.","error")
        return None
    return game


@app.context_processor
def inject_globals():
    state = load_state()
    game = get_game()
    return {"current_season": game["season"] if game else state["season"], "active_rule_count": len(state["active_rules"]), "nav_team": game["team"] if game else session.get("team")}

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
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("office") if user_game_id(user["id"]) else url_for("start_game")
            return redirect(target)
        flash("아이디 또는 비밀번호를 확인해 주세요.", "error")
    return render_template("auth.html", mode="login")

@app.route("/logout", methods=["POST"])
def logout():
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
    standing=sorted(game["teams"],key=lambda t:(game["standings"][t]["W"]/(game["standings"][t]["W"]+game["standings"][t]["L"]+game["standings"][t]["D"] or 1),game["standings"][t]["W"]),reverse=True)
    rank=standing.index(team)+1
    st=game["standings"][team]
    recent=[g for g in game["game_log"] if team in (g["home"],g["away"])][-5:][::-1]
    return render_template("office_game.html",game=game,team=team,info=TEAM_INFO[team],standing=st,rank=rank,roster=active_roster(game,team),recent=recent,played_days=game["day_index"],standings_order=standing)

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
    return render_template("roster_game.html",game=game,team=team,roster=roster,active_count=len(active_roster(game,team)),payroll=game["finances"][team]["payroll"])

@app.route("/roster/<player_id>/move",methods=["POST"])
def move_player(player_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; player=next((p for p in game["rosters"][team] if p["id"]==player_id),None)
    if not player: flash("선수를 찾을 수 없습니다.","error")
    else:
        target=request.form.get("status")
        active=active_roster(game,team)
        if target=="1군" and player["status"]!="1군" and len(active)>=28: flash("현재 데모 엔트리 한도(28명)에 도달했습니다.","error")
        elif target not in ("1군","퓨처스"): flash("올바르지 않은 이동입니다.","error")
        elif player.get("injury") and target=="1군": flash("부상 회복 중인 선수는 1군으로 등록할 수 없습니다.","error")
        else:
            player["status"]=target; game["reports"].insert(0,f"선수단 이동: {player['name']} → {target}"); save_game(game); flash(f"{player['name']} 선수를 {target}로 이동했습니다.","success")
    return redirect(url_for("roster_page"))

@app.route("/schedule")
def schedule_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]
    upcoming=[]
    for day in game["schedule"][game["day_index"]:game["day_index"]+12]:
        for fixture in day["games"]:
            if team in (fixture["home"],fixture["away"]): upcoming.append({**fixture,"date":day["date"]})
    return render_template("schedule_game.html",game=game,team=team,upcoming=upcoming,standings=sorted(game["teams"],key=lambda t:(game["standings"][t]["W"]/(game["standings"][t]["W"]+game["standings"][t]["L"]+game["standings"][t]["D"] or 1),game["standings"][t]["W"]),reverse=True))

@app.route("/season/advance-week",methods=["POST"])
def advance_week():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    if game["day_index"]>=len(game["schedule"]):
        flash("정규시즌 144경기를 모두 진행했습니다. 시즌 결산 화면에서 성적을 확인하세요.","info")
        return redirect(url_for("schedule_page"))
    old=len(game["game_log"]); played=advance_week_game(game); save_game(game)
    flash(f"{game['season']}시즌 {game['week']}주차 진행 완료: 리그 {len(played)}경기 처리.","success")
    return redirect(url_for("schedule_page"))

@app.route("/finance")
def finance_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; fin=game["finances"][team]
    return render_template("finance_game.html",game=game,team=team,fin=fin)

@app.route("/contracts")
def contracts_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]
    return render_template("contracts_game.html",game=game,team=team,free_agents=game["free_agents"],fin=game["finances"][team])

@app.route("/contracts/sign/<player_id>",methods=["POST"])
def sign_free_agent(player_id):
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; player=next((p for p in game["free_agents"] if p["id"]==player_id),None); fin=game["finances"][team]
    if not player: flash("이미 시장에서 사라진 선수입니다.","error")
    elif len(active_roster(game,team))>=28: flash("1군 엔트리가 가득 찼습니다. 먼저 선수를 퓨처스로 이동하세요.","error")
    elif fin["cash"] < player["salary"]: flash("현재 현금 잔액으로는 계약할 수 없습니다.","error")
    else:
        player["team"]=team; player["status"]="1군"; game["rosters"][team].append(player); game["free_agents"].remove(player)
        fin["payroll"]+=player["salary"]; fin["cash"]-=player["salary"]
        fin["transactions"].append({"date":"2027-01-02","type":"FA 계약금(데모)","amount":-player["salary"],"note":f"{player['name']} 영입 · 연봉 {player['salary']}만원"})
        game["reports"].insert(0,f"FA 영입 승인: {player['name']} · OVR {player['ovr']}")
        save_game(game); flash(f"{player['name']} 선수와 계약했습니다.","success")
    return redirect(url_for("contracts_page"))

@app.route("/staff")
def staff_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    staff=[{"role":"감독","name":"박진호","grade":"A","skill":79,"salary":500},{"role":"수석 코치","name":"최성훈","grade":"B","skill":72,"salary":230},{"role":"투수 코치","name":"김태성","grade":"A","skill":81,"salary":260},{"role":"타격 코치","name":"윤정민","grade":"B","skill":74,"salary":240},{"role":"육성 총괄","name":"한도윤","grade":"A","skill":80,"salary":210}]
    return render_template("staff_game.html",game=game,team=game["team"],staff=staff)

@app.route("/history")
def history_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    team=game["team"]; st=game["standings"][team]
    roster=active_roster(game,team)
    batters=sorted([p for p in roster if p["position"] not in ("SP","RP","CP")],key=lambda p:(p["stats"]["H"],p["stats"]["HR"]),reverse=True)
    pitchers=sorted([p for p in roster if p["position"] in ("SP","RP","CP")],key=lambda p:p["stats"]["IP"],reverse=True)
    return render_template("history_game.html",game=game,team=team,standing=st,logs=game["game_log"][-30:][::-1],batters=batters,pitchers=pitchers)

@app.route("/settings")
def settings_page():
    game=require_game()
    if not game: return redirect(url_for("start_game"))
    return render_template("settings_game.html",game=game,team=game["team"])

@app.route("/game/new",methods=["POST"])
def new_game_route():
    # Keep the current save until a replacement team is confirmed on /start.
    return redirect(url_for("start_game"))

@app.route("/rules")
def rules_page():
    return render_template("rules.html", rules=RULES)

@app.route("/rules/<rule_id>")
def rule_detail(rule_id):
    rule = rule_by_id(rule_id)
    if not rule:
        flash("해당 규정을 찾을 수 없습니다.", "error")
        return redirect(url_for("rules_page"))
    return render_template("rule_detail.html", rule=rule)

@app.route("/event/create", methods=["POST"])
def create_event():
    state = load_state()
    if any(e["status"] == "심의 중" for e in state["event_history"]):
        flash("이미 심의 중인 안건이 있습니다. 먼저 투표를 완료하세요.", "error")
        return redirect(url_for("office"))
    # 30% base chance. Demo override lets the user test the system.
    force = request.form.get("force") == "1"
    if not force and random.random() >= 0.30:
        state["last_event_season"] = state["season"]
        save_state(state)
        flash("이번 오프시즌에는 규정 변경 안건이 발의되지 않았습니다.", "info")
        return redirect(url_for("office"))
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
    return redirect(url_for("office"))

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
    flash("리그의 규정 심의 결과가 확정되었습니다.", "success")
    return redirect(url_for("office"))

@app.route("/season/advance", methods=["POST"])
def advance_season():
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
