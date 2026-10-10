from flask import Flask, render_template, request, redirect, url_for, flash
from datetime import datetime
import random
import json
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")

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

@app.context_processor
def inject_globals():
    state = load_state()
    return {"current_season": state["season"], "active_rule_count": len(state["active_rules"])}

@app.route("/")
def index():
    state = load_state()
    current_event = next((e for e in reversed(state["event_history"]) if e["status"] == "심의 중"), None)
    return render_template("index.html", state=state, rules=RULES, current_event=current_event)

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
        return redirect(url_for("index"))
    # 30% base chance. Demo override lets the user test the system.
    force = request.form.get("force") == "1"
    if not force and random.random() >= 0.30:
        state["last_event_season"] = state["season"]
        save_state(state)
        flash("이번 오프시즌에는 규정 변경 안건이 발의되지 않았습니다.", "info")
        return redirect(url_for("index"))
    pool = eligible_rules(state)
    if not pool:
        flash("현재 발의 가능한 규정이 없습니다. 시행 규정의 폐지·재개정 기능은 다음 버전에서 추가할 수 있습니다.", "info")
        return redirect(url_for("index"))
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
    return redirect(url_for("index"))

@app.route("/event/<int:event_id>/vote", methods=["POST"])
def vote_event(event_id):
    state = load_state()
    event = next((e for e in state["event_history"] if e["event_id"] == event_id), None)
    if not event or event["status"] != "심의 중":
        flash("투표할 수 있는 이벤트가 아닙니다.", "error")
        return redirect(url_for("index"))
    for proposal in event["proposals"]:
        stance = request.form.get(f"stance_{proposal['rule_id']}")
        if stance not in ("찬성", "반대"):
            flash("모든 안건에 찬성 또는 반대 의견을 선택하세요.", "error")
            return redirect(url_for("index"))
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
    return redirect(url_for("index"))

@app.route("/season/advance", methods=["POST"])
def advance_season():
    state = load_state()
    state["season"] += 1
    for rule in state["active_rules"]:
        if rule["status"] == "시행 예정" and rule["effective_season"] <= state["season"]:
            rule["status"] = "시행 중"
    save_state(state)
    flash(f"{state['season']}시즌으로 진행했습니다. 시행 예정 규정을 적용했습니다.", "success")
    return redirect(url_for("index"))

@app.route("/reset", methods=["POST"])
def reset():
    save_state({
        "season": 2027, "event_history": [], "active_rules": [],
        "next_event_id": 1, "last_event_season": None
    })
    flash("데모 데이터가 초기화되었습니다.", "info")
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)
