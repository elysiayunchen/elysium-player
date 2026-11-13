# -*- coding: utf-8 -*-
"""
FlacMusic - 无损音乐桌面客户端

架构：
    本地 ThreadingHTTPServer（127.0.0.1 随机端口）
        /                  静态前端（web/）
        /api/rpc           转发到 flac.music.hi.cn/ajax.php（绕开 CORS）
        /api/store         本地用户数据（收藏 / 历史 / 下载记录 / 设置）
        /api/eq/parse      AutoEq APO 文本解析（发烧客参数 EQ 导入，只解析不落盘）
        /api/downloads*    下载队列（并发、速度、取消、重试、自动降级）
        /api/save-lyric    保存 .lrc
    pywebview 开窗指向本地服务。

用户数据：%APPDATA%\\FlacMusic\\data.json
"""

import collections
import concurrent.futures as _cf
import hashlib
import http.client
import importer                      # 歌单导入（规则外置 + 三桶判定），见 providers/
import sync_engine                   # 多端同步（用户自己的 WebDAV）
import json
import math
import os
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "Elysium"
APP_TITLE = "Elysium - 在线音乐"
VERSION = "2.3.0-beta5"
_BASE_URL = ""            # main() 里落值；桌面歌词小窗据此建页
_LYRIC_WIN = None         # 桌面歌词小窗句柄（pywebview window 或 None）
LEGACY_NAME = "FlacMusic"
UPSTREAM = "https://flac.music.hi.cn/"
API_URL = UPSTREAM + "ajax.php?act="
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PLATFORMS = ("kuwo", "wyy")
UPSTREAM_HOST = "flac.music.hi.cn"
_TLS = threading.local()          # 每线程一条上游长连接


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def resource_dir():
    home = os.environ.get("ELYSIUM_HOME")      # Android / 自定义根目录
    if home:
        return home
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    root = os.environ.get("ELYSIUM_HOME") or os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(root, APP_NAME)
    os.makedirs(d, exist_ok=True)
    # 从旧名字迁移一次
    old = os.path.join(root, LEGACY_NAME)
    new_data = os.path.join(d, "data.json")
    old_data = os.path.join(old, "data.json")
    if not os.path.isfile(new_data) and os.path.isfile(old_data):
        try:
            import shutil
            shutil.copy2(old_data, new_data)
        except Exception:
            pass
    return d


def default_download_dir():
    music = os.path.join(os.path.expanduser("~"), "Music")
    if not os.path.isdir(music):
        music = data_dir()
    return os.path.join(music, APP_NAME)


DATA_FILE = os.path.join(data_dir(), "data.json")
DEFAULT_STORE = {
    "settings": {
        "platform": "kuwo",
        "quality": "best",             # 下载音质：best / 320 / 128
        "play_quality": "auto",        # 在线播放音质：auto / best / 320 / 128 / flac
        "download_dir": default_download_dir(),
        "concurrency": 2,
        "name_template": "{name} - {artist}",
        "dup_policy": "rename",     # rename / overwrite / skip
        "write_tags": True,
        "save_lyric": False,
        "auto_check_update": True,  # 自动追踪新版本：只提醒，不下载
        "theme": "dark",            # light / dark / auto（默认深色）
        "volume": 80,
        "cache_limit_mb": 2048,     # 试听缓存上限（当天）
        "cache_min_plays": 2,       # 当天听满几次进当天缓存
        "cache_week_plays": 5,      # 7 天内听满几次进长期缓存
        "cache_keep_days": 3,       # 长期缓存几天没听就清掉
        "auto_cache": True,         # 听满后自动缓存
        "mode": "order",            # order / one / shuffle
        "last_track": None,         # 上次播放，用于续播
        "personalized": True,       # 个性化推荐总开关
        "lrc_offset_ms": 0,         # 歌词时间轴整体偏移：正=歌词延后，负=提前
        # --- 播放引擎 2.0（双 audio + Web Audio）---
        # engine_v2 不写默认值：留空表示「跟随平台」——桌面开、Android 关
        # （P4.0 只做了桌面 spike，Android 真机验证前不默认接管音频图）
        "gapless": True,            # 专辑顺序播放时曲末无缝接下一首
        "crossfade_ms": 0,          # 交叉淡入淡出：0 = 关（只走 gapless 的短交叉）
        "eq_enabled": False,        # 均衡器总开关
        "eq_preset": "flat",        # flat / bass / vocal / classical / pop / rock / custom
        "eq_gains": [0] * 10,       # 10 段实际生效的 dB 值，eq_preset 只是来源标记
        # --- 发烧客：参数 EQ（Phase 2，plans/AUDIOPHILE-P2）---
        # eq_mode 缺省 graphic：老用户走原 10 推子路径，零变化
        "eq_mode": "graphic",       # graphic（10 推子）/ parametric（频率/Q/增益全可调）
        "eq_bands": [],             # 参数 EQ 段表 ≤10 段 {type,f,q,db}，空 = 全平直
        "eq_preamp": 0,             # 前级 dB；自动补偿拉着它防削波
        "eq_auto_preamp": True,     # 正增益先在前级降下来，EQ 拉满也不过 0dBFS
        "eq_headphone": "",         # AutoEq 导入的耳机型号名（仅显示用）
        # --- 发烧客：响度归一化 ---
        # 扫描结果由前端 Web Audio 算（EBU R128），后端只管存取；mode 开关与
        # 目标响度先立住契约，前端接 UI 时直接用。
        "loudness_mode": "off",     # off / track：按曲目扫描结果预置增益
        "loudness_target": -18,     # 归一化目标 LUFS（ReplayGain 2.0 音乐惯例）
        # 额外曲库目录：默认空 = 仍只扫下载目录，老用户零变化。
        # 只收用户显式添加的目录，不做全盘扫描（这条边界不放开）。
        "local_dirs": [],
        # --- 窗口 ---
        "tray": True,               # 托盘图标；关掉后关闭按钮只能退出
        "close_behavior": "exit",   # exit / tray：点关闭时退出还是收进托盘
    },
    "favorites": [],                # 收藏：{kind: song|album|artist, ...}
    "history": [],                  # 搜索历史
    "recent": [],                   # 最近播放（去重，最多 60 首）
    "downloads": [],
    "plays": {},                    # 当天收听计数 {key: {days, meta, ...}}，只留 7 天
    "daily": {},                    # 日汇总 {date: {n: 次数, s: 秒}}，统计用，留 400 天
    "hours": {},                    # 24 小时时段分布（累计）{"0"~"23": 次数}
    "feedback": {},                 # 显式反馈 {platform:id: {verdict, at}}
    "loudness": {},                 # 响度扫描 {platform:id: {lufs, tp, lra, at}}，上限 LOUDNESS_MAX
    "eqpresets": {},                # 参数 EQ 预设库 {name: {bands, preamp, at}}，
                                    # AutoEq 导入的耳机档案 / 用户自存调音，上限 EQPRESET_MAX
    "taste_note": "",               # 用户自述口味（旧字段，保留兼容，前端已不再暴露）
    "taste_genres": [],             # 用户手动选中的风格 id 列表
    "playlists": [],                # 用户自建歌单 [{id, name, created_at, updated_at, items:[...]}]
    "works": [],                    # K 歌作品（P8）[{id, name, artist, platform, sid, cover,
                                    #   duration, created, file, size}]，文件在 data_dir()/works/
}


# --------------------------------------------------------------------------
# 本地存储
# --------------------------------------------------------------------------
def fav_key(f):
    """收藏唯一键：类型 + id + 平台。旧数据没有 kind，按 song 处理。

    非对象项（手改 / 同步盘截断后混进来的数字、字符串）没有唯一键，
    返回一个永远不会与合法收藏相等的哨兵，调用方负责清理。
    """
    if not isinstance(f, dict):
        return (None, None, None)
    return ((f.get("kind") or "song"), str(f.get("id")), f.get("platform"))


def pl_item_key(it):
    """歌单内单曲唯一键：平台 + id。与收藏的三元组不同 —— 歌单只收 song。"""
    return (str(it.get("platform") or ""), str(it.get("id") or ""))


def pl_item_of(src):
    """把任意曲目对象转成歌单条目：引用 + 冗余兜底字段。

    只留显示必需的信息，冗余是为了歌单列表不必回源就能渲染标题。
    """
    src = src or {}
    out = {
        "platform": str(src.get("platform") or ""),
        "id": str(src.get("id") or ""),
        "name": str(src.get("name") or ""),
        "artist": str(src.get("artist") or ""),
    }
    for k in ("album_name", "cover", "duration", "album_id", "artist_id"):
        v = src.get(k)
        if v not in (None, ""):
            out[k] = v
    return out


# --------------------------------------------------------------------------
# K 歌作品（P8）
# --------------------------------------------------------------------------
# 翻唱录音的落盘位置：data_dir()/works/，与 data.json 同级。
# 刻意不放下载目录 —— 那是用户自己的音乐库，作品是应用数据，
# 混进去会被「本地音乐」扫描到，把翻唱当原曲再播一遍。
# works 元数据不同步 WebDAV（文件在本机，跨机没有意义，与本地曲目同口径）。
WORKS_DIR = os.path.join(data_dir(), "works")
WORK_MAX_BYTES = 200 * 1024 * 1024   # 4 分钟立体声 WAV ≈ 40MB，上限给足余量
WORK_ID_RE = re.compile(r"^[a-z0-9]{1,12}(-[a-z0-9]{1,12}){0,2}$")


def work_file(wid):
    """作品音频路径。id 白名单先验过，这里只拼不查。"""
    return os.path.join(WORKS_DIR, wid + ".wav")


def work_new_id():
    """毫秒时间戳 + 随机后缀：可读、可排序、碰不上。"""
    return "%x-%s" % (int(time.time() * 1000) & 0xFFFFFFFF, os.urandom(4).hex())


def _clamp_int(v, lo, hi, default=0):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_int(v, default=0):
    """任意输入转 int：脏数据（null / "abc" / NaN / inf）一律回默认值。

    int(float("inf")) 抛的是 OverflowError，不是 ValueError —— 只写
    (TypeError, ValueError) 会让一条畸形记录把整个聚合接口带崩。
    """
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(v))
        except (TypeError, ValueError, OverflowError):
            return default


def _as_dict(v):
    return v if isinstance(v, dict) else {}


# json.dumps 默认把 NaN / Infinity 写成同名非法字面量：Python 自己读得回来，
# 浏览器的 JSON.parse 直接 SyntaxError。data.json 里只要混进一个，
# /api/store 的响应就让前端 boot 失败 —— 整页数据全空。
# 所有从 HTTP 进来的 payload 与落盘数据都先过这一层：非有限浮点丢弃、
# 超深/超大结构截断，保证发给前端的永远是合法 JSON。
_JSON_DROP = object()


def _json_safe(v, depth=0):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else _JSON_DROP
    if depth >= 24:
        return _JSON_DROP
    if isinstance(v, list):
        out = []
        for x in v:
            y = _json_safe(x, depth + 1)
            if y is not _JSON_DROP:
                out.append(y)
        return out
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if not isinstance(k, str):
                continue
            y = _json_safe(x, depth + 1)
            if y is not _JSON_DROP:
                out[k] = y
        return out
    return _JSON_DROP


def work_meta(qs, size):
    """从上传 query 组装元数据。字符串一律截断：这些值会进 data.json，
    被页面反复渲染，放任长度等于给以后的自己埋雷。"""
    one = lambda k, d="": str((qs.get(k) or [d])[0])   # noqa: E731
    meta = {
        "id": one("id"),
        "name": (one("name") or "未命名作品")[:120],
        "artist": one("artist")[:120],
        "platform": one("platform")[:20],
        "sid": one("sid")[:64],
        "cover": one("cover")[:600],
        "duration": _clamp_int(one("duration"), 0, 7200),
        "created": int(time.time()),
        "size": _clamp_int(size, 0, WORK_MAX_BYTES),
    }
    # P9 音准结算（可选）：前端唱完才带上。老作品没有这两个字段，展示侧自行降级。
    # pitch_avg = 行内平均音分偏差（0~200，越小越准）；pitch_hit = 判准的行数。
    # isfinite 是硬墙：float("nan")/float("inf") 会穿透 min/max 链，NaN 一旦落进
    # data.json，json.dump 写出非法字面量，下次启动整个数据文件解析报废。
    try:
        v = float(one("pitch_avg"))
        if one("pitch_avg") and math.isfinite(v):
            meta["pitch_avg"] = round(max(0.0, min(200.0, v)), 1)
    except ValueError:
        pass
    try:
        if one("pitch_hit"):
            meta["pitch_hit"] = max(0, min(9999, int(one("pitch_hit"))))
    except ValueError:
        pass
    return meta


def _clean_work(item):
    """store 的 work_add 是公开 op：只收与 work_meta 同口径的字段。

    /api/works/upload 走 work_meta，但直接调 /api/store 可以绕过它 ——
    不在这里再收一次口，任意对象/NaN/超长字符串都能进 data.json。
    """
    item = _as_dict(item)
    wid = str(item.get("id") or "")
    if not WORK_ID_RE.match(wid):
        return None
    out = {"id": wid}
    for k, cap in (("name", 120), ("artist", 120), ("platform", 20),
                   ("sid", 64), ("cover", 600)):
        out[k] = str(item.get(k) or "")[:cap]
    out["name"] = out["name"] or "未命名作品"
    for k, lo, hi in (("duration", 0, 7200), ("created", 0, 2 ** 31),
                      ("size", 0, WORK_MAX_BYTES), ("pitch_hit", 0, 9999)):
        if k in item:
            out[k] = max(lo, min(hi, _safe_int(item.get(k))))
    try:
        v = float(item.get("pitch_avg"))
        if math.isfinite(v):
            out["pitch_avg"] = round(max(0.0, min(200.0, v)), 1)
    except (TypeError, ValueError, OverflowError):
        pass
    return out


# settings 里必须保持数值语义的键：写入前转 int 并钳制范围。
# 不设防的话，一个 "abc" 或 -5 会一路落进 data.json，下一次读取时
# （/api/cache、音量）int() 就崩，或者得到一个负的缓存上限。
_SETTING_NUM = {
    "volume": (0, 100),
    "concurrency": (1, 4),
    "cache_limit_mb": (0, 1024 * 1024),
    "cache_min_plays": (1, 100),
    "cache_week_plays": (1, 1000),
    "cache_keep_days": (1, 365),
    "lrc_offset_ms": (-10000, 10000),
    "crossfade_ms": (0, 2000),
    "loudness_target": (-40, -6),   # 归一化目标 LUFS：别让用户把「目标」设成啸叫
}


