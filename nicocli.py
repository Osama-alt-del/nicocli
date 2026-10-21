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


REASONS = {
    "DOMESTIC_VIDEO": "region-locked: only viewable from inside Japan",
    "PPV_VIDEO": "pay-per-view video",
    "CHANNEL_MEMBER_ONLY": "channel members only",
    "PREMIUM_ONLY": "premium members only",
    "HIDDEN_VIDEO": "private video",
    "DELETED_VIDEO": "video was deleted",
    "NOT_FOUND": "not found",
}


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
            err = json.load(e)
            meta = err.get("meta", {})
            code = (err.get("data") or {}).get("reasonCode") or meta.get("errorCode") \
                or meta.get("errorMessage")
            code = REASONS.get(code, code)
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


# ───────────────────────── mpv bridge ──────────────────────────

class Mpv:
    """Runs mpv in its own window and talks to it over its JSON IPC socket."""

    def __init__(self, url, title):
        self.path = os.path.join(tempfile.gettempdir(), f"nicocli-mpv-{os.getpid()}.sock")
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.proc = subprocess.Popen(
            ["mpv", "--force-window=immediate", "--no-terminal", f"--title={title}",
             f"--input-ipc-server={self.path}", url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.sock, self.buf, self.rid = None, b"", 0

    def alive(self):
        return self.proc.poll() is None

    def _connect(self):
        if self.sock is None and os.path.exists(self.path):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.25)
                s.connect(self.path)
                self.sock = s
            except OSError:
                pass
        return self.sock is not None

    def command(self, *args):
        if not self._connect():
            return None
        self.rid += 1
        try:
            self.sock.sendall(json.dumps({"command": list(args), "request_id": self.rid})
                              .encode() + b"\n")
            while True:
                while b"\n" in self.buf:
                    line, self.buf = self.buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("request_id") == self.rid:
                        return msg
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise OSError("closed")
                self.buf += chunk
        except OSError:
            self.sock, self.buf = None, b""
            return None

    def get(self, prop):
        r = self.command("get_property", prop)
        return r.get("data") if r and r.get("error") == "success" else None

    def close(self):
        if self.alive():
            self.command("quit")
            try:
                self.proc.wait(1)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
        if self.sock:
            self.sock.close()
        if os.path.exists(self.path):
            os.unlink(self.path)


def have_streamer():
    return shutil.which("yt-dlp") or shutil.which("youtube-dl")


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


# ─────────────────────────── history ───────────────────────────

HIST_PATH = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                         "nicocli", "history.json")


