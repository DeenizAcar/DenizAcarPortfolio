"""VAULT — SQLite schema, migrations, and row helpers.

Single-file DB layer. No ORM. JSON columns for list fields (tech_stack,
screenshots, notes) keep the schema flat. Migrations dict allows adding
columns in future versions without losing data.
"""
import sqlite3
import json
from datetime import datetime


MIGRATIONS = {
    1: [
        """CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            slug TEXT NOT NULL,
            description TEXT DEFAULT '',
            long_description TEXT DEFAULT '',
            category TEXT DEFAULT 'other',
            status TEXT DEFAULT 'finished',
            year INTEGER,
            tech_stack TEXT DEFAULT '[]',
            local_path TEXT,
            local_volume_label TEXT,
            github_url TEXT,
            itch_url TEXT,
            live_url TEXT,
            cover_image TEXT,
            screenshots TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_opened_at TEXT,
            notes TEXT DEFAULT '[]',
            is_public INTEGER DEFAULT 0,
            case_study TEXT DEFAULT '',
            is_pinned INTEGER DEFAULT 0,
            is_archived INTEGER DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_category ON projects(category)",
        "CREATE INDEX IF NOT EXISTS idx_status ON projects(status)",
        "CREATE INDEX IF NOT EXISTS idx_year ON projects(year)",
        "CREATE INDEX IF NOT EXISTS idx_archived ON projects(is_archived)",
        "CREATE INDEX IF NOT EXISTS idx_pinned ON projects(is_pinned)",
        """CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS enrichment_cache (
            url TEXT PRIMARY KEY,
            data TEXT,
            cached_at TEXT
        )""",
    ],
}


LIST_FIELDS = ("tech_stack", "screenshots", "notes")
BOOL_FIELDS = ("is_public", "is_pinned", "is_archived")
ALL_FIELDS = (
    "id", "title", "slug", "description", "long_description",
    "category", "status", "year", "tech_stack",
    "local_path", "local_volume_label", "github_url", "itch_url", "live_url",
    "cover_image", "screenshots",
    "created_at", "updated_at", "last_opened_at",
    "notes", "is_public", "case_study", "is_pinned", "is_archived",
)
WRITABLE_FIELDS = tuple(f for f in ALL_FIELDS if f not in (
    "id", "created_at", "updated_at", "slug",
))


def get_connection(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _current_version(conn):
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def init_db(db_path):
    conn = get_connection(db_path)
    try:
        current = _current_version(conn)
        target = max(MIGRATIONS.keys())
        for v in sorted(MIGRATIONS.keys()):
            if v > current:
                for stmt in MIGRATIONS[v]:
                    conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
            (str(target),),
        )
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    for f in LIST_FIELDS:
        if f in d:
            try:
                d[f] = json.loads(d[f] or "[]")
            except Exception:
                d[f] = []
    for f in BOOL_FIELDS:
        if f in d:
            d[f] = bool(d[f])
    return d


def serialize_value(field, value):
    if field in LIST_FIELDS:
        return json.dumps(value or [])
    if field in BOOL_FIELDS:
        return 1 if value else 0
    return value


def insert_project(conn, data):
    now = datetime.utcnow().isoformat(timespec="seconds")
    payload = {
        "title": data.get("title", "Untitled").strip() or "Untitled",
        "slug": data.get("slug") or "",
        "description": data.get("description", ""),
        "long_description": data.get("long_description", ""),
        "category": data.get("category", "other"),
        "status": data.get("status", "finished"),
        "year": data.get("year"),
        "tech_stack": data.get("tech_stack", []),
        "local_path": data.get("local_path"),
        "local_volume_label": data.get("local_volume_label"),
        "github_url": data.get("github_url"),
        "itch_url": data.get("itch_url"),
        "live_url": data.get("live_url"),
        "cover_image": data.get("cover_image"),
        "screenshots": data.get("screenshots", []),
        "notes": data.get("notes", []),
        "is_public": data.get("is_public", False),
        "case_study": data.get("case_study", ""),
        "is_pinned": data.get("is_pinned", False),
        "is_archived": data.get("is_archived", False),
        "last_opened_at": data.get("last_opened_at"),
        "created_at": now,
        "updated_at": now,
    }
    fields = list(payload.keys())
    placeholders = ",".join(["?"] * len(fields))
    values = [serialize_value(f, payload[f]) for f in fields]
    cur = conn.execute(
        f"INSERT INTO projects ({','.join(fields)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return cur.lastrowid


def update_project(conn, project_id, data):
    if not data:
        return
    sets = []
    values = []
    for field, value in data.items():
        if field not in WRITABLE_FIELDS:
            continue
        sets.append(f"{field} = ?")
        values.append(serialize_value(field, value))
    sets.append("updated_at = ?")
    values.append(datetime.utcnow().isoformat(timespec="seconds"))
    values.append(project_id)
    conn.execute(
        f"UPDATE projects SET {', '.join(sets)} WHERE id = ?",
        values,
    )
    conn.commit()


def get_project(conn, project_id):
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return row_to_dict(row)


def list_projects(conn, filters=None):
    filters = filters or {}
    where = []
    params = []

    if not filters.get("include_archived"):
        where.append("is_archived = 0")

    if filters.get("category"):
        where.append("category = ?")
        params.append(filters["category"])

    if filters.get("status"):
        where.append("status = ?")
        params.append(filters["status"])

    if filters.get("year"):
        where.append("year = ?")
        params.append(int(filters["year"]))

    if filters.get("tech"):
        # JSON array contains: simple LIKE on serialized JSON
        where.append("tech_stack LIKE ?")
        params.append(f'%"{filters["tech"]}"%')

    if filters.get("search"):
        q = f"%{filters['search']}%"
        where.append(
            "(title LIKE ? OR description LIKE ? OR long_description LIKE ? OR notes LIKE ? OR tech_stack LIKE ?)"
        )
        params.extend([q, q, q, q, q])

    sql = "SELECT * FROM projects"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY is_pinned DESC, COALESCE(last_opened_at, updated_at) DESC"

    rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(r) for r in rows]


def append_note(conn, project_id, text):
    project = get_project(conn, project_id)
    if not project:
        return None
    notes = project.get("notes", []) or []
    note = {
        "date": datetime.utcnow().isoformat(timespec="seconds"),
        "text": text,
    }
    notes.append(note)
    update_project(conn, project_id, {"notes": notes})
    return note


def soft_delete(conn, project_id):
    update_project(conn, project_id, {"is_archived": True})


def all_tech_stacks(conn):
    rows = conn.execute(
        "SELECT tech_stack FROM projects WHERE is_archived = 0"
    ).fetchall()
    counts = {}
    for r in rows:
        try:
            for t in json.loads(r["tech_stack"] or "[]"):
                if not t:
                    continue
                counts[t] = counts.get(t, 0) + 1
        except Exception:
            pass
    return counts


def all_years(conn):
    rows = conn.execute(
        "SELECT year, COUNT(*) as c FROM projects WHERE is_archived = 0 AND year IS NOT NULL GROUP BY year ORDER BY year DESC"
    ).fetchall()
    return [{"year": r["year"], "count": r["c"]} for r in rows]