def _clean_bands(v, cap=10):
    """参数 EQ 段表清洗，settings 写入与 AutoEq 解析共用同一套口径。

    NaN/inf 是硬墙：落进 data.json 后 json.dump 写出非法字面量，下次启动
    整个数据文件报废（与 loudness/pitch 同一条铁律）。单段非法只丢那一段，
    整表不是数组返回 None。返回 (合法段表, 丢弃数, 截断数)——丢弃 = 逐段
    校验没过的，截断 = 合法但超出 cap 的；先收合法段再截断，前面的段非法
    不占后面合法段的名额。
    """
    if not isinstance(v, list):
        return None, 0, 0
    bands, dropped, truncated = [], 0, 0
    for b in v:
        if len(bands) >= cap:
            truncated += 1
            continue
        if not isinstance(b, dict):
            dropped += 1
            continue
        try:
            f = float(b.get("f"))
            q = float(b.get("q"))
            db = float(b.get("db"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        t = str(b.get("type") or "peaking")
        if t not in ("peaking", "lowshelf", "highshelf"):
            dropped += 1
            continue
        if not (math.isfinite(f) and 20 <= f <= 20000
                and math.isfinite(q) and 0.1 <= q <= 10
                and math.isfinite(db) and -12 <= db <= 12):
            dropped += 1
            continue
        bands.append({"type": t, "f": round(f, 1),
                      "q": round(q, 3), "db": round(db, 1)})
    return bands, dropped, truncated


def _clean_settings(payload):
    """settings 写入前的清洗：非 dict 拒绝，数值键转 int 并钳制，非法值丢弃。"""
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("settings 必须是对象")
    out = {}
    for k, v in payload.items():
        if k == "local_dirs":
            # 必须是数组：写成字符串时老的 _local_roots 会逐字符当目录，
            # 万一碰上 "." 就会把整个工作目录扫进曲库。
            if isinstance(v, list):
                out[k] = [str(x).strip() for x in v if str(x or "").strip()][:20]
            continue
        if k == "eq_gains":
            # 图形 EQ 推子值：只收有限数值。NaN/inf 落盘会写出非法 JSON，
            # 字符串 / 对象也会让前端的曲线计算得到 NaN。
            if isinstance(v, list):
                gains = []
                for x in v[:10]:
                    if isinstance(x, (int, float)) and not isinstance(x, bool) \
                            and math.isfinite(float(x)):
                        gains.append(x)
                out[k] = gains
            continue
        if k == "download_dir":
            # 必须是字符串：写进对象 / 数字后，下载与歌词保存会在 os.path
            # 拼接处抛 TypeError，设置页却显示「已保存」。
            if isinstance(v, str):
                out[k] = v.strip()
            continue
        if k == "name_template":
            if isinstance(v, str):
                out[k] = v[:200]
            continue
        if k == "webdav":
            # 同步配置由 /api/sync/config 维护；这里只做类型兜底，
            # 防止一个数组/字符串让 /api/sync/status 的 .get 直接抛错。
            if isinstance(v, dict):
                out[k] = v
            continue
        if k == "eq_mode":
            # 白名单外丢弃：graphic 是老 10 推子路径，parametric 是 P2 参数 EQ
            if v in ("graphic", "parametric"):
                out[k] = v
            continue
        if k == "eq_bands":
            # 参数 EQ 段表，校验口径全在 _clean_bands（AutoEq 解析同源）。
            bands = _clean_bands(v)[0]
            if bands is not None:
                out[k] = bands
            continue
        if k == "eq_preamp":
            # 参数 EQ 前级保留一位小数：AutoEq 解析值是 -6.5 这类小数，走
            # _SETTING_NUM 的 int 口径会被截成 -6，跟解析值、UI 读数对不上。
            try:
                n = float(v)
            except (TypeError, ValueError):
                continue                # 非法值不落盘，保留原值
            if math.isfinite(n):        # NaN/inf 硬墙，与段表同一条铁律
                out[k] = round(max(-12.0, min(12.0, n)), 1)
            continue
        if k == "eq_auto_preamp":
            if isinstance(v, bool):
                out[k] = v
            continue
        if k == "eq_headphone":
            # 型号名仅显示用。空串是合法状态（用户清掉了型号）：显式清空要落盘，
            # 以前当脏数据丢弃会让前端清空后重启、旧型号又复活回 UI。
            if isinstance(v, str):
                out[k] = v.strip()[:80]
            continue
        rule = _SETTING_NUM.get(k)
        if not rule:
            vv = _json_safe(v)
            if vv is not _JSON_DROP:
                out[k] = vv
            continue
        lo, hi = rule
        try:
            n = int(float(v))
        except (TypeError, ValueError, OverflowError):
            continue                        # 非法值不落盘，保留原值
        out[k] = max(lo, min(hi, n))
    return out


def _int_setting(cfg, key, default):
    """读 settings 里的整数项：脏数据（"abc" / null）回落到默认值。"""
    try:
        return int((cfg or {}).get(key, default))
    except (TypeError, ValueError, AttributeError):
        return default


# --------------------------------------------------------------------------
# AutoEq 导入解析（plans/AUDIOPHILE-P2 C-1，解析放后端：段表校验口径只写
# main.py 一处，前端粘贴文本拿到的是可直接落 settings 的 eq_bands，不在
# JS 里复刻第二遍）。文本格式 = Equalizer APO / autoeq.app 的
# ParametricEQ.txt：
#     Preamp: -6.5 dB
#     Filter 1: ON PK Fc 31 Hz Gain 7.9 dB Q 0.70
#     Filter 8: ON HS Fc 10000 Hz Gain -2.0 dB Q 0.70
# PK/LS/HS → peaking/lowshelf/highshelf；其余行（注释 / 空行 / 其它工具的
# 导出段落）跳过；None 占位与 OFF 滤波器是作者主动空着 / 停用的，不算数据。
# --------------------------------------------------------------------------
_AUTOEQ_PREAMP_RE = re.compile(
    r"^preamp\s*:\s*([+-]?\d+(?:\.\d+)?)\s*dB\s*$", re.I)
_AUTOEQ_FILTER_RE = re.compile(
    r"^filter\s*\d+\s*:\s*(on|off)\s+(\S+)"
    r"\s+fc\s+(\d+(?:\.\d+)?)\s*(hz|khz)?"
    r"(?:\s+gain\s+([+-]?\d+(?:\.\d+)?)\s*dB)?"
    r"(?:\s+q\s+(\d+(?:\.\d+)?))?\s*$", re.I)
_AUTOEQ_TYPES = {"PK": "peaking", "LS": "lowshelf", "HS": "highshelf"}
AUTOEQ_TEXT_MAX = 65536          # 正则先限长（verify_edge 同款教训）


def parse_autoeq_apo(text):
    """解析 AutoEq 导出的 APO 参数 EQ 文本，返回可直接落 settings 的段表。

    返回 {"preamp": dB(已按 eq_preamp 口径钳 [-12,12])，"bands": [...],
          "total": 识别出的滤波器行数, "dropped": 校验没过的段数,
          "truncated": 超 10 段被截掉的合法段数}。
    一条滤波器都解不出来、或识别到的段全部无效时抛 ValueError —— 宁可不
    导入，不给假情报（plans/AUDIOPHILE-P2 C-1）。
    """
    if not isinstance(text, str):
        raise ValueError("导入内容必须是文本")
    if len(text) > AUTOEQ_TEXT_MAX:
        raise ValueError("导入内容过长（上限 %d 字符）" % AUTOEQ_TEXT_MAX)
    preamp = 0.0
    candidates = []                  # 段 dict 或 None（None = 解析到但校验必挂）
    total = 0
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _AUTOEQ_PREAMP_RE.match(line)
        if m:
            try:
                v = float(m.group(1))
                if math.isfinite(v):
                    preamp = v
            except ValueError:
                pass
            continue
        m = _AUTOEQ_FILTER_RE.match(line)
        if not m:
            continue
        state, ftype, fc, unit, gain, q = m.groups()
        if state.lower() != "on":
            continue                  # OFF = 作者主动停用，不是要导入的数据
        ftype = ftype.upper()
        if ftype == "NONE":
            continue                  # 老版 AutoEq 的空槽占位行
        total += 1
        if ftype not in _AUTOEQ_TYPES:
            candidates.append(None)   # LP/HP/BP/NO 等本应用不支持的类型
            continue
        try:
            f = float(fc) * (1000.0 if (unit or "hz").lower() == "khz" else 1.0)
            db = float(gain) if gain is not None else 0.0
            qv = float(q) if q is not None else 1.0
        except ValueError:
            candidates.append(None)
            continue
        if not (math.isfinite(f) and math.isfinite(db) and math.isfinite(qv)):
            candidates.append(None)
            continue
        candidates.append({"type": _AUTOEQ_TYPES[ftype],
                           "f": f, "q": qv, "db": db})
    bands, dropped, truncated = _clean_bands(candidates, cap=10)
    if not bands:
        if not total:
            raise ValueError("没有识别到任何滤波器行（应为 AutoEq / "
                             "Equalizer APO 的 ParametricEQ 文本）")
        raise ValueError("识别到 %d 段滤波器但没有一段可用（频率 20-20k / "
                         "Q 0.1-10 / 增益 ±12dB）" % total)
    return {"preamp": round(max(-12.0, min(12.0, preamp)), 1),
            "bands": bands, "total": total,
            "dropped": dropped, "truncated": truncated}


# 合法的 store 操作白名单。写在 apply 外面，HTTP 层与 apply 共用同一份，
# 避免「接口放行、内部静默忽略」的两套口径。
STORE_OPS = frozenset((
    "load", "settings", "history_add", "history_clear",
    "fav_toggle", "fav_remove", "download_add", "downloads_clear",
    "favorites_clear", "recent_add", "recent_clear",
    "feedback_set", "feedback_clear", "taste_note", "taste_genres",
    "playlist_create", "playlist_rename", "playlist_delete",
    "playlist_add", "playlist_remove", "playlist_reorder",
    "work_add", "work_remove", "loudness_set",
    "eqpreset_set", "eqpreset_del",
))

# 响度扫描结果条数上限：一首一条，正常人几千首也用不满；
# 超限按扫描时间淘汰最旧的，data.json 不许无限长。
LOUDNESS_MAX = 5000

# 参数 EQ 预设上限：一款耳机一条，AutoEq 库虽大但用户只会导入真用过的
# 几款；超限按保存时间淘汰最旧的，与 LOUDNESS_MAX 同一条策略。
EQPRESET_MAX = 50


def _prune_by_at(d, cap):
    """按 at 时间戳裁掉最旧的条目，字典不许无限长（响度 / EQ 预设共用）。"""
    if len(d) <= cap:
        return
    for k in sorted(d, key=lambda k: d[k].get("at", 0))[:len(d) - cap]:
        d.pop(k, None)


def _loudness_prune(d):
    _prune_by_at(d, LOUDNESS_MAX)


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    def _quarantine(self):
        """把读不动的 data.json 另存一份 .bak。

        回落默认值后下一次 save 会覆盖原文件；留一份备份，收藏 / 歌单还有机会
        人工抢救。只保留最近一份，反复损坏不会堆一堆文件。
        """
        try:
            if os.path.getsize(self.path) <= 0:
                return                      # 空文件没有可救的内容
            shutil.copy2(self.path, self.path + ".bak")
        except OSError:
            pass

    def _load(self):
        if not os.path.isfile(self.path):
            return json.loads(json.dumps(DEFAULT_STORE))
        try:
            # utf-8-sig：带 BOM 的文件（记事本 / 部分同步盘写出）以前会解析失败，
            # 整份数据被当成损坏回落默认值 —— 用户的收藏就这样无声消失。
            with open(self.path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
        except Exception:
            self._quarantine()
            return json.loads(json.dumps(DEFAULT_STORE))
        if not isinstance(raw, dict):
            # 被同步盘 / 编辑器写成一截字符串、数组或数字时，旧代码会直接在
            # `(raw or {}).items()` 上抛 AttributeError —— 进程启动即死，用户
            # 连设置页都打不开。类型不对一律按损坏处理，回落默认值。
            self._quarantine()
            return json.loads(json.dumps(DEFAULT_STORE))
        merged = json.loads(json.dumps(DEFAULT_STORE))
        for k, v in (raw or {}).items():
            if k == "settings" and isinstance(v, dict):
                merged["settings"].update(v)
            elif isinstance(merged.get(k), list) and isinstance(v, list):
                merged[k] = v
            elif isinstance(merged.get(k), dict) and isinstance(v, dict):
                # plays / feedback 都是 dict。之前只恢复 list，导致每次启动
                # 收听计数和反馈全被清空 —— 口味画像的数字因此永远对不上。
                merged[k] = v
        self._fix_paths(merged)
        merged = _json_safe(merged)
        return merged if isinstance(merged, dict) else json.loads(json.dumps(DEFAULT_STORE))

    def _fix_paths(self, data):
        """修正从别处带来的失效路径。

        data.json 里存着下载目录的绝对路径。别人分享数据文件、
        或用户换了机器 / 换了用户名，这份路径就指到不存在的目录了 ——
        下载会静默失败。这里在加载时校验并回落到本机默认目录。
        类型不对（被手改成对象 / 数字）同样回落：os.path.isdir 对非路径
        会抛 TypeError，不拦的话进程启动即死，连设置页都打不开。
        """
        want = data["settings"].get("download_dir")
        if want and (not isinstance(want, str) or not os.path.isdir(want)):
            fallback = default_download_dir()
            try:
                os.makedirs(fallback, exist_ok=True)
            except Exception:
                pass
            data["settings"]["download_dir"] = fallback

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    # allow_nan=False：宁可这次写入失败（HTTP 层回 ok:false），
                    # 也不把 NaN/Infinity 写进 data.json 让下次启动解析报废。
                    json.dump(self.data, f, ensure_ascii=False, indent=2,
                              allow_nan=False)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise

    @staticmethod
    def _snapshot(v):
        """深拷贝快照：给 HTTP 层的返回值用，见 apply 尾注。"""
        return json.loads(json.dumps(v))

    def apply(self, op, payload):
        if op not in STORE_OPS:
            # 未知 op 早就不该被当成「什么都不做但保存一次」——那会把页面发来的
            # 拼写错误吞掉，还白写一遍磁盘。
            raise ValueError("未知操作 %r" % (op,))
        # 统一净化入口：NaN/inf、非字符串键、超深结构一律在写模型之前丢掉。
        # 各类 op 内部再按自己的语义做长度/范围校验。
        if op != "load":
            payload = _json_safe(payload)
            if payload is _JSON_DROP:
                payload = None
        with self.lock:
            if op == "load":
                return self._snapshot(self.data)
            if op == "settings":
                self.data["settings"].update(_clean_settings(payload))
            elif op == "history_add":
                q = (payload or {}).get("q", "").strip()
                plat = (payload or {}).get("platform", "kuwo")
                if q:
                    items = [h for h in self.data["history"]
                             if not (h.get("q") == q and h.get("platform") == plat)]
                    items.insert(0, {"q": q, "platform": plat, "at": int(time.time())})
                    self.data["history"] = items[:40]
            elif op == "history_clear":
                self.data["history"] = []
            elif op == "fav_toggle":
                item = dict(payload or {})
                # id 是收藏的唯一键。没有 id 的「收藏」只会在列表里留一条
                # 永远点不开、也删不干净的幽灵条目。
                if not str(item.get("id") or "").strip():
                    raise ValueError("收藏缺少 id")
                item["kind"] = item.get("kind") or "song"
                key = fav_key(item)
                # 顺手清掉旧数据里混进来的非对象项：留着既渲染不出东西，
                # 又会让后面的遍历在 .get 上抛错。
                favs = [f for f in self.data["favorites"] if isinstance(f, dict)]
                self.data["favorites"] = favs
                idx = next((i for i, f in enumerate(favs) if fav_key(f) == key), -1)
                if idx >= 0:
                    favs.pop(idx)
                    active = False
                else:
                    item["added"] = int(time.time())
                    favs.insert(0, item)
                    active = True
                self.save()
                return {"favorites": self._snapshot(self.data["favorites"]), "active": active}
            elif op == "fav_remove":
                key = fav_key(payload or {})
                self.data["favorites"] = [f for f in self.data["favorites"]
                                          if isinstance(f, dict) and fav_key(f) != key]
            elif op == "download_add":
                self.data["downloads"].insert(0, payload or {})
                self.data["downloads"] = self.data["downloads"][:300]
            elif op == "downloads_clear":
                self.data["downloads"] = []
            elif op == "favorites_clear":
                kind = (payload or {}).get("kind")
                self.data["favorites"] = ([] if not kind
                                          else [f for f in self.data["favorites"]
                                                if isinstance(f, dict)
                                                and (f.get("kind") or "song") != kind])
            elif op == "recent_add":
                item = dict(payload or {})
                if not item.get("id"):
                    self.data["recent"] = self.data["recent"] or []
                else:
                    recent = self.data.setdefault("recent", [])
                    recent[:] = [r for r in recent
                                 if not (r.get("platform") == item.get("platform")
                                         and str(r.get("id")) == str(item.get("id")))]
                    item["played_at"] = int(time.time())
                    recent.insert(0, item)
                    del recent[60:]
            elif op == "recent_clear":
                self.data["recent"] = []
            elif op == "feedback_set":
                item = dict(payload or {})
                plat, sid = item.get("platform"), str(item.get("id") or "")
                verdict = (item.get("verdict") or "").strip()
                if plat and sid:
                    key = "%s:%s" % (plat, sid)
                    fb = self.data.setdefault("feedback", {})
                    if verdict in ("", "none"):
                        fb.pop(key, None)
                    else:
                        fb[key] = {"verdict": verdict, "at": int(time.time()),
                                   "name": item.get("name"), "artist": item.get("artist")}
            elif op == "feedback_clear":
                self.data["feedback"] = {}
            elif op == "taste_note":
                self.data["taste_note"] = str((payload or {}).get("text") or "")[:280]
            elif op == "taste_genres":
                ids = (payload or {}).get("ids")
                valid = {g["id"] for g in genre_list()["genres"]}
                picked = [str(x) for x in (ids or []) if str(x) in valid]
                self.data["taste_genres"] = list(dict.fromkeys(picked))[:24]
            elif op == "playlist_create":
                self._pl_create(payload or {})
            elif op == "playlist_rename":
                self._pl_rename(payload or {})
            elif op == "playlist_delete":
                self._pl_delete(payload or {})
            elif op == "playlist_add":
                self._pl_add(payload or {})
            elif op == "playlist_remove":
                self._pl_remove(payload or {})
            elif op == "playlist_reorder":
                self._pl_reorder(payload or {})
            elif op == "work_add":
                item = _clean_work(payload)
                if item:
                    works = self.data.setdefault("works", [])
                    works[:] = [w for w in works
                                if w.get("id") != item["id"]]
                    works.insert(0, item)
                    del works[200:]          # 上限与歌单同级：本地录音不该无限吃 data.json
            elif op == "work_remove":
                wid = str((payload or {}).get("id") or "")
                self.data["works"] = [w for w in self.data.setdefault("works", [])
                                      if w.get("id") != wid]
            elif op == "loudness_set":
                # 发烧客响度扫描结果（EBU R128 由前端算，这里只存取）。
                # NaN/inf 是硬墙：落进 data.json 后 json.dump 写出非法字面量，
                # 下次启动整个数据文件解析报废（与 work_meta 同一条铁律）。
                it = payload if isinstance(payload, dict) else {}
                plat = str(it.get("platform") or "").strip()
                sid = str(it.get("id") or "").strip()
                try:
                    lufs = float(it.get("lufs"))
                except (TypeError, ValueError):
                    lufs = None
                # platform 与 id 缺一不写：半个键落盘就是脏数据
                if plat and sid and lufs is not None and math.isfinite(lufs):
                    key = "%s:%s" % (plat, sid)
                    rec = {"lufs": round(max(-70.0, min(0.0, lufs)), 1)}
                    for f, lo, hi in (("tp", -70.0, 9.0), ("lra", 0.0, 90.0)):
                        try:
                            v = float(it.get(f))
                            if math.isfinite(v):
                                rec[f] = round(max(lo, min(hi, v)), 1)
                        except (TypeError, ValueError):
                            pass
                    rec["at"] = int(time.time())
                    loud = self.data.setdefault("loudness", {})
                    loud[key] = rec
                    _loudness_prune(loud)
            elif op == "eqpreset_set":
                # 参数 EQ 预设库（AutoEq 导入的耳机档案 / 用户自存调音）。
                # 段表走 _clean_bands 同一套口径（isfinite 硬墙 + 逐段校验）；
                # name 是检索键，空名不写、bands 为空不写——存一条空预设只会
                # 让预设列表里出现点了没反应的幽灵条目。
                it = payload if isinstance(payload, dict) else {}
                name = str(it.get("name") or "").strip()[:80]
                bands = _clean_bands(it.get("bands"))[0] or []
                try:
                    preamp = float(it.get("preamp"))
                    if not math.isfinite(preamp):
                        preamp = 0.0
                except (TypeError, ValueError):
                    preamp = 0.0
                if name and bands:
                    pre = self.data.setdefault("eqpresets", {})
                    pre[name] = {
                        "bands": bands,
                        "preamp": round(max(-12.0, min(12.0, preamp)), 1),
                        "at": int(time.time()),
                    }
                    _prune_by_at(pre, EQPRESET_MAX)
            elif op == "eqpreset_del":
                it = payload if isinstance(payload, dict) else {}
                name = str(it.get("name") or "").strip()[:80]
                if name:
                    self.data.setdefault("eqpresets", {}).pop(name, None)
            self.save()
            # 返回快照而不是活引用：序列化在锁外进行，交出活引用会和
            # /api/play 之类的并发写撞「dictionary changed size」；
            # /api/store GET 还会往返回值里塞 _data_path，活引用等于污染本体并落盘。
            return self._snapshot(self.data)

    # ------------------------------------------------------------ 用户歌单
    # 与 favorites 完全独立：加歌到歌单不写 favorites，反之亦然。
    def _pls(self):
        ls = self.data.setdefault("playlists", [])
        return ls if isinstance(ls, list) else []

    def _pl_find(self, pid):
        for pl in self._pls():
            if pl.get("id") == pid:
                return pl
        return None

    def _pl_create(self, payload):
        name = str(payload.get("name") or "").strip()[:60]
        if not name:
            return
        now = int(time.time())
        # id 带上时间戳 + 随机后缀，避免同毫秒建两个歌单撞 id
        pid = "pl_%d_%s" % (now, os.urandom(3).hex())
        entry = {"id": pid, "name": name, "created_at": now,
                 "updated_at": now, "items": []}
        # 导入歌单的来源元数据：纯展示用，不参与 items 结构，也不影响去重键。
        # 只收白名单字段，避免把整个预览结果或用户本机路径写进 data.json。
        src = payload.get("source")
        if isinstance(src, dict):
            allowed = ("mode", "platform", "label", "url", "filename",
                       "name", "partial", "note", "rules_version", "imported_at")
            clean = {k: src[k] for k in allowed if src.get(k) not in (None, "")}
            if clean:
                entry["source"] = clean
        self._pls().append(entry)

    def _pl_rename(self, payload):
        pl = self._pl_find(payload.get("id"))
        if not pl:
            return
        name = str(payload.get("name") or "").strip()[:60]
        if not name:
            return
        pl["name"] = name
        pl["updated_at"] = int(time.time())

    def _pl_delete(self, payload):
        pid = payload.get("id")
        self.data["playlists"] = [p for p in self._pls() if p.get("id") != pid]

    def _pl_add(self, payload):
        pl = self._pl_find(payload.get("id"))
        if not pl:
            return
        items = pl.setdefault("items", [])
        srcs = payload.get("items")
        if srcs is None:
            srcs = [payload.get("item")] if payload.get("item") else []
        added = 0
        existing = {pl_item_key(x) for x in items}      # 大批量导入时别 O(n·m) 逐条扫
        for src in srcs:
            it = pl_item_of(src)
            if not it["platform"] or not it["id"]:
                continue
            key = pl_item_key(it)
            if key in existing:
                continue                       # 已在歌单里，不重复加
            existing.add(key)
            it["added_at"] = int(time.time())
            items.append(it)
            added += 1
        if added:
            pl["updated_at"] = int(time.time())

    def _pl_remove(self, payload):
        pl = self._pl_find(payload.get("id"))
        if not pl:
            return
        key = (str(payload.get("platform") or ""), str(payload.get("sid") or ""))
        pl["items"] = [x for x in pl.get("items", []) if pl_item_key(x) != key]
        pl["updated_at"] = int(time.time())

    def _pl_reorder(self, payload):
        """按新顺序重排。order 是旧索引数组，元素不足则追加剩下的。"""
        pl = self._pl_find(payload.get("id"))
        if not pl:
            return
        items = pl.get("items", [])
        order = payload.get("order") or []
        # 去重且保留首次出现的顺序：order=[1,1,0] 曾经让同一首被复制一份、
        # 另一首被挤掉 —— 前端拖拽不会发重复索引，但接口不能靠调用方自觉。
        idx, seen = [], set()
        for i in order:
            if isinstance(i, bool) or not isinstance(i, int):
                continue
            if 0 <= i < len(items) and i not in seen:
                seen.add(i)
                idx.append(i)
        idx += [i for i in range(len(items)) if i not in seen]
        pl["items"] = [items[i] for i in idx]
        pl["updated_at"] = int(time.time())


STORE = Store(DATA_FILE)
sync_engine.bind(STORE, VERSION)


# --------------------------------------------------------------------------
# 上游接口
# --------------------------------------------------------------------------
def upstream_keepalive(act, params):
    """POST 一次 ajax.php，复用长连接。只给「同一关键词连续翻几十页」的场景用
    （歌手页）。上游首个请求要付 ~4s 的 TLS 握手，逐页新开连接时每页都重付
    （实测 6.5s/页 → 复用后 1.4s/页）。线程本地各持一条连接：服务端是多线程的，
    跨线程共用 socket 会串包。

    零散请求走下面 upstream() 的进程级连接池（不再每次新开）：桌面实测
    握手 0.95s/次 → 复用后 0.22s/次，手机网络差距更大。
    """
    body = urllib.parse.urlencode(
        {k: "" if v is None else v for k, v in (params or {}).items()}).encode()
    path = "/ajax.php?act=" + act
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Referer": UPSTREAM,
        "Origin": UPSTREAM.rstrip("/"),
        "Accept": "application/json, text/plain, */*",
    }
    conn = getattr(_TLS, "up", None)
    for attempt in (0, 1):                       # 连接被对端关掉时重连一次
        try:
            if conn is None:
                conn = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=30)
                _TLS.up = conn
            conn.request("POST", path, body=body, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            if r.status != 200:
                raise RuntimeError(f"上游 HTTP {r.status}")
            payload = json.loads(raw.decode("utf-8", "replace"))
            if payload.get("code") != 0:
                raise RuntimeError(payload.get("msg") or f"接口错误 {payload.get('code')}")
            return payload.get("data")
        except RuntimeError:
            raise
        except (http.client.HTTPException, OSError, ValueError):
            try:
                conn.close()
            except Exception:                    # noqa: BLE001
                pass
            conn = None
            _TLS.up = None
            if attempt:
                raise RuntimeError("上游连接失败")
    raise RuntimeError("上游无响应")


# 上游连接池：urllib 每次请求都重开 TLS，移动网络上一首歌能连带付好几次
# 握手钱（搜索 / 探测 / 取直链 / 歌词各一次）。HTTP/1.1 连接一次只借给一个
# 线程用，用完归还；被对端静默关掉的旧连接靠首次 retry 自愈。
_UP_IDLE = queue.LifoQueue(maxsize=8)
_UP_HEADERS = {
    "User-Agent": UA,
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "Referer": UPSTREAM,
    "Origin": UPSTREAM.rstrip("/"),
    "Accept": "application/json, text/plain, */*",
}


def _up_checkout():
    try:
        return _UP_IDLE.get_nowait()
    except queue.Empty:
        return None


def _up_checkin(conn):
    if conn is None:
        return
    try:
        _UP_IDLE.put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        except Exception:                    # noqa: BLE001
            pass


def upstream(act, params):
    body = urllib.parse.urlencode(
        {k: "" if v is None else v for k, v in (params or {}).items()}).encode()
    path = "/ajax.php?act=" + act
    for attempt in (0, 1):                   # 池里的旧连接可能已被对端关掉：换新重试一次
        conn = _up_checkout()
        if conn is None:
            conn = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=30)
        try:
            conn.request("POST", path, body=body, headers=_UP_HEADERS)
            r = conn.getresponse()
            raw, status = r.read(), r.status
        except (http.client.HTTPException, OSError, ValueError):
            try:
                conn.close()
            except Exception:                # noqa: BLE001
                pass
            if attempt:
                raise RuntimeError("上游连接失败")
            continue
        # HTTP 层已完整收包：即使业务报错，连接本身还能接着用
        _up_checkin(conn)
        if status != 200:
            raise RuntimeError(f"上游 HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise RuntimeError("上游返回不是 JSON")
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("msg") or f"接口错误 {payload.get('code')}")
        return payload.get("data")
    raise RuntimeError("上游无响应")


# --------------------------------------------------------------------------
# 第二通道（备用站）：只在主站失败/返回空时接手，目前仅覆盖网易云
# --------------------------------------------------------------------------
GD_BASE = "https://music-api.gdstudio.xyz/api.php"
STATION_NAME = {"hi": "主站", "gd": "备用站"}


def gd_rpc(types, **kw):
    qs = {"types": types}
    qs.update({k: v for k, v in kw.items() if v is not None})
    url = GD_BASE + "?" + urllib.parse.urlencode(qs)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://music.gdstudio.xyz/"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def gd_search(keyword, page=1, size=20):
    """备用站搜索，结构对齐主站。它不给时长和封面，先留空由探测定夺。"""
    arr = gd_rpc("search", source="netease", name=keyword, count=size, pages=page) or []
    if not isinstance(arr, list):
        return {"list": [], "total": None}
    items = []
    for it in arr:
        items.append({
            "id": str(it.get("id") or ""),
            "name": it.get("name") or "",
            "artist": " / ".join(it.get("artist") or []) if isinstance(it.get("artist"), list) else str(it.get("artist") or ""),
            "album_name": it.get("album") or "",
            "album_id": "",
            "duration": 0,
            "cover": "",
            "minfo": [{"format": "flac", "bitrate": "999"},
                      {"format": "mp3", "bitrate": "320"},
                      {"format": "mp3", "bitrate": "128"}],
            "time": None,
            "sign": None,
            "station": "gd",
        })
    return {"list": items, "total": None}


def search_any(platform, keyword, page=1, size=20, keepalive=False):
    """主站优先；主站报错或给空表时，网易云换备用站再试一次。

    keepalive=True 用于歌手页那种「一个词连翻几十页」的连续请求。
    """
    call = upstream_keepalive if keepalive else upstream
    try:
        data = call("search", {"platform": platform, "keyword": keyword,
                               "page": page, "size": size})
        lst = (data or {}).get("list") or []
        if lst:
            for it in lst:
                it["station"] = "hi"
            return data
    except Exception:  # noqa: BLE001
        lst = []
    if platform == "wyy":
        try:
            alt = gd_search(keyword, page, size)
            if alt.get("list"):
                return alt
        except Exception:  # noqa: BLE001
            pass
    return {"list": [], "total": None}


# --------------------------------------------------------------------------
# 跨源折叠：同一首歌在两个渠道各有一份时，只留一行，另一份塞进 alts
# --------------------------------------------------------------------------
# 副标题特征词：酷我的曲名常带「江南-《你很》电影片头曲」这种尾巴，
# 网易云只有「江南」。不剥掉尾巴就折不拢，会变成两行。
_DESC_HINT = (
    "主题曲", "主题歌", "片头曲", "片尾曲", "插曲", "印象曲", "推广曲", "宣传曲",
    "原声", "ost", "live", "现场", "演唱会", "伴奏", "instrumental", "翻唱",
    "cover", "remix", "demo", "版", "feat", "原唱", "演奏",
)
_PUNCT_RE = re.compile(r"[\s\u3000·・,，、。.!！?？'\"“”‘’()（）\[\]【】《》〈〉<>~\-_/&+*#:：;；|]+")


def _core_title(name):
    """曲名主干：只有尾巴更像「副标题 / 用途说明」时才切掉。"""
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    s = s.replace("（", "(").replace("）", ")")
    parts = re.split(r"\s*[-–—]\s*", s, maxsplit=1)
    if len(parts) == 2:
        head, tail = parts[0].strip(), parts[1].strip()
        low = tail.lower()
        if head and (tail.startswith("《") or any(h in low for h in _DESC_HINT)):
            s = head
    return s


def _fold_text(text):
    return _PUNCT_RE.sub("", unicodedata.normalize("NFKC", str(text or "")).lower())


def _main_artist(artist):
    """合唱署名取第一顺位：'林俊杰&五月天' 的主艺人是林俊杰。

    只用于折叠判定与专辑去重，不参与「记名歌手」归类，归类仍用完整署名。
    """
    s = str(artist or "")
    s = re.split(r"\s*(?:/|&|、|,|，|;|；|\bfeat\b|\bft\b|\bwith\b)\s*", s,
                 maxsplit=1, flags=re.I)[0]
    return _fold_text(s)


def fold_key(item):
    return (_fold_text(_core_title(item.get("name"))), _main_artist(item.get("artist")))


# 折叠后带进前端的字段：换源时要能直接播，sign/time 必须留着
_ALT_FIELDS = ("platform", "id", "name", "artist", "artist_id", "album_id",
               "album_name", "duration", "cover", "pic_url", "minfo", "time", "sign")


def fold_items(items, prefer="kuwo"):
    """同曲名 + 同主艺人 + 时长接近 → 折成一条，其余进 alts。

    代表条目优先取 prefer 源（云选的酷我），位置保持首次出现的名次，
    所以不重复的歌照旧互补，不会因为折叠被打乱顺序。
    """
    groups, index = [], {}
    for it in items:
        key = fold_key(it)
        dur = int(it.get("duration") or 0)
        hit = None
        for gi in index.get(key, ()):
            g = groups[gi]
            d0 = int(g["rep"].get("duration") or 0)
            if not dur or not d0 or abs(dur - d0) <= 4:
                hit = g
                break
        if hit is None:
            groups.append({"rep": it, "alts": []})
            index.setdefault(key, []).append(len(groups) - 1)
        else:
            hit["alts"].append(it)

    out = []
    for g in groups:
        rep, alts = g["rep"], g["alts"]
        same = [x for x in [rep] + alts if x.get("platform") == prefer]
        if same and same[0] is not rep:
            rep, alts = same[0], [x for x in [rep] + alts if x is not same[0]]
        if alts:
            rep = dict(rep)
            rep["alts"] = [{k: a.get(k) for k in _ALT_FIELDS} for a in alts]
        out.append(rep)
    return out


def _interleave(buckets):
    """各源轮流取一条。折叠前后都用它排，避免前半屏全是同一个来源。"""
    merged = []
    buckets = [list(b) for b in buckets]
    while any(buckets):
        for b in buckets:
            if b:
                merged.append(b.pop(0))
    return merged


def search_all(keyword, page=1, size=20, platforms=None):
    """多源聚合：并发向各平台各搜一遍，折叠重复项，不重复的互补。

    返回的每条都自带 platform；同一首在另一源也有时，另存 alts。
    """
    plats = [p for p in (platforms or PLATFORMS) if p in PLATFORMS]
    if not plats:
        plats = list(PLATFORMS)

    def one(p):
        try:
            d = search_any(p, keyword, page, size)
            return [normalize_item(i, p) for i in ((d or {}).get("list") or [])]
        except Exception:  # noqa: BLE001
            return []

    buckets = []
    with _cf.ThreadPoolExecutor(max_workers=len(plats)) as ex:
        for lst in ex.map(one, plats):
            buckets.append(list(lst))

    # 只要有一个源这一页还是满的，就说明后面还有
    has_more = any(len(b) >= int(size or 20) for b in buckets)
    merged = fold_items(_interleave(buckets))
    return {"list": merged, "total": len(merged), "has_more": has_more,
            "sources": {p: sum(1 for i in merged if i.get("platform") == p) for p in plats},
            "empty": [p for p, b in zip(plats, buckets) if not b]}



def gd_geturl(sid, fmt, br):
    want = str(br or "128")
    order = [want] + [x for x in ("999", "320", "128") if x != want]
    for b in order:
        try:
            j = gd_rpc("url", source="netease", id=sid, br=b)
            u = (j or {}).get("url")
            if u:
                return u
        except Exception:  # noqa: BLE001
            continue
    return None


def normalize_item(item, platform):
    out = dict(item or {})
    out["platform"] = platform
    out["id"] = str(out.get("id", ""))
    out["duration"] = int(out.get("duration") or 0)
    out["cover"] = out.get("pic_url") or ""
    minfo = out.get("minfo") or []
    for m in minfo:
        try:
            m["bitrate"] = str(m.get("bitrate"))
        except Exception:
            m["bitrate"] = "0"
    out["minfo"] = minfo
    return out


def normalize_list(data, platform):
    data = data or {}
    items = [normalize_item(i, platform) for i in (data.get("list") or [])]
    return {"list": items, "total": data.get("total"), "info": data.get("info")}


_RPC_CACHE = {}
_RPC_LOCK = threading.Lock()

# 歌手页深翻页：上限按「够全但不无限」定。酷我到 17 页以后基本只剩重复，
# 20 页 / 源在这台机器上实测 2~4 秒（长连接 + 3 线程并发翻页）。
ARTIST_PAGES = int(os.environ.get("ELYSIUM_ARTIST_PAGES") or 20)
ARTIST_MAX = 600
_PAGE_CACHE = {}
_PAGE_LOCK = threading.Lock()
_ARTIST_CACHE = {}
# 翻页专用线程池：线程不销毁，线程本地的那条长连接就能一直复用，
# 第二次打开歌手页不必再付一次 TLS 握手的钱。
_CRAWL_POOL = _cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="crawl")