def load_history():
    try:
        with open(HIST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_history(hist):
    try:
        os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
        with open(HIST_PATH, "w", encoding="utf-8") as f:
            json.dump(hist[:300], f, ensure_ascii=False)
    except OSError:
        pass


# ─────────────────────────── sources ───────────────────────────

class Source:
    """Something that produces pages of videos for the list screen."""
    paged = True

    def __init__(self):
        self.page, self.total = 0, None

    def fetch(self):
        raise NotImplementedError

    def keys(self):
        return {}


class RankingSource(Source):
    def __init__(self, genre, label):
        super().__init__()
        self.genre, self.label, self.term = genre, label, 1

    def title(self):
        return f"Ranking · {self.label} · {TERMS[self.term][1]}"

    def fetch(self):
        return ranking(self.genre, TERMS[self.term][0], self.page)

    def keys(self):
        def cycle(app):
            self.term = (self.term + 1) % len(TERMS)
            self.page = 0
            return True
        return {"t": ("term", cycle)}


class SearchSource(Source):
    def __init__(self, query, tag):
        super().__init__()
        self.query, self.tag, self.sort = query, tag, 0

    def title(self):
        kind = "Tag" if self.tag else "Search"
        tot = f" · {self.total:,} hits" if self.total is not None else ""
        return f"{kind}: {self.query} · {SORTS[self.sort][1]}{tot}"

    def fetch(self):
        items, more, self.total = search(self.query, self.tag, SORTS[self.sort][0], self.page)
        return items, more

    def keys(self):
        def cycle(app):
            self.sort = (self.sort + 1) % len(SORTS)
            self.page = 0
            return True

        def requery(app):
            q = app.prompt("Tag: " if self.tag else "Search: ", self.query)
            if not q:
                return False
            self.query, self.page = q, 0
            return True
        return {"s": ("sort", cycle), "/": ("new query", requery)}


class UserSource(Source):
    def __init__(self, uid, name):
        super().__init__()
        self.uid, self.name = uid, name

    def title(self):
        tot = f" · {self.total:,} videos" if self.total is not None else ""
        return f"Uploads by {self.name}{tot}"

    def fetch(self):
        items, more, self.total = user_videos(self.uid, self.page)
        return items, more


class HistorySource(Source):
    paged = False

    def __init__(self, app):
        super().__init__()
        self.app = app

    def title(self):
        return f"History · {len(self.app.history)} videos"

    def fetch(self):
        return list(self.app.history), False

    def keys(self):
        def clear(app):
            if app.confirm("Clear the whole watch history?"):
                app.history.clear()
                save_history(app.history)
            return True
        return {"X": ("clear", clear)}


# ───────────────────────────── UI ──────────────────────────────

BACK = ("q", "\x1b", "h", curses.KEY_LEFT, curses.KEY_BACKSPACE, "\x7f")
ENTER = ("\n", "\r", curses.KEY_ENTER, "l", curses.KEY_RIGHT)
UP = (curses.KEY_UP, "k")
DOWN = (curses.KEY_DOWN, "j")


class App:
    def __init__(self, scr):
        self.scr = scr
        self.history = load_history()
        self.flash_msg, self.flash_until = "", 0.0
        curses.curs_set(0)
        scr.keypad(True)
        self.has_color = curses.has_colors()
        self.c256 = False
        self.pairs = {}
        if self.has_color:
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            self.c256 = curses.COLORS >= 256
        accent = 209 if self.c256 else curses.COLOR_YELLOW
        self.A_HEAD = self.color(accent) | curses.A_REVERSE | curses.A_BOLD
        self.A_ACC = self.color(accent) | curses.A_BOLD
        self.A_DIM = curses.A_DIM
        self.A_SEL = curses.A_REVERSE

    # colours -------------------------------------------------------
    def color(self, fg):
        if not self.has_color or fg == -1:
            return 0
        if fg not in self.pairs:
            n = len(self.pairs) + 1
            if n >= curses.COLOR_PAIRS:
                return 0
            try:
                curses.init_pair(n, fg, -1)
            except curses.error:
                return 0
            self.pairs[fg] = n
        return curses.color_pair(self.pairs[fg])

    def comment_attr(self, c):
        attr = 0
        if c.color in COLOR_NAMES:
            c256, basic = COLOR_NAMES[c.color]
            attr = self.color(c256 if self.c256 else basic)
            if c.color.startswith("black"):
                attr |= curses.A_DIM
        elif c.color:
            r, g, b = (int(c.color[i:i + 2], 16) for i in (1, 3, 5))
            if self.c256:
                lv = [0, 95, 135, 175, 215, 255]
                idx = [min(range(6), key=lambda i: abs(lv[i] - x)) for x in (r, g, b)]
                attr = self.color(16 + 36 * idx[0] + 6 * idx[1] + idx[2])
            else:
                basic = (1 if r > 127 else 0) | (2 if g > 127 else 0) | (4 if b > 127 else 0)
                attr = self.color(basic if basic not in (0, 7) else -1)
        if c.fork == "owner" or c.big:
            attr |= curses.A_BOLD
        return attr

    # drawing helpers -----------------------------------------------
    def put(self, y, x, s, attr=0, x0=0, x1=None):
        h, w = self.scr.getmaxyx()
        x1 = w if x1 is None else min(x1, w)
        if not 0 <= y < h:
            return
        col, start, buf = x, None, []
        for ch in s:
            c = cw(ch)
            if col >= x1 or (col + c > x1):
                break
            if col >= x0:
                if start is None:
                    start = col
                buf.append(ch)
            col += c
        if buf:
            try:
                self.scr.addstr(y, start, "".join(buf), attr)
            except curses.error:
                pass   # writing the bottom-right cell always "fails"

    def frame(self, title, help_text):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.put(0, 0, " " * w, self.A_HEAD)
        self.put(0, 1, clip(f"ニコニコ nicocli │ {title}", w - 2), self.A_HEAD)
        if time.monotonic() < self.flash_until:
            self.put(h - 1, 0, " " * w, self.A_ACC | curses.A_REVERSE)
            self.put(h - 1, 1, clip(self.flash_msg, w - 2), self.A_ACC | curses.A_REVERSE)
        else:
            self.put(h - 1, 1, clip(help_text, w - 2), self.A_DIM)

    def flash(self, msg, secs=3.0):
        self.flash_msg, self.flash_until = msg, time.monotonic() + secs

    def getkey(self, timeout=-1):
        self.scr.timeout(timeout)
        try:
            k = self.scr.get_wch()
        except curses.error:
            return None
        if k == curses.KEY_RESIZE:
            curses.update_lines_cols()
        return k

    def loading(self, msg):
        h, w = self.scr.getmaxyx()
        self.put(h - 1, 0, " " * w, self.A_ACC | curses.A_REVERSE)
        self.put(h - 1, 1, clip(f"⏳ {msg}", w - 2), self.A_ACC | curses.A_REVERSE)
        self.scr.refresh()

    def popup(self, lines, footer):
        h, w = self.scr.getmaxyx()
        bw = min(w - 2, max(swidth(footer), *(swidth(l) for l in lines)) + 4)
        body = [l for line in lines for l in wrap(line, bw - 4)]
        bh = min(h - 2, len(body) + 4)
        y0, x0 = (h - bh) // 2, (w - bw) // 2
        self.put(y0, x0, "┌" + "─" * (bw - 2) + "┐", self.A_ACC)
        for i in range(1, bh - 1):
            self.put(y0 + i, x0, "│" + " " * (bw - 2) + "│", self.A_ACC)
        self.put(y0 + bh - 1, x0, "└" + "─" * (bw - 2) + "┘", self.A_ACC)
        for i, line in enumerate(body[:bh - 4]):
            self.put(y0 + 1 + i, x0 + 2, line)
        self.put(y0 + bh - 2, x0 + 2, clip(footer, bw - 4), self.A_DIM)
        self.scr.refresh()

    def alert(self, msg):
        self.popup(msg.split("\n"), "press any key")
        while self.getkey() in (None, curses.KEY_RESIZE):
            pass

    def confirm(self, msg):
        self.popup([msg], "y = yes, anything else = no")
        return self.getkey() in ("y", "Y")

    def prompt(self, label, initial=""):
        buf = list(initial)
        curses.curs_set(1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                text = "".join(buf)
                room = w - swidth(label) - 2
                while swidth(text) > room and text:   # keep the cursor end visible
                    text = text[1:]
                self.put(h - 1, 0, " " * w, curses.A_REVERSE)
                self.put(h - 1, 0, label + text, curses.A_REVERSE)
                try:
                    self.scr.move(h - 1, min(w - 1, swidth(label + text)))
                except curses.error:
                    pass
                self.scr.refresh()
                k = self.getkey()
                if k in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf).strip()
                if k == "\x1b":
                    return None
                if k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if buf:
                        buf.pop()
                elif k == "\x15":          # ctrl-u
                    buf.clear()
                elif isinstance(k, str) and k.isprintable():
                    buf.append(k)
        finally:
            curses.curs_set(0)

    def nav(self, k, sel, n, page):
        if k in UP:
            sel -= 1
        elif k in DOWN:
            sel += 1
        elif k == curses.KEY_PPAGE:
            sel -= page
        elif k == curses.KEY_NPAGE:
            sel += page
        elif k in (curses.KEY_HOME, "g"):
            sel = 0
        elif k in (curses.KEY_END, "G"):
            sel = n - 1
        return max(0, min(sel, n - 1))

    # screens -------------------------------------------------------
    def menu(self, title, options, help_text="↑↓/jk move · Enter select · q back", sel=0):
        top = 0
        while True:
            h, w = self.scr.getmaxyx()
            self.frame(title, help_text)
            rows = max(1, h - 4)
            top = min(max(top, sel - rows + 1), sel)
            for i, label in enumerate(options[top:top + rows]):
                idx = top + i
                num = f"{idx + 1:>2}. " if idx < 9 else "    "
                line = clip(f" {num}{label}", w - 4)
                attr = self.A_SEL if idx == sel else 0
                self.put(2 + i, 2, line + " " * (w - 4 - swidth(line)) if idx == sel else line, attr)
            k = self.getkey()
            if k in ENTER:
                return sel
            if k in BACK:
                return None
            if isinstance(k, str) and k.isdigit() and 0 < int(k) <= min(9, len(options)):
                return int(k) - 1
            sel = self.nav(k, sel, len(options), rows)

    def main_menu(self):
        opts = ["Ranking", "Search by keyword", "Search by tag",
                "Open video by ID / URL", "Watch history", "Quit"]
        sel = 0
        while True:
            sel = self.menu("Main menu", opts, "↑↓/jk move · Enter/number select · q quit", sel)
            if sel is None or sel == 5:
                return
            if sel == 0:
                g = self.menu("Ranking · pick a genre", [lbl for _, lbl in GENRES])
                if g is not None:
                    self.list_screen(RankingSource(*GENRES[g]))
            elif sel in (1, 2):
                q = self.prompt("Tag: " if sel == 2 else "Search: ")
                if q:
                    self.list_screen(SearchSource(q, tag=sel == 2))
            elif sel == 3:
                q = self.prompt("Video ID or URL (e.g. sm9): ")
                if q:
                    vid = parse_video_id(q)
                    if vid:
                        self.video_screen(vid)
                    else:
                        self.flash(f"'{q}' doesn't look like a video id")
            elif sel == 4:
                self.list_screen(HistorySource(self))

    def load(self, src):
        self.loading("Loading…")
        try:
            items, more = src.fetch()
        except NicoError as e:
            self.alert(f"Couldn't load the list:\n{e}")
            return None
        src.has_more = more
        return items

    def list_screen(self, src):
        items = self.load(src)
        if items is None:
            return
        sel = top = 0
        extra = src.keys()
        while True:
            h, w = self.scr.getmaxyx()
            helps = ["Enter open"]
            if src.paged:
                helps.append("n/p page")
            helps += [f"{k} {label}" for k, (label, _) in extra.items()]
            helps.append("q back")
            page = f" · page {src.page + 1}" if src.paged else ""
            self.frame(src.title() + page, " · ".join(helps))
            rows = max(1, h - 5)
            wide = w >= 72
            hdr = f"{'#':>4}  {'ID':<11} {'LEN':>7}"
            if wide:
                hdr += f" {'VIEWS':>11} {'CMTS':>9}"
            self.put(1, 0, clip(hdr + "  TITLE", w), self.A_DIM | curses.A_UNDERLINE)
            if not items:
                self.put(3, 4, "(nothing here)", self.A_DIM)
            top = min(max(top, sel - rows + 1), sel)
            for i, v in enumerate(items[top:top + rows]):
                idx = top + i
                rank = src.page * PER_PAGE + idx + 1
                line = f"{rank:>4}  {v['id']:<11} {fmt_time(v['length']):>7}"
                if wide:
                    line += f" {v['views']:>11,} {v['comments']:>9,}"
                line += "  "
                rest = clip(v["title"], w - swidth(line) - 1)
                if idx == sel:
                    full = line + rest
                    self.put(2 + i, 0, full + " " * max(0, w - swidth(full)), self.A_SEL)
                else:
                    self.put(2 + i, 0, line, self.A_DIM)
                    self.put(2 + i, swidth(line), rest)
            if items:
                v = items[sel]
                info = f"▸ {fmt_date(v['date'])}  ▶ {v['views']:,}  💬 {v['comments']:,}" \
                       f"  ♥ {v['likes']:,}  ☆ {v['mylists']:,}"
                if v.get("owner"):
                    info += f"  by {v['owner']}"
                self.put(h - 2, 1, clip(info, w - 2), self.A_ACC)

            k = self.getkey()
            if k in BACK:
                return
            if k in ENTER and items:
                self.video_screen(items[sel]["id"])
                if isinstance(src, HistorySource):
                    items = self.load(src) or []
                    sel = min(sel, max(0, len(items) - 1))
            elif k in ("n", "p") and src.paged:
                if k == "n" and not src.has_more:
                    self.flash("That's the last page")
                    continue
                if k == "p" and src.page == 0:
                    self.flash("Already on the first page")
                    continue
                src.page += 1 if k == "n" else -1
                new = self.load(src)
                if new is None:
                    src.page -= 1 if k == "n" else -1
                else:
                    items, sel, top = new, 0, 0
            elif k in extra:
                if extra[k][1](self):
                    new = self.load(src)
                    if new is not None:
                        items, sel, top = new, 0, 0
            elif k is not None:
                sel = self.nav(k, sel, len(items), rows)

    def video_screen(self, vid):
        self.loading(f"Loading {vid}…")
        try:
            info = watch_info(vid)
        except NicoError as e:
            self.alert(f"Couldn't open {vid}:\n{e}")
            return
        v = info["video"]
        cnt = v.get("count") or {}
        owner, channel = info.get("owner"), info.get("channel")
        by = owner["nickname"] if owner else (channel or {}).get("name", "?")
        tags = [t["name"] for t in (info.get("tag") or {}).get("items", [])]
        entry = {"id": v["id"], "title": v["title"], "views": cnt.get("view", 0),
                 "comments": cnt.get("comment", 0), "mylists": cnt.get("mylist", 0),
                 "likes": cnt.get("like", 0), "length": v.get("duration", 0),
                 "date": v.get("registeredAt", ""), "owner": by}
        self.history[:] = [entry] + [x for x in self.history if x["id"] != v["id"]]
        save_history(self.history)
        desc = html_to_text(v.get("description"))
        url = WATCH_URL + v["id"]
        comments = None
        scroll = 0

        def get_comments():
            nonlocal comments
            if comments is None:
                self.loading("Fetching comments…")
                try:
                    comments = fetch_comments(info)
                except NicoError as e:
                    self.alert(f"Couldn't fetch comments:\n{e}")
                    return None
            return comments

        while True:
            h, w = self.scr.getmaxyx()
            tw = max(10, w - 4)
            lines = [(l, curses.A_BOLD) for l in wrap(clean(v["title"]), tw)]
            lines.append(("", 0))
            meta = [
                ("ID", f"{v['id']}   {url}"),
                ("Posted", fmt_date(v.get("registeredAt"))),
                ("Length", fmt_time(v.get("duration", 0))),
                ("By", by + (f"  (user {owner['id']})" if owner else "  (channel)")),
                ("Genre", (info.get("genre") or {}).get("label") or "—"),
                ("Stats", f"▶ {cnt.get('view', 0):,} views   💬 {cnt.get('comment', 0):,}"
                          f"   ♥ {cnt.get('like', 0):,}   ☆ {cnt.get('mylist', 0):,}"),
            ]
            if comments is not None:
                meta.append(("Loaded", f"{len(comments):,} recent comments"))
            for key, val in meta:
                for j, l in enumerate(wrap(val, tw - 10)):
                    lines.append(((f"{key:<9} " if j == 0 else " " * 10) + l, 0))
            for j, l in enumerate(wrap("  ·  ".join(tags) or "—", tw - 10)):
                lines.append(((f"{'Tags':<9} " if j == 0 else " " * 10) + l, 0))
            lines.append(("", 0))
            lines.append(("── Description " + "─" * max(0, tw - 15), self.A_DIM))
            lines += [(l, 0) for l in wrap(desc or "(no description)", tw)]

            self.frame(v["id"], "Enter/d danmaku · c comments · p play (mpv) · T play in terminal"
                                " · t tags · u uploader · o browser · q back")
            rows = h - 3
            scroll = max(0, min(scroll, len(lines) - rows))
            for i, (text, attr) in enumerate(lines[scroll:scroll + rows]):
                self.put(2 + i, 2, text, attr)
            if len(lines) > rows:
                self.put(1, w - 12, f"{scroll + 1}-{min(len(lines), scroll + rows)}/{len(lines)}",
                         self.A_DIM)

            k = self.getkey()
            if k in ("q", "\x1b", "h", curses.KEY_LEFT, curses.KEY_BACKSPACE, "\x7f"):
                return
            elif k in UP:
                scroll -= 1
            elif k in DOWN:
                scroll += 1
            elif k == curses.KEY_PPAGE:
                scroll -= rows
            elif k in (curses.KEY_NPAGE, " "):
                scroll += rows
            elif k in ("\n", "\r", curses.KEY_ENTER, "d"):
                if get_comments() is not None:
                    self.player(info, comments)
            elif k == "c":
                if get_comments() is not None:
                    jump = self.comment_list(info, comments)
                    if jump is not None:
                        self.player(info, comments, start=jump)
            elif k == "p":
                if not shutil.which("mpv"):
                    self.alert("mpv isn't installed.")
                elif not have_streamer():
                    self.alert("mpv needs yt-dlp to stream niconico videos.\n"
                               "Install it with:  sudo pacman -S yt-dlp")
                elif get_comments() is not None:
                    self.player(info, comments, use_mpv=True)
            elif k == "T":
                self.play_in_terminal(url)
            elif k == "o":
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                self.flash(f"Opened {url} in your browser")
            elif k == "t":
                if not tags:
                    self.flash("This video has no tags")
                    continue
                i = self.menu(f"Tags on {v['id']} · pick one to search", tags)
                if i is not None:
                    self.list_screen(SearchSource(tags[i], tag=True))
            elif k == "u":
                if owner:
                    self.list_screen(UserSource(owner["id"], by))
                else:
                    self.flash("Listing channel uploads isn't supported — try 'o' for the browser")

    def play_in_terminal(self, url):
        if not shutil.which("mpv") or not have_streamer():
            self.alert("Terminal playback needs mpv and yt-dlp.\n"
                       "Install with:  sudo pacman -S mpv yt-dlp")
            return
        curses.endwin()
        print(f"Playing {url} as text art (mpv --vo=tct). Press q in mpv to come back.\n")
        r = subprocess.run(["mpv", "--vo=tct", "--really-quiet", url])
        if r.returncode not in (0, 4):
            input("\nmpv exited with an error. Press Enter to return…")
        self.scr.refresh()

    def comment_list(self, info, comments):
        """Scrollable list of comments. Returns a time to jump to, or None."""
        by_posted = False
        query = ""
        sel = top = 0
        while True:
            shown = [c for c in comments if query.lower() in c.text.lower()]
            if by_posted:
                shown.sort(key=lambda c: c.posted, reverse=True)
            h, w = self.scr.getmaxyx()
            order = "newest first" if by_posted else "by video time"
            filt = f" · filter “{query}”" if query else ""
            self.frame(f"Comments · {info['video']['id']} · {len(shown):,} · {order}{filt}",
                       "Enter jump to this point in danmaku · s sort · / filter · q back")
            rows = max(1, h - 3)
            sel = max(0, min(sel, len(shown) - 1))
            top = min(max(top, sel - rows + 1), sel)
            for i, c in enumerate(shown[top:top + rows]):
                idx = top + i
                mark = {"owner": "★", "easy": "·"}.get(c.fork, " ")
                stamp = f"{fmt_time(c.vpos):>7} {mark} "
                if by_posted:
                    stamp = f"{c.posted[5:16].replace('T', ' ')} {stamp}"
                body = clip(c.text, w - swidth(stamp) - 2)
                if idx == sel:
                    full = stamp + body
                    self.put(1 + i, 1, full + " " * max(0, w - 2 - swidth(full)), self.A_SEL)
                else:
                    self.put(1 + i, 1, stamp, self.A_DIM)
                    self.put(1 + i, 1 + swidth(stamp), body, self.comment_attr(c))
            if not shown:
                self.put(2, 4, "(no comments)", self.A_DIM)
            k = self.getkey()
            if k in BACK:
                return None
            if k in ENTER and shown:
                return max(0.0, shown[sel].vpos - 1.5)
            if k == "s":
                by_posted, sel, top = not by_posted, 0, 0
            elif k == "/":
                q = self.prompt("Filter comments: ", query)
                if q is not None:
                    query, sel, top = q, 0, 0
            elif k is not None:
                sel = self.nav(k, sel, len(shown), rows)

    def player(self, info, comments, start=0.0, use_mpv=False):
        v = info["video"]
        duration = v.get("duration") or (max((c.vpos for c in comments), default=0) + 5)
        show_easy = True
        dm = Danmaku(comments)
        pos, playing, rate = max(0.0, start), True, 1.0
        last = time.monotonic()
        mpv = None
        if use_mpv:
            mpv = Mpv(WATCH_URL + v["id"], f"{v['id']} — {v['title']}")
            playing = False
        pending_seek = start if use_mpv and start else None
        mpv_ready = False

        try:
            while True:
                now = time.monotonic()
                if mpv:
                    if not mpv.alive():
                        mpv = None
                        playing = False
                        self.flash("mpv closed — danmaku is on its own clock now (space to play)")
                    else:
                        p = mpv.get("time-pos")
                        if p is not None:
                            if not mpv_ready:
                                mpv_ready = True
                                if pending_seek:
                                    mpv.command("seek", pending_seek, "absolute")
                                    p = pending_seek
                            pos = p
                            playing = mpv.get("pause") is False
                elif playing:
                    pos += (now - last) * rate
                    if pos >= duration:
                        pos, playing = duration, False
                last = now

                h, w = self.scr.getmaxyx()
                area_top, area_h = 2, max(1, h - 5)
                dm.advance(pos, w, area_h)
                self.scr.erase()
                self.put(0, 0, " " * w, self.A_HEAD)
                self.put(0, 1, clip(f"{v['id']} │ {clean(v['title'])}", w - 2), self.A_HEAD)
                self.put(1, 0, "─" * w, self.A_DIM)
                self.put(area_top + area_h, 0, "─" * w, self.A_DIM)
                for c, row, col in dm.placements(pos):
                    attr = self.comment_attr(c)
                    for i, line in enumerate(c.lines[:area_h - row]):
                        self.put(area_top + row + i, col, line, attr, 0, w)
                if not comments:
                    self.put(area_top + area_h // 2, max(0, w // 2 - 8), "(no comments)", self.A_DIM)
                if mpv and not mpv_ready:
                    msg = "mpv is loading the video…"
                    self.put(area_top + area_h // 2, max(0, (w - swidth(msg)) // 2), msg, self.A_ACC)

                # status / progress bar
                icon = "▶" if playing else "‖"
                left = f" {icon} {fmt_time(pos)} / {fmt_time(duration)} "
                right = f" x{rate:.2g}  💬{len(dm.active):>3}{'' if show_easy else '  -easy'}" \
                        f"{'  [mpv]' if mpv else ''} "
                bar_w = max(0, w - swidth(left) - swidth(right))
                filled = int(bar_w * min(1.0, pos / duration)) if duration else 0
                self.put(h - 2, 0, left, self.A_ACC)
                self.put(h - 2, swidth(left), "━" * filled, self.A_ACC)
                self.put(h - 2, swidth(left) + filled, "─" * (bar_w - filled), self.A_DIM)
                self.put(h - 2, swidth(left) + bar_w, right, self.A_DIM)
                if time.monotonic() < self.flash_until:
                    self.put(h - 1, 1, clip(self.flash_msg, w - 2), self.A_ACC)
                else:
                    self.put(h - 1, 1, clip("space pause · ←→ 5s · ↑↓ 30s · 0-9 jump · [ ] speed"
                                            " · e easy comments · q back", w - 2), self.A_DIM)
                self.scr.refresh()

                k = self.getkey(33)
                if k is None or k == curses.KEY_RESIZE:
                    continue
                if k in ("q", "\x1b"):
                    return
                target = None
                if k == " ":
                    if mpv:
                        mpv.command("cycle", "pause")
                    else:
                        if pos >= duration:
                            pos = 0.0
                        playing = not playing
                elif k in (curses.KEY_LEFT, "h"):
                    target = pos - 5
                elif k in (curses.KEY_RIGHT, "l"):
                    target = pos + 5
                elif k in (curses.KEY_DOWN, "j"):
                    target = pos - 30
                elif k in (curses.KEY_UP, "k"):
                    target = pos + 30
                elif isinstance(k, str) and k.isdigit():
                    target = duration * int(k) / 10
                elif k in ("[", "]"):
                    rate = max(0.25, min(4.0, rate * (0.5 if k == "[" else 2)))
                    if mpv:
                        mpv.command("set_property", "speed", rate)
                elif k == "e":
                    show_easy = not show_easy
                    dm = Danmaku([c for c in comments if show_easy or c.fork != "easy"])
                    self.flash("Easy comments " + ("shown" if show_easy else "hidden"))
                if target is not None:
                    target = max(0.0, min(duration, target))
                    if mpv:
                        if mpv_ready:
                            mpv.command("seek", target, "absolute")
                        else:
                            pending_seek = target
                    pos = target
        finally:
            if mpv:
                mpv.close()

    def run(self, argv):
        if argv:
            vid = parse_video_id(argv[0])
            if vid:
                self.video_screen(vid)
            else:
                self.list_screen(SearchSource(" ".join(argv), tag=False))
        self.main_menu()


def main():
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        print("\nExamples:\n  nicocli.py            start at the main menu"
              "\n  nicocli.py sm9        open a video directly"
              "\n  nicocli.py 初音ミク    search for a keyword")
        return
    try:
        curses.wrapper(lambda scr: App(scr).run(sys.argv[1:]))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
