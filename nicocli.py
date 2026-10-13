#!/usr/bin/env python3
"""nicocli — a text-only niconico douga (ニコニコ動画) viewer for the terminal.

Browse rankings, search by keyword or tag, read video info, and watch the
comments fly across the screen as scrolling text (弾幕), either on their own
timeline or synced to a real mpv window (needs mpv + yt-dlp).

Standard library only. Run:  python3 nicocli.py [video id or url]
"""

import bisect
import curses
import html
import json
import locale
import os
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# ───────────────────────────── API ─────────────────────────────

UA = "nicocli/1.0 (terminal viewer)"
NV_HEADERS = {"User-Agent": UA, "X-Frontend-Id": "6", "X-Frontend-Version": "0"}
SNAPSHOT_URL = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/video/contents/search"
WATCH_URL = "https://www.nicovideo.jp/watch/"
PER_PAGE = 50

GENRES = [
    ("all", "All genres / 全ジャンル"),
    ("entertainment", "Entertainment / エンターテイメント"),
    ("radio", "Radio / ラジオ"),
    ("music_sound", "Music & Sound / 音楽・サウンド"),
    ("dance", "Dance / ダンス"),
    ("animal", "Animals / 動物"),
    ("nature", "Nature / 自然"),
    ("cooking", "Cooking / 料理"),
    ("traveling_outdoor", "Travel & Outdoor / 旅行・アウトドア"),
    ("vehicle", "Vehicles / 乗り物"),
    ("sports", "Sports / スポーツ"),
    ("society_politics_news", "Society, Politics & News / 社会・政治・時事"),
    ("technology_craft", "Technology & Crafts / 技術・工作"),
    ("commentary_lecture", "Commentary & Lectures / 解説・講座"),
    ("anime", "Anime / アニメ"),
    ("game", "Games / ゲーム"),
    ("other", "Other / その他"),
]
TERMS = [("hour", "hourly"), ("24h", "24 hours"), ("week", "weekly"),
         ("month", "monthly"), ("total", "all time")]
SORTS = [("-viewCounter", "most viewed"), ("-startTime", "newest"),
         ("-commentCounter", "most comments"), ("-likeCounter", "most liked"),
         ("-mylistCounter", "most mylisted"), ("-lastCommentTime", "latest comment"),
         ("-lengthSeconds", "longest")]


class NicoError(Exception):
    pass


def http_json(url, params=None, body=None, timeout=15):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = dict(NV_HEADERS)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            meta = json.load(e).get("meta", {})
            code = meta.get("errorCode") or meta.get("errorMessage")
        except Exception:
            code = None
        raise NicoError(f"HTTP {e.code}" + (f" ({code})" if code else "")) from None
    except (urllib.error.URLError, OSError) as e:
        raise NicoError(f"network error: {getattr(e, 'reason', e)}") from None
    except ValueError:
        raise NicoError("unreadable response from server") from None


def from_essential(e):
    c = e.get("count") or {}
    return {
        "id": e["id"], "title": e.get("title") or "",
        "views": c.get("view") or 0, "comments": c.get("comment") or 0,
        "mylists": c.get("mylist") or 0, "likes": c.get("like") or 0,
        "length": e.get("duration") or 0, "date": e.get("registeredAt") or "",
        "owner": (e.get("owner") or {}).get("name") or "",
    }


def search(query, tag=False, sort="-viewCounter", page=0):
    params = {
        "q": query,
        "targets": "tagsExact" if tag else "title,description,tags",
        "fields": "contentId,title,viewCounter,commentCounter,mylistCounter,"
                  "likeCounter,lengthSeconds,startTime",
        "_sort": sort, "_offset": page * PER_PAGE, "_limit": PER_PAGE,
        "_context": "nicocli",
    }
    d = http_json(SNAPSHOT_URL, params)
    items = [{
        "id": v["contentId"], "title": v.get("title") or "",
        "views": v.get("viewCounter") or 0, "comments": v.get("commentCounter") or 0,
        "mylists": v.get("mylistCounter") or 0, "likes": v.get("likeCounter") or 0,
        "length": v.get("lengthSeconds") or 0, "date": v.get("startTime") or "",
        "owner": "",
    } for v in d.get("data", [])]
    total = d.get("meta", {}).get("totalCount", 0)
    return items, (page + 1) * PER_PAGE < min(total, 100000), total


def ranking(genre, term, page=0):
    d = http_json(f"https://nvapi.nicovideo.jp/v1/ranking/genre/{genre}",
                  {"term": term, "page": page + 1})["data"]
    return [from_essential(e) for e in d.get("items", [])], bool(d.get("hasNext"))


def user_videos(uid, page=0):
    d = http_json(f"https://nvapi.nicovideo.jp/v3/users/{uid}/videos",
                  {"sortKey": "registeredAt", "sortOrder": "desc",
                   "pageSize": PER_PAGE, "page": page + 1})["data"]
    items = [from_essential(i["essential"]) for i in d.get("items", [])]
    return items, (page + 1) * PER_PAGE < d.get("totalCount", 0), d.get("totalCount", 0)


def watch_info(vid):
    track = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    track += f"_{int(time.time() * 1000)}"
    return http_json(f"https://www.nicovideo.jp/api/watch/v3_guest/{vid}",
                     {"actionTrackId": track})["data"]


def parse_video_id(text):
    m = re.search(r"\b((?:sm|so|nm)\d+)", text.strip())
    if m:
        return m.group(1)
    return f"sm{text.strip()}" if text.strip().isdigit() else None


# ─────────────────────────── text utils ────────────────────────

def cw(ch):
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def swidth(s):
    return sum(cw(c) for c in s)


def clean(s, keep_newlines=False):
    out = []
    for ch in s.replace("\r\n", "\n").replace("\t", " "):
        if ch == "\n":
            out.append("\n" if keep_newlines else " ")
        elif unicodedata.category(ch) not in ("Cc", "Cf", "Mn", "Cs"):
            out.append(ch)
    return "".join(out)


def clip(s, width):
    if width <= 0:
        return ""
    if swidth(s) <= width:
        return s
    out, w = [], 0
    for ch in s:
        if w + cw(ch) > width - 1:
            break
        out.append(ch)
        w += cw(ch)
    return "".join(out) + "…"


def wrap(text, width):
    lines = []
    for para in text.split("\n"):
        line, w = [], 0
        for ch in para:
            c = cw(ch)
            if w + c > width:
                lines.append("".join(line))
                line, w = [], 0
                if ch == " ":
                    continue
            line.append(ch)
            w += c
        lines.append("".join(line))
    return lines


def html_to_text(s):
    s = re.sub(r"(?i)<br\s*/?>|</p>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return clean(html.unescape(s), keep_newlines=True).strip()


def fmt_time(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def fmt_date(iso):
    return iso[:16].replace("T", " ") if iso else "?"