def cached(key, ttl, fn):
    """同一请求 60 秒内只打一次上游；上游限流时也能顶住翻页。"""
    now = time.time()
    with _RPC_LOCK:
        hit = _RPC_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _RPC_LOCK:
        if len(_RPC_CACHE) > 400:
            for k in sorted(_RPC_CACHE, key=lambda k: _RPC_CACHE[k][0])[:200]:
                _RPC_CACHE.pop(k, None)
        _RPC_CACHE[key] = (now, val)
    return val


def _artist_key(name):
    """记名歌手归一化：只做等价折叠，绝不做包含匹配、不剥装饰后缀。

    「周杰伦♚」「周杰伦 (翻唱)」这类带后缀的名字与「周杰伦」不相等，
    不能被并进同一个歌手。
    """
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    return re.sub(r"\s+", " ", s).lower()


def _artist_page_cache(plat, name, page):
    """歌手页翻页缓存：慢在握手，同一页 10 分钟内不再打上游。"""
    key = (plat, name, page)
    now = time.time()
    with _PAGE_LOCK:
        hit = _PAGE_CACHE.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
    d = search_any(plat, name, page, 20, keepalive=True)
    lst = [normalize_item(i, plat) for i in ((d or {}).get("list") or [])]
    with _PAGE_LOCK:
        if len(_PAGE_CACHE) > 800:
            for k in sorted(_PAGE_CACHE, key=lambda k: _PAGE_CACHE[k][0])[:400]:
                _PAGE_CACHE.pop(k, None)
        _PAGE_CACHE[key] = (now, lst)
    return lst


def _crawl_artist(plat, name, max_pages, workers=3, cap=ARTIST_MAX):
    """把某个源上这位歌手的召回一路翻到底。

    老版本的 `if len(chunk) < 20: break` 是「只有几十首」的真凶 ——
    酷我经常某页只返 19 条，被当成末页，第 2~3 页就收工了。
    现在只认「整批都是空页」为枯竭。
    """
    ranked, seen = [], set()
    for start in range(1, max_pages + 1, workers):
        batch = list(range(start, min(start + workers, max_pages + 1)))
        futs = [(p, _CRAWL_POOL.submit(_artist_page_cache, plat, name, p)) for p in batch]
        empty = 0
        for _p, f in futs:
            try:
                lst = f.result()
            except Exception:  # noqa: BLE001
                lst = []
            if not lst:
                empty += 1
                continue
            for it in lst:
                k = str(it.get("id") or "")
                if not k or k in seen:
                    continue
                seen.add(k)
                ranked.append(it)
        if empty == len(batch):          # 这一批全空 → 已经翻到枯竭
            break
        if len(ranked) >= cap:
            break
    return ranked


def artist_page(params):
    """歌手页：只收录上游明确记名到该歌手的作品（口径：仅以记名歌手归）。

    上游搜索是「关键词相关性」召回：搜「周杰伦」会带出他作词作曲给别人
    唱的歌（蔡依林《布拉格广场》、李玟《刀马旦》）和综艺学员翻唱。这些歌
    的 artist / artist_id 是真实演唱者，必须按它归，不能因为搜索词命中就
    塞进周杰伦名下。

    判据优先级（按源各自判，跨源不做 ID 互串）：
      1) artist_id 精确相等（前端点歌手卡片时带上，最权威）
      2) 本平台内 artist 名严格相等者里出现最多的 artist_id
      3) 平台不给 artist_id 时，只认 artist 名严格相等
    任何情况下都不做「一条都不匹配就把整批结果全收」的兜底。

    曲目量：双源并发深翻页后合并，所以「总曲目」是抓全的量，不是首页
    那几十条。前端按滚动分批渲染，接口本身一次给全。
    """
    name = str(params.get("name") or params.get("keyword") or "").strip()
    if not name:
        raise ValueError("缺少歌手名")
    plat0 = params.get("platform") if params.get("platform") in PLATFORMS else "kuwo"
    max_pages = max(1, min(ARTIST_PAGES, int(params.get("pages") or ARTIST_PAGES)))
    wanted_aid = str(params.get("artist_id") or "").strip()
    cache_key = (name, plat0, wanted_aid, max_pages)
    now = time.time()
    with _PAGE_LOCK:
        hit = _ARTIST_CACHE.get(cache_key)
        if hit and now - hit[0] < 600:
            return hit[1]

    plats = [plat0] + [p for p in PLATFORMS if p != plat0]

    def aid_of(it):
        return str(it.get("artist_id") or "").strip()

    def pick(items, aid):
        """→ (采用的 artist_id, 只含该记名歌手的曲目)"""
        if not aid:
            exact = [it for it in items
                     if _artist_key(it.get("artist")) == _artist_key(name) and aid_of(it)]
            if exact:
                aid = collections.Counter(aid_of(it) for it in exact).most_common(1)[0][0]
        if aid:
            return aid, [it for it in items if aid_of(it) == aid]
        return "", [it for it in items
                    if _artist_key(it.get("artist")) == _artist_key(name)]

    with _cf.ThreadPoolExecutor(max_workers=len(plats)) as ex:
        crawled = dict(zip(plats, ex.map(
            lambda p: _crawl_artist(p, name, max_pages), plats)))

    buckets, aids, per_src = [], {}, {}
    for p in plats:
        aid, matched = pick(crawled.get(p) or [],
                            wanted_aid if p == plat0 else "")
        aids[p] = aid
        per_src[p] = len(matched)
        if matched:
            buckets.append(matched)

    songs = fold_items(_interleave(buckets))          # 折叠：同一首只留一行
    songs = songs[:ARTIST_MAX]

    # 专辑：按「专辑名 + 主艺人」去重，两个源各有一份时只留一张卡
    albums, seen = [], {}
    for it in _interleave(buckets):
        alb_id = str(it.get("album_id") or "")
        alb_name = it.get("album_name") or ""
        if not alb_id or not alb_name:
            continue
        key = (_fold_text(_core_title(alb_name)), _main_artist(it.get("artist")))
        if key in seen:
            card = seen[key]
            if it.get("platform") == plat0 and card.get("platform") != plat0:
                # 换成主源的 id，原来那个降为备选（打不开时兜底）
                card.setdefault("alts", []).append({"id": card.get("id"),
                                                    "platform": card.get("platform")})
                card.update({"id": alb_id, "platform": plat0,
                             "cover": it.get("cover") or card.get("cover") or ""})
            elif card.get("platform") == it.get("platform"):
                card.setdefault("alts", []).append({"id": alb_id,
                                                    "platform": it.get("platform")})
            continue
        card = {"id": alb_id, "name": alb_name,
                "cover": it.get("cover") or "",
                "artist": it.get("artist") or name,
                "platform": it.get("platform") or plat0, "kind": "album"}
        seen[key] = card
        albums.append(card)

    reasons = []
    if not songs:
        reasons.append("该歌手在两个来源都没有记名作品")
    elif not (crawled.get(plat0) or []):
        reasons.append(f"{'酷我' if plat0 == 'kuwo' else '网易云'}未收录该歌手，以下来自另一来源")

    data = {"name": name, "platform": plat0, "artist_id": aids.get(plat0, ""),
            "platforms": [p for p in plats if per_src.get(p)],
            "fallback": not per_src.get(plat0),
            "list": songs, "albums": albums, "total": len(songs),
            "count": len(songs), "per_source": per_src,
            "scanned": max_pages, "more": max_pages < ARTIST_PAGES,
            "reason": " · ".join(reasons)}
    with _PAGE_LOCK:
        _ARTIST_CACHE[cache_key] = (now, data)
        # 每项最多装 ARTIST_MAX 首歌 + 专辑卡，是所有缓存里最重的：不裁会一直涨。
        if len(_ARTIST_CACHE) > 24:
            for k in sorted(_ARTIST_CACHE, key=lambda k: _ARTIST_CACHE[k][0])[:12]:
                _ARTIST_CACHE.pop(k, None)
    return data



# --------------------------------------------------------------------------
# 歌词：K 歌 / 滚动歌词共用
# --------------------------------------------------------------------------
# 歌词不随播放变化，取过一次就够整场会话反复进出 K 歌用；不占 45 秒的搜索缓存。
_LYRIC_CACHE = {}                 # key -> (at, text)
_LYRIC_LOCK = threading.Lock()
LYRIC_TTL = 6 * 3600


def _lyric_cache_get(key):
    now = time.time()
    with _LYRIC_LOCK:
        hit = _LYRIC_CACHE.get(key)
        if hit and now - hit[0] < LYRIC_TTL:
            return hit[1]
    return None


def _lyric_cache_put(key, text):
    if not text:
        return                    # 空结果不缓存：歌名后来补了 / 上游抖一下还能再取
    with _LYRIC_LOCK:
        if len(_LYRIC_CACHE) > 300:
            for k in sorted(_LYRIC_CACHE, key=lambda k: _LYRIC_CACHE[k][0])[:100]:
                _LYRIC_CACHE.pop(k, None)
        _LYRIC_CACHE[key] = (time.time(), text)


def _lyric_candidate(plat, name, artist, sid=""):
    """按歌名 + 歌手找一个可签名条目。本地文件、老歌单条目都没有 time/sign，
    直接问 getLyric 会被上游回「接口错误 -3」——先找到同一首拿新签名。"""
    kw = " ".join(x for x in (name, artist) if x).strip()
    if not kw:
        return None
    data = upstream("search", {"platform": plat, "keyword": kw, "page": 1, "size": 10})
    cands = (data or {}).get("list") or []
    if sid:
        for c in cands:
            if str(c.get("id")) == sid:
                return c
    want = _fold_text(_core_title(name))
    if not want:
        return None
    for c in cands:
        if _fold_text(_core_title(c.get("name"))) != want:
            continue
        if artist:
            a = _fold_text(_main_artist(artist))
            ca = _fold_text(_main_artist(c.get("artist")))
            if a and ca and a != ca and a not in ca and ca not in a:
                continue
        return c
    return None


def lyric_lookup(params):
    """getLyric 的完整取法：签名齐全直取；缺签名（本地 / 旧条目）回源按歌名找。"""
    plat = params.get("platform") or "kuwo"
    if plat not in PLATFORMS:
        plat = "kuwo"
    sid = str(params.get("id") or "")
    st, sg = params.get("time"), params.get("sign")
    name = str(params.get("name") or "")
    artist = str(params.get("artist") or "")
    if sid and st and sg:
        key = ("lyric", plat, sid)
    else:
        key = ("lyric", plat, _fold_text(_core_title(name)),
               _fold_text(_main_artist(artist)))
    req_key = key                     # 本地条目按歌名缓存，避免每次都重搜一次
    hit = _lyric_cache_get(key)
    if hit is not None:
        return hit
    if not (sid and st and sg):
        cand = _lyric_candidate(plat, name, artist, sid)
        if not cand:
            return ""
        sid = str(cand.get("id") or sid)
        st, sg = cand.get("time"), cand.get("sign")
        key = ("lyric", plat, sid)
        hit = _lyric_cache_get(key)   # 同一首可能刚被另一个条目取过
        if hit is not None:
            _lyric_cache_put(req_key, hit)
            return hit
    if not (sid and st and sg):
        return ""
    text = upstream("getLyric", {"platform": plat, "songid": sid, "time": st, "sign": sg})
    if isinstance(text, dict):        # 个别源回 {"lrc": ...}：只认真正的文本
        text = text.get("lyric") or text.get("lrc") or ""
    text = text if isinstance(text, str) else ""
    _lyric_cache_put(key, text)
    if req_key != key:                # 歌名键也存一份：下次同一条目/同名片直接命中
        _lyric_cache_put(req_key, text)
    return text


RPC_TABLE = {
    "search": lambda p: cached(
        ("search", p.get("keyword", ""), p.get("page", 1), p.get("size", 20)), 45,
        lambda: search_all(p.get("keyword", ""), p.get("page", 1), p.get("size", 20))),
    "search_one": lambda p: normalize_list(
        cached(("search1", p.get("platform", "kuwo"), p.get("keyword", ""),
                p.get("page", 1), p.get("size", 20)), 45,
               lambda: search_any(p.get("platform", "kuwo"), p.get("keyword", ""),
                                  p.get("page", 1), p.get("size", 20))),
        p.get("platform", "kuwo")),
    "song": lambda p: cached(
        ("song", p.get("platform", "kuwo"), str(p.get("id") or "")), 600,
        lambda: normalize_list(
            upstream("song", {"platform": p.get("platform", "kuwo"),
                              "id": p.get("id")}),
            p.get("platform", "kuwo"))),
    "album": lambda p: normalize_list(
        cached(("album", p.get("platform", "kuwo"), p.get("id")), 300,
               lambda: upstream("album", {"platform": p.get("platform", "kuwo"),
                                          "id": p.get("id")})),
        p.get("platform", "kuwo")),
    "playlist": lambda p: normalize_list(
        upstream("playlist", {"platform": p.get("platform", "kuwo"),
                              "id": p.get("id"), "page": p.get("page", 1),
                              "size": p.get("size", 100)}),
        p.get("platform", "kuwo")),
    "artist": lambda p: artist_page(p),
    "geturl": lambda p: upstream("getUrl", {"platform": p.get("platform", "kuwo"),
                                            "songid": p.get("id"),
                                            "format": p.get("format"),
                                            "bitrate": p.get("bitrate"),
                                            "time": p.get("time"),
                                            "sign": p.get("sign")}),
    "getlyric": lambda p: lyric_lookup(p),
}


# --------------------------------------------------------------------------
# 口味画像 + 跨源推荐（全部本地，不依赖任何推荐服务）
# 借鉴 Spotify 客户端的「信号层 + 编排层」：显式反馈、口味画像、
# 按 seed 生成候选、预览后入列、可一键关闭。召回/排序不做（在服务端）。
# --------------------------------------------------------------------------
FEEDBACK_NEGATIVE = ("dislike", "exclude", "hide")


def _artist_of(obj):
    if not isinstance(obj, dict):
        return ""
    return re.sub(r"\s+", " ", str(obj.get("artist") or "")).strip()


def _billed_as(obj, name):
    """曲目的记名歌手里是否含 name —— 严格相等，不做子串包含。

    '周杰伦 / 温岚' 这类合唱算（署名里有他）；'周杰伦♚'、'周杰伦(翻唱)'
    这类带后缀/山寨名不算。仅作词作曲参与的歌，上游记名是真实演唱者，
    自然被排除。
    """
    parts = re.split(r"[/／&、,，;；]|\s+(?:feat\.?|ft\.?|with)\s+",
                     _artist_of(obj), flags=re.I)
    k = _artist_key(name)
    return bool(k) and any(_artist_key(p) == k for p in parts if p.strip())


def _play_counts_by_artist():
    """每位歌手的真实收听次数（来自 plays 记录，不是加权分）。

    之前画像页显示的数字是「收藏×3 + 最近播放×2 + 反馈±4」这种加权分，
    跟用户理解的「听了几遍」对不上。这里按 song_key 归到记名歌手，
    数 plays 里每天的收听次数 —— 换音质不重复计，只在听完时计。
    """
    plays = STORE.data.get("plays") or {}
    if not isinstance(plays, dict):
        plays = {}
    cnt, cover = {}, {}
    for rec in plays.values():
        if not isinstance(rec, dict):
            continue
        days = rec.get("days")
        if not isinstance(days, dict):
            continue
        n = sum(max(0, _safe_int(v)) for v in days.values())
        if n <= 0:
            continue
        meta = rec.get("meta")
        if not isinstance(meta, dict):
            continue
        name = _artist_of(meta)
        if not name:
            continue
        cnt[name] = cnt.get(name, 0) + n
        if not cover.get(name):
            cover[name] = meta.get("cover") or ""
    return cnt, cover


def taste_profile():
    """口味画像。

    两个口径严格分开，别再混：
      plays   —— 真实收听次数（听完才计，按歌不按音质）
      score   —— 排序用的亲和度（收藏/最近播放/显式反馈加权）
    页面上给用户看的必须是 plays，score 只用来排顺序。
    """
    data = STORE.data
    favs = [x for x in (data.get("favorites") or []) if isinstance(x, dict)]
    recent = [x for x in (data.get("recent") or []) if isinstance(x, dict)]
    fb = data.get("feedback") if isinstance(data.get("feedback"), dict) else {}
    score, sample = {}, {}

    def bump(name, w, it=None):
        n = str(name or "").strip()
        if not n:
            return
        score[n] = score.get(n, 0) + w
        if it and n not in sample:
            sample[n] = it.get("cover") or ""

    for f in favs:
        kind = f.get("kind") or "song"
        bump(f.get("name") if kind == "artist" else _artist_of(f), 3, f)
    for i, r in enumerate(recent[:30]):
        bump(_artist_of(r), 1 if i > 8 else 2, r)
    for v in fb.values():
        if not isinstance(v, dict):
            continue
        name = str(v.get("artist") or "").strip()
        if not name:
            continue
        if v.get("verdict") in FEEDBACK_NEGATIVE:
            score[name] = score.get(name, 0) - 4
        elif v.get("verdict") in ("like", "approve"):
            score[name] = score.get(name, 0) + 4

    plays_by_artist, play_cover = _play_counts_by_artist()

    # 出现在画像里的歌手 = 有收听记录 ∪ 有收藏 ∪ 有反馈
    names = set(plays_by_artist) | set(score)
    artists = []
    for n in names:
        sc = score.get(n, 0)
        pl = plays_by_artist.get(n, 0)
        if sc <= 0 and pl <= 0:
            continue
        artists.append({
            "name": n,
            "plays": pl,                     # 真实收听次数
            "score": sc,                     # 亲和度，仅用于排序
            "cover": play_cover.get(n) or sample.get(n, ""),
        })
    # 排序：先按真实收听次数，再按亲和度；都没听过但收藏过的排最后
    artists.sort(key=lambda a: (-a["plays"], -a["score"], a["name"]))

    total_plays = sum(plays_by_artist.values())
    distinct_songs = 0
    for v in (STORE.data.get("plays") or {}).values():
        if not isinstance(v, dict):
            continue
        days = v.get("days")
        if isinstance(days, dict) and sum(_safe_int(x) for x in days.values()) > 0:
            distinct_songs += 1

    return {
        "artists": artists,
        "excluded": [k for k, v in fb.items()
                     if isinstance(v, dict) and v.get("verdict") in FEEDBACK_NEGATIVE],
        "liked": [k for k, v in fb.items()
                  if isinstance(v, dict) and v.get("verdict") in ("like", "approve")],
        "listened": total_plays,            # 真实收听总次数
        "songs": distinct_songs,            # 听过的不同曲目数
        "favorites": len(favs),
        "enabled": bool(data["settings"].get("personalized", True)),
        "genres": list(data.get("taste_genres") or []),
    }


_RECO_CACHE = {"at": 0, "pool": None, "seeds": []}
_RECO_ROUND = 0


# --------------------------------------------------------------------------
# 风格分类（本地内置，不依赖任何外部分类服务）
#
# 上游搜索结果里没有 genre 字段（实测只有 id/name/artist/album/minfo 等），
# 所以风格标签只能自己建：每个风格带一组代表歌手，用歌手去上游检索，
# 命中后严格按记名歌手校验。这样「摇滚」卡片里出来的就是真唱摇滚的歌手的歌，
# 不是名字里带「摇滚」两个字的蹭流歌曲。
#
# 命名沿用 Apple Music / Spotify 的通用习惯（用户认得出来），
# 但只保留用户真正常听的中日 + 欧美主流风格，不铺几千条细分。
# 分类体系调研见 _art/STYLE_TAXONOMY.md
# --------------------------------------------------------------------------
GENRES = [
    # (id, 中文名, 英文名, 分组, 代表歌手)
    ("pop", "流行", "Pop", "流行",
     ["周杰伦", "蔡依林", "林俊杰", "陈奕迅", "Taylor Swift", "Ed Sheeran"]),
    ("citypop", "都市流行", "City Pop", "流行",
     ["竹内まりや", "山下達郎", "Michael Jackson", "陈淑桦"]),
    ("mandopop", "华语流行", "Mandopop", "流行",
     ["林俊杰", "薛之谦", "张惠妹", "李荣浩"]),
    ("rock", "摇滚", "Rock", "摇滚",
     ["五月天", "朴树", "窦唯", "Coldplay", "Oasis", "Radiohead"]),
    ("indierock", "独立摇滚", "Indie Rock", "摇滚",
     ["落日飞车", "痛仰", "万能青年旅店", "Phum Viphurit"]),
    ("punk", "朋克", "Punk", "摇滚",
     ["新裤子", "脑浊", "Ramones", "Sex Pistols"]),
    ("folk", "民谣", "Folk", "民谣与根源",
     ["赵雷", "陈粒", "马頔", "Bob Dylan", "Fleet Foxes"]),
    ("acoustic", "原声", "Acoustic", "民谣与根源",
     ["陈绮贞", "苏打绿", "Iron & Wine"]),
    ("country", "乡村", "Country", "民谣与根源",
     ["Taylor Swift", "陈淑桦", "Kacey Musgraves"]),
    ("electronic", "电子", "Electronic", "电子与舞曲",
     ["Daft Punk", "Kraftwerk", "陈粒", "Hyukoh"]),
    ("house", "浩室", "House", "电子与舞曲",
     ["Avicii", "Frank Ocean", "Peggy Gou"]),
    ("futurebass", "Future Bass", "Future Bass", "电子与舞曲",
     ["Hatsune Miku", "Kotoko", "m-flo"]),
    ("techno", "Techno", "Techno", "电子与舞曲",
     ["Charlotte de Witte", "Amelie Lens"]),
    ("dnb", "Drum & Bass", "Drum & Bass", "电子与舞曲",
     ["Netsky", "Noisia"]),
    ("hiphop", "嘻哈", "Hip-Hop", "说唱与嘻哈",
     ["法老", "Higher Brothers", "Kendrick Lamar", "Eminem"]),
    ("trap", "Trap", "Trap", "说唱与嘻哈",
     ["Travis Scott", "Future"]),
    ("rnb", "R&B", "R&B", "R&B 与灵魂",
     ["方大同", "陶喆", "The Weeknd", "SZA"]),
    ("soul", "灵魂乐", "Soul", "R&B 与灵魂",
     ["James Brown", "Aretha Franklin"]),
    ("funk", "放克", "Funk", "R&B 与灵魂",
     ["Jamiroquai", "Vulfpeck"]),
    ("metal", "金属", "Metal", "金属与朋克",
     ["Metallica", "林志炫", "Ghost", "Dimmu Borgir"]),
    ("hardrock", "硬摇滚", "Hard Rock", "金属与朋克",
     ["Guns N' Roses", "崔健"]),
    ("jazz", "爵士", "Jazz", "爵士与蓝调",
     ["Miles Davis", "Chet Baker", "小野丽莎"]),
    ("blues", "蓝调", "Blues", "爵士与蓝调",
     ["B.B. King", "Gary Clark Jr."]),
    ("classical", "古典", "Classical", "古典与轻音乐",
     ["久石让", "坂本龙一", "Ludovico Einaudi", "Yiruma"]),
    ("ambient", "氛围音乐", "Ambient", "古典与轻音乐",
     ["Sigur Rós", "Ólafur Arnalds", "Nujabes"]),
    ("piano", "纯钢琴", "Piano", "古典与轻音乐",
     ["Yiruma", "久石让", "Joe Hisaishi"]),
    ("game", "游戏音乐", "Game OST", "二次元与游戏",
     ["米津玄师", "Aimer", "Yoasobi", "澤野弘之"]),
    ("anime", "动画歌曲", "Anime", "二次元与游戏",
     ["LiSA", "Mrs. GREEN APPLE", "RADWIMPS", "宇多田ヒカル"]),
    ("jpop", "J-POP", "J-Pop", "二次元与游戏",
     ["米津玄师", "Aimer", "宇多田ヒカル", "藤井風", "Ado"]),
    ("kpop", "K-POP", "K-Pop", "二次元与游戏",
     ["NewJeans", "BTS", "aespa"]),
    ("vocaloid", "虚拟歌手", "Vocaloid", "二次元与游戏",
     ["初音ミク", "Hatsune Miku", "Kagamine Rin"]),
    ("guofeng", "国风", "Chinese Style", "国风与东方",
     ["周杰伦", "李荣浩", "毛不易"]),
    ("gufeng", "古风", "Guofeng", "国风与东方",
     ["河图", "银临", "HITA", "Finale"]),
    ("chinese", "中国风", "Chinese Traditional", "国风与东方",
     ["邓丽君", "费玉清", "周杰伦"]),
    ("world", "世界音乐", "World", "世界音乐",
     ["Of Monsters and Men", "Shovels & Rope"]),
    ("latin", "拉丁", "Latin", "世界音乐",
     ["Bad Bunny", "Daddy Yankee", "J Balvin"]),
    ("reggae", "雷鬼", "Reggae", "世界音乐",
     ["Bob Marley", "Toots and the Maroons"]),
    ("lofi", "Lo-Fi", "Lo-Fi", "电子与舞曲",
     ["Nujabes", "Tom Misch", "Idealism"]),
    ("chill", "Chill", "Chill", "电子与舞曲",
     ["Bonobo", "Khruangbin"]),
    ("soundtrack", "影视原声", "Soundtrack", "古典与轻音乐",
     ["久石让", "Hans Zimmer", "千と千尋の神話"]),
]


