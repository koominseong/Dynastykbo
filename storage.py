# -*- coding: utf-8 -*-
"""계정·세이브·로그인 기록 저장소.

- 운영(Render): 환경변수 SUPABASE_URL, SUPABASE_SECRET_KEY 가 있으면 Supabase Postgres에 저장한다.
  테이블 정의는 supabase_schema.sql 참고. 서버의 secret key로만 접근하며 브라우저에는 키를 내보내지 않는다.
- 로컬 개발: 위 환경변수가 없으면 DATA_DIR 아래 SQLite 파일과 JSON 파일에 저장한다.

두 저장소는 같은 함수 이름을 갖는다:
  upsert_profile(user_id, email, display_name)
  load_game(user_id) -> dict | None          계정당 세이브 1개
  save_game(user_id, game)
  log_auth_event(user_id, email, event, result)
  auth_logs(user_id, limit=100) -> [{"email","event","result","occurred_at"}]
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import requests


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StorageError(RuntimeError):
    """저장소 읽기·쓰기 실패. 세이브가 없는 것과 구분하기 위해 예외로 올린다."""


class SupabaseStore:
    kind = "supabase"

    def __init__(self, url, secret_key, timeout=20):
        self.base = url.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()
        # 새 secret key(sb_secret_...)는 apikey 헤더로 보낸다. 기존 service_role JWT도 apikey와
        # 같은 값을 Authorization에 넣는 것은 허용되므로 두 종류 키 모두 이 헤더 조합으로 동작한다.
        self.http.headers.update({
            "apikey": secret_key,
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
        })

    def _request(self, method, table, params=None, body=None, prefer=None):
        headers = {"Prefer": prefer} if prefer else {}
        try:
            res = self.http.request(method, f"{self.base}/rest/v1/{table}", params=params,
                                    data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
                                    headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise StorageError(f"Supabase 요청 실패({table}): {exc}") from exc
        if res.status_code >= 400:
            raise StorageError(f"Supabase 오류({table}) HTTP {res.status_code}: {res.text[:300]}")
        return res

    def upsert_profile(self, user_id, email, display_name):
        self._request("POST", "profiles", params={"on_conflict": "user_id"},
                      body={"user_id": user_id, "email": email, "display_name": display_name, "last_login_at": _now_iso()},
                      prefer="resolution=merge-duplicates,return=minimal")

    def load_game(self, user_id):
        res = self._request("GET", "game_saves", params={"user_id": f"eq.{user_id}", "select": "data"})
        rows = res.json()
        return rows[0]["data"] if rows else None

    def save_game(self, user_id, game):
        self._request("POST", "game_saves", params={"on_conflict": "user_id"},
                      body={"user_id": user_id, "game_id": game.get("game_id"), "team": game.get("team"),
                            "season": game.get("season"), "data": game, "updated_at": _now_iso()},
                      prefer="resolution=merge-duplicates,return=minimal")

    def log_auth_event(self, user_id, email, event, result):
        self._request("POST", "auth_logs",
                      body={"user_id": user_id, "email": (email or "")[:200], "event": event[:40], "result": result[:40]},
                      prefer="return=minimal")

    def auth_logs(self, user_id, limit=100):
        res = self._request("GET", "auth_logs", params={"user_id": f"eq.{user_id}", "select": "email,event,result,occurred_at",
                                                         "order": "occurred_at.desc", "limit": str(int(limit))})
        return res.json()


class LocalStore:
    """로컬 개발용. Render에서는 재배포 때 파일이 사라지므로 운영에 쓰지 않는다."""
    kind = "local"

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.game_dir = os.path.join(data_dir, "games")
        os.makedirs(self.game_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "dynasty_local.db")
        with self._db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS profiles (user_id TEXT PRIMARY KEY, email TEXT, display_name TEXT, last_login_at TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS auth_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, email TEXT, event TEXT NOT NULL, result TEXT NOT NULL, occurred_at TEXT NOT NULL)")

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _path(self, user_id):
        safe = "".join(ch for ch in str(user_id) if ch.isalnum() or ch in "-_")
        return os.path.join(self.game_dir, f"{safe}.json")

    def upsert_profile(self, user_id, email, display_name):
        with self._db() as conn:
            conn.execute("INSERT INTO profiles(user_id,email,display_name,last_login_at) VALUES(?,?,?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET email=excluded.email, display_name=excluded.display_name, last_login_at=excluded.last_login_at",
                         (user_id, email, display_name, _now_iso()))

    def load_game(self, user_id):
        try:
            with open(self._path(user_id), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"세이브 파일을 읽을 수 없습니다: {exc}") from exc

    def save_game(self, user_id, game):
        path = self._path(user_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(game, f, ensure_ascii=False)
        os.replace(tmp, path)

    def log_auth_event(self, user_id, email, event, result):
        with self._db() as conn:
            conn.execute("INSERT INTO auth_logs(user_id,email,event,result,occurred_at) VALUES(?,?,?,?,?)",
                         (user_id, (email or "")[:200], event[:40], result[:40], _now_iso()))

    def auth_logs(self, user_id, limit=100):
        with self._db() as conn:
            rows = conn.execute("SELECT email,event,result,occurred_at FROM auth_logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
                                (user_id, int(limit))).fetchall()
        return [dict(r) for r in rows]


def make_store(data_dir):
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if url and key:
        return SupabaseStore(url, key)
    return LocalStore(data_dir)
