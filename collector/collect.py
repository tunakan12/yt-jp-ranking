#!/usr/bin/env python3
"""
日本のYouTube 通常動画ランキング / 急上昇 コレクター

- YouTube Data API v3 の無料枠（1日10,000ユニット）内で動くように設計
- ショート動画・ライブ中/配信予定は除外
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
    "search_durations": ["medium", "long"],  # 4分以上（short=4分未満はショート混入が多いので検索しない）
    "most_popular_pages": 4,          # 公式 mostPopular(JP) 最大200件
    "channel_poll_per_run": 250,      # 1回の実行で新着チェックするチャンネル数
    "channel_new_window_h": 72,       # チャンネル新着として追加する投稿の新しさ
    "keep_snaps_h": 24 * 8,           # スナップショット保持期間
    "keep_top_weekly": 3000,          # 古い動画でも週間の伸び上位はこの件数まで追跡継続
    "min_duration_s": 61,             # これ以下はショート扱いで除外
    "shorts_check_max_s": 180,        # 61〜180秒は /shorts/ URL で判定
    "trend_max_age_h": 72,            # 急上昇の対象は投稿72時間以内
    "trend_window_h": 4,              # 急上昇の速度を測る直近時間
    "list_size": 100,
    "quota_budget_per_run": 750,      # 12回/日 × 750 = 9,000（安全マージン込み）
}

QUOTA_COST = {"videos": 1, "channels": 1, "playlistItems": 1, "search": 100}

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
quota_used = 0


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- API
def api(endpoint, **params):
    global quota_used
    cost = QUOTA_COST[endpoint]
    if quota_used + cost > CFG["quota_budget_per_run"]:
        raise QuotaBudgetExceeded(endpoint)
    quota_used += cost
    if MOCK:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import mock_api  # noqa
        return mock_api.handle(endpoint, params, NOW)
    params["key"] = KEY
    url = "https://www.googleapis.com/youtube/v3/" + endpoint + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code == 403 and "quota" in body.lower():
                raise QuotaBudgetExceeded("daily quota exhausted: " + body)
            if e.code >= 500 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{endpoint} HTTP {e.code}: {body}")
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise


class QuotaBudgetExceeded(Exception):
    pass


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
    st.setdefault("videos", {})     # id -> info + snaps
    st.setdefault("channels", {})   # id -> {t, country, subs, cursor, heat}
    st.setdefault("rejected", {})   # id -> ts（再取得しない）
    st.setdefault("prev_ranks", {})
    st.setdefault("channel_cursor", 0)
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- 収集
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

    # 2) 検索: 直近48hで再生数の多い日本向け動画（1回100ユニットなので最小限）
    after = ts_to_iso(NOW - CFG["search_window_h"] * 3600)
    n0 = len(cands)
    for dur in CFG["search_durations"]:
        r = api("search", part="id", type="video", order="viewCount", regionCode=CFG["region"],
                relevanceLanguage="ja", videoDuration=dur, publishedAfter=after, maxResults=50)
        cands.update(i["id"]["videoId"] for i in r.get("items", []) if i.get("id", {}).get("videoId"))
    log(f"search: +{len(cands) - n0}")

    # 3) 追跡中チャンネルの新着（1ユニット/チャンネル）: 勢いのあるチャンネル優先＋残りを巡回
    chans = st["channels"]
    ranked = sorted(chans, key=lambda c: -chans[c].get("heat", 0))
    limit = CFG["channel_poll_per_run"]
    hot = ranked[: limit // 2]
    rest = ranked[limit // 2:]
    cur = st["channel_cursor"] % max(1, len(rest)) if rest else 0
    rot = (rest[cur:] + rest[:cur])[: limit - len(hot)]
    st["channel_cursor"] = cur + len(rot)
    n0 = len(cands)
    cutoff = NOW - CFG["channel_new_window_h"] * 3600
    for cid in hot + rot:
        try:
            r = api("playlistItems", part="contentDetails", playlistId="UU" + cid[2:], maxResults=10)
        except RuntimeError as e:  # チャンネル削除など
            log("skip channel", cid, str(e)[:80])
            continue
        for it in r.get("items", []):
            cd = it.get("contentDetails", {})
            pub = cd.get("videoPublishedAt")
            if pub and iso_to_ts(pub) >= cutoff:
                cands.add(cd["videoId"])
    log(f"channels polled {len(hot) + len(rot)}: +{len(cands) - n0}")
    return cands


def admit_new(st, cands):
    """新規候補の詳細を取得して、日本の通常動画だけ追跡対象に加える"""
    new = [v for v in cands if v not in st["videos"] and v not in st["rejected"]]
    info = {}
    for ch in chunks(new, 50):
        r = api("videos", part="snippet,contentDetails,statistics", id=",".join(ch), maxResults=50)
        for it in r.get("items", []):
            info[it["id"]] = it

    # 新しいチャンネルの国情報
    need = {it["snippet"]["channelId"] for it in info.values()} - set(st["channels"])
    for ch in chunks(need, 50):
        r = api("channels", part="snippet,statistics", id=",".join(ch), maxResults=50)
        for it in r.get("items", []):
            st["channels"][it["id"]] = {
                "t": it["snippet"]["title"],
                "country": it["snippet"].get("country", ""),
                "subs": int(it.get("statistics", {}).get("subscriberCount", 0) or 0),
                "lang": it["snippet"].get("defaultLanguage", ""),
                "heat": 0,
            }

    added = 0
    for vid, it in info.items():
        sn, cd, stt = it["snippet"], it["contentDetails"], it.get("statistics", {})
        if sn.get("liveBroadcastContent") in ("live", "upcoming"):
            continue  # 終わったら次回以降に再評価
        ch = st["channels"].get(sn["channelId"], {})
        lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower()
        kana = bool(KANA_RE.search(sn.get("title", "")))
        if lang and not lang.startswith("ja"):
            japanese = kana  # 音声言語が日本語以外でも、タイトルにかなが入っていれば日本向けとみなす
        else:
            japanese = (lang.startswith("ja") or kana or ch.get("country") == "JP"
                        or bool(KANA_RE.search(sn.get("channelTitle", ""))))
        dur = parse_duration(cd.get("duration"))
        reason = None
        if not japanese:
            reason = "lang"
        elif dur < CFG["min_duration_s"]:
            reason = "short"
        elif dur <= CFG["shorts_check_max_s"]:
            s = is_shorts_url(vid)
            if s is True or (s is None and dur <= 60):
                reason = "short"
        if reason:
            st["rejected"][vid] = NOW
            continue
        st["videos"][vid] = {
            "t": sn["title"],
            "c": sn["channelId"],
            "ct": sn["channelTitle"],
            "p": iso_to_ts(sn["publishedAt"]),
            "d": dur,
            "cat": sn.get("categoryId", ""),
            "snaps": [],
        }
        added += 1
    log(f"new candidates {len(new)} -> admitted {added}")


def refresh_stats(st):
    ids = list(st["videos"])
    alive = set()
    for ch in chunks(ids, 50):
        r = api("videos", part="statistics,snippet", id=",".join(ch), maxResults=50)
        for it in r.get("items", []):
            v = st["videos"][it["id"]]
            s = it.get("statistics", {})
            v["t"] = it["snippet"]["title"]  # タイトル変更に追従
            v["snaps"].append([NOW, int(s.get("viewCount", 0) or 0),
                               int(s.get("likeCount", 0) or 0), int(s.get("commentCount", 0) or 0)])
            alive.add(it["id"])
    for vid in set(ids) - alive:  # 削除・非公開
        del st["videos"][vid]
    log(f"stats refreshed: {len(alive)}")


# ---------------------------------------------------------------- 計算
def views_at(v, t):
    """時刻tの推定再生数。(値, 推定かどうか)"""
    snaps = v["snaps"]
    if t <= v["p"]:
        return 0, False
    if not snaps:
        return None, True
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


def compute(st):
    vids = {k: v for k, v in st["videos"].items() if v["snaps"]}
    daily, weekly, trend = [], [], []
    for vid, v in vids.items():
        g24, e24 = gain(v, 24)
        g7, e7 = gain(v, 24 * 7)
        v["g24"], v["g7"] = g24, g7
        daily.append((g24, vid, e24))
        weekly.append((g7, vid, e7))
        age_h = (NOW - v["p"]) / 3600
        if age_h <= CFG["trend_max_age_h"]:
            w = min(CFG["trend_window_h"], max(age_h, 0.25))
            gw, _ = gain(v, w)
            rate = gw / w
            score = rate / math.sqrt(1 + age_h / 12)  # 新しさで補正（調整可）
            trend.append((score, vid, rate))

    n = CFG["list_size"]
    prev = st.get("prev_ranks", {})
    out = {"updatedAt": NOW, "lists": {}}
    new_prev = {}
    for name, arr, key in (("trending", trend, "rate"), ("daily", daily, "gain"), ("weekly", weekly, "gain")):
        arr.sort(reverse=True)
        lst, ranks = [], {}
        for i, (score, vid, x) in enumerate(arr[:n], 1):
            v = vids[vid]
            extra = {"rank": i, "prev": prev.get(name, {}).get(vid)}
            if name == "trending":
                extra.update(rate=round(x), score=round(score, 2))
            else:
                extra.update(gain=score, est=bool(x))
            lst.append(item(v, vid, **extra))
            ranks[vid] = i
        out["lists"][name] = lst
        new_prev[name] = ranks
    st["prev_ranks"] = new_prev

    # チャンネルの勢い（次回の新着チェック優先度）
    heat = {}
    for vid, v in vids.items():
        heat[v["c"]] = heat.get(v["c"], 0) + v.get("g7", 0)
    for cid, ch in st["channels"].items():
        ch["heat"] = int(ch.get("heat", 0) * 0.7 + heat.get(cid, 0) * 0.3)
    return out


def prune(st):
    keep_from = NOW - CFG["keep_snaps_h"] * 3600
    vids = st["videos"]
    for v in vids.values():
        v["snaps"] = [s for s in v["snaps"] if s[0] >= keep_from] or v["snaps"][-1:]
    # 3日以内の動画は全保持、それ以外は週間の伸び上位のみ
    old = [k for k, v in vids.items() if NOW - v["p"] > 72 * 3600]
    old.sort(key=lambda k: -vids[k].get("g7", 0))
    for k in old[CFG["keep_top_weekly"]:]:
        del vids[k]
    # 伸びのない古い動画も削除
    for k in [k for k, v in vids.items() if NOW - v["p"] > 72 * 3600 and v.get("g7", 0) < 1000]:
        del vids[k]
    st["rejected"] = {k: t for k, t in st["rejected"].items() if t >= NOW - 8 * 86400}
    # 動画が1本も残っておらず勢いもないチャンネルは削除（上限管理）
    used = {v["c"] for v in vids.values()}
    chans = st["channels"]
    if len(chans) > 5000:
        for cid in sorted(chans, key=lambda c: chans[c].get("heat", 0))[: len(chans) - 5000]:
            if cid not in used:
                del chans[cid]


def main():
    if not MOCK and not KEY:
        sys.exit("YT_API_KEY が設定されていません")
    st = load_state()
    log(f"state: videos={len(st['videos'])} channels={len(st['channels'])}")
    try:
        cands = discover(st)
        admit_new(st, cands)
    except QuotaBudgetExceeded as e:
        log("quota budget reached during discovery:", e)
    # 統計更新は残り予算で必ず実行（足りなければ途中まで）
    global quota_used
    need = math.ceil(len(st["videos"]) / 50)
    if quota_used + need > CFG["quota_budget_per_run"]:
        log("warning: stats refresh exceeds budget; allowing overrun of", need)
        CFG["quota_budget_per_run"] = quota_used + need
    refresh_stats(st)
    out = compute(st)
    prune(st)
    out["quota"] = quota_used
    out["tracked"] = len(st["videos"])
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "ranking.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    save_state(st)
    log(f"done. quota used this run: {quota_used}, tracked: {len(st['videos'])}")


if __name__ == "__main__":
    main()