def genre_list():
    """前端拿风格清单用。分组顺序固定，方便 UI 折叠。"""
    order, out = [], []
    for gid, name, en, group, seeds in GENRES:
        gid = gid.strip()
        if group not in order:
            order.append(group)
        out.append({"id": gid, "name": name, "en": en, "group": group,
                    "seeds": list(seeds)})
    return {"groups": order, "genres": out}


def genre_by_id(gid):
    for g in genre_list()["genres"]:
        if g["id"] == gid:
            return g
    return None


def _day_seed():
    """当日确定性种子：同一天同一风格拿到同一批歌，跨天自动换批。

    不用随机数 —— 刷新页面就变一批的话，用户会以为推荐在乱跳。
    """
    day = time.strftime("%Y-%m-%d")
    return int(hashlib.md5(day.encode("utf-8")).hexdigest()[:8], 16)


def _style_weight():
    """每个风格当前的权重：用户在该风格下歌手的收听次数 + 用户手动选中的加成。"""
    plays_by_artist, _ = _play_counts_by_artist()
    picked = set(STORE.data.get("taste_genres") or [])
    w = {}
    for g in genre_list()["genres"]:
        n = sum(plays_by_artist.get(s, 0) for s in g["seeds"])
        if g["id"] in picked:
            n += 6          # 用户明确选过的风格，优先级抬高
        w[g["id"]] = n
    return w


# 全风格卡片的基础缓存：卡片元数据 + 每张卡的完整候选池（不含轮次窗口）。
# 「换一批」不重建这份东西，只从池子里重新切窗口 —— 所以换批是纯切片，
# 既不碰上游，也不重复跑去重/记名校验。
# empty_at：上游整体挂掉时记个时间，60 秒内换批不再重打（空结果不进 data 缓存，
# 否则上游恢复后的 30 分钟里用户拿到的还是空卡）。
_STYLE_BASE = {"at": 0, "data": None, "key": "", "empty_at": 0.0}
_STYLE_ROUND = 0
# 上游「按歌手搜一页」的结果缓存：seed -> (取回时间, 曲目列表)
_STYLE_SEEDS = {}
_STYLE_SEED_TTL = 900
# 空结果只挡 60 秒：上游抖一下不该让这个歌手被永久判死，
# 但也不能每次换批都为一个已经空了的种子再等一次超时。
_STYLE_SEED_EMPTY_TTL = 60
_STYLE_SEED_EMPTY = {}


def _seed_search(seed):
    """带缓存的上游检索。失败时退回上一次的成功结果，不把空列表写进长期缓存。"""
    now = time.time()
    hit = _STYLE_SEEDS.get(seed)
    if hit and now - hit[0] < _STYLE_SEED_TTL:
        return hit[1]
    miss = _STYLE_SEED_EMPTY.get(seed)
    if not hit and miss and now - miss < _STYLE_SEED_EMPTY_TTL:
        return []
    try:
        d = search_all(seed, 1, 20)
    except Exception:      # noqa: BLE001
        return hit[1] if hit else []
    lst = d.get("list") or []
    if lst:
        _STYLE_SEEDS[seed] = (now, lst)
        _STYLE_SEED_EMPTY.pop(seed, None)
    else:
        _STYLE_SEED_EMPTY[seed] = now
    return lst


def _window_of(pool, per, jitter=0, round_=0):
    """从候选池里切一段。纯本地运算，换批就靠它。"""
    if per <= 0:
        return pool
    if len(pool) <= per:
        # 池子还没装满一窗（小众风格经记名校验后常常只剩五六首）。
        # 这时从池子里取窗口永远是同一份 —— 用户点「换一批」会觉得按钮坏了。
        # 轮转一下顺序：歌就这么多是实话，但至少首曲和排列是新的。
        if round_ and len(pool) > 1:
            k = round_ % len(pool)
            pool = pool[k:] + pool[:k]
        return pool[:per]
    span = len(pool) - per + 1
    start = (jitter + round_ * per) % span
    return pool[start:start + per]


def style_playlists(limit=8, per=6, round_=0):
    """SP 式「全风格卡片歌单」。

    卡片 = 风格；卡片内 = 该风格下的真实曲目。
    排序：用户口味加权高的排前面，但每天轮换位次，
    保证「每天打开看到不同的风格组合」，同时同一天稳定不变。

    round_ 是「换一批」的轮次：卡片集合与候选池按「当日种子」定，
    换批只改每张卡窗口的起点 —— 缓存里存的是池子，不是切好的结果，
    所以连点十次「换一批」也只是十次切片，没有一次上游请求。
    """
    key = "%s|%s|%s" % (limit, per, _day_seed())
    now = time.time()
    b = _STYLE_BASE
    base = None
    if b["data"] is not None and b["key"] == key and now - b["at"] < 1800:
        base = b["data"]
    if base is None:
        if now - _STYLE_BASE.get("empty_at", 0.0) < 60:
            base = []                    # 刚刚整体取空过：60 秒内不重打上游
        else:
            w = _style_weight()
            seed = _day_seed()
            gs = genre_list()["genres"]

            # 轮换：用当日种子给每个风格算一个伪随机位次，
            # 权重高的整体往前挪一点，而不是硬按权重排死。
            scored = []
            for i, g in enumerate(gs):
                jitter = int(hashlib.md5(("%s|%s" % (seed, g["id"])).encode()).hexdigest()[:6], 16)
                scored.append((g, w.get(g["id"], 0), jitter))
            scored.sort(key=lambda x: (-x[1], x[2]))

            hot = [x for x in scored if x[1] > 0]
            cold = [x for x in scored if x[1] <= 0]
            order = hot + cold if hot else cold

            # 并发检索各风格的曲目：串行 8 张卡要 30s 以上。
            # 这里把所有 (风格, 歌手) 组合摊平后一次性并发取回，
            # 再按风格归组 —— 8 风格 × 3 seed = 24 个请求一起打，而不是 8×3 轮串行。
            import concurrent.futures as _cf
            head = order[:limit]

            jobs = []                       # (entry, seed)
            for entry in head:
                for s in _genre_seeds(entry[0]):
                    jobs.append((entry, s))

            fetched = {}

            def grab(job):
                entry, seed = job
                return job, _seed_search(seed)

            if jobs:
                with _cf.ThreadPoolExecutor(max_workers=12) as ex:
                    for job, lst in ex.map(grab, jobs):
                        fetched.setdefault(job[0][0]["id"], []).append((job[1], lst))

            base = []
            for entry in head:
                g, weight, jitter = entry
                # window=False：这里只攒候选池，切窗口留给下面统一做。
                pool = _assemble_genre(g, jitter, fetched.get(g["id"], []), per, 0,
                                       window=False)
                if not pool:
                    continue
                base.append({"genre": g["id"], "name": g["name"], "en": g["en"],
                             "group": g["group"], "weight": weight,
                             "jitter": jitter, "pool": pool})
            if base:
                _STYLE_BASE.update(at=now, data=base, key=key)
            else:
                _STYLE_BASE["empty_at"] = now

    # 换批只走到这里：池子已经在了，按轮次切一段即可。
    cards = []
    for c in base:
        cards.append({"genre": c["genre"], "name": c["name"], "en": c["en"],
                      "group": c["group"], "weight": c["weight"],
                      "list": _window_of(c["pool"], per, c["jitter"], round_)})
    return {"cards": cards, "day": time.strftime("%Y-%m-%d"),
            "genres": genre_list()["genres"], "round": round_}


def _genre_seeds(g):
    """该风格用于检索的歌手：风格自带代表歌手 + 用户手动选中时的偏好歌手。"""
    seeds = list(g["seeds"])
    if g["id"] in set(STORE.data.get("taste_genres") or []):
        plays_by_artist, _ = _play_counts_by_artist()
        top = sorted(plays_by_artist.items(), key=lambda x: -x[1])[:2]
        seeds = [s for s, _ in top] + seeds
    return seeds[:3]


def _assemble_genre(g, jitter, got, per, round_=0, window=True):
    """把各 seed 的检索结果合成一个风格歌单。

    严格按记名歌手校验（_billed_as），只认真唱这首歌的人；
    同平台 id 去重 + 同歌名同歌手去重（跨平台同名歌只留一条）。

    轮转取歌：每个 seed 轮流出一首，避免整张卡都是同一个人的歌
    （上游按歌手搜索，命中结果天然集中在该歌手身上）。
    """
    pairs = list(got)
    if not pairs:
        return []
    # 当日抖动：换一个 seed 起点，歌会换但风格不变
    off = jitter % len(pairs)
    pairs = pairs[off:] + pairs[:off]

    # 每个 seed 先过一遍记名校验与去重
    buckets = []
    for seed, lst in pairs:
        seen, titles, keep = set(), set(), []
        for it in lst:
            key = "%s:%s" % (it.get("platform"), it.get("id"))
            if key in seen or not _billed_as(it, seed):
                continue
            tk = "%s|%s" % (re.sub(r"\s+", "", it.get("name") or "").lower(),
                            re.sub(r"\s+", "", _artist_of(it)).lower())
            if tk in titles:
                continue
            seen.add(key)
            titles.add(tk)
            keep.append(it)
        buckets.append(keep)

    out, seen, seen_title = [], set(), set()
    i = 0
    # 先交错排到足够长（最多 per*3），再按轮次取一段窗口。
    # 只取 per 首的话「换一批」永远只能换出同样的前几首 —— 候选池明明有几十首。
    want = per if per <= 0 else per * 3
    while len(out) < want and any(buckets):
        b = buckets[i % len(buckets)]
        if b:
            it = b.pop(0)
            key = "%s:%s" % (it.get("platform"), it.get("id"))
            tk = "%s|%s" % (re.sub(r"\s+", "", it.get("name") or "").lower(),
                            re.sub(r"\s+", "", _artist_of(it)).lower())
            if key not in seen and tk not in seen_title:
                seen.add(key)
                seen_title.add(tk)
                out.append(it)
        i += 1
    if not window:
        return out                    # 完整候选池，调用方按轮次自己切
    if len(out) <= per or per <= 0:
        return out[:per] if per > 0 else out
    # 窗口起点同时受当日抖动与「换一批」轮次影响：同一天里每点一次都是新的组合，
    # 但都是本地切片，没有任何上游请求。
    start = (jitter + round_ * per) % (len(out) - per + 1)
    return out[start:start + per]


def _genre_items(g, per, jitter):
    """兼容入口：单风格取歌（供其它地方复用）。"""
    pairs = []
    for s in _genre_seeds(g):
        try:
            d = search_all(s, 1, 20)
        except Exception:      # noqa: BLE001
            continue
        pairs.append((s, d.get("list") or []))
    return _assemble_genre(g, jitter, pairs, per)


def _reco_page(prof, limit):
    """从候选池里切一页。

    池子存的是完整候选（不是某一页），所以「换一批」和「显示全部」
    都只是换窗口 —— 不发请求、不等上游，点下去立刻就有。
    """
    pool = _RECO_CACHE.get("pool") or []
    limit = max(1, limit)
    span = max(1, len(pool) - limit + 1)
    off = 0
    if len(pool) > limit:
        off = (_RECO_ROUND * limit) % span
    page = pool[off:off + limit]
    return {"list": page, "seeds": _RECO_CACHE.get("seeds") or [], "disabled": False,
            "profile": prof, "pool": len(pool), "round": _RECO_ROUND, "limit": limit}


def _explore_seeds(prof, plays_by_artist):
    """探索种子：风格代表歌手里挑「没听过的」，口味加权 + 当日轮换。

    familiar seed 永远是听过的歌手，推荐会越收越窄 —— 这里保证每天的
    候选里都掺进没听过、但风格上顺路的人（探索配额的来源）。
    同一天拿到同一批（当日种子），跨天自动换人。
    """
    known = set(plays_by_artist) | {a["name"] for a in prof["artists"]}
    for r in (STORE.data.get("recent") or [])[:40]:
        n = _artist_of(r)
        if n:
            known.add(n)
    w = _style_weight()
    day = _day_seed()
    scored = []
    for g in genre_list()["genres"]:
        gw = w.get(g["id"], 0)
        for i, s in enumerate(g["seeds"]):
            if s in known:
                continue
            jitter = int(hashlib.md5(("%s|%s" % (day, s)).encode()).hexdigest()[:6], 16)
            scored.append((gw * 2 - i, jitter, s))     # 口味加权 + 代表度 + 当日抖动
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for _sc, _j, s in scored:
        if s not in out:
            out.append(s)
        if len(out) >= 2:
            break
    return out


def recommend_candidates(limit=12, fresh=False):
    """用 top 歌手当 seed，跨源搜相似曲目；排除已收藏/已排除/最近听过。

    候选池只建一份（不按 limit 分开建），首页 12 条和「显示全部」26 条
    都从同一个池子里切 —— 否则点展开会命中 12 条的缓存，看着像按钮失灵。

    fresh=True 是「换一批」：池子里还有没露过面的候选就立刻换一页，
    只有池子本身不够长时才真的回上游。之前是无条件清缓存重取 ——
    用户点完盯着不动的页面等十几秒，这就是「加载很缓慢」的来源。
    """
    now = time.time()
    c = _RECO_CACHE
    global _RECO_ROUND
    prof = taste_profile()
    if not prof["enabled"]:
        return {"list": [], "seeds": [], "disabled": True, "profile": prof}

    pool = c.get("pool")
    pool_ok = bool(pool) and now - c.get("at", 0) < 300
    if pool_ok and len(pool) > limit:
        if fresh:
            _RECO_ROUND += 1
        return _reco_page(prof, limit)

    seeds = [a["name"] for a in prof["artists"][:4]]
    if not seeds:                       # 冷启动：退回最近播放里的歌手
        seeds = list(dict.fromkeys(
            _artist_of(r) for r in (STORE.data.get("recent") or []) if _artist_of(r)))[:3]

    # 探索种子（防信息茧房）：没听过的风格代表歌手，掺进候选池。
    # 冷启动（连 recent 都没有）时不掺 —— 没有任何口味依据时，
    # 先让人听出画像再谈探索。
    plays_by_artist, _ = _play_counts_by_artist()
    explore = _explore_seeds(prof, plays_by_artist) if seeds else []
    exp_set = set(explore)

    banned = set(prof["excluded"])
    seen = set(banned)
    for r in (STORE.data.get("recent") or [])[:40]:
        seen.add("%s:%s" % (r.get("platform"), r.get("id")))
    for f in (STORE.data.get("favorites") or []):
        if isinstance(f, dict) and (f.get("kind") or "song") == "song":
            seen.add("%s:%s" % (f.get("platform"), f.get("id")))

    fam_pool, exp_pool, seen_titles = [], [], set()
    target = max(48, limit * 2)         # 池子要够长，换批才有东西可换

    def title_key(name, artist):
        n = str(name or "")
        n = re.sub(r"[-－]\s*《[^》]*》.*$", "", n)          # 去掉 -《电影》片尾曲 之类
        n = re.sub(r"[（(][^）)]*(电影|电视剧|主题曲|片尾曲|插曲)[^）)]*[）)]", "", n)
        return re.sub(r"\s+", "", n).lower() + "|" + re.sub(r"\s+", "", artist or "").lower()

    for s in seeds[:4] + explore:
        try:
            d = _seed_search(s)
        except Exception:  # noqa: BLE001
            continue
        for it in (d or []):
            key = "%s:%s" % (it.get("platform"), it.get("id"))
            if key in seen:
                continue
            a = re.sub(r"\s+", "", _artist_of(it))
            if not _billed_as(it, s):      # 只认记名歌手，别把相关性召回的歌混进来
                continue
            # 同一首歌在两个平台各来一条 → 只留一条，避免列表看着乱
            tk = title_key(it.get("name"), a)
            if tk in seen_titles:
                continue
            seen_titles.add(tk)
            it["reco_seed"] = s
            (exp_pool if s in exp_set else fam_pool).append(it)
            seen.add(key)
            if len(fam_pool) + len(exp_pool) >= target:
                break
        if len(fam_pool) + len(exp_pool) >= target:
            break

    # 交错合并：探索候选按 ~30%（7 熟悉配 3 探索）均匀混进池子，
    # 而不是排成「前段全探索、后段全熟悉」两截。探索池天然偏小
    # （每个探索 seed 只有一页检索），能给多少给多少，不硬凑。
    out, fi, ei = [], 0, 0
    while len(out) < target and (fi < len(fam_pool) or ei < len(exp_pool)):
        for _ in range(7):
            if fi < len(fam_pool) and len(out) < target:
                out.append(fam_pool[fi])
                fi += 1
        for _ in range(3):
            if ei < len(exp_pool) and len(out) < target:
                out.append(exp_pool[ei])
                ei += 1
        if fi >= len(fam_pool) and ei >= len(exp_pool):
            break

    _RECO_CACHE.update(at=now, pool=out, seeds=seeds + explore)
    return _reco_page(prof, limit)


# --------------------------------------------------------------------------
# 音质挑选（含降级顺序）
# --------------------------------------------------------------------------
def _ibr(m):
    try:
        return int(m.get("bitrate") or 0)
    except (TypeError, ValueError):
        return 0


def quality_candidates(item, want):
    """返回按优先级排列的候选音质列表；目标缺失时依次降级。

    want 兼容两种写法：设置里的「320」和音质菜单传来的「mp3:320」/「flac:999」。
    之前菜单的 format:bitrate 写法不匹配任何分支，落进 else 变成 FLAC 优先 ——
    用户显式选 MP3 却下回无损，体积差 3~5 倍。
    """
    want = str(want or "").strip().lower()
    if ":" in want:                      # 前端音质菜单的 format:bitrate 写法
        fmt, _, br = want.partition(":")
        if fmt in ("mp3", "flac"):
            want = br
    minfo = item.get("minfo") or []
    if not minfo:
        # 歌单条目落盘时裁掉了 minfo（pl_item_of 只留展示字段）：按标准档位给一条
        # 降级顺序兜底，getUrl 解析不出的档位会在 _resolve 里自动落到下一个。
        synth = [{"format": "flac", "bitrate": "2000"},
                 {"format": "mp3", "bitrate": "320"},
                 {"format": "mp3", "bitrate": "128"}]
        if want in ("320", "128"):
            n = int(want)
            synth.sort(key=lambda m: (m["format"] != "mp3", abs(int(m["bitrate"]) - n)))
        return synth
    flacs = sorted((m for m in minfo if m.get("format") == "flac"), key=_ibr, reverse=True)
    mp3s = sorted((m for m in minfo if m.get("format") == "mp3"), key=_ibr, reverse=True)

    if want in (None, "", "best", "flac"):
        first = flacs or mp3s
    elif want in ("2000", "646"):
        hit = [m for m in flacs if _ibr(m) == int(want)]
        first = hit or flacs or mp3s
    elif want in ("320", "128"):
        hit = [m for m in mp3s if _ibr(m) == int(want)]
        first = hit or [m for m in mp3s if _ibr(m) <= int(want)] or mp3s or flacs
    else:
        first = flacs or mp3s

    ordered, seen = [], set()
    for m in list(first) + flacs + mp3s:
        key = (m.get("format"), str(m.get("bitrate")))
        if key not in seen:
            seen.add(key)
            ordered.append(m)
    return ordered


def pick_quality(item, want):
    cands = quality_candidates(item, want)
    return cands[0] if cands else None


