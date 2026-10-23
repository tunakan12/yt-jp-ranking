"""テスト用の疑似 YouTube Data API。時刻に応じて再生数が増える架空の動画・ライブ配信を返す。"""
import hashlib
import math
import os
from datetime import datetime, timezone

BASE = 1_790_000_000  # 架空世界の起点
WORDS = ["【検証】", "ゲーム実況", "新曲MV", "料理してみた", "旅行Vlog", "ニュース解説", "車レビュー",
         "猫の一日", "やってみた", "ドッキリ", "歌ってみた", "まとめ", "雑談"]
CATS = ["20", "10", "22", "24", "23", "25", "2", "15", "26", "27", "28", "17"]


def h(*a):
    return int(hashlib.md5("|".join(map(str, a)).encode()).hexdigest()[:8], 16)


N_CH = 400
CHANNELS = [f"UC{'%022d' % i}" for i in range(N_CH)]


def is_streamer(c):
    return c % 7 == 0


def is_premiere(c, k):
    return c % 11 == 0 and k % 3 == 0


def video_list():
    vids = []
    for c in range(N_CH):
        for k in range(40):
            pub = BASE + (h(c, k) % (40 * 86400)) - 30 * 86400
            vids.append((f"v{c:03d}x{k:02d}xxx"[:11], c, k, pub))
    return vids


VIDEOS = video_list()
BY_ID = {v[0]: v for v in VIDEOS}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def duration(c, k):
    if is_streamer(c):
        return 3600 + h("s", c, k) % (5 * 3600)    # 配信1〜6時間
    if is_premiere(c, k):
        return 200 + h("s", c, k) % 200            # MVのプレミア公開
    r = h("d", c, k) % 100
    if r < 25:
        return 15 + h("s", c, k) % 45        # ショート
    if r < 32:
        return 61 + h("s", c, k) % 119       # 61-179秒（一部ショート）
    return 240 + h("s", c, k) % 3000


def live_state(vid, now):
    _, c, k, pub = BY_ID[vid]
    if is_streamer(c) or is_premiere(c, k):
        if now < pub:
            return "upcoming"
        if now < pub + duration(c, k):
            return "live"
    return "none"


def is_shorts(vid):
    v = BY_ID[vid]
    d = duration(v[1], v[2])
    return d < 60 or (d <= 180 and h("sh", vid) % 2 == 0)


def views(vid, now):
    _, c, k, pub = BY_ID[vid]
    age = max(0, now - pub) / 3600
    pop = (h("p", c) % 1000) / 1000
    mag = 10 ** (2 + pop * 4 + (h("m", c, k) % 100) / 60)  # 100〜数千万
    tau = 10 + h("t", c, k) % 60
    delay = h("dl", c, k) % 120  # 遅れてバズる動画
    base = mag * (1 - math.exp(-age / tau))
    late = 0
    if h("bz", c, k) % 15 == 0 and age > delay:
        late = mag * 2 * (1 - math.exp(-(age - delay) / 8))
    return int(base + late)


def lang(c):
    return "en" if c % 13 == 0 else "ja"


def snippet(vid, now):
    _, c, k, pub = BY_ID[vid]
    title = f"{WORDS[h('w', c, k) % len(WORDS)]} 動画その{k} #{c}" if lang(c) == "ja" else f"English video {k} #{c}"
    if is_streamer(c):
        title = f"【配信】{title}"
    return {"publishedAt": iso(pub - (3600 if is_streamer(c) else 0)), "channelId": CHANNELS[c], "title": title,
            "channelTitle": f"チャンネル{c}" if lang(c) == "ja" else f"Channel {c}",
            "categoryId": CATS[h("c", c) % len(CATS)], "liveBroadcastContent": live_state(vid, now),
            "defaultAudioLanguage": lang(c)}


def vitem(vid, now):
    _, c, k, pub = BY_ID[vid]
    vw = views(vid, now)
    stt = live_state(vid, now)
    d = duration(c, k)
    shown = 0 if (is_streamer(c) and stt != "none") else d
    it = {"id": vid, "snippet": snippet(vid, now),
          "contentDetails": {"duration": f"PT{shown // 60}M{shown % 60}S"},
          "statistics": {"viewCount": str(vw), "likeCount": str(vw // 40), "commentCount": str(vw // 900)}}
    if is_streamer(c) or is_premiere(c, k):
        lsd = {"scheduledStartTime": iso(pub)}
        if stt != "upcoming":
            lsd["actualStartTime"] = iso(pub)
        if stt == "live":
            lsd["concurrentViewers"] = str(max(1, vw // 20))
        if stt == "none":
            lsd["actualEndTime"] = iso(pub + d)
        it["liveStreamingDetails"] = lsd
    return it


def cat_of(v):
    return CATS[h("c", v[1]) % len(CATS)]


class MockHttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def handle(endpoint, p, now):
    if endpoint == "videos:batchGetStats" and os.environ.get("MOCK_BATCH_FAIL") == "1":
        raise MockHttpError(403, "batchGetStats forbidden (mock)")
    # 通常動画は公開後、配信/プレミアの予定枠は1日前から見える
    visible = [v for v in VIDEOS if v[3] <= now or (v[3] - 86400 <= now and live_state(v[0], now) == "upcoming")]
    if endpoint == "videos":
        if p.get("chart") == "mostPopular":
            recent = [v for v in visible if 0 <= now - v[3] < 7 * 86400 and cat_of(v) in ("10", "20")]
            recent.sort(key=lambda v: -views(v[0], now))
            start = int(p.get("pageToken") or 0)
            r = {"items": [{"id": v[0]} for v in recent[start:start + 50]]}
            if start + 50 < min(200, len(recent)):
                r["nextPageToken"] = str(start + 50)
            return r
        vis = {v[0] for v in visible}
        return {"items": [vitem(i, now) for i in p["id"].split(",") if i in vis]}
    if endpoint == "videos:batchGetStats":
        ids = [i for i in p["id"].split(",") if i in BY_ID and BY_ID[i][3] <= now]
        return {"items": [{"id": i, "statistics": vitem(i, now)["statistics"]} for i in ids]}
    if endpoint == "search":
        if p.get("eventType") == "live":
            rs = [v for v in visible if live_state(v[0], now) == "live"]
        else:
            after = int(datetime.fromisoformat(p["publishedAfter"].replace("Z", "+00:00")).timestamp())
            rs = [v for v in visible if after <= v[3] <= now]
            if p.get("videoCategoryId"):
                rs = [v for v in rs if cat_of(v) == p["videoCategoryId"]]
        rs.sort(key=lambda v: -views(v[0], now))
        return {"items": [{"id": {"videoId": v[0]}} for v in rs[:50]]}
    if endpoint == "channels":
        out = []
        for cid in p["id"].split(","):
            c = int(cid[2:])
            out.append({"id": cid, "snippet": {"title": f"チャンネル{c}", "country": "" if c % 3 else "JP"},
                        "statistics": {"subscriberCount": str(h("sub", c) % 2_000_000)}})
        return {"items": out}
    if endpoint == "playlistItems":
        c = int(p["playlistId"][2:])
        mine = sorted([v for v in visible if v[1] == c], key=lambda v: -v[3])[: int(p.get("maxResults", 5))]
        return {"items": [{"contentDetails": {"videoId": v[0], "videoPublishedAt": iso(v[3])}} for v in mine]}
    raise ValueError(endpoint)
