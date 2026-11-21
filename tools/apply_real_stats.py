# -*- coding: utf-8 -*-
"""실제 시즌 기록으로 rosters_2026.py의 OVR(과 출생연도)을 다시 계산하는 도구.

사용법:
  python tools/apply_real_stats.py --batting 타자기록.csv --pitching 투수기록.csv            # 바뀔 내용만 출력
  python tools/apply_real_stats.py --batting 타자기록.csv --pitching 투수기록.csv --write    # rosters_2026.py에 반영

기록 파일: 첫 줄이 열 이름인 CSV 또는 탭 구분 텍스트(UTF-8). KBO 기록실 표를 복사해 붙여 넣은 형태를 그대로 읽는다.
  타자에 필요한 열: 선수명, 팀명, PA(타석), 그리고 OPS  (OPS가 없으면 OBP와 SLG, 그것도 없으면 AVG만으로 추정)
  투수에 필요한 열: 선수명, 팀명, IP(이닝), ERA  (있으면 WHIP, SV, HLD도 사용)
  선택 열: 생년 또는 생년월일 (있으면 출생연도도 고친다)
같은 구단에 같은 이름이 둘 이상이면 포지션(투수/타자)으로 구분하고, 그래도 구분되지 않으면 건드리지 않고 보고한다.

계산식 (게임 기본안):
  타자  성적 OVR = 66 + (OPS - 0.730) × 82 + 포지션 보정(포수·유격수 +3, 2루수·중견수 +2, 3루수 +1, 1루수·좌익수·지명타자 -1)
                      + 도루 보정(도루 15개당 +1, 최대 +3)
        신뢰도 = min(0.75, 타석 / 400)
  선발  성적 OVR = 66 + (4.30 - ERA) × 8.5 + (1.38 - WHIP) × 8 + 이닝 가산(120이닝 초과 20이닝당 +1, 최대 +3)
        신뢰도 = min(0.75, 이닝 / 120)
  불펜  성적 OVR = 62 + (4.30 - ERA) × 6 + (1.38 - WHIP) × 6 + min(5, (세이브 + 홀드) / 7)
        신뢰도 = min(0.75, 이닝 / 50)
  최종 OVR = 신뢰도 × 성적 OVR + (1 - 신뢰도) × 기존 OVR,  40~92 범위로 제한
  기준점: 리그 평균 수준(OPS 0.730, 선발 ERA 4.30)이 OVR 66, OPS 0.900이 약 80, 선발 ERA 3.00이 약 77.
  → 기록이 충분한 선수는 실제 성적이 75%, 기존 평가가 25% 반영되고, 출전이 적은 선수는 기존 평가에 가깝게 남는다.
  선발 판정은 선발 등판 8경기 이상(또는 등판 대부분이 선발)이고, 30이닝 이상 던진 투수는 실제 기용에 맞춰 보직(SP/RP/CP)도 고친다.
"""
import argparse
import csv
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROSTER_FILE = os.path.join(ROOT, "rosters_2026.py")

TEAM_ALIASES = {
    "LG": "LG 트윈스", "KT": "KT 위즈", "SSG": "SSG 랜더스", "NC": "NC 다이노스", "두산": "두산 베어스",
    "KIA": "KIA 타이거즈", "롯데": "롯데 자이언츠", "삼성": "삼성 라이온즈", "한화": "한화 이글스", "키움": "키움 히어로즈",
}
COLS = {
    "name": ["선수명", "선수", "이름", "name", "player"], "team": ["팀명", "팀", "구단", "team"],
    "pa": ["pa", "타석"], "ops": ["ops"], "obp": ["obp", "출루율"], "slg": ["slg", "장타율"], "avg": ["avg", "타율", "ba"],
    "ip": ["ip", "이닝"], "era": ["era", "평균자책점", "방어율"], "whip": ["whip"], "sv": ["sv", "세이브"], "hld": ["hld", "홀드"],
    "birth": ["생년", "생년월일", "출생", "birth"], "sb": ["sb", "도루"], "gs": ["gs", "선발"],
}
POS_ADJ = {"C": 3, "SS": 3, "2B": 2, "CF": 2, "3B": 1, "RF": 0, "1B": -1, "LF": -1, "DH": -1}
PITCHERS = ("SP", "RP", "CP")
MAX_WEIGHT = 0.75   # 한 시즌 기록이 아무리 많아도 기존 평가를 25%는 남긴다(한 해 부진·반짝 활약으로 평가가 전부 바뀌지 않게)