def sniff_ext(url, fallback="bin"):
    """接口标的 format 常常不准（128k 给 m4a），靠 magic 定后缀。
    判定统一走 sniff_container，别维护两份魔数表。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-15"})
        with urllib.request.urlopen(req, timeout=20) as r:
            head = r.read(16)
    except Exception:
        return fallback
    fmt, _ = sniff_container(head)
    return fmt if fmt and fmt != "bin" else fallback


_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                 | {"COM%d" % i for i in range(1, 10)}
                 | {"LPT%d" % i for i in range(1, 10)})


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s or "")).strip(" .")
    s = s[:150] or "unknown"
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s            # 设备名不能当文件名，加个前缀就落得下去
    return s


def render_template(tpl, item, fmt="", bitrate=""):
    text = tpl or "{name} - {artist}"
    values = {
        "name": item.get("name") or "",
        "artist": item.get("artist") or "",
        "album": item.get("album_name") or "",
        "platform": item.get("platform") or "",
        "format": fmt or "",
        "bitrate": str(bitrate or ""),
        "id": str(item.get("id") or ""),
    }
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    text = re.sub(r"\s*-\s*$", "", text)
    return safe_name(text.strip(" -"))


def target_path(folder, base, ext, policy):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{base}.{ext}")
    if not os.path.exists(path):
        return path
    if policy == "overwrite":
        return path
    if policy == "skip":
        return None
    i = 2
    while os.path.exists(os.path.join(folder, f"{base} ({i}).{ext}")):
        i += 1
    return os.path.join(folder, f"{base} ({i}).{ext}")


# --------------------------------------------------------------------------
# 标签写入
# --------------------------------------------------------------------------
def fetch_bytes(url, limit=4 * 1024 * 1024):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": UPSTREAM})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(limit)
        return data or None
    except Exception:
        return None


# --------------------------------------------------------------------------
# 试听缓存
# --------------------------------------------------------------------------
AUDIO_MIME = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "bin": "application/octet-stream",
}


class MediaCache:
    """两层试听缓存。

    today/：当天听满 N 次的歌，跨天清空。
    long/ ：最近 7 天听满 5 次以上的歌，3 天没再听就自动删。
    """

    def __init__(self, root):
        self.root = root
        self.today_root = os.path.join(root, "today")
        self.long_root = os.path.join(root, "long")
        self.index_path = os.path.join(self.long_root, "index.json")
        os.makedirs(self.today_root, exist_ok=True)
        os.makedirs(self.long_root, exist_ok=True)
        self.lock = threading.RLock()
        self.warming = set()
        self._day = None
        # 缓存维护节流：evict/enforce 都要全量扫盘（读 index / walk 两棵树），
        # 之前每次听完、每首歌下完都跑一遍，2GB 小文件在 HDD 上是几千次 stat。
        self._last_evict = 0.0
        self._last_enforce = 0.0
        self._purge_legacy()
        self.roll()
        self.evict_long()

    def _purge_legacy(self):
        """清掉旧版平铺目录（cache/<日期>/ 或 cache/ku/）。"""
        for name in list(os.listdir(self.root)):
            if name in ("today", "long"):
                continue
            full = os.path.join(self.root, name)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                try:
                    os.remove(full)
                except OSError:
                    pass

    # -- 天目录 ---------------------------------------------------------
    def roll(self):
        day = time.strftime("%Y-%m-%d")
        if day == self._day:
            return day
        with self.lock:
            self._day = day
            os.makedirs(os.path.join(self.today_root, day), exist_ok=True)
        for name in list(os.listdir(self.today_root)):
            full = os.path.join(self.today_root, name)
            if os.path.isdir(full) and name != day:
                shutil.rmtree(full, ignore_errors=True)
        self._clean_parts(self.today_root)
        return day

    @staticmethod
    def _clean_parts(base):
        for dirpath, _, files in os.walk(base):
            for f in files:
                if f.endswith(".part"):
                    try:
                        os.remove(os.path.join(dirpath, f))
                    except OSError:
                        pass

    @staticmethod
    def safe_key(raw):
        return re.sub(r"[^A-Za-z0-9_-]", "_", str(raw))[:120] or "x"

    @staticmethod
    def key_of(url):
        """没有 play_key 时（/api/media 直接给 url）的兜底身份：直链的散列。"""
        return hashlib.md5(str(url).encode("utf-8")).hexdigest()

    @staticmethod
    def _ext(ext):
        return re.sub(r"[^a-z0-9]", "", (ext or "bin").lower())[:5] or "bin"

    def path_today(self, key, ext):
        k = self.safe_key(key)
        return os.path.join(self.today_root, self.roll(), k[:2], k + "." + self._ext(ext))

    def path_long(self, key, ext):
        k = self.safe_key(key)
        return os.path.join(self.long_root, k[:2], k + "." + self._ext(ext))

    # -- 长时缓存索引 ---------------------------------------------------
    def load_index(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def save_index(self, idx):
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.index_path)

    def _scan(self, base, key):
        k = self.safe_key(key)
        d = os.path.join(base, k[:2])
        if not os.path.isdir(d):
            return None
        for name in os.listdir(d):
            if name.startswith(k + ".") and not name.endswith(".part"):
                p = os.path.join(d, name)
                if os.path.isfile(p) and os.path.getsize(p) > 1024:
                    return p
        return None

    def find(self, key, tier="any"):
        if tier in ("any", "today"):
            p = self._scan(os.path.join(self.today_root, self.roll()), key)
            if p:
                try:
                    os.utime(p, None)
                except OSError:
                    pass
                return p
            if tier == "today":
                return None
        p = self._scan(self.long_root, key)
        if p:
            self.touch_long(key)          # 长期缓存被用到就续命
        return p

    def touch_long(self, key):
        with self.lock:
            idx = self.load_index()
            if key in idx:
                idx[key]["last_at"] = int(time.time())
                self.save_index(idx)

    def evict_long(self, days=3):
        """长期缓存：超过 N 天没听过就删掉。每次听完都会被调，60 秒内不重复扫。"""
        now = time.time()
        if now - self._last_evict < 60:
            return 0
        self._last_evict = now
        cutoff = time.time() - days * 86400
        removed = 0
        with self.lock:
            idx = self.load_index()
            for k in [k for k, v in idx.items() if int(v.get("last_at") or 0) < cutoff]:
                f = idx[k].get("file") or self._scan(self.long_root, k)
                if f:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                idx.pop(k, None)
                removed += 1
            if removed:
                self.save_index(idx)
        self._clean_parts(self.long_root)
        return removed

    # -- 统计 -----------------------------------------------------------
    def _sum(self, base):
        total = count = 0
        for dirpath, _, files in os.walk(base):
            for f in files:
                if f.endswith((".part", ".json")):
                    continue
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                    count += 1
                except OSError:
                    pass
        return total, count

    def stat(self):
        t_size, t_count = self._sum(os.path.join(self.today_root, self.roll()))
        l_size, l_count = self._sum(self.long_root)
        return {"size": t_size + l_size, "count": t_count + l_count,
                "today_size": t_size, "today_count": t_count,
                "long_size": l_size, "long_count": l_count}

    def enforce(self, limit_mb):
        """缓存体积超限时按最久未用淘汰。每首歌下完都会被调，120 秒内不重复扫。"""
        now = time.time()
        if now - self._last_enforce < 120:
            return
        self._last_enforce = now
        limit = max(64, int(limit_mb or 2048)) * 1024 * 1024
        st = self.stat()
        if st["size"] <= limit:
            return
        entries = []
        for base in (self.long_root, os.path.join(self.today_root, self.roll())):
            for dirpath, _, files in os.walk(base):
                for f in files:
                    if f.endswith((".part", ".json")):
                        continue
                    p = os.path.join(dirpath, f)
                    try:
                        entries.append((os.path.getmtime(p), os.path.getsize(p), p))
                    except OSError:
                        pass
        entries.sort()
        total = st["size"]
        for _, size, p in entries:
            if total <= limit:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass

    def clear(self, tier="all"):
        if tier in ("all", "today"):
            shutil.rmtree(os.path.join(self.today_root, self.roll()), ignore_errors=True)
            os.makedirs(os.path.join(self.today_root, self.roll()), exist_ok=True)
        if tier in ("all", "long"):
            for name in list(os.listdir(self.long_root)):
                full = os.path.join(self.long_root, name)
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
            try:
                os.remove(self.index_path)
            except OSError:
                pass
        return self.stat()

    # -- 落盘 -----------------------------------------------------------
    def warm(self, key, url, ext, tier="today", meta=None):
        key = self.safe_key(key)
        if self.find(key, tier) or key in self.warming:
            return False
        # 已经在另一层存着：直接挪过去，不重复下载
        if tier == "long":
            src = self._scan(os.path.join(self.today_root, self.roll()), key)
            if src:
                dst = self.path_long(key, os.path.splitext(src)[1].lstrip("."))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    os.replace(src, dst)
                    self._remember_long(key, dst, meta)
                    return True
                except OSError:
                    pass
        self.warming.add(key)

        def run():
            tmp = (self.path_long(key, ext) if tier == "long" else self.path_today(key, ext)) + ".part"
            final = tmp[:-5]
            try:
                os.makedirs(os.path.dirname(tmp), exist_ok=True)
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": UPSTREAM})
                with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                os.replace(tmp, final)
                if tier == "long":
                    self._remember_long(key, final, meta)
                self.enforce(STORE.data["settings"].get("cache_limit_mb", 2048))
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            finally:
                self.warming.discard(key)

        threading.Thread(target=run, daemon=True).start()
        return True

    def _remember_long(self, key, path, meta=None):
        with self.lock:
            idx = self.load_index()
            idx[key] = {"file": path, "last_at": int(time.time()), "meta": meta or {}}
            self.save_index(idx)


CACHE = MediaCache(os.path.join(data_dir(), "cache"))


def resolve_play_url(platform, sid, fmt, br, sign_time=None, sign=None, name=None):
    """拿到可播放直链。主站签名过期就重搜换签；主站整条挂掉时网易云转备用站。

    同一条链接 3 分钟内复用：探测 / 起播 / K 歌分析会对同一首歌各要一次直链，
    每次重走一遍上游只会把首播拖慢（签名链接本身有二十来分钟寿命，3 分钟安全）。
    """
    key = ("url", platform, str(sid), str(fmt), str(br),
           str(sign_time or ""), str(sign or ""), str(name or ""))
    return cached(key, 180, lambda: _resolve_play_url(
        platform, sid, fmt, br, sign_time, sign, name))


def _resolve_play_url(platform, sid, fmt, br, sign_time=None, sign=None, name=None):
    def call(t, s):
        data = upstream("getUrl", {"platform": platform, "songid": sid, "format": fmt,
                                   "bitrate": br, "time": t, "sign": s})
        return (data or {}).get("url")

    def backup():
        if platform == "wyy":
            return gd_geturl(sid, fmt, br)
        return None

    try:
        url = call(sign_time, sign)
        if url:
            return url
    except Exception as e:  # noqa: BLE001
        if "过期" not in str(e):
            alt = backup()
            if alt:
                return alt
            raise

    if name:
        try:
            data = upstream("search", {"platform": platform, "keyword": name, "page": 1, "size": 20})
            for cand in (data or {}).get("list") or []:
                if str(cand.get("id")) == str(sid):
                    url = call(cand.get("time"), cand.get("sign"))
                    if url:
                        return url
        except Exception:  # noqa: BLE001
            pass

    alt = backup()
    if alt:
        return alt
    raise RuntimeError("解析直链失败")


# --------------------------------------------------------------------------
# 音质实测：不信上游声明的 format，只信真实字节
# --------------------------------------------------------------------------
_MAGIC = (
    (b"fLaC", "flac", "FLAC"),
    (b"OggS", "ogg", "OGG"),
    (b"RIFF", "wav", "WAV"),
    (b"ID3", "mp3", "MP3"),
    (b"\xff\xfb", "mp3", "MP3"),
    (b"\xff\xf3", "mp3", "MP3"),
    (b"\xff\xf2", "mp3", "MP3"),
)


def sniff_container(head):
    """按文件头判容器。fLaC 才算真无损，别的都不许标 FLAC。"""
    if not head:
        return "", ""
    for sig, fmt, label in _MAGIC:
        if head.startswith(sig):
            return fmt, label
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a", "M4A"
    if head.startswith(b"\xff\xf1") or head.startswith(b"\xff\xf9"):
        return "aac", "AAC"
    return "bin", "未知"


def parse_flac_streaminfo(buf):
    """从 FLAC 文件头部小段字节解 STREAMINFO：采样率 / 位深 / 声道 / 总样本数。

    buf 是 Range 探测拉回的头部，可能带 ID3v2 前缀。STREAMINFO 恒为第一个
    metadata block（块头 4B + 内容 34B），内容第 10~17 字节按位打包：
    采样率 20 / 声道 3（存 -1）/ 位深 5（存 -1）/ 总样本数 36。
    找不到合法结构返回 None —— 上游标称 flac 的可能是贴了魔数的假文件，
    Hi-Res 徽标宁可少报不虚报。
    """
    if not buf:
        return None
    off = 0
    if buf[:3] == b"ID3":
        if len(buf) < 10:
            return None
        # ID3v2 头尾部 4 字节是 syncsafe 大端长度（每字节只用低 7 位）
        size = ((buf[6] & 0x7F) << 21) | ((buf[7] & 0x7F) << 14) \
               | ((buf[8] & 0x7F) << 7) | (buf[9] & 0x7F)
        off = 10 + size
    if buf[off:off + 4] != b"fLaC":
        return None
    hdr = buf[off + 4:off + 8]          # 块头：1B（bit7=末块标志，低 7 位=类型）+ 3B 内容长度
    if len(hdr) < 4 or hdr[0] & 0x7F or int.from_bytes(hdr[1:4], "big") < 34:
        return None
    b = buf[off + 8:off + 8 + 34]
    if len(b) < 34:
        return None
    # 位打包从 b[10] 起：采样率 20 位（b10~b12 右移 4）、声道 3 位（b12 中段）、
    # 位深 5 位（b12 末位 + b13 高半）、总样本 36 位（b13 低半 + b14~b17）
    sample_rate = int.from_bytes(b[10:13], "big") >> 4
    channels = ((b[12] >> 1) & 0x07) + 1
    bit_depth = ((b[12] & 0x01) << 4 | (b[13] >> 4)) + 1
    total = ((b[13] & 0x0F) << 32) | int.from_bytes(b[14:18], "big")
    # 合理性硬墙：魔数后面跟一段垃圾时给出的是天文数字，别让它流出去
    if not (8000 <= sample_rate <= 192000) or not (1 <= channels <= 8) \
            or not (4 <= bit_depth <= 32):
        return None
    out = {"sample_rate": sample_rate, "bit_depth": bit_depth,
           "channels": channels, "total_samples": total}
    if total and sample_rate:
        out["duration"] = round(total / sample_rate, 3)
    return out


def _flac_hires(url, head):
    """对容器判定为 FLAC 的曲目尽力解 Hi-Res 元信息，最多两次几百字节请求。

    ID3v2 标签很大时 256B 探头够不到 fLaC，按标签尺寸补一个定向 Range
    直接拉 STREAMINFO 所在的那一小段再解一次。
    """
    hires = parse_flac_streaminfo(head)
    if hires or head[:3] != b"ID3" or len(head) < 10:
        return hires
    size = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) \
           | ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
    start = 10 + size
    if start > 16 * 1024 * 1024:        # 荒谬的标签尺寸当垃圾处理，不再追
        return None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Range": "bytes=%d-%d" % (start, start + 47),
            "Referer": UPSTREAM, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=12) as r:
            extra = r.read(48)
    except Exception:  # noqa: BLE001
        return None
    return parse_flac_streaminfo(extra)


_PROBE_CACHE = {}
_PROBE_LOCK = threading.Lock()
# 探测专用常驻池：每批新建/销毁 8 个线程纯属浪费，线程留着还能白捡 HTTP keep-alive
_PROBE_POOL = _cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")


def probe_one(platform, sid, fmt, br, sign_time=None, sign=None, duration=0, name=None):
    """探一首歌：拉 256B 头判真格式 + 读 Content-Range 拿大小算真实码率。

    返回 valid/fmt/label/size/kbps/container（FLAC 另带 hires 元信息），
    全程只消耗几百字节。
    """
    key = ("%s|%s|%s|%s" % (platform, sid, fmt, br))
    now = time.time()
    with _PROBE_LOCK:
        hit = _PROBE_CACHE.get(key)
        if hit and now - hit[0] < 300:
            return hit[1]
    out = {"valid": False, "fmt": "", "label": "", "size": 0, "kbps": 0,
           "container": "", "declared": str(fmt or "").lower(), "reason": "",
           "hires": None}
    try:
        url = resolve_play_url(platform, sid, fmt, br, sign_time, sign, name)
    except Exception as e:  # noqa: BLE001
        out["reason"] = "解析失败: %s" % e
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = (now, out)
        return out
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Range": "bytes=0-255", "Referer": UPSTREAM,
            "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=12) as r:
            head = r.read(256)
            cr = r.headers.get("Content-Range") or ""
            size = 0
            if "/" in cr:
                try:
                    size = int(cr.rsplit("/", 1)[1])
                except Exception:
                    size = 0
            if not size:
                try:
                    size = int(r.headers.get("Content-Length") or 0)
                except Exception:
                    size = 0
            if r.status in (200, 206) and head:
                out["valid"] = True
                out["size"] = size
                fmt2, label = sniff_container(head)
                out["fmt"], out["label"] = fmt2, label
                out["container"] = fmt2
                if size and duration:
                    try:
                        out["kbps"] = int(round(size * 8 / float(duration) / 1000))
                    except Exception:
                        out["kbps"] = 0
                out["mismatch"] = (out["declared"] == "flac" and fmt2 not in ("flac", ""))
                if fmt2 == "flac":
                    out["hires"] = _flac_hires(url, head)
    except Exception as e:  # noqa: BLE001
        out["reason"] = "不可达: %s" % e
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = (now, out)
        if len(_PROBE_CACHE) > 800:
            for k in sorted(_PROBE_CACHE, key=lambda k: _PROBE_CACHE[k][0])[:200]:
                _PROBE_CACHE.pop(k, None)
    return out


def probe_batch(items, limit=64, workers=8, deadline=20.0):
    """批量探测：并发跑，分片由前端控制（一次 12 条），别让整页等太久。

    带一个软截止：上游偶发挂住时，一批里只要有一条卡到超时，ex.map 就会
    拖着整批不回，前端的格子永远停在「检测中」。所以按完成顺序收，
    到点就返回已拿到的，没赶上的标成待测（前端有对应静态态）。
    """
    todo = []
    for it in (items or [])[:limit]:
        plat, sid = it.get("platform"), it.get("id")
        if not plat or not sid:
            continue
        q = it.get("_probeq") or {}
        todo.append((it, plat, sid, q.get("format") or "mp3", str(q.get("bitrate") or "320"),
                     it.get("time"), it.get("sign"), int(it.get("duration") or 0),
                     it.get("name")))
    out = {}
    if not todo:
        return out

    def run(t):
        it, plat, sid, fmt, br, st, sg, dur, nm = t
        res = probe_one(plat, sid, fmt, br, st, sg, dur, nm)
        res["key"] = "%s|%s|%s|%s" % (plat, sid, fmt, br)
        return (plat, str(sid), res)

    ex = _PROBE_POOL
    futs = {ex.submit(run, t): (t[1], str(t[2])) for t in todo}
    try:
        for fut in _cf.as_completed(futs, timeout=deadline):
            try:
                plat, sid, res = fut.result()
            except Exception:  # noqa: BLE001
                continue
            out["%s:%s" % (plat, sid)] = res
    except _cf.TimeoutError:
        for fut, (plat, sid) in futs.items():
            if not fut.done():
                fut.cancel()
                out["%s:%s" % (plat, sid)] = {
                    "valid": False, "unresolved": True, "reason": "探测超时，可稍后重试",
                    "fmt": "", "label": "", "size": 0, "kbps": 0, "container": "",
                    "declared": "", "mismatch": False, "hires": None}
    # 没进结果的（请求异常 / 被取消）也补一个待测态，别留空格子
    for t in todo:
        k = "%s:%s" % (t[1], str(t[2]))
        out.setdefault(k, {"valid": False, "unresolved": True, "reason": "探测未返回结果",
                           "fmt": "", "label": "", "size": 0, "kbps": 0, "container": "",
                           "declared": "", "mismatch": False, "hires": None})
    return out


def play_key(item, quality=None):
    """缓存文件身份：同一首歌不同音质是不同文件。"""
    q = quality or {}
    return "%s_%s_%s_%s" % (item.get("platform"), item.get("id"),
                            q.get("bitrate") or "", q.get("format") or "")


def song_key(item):
    """收听计数身份：只认歌曲本身，切换音质不算换了一首。"""
    return "%s_%s" % (item.get("platform"), item.get("id"))


def merge_legacy_plays(plays, key, plat, sid, day):
    """旧版把收听次数按音质分开存（play_key），升级后合并到 song_key。

    不合并的话用户切一次音质、或升级到这版，历史收听次数会凭空归零，
    缓存规则看起来就像坏了。
    """
    prefix = "%s_%s_" % (plat, sid)
    merged = None
    for old in [k for k in list(plays) if isinstance(k, str)
                and k.startswith(prefix) and k != key]:
        r = _migrate_play_record(plays.pop(old), day)
        if merged is None:
            merged = r
            continue
        for d, n in (r.get("days") or {}).items():
            merged["days"][d] = _safe_int(merged["days"].get(d)) + _safe_int(n)
        merged["last_at"] = max(_safe_int(merged.get("last_at")),
                                _safe_int(r.get("last_at")))
        merged["cached_today"] = bool(merged.get("cached_today") or r.get("cached_today"))
        merged["cached_long"] = bool(merged.get("cached_long") or r.get("cached_long"))
    return merged


def cache_track(item, quality, tier="today"):
    """把一首歌整首拉进缓存。签名过期会自动重搜换新签名。"""
    plat, sid = item.get("platform"), item.get("id")
    if not plat or not sid:
        return False
    fmt = (quality or {}).get("format") or "mp3"
    br = str((quality or {}).get("bitrate") or "320")
    key = play_key(item, quality)
    if CACHE.find(key, tier):
        return False
    try:
        url = resolve_play_url(plat, sid, fmt, br,
                               item.get("time"), item.get("sign"), item.get("name"))
    except Exception:
        return False
    meta = {"id": item.get("id"), "name": item.get("name"),
            "artist": item.get("artist"), "platform": plat}
    return CACHE.warm(key, url, fmt, tier=tier, meta=meta)


# --------------------------------------------------------------------------
# 本地曲库：把下载目录里已有的文件接回应用内
#
# 范围刻意克制（最小可用版）：只扫 download_dir 一个目录，不碰系统其它位置；
# 标签读得出就用标签，读不出就整段用文件名，绝不因单个文件失败中断。
# 播放直连 /api/local/file（本地盘 Range 流式），不走在线代理、不进缓存、不计数缓存。
# --------------------------------------------------------------------------
LOCAL_AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".ogg", ".oga", ".opus",
                    ".wav", ".wma", ".ape")
_LOCAL_SCAN = {"at": 0.0, "roots": (), "sig": None, "items": []}
_LOCAL_TAGS = {}          # path -> (mtime, tags tuple)；重扫时只读新增/变过的文件
_LOCAL_COVER = {"key": "", "blob": None, "mime": ""}
_LOCAL_LOCK = threading.RLock()
# 大库不能同步扫：mutagen 逐个文件读标签，10k 首要几十秒，前端会先等到超时。
# 超过这个数就先给文件名列表（秒级），标签放后台补，进度走 scanning 字段。
_LOCAL_ASYNC_LIMIT = 800
_LOCAL_TASK = {"running": False, "total": 0, "done": 0, "started": 0.0, "error": ""}


def _local_dir():
    return STORE.data.get("settings", {}).get("download_dir") or default_download_dir()


def _local_make_item(p, size, lid, deep):
    stem = os.path.splitext(os.path.basename(p))[0]
    if deep:
        title, artist, album, fmt, dur, br, sr, bits = _local_read_tags(p, stem)
    else:
        title, artist, album = stem, "", ""
        fmt, dur, br, sr, bits = os.path.splitext(p)[1].lower().lstrip("."), 0, 0, 0, 0
    if not fmt:
        fmt = os.path.splitext(p)[1].lower().lstrip(".") or "mp3"
    item = {
        "platform": "local", "id": lid, "name": title, "artist": artist,
        "album_name": album, "duration": dur, "size": size,
        "format": fmt, "bitrate": br,
        "minfo": [{"format": fmt, "bitrate": br or 320}],
    }
    if sr and bits and (sr > 48000 or bits > 16):
        # Hi-Res：两项都实测到、且超过 CD 规格才标（宁少报不虚报）
        item["hires"] = {"sample_rate": sr, "bit_depth": bits}
    return item


def _local_scan_worker(entries, sig):
    """后台补标签：逐条替换首批的「文件名占位」，每完成一条就推进进度。"""
    try:
        for i, (p, size, _mtime, lid) in enumerate(entries):
            item = _local_make_item(p, size, lid, True)
            with _LOCAL_LOCK:
                if i < len(_LOCAL_SCAN["items"]):
                    _LOCAL_SCAN["items"][i] = item
                _LOCAL_TASK["done"] = i + 1
        with _LOCAL_LOCK:
            _LOCAL_SCAN["sig"] = sig
            _LOCAL_SCAN["at"] = time.time()
            _LOCAL_TASK["running"] = False
    except Exception as e:                                  # noqa: BLE001
        with _LOCAL_LOCK:
            _LOCAL_TASK["running"] = False
            _LOCAL_TASK["error"] = str(e)


def local_scan(force=False):
    """扫描全部曲库根目录（下载目录 + 用户显式添加的目录）。

    签名 = (根列表, 文件数, 最新 mtime)。签名没变且 120 秒内直接用缓存 ——
    标签要逐文件走 mutagen，每次重扫都是几十秒的代价。
    大库（> _LOCAL_ASYNC_LIMIT）先秒级返回文件名列表，标签在后台补。
    """
    roots = _local_roots()
    with _LOCAL_LOCK:
        if _LOCAL_TASK["running"]:
            return {"dir": roots[0] if roots else "", "roots": roots,
                    "items": _LOCAL_SCAN["items"], "cached": True,
                    "scanning": dict(_LOCAL_TASK)}

    entries = []
    for idx, base in enumerate(roots):
        try:
            for root, dirs, files in os.walk(base):
                dirs[:] = [x for x in dirs if not x.startswith((".", "$"))]
                for fn in files:
                    if os.path.splitext(fn)[1].lower() not in LOCAL_AUDIO_EXTS:
                        continue
                    p = os.path.join(root, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    rel = os.path.relpath(p, base).replace("\\", "/")
                    # 0 号根保持纯相对路径：老收藏 / 老歌单里存的就是这个格式
                    entries.append((p, st.st_size, st.st_mtime,
                                    rel if idx == 0 else "@%d/%s" % (idx, rel)))
        except OSError:
            continue
    entries.sort(key=lambda e: -e[2])
    sig = (tuple(roots), len(entries), int(max([e[2] for e in entries], default=0)))

    with _LOCAL_LOCK:
        fresh = (not force and _LOCAL_SCAN["sig"] == sig
                 and time.time() - _LOCAL_SCAN["at"] < 120)
        if fresh:
            return {"dir": roots[0] if roots else "", "roots": roots,
                    "items": _LOCAL_SCAN["items"], "cached": True,
                    "scanning": dict(_LOCAL_TASK)}

        fast = [_local_make_item(p, size, lid, False) for p, size, _m, lid in entries]
        _LOCAL_SCAN.update(at=time.time(), roots=tuple(roots), sig=None, items=fast)

    head = {"dir": roots[0] if roots else "", "roots": roots}

    if len(entries) <= _LOCAL_ASYNC_LIMIT:
        items = [_local_make_item(p, size, lid, True) for p, size, _m, lid in entries]
        with _LOCAL_LOCK:
            _LOCAL_SCAN.update(at=time.time(), sig=sig, items=items)
        return dict(head, items=items, cached=False, scanning=dict(_LOCAL_TASK))

    with _LOCAL_LOCK:
        _LOCAL_TASK.update(running=True, total=len(entries), done=0,
                           started=time.time(), error="")
    threading.Thread(target=_local_scan_worker, name="local-scan",
                     args=(entries, sig), daemon=True).start()
    return dict(head, items=fast, cached=False, scanning=dict(_LOCAL_TASK))


_LOCAL_ID_PREFIX = re.compile(r"^@(\d+)/(.+)$")


def _local_roots():
    """曲库根目录。0 号永远是下载目录（老数据的 id 就是它的相对路径），
    后面是用户在设置里显式添加的目录。

    只收真实存在的目录：移动硬盘拔掉时该根直接消失，不该让整个库扫不出来。
    """
    roots, seen = [], set()
    main = _local_dir()
    if main:
        ap = os.path.abspath(main)
        roots.append(ap)
        seen.add(os.path.normcase(os.path.realpath(ap)))
    for d in (STORE.data.get("settings", {}).get("local_dirs") or []):
        d = str(d or "").strip()
        if not d:
            continue
        ap = os.path.abspath(d)
        key = os.path.normcase(os.path.realpath(ap))
        if key in seen or not os.path.isdir(ap):
            continue
        seen.add(key)
        roots.append(ap)
    return roots


def _local_split(local_id):
    """曲目 id -> (根序号, 相对路径)。

    0 号根的 id 保持「纯相对路径」不变 —— 老收藏、老歌单里存的就是这个格式，
    改格式等于把用户的库弄丢。额外目录才带 `@n/` 前缀。
    """
    s = str(local_id or "").replace("\\", "/").lstrip("/")
    m = _LOCAL_ID_PREFIX.match(s)
    if m:
        return int(m.group(1)), m.group(2)
    return 0, s


def local_resolve(rel):
    """曲目 id -> 绝对路径。越界（.. / 绝对路径 / 目录外）一律拒绝。"""
    idx, tail = _local_split(rel)
    roots = _local_roots()
    if idx < 0 or idx >= len(roots):
        return None
    tail = tail.strip("/")
    if not tail or any(seg in ("", ".", "..") for seg in tail.split("/")):
        return None
    base = os.path.realpath(roots[idx])
    p = os.path.realpath(os.path.join(base, *tail.split("/")))
    low_p, low_b = p.lower(), base.lower()
    if not (low_p == low_b or low_p.startswith(low_b + os.sep)):
        return None
    if os.path.splitext(p)[1].lower() not in LOCAL_AUDIO_EXTS or not os.path.isfile(p):
        return None
    return p


def _local_fmt_of(m):
    """mutagen 探到的真实封装 -> 本项目口径的格式名（下载侧同一思路）。"""
    try:
        mime = (m.mime or [""])[0].lower()
    except Exception:                                       # noqa: BLE001
        mime = ""
    if "flac" in mime:
        return "flac"
    if "mp4" in mime or "aac" in mime or "m4a" in mime:
        return "m4a"
    if "mpeg" in mime or "mp3" in mime:
        return "mp3"
    if "ogg" in mime or "opus" in mime:
        return "opus" if "opus" in mime else "ogg"
    if "ape" in mime or "monkey" in mime:
        return "ape"
    if "wav" in mime or "x-wav" in mime:
        return "wav"
    if "ms-wma" in mime or "wma" in mime:
        return "wma"
    return ""


def _local_read_tags(path, stem):
    """标签读取（带 per-file 缓存）：标题/艺人/专辑/真实格式/时长/码率。"""
    try:
        mt = int(os.path.getmtime(path))
    except OSError:
        mt = 0
    hit = _LOCAL_TAGS.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    title, artist, album, fmt, dur, br, sr, bits = stem, "", "", "", 0, 0, 0, 0
    try:
        from mutagen import File as MFile
        m = MFile(path, easy=True)
        if m is not None:
            tags = m.tags or {}

            def first(k):
                v = tags.get(k)
                return str(v[0]).strip() if v else ""

            title = first("title") or title
            artist = first("artist")
            album = first("album")
            fmt = _local_fmt_of(m)
            dur = int(getattr(m.info, "length", 0) or 0)
            br = int((getattr(m.info, "bitrate", 0) or 0) // 1000)
            # 采样率/位深（发烧客实测口径：文件本体读出，不是标签声称）。
            # WAV/AIFF 的 bits_per_sample 某些 mutagen 版本没有 —— 读不到就
            # 留 0，前端不标（宁少报不虚报）。
            try:
                sr = int(getattr(m.info, "sample_rate", 0) or 0)
                bits = int(getattr(m.info, "bits_per_sample", 0) or 0)
            except Exception:
                sr, bits = 0, 0
    except Exception:                                       # noqa: BLE001
        pass                                                # 单文件坏标签不影响整库
    if " - " in title and not artist:                       # 「歌手 - 歌名」文件名兜底
        artist, _, rest = title.partition(" - ")
        title, artist = rest.strip() or title, artist.strip()
    out = (title, artist, album, fmt, dur, br, sr, bits)
    if len(_LOCAL_TAGS) > 8000:                             # 防极端目录把内存吃穿
        _LOCAL_TAGS.clear()
    _LOCAL_TAGS[path] = (mt, out)
    return out


def _sniff_image(b):
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return ""


def local_cover(rel):
    """内嵌封面 -> 图片字节。单槽缓存（当前在放的那张）。"""
    p = local_resolve(rel)
    if not p:
        return None, ""
    try:
        key = p + "|" + str(int(os.path.getmtime(p)))
    except OSError:
        return None, ""
    if _LOCAL_COVER["key"] == key and _LOCAL_COVER["blob"]:
        return _LOCAL_COVER["blob"], _LOCAL_COVER["mime"]
    blob, mime = None, ""
    try:
        from mutagen import File as MFile
        m = MFile(p)
        if m is not None:
            pics = list(getattr(m, "pictures", None) or [])      # FLAC / Ogg
            tags = getattr(m, "tags", None)
            if tags is not None:                                  # MP3 (ID3)
                try:
                    pics += list(tags.getall("APIC"))
                except Exception:                               # noqa: BLE001
                    pass
                covr = None
                try:
                    covr = tags.get("covr")                     # MP4
                except Exception:                               # noqa: BLE001
                    covr = None
                for c in covr or []:
                    pics.append(type("P", (), {"data": bytes(c)})())
            if pics:
                blob = bytes(pics[0].data)
                mime = (getattr(pics[0], "mime", "") or "").lower()
                if mime not in ("image/jpeg", "image/png", "image/webp"):
                    mime = _sniff_image(blob)
                if mime not in ("image/jpeg", "image/png", "image/webp"):
                    blob = None
    except Exception:                                       # noqa: BLE001
        blob = None
    if blob:
        _LOCAL_COVER.update(key=key, blob=blob, mime=mime)
    return blob, mime


# 桌面歌词小窗中继：主页面把当前行推过来，歌词窗轮询拉走。纯内存，不落盘。
_LYRIC_NOW = {"data": {}, "lock": threading.Lock()}


def lyric_now_set(payload):
    p = payload or {}
    out = {}
    for k in ("title", "artist", "line", "next", "cover", "trans"):
        out[k] = str(p.get(k) or "")[:200]
    out["playing"] = bool(p.get("playing"))
    with _LYRIC_NOW["lock"]:
        _LYRIC_NOW["data"] = out
    return {"ok": True}


def lyric_now_get():
    with _LYRIC_NOW["lock"]:
        return dict(_LYRIC_NOW["data"])


def _clean_daily(daily, keep=400):
    """日汇总只留最近 keep 天，防止无限膨胀。

    返回的每个值保证是 dict：旧数据 / 手改后的 {"日期": 5} 这种形状
    会让统计页的 .get 直接抛 AttributeError。
    """
    if not isinstance(daily, dict) or not daily:
        return {}
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep * 86400))
    return {d: v for d, v in daily.items()
            if isinstance(d, str) and d >= cutoff and isinstance(v, dict)}


def stats_summary():
    """听歌统计：纯只读聚合，不写任何业务数据。

    plays 明细只留 7 天（缓存判定用的），历史统计走 daily 日汇总；
    老数据没有 daily 时，累计口径回落到 7 天窗口的实测值 —— 宁可少报，不虚报。
    """
    data = STORE.data
    plays = data.get("plays") if isinstance(data.get("plays"), dict) else {}
    daily = _clean_daily(data.get("daily") or {})
    hours = data.get("hours") if isinstance(data.get("hours"), dict) else {}

    day = time.strftime("%Y-%m-%d")
    plays_7d = 0
    song_counts = {}                     # key -> (次数, meta)
    for key, rec in plays.items():
        if not isinstance(rec, dict):
            continue
        days = rec.get("days")
        if not isinstance(days, dict):
            continue
        n = sum(max(0, _safe_int(v)) for v in days.values())
        if n <= 0:
            continue
        plays_7d += n
        meta = rec.get("meta")
        if isinstance(meta, dict):
            song_counts[key] = (n, meta)

    top_songs = []
    for _k, (n, meta) in sorted(song_counts.items(), key=lambda kv: -kv[1][0])[:10]:
        if not meta.get("name"):
            continue
        top_songs.append({"id": meta.get("id"), "platform": meta.get("platform"),
                          "name": meta.get("name"), "artist": meta.get("artist"),
                          "cover": meta.get("cover") or "", "plays": n})

    artist_cnt, artist_cover = _play_counts_by_artist()
    top_artists = [{"name": n, "plays": c, "cover": artist_cover.get(n) or ""}
                   for n, c in sorted(artist_cnt.items(), key=lambda kv: -kv[1])[:10]]

    total_plays = sum(max(0, _safe_int(v.get("n"))) for v in daily.values()) or plays_7d
    total_seconds = sum(max(0, _safe_int(v.get("s"))) for v in daily.values())

    days_seq = []
    for i in range(29, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        v = daily.get(d) or {}
        days_seq.append({"date": d, "n": _safe_int(v.get("n")),
                         "s": _safe_int(v.get("s"))})

    return {
        "today": _safe_int((daily.get(day) or {}).get("n")),
        "plays_7d": plays_7d,           # 7 天收听次数
        "songs_7d": len(song_counts),   # 7 天不同曲目
        "total_plays": total_plays,     # 累计次数（daily 全量）
        "total_seconds": total_seconds, # 累计时长（秒）
        "listening_days": len(daily),   # 累计天数
        "by_day": days_seq,             # 近 30 天趋势（缺天补零）
        "hours": [_safe_int(hours.get(str(h))) for h in range(24)],
        "top_songs": top_songs,
        "top_artists": top_artists,
        "favorites": len(data.get("favorites") or []),
        "playlists": len(data.get("playlists") or []),
        "downloads": len(data.get("downloads") or []),
        "empty": not daily and plays_7d == 0,
    }


def _migrate_play_record(rec, day):
    """兼容旧版 {day,count,cached} 结构。"""
    if not isinstance(rec, dict):
        return {"days": {}, "last_at": 0, "cached_today": False, "cached_long": False}
    if "days" in rec:
        days = rec.get("days")
        if not isinstance(days, dict):
            days = {}
        rec["days"] = {str(d): max(0, _safe_int(n)) for d, n in days.items()}
        rec.setdefault("cached_today", rec.get("cached", False))
        rec.setdefault("cached_long", False)
        rec.setdefault("last_at", _safe_int(rec.get("last_at")))
        return rec
    days = {}
    if rec.get("day") == day and rec.get("count"):
        days[day] = max(0, _safe_int(rec["count"]))
    return {"days": days, "last_at": int(time.time()) if days else 0,
            "cached_today": bool(rec.get("cached")), "cached_long": False}


def record_play(payload):
    """记一次收听：只有听完才计数。

    当天听完 N 次  → 进当天缓存（跨天清空）
    7 天内听完 >M 次 → 进长期缓存（3 天没听就清）
    """
    payload = _as_dict(payload)
    item = _json_safe(_as_dict(payload.get("item")))
    quality = _json_safe(_as_dict(payload.get("quality")))
    item = item if isinstance(item, dict) else {}
    quality = quality if isinstance(quality, dict) else {}
    completed = bool(payload.get("completed"))
    plat, sid = item.get("platform"), item.get("id")
    if not plat or not sid:
        return {"ok": False, "error": "缺少曲目信息"}
    local = plat == "local"             # 本地文件只计数，不进在线缓存那套

    key = song_key(item)                  # 计数按「歌」，换音质不重置
    cache_key = play_key(item, quality)   # 缓存文件仍按音质分开
    day = time.strftime("%Y-%m-%d")
    st = STORE.data["settings"]
    # _int_setting：data.json 被手改/旧版写进 "abc" 时不让一条记录整单失败
    need_today = max(1, _int_setting(st, "cache_min_plays", 2))
    need_week = max(1, _int_setting(st, "cache_week_plays", 5))
    auto = st.get("auto_cache", True) is not False

    with STORE.lock:
        plays = STORE.data.setdefault("plays", {})
        if not isinstance(plays, dict):
            plays = {}
            STORE.data["plays"] = plays
        if key not in plays:
            legacy = merge_legacy_plays(plays, key, plat, sid, day)
            if legacy:
                plays[key] = legacy
        rec = _migrate_play_record(plays.get(key), day)
        days = rec["days"]
        # 跨天重置：今天还没听过却挂着「已进今日缓存」标志，说明是之前哪天留下的 ——
        # 今日缓存文件早随 roll() 清掉了，标志不重置的话这首歌当天永远不会再进缓存。
        if rec.get("cached_today") and not days.get(day):
            rec["cached_today"] = False
        if completed:
            days[day] = _safe_int(days.get(day)) + 1
            rec["last_at"] = int(time.time())
            # meta 每次收听覆盖，但空字段保留旧值 —— 换个没带封面的来源续播时，
            # 不能把之前记下的封面抹掉（统计 top 榜会丢图）
            old_meta = _as_dict(rec.get("meta"))
            rec["meta"] = {
                "id": item.get("id"), "name": item.get("name") or old_meta.get("name"),
                "artist": item.get("artist") or old_meta.get("artist"),
                "platform": plat,
                "cover": item.get("cover") or old_meta.get("cover") or "",
            }
            # 日汇总 + 时段分布：统计专用，与缓存判定（plays.days）无关
            daily = STORE.data.setdefault("daily", {})
            if not isinstance(daily, dict):
                daily = {}
                STORE.data["daily"] = daily
            dv = daily.get(day)
            if not isinstance(dv, dict):
                dv = {"n": 0, "s": 0}
            dv["n"] = max(0, _safe_int(dv.get("n"))) + 1
            dv["s"] = max(0, _safe_int(dv.get("s"))) + max(0, _safe_int(item.get("duration")))
            daily[day] = dv
            STORE.data["daily"] = _clean_daily(daily)
            hrs = STORE.data.setdefault("hours", {})
            if not isinstance(hrs, dict):
                hrs = {}
                STORE.data["hours"] = hrs
            hh = str(time.localtime().tm_hour)
            hrs[hh] = max(0, _safe_int(hrs.get(hh))) + 1
            recent = STORE.data.setdefault("recent", [])
            if not isinstance(recent, list):
                recent = []
                STORE.data["recent"] = recent
            recent[:] = [r for r in recent if isinstance(r, dict) and
                         not (r.get("platform") == plat and str(r.get("id")) == str(sid))]
            entry = dict(item)
            entry["platform"] = plat
            entry["played_at"] = int(time.time())
            recent.insert(0, entry)
            del recent[60:]
            rec["quality"] = {"format": quality.get("format"), "bitrate": quality.get("bitrate")}
        # 只保留最近 7 天
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
        rec["days"] = {d: n for d, n in days.items()
                       if isinstance(d, str) and d >= cutoff}
        plays[key] = rec
        STORE.save()

    today_count = _safe_int(rec["days"].get(day))
    week_count = sum(_safe_int(n) for n in rec["days"].values())

    triggered = None
    if completed and auto and not local:
        if today_count >= need_today and not rec.get("cached_today"):
            if cache_track(item, quality, "today"):
                triggered = "today"
                with STORE.lock:
                    STORE.data["plays"][key]["cached_today"] = True
                    STORE.save()
        if week_count > need_week and not rec.get("cached_long"):
            if cache_track(item, quality, "long"):
                triggered = triggered or "long"
                with STORE.lock:
                    STORE.data["plays"][key]["cached_long"] = True
                    STORE.save()
    elif not local and rec.get("cached_long"):
        CACHE.touch_long(cache_key)           # 长期缓存续命（按音质文件）

    CACHE.evict_long(max(1, _int_setting(st, "cache_keep_days", 3)))

    return {"ok": True, "data": {
        "key": key, "day": day,
        "today_count": today_count, "week_count": week_count,
        "need_today": need_today, "need_week": need_week,
        "cached_today": bool(rec.get("cached_today")),
        "cached_long": bool(rec.get("cached_long")),
        "triggered": triggered,
    }}


def write_tags(path, item, lyric=None, cover=None):
    """把标题/歌手/专辑/封面/歌词写进标签。失败一律忽略，不影响下载结果。"""
    try:
        ext = os.path.splitext(path)[1].lower()
        name = item.get("name") or ""
        artist = item.get("artist") or ""
        album = item.get("album_name") or ""

        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            f = FLAC(path)
            if name:
                f["title"] = name
            if artist:
                f["artist"] = artist
            if album:
                f["album"] = album
            if lyric:
                f["lyrics"] = lyric
            if cover:
                pic = Picture()
                pic.type = 3
                # 封面不一定是 jpeg：上游经常直接给 PNG，mime 写错部分播放器不显示
                pic.mime = ("image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n"
                            else "image/jpeg")
                pic.data = cover
                f.add_picture(pic)
            f.save()

        elif ext == ".mp3":
            from mutagen.id3 import ID3, APIC, TALB, TIT2, TPE1, USLT, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            if name:
                tags.delall("TIT2")
                tags.add(TIT2(encoding=3, text=name))
            if artist:
                tags.delall("TPE1")
                tags.add(TPE1(encoding=3, text=artist))
            if album:
                tags.delall("TALB")
                tags.add(TALB(encoding=3, text=album))
            if lyric:
                tags.delall("USLT")
                tags.add(USLT(encoding=3, lang="chi", desc="", text=lyric))
            if cover:
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
            tags.save(path)

        elif ext in (".m4a", ".aac"):
            from mutagen.mp4 import MP4, MP4Cover
            m = MP4(path)
            if name:
                m["\xa9nam"] = [name]
            if artist:
                m["\xa9ART"] = [artist]
            if album:
                m["\xa9alb"] = [album]
            if lyric:
                m["\xa9lyr"] = [lyric]
            if cover:
                fmt = (MP4Cover.FORMAT_PNG if cover[:8] == b"\x89PNG\r\n\x1a\n"
                       else MP4Cover.FORMAT_JPEG)
                m["covr"] = [MP4Cover(cover, imageformat=fmt)]
            m.save()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 下载队列
# --------------------------------------------------------------------------
class DownloadManager:
    """并发下载队列：自动降级、速度统计、取消、重试。"""

    MAX_WORKERS = 4

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        # 槽位等待用 Condition：原来 0.2s 轮询抢锁（4 线程忙等），
        # 且 waited>300 跳出后任务照样执行，超时语义名存实亡。
        self._can_run = threading.Condition(self.lock)
        self.tasks = {}
        self.order = []
        self.q = queue.Queue()
        self.running = 0
        self._started = False

    def _ensure_workers(self):
        with self.lock:
            if self._started:
                return
            self._started = True
            for i in range(self.MAX_WORKERS):
                threading.Thread(target=self._worker, name=f"dl-{i}", daemon=True).start()

    def _worker(self):
        while True:
            tid = self.q.get()
            task = None
            try:
                with self._can_run:
                    task = self.tasks.get(tid)
                    if not task or task["state"] != "queued" or task.get("cancel"):
                        continue
                    limit = max(1, int(self.store.data["settings"].get("concurrency") or 2))
                    # 等槽位：被唤醒或超 1s 重查一次（并发数被调大时要能及时感知）
                    while self.running >= limit:
                        self._can_run.wait(timeout=1)
                        task = self.tasks.get(tid)
                        if not task or task["state"] != "queued" or task.get("cancel"):
                            break
                    if not task or task["state"] != "queued" or task.get("cancel"):
                        continue
                    task["state"] = "running"
                    task["started"] = time.time()
                    self.running += 1
                self._run(task)
            except Exception as e:  # noqa: BLE001
                if task:
                    with self.lock:
                        task["state"] = "error"
                        task["error"] = str(e)
            finally:
                with self._can_run:
                    t = self.tasks.get(tid)
                    if t and t["state"] in ("done", "error", "cancelled", "skipped", "running"):
                        if t.get("started") and t["state"] != "queued":
                            self.running = max(0, self.running - 1)
                    # 释放槽位 / 任务被取消时，把排队中的 worker 叫醒
                    self._can_run.notify_all()
                try:
                    self.q.task_done()
                except ValueError:
                    pass

    def _resolve(self, item, want):
        """按降级顺序解析直链，返回 (url, quality, ext)。"""
        last_err = None
        for q in quality_candidates(item, want):
            data = None
            try:
                data = upstream("getUrl", {
                    "platform": item.get("platform", "kuwo"),
                    "songid": item.get("id"),
                    "format": q["format"], "bitrate": q["bitrate"],
                    "time": item.get("time"), "sign": item.get("sign"),
                })
            except Exception as e:  # noqa: BLE001
                last_err = e
                if "过期" in str(e) and self._refresh_sign(item):
                    try:
                        data = upstream("getUrl", {
                            "platform": item.get("platform", "kuwo"),
                            "songid": item.get("id"),
                            "format": q["format"], "bitrate": q["bitrate"],
                            "time": item.get("time"), "sign": item.get("sign"),
                        })
                    except Exception as e2:  # noqa: BLE001
                        last_err = e2
                        continue
            url = (data or {}).get("url")
            if url:
                return url, q, sniff_ext(url, q["format"])
        raise RuntimeError(f"所有音质都解析失败：{last_err}")

    def _refresh_sign(self, item):
        try:
            data = upstream("search", {"platform": item.get("platform", "kuwo"),
                                       "keyword": item.get("name", ""),
                                       "page": 1, "size": 20})
            for cand in (data or {}).get("list") or []:
                if str(cand.get("id")) == str(item.get("id")):
                    item["time"] = cand.get("time")
                    item["sign"] = cand.get("sign")
                    return True
        except Exception:
            pass
        return False

    def _run(self, task):
        item = task["item"]
        settings = self.store.data["settings"]
        want = task.get("want_quality") or settings.get("quality") or "best"
        folder = task.get("dir") or settings.get("download_dir") or default_download_dir()

        try:
            url, q, ext = self._resolve(item, want)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                task.update(state="error", error=str(e), speed=0)
            return

        base = render_template(settings.get("name_template"), item, q["format"], q["bitrate"])
        policy = settings.get("dup_policy") or "rename"
        path = target_path(folder, base, ext, policy)
        if path is None:
            with self.lock:
                task.update(state="skipped", error="同名文件已存在（设置为跳过）", speed=0)
            return

        with self.lock:
            task.update(path=path, filename=os.path.basename(path), format=ext,
                        bitrate=str(q.get("bitrate")), quality_label=q["format"])

        tmp = path + ".part"
        got, total = 0, 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": UPSTREAM})
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0)
                with self.lock:
                    task["total"] = total
                with open(tmp, "wb") as f:
                    while True:
                        if task.get("cancel"):
                            raise InterruptedError("已取消")
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        with self.lock:
                            task["got"] = got
                            now = time.time()
                            task["_samples"].append((now, got))
                            task["_samples"] = [s for s in task["_samples"] if now - s[0] <= 3]
            os.replace(tmp, path)

            lyric = None
            if settings.get("save_lyric") or settings.get("write_tags"):
                try:
                    lyric = upstream("getLyric", {"platform": item.get("platform", "kuwo"),
                                                  "songid": item.get("id"),
                                                  "time": item.get("time"),
                                                  "sign": item.get("sign")})
                except Exception:
                    lyric = None
            if settings.get("save_lyric") and lyric:
                try:
                    with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as f:
                        f.write(lyric)
                except OSError:
                    pass
            if settings.get("write_tags"):
                cover = fetch_bytes(item.get("cover") or "") if item.get("cover") else None
                write_tags(path, item, lyric, cover)

            with self.lock:
                task.update(state="done", got=got, total=got or total,
                            finished=time.time(), speed=0)
            self.store.apply("download_add", {
                "name": item.get("name", ""),
                "artist": item.get("artist", ""),
                "album": item.get("album_name", ""),
                "path": path,
                "filename": os.path.basename(path),
                "format": ext,
                "bitrate": str(q.get("bitrate") or ""),
                "size": got,
                "platform": item.get("platform", ""),
                "cover": item.get("cover", ""),
                "at": int(time.time()),
            })
        except InterruptedError:
            self._cleanup(tmp)
            with self.lock:
                task.update(state="cancelled", error="已取消", speed=0)
        except Exception as e:  # noqa: BLE001
            self._cleanup(tmp)
            with self.lock:
                task.update(state="error", error=str(e), speed=0)

    @staticmethod
    def _cleanup(tmp):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

    def add(self, item, want=None, dir_=None):
        # 没有平台 / id 的条目解析直链必然失败：放进队列只会在几秒后
        # 留一条红色失败任务，还白占一个 worker。
        if not isinstance(item, dict) or not item.get("platform") or not item.get("id"):
            return None
        if dir_ is not None and not isinstance(dir_, str):
            dir_ = None
        self._ensure_workers()
        tid = uuid.uuid4().hex[:12]
        task = {
            "id": tid, "item": item,
            "name": item.get("name", ""), "artist": item.get("artist", ""),
            "album": item.get("album_name", ""), "cover": item.get("cover", ""),
            "platform": item.get("platform", ""),
            "want_quality": want, "dir": dir_,
            "state": "queued", "got": 0, "total": 0, "speed": 0,
            "error": None, "path": None, "filename": None,
            "format": None, "bitrate": None, "quality_label": None,
            "created": time.time(), "finished": None, "started": None,
            "_samples": [],
        }
        with self.lock:
            self.tasks[tid] = task
            self.order.append(tid)
        self.q.put(tid)
        return tid

    def add_many(self, items, want=None, dir_=None):
        if not isinstance(items, list):
            return []
        return [tid for tid in (self.add(it, want, dir_) for it in items) if tid]

    def cancel(self, tid):
        with self._can_run:
            task = self.tasks.get(tid)
            if not task:
                return False
            if task["state"] == "queued":
                task.update(state="cancelled", error="已取消")
                self._can_run.notify_all()       # 等着这个 tid 的 worker 醒来后直接跳过
                return True
            if task["state"] == "running":
                task["cancel"] = True
                return True
            return False

    def retry(self, tid):
        with self.lock:
            task = self.tasks.get(tid)
            if not task or task["state"] not in ("error", "cancelled", "skipped"):
                return False
            task.update(state="queued", error=None, got=0, total=0, speed=0,
                        cancel=False, started=None, finished=None, _samples=[])
        self.q.put(tid)
        return True

    def remove(self, tid):
        with self._can_run:
            task = self.tasks.get(tid)
            if not task:
                return False
            if task["state"] == "running":
                task["cancel"] = True
                return False
            if task["state"] == "queued":
                # 排队中的直接置终态：原来只挂 cancel 标志，worker 照样会解析
                # 直链、建 .part，然后才在下载循环里取消 —— 白付一次解析。
                task.update(state="cancelled", error="已取消")
                self._can_run.notify_all()
                return False
            self.tasks.pop(tid, None)
            if tid in self.order:
                self.order.remove(tid)
        return True

    def snapshot(self):
        out = []
        with self.lock:
            for tid in list(self.order):
                t = self.tasks.get(tid)
                if not t:
                    continue
                speed = 0
                if t["state"] == "running" and t["_samples"]:
                    first, last = t["_samples"][0], t["_samples"][-1]
                    dt = last[0] - first[0]
                    if dt > 0.4:
                        speed = (last[1] - first[1]) / dt
                out.append({
                    "id": tid, "name": t["name"], "artist": t["artist"],
                    "album": t["album"], "cover": t["cover"], "platform": t["platform"],
                    "state": t["state"], "got": t["got"], "total": t["total"],
                    "speed": speed if t["state"] == "running" else 0,
                    "error": t["error"], "path": t["path"], "filename": t["filename"],
                    "format": t["format"], "bitrate": t["bitrate"],
                    "created": t["created"], "finished": t["finished"],
                    "eta": int((t["total"] - t["got"]) / speed) if speed > 4096 and t["total"] else None,
                    "age": time.time() - t["created"],
                })
        return out


DM = DownloadManager(STORE)


# --------------------------------------------------------------------------
# 更新追踪（notify-only）
#
# 和规则热更新同一个原则：只比对版本号、点亮提示，绝不自动下载或替换任何文件。
# 应用版本走 GitHub Releases（/releases/latest 本身就排除 draft/prerelease），
# 规则版本沿用 providers/manifest.json 的 update_url 通道（留空则报 not_configured）。
# 结果缓存在 UPDATE_STATE，前端轮询 /api/update/status，不打爆 GitHub。
# --------------------------------------------------------------------------
GITHUB_REPO = "elysiayunchen/elysium-player"
RELEASE_URL = "https://github.com/" + GITHUB_REPO + "/releases/latest"
UPDATE_PERIOD = 6 * 3600        # 周期检查间隔：6 小时
# 更新源链（国内可达性优先）：仓库内 update.json 走 jsDelivr 分发，是唯一携带
# 资产直链 / mirrors / sha256 的源；GitHub Releases API 只提供 tag 与发布页，作兜底。
# 两个源经常一个通一个不通，所以谁先通用谁；手动「检查更新」反向优先（见 do_GET）。
JSDELIVR_HOSTS = ["cdn.jsdelivr.net", "fastly.jsdelivr.net", "gcore.jsdelivr.net"]
UPDATE_STATE = {
    "last_check": 0,            # 上次检查的 epoch 秒（0 = 还没查过）
    "latest": "",               # 远端最新版本号，如 v1.5.0
    "has_update": False,        # latest 比本地 VERSION 新
    "url": RELEASE_URL,         # 发布页，前端「发布页 ›」直达
    "assets": [],               # [{name, url, mirrors[], sha256}]，打不开 GitHub 时给直链
    "source": "",               # 本次结果来自哪个源：manifest / github
    "error": "",                # 上次检查失败原因（全部源不可达时非空）
}


def _ver_tuple(v):
    """'v1.4.2-beta' -> (1, 4, 2)。只认数字段，后缀不参与比较。

    数字段截到 9 位：版本号不会有 10 位，而 int() 有 4300 位上限 ——
    远端给一串超长数字时整条检查更新链路会抛异常。
    """
    return tuple(int(x[:9]) for x in re.findall(r"\d+", str(v or "").split("-")[0])) or (0,)


def _fetch_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Elysium/" + VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _norm_assets(raw):
    """update.json 的 assets 数组 → 干净的展示列表；url 必须是 http(s)，镜像最多 6 个。"""
    out = []
    for a in raw or []:
        if not isinstance(a, dict):
            continue
        url = str(a.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        out.append({
            "name": str(a.get("name") or "下载"),
            "url": url,
            "mirrors": [str(m).strip() for m in (a.get("mirrors") or [])
                        if str(m).startswith(("http://", "https://"))][:6],
            "sha256": str(a.get("sha256") or "").strip(),
        })
    return out[:4]


def _check_remote(prefer_github=False, timeout=8):
    """沿源链逐个试，返回归一化的远端信息；全挂就抛最后一个异常。

    update.json（jsDelivr）形状：{version, notes, page, assets}；
    GitHub API 形状：{tag_name, name, html_url}。分头识别、归一成同一种返回。
    GitHub API 先通时不立即返回：版本号以它为准（权威、无 CDN 缓存），
    但再扫一遍 jsDelivr 把 assets（直链 / mirrors / sha256）补上——
    API 本身不带这些，少了它们应用内下载就没有资产行。
    """
    api = "https://api.github.com/repos/" + GITHUB_REPO + "/releases/latest"
    jsd = ["https://" + h + "/gh/" + GITHUB_REPO + "@main/update.json"
           for h in JSDELIVR_HOSTS]
    chain = ([api] + jsd) if prefer_github else (jsd + [api])
    result = None
    last_err = None
    for url in chain:
        try:
            j = _fetch_json(url, timeout)
        except Exception as e:                              # noqa: BLE001
            last_err = e
            continue
        if isinstance(j, dict) and j.get("tag_name") is not None:
            tag = str(j.get("tag_name") or "").strip()
            result = {"latest": tag, "name": str(j.get("name") or "")[:120],
                      "url": str(j.get("html_url") or "") or RELEASE_URL,
                      "assets": [], "source": "github"}
            break                                           # 版本已定，出去补资产
        if isinstance(j, dict) and str(j.get("version") or "").strip():
            return {"latest": str(j["version"]).strip(),
                    "name": str(j.get("notes") or "")[:120],
                    "url": str(j.get("page") or "") or RELEASE_URL,
                    "assets": _norm_assets(j.get("assets")), "source": "manifest"}
    if result is None:
        raise last_err or RuntimeError("所有更新源都不可达")
    for url in jsd:                                         # GitHub 先通：补资产清单
        try:
            j = _fetch_json(url, timeout)
        except Exception:                                   # noqa: BLE001
            continue
        if isinstance(j, dict) and str(j.get("version") or "").strip():
            result["assets"] = _norm_assets(j.get("assets"))
            break
    return result


def update_check_now(prefer_github=False):
    """同步查一次（应用 + 规则），结果写进 UPDATE_STATE 并返回。最多阻塞 8s。"""
    out = {"client_version": VERSION, "checked_at": int(time.time())}
    try:
        r = _check_remote(prefer_github=prefer_github)
        out["app"] = {"latest": r["latest"], "name": r["name"],
                      "has_update": _ver_tuple(r["latest"]) > _ver_tuple(VERSION),
                      "url": r["url"], "source": r["source"]}
        out["assets"] = r["assets"]
        UPDATE_STATE.update(last_check=out["checked_at"], latest=r["latest"],
                            has_update=out["app"]["has_update"], url=r["url"],
                            assets=r["assets"], source=r["source"], error="")
    except Exception as e:                                  # noqa: BLE001
        out["app"] = "unavailable"
        out["error"] = str(e)[:160]
        UPDATE_STATE.update(last_check=out["checked_at"], error=str(e)[:160])
    try:
        out["rules"] = importer.update_check()
    except Exception:                                       # noqa: BLE001
        out["rules"] = None
    return out


def update_tracker():
    """后台循环：启动 8s 后首查（让首屏先起来），之后每 6h 一次。

    设置里关掉自动检查就只睡不查；手动「检查更新」走 /api/update/status?refresh=1。
    """
    time.sleep(8)
    while True:
        try:
            if STORE.data.get("settings", {}).get("auto_check_update", True):
                update_check_now()
        except Exception:                                   # noqa: BLE001
            pass                                            # 追踪器绝不影响主服务
        time.sleep(UPDATE_PERIOD)


# ---- 应用内下载新版本 -------------------------------------------------------
# 下载 + SHA-256 校验后就停：文件落在本机下载目录，替换/运行由用户手动完成，
# 不碰运行中的 exe，不写注册表，砖不了机。第三方镜像可能被替换文件，
# 所以 update.json 里给了 sha256 就强制校验，不匹配直接删掉半成品。
UPDATE_DL = {
    "state": "idle",            # idle / running / done / error
    "url": "",                  # 本次下载的直链（可能是镜像）
    "path": "",                 # 完成后的本地路径
    "got": 0, "total": 0,       # 字节进度
    "speed": 0,                 # B/s
    "verified": None,           # True=校验通过 / False=不匹配 / None=远端未提供 sha256
    "error": "",
}
_DL_LOCK = threading.Lock()


def _download_update(url, sha256):
    """流式下载资产到下载目录，边下边算 SHA-256。任何失败都清理 .part 半成品。"""
    tmp = None
    try:
        with _DL_LOCK:
            if UPDATE_DL["state"] == "running":
                return
            UPDATE_DL.update(state="running", url=url, path="", got=0, total=0,
                             speed=0, verified=None, error="")
        if not url.startswith(("http://", "https://")):
            raise ValueError("只支持 http(s) 直链")
        latest = UPDATE_STATE.get("latest") or "new"
        # 版本号来自远端 update.json / GitHub tag：只留文件名安全字符。
        # 不洗的话 "../.." 之类会把更新包写到下载目录之外。
        latest = re.sub(r"[^0-9A-Za-z._-]", "_", str(latest))[:32].strip("._") or "new"
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
        if ext not in (".exe", ".apk", ".zip", ".7z"):
            ext = ".exe"
        ddir = STORE.data.get("settings", {}).get("download_dir") or default_download_dir()
        if not isinstance(ddir, str):
            ddir = default_download_dir()
        os.makedirs(ddir, exist_ok=True)
        path = os.path.join(ddir, "Elysium-%s%s" % (latest, ext))
        tmp = path + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "Elysium/" + VERSION})
        t0 = time.time()
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
            UPDATE_DL["total"] = int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                dt = time.time() - t0
                UPDATE_DL.update(got=f.tell(), speed=int(f.tell() / dt) if dt > 0.5 else 0)
        if sha256:
            ok = h.hexdigest().lower() == str(sha256).strip().lower()
            UPDATE_DL["verified"] = ok
            if not ok:
                os.remove(tmp)
                tmp = None
                UPDATE_DL.update(state="error", path="",
                                 error="SHA-256 校验不通过，文件已删除（来源可能被替换）")
                return
        os.replace(tmp, path)
        tmp = None
        UPDATE_DL.update(state="done", path=path, speed=0)
    except Exception as e:                                  # noqa: BLE001
        UPDATE_DL.update(state="error", speed=0, error=str(e)[:160])
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def run_diag():
    """网络自检：逐跳定位「搜得到歌但放不出来」到底断在哪一跳。

    搜索与取流是两批不同主机 —— 主站（搜索/解析）通、CDN（真正取音频）不通，
    是真机上最典型的形态；每跳独立计时并捕获异常，当场指认环节。
    """
    import socket
    hops = []
    song = {}

    def hop(name, fn):
        t0 = time.time()
        try:
            detail = fn()
            hops.append({"name": name, "ok": True,
                         "ms": int((time.time() - t0) * 1000), "detail": str(detail)})
        except Exception as e:  # noqa: BLE001
            hops.append({"name": name, "ok": False,
                         "ms": int((time.time() - t0) * 1000),
                         "detail": "%s: %s" % (type(e).__name__, e)})

    def _cache_write():
        p = os.path.join(CACHE.today_root, ".diagtest")
        with open(p, "wb") as f:
            f.write(b"ok")
        os.remove(p)
        return "可写"

    def _clock():
        # TLS 证书有效期按系统时间校验：时间错的设备 HTTPS 会全挂
        return "%s 本地 / %s UTC" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))

    def _dns():
        addrs = sorted({i[4][0] for i in socket.getaddrinfo(UPSTREAM_HOST, 443)})
        return "→ " + ", ".join(addrs)

    def _search():
        data = upstream("search", {"platform": "kuwo", "keyword": "晴天",
                                   "page": 1, "size": 5})
        lst = (data or {}).get("list") or []
        if not lst:
            raise RuntimeError("主站正常返回但 0 条结果")
        song.update(lst[0])
        return "「%s」等 %d 条" % (lst[0].get("name"), len(lst))

    def _resolve():
        if not song:
            raise RuntimeError("上一步失败，跳过")
        url = resolve_play_url("kuwo", song.get("id"), "mp3", "128",
                               song.get("time"), song.get("sign"), song.get("name"))
        song["url"] = url
        return urllib.parse.urlparse(url).netloc

    def _cdn():
        if not song.get("url"):
            raise RuntimeError("上一步失败，跳过")
        req = urllib.request.Request(song["url"], headers={
            "User-Agent": UA, "Range": "bytes=0-15", "Referer": UPSTREAM})
        with urllib.request.urlopen(req, timeout=15) as r:
            head = r.read(16)
            return "HTTP %s · %s · 头 %s" % (
                r.status, r.headers.get("Content-Type"), head[:8].hex())

    hop("1/6 缓存目录可写", _cache_write)
    hop("2/6 系统时钟", _clock)
    hop("3/6 主站域名解析", _dns)
    hop("4/6 主站搜索", _search)
    hop("5/6 解析播放直链", _resolve)
    hop("6/6 CDN 取流（播放真正走的链路）", _cdn)
    return {"hops": hops}


_DIAG_TONE = None


def diag_tone_wav():
    """自检测试音：3 秒 440Hz 正弦 WAV。不走网络 —— 测试音能响就说明
    WebView 解码 / 音频焦点 / 通知服务这条本地链路没坏，问题只在网络链路。"""
    global _DIAG_TONE
    if _DIAG_TONE is None:
        import io
        import struct
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"".join(
                struct.pack("<h", int(20000 * math.sin(2 * math.pi * 440 * i / 22050)))
                for i in range(22050 * 3)))
        _DIAG_TONE = buf.getvalue()
    return _DIAG_TONE


def _range_num(s, fallback):
    """Range 里的数字：超过 18 位直接按 fallback 处理。

    Python 3.11+ 的 int() 有 4300 位上限，把 5000 个 9 丢进去会抛
    ValueError；而这段在 handler 主路径上、外面没有兜底 —— 一条请求就能让
    连接无响应。正常 Range 数字不会超过 18 位（EB 级）。
    """
    if len(s) > 18:
        return fallback
    try:
        return int(s)
    except (TypeError, ValueError):
        return fallback


class Handler(BaseHTTPRequestHandler):
    server_version = "FlacMusic/" + VERSION

    def log_message(self, *args):
        pass

    def _local_request(self):
        """只服务本机回环发起的请求。服务虽然只绑 127.0.0.1，但显式校验 Host
        可以挡住 DNS rebinding（恶意网页把域名解析到 127.0.0.1 后摸 API）。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):            # [::1]:port / [::1]
            host = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:          # 127.0.0.1:port
            host = host.rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # 页面刷新、切歌、关窗都会先掐连接；写响应时报这些是常态，
            # 不该让每次断连在日志里留一段 traceback。
            pass

    def _json_body(self):
        """读 JSON body：解析失败 / 不是对象 / 长度非法，一律返回 {}。

        返回 dict 是硬约定 —— 上层直接 body.get(...)，返回 list / str 会在取
        字段时抛 AttributeError，最后被兜底成一句看不出问题的 ok:false。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    # -- 音频代理 / 缓存 ---------------------------------------------------
    def _serve_media(self, key, url, ext):
        """命中今天的缓存就直接读盘，否则原样转发上游（含 Range），不落盘。"""
        target = CACHE.find(key)
        if target:
            self._serve_cached(target, ext)
            return
        self._proxy_stream(url, ext)

    def _serve_cached(self, path, ext):
        try:
            size = os.path.getsize(path)
        except OSError:
            self._send(404, {"error": "cache miss"})
            return
        start, end = 0, size - 1
        header = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", header)
        partial = False
        if m and (m.group(1) or m.group(2)):
            partial = True
            if m.group(1):
                start = _range_num(m.group(1), size)
                if m.group(2):
                    end = min(_range_num(m.group(2), size - 1), size - 1)
            else:                                   # bytes=-N 取尾部
                start = max(0, size - _range_num(m.group(2), size))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */" + str(size))
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", AUDIO_MIME.get((ext or "bin").lower(),
                                                       "application/octet-stream"))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _proxy_stream(self, url, ext):
        """纯转发：把客户端的 Range 原样带给上游，206 就回 206，支持随时拖动。"""
        mime = AUDIO_MIME.get((ext or "bin").lower(), "application/octet-stream")
        headers = {"User-Agent": UA, "Referer": UPSTREAM}
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=45)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                self.send_response(416)
                self.send_header("Content-Range", e.headers.get("Content-Range") or "bytes */*")
                self.end_headers()
                return
            self._send(502, {"error": "上游取流出错: %s" % e})
            return
        except Exception as e:  # noqa: BLE001
            self._send(502, {"error": "上游取流出错: %s" % e})
            return

        self.send_response(resp.status)
        self.send_header("Content-Type", resp.headers.get("Content-Type") or mime)
        for h in ("Content-Length", "Content-Range"):
            if resp.headers.get(h):
                self.send_header(h, resp.headers[h])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _static(self, path):
        rel = (path.lstrip("/") or "index.html").split("?")[0].split("#")[0]
        target = os.path.normpath(os.path.join(resource_dir(), "web", rel))
        webroot = os.path.normpath(os.path.join(resource_dir(), "web"))
        if not (target == webroot or target.startswith(webroot + os.sep)) \
                or not os.path.isfile(target):
            self._send(404, {"error": "not found"})
            return
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    def do_GET(self):
        if not self._local_request():
            self._send(403, {"error": "forbidden"})
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/store":
            data = STORE.apply("load", None)
            data["_data_path"] = DATA_FILE
            data["_version"] = VERSION
            # 同步密码不回给页面：拿不到就不可能被 XSS / 截图带走。
            # 前端只拿到 has_pass，改设置时密码留空即表示「沿用原密码」。
            wd = (data.get("settings") or {}).get("webdav")
            if isinstance(wd, dict):
                wd["has_pass"] = bool(wd.pop("pass", ""))
            self._send(200, data)
            return
        if path == "/api/import/status":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, {"ok": True,
                             "data": importer.status((q.get("id") or [""])[0])})
            return
        if path == "/api/providers/health":
            # 按需自检：三个探针各取一个固定公开歌单，失败只标记降级，不影响主流程
            self._send(200, {"ok": True, "data": importer.health()})
            return
        if path == "/api/update/check":
            # notify-only：只比版本号，绝不自动下载 / 替换文件
            self._send(200, {"ok": True, "data": importer.update_check()})
            return
        if path == "/api/update/status":
            # 更新追踪状态；?refresh=1 当场查一次（最多 8s，给手动按钮用）。
            # 手动检查反向优先 GitHub API（最权威、无 CDN 缓存延迟），不通自动落到 jsDelivr。
            # 两种情况都吐 UPDATE_STATE 同一种扁平形状，前端不用分叉。
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("refresh"):
                update_check_now(prefer_github=True)
            st = dict(UPDATE_STATE)
            st["client_version"] = VERSION
            st["auto"] = bool(STORE.data.get("settings", {}).get("auto_check_update", True))
            self._send(200, {"ok": True, "data": st})
            return
        if path == "/api/update/download":
            self._send(200, {"ok": True, "data": dict(UPDATE_DL)})
            return
        if path == "/api/store/export":
            # 导出脱敏副本：去掉含用户名的本机绝对路径，
            # 分享给别人时不会泄漏磁盘结构与用户名。
            data = json.loads(json.dumps(STORE.apply("load", None)))
            data.pop("_data_path", None)
            data.pop("_version", None)
            if data.get("settings"):
                data["settings"]["download_dir"] = "<本机音乐目录>"
                data["settings"].pop("webdav", None)   # 同步凭据绝不外流
            data.pop("works", None)    # 作品文件在本机，meta 出去也是僵尸条目
            self._send(200, {"ok": True, "data": data})
            return
        if path == "/api/sync/status":
            self._send(200, {"ok": True, "data": sync_engine.status()})
            return
        if path == "/api/works":
            self._send(200, {"ok": True,
                             "data": STORE.apply("load", None).get("works") or []})
            return
        if path == "/api/works/file":
            # K 歌回放。id 在 work_file 里拼路径前已由白名单挡住路径穿越。
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            wid = (qs.get("id") or [""])[0]
            if not WORK_ID_RE.match(wid):
                self._send(400, {"error": "作品 id 不合法"})
                return
            p = work_file(wid)
            if not os.path.isfile(p):
                self._send(404, {"error": "作品文件不在了"})
                return
            self._serve_cached(p, "wav")
            return
        if path == "/api/downloads":
            self._send(200, {"ok": True, "data": {
                "tasks": DM.snapshot(),
                "concurrency": STORE.data["settings"].get("concurrency", 2)}})
            return
        if path == "/api/diag":
            # 网络自检（设置抽屉按钮）：逐跳计时/捕获，真机上当场指认断点
            self._send(200, {"ok": True, "data": run_diag()})
            return
        if path == "/api/diag/tone":
            # 自检测试音：本地生成 WAV，不经网络与缓存
            self._send(200, diag_tone_wav(), "audio/wav")
            return
        if path == "/api/media":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (qs.get("key") or [""])[0]
            url = (qs.get("url") or [""])[0]
            # 直链只接受 http/https：urllib 认得 file:// 等本地 scheme，
            # 放进来等于让任何本机页面借这个端点读任意文件。
            if url and urllib.parse.urlparse(url).scheme not in ("http", "https"):
                self._send(400, {"error": "仅支持 http/https 直链"})
                return
            ext = (qs.get("ext") or ["bin"])[0]
            if not url:
                sid = (qs.get("sid") or [""])[0]
                if not key or not sid:
                    self._send(400, {"error": "缺少参数"})
                    return
                try:
                    url = resolve_play_url(qs.get("platform", ["kuwo"])[0], sid,
                                           qs.get("fmt", ["mp3"])[0], qs.get("br", ["320"])[0],
                                           (qs.get("time") or [None])[0], (qs.get("sign") or [None])[0],
                                           (qs.get("name") or [None])[0])
                except Exception as e:  # noqa: BLE001
                    self._send(502, {"error": f"解析失败: {e}"})
                    return
            if not key:
                key = MediaCache.key_of(url)
            self._serve_media(key, url, ext)
            return
        if path == "/api/stats/summary":
            # 听歌统计：纯只读聚合，不写任何业务数据
            self._send(200, {"ok": True, "data": stats_summary()})
            return
        if path == "/api/taste":
            self._send(200, {"ok": True, "data": taste_profile()})
            return
        if path == "/api/genres":
            self._send(200, {"ok": True, "data": genre_list()})
            return
        if path == "/api/styles":
            global _STYLE_ROUND
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if (q.get("refresh") or [""])[0] == "1":
                _STYLE_ROUND += 1
            try:
                self._send(200, {"ok": True, "data": style_playlists(
                    int((q.get("cards") or [8])[0]),
                    int((q.get("per") or [6])[0]),
                    _STYLE_ROUND)})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/recommend":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fresh = (q.get("refresh") or [""])[0] == "1"
            try:
                self._send(200, {"ok": True, "data": recommend_candidates(
                    int((q.get("limit") or [12])[0]), fresh=fresh)})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/cache":
            st = CACHE.stat()
            day = time.strftime("%Y-%m-%d")
            plays = STORE.data.get("plays") or {}
            heard = 0
            for v in plays.values():
                if not isinstance(v, dict):
                    continue
                days = v.get("days")
                if isinstance(days, dict) and _int_setting(days, day, 0) > 0:
                    heard += 1
            cfg = STORE.data["settings"]
            st.update({
                "day": day,
                "heard": heard,
                "min_plays": _int_setting(cfg, "cache_min_plays", 2),
                "week_plays": _int_setting(cfg, "cache_week_plays", 5),
                "keep_days": _int_setting(cfg, "cache_keep_days", 3),
                "limit": _int_setting(cfg, "cache_limit_mb", 2048),
            })
            self._send(200, {"ok": True, "data": st})
            return
        if path == "/api/local/list":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self._send(200, {"ok": True,
                                 "data": local_scan(force=(q.get("refresh") or [""])[0] == "1")})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/local/file":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            p = local_resolve((qs.get("p") or [""])[0])
            if not p:
                self._send(404, {"error": "文件不在下载目录里（或已被移动 / 删除）"})
                return
            self._serve_cached(p, os.path.splitext(p)[1].lstrip(".").lower())
            return
        if path == "/api/local/cover":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            blob, mime = local_cover((qs.get("p") or [""])[0])
            if not blob:
                self._send(404, {"error": "no cover"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mime or "image/jpeg")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            try:
                self.wfile.write(blob)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if path == "/api/lyric/now":
            self._send(200, {"ok": True, "data": lyric_now_get()})
            return
        if path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        self._static(path)

    def do_POST(self):
        if not self._local_request():
            self._send(403, {"error": "forbidden"})
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/rpc":
                body = self._json_body()
                fn = body.get("fn")
                if fn not in RPC_TABLE:
                    self._send(400, {"error": f"未知方法 {fn}"})
                    return
                self._send(200, {"ok": True, "data": RPC_TABLE[fn](body.get("params") or {})})
                return

            if path == "/api/store":
                try:
                    clen = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    clen = 0
                if clen > 4 * 1024 * 1024:
                    # 用户数据的单次写入不该有 MB 级；放进来只会把 data.json 撑爆。
                    # 先把已声明的 body 读掉再回 413：直接关连接会让还在发大
                    # body 的客户端收到 RST，拿不到「请求体过大」这句人话。
                    # 但 Content-Length 可能是谎报的（客户端根本不发 body），
                    # 所以设 0.6s 读超时，骗人的长度不能把服务线程挂死。
                    left = min(clen, 8 * 1024 * 1024)
                    sock = self.connection
                    old_to = sock.gettimeout()
                    try:
                        sock.settimeout(0.6)
                        while left > 0:
                            chunk = self.rfile.read(min(65536, left))
                            if not chunk:
                                break
                            left -= len(chunk)
                    except OSError:
                        pass
                    finally:
                        try:
                            sock.settimeout(old_to)
                        except OSError:
                            pass
                    self._send(413, {"ok": False, "error": "请求体过大"})
                    return
                body = self._json_body()
                op = body.get("op")
                if op not in STORE_OPS:
                    # 空 body / 非法 JSON / 拼错 op 以前会被当成「读取快照」，
                    # 回一个 ok:true 还白写一次盘 —— 直接明确拒绝。
                    self._send(400, {"ok": False, "error": "未知操作 %r" % (op,)})
                    return
                out = {"ok": True, "data": STORE.apply(op, body.get("payload"))}
                if op != "load":
                    sync_engine.touch()      # 让自动模式知道「本地变了」
                self._send(200, out)
                return

            # ---------------------------------------------------------- AutoEq 导入
            # 纯解析查询，不写盘。段表校验口径在 parse_autoeq_apo 里与
            # settings eq_bands 同源；解析失败 400 + 明确原因，前端拿到的
            # 要么是可预览的段表、要么是能直接展示给用户的错误文案。
            if path == "/api/eq/parse":
                try:
                    clen = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    clen = 0
                if clen > 1024 * 1024:
                    self._send(413, {"ok": False, "error": "请求体过大"})
                    return
                try:
                    data = parse_autoeq_apo((self._json_body() or {}).get("text"))
                except ValueError as e:
                    self._send(400, {"ok": False, "error": str(e)})
                    return
                self._send(200, {"ok": True, "data": data})
                return

            # ---------------------------------------------------------- 多端同步
            # 全部在后台线程做阻塞 IO，前端拿到的是结果不是超时。规则：
            # test / push / pull 只读或只写远端与本机配置；真正改本地数据只有 commit。
            if path in ("/api/sync/config", "/api/sync/test", "/api/sync/push",
                        "/api/sync/pull", "/api/sync/commit"):
                body = self._json_body() or {}
                fn = {
                    "config": sync_engine.configure,
                    "test": lambda _b: sync_engine.test(),
                    "push": lambda _b: sync_engine.push(),
                    "pull": lambda _b: sync_engine.pull(),
                    "commit": sync_engine.commit,
                }[path.rsplit("/", 1)[-1]]
                try:
                    self._send(200, {"ok": True, "data": fn(body)})
                except sync_engine.DavError as e:
                    self._send(200, {"ok": False, "error": str(e)})
                except Exception as e:                      # noqa: BLE001
                    self._send(200, {"ok": False, "error": "同步失败：%s" % e})
                return

            # ---------------------------------------------------------- K 歌作品
            # 上传是 raw body（WAV 字节），元数据走 query —— 前端把两样一次送来，
            # 服务端先落盘再登记 meta，顺序反了会出现「列表里有、文件没有」的僵尸条目。
            if path == "/api/works/upload":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                wid = work_new_id()
                length = int(self.headers.get("Content-Length") or 0)
                if not (44 <= length <= WORK_MAX_BYTES):
                    self._send(200, {"ok": False, "error": "录音大小不对（%d 字节）" % length})
                    return
                os.makedirs(WORKS_DIR, exist_ok=True)
                tmp = work_file(wid) + ".part"
                dst = work_file(wid)
                try:
                    left, received = length, 0
                    with open(tmp, "wb") as f:
                        while left > 0:
                            chunk = self.rfile.read(min(65536, left))
                            if not chunk:
                                break
                            f.write(chunk)
                            left -= len(chunk)
                            received += len(chunk)
                    if received != length:
                        raise OSError("上传不完整（%d/%d）" % (received, length))
                    os.replace(tmp, dst)
                except Exception as e:                      # noqa: BLE001
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    self._send(200, {"ok": False, "error": "保存录音失败：%s" % e})
                    return
                meta = work_meta(qs, os.path.getsize(dst))
                meta["id"] = wid
                STORE.apply("work_add", meta)
                self._send(200, {"ok": True, "data": meta})
                return

            if path == "/api/works/delete":
                wid = str((self._json_body() or {}).get("id") or "")
                if not WORK_ID_RE.match(wid):
                    self._send(200, {"ok": False, "error": "作品 id 不合法"})
                    return
                try:
                    os.remove(work_file(wid))
                except OSError:
                    pass                                    # 文件早没了也算删成功
                STORE.apply("work_remove", {"id": wid})
                self._send(200, {"ok": True})
                return

            if path == "/api/update/download":
                # 应用内下载新版本：只下载 + 校验，替换由用户手动完成
                body = self._json_body()
                url = str(body.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    self._send(400, {"ok": False, "error": "只支持 http(s) 直链"})
                    return
                if UPDATE_DL["state"] == "running":
                    self._send(409, {"ok": False, "error": "已有下载在进行"})
                    return
                threading.Thread(target=_download_update, name="update-dl",
                                 args=(url, str(body.get("sha256") or "").strip()),
                                 daemon=True).start()
                self._send(200, {"ok": True, "data": dict(UPDATE_DL)})
                return

            if path == "/api/import/preview":
                # dry-run：只解析 + 三桶判定，绝不写用户数据
                self._send(200, {"ok": True,
                                 "data": importer.start_preview(self._json_body())})
                return

            if path == "/api/import/cancel":
                tid = str((self._json_body() or {}).get("id") or "")
                self._send(200, {"ok": True, "data": {"cancelled": importer.cancel(tid)}})
                return

            if path == "/api/import/commit":
                body = self._json_body() or {}
                # 只接受本项目能播放的两个音源；QQ / 酷狗的原生 id 绝不能进歌单，
                # 否则会出现一条永远播不了、也换不了源的僵尸曲目。
                items = [x for x in (body.get("items") or [])
                         if isinstance(x, dict) and x.get("platform") in PLATFORMS
                         and str(x.get("id") or "").strip()]
                if not items:
                    self._send(200, {"ok": False, "error": "没有勾选任何可导入的曲目"})
                    return
                target = str(body.get("target_id") or "").strip()
                if target and not any(p.get("id") == target
                                      for p in STORE.data.get("playlists") or []):
                    self._send(200, {"ok": False, "error": "目标歌单不存在"})
                    return
                if not target:
                    name = str(body.get("name") or "").strip()[:60] or "导入歌单"
                    src = body.get("source") if isinstance(body.get("source"), dict) else {}
                    src = dict(src)
                    src["imported_at"] = int(time.time())
                    STORE.apply("playlist_create", {"name": name, "source": src})
                    pls = STORE.data.get("playlists") or []
                    target = pls[-1]["id"] if pls else ""
                if not target:
                    self._send(200, {"ok": False, "error": "歌单创建失败"})
                    return
                pl = next((p for p in STORE.data["playlists"]
                           if p.get("id") == target), None)
                before = len((pl or {}).get("items") or [])
                STORE.apply("playlist_add", {"id": target, "items": items})
                pl = next((p for p in STORE.data["playlists"]
                           if p.get("id") == target), None)
                self._send(200, {"ok": True, "data": {
                    "id": target, "name": (pl or {}).get("name") or "",
                    "added": len((pl or {}).get("items") or []) - before,
                    "total": len((pl or {}).get("items") or [])}})
                return

            if path == "/api/feedback":
                body = self._json_body()
                self._send(200, {"ok": True,
                                 "data": STORE.apply("feedback_set", body)})
                _RECO_CACHE.update(at=0, data=None, pool=None)   # 反馈变了，候选立即作废
                return

            if path == "/api/taste_note":
                self._send(200, {"ok": True, "data": STORE.apply(
                    "taste_note", self._json_body())})
                _RECO_CACHE.update(at=0, data=None, pool=None)
                return
            if path == "/api/taste_genres":
                self._send(200, {"ok": True, "data": STORE.apply(
                    "taste_genres", self._json_body())})
                _RECO_CACHE.update(at=0, data=None, pool=None)
                return

            if path == "/api/probe":
                body = self._json_body()
                items = body.get("items") or []
                limit = int(body.get("limit") or 64)
                try:
                    data = probe_batch(items, limit=limit,
                                       workers=int(body.get("workers") or 8))
                    self._send(200, {"ok": True, "data": data})
                except Exception as e:  # noqa: BLE001
                    self._send(200, {"ok": False, "error": str(e)})
                return

            if path == "/api/play":
                self._send(200, record_play(self._json_body()))
                return

            if path == "/api/lyric/now":
                # 桌面歌词小窗的数据源：主页面推当前行，纯内存中继
                self._send(200, lyric_now_set(self._json_body()))
                return

            if path in ("/api/download", "/api/downloads/add"):
                body = self._json_body()
                items = body.get("items")
                if not items:
                    one = body.get("item") or body
                    items = [one] if one.get("id") else []
                if not items:
                    self._send(200, {"ok": False, "error": "缺少歌曲信息"})
                    return
                ids = DM.add_many(items, body.get("quality"), body.get("dir"))
                if not ids:
                    self._send(200, {"ok": False, "error": "缺少歌曲信息"})
                    return
                self._send(200, {"ok": True, "data": {"ids": ids}})
                return

            if path in ("/api/downloads/cancel", "/api/downloads/retry", "/api/downloads/remove"):
                body = self._json_body()
                tid = body.get("id")
                fn = {"cancel": DM.cancel, "retry": DM.retry,
                      "remove": DM.remove}[path.rsplit("/", 1)[1]]
                self._send(200, {"ok": bool(fn(tid))})
                return

            if path == "/api/cache/clear":
                body = {}
                try:
                    body = self._json_body()
                except Exception:
                    pass
                st = CACHE.clear((body or {}).get("tier") or "all")
                self._send(200, {"ok": True, "data": st})
                return

            if path == "/api/save-lyric":
                body = self._json_body()
                folder = STORE.data["settings"].get("download_dir") or default_download_dir()
                base = render_template(STORE.data["settings"].get("name_template"),
                                       {"name": body.get("name"), "artist": body.get("artist")})
                os.makedirs(folder, exist_ok=True)
                path_lrc = os.path.join(folder, base + ".lrc")
                with open(path_lrc, "w", encoding="utf-8") as f:
                    f.write(body.get("content") or "")
                self._send(200, {"ok": True, "data": {"path": path_lrc}})
                return

            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(200, {"ok": False, "error": str(e)})


def port_busy(port):
    """端口是否已经被别的进程占着。

    Windows 上 SO_REUSEADDR 允许同端口重复绑定，不查就会静默起第二个实例：
    两个进程各持一份完整 store，谁后写谁覆盖磁盘，设置会莫名其妙回滚。
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def serve(port=0):
    if port and port_busy(port):
        raise OSError("端口 %d 已被占用（Elysium 可能已经在运行）" % int(port))
    # 默认 listen backlog 只有 5：页面一次带几十张封面 / K 歌分析并发起流时，
    # 多出来的连接会被系统直接 RST（客户端看到的是「连不上」而不是排队）。
    # 128 对单机应用足够，且不需要额外线程。
    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128

        def handle_error(self, request, client_address):
            # 页面刷新 / 切歌 / 关窗会先把连接掐掉，读请求行时报
            # ConnectionReset 是常态。socketserver 默认把整段 traceback
            # 打到 stderr —— 正常使用下的日志会被这类噪音淹没。
            import sys as _sys
            etype = _sys.exc_info()[0]
            if etype and issubclass(etype, (ConnectionResetError, ConnectionAbortedError,
                                            BrokenPipeError, TimeoutError)):
                return
            super().handle_error(request, client_address)

    httpd = _Server(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=update_tracker, name="update-tracker", daemon=True).start()
    return httpd, httpd.server_address[1]


