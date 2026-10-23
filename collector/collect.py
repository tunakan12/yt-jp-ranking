#!/usr/bin/env python3
"""
日本のYouTube ランキング コレクター（動画 / ライブ配信を分けて集計）

- YouTube Data API v3 の無料枠だけで動くように設計（2026年6月以降のクォータ方式に対応）
    * 通常バケット      : 10,000ユニット/日（videos.list, channels.list, playlistItems.list など）
    * search.list       : 100回/日（専用バケット）
    * videos.batchGetStats : 10,000ユニット/日（専用バケット。再生数の定期取得に使う）
- ショート動画は除外
- 2時間おきに再生数のスナップショットを取り、伸びからランキングを計算
- 標準ライブラリのみ（pip install 不要）

環境変数:
  YT_API_KEY   : YouTube Data API キー（必須。YT_MOCK=1 のときは不要）
  YT_MOCK=1    : 疑似API（tools/mock_api.py）で動かす（テスト用）
  YT_NOW       : 現在時刻をUNIX秒で上書き（テスト用）
"""
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "state", "state.json.gz")
OUT_DIR = os.path.join(ROOT, "site", "data")

# ---------------------------------------------------------------- 設定
CFG = {
    "region": "JP",
    "search_window_h": 48,            # 検索で拾う投稿の新しさ
    "most_popular_pages": 4,          # 公式 mostPopular(JP) 最大200件
    "channel_poll_per_run": 400,      # 1回の実行で新着チェックするチャンネル数（1件1ユニット）
    "channel_new_window_h": 72,       # チャンネル新着として追加する投稿の新しさ
    "keep_snaps_h": 24 * 8,           # スナップショット保持期間
    "keep_top_weekly": 3000,          # 3日より古い動画は、週間の伸び上位この件数まで追跡継続
    "max_tracked": 8000,              # 追跡する動画の上限
    "min_duration_s": 61,             # これ以下はショート扱いで除外
    "shorts_check_max_s": 180,        # 61〜180秒は /shorts/ URL で判定
    "live_min_duration_s": 600,       # 配信中に一度も見ていない動画は「ライブ痕跡あり＆10分以上」をライブ扱い
    "trend_max_age_h": 72,            # 急上昇の対象は投稿72時間以内
    "trend_window_h": 4,              # 急上昇の速度を測る直近時間
    "list_size": 100,
    "live_now_size": 50,
    # 1回あたりの予算（1日12回実行）
    "main_budget_per_run": 750,       # 通常バケット: 12 × 750 = 9,000 / 10,000
    "search_calls_per_run": 6,        # search.list: 12 × 6 = 72 / 100（手動実行の余裕を残す）
    "stats_budget_per_run": 800,      # batchGetStats: 12 × 800 = 9,600 / 10,000
}

# 検索で巡回するカテゴリ（1回の実行で3つずつ）
SEARCH_ROTATION = [
    ("10", "any"), ("20", "long"), ("24", "medium"), ("22", "medium"), ("23", "medium"),
    ("17", "medium"), ("25", "medium"), ("26", "medium"), ("20", "medium"), ("27", "medium"),
    ("28", "medium"), ("2", "medium"), ("15", "medium"), ("1", "medium"), ("24", "long"), ("19", "medium"),
]

BUCKET = {"videos": "main", "channels": "main", "playlistItems": "main",
          "search": "search", "videos:batchGetStats": "stats"}
BUDGET = {"main": CFG["main_budget_per_run"], "search": CFG["search_calls_per_run"],
          "stats": CFG["stats_budget_per_run"]}

CATEGORY_JA = {
    "1": "映画とアニメ", "2": "自動車と乗り物", "10": "音楽", "15": "ペットと動物",
    "17": "スポーツ", "19": "旅行とイベント", "20": "ゲーム", "22": "ブログ",
    "23": "コメディー", "24": "エンターテイメント", "25": "ニュースと政治",
    "26": "ハウツーとスタイル", "27": "教育", "28": "科学と技術", "29": "非営利団体と社会活動",
}

