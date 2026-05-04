"""VAULT — local-first personal project archive.

Run:    python vault.py
Opens:  http://localhost:8765 in your default browser.
Data:   ~/.vault/  (vault.db, thumbnails/, config.json)
"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI, HTTPException, UploadFile, File, Form, Request, Body, Query
)
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, StreamingResponse
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

import schema
import enrichers


# ============================================================
# CONFIG
# ============================================================
VAULT_DIR = Path.home() / ".vault"
VAULT_DIR.mkdir(exist_ok=True)
DB_PATH = VAULT_DIR / "vault.db"
THUMBS_DIR = VAULT_DIR / "thumbnails"
THUMBS_DIR.mkdir(exist_ok=True)
CONFIG_PATH = VAULT_DIR / "config.json"
PORT_FILE = VAULT_DIR / "server.port"

DEFAULT_PORT = 8765
SCRIPT_DIR = Path(__file__).resolve().parent
STATIC_DIR = SCRIPT_DIR / "static"

PROJECT_MARKERS = {
    ".git": "git",
    "package.json": "node",
    "Assets": "unity-folder",
    "ProjectSettings": "unity-folder",
    "index.html": "web",
    "Cargo.toml": "rust",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "Gemfile": "ruby",
    "pom.xml": "java",
    "go.mod": "go",
}
PROJECT_MARKER_GLOBS = {
    "*.csproj": "C#",
    "*.sln": "dotnet",
    "*.unity": "Unity",
    "*.uproject": "Unreal",
    "*.xcodeproj": "Xcode",
}
SKIP_DIRS = {
    "node_modules", ".git", "Library", "Temp", "obj", "bin",
    "build", "dist", ".next", ".cache", "__pycache__", ".venv",
    "venv", "env", ".idea", ".vscode", "Logs",
}


# ============================================================
# UTIL
# ============================================================
def slugify(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or f"project-{int(time.time())}"


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def find_free_port(start=DEFAULT_PORT, max_tries=20) -> Optional[int]:
    for i in range(max_tries):
        port = start + i
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            try:
                s.close()
            except Exception:
                pass
            continue
    return None


def is_server_alive(port: int) -> bool:
    try:
        s = socket.socket()
        s.settimeout(0.4)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ============================================================
# VOLUMES
# ============================================================
def list_volumes() -> list[dict]:
    """Return list of mounted drives with labels. Cross-platform."""
    results = []
    system = platform.system()

    if system == "Windows":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            bitmask = kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    drive = f"{letter}:\\"
                    label_buf = ctypes.create_unicode_buffer(1024)
                    fs_buf = ctypes.create_unicode_buffer(1024)
                    rc = kernel32.GetVolumeInformationW(
                        ctypes.c_wchar_p(drive),
                        label_buf, ctypes.sizeof(label_buf),
                        None, None, None,
                        fs_buf, ctypes.sizeof(fs_buf),
                    )
                    label = label_buf.value if rc else ""
                    results.append({
                        "mount": drive,
                        "label": label or letter,
                        "id": label or drive.rstrip("\\"),
                    })
                bitmask >>= 1
        except Exception:
            pass
    else:
        if PSUTIL_AVAILABLE:
            try:
                for p in psutil.disk_partitions(all=False):
                    results.append({
                        "mount": p.mountpoint,
                        "label": Path(p.mountpoint).name or p.device,
                        "id": Path(p.mountpoint).name or p.device,
                    })
            except Exception:
                pass

    return results


def detect_volume_for_path(path: str) -> Optional[str]:
    """Given a path like 'D:/projects', return the volume label."""
    if not path:
        return None
    try:
        path_obj = Path(path).resolve()
    except Exception:
        return None
    for vol in list_volumes():
        try:
            mount = Path(vol["mount"]).resolve()
            if str(path_obj).startswith(str(mount)):
                return vol["label"]
        except Exception:
            continue
    return None


def is_volume_online(label: Optional[str]) -> bool:
    if not label:
        return True
    return any(v["label"] == label for v in list_volumes())


def path_is_accessible(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        return Path(path).exists()
    except Exception:
        return False


# ============================================================
# FOLDER SCAN
# ============================================================
def looks_like_project(folder: Path) -> tuple[bool, list[str]]:
    """Heuristic: does this folder look like a project root?"""
    detected = []
    try:
        children = list(folder.iterdir())
    except (PermissionError, OSError):
        return False, []

    names = {c.name for c in children}
    for marker, label in PROJECT_MARKERS.items():
        if marker in names:
            detected.append(label)

    for pat, label in PROJECT_MARKER_GLOBS.items():
        try:
            if any(folder.glob(pat)):
                detected.append(label)
        except Exception:
            pass

    is_unity = "Assets" in names and "ProjectSettings" in names
    if is_unity:
        detected = [d for d in detected if d != "unity-folder"]
        detected.append("Unity")

    detected = list(dict.fromkeys(detected))
    return bool(detected), detected


def detect_tech_stack(folder: Path, markers: list[str]) -> list[str]:
    """From markers + file inspection, infer tech tags."""
    tech = set()
    mapping = {
        "node": "Node.js",
        "python": "Python",
        "rust": "Rust",
        "ruby": "Ruby",
        "java": "Java",
        "go": "Go",
        "git": None,
        "web": "HTML",
        "Unity": "Unity",
        "Unreal": "Unreal",
        "C#": "C#",
        "dotnet": ".NET",
        "Xcode": "Swift",
    }
    for m in markers:
        if mapping.get(m):
            tech.add(mapping[m])

    try:
        names = {c.name.lower() for c in folder.iterdir()}
    except Exception:
        names = set()

    if "package.json" in names:
        tech.add("JavaScript")
        try:
            pkg = json.loads((folder / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "react" in deps:
                tech.add("React")
            if "vue" in deps:
                tech.add("Vue")
            if "next" in deps:
                tech.add("Next.js")
            if "typescript" in deps:
                tech.add("TypeScript")
        except Exception:
            pass

    if any(n.endswith(".php") for n in names):
        tech.add("PHP")
    if "wp-config.php" in names or "wp-content" in names:
        tech.add("WordPress")

    return sorted(tech)


def folder_size_mb(folder: Path, max_files=2000) -> float:
    total = 0
    count = 0
    try:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                    count += 1
                    if count >= max_files:
                        return round(total / 1_000_000, 1)
                except OSError:
                    continue
    except Exception:
        pass
    return round(total / 1_000_000, 1)


def folder_last_modified(folder: Path) -> Optional[str]:
    try:
        latest = folder.stat().st_mtime
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files[:50]:
                try:
                    m = os.path.getmtime(os.path.join(root, f))
                    if m > latest:
                        latest = m
                except OSError:
                    continue
            break
        return datetime.fromtimestamp(latest).isoformat(timespec="seconds")
    except Exception:
        return None


def readme_preview(folder: Path) -> str:
    for name in ("README.md", "readme.md", "Readme.md", "README.txt", "README"):
        p = folder / name
        if p.exists() and p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                return text[:1500]
            except Exception:
                continue
    return ""


def find_first_image(folder: Path) -> Optional[Path]:
    exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    candidates = []
    try:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if Path(f).suffix.lower() in exts:
                    candidates.append(Path(root) / f)
            if len(candidates) > 30:
                break
    except Exception:
        return None
    if not candidates:
        return None
    # prefer screenshots/cover/banner/preview
    priority = ["cover", "banner", "screenshot", "preview", "thumb", "logo"]
    for p in priority:
        for c in candidates:
            if p in c.name.lower():
                return c
    return candidates[0]


def scan_folder(root: str, max_depth=4) -> list[dict]:
    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        raise FileNotFoundError(f"Path not found: {root}")

    results = []
    errors = []

    def walk(folder: Path, depth: int):
        if depth > max_depth:
            return
        try:
            children = list(folder.iterdir())
        except (PermissionError, OSError) as e:
            errors.append(str(folder))
            return

        # check current folder
        is_proj, markers = looks_like_project(folder)
        if is_proj:
            tech = detect_tech_stack(folder, markers)
            results.append({
                "path": str(folder),
                "name": folder.name,
                "markers": markers,
                "tech_stack": tech,
                "size_mb": folder_size_mb(folder),
                "last_modified": folder_last_modified(folder),
                "readme": readme_preview(folder),
                "volume_label": detect_volume_for_path(str(folder)),
            })
            # don't recurse into a project's subfolders
            return

        for c in children:
            if c.is_dir() and c.name not in SKIP_DIRS and not c.name.startswith("."):
                walk(c, depth + 1)

    walk(root_path, 0)
    return {"candidates": results, "errors_skipped": len(errors)}


# ============================================================
# THUMBNAILS
# ============================================================
PLACEHOLDER_COLORS = [
    "#e63946", "#f4a261", "#2a9d8f", "#264653",
    "#9b5de5", "#00bbf9", "#06d6a0", "#ef476f",
]


def make_placeholder_thumbnail(letter: str, color: str) -> bytes:
    """Generate a simple SVG placeholder. Returned as bytes."""
    letter = (letter or "?")[0].upper()
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400" viewBox="0 0 600 400">
<rect width="600" height="400" fill="{color}"/>
<rect width="600" height="400" fill="url(#g)"/>
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#000" stop-opacity="0"/>
<stop offset="1" stop-color="#000" stop-opacity="0.4"/>
</linearGradient></defs>
<text x="300" y="240" font-family="Georgia, serif" font-size="220" font-style="italic" font-weight="300" fill="#fff" text-anchor="middle" opacity="0.9">{letter}</text>
</svg>'''
    return svg.encode("utf-8")


def save_thumbnail(source_bytes: bytes, ext: str = "jpg") -> str:
    """Save bytes as a resized thumbnail in THUMBS_DIR. Return filename."""
    if not PIL_AVAILABLE:
        # fall back: store as-is
        fname = f"{uuid.uuid4().hex}.{ext}"
        (THUMBS_DIR / fname).write_bytes(source_bytes)
        return fname
    try:
        img = Image.open(io.BytesIO(source_bytes))
        img.thumbnail((1200, 1200))
        if img.mode in ("RGBA", "P"):
            bg = Image.new("RGB", img.size, (10, 10, 12))
            bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = bg
        fname = f"{uuid.uuid4().hex}.jpg"
        img.save(THUMBS_DIR / fname, "JPEG", quality=85)
        return fname
    except Exception:
        fname = f"{uuid.uuid4().hex}.{ext}"
        (THUMBS_DIR / fname).write_bytes(source_bytes)
        return fname


def save_placeholder_for(title: str) -> str:
    letter = (title or "?").strip()[:1]
    color = PLACEHOLDER_COLORS[hash(title) % len(PLACEHOLDER_COLORS)]
    svg = make_placeholder_thumbnail(letter, color)
    fname = f"{uuid.uuid4().hex}.svg"
    (THUMBS_DIR / fname).write_bytes(svg)
    return fname


# ============================================================
# RESUME
# ============================================================
def _open_in_explorer(path: str):
    system = platform.system()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Path missing: {path}")
    if system == "Windows":
        os.startfile(str(p))
    elif system == "Darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def _open_in_vscode(path: str):
    cmd = "code"
    if platform.system() == "Windows":
        # try code, code.cmd
        for c in ("code.cmd", "code"):
            if shutil.which(c):
                cmd = c
                break
    if not shutil.which(cmd):
        raise RuntimeError("VS Code 'code' command not on PATH. Install from VS Code: View → Command Palette → 'Shell Command: Install code'.")
    subprocess.Popen([cmd, path], shell=(platform.system() == "Windows"))


def _open_url(url: str):
    webbrowser.open(url, new=2)


def _open_unity_project(path: str):
    """Open a Unity project via Unity Hub deep link."""
    encoded = urllib_quote(path)
    deep_link = f"unityhub://2022.3.0f1/{encoded}"
    _open_url(deep_link)


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def detect_resume_actions(project: dict) -> list[dict]:
    actions = []
    local_path = project.get("local_path")
    if local_path:
        is_online = is_volume_online(project.get("local_volume_label")) if project.get("local_volume_label") else True
        accessible = path_is_accessible(local_path) if is_online else False
        if accessible:
            actions.append({"key": "explorer", "label": "Open folder", "primary": True})
            p = Path(local_path)
            try:
                children = {c.name for c in p.iterdir()}
            except Exception:
                children = set()
            if (p / ".git").exists() or any((p / m).exists() for m in ("package.json", "pyproject.toml", "Cargo.toml")):
                actions.append({"key": "vscode", "label": "Open in VS Code"})
            if "Assets" in children and "ProjectSettings" in children:
                actions.append({"key": "unity", "label": "Open in Unity"})
            if "index.html" in children:
                actions.append({"key": "browser", "label": "Open index.html"})
        else:
            actions.append({
                "key": "offline",
                "label": f"Files offline" + (f" — connect {project.get('local_volume_label')}" if project.get("local_volume_label") else ""),
                "disabled": True,
            })
    if project.get("github_url"):
        actions.append({"key": "github", "label": "Open GitHub"})
    if project.get("itch_url"):
        actions.append({"key": "itch", "label": "Open itch"})
    if project.get("live_url"):
        actions.append({"key": "live", "label": "Open live demo"})
    return actions


def perform_resume(project: dict, action: str) -> dict:
    if action == "explorer":
        _open_in_explorer(project["local_path"])
    elif action == "vscode":
        _open_in_vscode(project["local_path"])
    elif action == "unity":
        _open_in_explorer(project["local_path"])  # fallback to folder
    elif action == "browser":
        idx = Path(project["local_path"]) / "index.html"
        _open_url(idx.as_uri())
    elif action == "github" and project.get("github_url"):
        _open_url(project["github_url"])
    elif action == "itch" and project.get("itch_url"):
        _open_url(project["itch_url"])
    elif action == "live" and project.get("live_url"):
        _open_url(project["live_url"])
    else:
        raise ValueError(f"Unknown or unavailable resume action: {action}")
    return {"action": action, "ok": True}


# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(title="VAULT", docs_url=None, redoc_url=None)


def get_db(request: Request):
    return request.state.db


@app.middleware("http")
async def db_middleware(request: Request, call_next):
    request.state.db = schema.get_connection(DB_PATH)
    try:
        response = await call_next(request)
    finally:
        try:
            request.state.db.close()
        except Exception:
            pass
    return response


@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>VAULT static files missing</h1>", status_code=500)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/api/projects")
async def api_list_projects(
    request: Request,
    category: Optional[str] = None,
    status: Optional[str] = None,
    year: Optional[int] = None,
    tech: Optional[str] = None,
    search: Optional[str] = None,
    include_archived: bool = False,
):
    conn = get_db(request)
    items = schema.list_projects(conn, {
        "category": category,
        "status": status,
        "year": year,
        "tech": tech,
        "search": search,
        "include_archived": include_archived,
    })
    # decorate with online status + resume actions (cheap)
    for p in items:
        p["online"] = is_volume_online(p.get("local_volume_label")) if p.get("local_volume_label") else True
    return {
        "items": items,
        "total": len(items),
        "facets": {
            "tech": schema.all_tech_stacks(conn),
            "years": schema.all_years(conn),
        }
    }


@app.get("/api/projects/{project_id}")
async def api_get_project(project_id: int, request: Request):
    conn = get_db(request)
    project = schema.get_project(conn, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    project["online"] = is_volume_online(project.get("local_volume_label")) if project.get("local_volume_label") else True
    project["accessible"] = path_is_accessible(project.get("local_path")) if project.get("local_path") else False
    project["resume_actions"] = detect_resume_actions(project)
    return project


class ProjectCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    long_description: Optional[str] = ""
    category: Optional[str] = "other"
    status: Optional[str] = "finished"
    year: Optional[int] = None
    tech_stack: Optional[list[str]] = []
    local_path: Optional[str] = None
    github_url: Optional[str] = None
    itch_url: Optional[str] = None
    live_url: Optional[str] = None
    cover_image: Optional[str] = None
    screenshots: Optional[list[str]] = []
    is_public: Optional[bool] = False
    case_study: Optional[str] = ""


@app.post("/api/projects")
async def api_create_project(payload: ProjectCreate, request: Request):
    conn = get_db(request)
    data = payload.model_dump()
    data["slug"] = slugify(data["title"])
    if data.get("local_path"):
        data["local_volume_label"] = detect_volume_for_path(data["local_path"])
    if not data.get("cover_image"):
        data["cover_image"] = save_placeholder_for(data["title"])
    new_id = schema.insert_project(conn, data)
    return schema.get_project(conn, new_id)


@app.patch("/api/projects/{project_id}")
async def api_update_project(project_id: int, payload: dict = Body(...), request: Request = None):
    conn = get_db(request)
    project = schema.get_project(conn, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if "local_path" in payload and payload["local_path"]:
        payload["local_volume_label"] = detect_volume_for_path(payload["local_path"])
    if "title" in payload and payload["title"]:
        payload["slug"] = slugify(payload["title"])
    schema.update_project(conn, project_id, payload)
    return schema.get_project(conn, project_id)


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: int, request: Request):
    conn = get_db(request)
    schema.soft_delete(conn, project_id)
    return {"ok": True}


class NoteCreate(BaseModel):
    text: str


@app.post("/api/projects/{project_id}/notes")
async def api_add_note(project_id: int, payload: NoteCreate, request: Request):
    conn = get_db(request)
    note = schema.append_note(conn, project_id, payload.text)
    if note is None:
        raise HTTPException(404, "Project not found")
    return note


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    contents = await file.read()
    ext = Path(file.filename or "").suffix.lstrip(".").lower() or "jpg"
    if ext not in ("jpg", "jpeg", "png", "webp", "gif", "svg"):
        ext = "jpg"
    fname = save_thumbnail(contents, ext=ext)
    return {"filename": fname, "url": f"/api/thumbnails/{fname}"}


@app.get("/api/thumbnails/{filename}")
async def api_get_thumbnail(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "bad filename")
    p = THUMBS_DIR / filename
    if not p.exists():
        raise HTTPException(404, "Thumbnail not found")
    media_type = "image/svg+xml" if filename.endswith(".svg") else None
    return FileResponse(str(p), media_type=media_type)


class EnrichRequest(BaseModel):
    url: str


@app.post("/api/enrich/github")
async def api_enrich_github(payload: EnrichRequest, request: Request):
    conn = get_db(request)
    try:
        result = enrichers.enrich_github(payload.url, conn)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Enrichment failed: {e}")

    # if there's a cover image URL, fetch + save thumbnail
    if result.get("cover_image_url"):
        thumb = _fetch_and_save_thumbnail(result["cover_image_url"])
        if thumb:
            result["cover_image"] = thumb
    return result


@app.post("/api/enrich/itch")
async def api_enrich_itch(payload: EnrichRequest, request: Request):
    conn = get_db(request)
    try:
        result = enrichers.enrich_itch(payload.url, conn)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Enrichment failed: {e}")

    if result.get("cover_image_url"):
        thumb = _fetch_and_save_thumbnail(result["cover_image_url"])
        if thumb:
            result["cover_image"] = thumb
    return result


def _fetch_and_save_thumbnail(url: str) -> Optional[str]:
    if not url or not url.startswith("http"):
        return None
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": enrichers.USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        ext = "jpg"
        ct = resp.headers.get("Content-Type", "")
        if "png" in ct:
            ext = "png"
        elif "webp" in ct:
            ext = "webp"
        elif "gif" in ct:
            ext = "gif"
        return save_thumbnail(data, ext=ext)
    except Exception:
        return None


class ScanRequest(BaseModel):
    path: str


@app.post("/api/import/scan")
async def api_import_scan(payload: ScanRequest):
    try:
        return scan_folder(payload.path)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Scan failed: {e}")


class BulkImportItem(BaseModel):
    path: str
    title: str
    category: Optional[str] = "other"
    status: Optional[str] = "finished"
    year: Optional[int] = None
    tech_stack: Optional[list[str]] = []
    description: Optional[str] = ""
    long_description: Optional[str] = ""
    volume_label: Optional[str] = None


class BulkImportRequest(BaseModel):
    items: list[BulkImportItem]


@app.post("/api/import/bulk")
async def api_import_bulk(payload: BulkImportRequest, request: Request):
    conn = get_db(request)
    created = []
    for item in payload.items:
        folder = Path(item.path)
        cover = None
        first_img = find_first_image(folder) if folder.exists() else None
        if first_img:
            try:
                cover = save_thumbnail(first_img.read_bytes(), ext=first_img.suffix.lstrip("."))
            except Exception:
                cover = None
        if not cover:
            cover = save_placeholder_for(item.title)

        data = item.model_dump()
        data["local_path"] = data.pop("path")
        data["local_volume_label"] = data.pop("volume_label", None) or detect_volume_for_path(data["local_path"])
        data["slug"] = slugify(item.title)
        data["cover_image"] = cover
        new_id = schema.insert_project(conn, data)
        created.append(new_id)
    return {"created": created, "count": len(created)}


@app.get("/api/volumes")
async def api_volumes():
    return {"volumes": list_volumes()}


class ResumeRequest(BaseModel):
    action: str


@app.post("/api/resume/{project_id}")
async def api_resume(project_id: int, payload: ResumeRequest, request: Request):
    conn = get_db(request)
    project = schema.get_project(conn, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    try:
        result = perform_resume(project, payload.action)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    schema.update_project(conn, project_id, {"last_opened_at": now_iso()})
    return result


@app.get("/api/projects/{project_id}/export")
async def api_export(project_id: int, request: Request):
    conn = get_db(request)
    project = schema.get_project(conn, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(project, indent=2))
        if project.get("cover_image"):
            p = THUMBS_DIR / project["cover_image"]
            if p.exists():
                z.write(p, f"cover{p.suffix}")
        for s in project.get("screenshots", []):
            p = THUMBS_DIR / s
            if p.exists():
                z.write(p, f"screenshots/{s}")
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project["slug"]}.zip"'},
    )


@app.get("/api/health")
async def api_health():
    return {"ok": True, "version": 1}


# ============================================================
# STARTUP
# ============================================================
def main():
    schema.init_db(DB_PATH)

    # single-instance: if a server is already alive on the recorded port, just
    # open the browser to it and exit.
    if PORT_FILE.exists():
        try:
            existing = int(PORT_FILE.read_text().strip())
            if is_server_alive(existing):
                print(f"VAULT already running on port {existing}. Opening browser.")
                webbrowser.open(f"http://localhost:{existing}", new=2)
                return
        except Exception:
            pass

    port = find_free_port(DEFAULT_PORT)
    if port is None:
        print("No free port available in range. Aborting.")
        sys.exit(1)
    PORT_FILE.write_text(str(port))

    print(f"VAULT — local-first project archive")
    print(f"  data dir: {VAULT_DIR}")
    print(f"  url:      http://localhost:{port}")
    print(f"  PIL:      {'yes' if PIL_AVAILABLE else 'no (thumbnails not resized)'}")
    print(f"  psutil:   {'yes' if PSUTIL_AVAILABLE else 'no'}")
    print()

    def _open_browser():
        time.sleep(0.7)
        try:
            webbrowser.open(f"http://localhost:{port}", new=2)
        except Exception:
            pass

    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        try:
            PORT_FILE.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