# --------------------------------------------------------------------------
# pywebview 桥
# --------------------------------------------------------------------------
class Bridge:
    def choose_dir(self):
        import webview
        win = webview.windows[0] if webview.windows else None
        if not win:
            return ""
        result = win.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=STORE.data["settings"].get("download_dir") or default_download_dir())
        if not result:
            return ""
        path = result[0] if isinstance(result, (list, tuple)) else result
        STORE.apply("settings", {"download_dir": path})
        return path

    def pick_dir(self):
        """选一个目录，只回路径、不动任何设置 —— 给「添加曲库目录」用。"""
        import webview
        win = webview.windows[0] if webview.windows else None
        if not win:
            return ""
        try:
            result = win.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=STORE.data["settings"].get("download_dir") or default_download_dir())
        except Exception:                                   # noqa: BLE001
            return ""
        if not result:
            return ""
        path = result[0] if isinstance(result, (list, tuple)) else result
        return str(path or "")

    def mini_mode(self, on=True):
        """迷你条（会话级，不落盘）：缩成小窗贴右下角，只留播放条。"""
        win = _TRAY.get("win")
        if not win:
            return False
        try:
            if on:
                try:
                    Bridge._geo = (win.width or NORMAL_SIZE[0], win.height or NORMAL_SIZE[1])
                except Exception:                       # noqa: BLE001
                    Bridge._geo = NORMAL_SIZE
                win.restore()
                win.resize(*MINI_SIZE)
                try:
                    win.on_top = True
                except Exception:                       # noqa: BLE001
                    pass
                try:
                    import webview
                    sc = webview.screens[0]
                    win.move(max(0, sc.x + sc.width - MINI_SIZE[0] - 36),
                             max(0, sc.y + sc.height - MINI_SIZE[1] - 96))
                except Exception:                       # noqa: BLE001
                    pass
                return True
            geo = getattr(Bridge, "_geo", NORMAL_SIZE) or NORMAL_SIZE
            win.resize(int(geo[0]), int(geo[1]))
            try:
                win.on_top = False
            except Exception:                           # noqa: BLE001
                pass
            win.show()
            return True
        except Exception:                               # noqa: BLE001
            return False

    def open_path(self, path):
        try:
            if path and os.path.isdir(path):
                os.startfile(path)  # noqa: S606
                return True
            if path and os.path.isfile(path):
                os.startfile(os.path.dirname(path))  # noqa: S606
                return True
        except Exception:
            return False
        return False

    def reveal(self, path):
        return self.open_path(path)

    def open_url(self, url):
        """只放行 http(s)，交给系统默认浏览器（更新发布页等外链）。"""
        u = str(url or "")
        if not u.startswith(("http://", "https://")):
            return False
        try:
            webbrowser.open(u)
            return True
        except Exception:
            return False

    # ---- 窗口壳 API（只服务 window-chrome.js）------------------------------
    def win_material_state(self):
        """前端据此决定主题底色是否半透明（L3 系统材质 / L2 CSS 玻璃层）。"""
        return dict(_CHROME_STATE)

    def win_minimize(self):
        try:
            import webview
            if webview.windows:
                webview.windows[0].minimize()
                return True
        except Exception:
            pass
        return False

    def win_toggle_maximize(self):
        try:
            import ctypes
            import webview
            win = webview.windows[0] if webview.windows else None
            if win is None:
                return False
            hwnd = _chrome_hwnd()
            zoomed = bool(hwnd and ctypes.windll.user32.IsZoomed(
                ctypes.c_void_p(hwnd)))
            if zoomed:
                win.restore()
            else:
                win.maximize()
            return not zoomed
        except Exception:
            return False

    def win_is_maximized(self):
        try:
            import ctypes
            hwnd = _chrome_hwnd()
            return bool(hwnd and ctypes.windll.user32.IsZoomed(
                ctypes.c_void_p(hwnd)))
        except Exception:
            return False

    def win_close(self):
        try:
            import webview
            if webview.windows:
                webview.windows[0].destroy()
                return True
        except Exception:
            pass
        return False

    def win_set_theme(self, dark=True):
        """前端切主题时同步 DWM 暗色，并把 Acrylic 重申回来。"""
        hwnd = _chrome_hwnd()
        if not hwnd:
            return False
        _dwm_set(hwnd, 20, 1 if dark else 0)
        if _CHROME_STATE.get("acrylic"):
            _dwm_set(hwnd, 38, 3)
        return True

    # ---- 桌面歌词小窗 ------------------------------------------------------
    def lyric_window(self, on=True):
        """开 / 关桌面歌词小窗（仅桌面端；--serve 与 Android 没有窗口栈，no-op）。"""
        global _LYRIC_WIN
        try:
            import webview
            if not on:
                if _LYRIC_WIN is not None:
                    try:
                        _LYRIC_WIN.destroy()
                    except Exception:                       # noqa: BLE001
                        pass
                    _LYRIC_WIN = None
                return True
            if _LYRIC_WIN is not None:
                return True
            if not _BASE_URL:
                return False
            x = y = None
            try:                                            # 贴屏幕右下角
                sc = webview.screens[0]
                x, y = sc.x + sc.width - 620, sc.y + sc.height - 190
            except Exception:                               # noqa: BLE001
                pass
            kw = {"width": 600, "height": 96, "on_top": True,
                  "frameless": True, "easy_drag": True,
                  "background_color": "#101014", "js_api": LyricApi()}
            if x is not None:
                kw.update(x=x, y=y)
            _LYRIC_WIN = webview.create_window(
                "Elysium 歌词", _BASE_URL + "lyrics.html", **kw)
            return True
        except Exception:                                   # noqa: BLE001
            return False


