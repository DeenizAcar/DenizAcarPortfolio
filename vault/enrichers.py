"""VAULT — URL enrichment for GitHub and itch.io.

GitHub: REST API, no auth (60 req/hr limit). Pulls metadata, languages, README.
itch.io: HTML scrape via stdlib + minimal regex (no BeautifulSoup needed for
what we extract: og:image, og:description, screenshot list).

All results cached in enrichment_cache table for 1 hour to dodge rate limits.
"""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


USER_AGENT = "VAULT/1.0 (personal project archive)"
CACHE_TTL_SECONDS = 3600


def _http_get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _cache_get(conn, url):
    if conn is None:
        return None
    row = conn.execute(
        "SELECT data, cached_at FROM enrichment_cache WHERE url = ?", (url,)
    ).fetchone()
    if not row:
        return None
    try:
        age = (datetime.utcnow() - datetime.fromisoformat(row["cached_at"])).total_seconds()
        if age < CACHE_TTL_SECONDS:
            return json.loads(row["data"])
    except Exception:
        return None
    return None


def _cache_set(conn, url, result):
    if conn is None:
        return
    conn.execute(
        "INSERT OR REPLACE INTO enrichment_cache (url, data, cached_at) VALUES (?, ?, ?)",
        (url, json.dumps(result), datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()


def parse_github_url(url):
    m = re.match(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/?#\s]+)", url.strip())
    if not m:
        return None, None
    owner = m.group(1)
    repo = m.group(2).rstrip(".git")
    return owner, repo


def enrich_github(url, conn=None):
    cached = _cache_get(conn, url)
    if cached is not None:
        return cached

    owner, repo = parse_github_url(url)
    if not owner:
        raise ValueError("Not a recognizable GitHub repo URL")

    api = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        data = _http_get_json(api)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RuntimeError(
                "GitHub API rate limited (60/hr without auth). Try again in ~60 minutes."
            )
        if e.code == 404:
            raise RuntimeError("Repo not found, private, or moved.")
        raise

    languages = []
    try:
        lang_data = _http_get_json(f"https://api.github.com/repos/{owner}/{repo}/languages")
        languages = list(lang_data.keys())
    except Exception:
        if data.get("language"):
            languages = [data["language"]]

    readme_text = ""
    cover_image_url = None
    try:
        rd = _http_get_json(f"https://api.github.com/repos/{owner}/{repo}/readme")
        if rd.get("content"):
            readme_text = base64.b64decode(rd["content"]).decode("utf-8", errors="replace")
            img = re.search(r"!\[[^\]]*\]\(([^)\s]+)", readme_text)
            if img:
                src = img.group(1)
                if src.startswith("http"):
                    cover_image_url = src
                else:
                    branch = data.get("default_branch", "main")
                    cover_image_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{src.lstrip('/')}"
    except Exception:
        pass

    year = None
    pushed = data.get("pushed_at") or data.get("created_at")
    if pushed:
        try:
            year = int(pushed[:4])
        except Exception:
            pass

    result = {
        "title": data.get("name") or repo,
        "description": (data.get("description") or "")[:500],
        "tech_stack": languages,
        "github_url": data.get("html_url") or url,
        "live_url": data.get("homepage") or None,
        "long_description": readme_text,
        "cover_image_url": cover_image_url,
        "year": year,
        "stars": data.get("stargazers_count", 0),
        "default_branch": data.get("default_branch"),
        "pushed_at": data.get("pushed_at"),
    }
    _cache_set(conn, url, result)
    return result


def _meta_content(html, prop):
    pat1 = re.compile(
        r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']+)["\']',
        re.I,
    )
    pat2 = re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\']',
        re.I,
    )
    m = pat1.search(html) or pat2.search(html)
    return m.group(1) if m else None


def enrich_itch(url, conn=None):
    cached = _cache_get(conn, url)
    if cached is not None:
        return cached

    if "itch.io" not in url:
        raise ValueError("Not an itch.io URL")

    try:
        html = _http_get_html(url)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"itch.io returned HTTP {e.code}")

    title = _meta_content(html, "og:title") or ""
    description = _meta_content(html, "og:description") or ""
    cover = _meta_content(html, "og:image")

    screenshots = []
    for m in re.finditer(
        r'<a[^>]+class=["\'][^"\']*screenshot_link[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
        html, re.I,
    ):
        screenshots.append(m.group(1))
    if not screenshots:
        for m in re.finditer(
            r'<img[^>]+class=["\'][^"\']*screenshot[^"\']*["\'][^>]+src=["\']([^"\']+)["\']',
            html, re.I,
        ):
            screenshots.append(m.group(1))

    # de-dupe, cap
    seen = set()
    unique_screens = []
    for s in screenshots:
        if s not in seen:
            seen.add(s)
            unique_screens.append(s)
    unique_screens = unique_screens[:8]

    result = {
        "title": title.strip(),
        "description": description.strip()[:500],
        "cover_image_url": cover,
        "screenshot_urls": unique_screens,
        "itch_url": url,
    }
    _cache_set(conn, url, result)
    return result


def download_image(url, dest_path):
    """Download an image URL to dest_path. Returns True on success."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False
