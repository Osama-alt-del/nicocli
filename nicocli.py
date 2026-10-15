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


# ─────────────────────────── comments ──────────────────────────

# name -> (256-colour index, basic curses colour, dim?)
COLOR_NAMES = {
    "white": (15, -1), "red": (196, curses.COLOR_RED), "pink": (211, curses.COLOR_MAGENTA),
    "orange": (208, curses.COLOR_YELLOW), "yellow": (226, curses.COLOR_YELLOW),
    "green": (46, curses.COLOR_GREEN), "cyan": (51, curses.COLOR_CYAN),
    "blue": (33, curses.COLOR_BLUE), "purple": (135, curses.COLOR_MAGENTA),
    "black": (244, -1), "white2": (187, -1), "niconicowhite": (187, -1),
    "red2": (124, curses.COLOR_RED), "truered": (124, curses.COLOR_RED),
    "pink2": (207, curses.COLOR_MAGENTA), "orange2": (172, curses.COLOR_YELLOW),
    "passionorange": (172, curses.COLOR_YELLOW), "yellow2": (142, curses.COLOR_YELLOW),
    "madyellow": (142, curses.COLOR_YELLOW), "green2": (35, curses.COLOR_GREEN),
    "elementalgreen": (35, curses.COLOR_GREEN), "cyan2": (37, curses.COLOR_CYAN),
    "blue2": (26, curses.COLOR_BLUE), "marineblue": (26, curses.COLOR_BLUE),
    "purple2": (97, curses.COLOR_MAGENTA), "nobleviolet": (97, curses.COLOR_MAGENTA),
    "black2": (240, -1),
}
HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")
SCROLL_TIME = 4.0   # seconds a scrolling (naka) comment spends crossing the screen
FIXED_TIME = 3.0    # seconds an ue/shita comment stays put


class Comment:
    __slots__ = ("vpos", "start", "end", "lines", "text", "width", "pos", "color",
                 "fork", "big", "posted", "no")

    def __init__(self, raw, fork):
        self.vpos = (raw.get("vposMs") or 0) / 1000
        body = clean(raw.get("body") or "", keep_newlines=True)
        self.lines = [ln.rstrip() for ln in body.split("\n")] or [""]
        while len(self.lines) > 1 and not self.lines[-1]:
            self.lines.pop()
        self.text = " ".join(ln.strip() for ln in self.lines)
        self.width = max(1, max(swidth(ln) for ln in self.lines))
        self.fork = fork
        self.pos, self.color, self.big = "naka", None, False
        for cmd in raw.get("commands") or []:
            if cmd in ("ue", "shita", "naka"):
                self.pos = cmd
            elif cmd in COLOR_NAMES or HEX_RE.fullmatch(cmd):
                self.color = cmd
            elif cmd == "big":
                self.big = True
        if self.pos == "naka":
            self.start, self.end = self.vpos - 1.0, self.vpos + SCROLL_TIME - 1.0
        else:
            self.start, self.end = self.vpos, self.vpos + FIXED_TIME
        self.posted = raw.get("postedAt") or ""
        self.no = raw.get("no") or 0


def fetch_comments(info):
    nv = (info.get("comment") or {}).get("nvComment")
    if not nv:
        return []
    d = http_json(nv["server"].rstrip("/") + "/v1/threads",
                  body={"params": nv["params"], "threadKey": nv["threadKey"],
                        "additionals": {}})
    out = []
    for th in d.get("data", {}).get("threads", []):
        for c in th.get("comments", []):
            if (c.get("score") or 0) <= -4800:   # niconico's own NG-score filter
                continue
            out.append(Comment(c, th.get("fork", "main")))
    out.sort(key=lambda c: c.vpos)
    return out


class Danmaku:
    """Lays comments out in lanes and tells you where each one is at time t."""

    def __init__(self, comments):
        self.cs = sorted(comments, key=lambda c: c.start)
        self.starts = [c.start for c in self.cs]
        self.W = self.H = 0
        self.t = -1e9
        self.active = []

    def reset(self, t, W, H):
        self.W, self.H = W, H
        self.naka = [None] * H          # per lane: (start, speed, width) of the last comment
        self.fixed = [0.0] * H          # per lane: time the ue/shita comment there expires
        self.active = []
        self.idx = bisect.bisect_left(self.starts, t - SCROLL_TIME)
        self.t = t

    def advance(self, t, W, H):
        if (W, H) != (self.W, self.H) or t < self.t or t - self.t > 2.0:
            self.reset(t, W, H)
        while self.idx < len(self.cs) and self.starts[self.idx] <= t:
            c = self.cs[self.idx]
            self.idx += 1
            if c.end > t and H > 0:
                self.spawn(c)
        self.active = [a for a in self.active if a[0].end > t]
        self.t = t

    def _naka_free(self, lane, s, v):
        prev = self.naka[lane]
        if prev is None:
            return True
        ps, pv, pw = prev
        pe = ps + SCROLL_TIME
        if pe <= s:
            return True
        if self.W - pv * (s - ps) + pw >= self.W:      # previous hasn't fully entered yet
            return False
        return self.W - v * (pe - s) >= 1              # we won't catch up before it leaves

    def spawn(self, c):
        rows = min(len(c.lines), self.H)
        span = range(self.H - rows + 1)
        if c.pos == "naka":
            v = (self.W + c.width) / SCROLL_TIME
            lane = next((ln for ln in span
                         if all(self._naka_free(i, c.start, v) for i in range(ln, ln + rows))),
                        None)
            if lane is None:
                lane = random.choice(span)
            for i in range(lane, lane + rows):
                self.naka[i] = (c.start, v, c.width)
            self.active.append((c, lane, v))
        else:
            order = span if c.pos == "ue" else reversed(span)
            lane = next((ln for ln in order
                         if all(self.fixed[i] <= c.start for i in range(ln, ln + rows))), None)
            if lane is None:
                lane = random.choice(span)
            for i in range(lane, lane + rows):
                self.fixed[i] = c.end
            self.active.append((c, lane, 0))

    def placements(self, t):
        """Yield (comment, row, col) for everything on screen, naka first."""
        fixed = []
        for c, lane, v in self.active:
            if c.pos == "naka":
                yield c, lane, int(round(self.W - v * (t - c.start)))
            else:
                fixed.append((c, lane, (self.W - c.width) // 2))
        yield from fixed


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