class LyricApi:
    """歌词小窗自己的桥：只有一个「关掉自己」。"""

    def close(self):
        global _LYRIC_WIN
        try:
            import webview
            for w in list(webview.windows):
                if getattr(w, "title", "") == "Elysium 歌词":
                    w.destroy()
            _LYRIC_WIN = None
            return True
        except Exception:                                   # noqa: BLE001
            return False


def startup_bg():
    """窗口首帧底色必须跟用户上次选的主题一致。

    pywebview 的 background_color 只在 WebView2 还没画出第一帧时露出来，
    写死浅色的话，深色主题用户每次启动都会先闪一下白底 —— 打包成 exe 后特别明显。
    跟随系统（auto）时读注册表里的 AppsUseLightTheme。
    """
    theme = (STORE.data.get("settings") or {}).get("theme") or "dark"
    if theme == "auto":
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                theme = "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        except OSError:
            theme = "dark"
    return "#fbfbfd" if theme == "light" else "#08080b"


# --------------------------------------------------------------------------
# 窗口壳（无边框 + Win11 系统材质）
#
# 这一段是 Windows 桌面宿主专属，APK / --serve 浏览器调试不会执行。
# 全部都是「能力增强」：拿不到句柄、DWM 不支持、材质被环境变量关闭时
# 一律静默降级到前端 CSS 壳，不影响启动、播放与下载。
# --------------------------------------------------------------------------
_CHROME_STATE = {"platform": "desktop", "acrylic": False, "material": False}