def read_table(path):
    text = open(path, encoding="utf-8-sig").read()
    delim = "\t" if text.count("\t") > text.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    header = [h.strip().lower() for h in rows[0]]
    idx = {}
    for key, names in COLS.items():
        for n in names:
            if n in header:
                idx[key] = header.index(n); break
    out = []
    for r in rows[1:]:
        if len(r) < len(header) or not r[idx["name"]].strip():
            continue
        out.append({k: r[i].strip() for k, i in idx.items()})
    return out


def num(v, default=None):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def innings(v):
    """'123 1/3', '123.1'(=123과 1/3), '123' 형식을 이닝 수로 바꾼다."""
    s = str(v).strip()
    m = re.match(r"^(\d+)\s+(\d)/3$", s)
    if m: return int(m.group(1)) + int(m.group(2)) / 3
    m = re.match(r"^(\d+)\.(\d)$", s)
    if m and m.group(2) in "012": return int(m.group(1)) + int(m.group(2)) / 3
    return num(s, 0.0) or 0.0


def team_name(v):
    v = v.strip()
    return TEAM_ALIASES.get(v.upper(), TEAM_ALIASES.get(v, v))


def batter_ovr(row, pos, old):
    pa = num(row.get("pa"), 0) or 0
    ops = num(row.get("ops"))
    if ops is None and num(row.get("obp")) is not None and num(row.get("slg")) is not None:
        ops = num(row["obp"]) + num(row["slg"])
    if ops is None and num(row.get("avg")) is not None:
        ops = 0.730 + (num(row["avg"]) - 0.265) * 2.6      # 타율만 있을 때의 거친 추정
    if ops is None:
        return None
    perf = 66 + (ops - 0.730) * 82 + POS_ADJ.get(pos, 0) + min(3.0, (num(row.get("sb"), 0) or 0) / 15)
    w = min(MAX_WEIGHT, pa / 400)
    return max(40, min(92, round(w * perf + (1 - w) * old)))


def pitcher_ovr(row, pos, old):
    ip = innings(row.get("ip", 0)); era = num(row.get("era"))
    if era is None:
        return None
    whip = num(row.get("whip"))
    gs = num(row.get("gs"))
    # 선발 판정: 선발 8경기 이상, 또는 등판 대부분이 선발인 경우(부상 등으로 이닝이 적은 선발). 선발 등판 수가 없으면 선수단 데이터의 보직.
    starter = (gs >= 8 or (gs >= 3 and gs * 5 >= ip)) if gs is not None else (pos == "SP")
    if starter:
        # 선발: ERA·WHIP에 이닝 소화 가산(120이닝을 넘는 20이닝마다 +1, 최대 +3)
        perf = 66 + (4.30 - era) * 8.5 + ((1.38 - whip) * 8 if whip is not None else 0) + max(0.0, min(3.0, (ip - 120) / 20))
    else:
        # 불펜: 이닝이 적어 ERA가 크게 흔들리므로 선발보다 기준점과 기울기를 낮추고, 세이브·홀드로 중요한 상황 기용을 반영한다
        perf = 62 + (4.30 - era) * 6 + ((1.38 - whip) * 6 if whip is not None else 0) + min(5.0, ((num(row.get("sv"), 0) or 0) + (num(row.get("hld"), 0) or 0)) / 7)
    w = min(MAX_WEIGHT, ip / (120 if starter else 50))
    return max(40, min(92, round(w * perf + (1 - w) * old)))