KANA_RE = re.compile(r"[\u3040-\u30ff]")
NOW = int(os.environ.get("YT_NOW") or time.time())
MOCK = os.environ.get("YT_MOCK") == "1"
KEY = os.environ.get("YT_API_KEY", "")
used = {"main": 0, "search": 0, "stats": 0}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- API
class QuotaBudgetExceeded(Exception):
    pass


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def api(endpoint, **params):
    bucket = BUCKET[endpoint]
    if used[bucket] + 1 > BUDGET[bucket]:
        raise QuotaBudgetExceeded(f"{bucket} budget for this run")
    used[bucket] += 1
    if MOCK:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import mock_api  # noqa
        try:
            return mock_api.handle(endpoint, params, NOW)
        except mock_api.MockHttpError as e:
            raise ApiError(e.code, str(e))
    params["key"] = KEY
    url = "https://www.googleapis.com/youtube/v3/" + endpoint + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code == 403 and "quota" in body.lower():
                BUDGET[bucket] = 0  # この実行ではもう使わない
                raise QuotaBudgetExceeded(f"daily quota exhausted ({bucket})")
            if e.code >= 500 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise ApiError(e.code, f"{endpoint} HTTP {e.code}: {body}")
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def iso_to_ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def ts_to_iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


DUR_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration(s):
    m = DUR_RE.fullmatch(s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


# ---------------------------------------------------------------- ショート判定（クォータ消費なし）
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def is_shorts_url(video_id):
    """/shorts/ID が 200 ならショート、303等のリダイレクトなら通常動画。判定不能なら None"""
    if MOCK:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import mock_api  # noqa
        return mock_api.is_shorts(video_id)
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        f"https://www.youtube.com/shorts/{video_id}", method="HEAD",
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"})
    try:
        with opener.open(req, timeout=10) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return False
        return None
    except Exception:
        return None


# ---------------------------------------------------------------- 状態
def load_state():
    if os.path.exists(STATE_PATH):
        with gzip.open(STATE_PATH, "rt", encoding="utf-8") as f:
            st = json.load(f)
    else:
        st = {}
    st.setdefault("videos", {})      # id -> info + snaps
    st.setdefault("channels", {})    # id -> {t, country, subs, heat}
    st.setdefault("rejected", {})    # id -> ts（再取得しない）
    st.setdefault("live_seen", {})   # 配信中/配信予定で見かけたID -> {t, real}
    st.setdefault("prev_ranks", {})
    st.setdefault("channel_cursor", 0)
    st.setdefault("search_cursor", 0)
    st.setdefault("batch_stats_off_until", 0)
    st.pop("batch_stats_ok", None)
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- 収集
def search_ids(**p):
    r = api("search", part="id", type="video", regionCode=CFG["region"], relevanceLanguage="ja",
            maxResults=50, **p)
    return {i["id"]["videoId"] for i in r.get("items", []) if i.get("id", {}).get("videoId")}


def discover(st):
    """候補動画IDを集める"""
    cands = set()
    # 1) 公式 mostPopular（日本）: 音楽/映画/ゲーム中心だが安い
    token = None
    for _ in range(CFG["most_popular_pages"]):
        p = dict(part="id", chart="mostPopular", regionCode=CFG["region"], maxResults=50)
        if token:
            p["pageToken"] = token
        r = api("videos", **p)
        cands.update(i["id"] for i in r.get("items", []))
        token = r.get("nextPageToken")
        if not token:
            break
    log(f"mostPopular: {len(cands)}")

    # 2) 検索（search.list 専用バケット: 1日100回）
    n0 = len(cands)
    after = ts_to_iso(NOW - CFG["search_window_h"] * 3600)
    try:
        cands |= search_ids(eventType="live", order="viewCount")          # 配信中
        for dur in ("medium", "long"):                                      # 全体の人気
            cands |= search_ids(order="viewCount", videoDuration=dur, publishedAfter=after)
        k = st["search_cursor"]
        for i in range(max(0, CFG["search_calls_per_run"] - used["search"])):
            cat, dur = SEARCH_ROTATION[(k + i) % len(SEARCH_ROTATION)]
            cands |= search_ids(order="viewCount", videoDuration=dur, publishedAfter=after,
                                videoCategoryId=cat)
            st["search_cursor"] = (k + i + 1) % len(SEARCH_ROTATION)
    except QuotaBudgetExceeded as e:
        log("search stopped:", e)
    except ApiError as e:
        log("search error:", e)
    log(f"search: +{len(cands) - n0} ({used['search']} calls)")

    # 3) 前回まで配信中/配信予定だったもの（終わったらアーカイブとして登録するため）
    cands.update(k for k, v in st["live_seen"].items() if NOW - v["t"] < 3 * 86400)

    # 4) 追跡中チャンネルの新着: 勢いのあるチャンネル優先＋残りを巡回
    chans = st["channels"]
    ranked = sorted(chans, key=lambda c: -chans[c].get("heat", 0))
    # 新着詳細・統計の取り直し（batchGetStatsが使えない場合）・タイトル更新の分を残しておく
    reserve = 40 + math.ceil(len(st["videos"]) / 50) * (2 if st.get("batch_stats_off_until", 0) > NOW else 1)
    limit = max(0, min(CFG["channel_poll_per_run"], BUDGET["main"] - used["main"] - reserve))
    hot, rest = ranked[: limit // 2], ranked[limit // 2:]
    cur = st["channel_cursor"] % len(rest) if rest else 0
    rot = (rest[cur:] + rest[:cur])[: limit - len(hot)]
    st["channel_cursor"] = cur + len(rot)
    n0 = len(cands)
    cutoff = NOW - CFG["channel_new_window_h"] * 3600
    try:
        for cid in hot + rot:
            try:
                r = api("playlistItems", part="contentDetails", playlistId="UU" + cid[2:], maxResults=10)
            except ApiError as e:  # チャンネル削除など
                log("skip channel", cid, str(e)[:80])
                continue
            for it in r.get("items", []):
                cd = it.get("contentDetails", {})
                pub = cd.get("videoPublishedAt")
                if pub and iso_to_ts(pub) >= cutoff:
                    cands.add(cd["videoId"])
    except QuotaBudgetExceeded as e:
        log("channel polling stopped:", e)
    log(f"channels polled: +{len(cands) - n0}")
    return cands


def ensure_channels(st, channel_ids):
    need = set(channel_ids) - set(st["channels"])
    for ch in chunks(need, 50):
        r = api("channels", part="snippet,statistics", id=",".join(ch), maxResults=50)
        for it in r.get("items", []):
            st["channels"][it["id"]] = {
                "t": it["snippet"]["title"],
                "country": it["snippet"].get("country", ""),
                "subs": int(it.get("statistics", {}).get("subscriberCount", 0) or 0),
                "heat": 0,
            }


def is_japanese(sn, ch):
    lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower()
    kana = bool(KANA_RE.search(sn.get("title", "")))
    if lang and not lang.startswith("ja"):
        return kana  # 音声言語が日本語以外でも、タイトルにかなが入っていれば日本向けとみなす
    return (lang.startswith("ja") or kana or ch.get("country") == "JP"
            or bool(KANA_RE.search(sn.get("channelTitle", ""))))


def admit_new(st, cands):
    """新規候補の詳細を取得。日本の通常動画/ライブアーカイブを追跡対象に加え、配信中のものは集めて返す"""
    new = [v for v in cands if v not in st["videos"] and v not in st["rejected"]]
    info = {}
    for ch in chunks(new, 50):
        r = api("videos", part="snippet,contentDetails,statistics,liveStreamingDetails",
                id=",".join(ch), maxResults=50)
        for it in r.get("items", []):
            info[it["id"]] = it
    for vid in new:  # 削除・非公開になった配信
        if vid not in info:
            st["live_seen"].pop(vid, None)
    ensure_channels(st, {it["snippet"]["channelId"] for it in info.values()})

    live_now, added = [], 0
    for vid, it in info.items():
        sn, cd = it["snippet"], it["contentDetails"]
        lsd = it.get("liveStreamingDetails")
        ch = st["channels"].get(sn["channelId"], {})
        dur = parse_duration(cd.get("duration"))
        state = sn.get("liveBroadcastContent")
        if state in ("live", "upcoming"):
            sched = (lsd or {}).get("scheduledStartTime")
            if state == "upcoming" and (not sched or iso_to_ts(sched) - NOW > 48 * 3600):
                continue  # 先の予定枠（フリーチャット等）は追わない
            # 本物のライブは配信中/予定の時点で長さが0。プレミア公開は動画の長さが入っている
            seen = st["live_seen"].setdefault(vid, {"t": NOW, "real": dur == 0})
            seen["t"] = NOW
            if state == "live" and dur == 0 and is_japanese(sn, ch) and lsd:
                live_now.append({
                    "id": vid, "title": sn["title"], "ch": sn["channelTitle"], "cid": sn["channelId"],
                    "cat": CATEGORY_JA.get(sn.get("categoryId", ""), "その他"),
                    "viewers": int(lsd.get("concurrentViewers", 0) or 0),
                    "start": iso_to_ts(lsd["actualStartTime"]) if lsd.get("actualStartTime") else None,
                })
            continue
        if not is_japanese(sn, ch):
            st["rejected"][vid] = NOW
            st["live_seen"].pop(vid, None)
            continue
        seen = st["live_seen"].pop(vid, None)
        if seen is not None:
            kind = "live" if seen["real"] else "video"
        else:
            kind = "live" if (lsd and dur >= CFG["live_min_duration_s"]) else "video"
        if kind == "video":
            short = dur < CFG["min_duration_s"]
            if not short and dur <= CFG["shorts_check_max_s"]:
                s = is_shorts_url(vid)
                short = s is True or (s is None and dur <= 60)
            if short:
                st["rejected"][vid] = NOW
                continue
        st["videos"][vid] = {
            "t": sn["title"], "c": sn["channelId"], "ct": sn["channelTitle"],
            "p": iso_to_ts((lsd or {}).get("actualStartTime") or sn["publishedAt"]),
            "d": dur, "cat": sn.get("categoryId", ""), "k": kind, "snaps": [],
        }
        added += 1
    log(f"new candidates {len(new)} -> admitted {added}, live now {len(live_now)}")
    return live_now


def migrate(st):
    """旧バージョンの追跡データに 動画/ライブ の区別を付ける（1回だけ）"""
    old = [k for k, v in st["videos"].items() if "k" not in v]
    for ch in chunks(old, 50):
        r = api("videos", part="contentDetails,liveStreamingDetails", id=",".join(ch), maxResults=50)
        for it in r.get("items", []):
            v = st["videos"][it["id"]]
            lsd = it.get("liveStreamingDetails")
            v["k"] = "live" if (lsd and v["d"] >= CFG["live_min_duration_s"]) else "video"
            if lsd and lsd.get("actualStartTime"):
                v["p"] = iso_to_ts(lsd["actualStartTime"])
    for k in old:
        st["videos"][k].setdefault("k", "video")
    if old:
        log(f"migrated {len(old)} videos")


def add_snap(v, s):
    v["snaps"].append([NOW, int(s.get("viewCount", 0) or 0),
                       int(s.get("likeCount", 0) or 0), int(s.get("commentCount", 0) or 0)])


def refresh_stats(st):
    """全追跡動画の再生数を記録。batchGetStats（専用バケット）を優先し、使えなければ videos.list"""
    ids = list(st["videos"])
    got = set()
    if st.get("batch_stats_off_until", 0) <= NOW:
        try:
            for ch in chunks(ids, 50):
                r = api("videos:batchGetStats", part="statistics", id=",".join(ch))
                for it in r.get("items", []):
                    if it.get("id") in st["videos"] and it.get("statistics"):
                        add_snap(st["videos"][it["id"]], it["statistics"])
                        got.add(it["id"])
        except ApiError as e:
            log("batchGetStats unavailable, falling back to videos.list:", e)
            if 400 <= e.code < 500:
                st["batch_stats_off_until"] = NOW + 86400  # 1日後に再挑戦
        except QuotaBudgetExceeded as e:
            log("batchGetStats budget:", e)
    rest = [k for k in ids if k not in got]
    alive = set(got)
    try:
        for ch in chunks(rest, 50):
            r = api("videos", part="statistics", id=",".join(ch), maxResults=50)
            for it in r.get("items", []):
                add_snap(st["videos"][it["id"]], it.get("statistics", {}))
                alive.add(it["id"])
        for vid in set(rest) - alive:  # 削除・非公開
            del st["videos"][vid]
    except QuotaBudgetExceeded as e:
        log("stats refresh incomplete:", e)
    log(f"stats refreshed: {len(alive)} (batch {len(got)})")


def refresh_titles(st, ids):
    """表示する動画だけタイトル変更に追従（数ユニット）"""
    try:
        for ch in chunks(ids, 50):
            r = api("videos", part="snippet", id=",".join(ch), maxResults=50)
            for it in r.get("items", []):
                if it["id"] in st["videos"]:
                    st["videos"][it["id"]]["t"] = it["snippet"]["title"]
    except (QuotaBudgetExceeded, ApiError) as e:
        log("title refresh skipped:", e)


# ---------------------------------------------------------------- 計算
def views_at(v, t):
    """時刻tの推定再生数。(値, 推定かどうか)"""
    snaps = v["snaps"]
    if t <= v["p"]:
        return 0, False
    if t >= snaps[-1][0]:
        return snaps[-1][1], False
    if t < snaps[0][0]:
        # 追跡開始前: 公開時刻(0回)と最初のスナップショットを直線補間
        t0, v0 = snaps[0][0], snaps[0][1]
        frac = (t - v["p"]) / max(1, t0 - v["p"])
        return int(v0 * frac), True
    for (ta, va, *_), (tb, vb, *_) in zip(snaps, snaps[1:]):
        if ta <= t <= tb:
            return int(va + (vb - va) * (t - ta) / max(1, tb - ta)), False
    return snaps[-1][1], False


def gain(v, hours):
    now_v = v["snaps"][-1][1]
    past, est = views_at(v, NOW - hours * 3600)
    return max(0, now_v - (past or 0)), est


def item(v, vid, **extra):
    last = v["snaps"][-1]
    d = {
        "id": vid, "title": v["t"], "ch": v["ct"], "cid": v["c"],
        "pub": v["p"], "dur": v["d"], "cat": CATEGORY_JA.get(v["cat"], "その他"),
        "views": last[1], "likes": last[2], "comments": last[3],
    }
    d.update(extra)
    return d


def rank_lists(st):
    """kind(video/live) ごとに 急上昇・デイリー・ウィークリー のスコア順リストを作る"""
    vids = {k: v for k, v in st["videos"].items() if v["snaps"] and v["snaps"][-1][0] == NOW}
    raw = {k: {"trending": [], "daily": [], "weekly": []} for k in ("video", "live")}
    for vid, v in vids.items():
        g24, e24 = gain(v, 24)
        g7, e7 = gain(v, 24 * 7)
        v["g24"], v["g7"] = g24, g7
        r = raw[v.get("k", "video")]
        r["daily"].append((g24, vid, e24))
        r["weekly"].append((g7, vid, e7))
        age_h = max(0.0, (NOW - v["p"]) / 3600)
        if age_h <= CFG["trend_max_age_h"]:
            w = min(CFG["trend_window_h"], max(age_h, 0.25))
            gw, est = gain(v, w)
            rate = gw / w
            score = rate / math.sqrt(1 + age_h / 12)  # 新しさで補正（調整可）
            r["trending"].append((score, vid, (rate, est)))
    for r in raw.values():
        for arr in r.values():
            arr.sort(key=lambda x: (-x[0], x[1]))
            del arr[CFG["list_size"]:]
    return vids, raw


def build_output(st, vids, raw, live_now):
    prev = st.get("prev_ranks", {})
    out = {"updatedAt": NOW, "lists": {"video": {}, "live": {}}}
    new_prev = {}
    for kind, lists in raw.items():
        for name, arr in lists.items():
            key = f"{kind}/{name}"
            lst = []
            for i, (score, vid, x) in enumerate(arr, 1):
                extra = {"rank": i, "prev": prev.get(key, {}).get(vid)}
                if name == "trending":
                    extra.update(rate=round(x[0]), est=bool(x[1]))
                else:
                    extra.update(gain=score, est=bool(x))
                lst.append(item(vids[vid], vid, **extra))
            out["lists"][kind][name] = lst
            new_prev[key] = {x["id"]: x["rank"] for x in lst}
    live_now.sort(key=lambda x: (-x["viewers"], x["id"]))
    now_list = []
    for i, x in enumerate(live_now[: CFG["live_now_size"]], 1):
        x.update(rank=i, prev=prev.get("live/now", {}).get(x["id"]))
        now_list.append(x)
    out["lists"]["live"]["now"] = now_list
    new_prev["live/now"] = {x["id"]: x["rank"] for x in now_list}
    st["prev_ranks"] = new_prev

    # チャンネルの勢い（次回の新着チェック優先度）
    heat = {}
    for v in vids.values():
        heat[v["c"]] = heat.get(v["c"], 0) + v.get("g7", 0)
    for x in live_now:
        heat[x["cid"]] = heat.get(x["cid"], 0) + x["viewers"] * 10
    for cid, ch in st["channels"].items():
        ch["heat"] = int(ch.get("heat", 0) * 0.7 + heat.get(cid, 0) * 0.3)
    return out


def prune(st):
    keep_from = NOW - CFG["keep_snaps_h"] * 3600
    vids = st["videos"]
    for v in vids.values():
        v["snaps"] = [s for s in v["snaps"] if s[0] >= keep_from] or v["snaps"][-1:]
    old_h = 72 * 3600
    # 3日より古い動画: 週間の伸び上位だけ残す。伸びのないものは削除
    old = sorted((k for k, v in vids.items() if NOW - v["p"] > old_h), key=lambda k: -vids[k].get("g7", 0))
    for k in old[CFG["keep_top_weekly"]:]:
        del vids[k]
    for k in [k for k, v in vids.items() if NOW - v["p"] > old_h and v.get("g7", 0) < 1000]:
        del vids[k]
    # 全体の上限: 24時間の伸びが小さい順に削除
    if len(vids) > CFG["max_tracked"]:
        for k in sorted(vids, key=lambda k: vids[k].get("g24", 0))[: len(vids) - CFG["max_tracked"]]:
            del vids[k]
    st["rejected"] = {k: t for k, t in st["rejected"].items() if t >= NOW - 8 * 86400}
    st["live_seen"] = {k: v for k, v in st["live_seen"].items() if NOW - v["t"] < 3 * 86400}
    chans = st["channels"]
    if len(chans) > 6000:
        used_c = {v["c"] for v in vids.values()}
        for cid in sorted(chans, key=lambda c: chans[c].get("heat", 0))[: len(chans) - 6000]:
            if cid not in used_c:
                del chans[cid]


def main():
    if not MOCK and not KEY:
        sys.exit("YT_API_KEY が設定されていません")
    st = load_state()
    log(f"state: videos={len(st['videos'])} channels={len(st['channels'])}")
    live_now = []
    try:
        migrate(st)
        cands = discover(st)
        live_now = admit_new(st, cands)
    except QuotaBudgetExceeded as e:
        log("quota budget reached during discovery:", e)
    refresh_stats(st)
    vids, raw = rank_lists(st)
    listed = {vid for r in raw.values() for arr in r.values() for _, vid, _ in arr}
    refresh_titles(st, listed)
    out = build_output(st, vids, raw, live_now)
    prune(st)
    out["quota"] = dict(used)
    out["tracked"] = len(st["videos"])
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "ranking.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    save_state(st)
    log(f"done. quota used this run: {used}, tracked: {len(st['videos'])}")


if __name__ == "__main__":
    main()