def _chrome_hwnd():
    """原生窗口句柄；拿不到返回 0。winforms 是 Form.Handle.ToInt32()。"""
    try:
        import webview
        win = webview.windows[0] if webview.windows else None
        if win is None or getattr(win, "native", None) is None:
            return 0
        return int(win.native.Handle.ToInt32())
    except Exception:
        return 0


def _dwm_set(hwnd, attr, value):
    """DwmSetWindowAttribute 的防御式封装，返回是否成功（HRESULT==0）。"""
    if not hwnd or os.name != "nt":
        return False
    try:
        import ctypes
        v = ctypes.c_int(int(value))
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(int(attr)),
            ctypes.byref(v), ctypes.sizeof(v))
        return hr == 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# 托盘 + 迷你条（P6）
# --------------------------------------------------------------------------
_TRAY = {"icon": None, "win": None}
MINI_SIZE = (430, 138)
NORMAL_SIZE = (1240, 820)


def _tray_image():
    """托盘图标：优先用应用自己的 logo，取不到就退回一块纯色，绝不因此起不来。"""
    try:
        from PIL import Image
    except Exception:                                       # noqa: BLE001
        return None
    for name in ("app.png", "app.ico"):
        p = os.path.join(resource_dir(), name)
        if os.path.isfile(p):
            try:
                im = Image.open(p)
                im.load()
                return im if im.mode == "RGBA" else im.convert("RGBA")
            except Exception:                               # noqa: BLE001
                continue
    return Image.new("RGBA", (64, 64), (255, 95, 141, 255))


def _tray_js(code):
    win = _TRAY.get("win")
    if not win:
        return False
    try:
        win.evaluate_js(code)
        return True
    except Exception:                                       # noqa: BLE001
        return False


def _tray_show(icon=None, item=None):
    win = _TRAY.get("win")
    if not win:
        return
    try:
        win.show()
        win.restore()
    except Exception:                                       # noqa: BLE001
        pass


def _tray_quit(icon=None, item=None):
    cur = _TRAY.get("icon")
    try:
        if cur:
            cur.stop()
    except Exception:                                       # noqa: BLE001
        pass
    win = _TRAY.get("win")
    # 先摘引用：_on_closing 看到 win 为空就知道这次是「真退出」，不再拦
    _TRAY["win"] = None
    try:
        if win:
            win.destroy()
    except Exception:                                       # noqa: BLE001
        pass


def start_tray(win):
    """建托盘图标。装不上 pystray / 起不来就静默降级，主功能一点不受影响。"""
    if _TRAY["icon"] is not None or os.name != "nt":
        return False
    try:
        import pystray
        from pystray import Menu, MenuItem
    except Exception:                                       # noqa: BLE001
        return False
    image = _tray_image()
    if image is None:
        return False
    _TRAY["win"] = win
    menu = Menu(
        MenuItem("显示主窗口", _tray_show, default=True),
        Menu.SEPARATOR,
        MenuItem("播放 / 暂停", lambda i, it: _tray_js(
            "document.querySelector('#playBtn').click()")),
        MenuItem("上一首", lambda i, it: _tray_js("playNext(-1)")),
        MenuItem("下一首", lambda i, it: _tray_js("playNext(1)")),
        Menu.SEPARATOR,
        MenuItem("退出 Elysium", _tray_quit),
    )
    try:
        icon = pystray.Icon("Elysium", image, "Elysium", menu)
        icon.run_detached()
    except Exception:                                       # noqa: BLE001
        return False
    _TRAY["icon"] = icon
    return True


def _on_closing(*_args):
    """点关闭按钮：按设置决定「退出」还是「收进托盘」。

    pywebview 的 closing 事件不传窗口实参（新版直接零参调用），窗口从
    _TRAY 取；写成必填 win 会在关窗时抛 TypeError，托盘隐藏路径静默失效。
    托盘没起来时一律真退出 —— 否则窗口一藏就再也叫不出来了，「关不掉」
    比「关了就退」更吓人。
    """
    win = _TRAY.get("win")
    if win is None:
        return True
    if (STORE.data.get("settings") or {}).get("close_behavior") != "tray":
        return True
    if _TRAY.get("icon") is None:
        return True
    try:
        win.hide()
    except Exception:                                       # noqa: BLE001
        return True
    return False


def _apply_window_chrome(win=None):
    """窗口 ready 后补齐 ZCode 式质感，由 webview.start(func=...) 调用。

    38 = DWMWA_SYSTEMBACKDROP_TYPE, 3 = DWMSBT_TRANSIENTWINDOW (Desktop Acrylic)
    33 = DWMWA_WINDOW_CORNER_PREFERENCE, 2 = DWMWCP_ROUND
    20 = DWMWA_USE_IMMERSIVE_DARK_MODE
     2 = DWMWA_NCRENDERING_POLICY

    pywebview 自己在 update_title_bar_theme() 里会写 38=2(Mica)/1(NONE)，
    所以必须等窗口创建完再覆盖成 Acrylic。系统主题变化事件可能再写回 Mica，
    那依然有材质、界面不受影响；前端每次切主题也会经 win_set_theme 重申。
    """
    if win is None:
        try:
            import webview
            win = webview.windows[0] if webview.windows else None
        except Exception:                                   # noqa: BLE001
            win = None
    if win is not None:
        _TRAY["win"] = win
        try:
            win.events.closing += _on_closing
        except Exception:                                   # noqa: BLE001
            pass
        if (STORE.data.get("settings") or {}).get("tray") is not False:
            start_tray(win)

    hwnd = _chrome_hwnd()
    if not hwnd:
        return
    _CHROME_STATE["acrylic"] = _dwm_set(hwnd, 38, 3)
    _dwm_set(hwnd, 33, 2)
    _dwm_set(hwnd, 20, 0 if startup_bg() == "#fbfbfd" else 1)
    if os.environ.get("ELYSIUM_WINDOW_MATERIAL") == "off":
        _dwm_set(hwnd, 38, 1)          # DWMSBT_NONE：一键回退，便于发布排查
        _CHROME_STATE["acrylic"] = False
    _CHROME_STATE["material"] = bool(_CHROME_STATE["acrylic"])
    if _CHROME_STATE["material"]:
        # transparent=True 时 pywebview 跳过自己的 shadow 分支，这里补上
        # 1px 非客户区扩展，让 DWM 照样画窗口阴影。
        try:
            import ctypes

            class _MARGINS(ctypes.Structure):
                _fields_ = [("cxLeftWidth", ctypes.c_int),
                            ("cxRightWidth", ctypes.c_int),
                            ("cyTopHeight", ctypes.c_int),
                            ("cyBottomHeight", ctypes.c_int)]

            m = _MARGINS(1, 1, 1, 1)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                ctypes.c_void_p(hwnd), ctypes.byref(m))
            _dwm_set(hwnd, 2, 2)
        except Exception:
            pass


def main():
    # 导入模块不 import main（避免循环依赖），严格判定统一复用这里的函数，
    # 保证「歌单导入」与「换源 / 歌手归类」是同一套口径，不会两套标准漂移。
    importer.bind(search_all=search_all, billed_as=_billed_as,
                  fold_text=_fold_text, resource_dir=resource_dir)
    sync_engine.start_auto()      # 自动模式默认关，开了才有活干（线程自己判断）
    args = sys.argv[1:]
    port_arg = 0
    for a in args:
        if a.startswith("--port="):
            port_arg = int(a.split("=", 1)[1])
    if os.environ.get("FLACMUSIC_PORT"):
        port_arg = int(os.environ["FLACMUSIC_PORT"])
    headless = "--browser" in args or "--serve" in args
    try:
        httpd, port = serve(port_arg)
    except OSError as e:
        print("[%s] 启动失败：%s" % (APP_NAME, e), flush=True)
        raise SystemExit(2)
    try:
        with open(os.path.join(data_dir(), "port.txt"), "w", encoding="utf-8") as f:
            f.write(str(port))
    except OSError:
        pass
    url = f"http://127.0.0.1:{port}/"
    global _BASE_URL
    _BASE_URL = url
    if headless:
        print(f"[{APP_NAME}] 本地服务已启动：{url}", flush=True)
        if "--browser" in args:
            webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    import webview
    # 窗口壳：无系统边框 + WebView2 透明背景，让 Win11 Desktop Acrylic 透出来。
    # easy_drag=False 是关键：开启后 pywebview 会给整个窗口加 mousedown，
    # 列表拖拽、进度条、文本选择会被窗口移动吞掉；改由标题栏的
    # .pywebview-drag-region 精确承担拖拽（等价于 ZCode 的 app-region:drag）。
    # ELYSIUM_WINDOW_MATERIAL=off 可一键回到不透明窗口，便于发布排查。
    material_on = os.environ.get("ELYSIUM_WINDOW_MATERIAL") != "off"
    webview.create_window(APP_TITLE, url, width=1240, height=820,
                          min_size=(960, 640), background_color=startup_bg(),
                          js_api=Bridge(), text_select=True,
                          frameless=True, easy_drag=False,
                          transparent=material_on, shadow=True)
    webview.start(_apply_window_chrome, debug="--debug" in args)
    # 窗口是应用的全部 UI：它一关，进程必须跟着死。
    # 后台是 daemon 线程 + 两个模块级 ThreadPoolExecutor（_CRAWL/_PROBE，
    # 线程非 daemon）——池里若有任务卡在挂起的网络上，解释器 shutdown 会
    # 无限期等它，窗口没了进程还在（桌面版僵尸进程实测）。
    # data.json / 索引都走 tmp+replace 原子写，硬退不毁库。
    os._exit(0)


if __name__ == "__main__":
    main()


def serve_android(port=18810):
    """Chaquopy 入口：Java 侧在后台线程调用，WebView 加载 http://127.0.0.1:<port>/"""
    os.environ["FLACMUSIC_PORT"] = str(port)
    sys.argv = ["main.py", "--serve"]
    main()