def pitcher_role(row, pos):
    """실제 기용에 맞춘 보직: 선발 8경기 이상 SP, 세이브 10개 이상 CP, 그 밖에 30이닝 이상 던진 투수는 RP. 기록이 적으면 기존 보직 유지."""
    gs = num(row.get("gs")); sv = num(row.get("sv"), 0) or 0
    if gs is None or innings(row.get("ip", 0)) < 30: return pos
    if gs >= 8: return "SP"
    if sv >= 10: return "CP"
    return "RP"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batting"); ap.add_argument("--pitching"); ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    if not args.batting and not args.pitching:
        ap.error("--batting 또는 --pitching 파일을 하나 이상 지정하세요.")
    sys.path.insert(0, ROOT)
    from rosters_2026 import REAL_ROSTERS_2026
    stats = {}
    for path, kind in ((args.batting, "bat"), (args.pitching, "pit")):
        if not path: continue
        for row in read_table(path):
            stats.setdefault((team_name(row.get("team", "")), row["name"], kind), []).append(row)
    src = open(ROSTER_FILE, encoding="utf-8").read()
    changes, ambiguous, matched = [], [], set()
    for team, rows in REAL_ROSTERS_2026.items():
        start = src.index(f'"{team}": [')
        end = src.index("],\n", start)
        block = src[start:end]
        for name, pos, birth, ovr, note in rows:
            kind = "pit" if pos in PITCHERS else "bat"
            found = stats.get((team, name, kind))
            if not found: continue
            same = [r for r in rows if r[0] == name and (r[1] in PITCHERS) == (pos in PITCHERS)]
            if len(found) > 1 or len(same) > 1:
                ambiguous.append(f"{team} {name}"); continue
            row = found[0]; matched.add((team, name, kind))
            new = pitcher_ovr(row, pos, ovr) if kind == "pit" else batter_ovr(row, pos, ovr)
            new_pos = pitcher_role(row, pos) if kind == "pit" else pos
            new_birth = birth
            if row.get("birth") and re.match(r"^(19|20)\d\d", row["birth"]):
                new_birth = int(row["birth"][:4])
            if new is None or (new == ovr and new_birth == birth and new_pos == pos): continue
            old_t = f'("{name}","{pos}",{birth},{ovr},"{note}")'; new_t = f'("{name}","{new_pos}",{new_birth},{new},"{note}")'
            if block.count(old_t) != 1:
                ambiguous.append(f"{team} {name}"); continue
            block = block.replace(old_t, new_t)
            changes.append((team, name, pos + ("→" + new_pos if new_pos != pos else ""), ovr, new, birth, new_birth))
        src = src[:start] + block + src[end:]
    unmatched = sorted(k for k in stats if k not in matched)
    for team, name, pos, old, new, b0, b1 in sorted(changes, key=lambda c: (c[0], -abs(c[4] - c[3]))):
        print(f"{team:10s} {name:8s} {pos:6s} OVR {old} → {new}" + (f" · 출생 {b0} → {b1}" if b0 != b1 else ""))
    print(f"\n바뀌는 선수 {len(changes)}명 / 기록 파일의 선수 {len(stats)}명 중 선수단에서 찾지 못한 선수 {len(unmatched)}명")
    if unmatched: print("찾지 못함(이름 또는 구단이 선수단 데이터와 다름):", ", ".join(f"{t.split()[0]} {n}" for t, n, _ in unmatched))
    if ambiguous: print("동명이인 등으로 건드리지 않음:", ", ".join(sorted(set(ambiguous))))
    if args.write:
        open(ROSTER_FILE, "w", encoding="utf-8").write(src)
        print("rosters_2026.py에 반영했습니다. 새 게임을 시작하면 적용됩니다(진행 중인 세이브에는 적용되지 않습니다).")
    else:
        print("미리 보기만 했습니다. 반영하려면 --write 를 붙여 다시 실행하세요.")


if __name__ == "__main__":
    main()
