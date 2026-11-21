# -*- coding: utf-8 -*-
"""data_sources/fangraphs_2026_*_raw.txt 를 apply_real_stats.py가 읽는 CSV로 바꾼다.
구단이 '??'(시즌 중 이적으로 구단이 여러 개인 선수)인 경우, 선수단 데이터에서 그 이름이 한 구단에만 있으면 그 구단으로 넣는다."""
import csv, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from rosters_2026 import REAL_ROSTERS_2026
TEAM = {"SS": "삼성", "LT": "롯데", "KT": "KT", "LG": "LG", "SK": "SSG", "KI": "KIA", "HH": "한화", "DS": "두산", "NC": "NC", "KW": "키움"}
FULL = {"삼성": "삼성 라이온즈", "롯데": "롯데 자이언츠", "KT": "KT 위즈", "LG": "LG 트윈스", "SSG": "SSG 랜더스", "KIA": "KIA 타이거즈", "한화": "한화 이글스", "두산": "두산 베어스", "NC": "NC 다이노스", "키움": "키움 히어로즈"}
ALIAS = {"나종덕": "나균안", "ArielJurado": "후라도", "타케다": "다케다"}   # FanGraphs 표기 → 선수단 데이터 표기
PIT = ("SP", "RP", "CP")

def rows(name):
    out = []
    for line in open(os.path.join(ROOT, "data_sources", name), encoding="utf-8"):
        if line.startswith("#") or not line.strip(): continue
        out += [x.split(",") for x in line.strip().split(";") if x.count(",") >= 4]
    return out

def team_of(name, code, pitcher):
    if code in TEAM: return TEAM[code]
    clubs = [t for t, rs in REAL_ROSTERS_2026.items() if any(r[0] == name and (r[1] in PIT) == pitcher for r in rs)]
    return next(k for k, v in FULL.items() if v == clubs[0]) if len(clubs) == 1 else ""

bat = [["선수명", "팀명", "PA", "OPS", "SB"]]
for n, c, pa, ops, sb in rows("fangraphs_2026_batting_raw.txt"):
    n = ALIAS.get(n, n); t = team_of(n, c, False)
    if t: bat.append([n, t, pa, f"{int(ops)/1000:.3f}", sb])
pit = [["선수명", "팀명", "GS", "IP", "ERA", "WHIP", "SV", "HLD"]]
for n, c, gs, ip, era, whip, sv, hld in rows("fangraphs_2026_pitching_raw.txt"):
    n = ALIAS.get(n, n); t = team_of(n, c, True)
    if t: pit.append([n, t, gs, ip, era, whip, sv, hld])
for name, data in (("2026_batting_for_tool.csv", bat), ("2026_pitching_for_tool.csv", pit)):
    csv.writer(open(os.path.join(ROOT, "data_sources", name), "w", encoding="utf-8", newline="")).writerows(data)
    print(name, len(data) - 1, "명")
