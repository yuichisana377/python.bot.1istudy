import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
from datetime import date as _date
from flask import Flask, request, jsonify, make_response, redirect, Response
from flask_cors import CORS
from threading import Thread, Lock
from pytz import timezone
import json
import os
import requests
import base64
import asyncio
import time
import hashlib
import hmac
import re
import secrets
import random
import difflib
import queue
import subprocess
import shutil
from urllib.parse import urlencode

# ================================
#  設定
# ================================
# ★ 変更：以前はデータの永続化先としてGitHubリポジトリ（Contents API）を
#   使っていたが、Ubuntuサーバー上で常時稼働させる構成に合わせて、
#   すべてサーバーのローカルディスク（DATA_DIR配下）に保存する方式に変更した。
#   ・DATA_DIR は環境変数で変更可能（未設定時はこのスクリプトと同じ場所の
#     "data" フォルダを使う）。
#   ・systemd等で再起動してもデータが消えないよう、DATA_DIRは外部ボリュームや
#     永続的なディスク上のパスを指定することを推奨する。
DATA_DIR            = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
print(f"[INFO] DATA_DIR = {os.path.abspath(DATA_DIR)}")
try:
    _existing = os.listdir(DATA_DIR)
    print(f"[INFO] DATA_DIR 内のファイル数: {len(_existing)}"
          + (f"（例: {_existing[:5]}）" if _existing else "（空です。データを配置しましたか？）"))
except OSError as e:
    print(f"[WARN] DATA_DIR の読み取りに失敗しました: {e}")
TOKEN               = os.getenv("TOKEN")
SUBJECT_CATEGORY_ID = os.getenv("SUBJECT_CATEGORY_ID")  # カテゴリID（優先）
SUBJECT_CATEGORY    = os.getenv("SUBJECT_CATEGORY")     # カテゴリ名（フォールバック）
JST = timezone("Asia/Tokyo")

# ================================
#  ★ Discord OAuth2（「Discordでログイン」方式のアカウント連携）
#  ─────────────────────────────
#  コード手入力方式（/id連携）に加えて、生徒がボタン一つでDiscordの
#  認可画面から直接連携できるようにするための設定。
#  ・DISCORD_CLIENT_ID     : 公開情報なのでハードコードのフォールバックでも問題ない
#  ・DISCORD_CLIENT_SECRET : 機密情報。必ずサーバーの環境変数に設定すること。
#                            これがコードに書かれていたり漏れたりすると、
#                            第三者がこのアプリになりすましてDiscordユーザーの
#                            情報を取得できてしまう。
#  ・DISCORD_OAUTH_REDIRECT_URI : Discord Developer Portal の
#                            OAuth2 → Redirects に登録したURLと
#                            1文字違わず完全一致している必要がある。
#                            ★ Ubuntuサーバーに移行する場合は、実際に
#                              このサーバーへアクセスできるURL（独自ドメイン
#                              やサーバーのグローバルIPなど）を環境変数
#                              DISCORD_OAUTH_REDIRECT_URI に設定し、
#                              Discord Developer Portal 側のRedirect URLも
#                              同じ値に変更すること。
# ================================
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "1515358957542047975")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_OAUTH_REDIRECT_URI = os.getenv(
    "DISCORD_OAUTH_REDIRECT_URI",
    "https://chiro-ubuntuserver.tail1130ba.ts.net/discord_oauth_callback"
)
if not DISCORD_CLIENT_SECRET:
    print("[WARN] 環境変数 DISCORD_CLIENT_SECRET が未設定です。"
          "Discord OAuth連携（/discord_oauth_start, /discord_oauth_callback）は無効化されます。"
          "使う場合はサーバーの環境変数にDISCORD_CLIENT_SECRETを設定してください。")

# --- 通生/寮生 振り分け用の絵文字 ---
EMOJI_COMMUTER = "🚃"  # 通生
EMOJI_DORM     = "🏠"  # 寮生

scheduler = AsyncIOScheduler(timezone=JST)

# ================================
#  Flask アプリ
# ================================
app = Flask("")
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

# ★ 追加：ブラウザ（特にChrome）がこれらのGET APIレスポンスを
#   ディスクキャッシュに保存してしまい、新しく作成・公開したデッキや
#   フォルダ、科目一覧がクライアント側の一覧にすぐ反映されない不具合の
#   根本対策。フロント側（Cardmaker.js）でも fetch に cache: 'no-store' を
#   付けているが、サーバー側でも明示的に Cache-Control: no-store を返す
#   ことで、ブラウザ・CDN・中間プロキシのどこでキャッシュされても
#   確実に最新のデータが返るようにする。
NO_CACHE_PATHS = {
    "/list_cards",
    "/get_card_set",
    "/list_folders",
    "/list_order",
    "/channels",
    "/list_in_progress",
    "/timer_state",
    "/get_study_data",
    "/deck_understanding",
    "/list_notices",
    "/quiz_state",
}

@app.after_request
def add_no_cache_headers(response):
    if request.method == "GET" and request.path in NO_CACHE_PATHS:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ★ 追加：一般的なWebアプリのベースラインとして推奨される、
#   HTTPレスポンスの標準的なセキュリティヘッダー。
#   ・X-Content-Type-Options: ブラウザがContent-Typeを勝手に
#     「推測」して実行してしまう（MIMEスニッフィング）のを防ぐ。
#   ・X-Frame-Options: 他サイトの<iframe>にこのAPIの応答を埋め込ませない
#     （クリックジャッキング対策。JSON APIなので実害は薄いが定番として）。
#   ・Referrer-Policy: 他サイトへ移動する際、URL（クエリにトークン等が
#     含まれる可能性がある）を丸ごとreferrerとして渡さないようにする。
@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        res = make_response()
        res.headers["Access-Control-Allow-Origin"]  = "*"
        res.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return res, 200

# ================================
#  ★ リアルタイム更新通知（Server-Sent Events）
#  ─────────────────────────────
#  以前は各ページ（予定一覧・時間割・CardMaker・学習ログなど）が10秒おきに
#  ポーリングして内容のハッシュを比較する方式だったため、変更が画面に
#  反映されるまで最大10秒のタイムラグがあった。実際には常時稼働している
#  サーバーがあるので、データが保存された瞬間にpushで知らせて即座に
#  再取得させる方式に変える。
#  ・スレッド安全なQueueを使った単純なpub/sub（外部ミドルウェアは使わない）。
#  ・guild_idごとに購読者（Queue）の集合を持つ。cards/folders/order のように
#    guildをまたいで共有されるデータは guild_id=None で全購読者に配信する。
#  ・接続は /events へのGETで張られ続ける（text/event-stream）。
#    ブラウザがタブを閉じる／リロードするとジェネレータが終了し、
#    finally節で自動的に購読解除される。
#  ・イベントの中身自体は「〇〇が変わった」という合図だけで、実際のデータは
#    含めない（受け取った側が、これまで通りの各GET APIで取りに行く）。
#    これにより、通知漏れ・順序の入れ替わりが起きても「取りに行けば必ず
#    最新の状態になる」という結果整合性が保たれ、実装がシンプルになる。
#  ・万一 /events の接続が切れていても（スリープ復帰直後など）画面が
#    永久に古いままにならないよう、フロント側では長めの間隔（60秒）の
#    フォールバックポーリングも残してある。
# ================================
EVENT_SUBSCRIBERS = {}  # guild_id(int) or None(全guild共有分) -> set[queue.Queue]
EVENT_SUBSCRIBERS_LOCK = Lock()
EVENT_KEEPALIVE_SEC = 20  # プロキシ（Tailscale等）による無通信タイムアウト切断を防ぐ

def notify_change(guild_id=None):
    """
    guild_id を指定：そのguildを購読しているクライアントにだけ通知する
    （予定・時間割・学習ログなど、guildごとのデータ用）。
    guild_id=None：全クライアントに通知する
    （CardMakerのカード・フォルダ・並び順など、guildをまたいで共有される
    データ用。guild_idの概念を持たないため）。
    """
    with EVENT_SUBSCRIBERS_LOCK:
        targets = list(EVENT_SUBSCRIBERS.get(guild_id, ()))
    for q in targets:
        try:
            q.put_nowait(1)
        except Exception:
            pass

@app.route("/events", methods=["GET"])
def sse_events():
    guild_id = request.args.get("guild_id")
    guild_id = int(guild_id) if guild_id else None

    q = queue.Queue()
    with EVENT_SUBSCRIBERS_LOCK:
        EVENT_SUBSCRIBERS.setdefault(guild_id, set()).add(q)
        # ついでに全guild共有分（None）も同じ接続で受け取れるようにする
        # （CardMaker側はguild_idを渡さないので既にNoneキーそのものだが、
        #  guild_id付きで接続しているページでも共有データの更新を拾えるように
        #  Noneキューにも同じQueueを登録しておく）。
        if guild_id is not None:
            EVENT_SUBSCRIBERS.setdefault(None, set()).add(q)

    def gen():
        try:
            while True:
                try:
                    q.get(timeout=EVENT_KEEPALIVE_SEC)
                    yield "data: changed\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"  # SSEコメント行。データ更新扱いにはならない
        finally:
            with EVENT_SUBSCRIBERS_LOCK:
                EVENT_SUBSCRIBERS.get(guild_id, set()).discard(q)
                if guild_id is not None:
                    EVENT_SUBSCRIBERS.get(None, set()).discard(q)

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # nginx等のプロキシでバッファリングされて遅延しないように
    })

@app.route("/")
def home():
    return "I'm alive"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()
    print("[INFO] Flask thread started")

# ================================
#  Discord Bot
# ================================
intents = discord.Intents.default()
intents.message_content = True
intents.presences = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================================
#  ローカルファイル ユーティリティ
#  ─────────────────────────────
#  以前はここでGitHub Contents APIを叩いてJSONファイルの読み書きを
#  行っていたが、Ubuntuサーバー上でローカルディスクに直接保存する方式に
#  変更した。呼び出し側のインターフェース（(data, sha) を返す・
#  sha衝突時は例外を送出する、など）は変更していないため、以降の
#  ロジックはそのまま動作する。
#  ・sha相当のものとして、保存されているファイル内容のSHA256ハッシュ値を
#    使う。GitHubの「誰かが先に更新していたら409で拒否する」という
#    楽観的排他制御を、ローカルファイルでも同じ考え方で再現している
#    （同時書き込みで片方の変更が消えるのを防ぐ）。
# ================================
class DataWriteError(Exception):
    pass

def _data_path(filename):
    return os.path.join(DATA_DIR, filename)

def _file_sha(raw_bytes):
    return hashlib.sha256(raw_bytes).hexdigest()

def local_get(filename):
    """filename の中身をJSONとして読み込み (data, sha) を返す。存在しなければ (None, None)。"""
    path = _data_path(filename)
    if not os.path.isfile(path):
        return None, None
    with open(path, "rb") as f:
        raw = f.read()
    try:
        data = json.loads(raw.decode("utf-8")) if raw.strip() else None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # ★ ここで失敗を握りつぶすと「読み込まれない（原因不明）」に見えて
        #   デバッグしづらいため、必ずログに出す。
        print(f"[ERROR] {path} の読み込みに失敗しました（JSON形式が壊れているか、"
              f"文字コードがUTF-8ではない可能性があります）: {e}")
        data = None
    return data, _file_sha(raw)

def _local_write_once(filename, content_obj, expected_sha=None):
    """expected_sha が指定されていて現在のファイル内容と一致しない場合は
    (False, 現在のsha) を返す（＝GitHubでいう409相当）。
    書き込みに成功した場合は (True, 新しいsha) を返す。"""
    path = _data_path(filename)
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    current_sha = None
    if os.path.isfile(path):
        with open(path, "rb") as f:
            current_sha = _file_sha(f.read())

    if expected_sha is not None and current_sha is not None and expected_sha != current_sha:
        return False, current_sha

    encoded = json.dumps(content_obj, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(encoded)
    os.replace(tmp_path, path)
    return True, _file_sha(encoded)

def local_put(filename, content_obj, sha=None):
    """
    ローカルディスクへの書き込み。失敗した場合は例外を送出する（以前は無視していた）。
    ・保存直前の内容が、渡された sha（前回読み込み時のハッシュ）と一致しない場合
      （＝他の誰かが先に保存した）は、最新の内容を読み直して1回だけ自動再試行する。
    ・それでも失敗する場合は DataWriteError を送出するので、呼び出し側で
      「保存に失敗しました」とユーザーに伝えられるようにする。
    """
    ok, info = _local_write_once(filename, content_obj, sha)
    if ok:
        return {"content": {"sha": info}}

    # 誰かが先に更新した（sha不一致）→ 無条件で1回だけ再試行
    ok2, info2 = _local_write_once(filename, content_obj, None)
    if ok2:
        return {"content": {"sha": info2}}

    raise DataWriteError(f"ローカル保存に失敗しました（再試行後も失敗）: {filename}")

async def async_local_get(filename):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, local_get, filename)

async def async_local_put(filename, content_obj, sha=None):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, local_put, filename, content_obj, sha)

# ================================
#  システム運用ログ（Webの「サービス情報」ページに表示する）
#  ─────────────────────────────────────────────
#  ・目的：これまでprintでしか見えなかった「何がいつ変更されたか」を、
#    ログインしていれば誰でもWeb上で確認できるようにする（利用者が11人の
#    小規模運用のため、閲覧に管理者権限のような特別な区分けは設けない）。
#  ・粒度：JSONファイルへの書き込み単位ではなく「1つのユーザー操作」単位で
#    1件にまとめる（例：勉強時間の記録は study_logs と points の2つの
#    JSONを書き換えるが、ログには1件だけ残す）。呼び出し側で、複数の保存
#    処理が終わったあとに1回だけ log_event() を呼ぶ形にすること。
#  ・Discord ID・本名など、ニックネーム以上に個人を特定できる情報は記録しない。
#    ニックネームは他の画面（学習ログ・カード公開者名等）でも既に表示されて
#    いるため、実行者として記録してよいことになっている（2026/08/19、
#    ユーザーの要望により追加。バックアップ等サーバー主導の処理は
#    実行者が存在しないため actor=None のまま記録する）。
#  ・「本人にのみ表示される情報」は運用ログに残さない（2026/08/19、
#    ユーザーの要望により追加。運用ログ自体はログインなしでも閲覧できる
#    ＝実質公開の場なので、Web上のどこかで他の生徒にも見えている情報
#    だけを載せてよい、という基準）。この基準により課題の達成/取り消し
#    （StudyLog.js上「自分のみ」表示）は log_event を呼ばない。学習ログは
#    「みんなの記録」で全員に見えているためこの基準に反せず対象のまま。
#    累計ポイント（points_{guild_id}.json）も同じ基準で対象外（2026/08/19、
#    ユーザーの指摘で追加。ヘッダーバッジで見えるのは自分の累計だけで、
#    週間ランキングはstudy_logsから毎回再計算した別の値のため、
#    points_{guild_id}.json自体の中身は本人にしか見えない）。
# ================================
SYSTEM_LOG_FILE = "system_log.json"
SYSTEM_LOG_MAX_ENTRIES = 300
_system_log_lock = Lock()

def log_event(category, summary, level="info", actor=None, detail=None):
    """運用ログに1件追加する。summary は日本語の短い説明文。
    actor は実行者のニックネーム（分からない/サーバー主導の処理の場合は
    Noneのままでよい。Discord IDなどより強く個人を特定できる情報は渡さない
    こと）。level は "info" または "error"（失敗をWeb側で視覚的に区別する
    ため）。失敗してもBot本体は止めない。

    detail は「具体的に何が変更されたか」を表す情報（2026/08/19追加）。
    Web側では一覧には出さず、該当行をタップしたときだけ展開表示する。
    ★ 2026/08/19、「GitHubのコミットの『変更されたファイル』表示にほぼ
    そのまま近い見た目にしたい（ファイル名込みで、ファイルごとに折り畳める
    ように）」という要望を受けて、単なる1本の文字列ではなく
    `[{"file": "実際のファイルパス or None", "diff": "+/-形式のテキスト",
    "status": "added"/"deleted"/"modified"/None}, ...]` という
    「ファイルごとの差分」のリスト形式にした（file_diff()参照）。file は
    このBotのデータディレクトリ内の実パス（例: "words/set_20260819_...json"）
    をそのまま見せる（隠さない）。file が無い項目（対象がファイル1つに
    対応しない場合）は None のままでよく、Web側はファイル名見出し無しの
    差分ブロックとして表示する。status は、ファイル自体を新規作成/削除した
    のか、既存ファイルの中身を書き換えただけなのかの区別（カードデッキ・
    お知らせは保存/削除のたびに実ファイルが作成/削除されるが、予定・時間割等は
    既存の共有ファイルの1エントリを書き換えるだけなので、ほぼ常にmodified）。
    後方互換のため、旧形式（プレーン文字列）が渡された場合も自動的に
    1件のfile:None・status:Noneエントリとして扱う。"""
    try:
        with _system_log_lock:
            entries, sha = local_get(SYSTEM_LOG_FILE)
            if not isinstance(entries, list):
                entries = []
            entry = {
                "time": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                "category": category,
                "summary": summary,
                "level": level,
                "actor": actor,
            }
            files = detail if isinstance(detail, list) else ([{"file": None, "diff": str(detail)}] if detail else [])
            safe_files = []
            for f in files:
                diff_text = str((f or {}).get("diff") or "").strip()
                if not diff_text:
                    continue
                safe_files.append({
                    "file": (f or {}).get("file"),
                    "diff": diff_text[:4000],  # ★ ファイル単位の上限（全体はさらに下でも切り詰める）
                    "status": (f or {}).get("status"),  # ★ "added"/"deleted"/"modified"（無ければNone）
                })
                if len(safe_files) >= 12:  # ★ 1回の操作で変更されるファイル数は通常1〜2件なので十分な上限
                    break
            if safe_files:
                entry["detail"] = safe_files
            entries.append(entry)
            entries = entries[-SYSTEM_LOG_MAX_ENTRIES:]
            local_put(SYSTEM_LOG_FILE, entries, sha)
    except Exception as e:
        print(f"[WARN] システムログの記録に失敗しました: {e}")

def _json_block(fields, label=None):
    """「フィールド名: 値」の一覧を、GitHubのコミット差分でJSONファイルを
    見るときのような { ... } のブロック（複数行のリスト）にする
    （2026/08/19、ユーザーの要望により追加）。新規追加/削除された
    レコードでは { と } を含む全行が +/- として表示され、実際のJSONファイルの
    コミット差分に近い見た目になる（1フィールドだけの変更時は、この機能の
    元々の方針通り、変わった行だけが表示され { }自体は出ない＝差分自体は
    今まで通り簡潔なまま）。fields は (ラベル, 値) のタプルのリスト。"""
    open_line = f"{label} {{" if label else "{"
    lines = [open_line]
    lines.extend(f"  {k}: {v}" for k, v in fields)
    lines.append("}")
    return lines

def file_diff(file, old_text, new_text, max_lines=60):
    """指定したファイル1つ分の変更を、+/- 形式の行テキストにまとめて
    返す（GitHubのファイル差分と同じ考え方）。差分が無ければ None。
    log_event の detail に渡すリストの1要素を作るための共通ヘルパー。

    ★ 追加（2026/08/19）：statusに"added"（旧内容が空＝ファイル自体を
    新規作成）/"deleted"（新内容が空＝ファイル自体を削除）/"modified"
    （既存ファイルの中身を書き換えただけ）を入れる。カードデッキ・お知らせは
    保存/削除のたびに実際にファイルが作成/削除されるが、予定・時間割等は
    既存の共有ファイル（plans_<guild_id>.json等）の中の1エントリを
    書き換えるだけなので、ほぼ常にmodifiedになる。Web側はaddedを
    「新規作成」、deletedを「削除」のバッジとしてファイル名の横に表示する。"""
    diff = _text_diff_lines(old_text, new_text, max_lines=max_lines)
    if not diff:
        return None
    if not (old_text or "").strip():
        status = "added"
    elif not (new_text or "").strip():
        status = "deleted"
    else:
        status = "modified"
    return {"file": file, "diff": diff, "status": status}

@app.route("/system_log", methods=["GET"])
def system_log():
    entries, _ = local_get(SYSTEM_LOG_FILE)
    if not isinstance(entries, list):
        entries = []
    try:
        limit = max(1, min(200, int(request.args.get("limit", 100))))
    except (TypeError, ValueError):
        limit = 100
    return jsonify({"ok": True, "entries": list(reversed(entries))[:limit]})

# ================================
#  設定ファイル
# ================================
def load_config(guild_id: int):
    data, _ = local_get(f"config_{guild_id}.json")
    return data or {}

def save_config(guild_id: int, data: dict):
    _, sha = local_get(f"config_{guild_id}.json")
    local_put(f"config_{guild_id}.json", data, sha)

async def async_load_config(guild_id: int):
    data, _ = await async_local_get(f"config_{guild_id}.json")
    return data or {}

async def async_save_config(guild_id: int, data: dict):
    _, sha = await async_local_get(f"config_{guild_id}.json")
    await async_local_put(f"config_{guild_id}.json", data, sha)

def list_all_configs():
    if not os.path.isdir(DATA_DIR):
        return []
    return [
        name for name in os.listdir(DATA_DIR)
        if name.startswith("config_")
        and name.endswith(".json")
        and os.path.isfile(_data_path(name))
    ]

# ================================
#  ★ 入力チェック：制御文字・不可視文字・壊れた符号位置を弾く
#  ─────────────────────────────────────────────
#  Cardmaker.js の findBugChars() / warnIfBugChars()（フロント側チェック）と
#  完全に同じ判定基準をサーバー側にも移植したもの。
#  フロント側のチェックは devtools 等で直接APIを叩けば素通りしてしまうため、
#  「制御文字などを弾く」という判断自体はサーバー側でも独立して行う必要がある。
#  ・①②③ ㈱㈲㈹ ㍾㍽㎜㎡ などの「機種依存文字」は許可（見た目が出るため）。
#  ・弾くのは主に次の3種類：
#    1) 制御文字（RLO/LROなどの双方向制御・Unicodeタグ文字など）
#    2) 見た目に何も表示されないが実害の大きい文字
#       （ゼロ幅スペース／Word Joiner／BOMなど）
#    3) 壊れた符号位置（孤立サロゲート・非文字コードポイント）
#       → GitHub等でエラーになったり読み込めなくなったりする原因
# ================================
BUG_CHAR_RANGES = [
    (0xE000, 0xF8FF),    # 私用領域（外字・gaiji）
    (0xFDD0, 0xFDEF),    # 非文字コードポイント
]
BUG_CHAR_CODES = {0xFFFE, 0xFFFF}  # 非文字コードポイント（BMP末尾）

INVISIBLE_CHAR_RANGES = [
    (0x200B, 0x200C),    # ゼロ幅スペース、ZWNJ（※200Dは含まない＝ZWJは許可）
    (0x2060, 0x2064),    # Word Joiner、不可視の演算子記号など
    (0x2066, 0x2069),    # 双方向テキストの分離文字（LRI/RLI/FSI/PDI）
    (0x202A, 0x202E),    # 双方向テキストの埋め込み・上書き（LRE/RLE/PDF/LRO/RLO）
    (0xE0000, 0xE007F),  # Unicodeタグ文字（見えないままテキストを埋め込める）
]
INVISIBLE_CHAR_CODES = {0x00AD, 0x180E, 0xFEFF}  # ソフトハイフン／モンゴル母音分離符／BOM


def _is_allowed_invisible(cp: int) -> bool:
    if cp == 0x200D:
        return True  # ZWJ（絵文字結合）
    if 0xFE00 <= cp <= 0xFE0F:
        return True  # VS1-16（異体字・絵文字表示指定）
    if 0xE0100 <= cp <= 0xE01EF:
        return True  # VS17-256（IVS用）
    return False


def find_bug_chars(s):
    """文字列中の「バグ文字」だけを重複なく抽出して返す（無ければ空リスト）"""
    if not s:
        return []
    found = []
    for ch in str(s):
        cp = ord(ch)
        if _is_allowed_invisible(cp):
            continue
        is_ctrl = cp < 0x20 and ch not in ("\t", "\n", "\r")
        is_del  = cp == 0x7F
        # Python の str は既にUnicodeなので「孤立サロゲート」はサロゲートペア分解後には
        # 通常出現しないが、外部（JSON等）から紛れ込むケースに備えて同じ範囲を弾いておく。
        is_lone_sg = 0xD800 <= cp <= 0xDFFF
        is_range = any(s0 <= cp <= e0 for s0, e0 in BUG_CHAR_RANGES) or cp in BUG_CHAR_CODES
        is_invis = any(s0 <= cp <= e0 for s0, e0 in INVISIBLE_CHAR_RANGES) or cp in INVISIBLE_CHAR_CODES
        if (is_ctrl or is_del or is_lone_sg or is_range or is_invis) and ch not in found:
            found.append(ch)
    return found


def reject_if_bug_chars(fields: dict):
    """
    fields: { "表示用フィールド名": 値 } の辞書。
    いずれかの値に禁止文字が含まれていれば、Flaskのjsonレスポンス（エラー）を返す。
    問題なければ None を返す。
    呼び出し側は `err = reject_if_bug_chars(...); if err: return err` の形で使う。
    """
    for field_name, value in fields.items():
        if value is None:
            continue
        bad = find_bug_chars(value)
        if bad:
            return jsonify({
                "ok": False,
                "error": f"{field_name} に使用できない文字が含まれています（制御文字・不可視文字など）: "
                         + " ".join(bad)
            })
    return None


# ================================
#  予定データ
# ================================
def load_plans(guild_id: int):
    data, _ = local_get(f"plans_{guild_id}.json")
    return data or []

def save_plans(guild_id: int, plans: list):
    _, sha = local_get(f"plans_{guild_id}.json")
    local_put(f"plans_{guild_id}.json", plans, sha)
    notify_change(guild_id)

async def async_load_plans(guild_id: int):
    data, _ = await async_local_get(f"plans_{guild_id}.json")
    return data or []

def _plan_lines(p):
    """運用ログ用：予定1件を { ... } のブロックにする（GitHubのコミット
    差分に使うfile_diff()と組み合わせて、plans_{guild_id}.jsonの変更点
    だけを +/- で浮かび上がらせるために使う）。"""
    fields = [
        ("日付", p.get('date')),
        ("科目", p.get('subject')),
        ("内容", p.get('content')),
    ]
    if p.get("points") is not None:
        fields.append(("ポイント", f"{p['points']}pt"))
    return _json_block(fields)

def _plans_text(plans):
    lines = []
    for p in (plans or []):
        lines.extend(_plan_lines(p))
    return "\n".join(lines)

async def async_save_plans(guild_id: int, plans: list):
    _, sha = await async_local_get(f"plans_{guild_id}.json")
    await async_local_put(f"plans_{guild_id}.json", plans, sha)
    notify_change(guild_id)

# ================================
#  ログ
# ================================
def write_log(guild_id: int, log_type: str, detail: str):
    """
    ★ ログ保存の失敗は本質的な機能ではないので、例外を握りつぶして良い。
       ただし今後の調査用に標準出力へは残す。
    """
    try:
        filename = f"logs_{guild_id}.json"
        logs, sha = local_get(filename)
        logs = logs or []
        now_jst = datetime.now(JST)
        now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
        logs = [
            log for log in logs
            if (now_jst - datetime.strptime(log["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)).days <= 30
        ]
        logs.append({"time": now_str, "type": log_type, "detail": detail})
        local_put(filename, logs, sha)
    except Exception as e:
        print(f"[WARN] write_log failed (ignored): {e}")

async def async_write_log(guild_id: int, log_type: str, detail: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, write_log, guild_id, log_type, detail)

# ================================
#  勉強ログ データ
# ================================
def load_study_logs(guild_id: int):
    data, _ = local_get(f"study_logs_{guild_id}.json")
    return data or []

def save_study_logs(guild_id: int, logs: list):
    _, sha = local_get(f"study_logs_{guild_id}.json")
    local_put(f"study_logs_{guild_id}.json", logs, sha)
    notify_change(guild_id)

def _study_log_lines(l):
    """運用ログ用：勉強ログ1件を { ... } のブロックにする。"""
    fields = [
        ("記録日時", l.get('time')),
        ("実行者", l.get('nickname')),
        ("科目", l.get('subject')),
        ("時間", f"{l.get('minutes')}分"),
        ("メモ", l.get('memo') or "(なし)"),
    ]
    return _json_block(fields)

def _study_logs_text(logs):
    lines = []
    for l in (logs or []):
        lines.extend(_study_log_lines(l))
    return "\n".join(lines)

async def async_load_study_logs(guild_id: int):
    data, _ = await async_local_get(f"study_logs_{guild_id}.json")
    return data or []

async def async_save_study_logs(guild_id: int, logs: list):
    _, sha = await async_local_get(f"study_logs_{guild_id}.json")
    await async_local_put(f"study_logs_{guild_id}.json", logs, sha)
    notify_change(guild_id)

# ================================
#  ★ 勉強タイマー状態（複数端末で共有）
#  ─────────────────────────────
#  以前はブラウザのlocalStorageだけにタイマーの開始時刻を保存していたため、
#  ①別端末・別ブラウザで開いても状態が見えず、二重に計測を始められてしまう
#  ②タブを閉じる／バックグラウンドに置くとJSが止まり、「3時間経過」の検知が
#    ブラウザの復帰まで遅れる（＝精度が低い。時には全く違う経過時間で
#    「破棄」判定されてしまう）
#  という2つの問題があった。
#  → 開始・一時停止・再開の「時刻」そのものをサーバー（ローカルディスク）で管理し、
#    どの端末で開いても同じ状態を見られるようにする。
#    3時間経過の判定・DM通知も、クライアントのタブが開いているかに関係なく
#    サーバー側の定期ジョブ（check_study_timers）で正確に行う。
#
#  study_timers_{guild_id}.json の中身: { student_id: エントリ, ... }
#  エントリ:
#    state             : "running" | "paused"
#    run_start_epoch   : 現在の計測区間が始まった時刻（ms epoch、running時のみ値あり）
#    accumulated_sec   : 直近の計測区間を含まない、これまでの累計秒
#    next_checkpoint_sec : 次に「3時間経過」の自動休憩を入れる累計秒のライン
#    pause_reason      : "manual"（自分で休憩） / "checkpoint"（3時間経過での自動休憩）
# ================================
TIMER_CHECKPOINT_SEC = 10800  # 3時間ごとに自動的に休憩へ切り替える
MSG_TIMER_AUTO_PAUSED = (
    "3時間が経過したため、自動的に休憩（一時停止）にしました。"
    "アプリの「再開」から続きを計測できます。"
)

def load_study_timers(guild_id: int) -> dict:
    data, _ = local_get(f"study_timers_{guild_id}.json")
    return data or {}

def save_study_timers(guild_id: int, timers: dict, sha=None):
    if sha is None:
        _, sha = local_get(f"study_timers_{guild_id}.json")
    local_put(f"study_timers_{guild_id}.json", timers, sha)
    notify_change(guild_id)

async def async_load_study_timers(guild_id: int) -> dict:
    data, _ = await async_local_get(f"study_timers_{guild_id}.json")
    return data or {}

async def async_save_study_timers(guild_id: int, timers: dict, sha=None):
    if sha is None:
        _, sha = await async_local_get(f"study_timers_{guild_id}.json")
    await async_local_put(f"study_timers_{guild_id}.json", timers, sha)
    notify_change(guild_id)

def _finalize_study_timer(entry, now_ms):
    """
    エントリを現在時刻(now_ms)で評価し、必要なら状態遷移させる（副作用なしの純粋関数）。
    戻り値: (新しいエントリ or None, 通知種別 "auto_paused"/None)
    ・running で累計3時間（の倍数）に達するたびに → paused へ遷移し "auto_paused" を通知
      （保存を強制したり破棄したりはしない。休憩と同様、いつでも「再開」で続きから計測できる）
    ・それ以外（paused 等）はそのまま
    """
    if not entry:
        return None, None

    state = entry.get("state")

    if state == "running":
        run_start = entry.get("run_start_epoch")
        accumulated = entry.get("accumulated_sec", 0) or 0
        if run_start is not None:
            elapsed = accumulated + (now_ms - run_start) / 1000.0
        else:
            elapsed = accumulated
        checkpoint = entry.get("next_checkpoint_sec", TIMER_CHECKPOINT_SEC) or TIMER_CHECKPOINT_SEC
        if elapsed >= checkpoint:
            new_entry = dict(entry)
            new_entry["accumulated_sec"] = int(round(elapsed))
            new_entry["run_start_epoch"] = None
            new_entry["state"] = "paused"
            new_entry["pause_reason"] = "checkpoint"
            # ★ 再開後さらに3時間動かしたら、また同じように自動休憩を挟む
            new_entry["next_checkpoint_sec"] = checkpoint + TIMER_CHECKPOINT_SEC
            return new_entry, "auto_paused"
        return entry, None

    # paused など：変化なし
    return entry, None

def _try_notify_timer(guild_id, student_id, message):
    """通知はベストエフォート。失敗してもタイマーの状態遷移自体は成立させる。"""
    try:
        send_discord_dm(guild_id, student_id, "StudyLog", message)
    except Exception as e:
        print(f"[WARN] study_timer DM通知に失敗しました（student_id={student_id}）: {e}")

def _sync_timer_entry(guild_id, student_id):
    """
    現在のタイマーエントリを取得し、3時間経過による自動休憩への遷移が
    必要ならその場で適用・保存・通知してから返す。
    どのエンドポイントも、まずこれを呼んでから自分の処理を行う。
    戻り値: (timers辞書（最新）, 現在のエントリ or None)
    """
    timers = load_study_timers(guild_id)
    now_ms = int(time.time() * 1000)
    entry = timers.get(student_id)
    new_entry, notify_kind = _finalize_study_timer(entry, now_ms)

    if new_entry != entry:
        if new_entry is None:
            timers.pop(student_id, None)
        else:
            timers[student_id] = new_entry
        try:
            save_study_timers(guild_id, timers)
            if notify_kind == "auto_paused":
                _try_notify_timer(guild_id, student_id, MSG_TIMER_AUTO_PAUSED)
        except DataWriteError as e:
            print(f"[WARN] study_timers 保存に失敗しました: {e}")
            # 保存に失敗した場合は通知せず、次回アクセス時に再評価される

    return timers, new_entry

def _timer_entry_json(entry, now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if not entry:
        return {"state": "idle", "elapsed_sec": 0, "run_start_epoch": None, "accumulated_sec": 0}
    state = entry.get("state")
    accumulated = entry.get("accumulated_sec", 0) or 0
    run_start = entry.get("run_start_epoch")
    if state == "running" and run_start is not None:
        elapsed = accumulated + (now_ms - run_start) / 1000.0
    else:
        elapsed = accumulated
    return {
        "state": state,
        "elapsed_sec": int(elapsed),
        "run_start_epoch": run_start,
        "accumulated_sec": int(round(accumulated)),  # ★ 端数（ミリ秒由来）が残らないよう常に整数化
        "pause_reason": entry.get("pause_reason") if state == "paused" else None,
    }

def _timer_auth_from_json():
    """POST系タイマーAPI共通：JSONボディからguild_id・session_tokenを検証する。"""
    data = request.json or {}
    guild_id = data.get("guild_id")
    if not guild_id:
        return None, None, jsonify({"ok": False, "error": "missing guild_id"})
    guild_id = int(guild_id)
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return None, None, jsonify({"ok": False, "error": "not_logged_in"})
    return guild_id, student_id, None

@app.route("/timer_state", methods=["GET"])
def timer_state():
    """現在のタイマー状態を返す（端末を問わず常に最新・かつ3時間ごとの自動休憩判定を反映済み）。"""
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    guild_id = int(guild_id)
    student_id = resolve_session(request.args.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    _, entry = _sync_timer_entry(guild_id, student_id)
    resp = {"ok": True, "server_now": int(time.time() * 1000)}
    resp.update(_timer_entry_json(entry))
    return jsonify(resp)

@app.route("/timer_start", methods=["POST"])
def timer_start():
    """
    タイマー開始。
    ・誰も動かしていなければ新規に開始する
    ・既に他端末で計測中なら、その記録にそのまま合流する（新規に作らない）
    ・一時停止中の場合は開始せず、その状態を返す
      （フロント側はそれに合わせて画面を出し分ける）
    """
    guild_id, student_id, err = _timer_auth_from_json()
    if err:
        return err

    timers, entry = _sync_timer_entry(guild_id, student_id)
    now_ms = int(time.time() * 1000)

    if entry is None:
        new_entry = {
            "state": "running",
            "run_start_epoch": now_ms,
            "accumulated_sec": 0,
            "next_checkpoint_sec": TIMER_CHECKPOINT_SEC,
        }
        timers[student_id] = new_entry
        try:
            save_study_timers(guild_id, timers)
        except DataWriteError as e:
            return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
        resp = {"ok": True, "created": True}
        resp.update(_timer_entry_json(new_entry, now_ms))
        return jsonify(resp)

    if entry.get("state") == "running":
        resp = {"ok": True, "created": False, "joined": True}
        resp.update(_timer_entry_json(entry, now_ms))
        return jsonify(resp)

    # paused：新規開始はせず、現在の状態を伝える
    resp = {"ok": False, "error": "already_" + str(entry.get("state"))}
    resp.update(_timer_entry_json(entry, now_ms))
    return jsonify(resp)

@app.route("/timer_pause", methods=["POST"])
def timer_pause():
    """計測中 → 一時停止（自分の意思での休憩）。他端末にも即座に反映される。"""
    guild_id, student_id, err = _timer_auth_from_json()
    if err:
        return err

    timers, entry = _sync_timer_entry(guild_id, student_id)
    if not entry or entry.get("state") != "running":
        resp = {"ok": False, "error": "not_running"}
        resp.update(_timer_entry_json(entry))
        return jsonify(resp)

    now_ms = int(time.time() * 1000)
    # ★ ここで整数に丸めておかないと、次にJSON化された際に小数点以下
    #   （ミリ秒由来の端数）が残り、表示側で「00:00:16.885」のように
    #   秒の桁に小数が出てしまう。
    accumulated = int(round((entry.get("accumulated_sec", 0) or 0) + (now_ms - entry["run_start_epoch"]) / 1000.0))
    new_entry = dict(entry)
    new_entry["accumulated_sec"] = accumulated
    new_entry["run_start_epoch"] = None
    new_entry["state"] = "paused"
    new_entry["pause_reason"] = "manual"
    timers[student_id] = new_entry
    try:
        save_study_timers(guild_id, timers)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    resp = {"ok": True}
    resp.update(_timer_entry_json(new_entry, now_ms))
    return jsonify(resp)

@app.route("/timer_resume", methods=["POST"])
def timer_resume():
    """一時停止 → 再開（3時間経過による自動休憩からの再開も含む）。他端末にも即座に反映される。"""
    guild_id, student_id, err = _timer_auth_from_json()
    if err:
        return err

    timers, entry = _sync_timer_entry(guild_id, student_id)
    if not entry or entry.get("state") != "paused":
        resp = {"ok": False, "error": "not_paused"}
        resp.update(_timer_entry_json(entry))
        return jsonify(resp)

    now_ms = int(time.time() * 1000)
    new_entry = dict(entry)
    new_entry["run_start_epoch"] = now_ms
    new_entry["state"] = "running"
    new_entry.pop("pause_reason", None)
    if "next_checkpoint_sec" not in new_entry:
        # ★ 古い形式のデータ互換用（次のチェックポイントが無ければ現在値+3時間に設定）
        new_entry["next_checkpoint_sec"] = (new_entry.get("accumulated_sec", 0) or 0) + TIMER_CHECKPOINT_SEC
    timers[student_id] = new_entry
    try:
        save_study_timers(guild_id, timers)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    resp = {"ok": True}
    resp.update(_timer_entry_json(new_entry, now_ms))
    return jsonify(resp)

@app.route("/timer_stop", methods=["POST"])
def timer_stop():
    """
    サーバー側のタイマー状態を消す。
    ユーザーが手動で「停止」した直後（この後はクライアント側の確認画面で
    保存／破棄を決めるので、サーバー側で計測中／休憩中として残す必要はない）
    に使う、現在の状態を問わない汎用クリア用エンドポイント。
    """
    guild_id, student_id, err = _timer_auth_from_json()
    if err:
        return err

    timers = load_study_timers(guild_id)
    if student_id in timers:
        timers.pop(student_id, None)
        try:
            save_study_timers(guild_id, timers)
        except DataWriteError as e:
            return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    return jsonify({"ok": True})

# ================================
#  ポイント データ
# ================================
def load_points(guild_id: int) -> dict:
    data, _ = local_get(f"points_{guild_id}.json")
    return data or {}

def save_points(guild_id: int, pts: dict, sha=None):
    if sha is None:
        _, sha = local_get(f"points_{guild_id}.json")
    local_put(f"points_{guild_id}.json", pts, sha)
    notify_change(guild_id)

# ============================================================
#  課題達成データ
# ============================================================
def load_completed_tasks(guild_id: int) -> dict:
    data, _ = local_get(f"completed_tasks_{guild_id}.json")
    return data or {}

def save_completed_tasks(guild_id: int, tasks: dict, sha=None):
    if sha is None:
        _, sha = local_get(f"completed_tasks_{guild_id}.json")
    local_put(f"completed_tasks_{guild_id}.json", tasks, sha)
    notify_change(guild_id)


def _task_id_of_plan(plan: dict) -> str:
    """フロント（StudyLog.js）の `${p.date}_${p.subject}_${p.content}` と全く同じ規則でIDを作る"""
    return f"{plan.get('date')}_{plan.get('subject')}_{plan.get('content')}"

def find_task_points(guild_id: int, task_id: str):
    """
    task_id に対応する予定（課題）をサーバー側の plans から探し、
    本来のポイント数を返す。クライアントが自己申告する points は一切信用しない。
    見つからない場合は None を返す（＝そもそも存在しない課題IDとして扱う）。
    """
    plans = load_plans(guild_id)
    for p in plans:
        if _task_id_of_plan(p) == task_id:
            pts = p.get("points")
            return pts if pts is not None else DEFAULT_TASK_POINTS
    return None


def _normalize_task_entry(entry):
    """旧形式（文字列）・旧dict形式（points/nicknameなし）・新形式を統一する"""
    if isinstance(entry, str):
        return {"id": entry, "date": None, "points": None, "nickname": None}
    entry = dict(entry)
    if "points" not in entry:
        entry["points"] = None
    if "nickname" not in entry:
        entry["nickname"] = None
    return entry


# ================================
#  ユーザーデータ
# ================================
def load_users(guild_id: int):
    data, _ = local_get(f"users_{guild_id}.json")
    return data or []

def save_users(guild_id: int, users: list):
    _, sha = local_get(f"users_{guild_id}.json")
    local_put(f"users_{guild_id}.json", users, sha)

def _users_text(users):
    """運用ログ用：users_{guild_id}.json を { ... } のブロックの並びにする。
    ★ password_hash/password_saltは絶対に含めない（他の項目とは違い、
    これは公開してよい情報ではないため）。"""
    lines = []
    for u in (users or []):
        lines.extend(_json_block([("学籍番号", u.get('id')), ("ニックネーム", u.get('nickname'))]))
    return "\n".join(lines)

def find_user(guild_id: int, student_id: str):
    users = load_users(guild_id)
    return next((u for u in users if u.get("id") == student_id), None)

# ================================
#  ★ パスワード関連ユーティリティ
#  ─────────────────────────────
#  パスワードは絶対に平文のまま保存しない。
#  users_{guild_id}.json はサーバーのローカルディスクに保存されているが、
#  ファイルが何らかの理由で漏洩・閲覧された場合に備えて、
#  平文で置かないようにする。
#  そのため PBKDF2-HMAC-SHA256（ソルト付き・十分な反復回数）で
#  ハッシュ化した値だけを保存し、元のパスワードはサーバーのメモリ上
#  ですら検証の一瞬しか扱わない。
# ================================
PBKDF2_ITERATIONS = 210_000

def hash_password(password: str, salt_hex: str = None):
    """(hash_hex, salt_hex) を返す。salt_hex を渡さなければ新規のソルトを生成する。"""
    if salt_hex is None:
        salt_hex = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    )
    return dk.hex(), salt_hex

def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    if not salt_hex or not expected_hash_hex:
        return False
    actual_hash_hex, _ = hash_password(password, salt_hex)
    # ★ タイミング攻撃対策のため、単純な == ではなく定数時間比較を使う
    return hmac.compare_digest(actual_hash_hex, expected_hash_hex)

# ================================
#  ★ 追加：簡易レート制限（総当たり攻撃対策）
#  ─────────────────────────────
#  ログイン・パスワード確認コード（6桁・100万通り）など、繰り返し
#  試行される攻撃の的になりやすいエンドポイント向け。外部ライブラリを
#  増やさず、プロセス内メモリだけでIPアドレス単位の直近の試行回数を
#  数える簡易実装（この用途には十分。複数プロセス/複数台で動かす
#  場合はRedis等の共有ストアへの置き換えが必要）。
# ================================
_rate_limit_hits = {}  # "{bucket}:{ip}" -> [試行時刻, ...]
RATE_LIMIT_WINDOW_SEC = 15 * 60  # 15分
RATE_LIMIT_MAX_HITS = 10         # この回数を超えたら一時的に拒否

def _client_ip() -> str:
    # ★ リバースプロキシ配下でも実クライアントIPを見られるよう、
    #   X-Forwarded-For があれば先頭（＝最初にプロキシへ渡ってきた値）を使う。
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"

def rate_limited(bucket: str) -> bool:
    """呼び出すたびに1回分の試行として記録する。直近 RATE_LIMIT_WINDOW_SEC 秒
    以内の試行数が RATE_LIMIT_MAX_HITS を超えていれば True（＝拒否すべき）。"""
    key = f"{bucket}:{_client_ip()}"
    now = time.time()
    hits = _rate_limit_hits.setdefault(key, [])
    hits[:] = [t for t in hits if now - t < RATE_LIMIT_WINDOW_SEC]
    if len(hits) >= RATE_LIMIT_MAX_HITS:
        return True
    hits.append(now)
    return False

def rate_limit_response():
    return jsonify({"ok": False, "error": "too_many_attempts", "retry_after_sec": RATE_LIMIT_WINDOW_SEC}), 429

# ================================
#  ★ ログインセッション（署名付きトークン方式・状態を持たない）
#  ─────────────────────────────
#  最初は「サーバーのメモリ上でトークンを管理する」方式で実装していたが、
#  Renderの無料枠は一定時間アクセスが無いとプロセスごと再起動され、
#  メモリの中身（＝発行済みトークン一覧）が消えてしまう。
#  そうなると「少し間を空けてアクセスしただけで全員ログアウトされる」
#  という実用上まずい挙動になるため、トークン自体に
#  「guild_id・student_id・発行時刻」を埋め込み、HMAC署名を付けて
#  改ざんを検知する方式に変更した（JWTの簡易版のようなもの）。
#  ・SESSION_SECRET はプロセス再起動をまたいでも変わらないよう、
#    環境変数として設定することを強く推奨する（TOKENと同様）。
#    未設定の場合はプロセスごとにランダム値を使うため、その場合は結局
#    再起動のたびに全員ログアウトされる（が、パスワード自体は漏れない）。
#  ・トークンは有効期限が来るまで有効であり続ける。パスワード変更時に
#    「以前発行したトークンを強制的に無効化する」仕組みは持たない
#    （このアプリの規模ではオーバーエンジニアリングと判断）。
#    より厳密に失効させたい場合は、失効リストを別ファイルに保存する方式に
#    拡張できる。
# ================================
SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    print("[WARN] 環境変数 SESSION_SECRET が未設定です。プロセス再起動のたびに"
          "全ユーザーのログインセッションが無効になります。サーバーの環境変数に"
          "SESSION_SECRET（ランダムな文字列）を設定することを推奨します。")

SESSION_TTL_SEC = 60 * 60 * 24 * 30  # 30日間ログイン状態を維持

def create_session(guild_id: int, student_id: str) -> str:
    payload = json.dumps({"g": guild_id, "s": student_id, "t": int(time.time())}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(SESSION_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"

def resolve_session(token, guild_id: int):
    """有効な署名付きトークンなら student_id を返す。無効・期限切れ・改ざんなら None。"""
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(SESSION_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
        if payload.get("g") != guild_id:
            return None
        if time.time() - payload.get("t", 0) > SESSION_TTL_SEC:
            return None
        return payload.get("s")
    except Exception:
        return None

# ================================
#  ★ 追加（2026/08/19）：「変更」系API共通のログイン必須チェック
#  ─────────────────────────────
#  以前は予定・時間割・カードデッキ・フォルダ・お知らせの追加/編集/削除系
#  APIが、ログイン確認をせずクライアント自己申告の nickname をそのまま
#  信用していた（＝ログインしていなくても、APIを直接叩けば変更できて
#  しまっていた）。timer_*・add_study_log・complete_task等と同じ考え方で、
#  「変更」は必ずログイン済み（有効なsession_token）を要求するように統一する。
#  ・閲覧（GET系）は従来通りログイン不要のまま変えない
#    （予定・時間割ページは「見るだけなら誰でもOK」という仕様を維持）。
#  ・nicknameはクライアントの自己申告を使わず、サーバー側のユーザーデータから
#    引き直す（表示名の詐称防止。/add_study_logなどと同じ）。
# ================================
def require_login_json(data):
    """POST系「変更」API共通：JSONボディのguild_id・session_tokenを検証する。
    戻り値 (guild_id, student_id, nickname, err)。err が None でなければ、
    呼び出し側はそのまま `return err` してよい。"""
    guild_id = data.get("guild_id")
    if not guild_id:
        return None, None, None, jsonify({"ok": False, "error": "missing guild_id"})
    try:
        guild_id = int(guild_id)
    except (TypeError, ValueError):
        return None, None, None, jsonify({"ok": False, "error": "invalid guild_id"})
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return None, None, None, jsonify({"ok": False, "error": "not_logged_in"})
    user = find_user(guild_id, student_id)
    nickname = user["nickname"] if user else None
    return guild_id, student_id, nickname, None

# ================================
#  Discordアカウント連携（生徒ID ⇔ Discordユーザー）
#  ★ 個別DM通知のために、StudyLogの生徒IDとDiscordアカウントを
#     紐付けて保存しておく。{ "1I001": 123456789012345678, ... }
# ================================
def load_discord_links(guild_id: int) -> dict:
    data, _ = local_get(f"discord_links_{guild_id}.json")
    return data or {}

def save_discord_links(guild_id: int, links: dict, sha=None):
    if sha is None:
        _, sha = local_get(f"discord_links_{guild_id}.json")
    local_put(f"discord_links_{guild_id}.json", links, sha)

# ================================
#  ★ アカウント連携コード（なりすまし対策）
#  ─────────────────────────────
#  以前は Discord 上で `/id連携 <生徒ID>` を実行するだけで、誰でも
#  好きな生徒IDを自分のDiscordアカウントに紐付けられてしまっていた。
#  生徒IDは推測しやすい形式（例: 1I001）な上、StudyLog自体に
#  ログインしていなくても実行できたため、他人になりすまして通知を
#  横取りしたり、パスワード再設定の確認コードを自分宛に届かせて
#  本人のアカウントごと乗っ取ることが可能だった。
#
#  対策：連携には「StudyLog側で既にログイン（パスワード認証）済み
#  であること」を証明する、短時間だけ有効なワンタイムコードを要求する。
#    1) 生徒がStudyLogにログインした状態で連携コードを発行する
#       （/generate_link_code）→ その生徒のstudent_idに紐付いた
#       ランダムな8桁コードが発行される（5分間だけ有効・1回使い切り）
#    2) 生徒はDiscord上で `/id連携 <発行されたコード>` を実行する
#       → コードが有効なら、そのコードに紐付いていたstudent_idと
#         「今コマンドを実行したDiscordユーザー」を連携する
#  コードを知らない第三者は、他人の生徒IDを知っていても連携できない。
# ================================
LINK_CODE_TTL_SEC      = 5 * 60   # コードの有効期限：5分
LINK_CODE_COOLDOWN_SEC = 30       # 連続発行の連打防止（クールダウン）

LINK_CODES = {}             # code(str) -> {"guild_id", "student_id", "expires"}
LINK_CODE_LAST_ISSUED = {}  # f"{guild_id}:{student_id}" -> 直近の発行時刻（連打防止用）

def _generate_link_code() -> str:
    # Discord上で打ち込みやすいよう、紛らわしい文字(0/O, 1/I等)を除いた
    # 大文字+数字の8桁。5分の有効期限内に総当たりで的中させるのは非現実的な文字数。
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if code not in LINK_CODES:
            return code

def issue_link_code(guild_id: int, student_id: str) -> dict:
    """StudyLogにログイン済み（session_token検証済み）の本人だけが呼び出す想定。"""
    key = f"{guild_id}:{student_id}"
    now = time.time()
    last = LINK_CODE_LAST_ISSUED.get(key)
    if last and (now - last < LINK_CODE_COOLDOWN_SEC):
        remain = int(LINK_CODE_COOLDOWN_SEC - (now - last)) + 1
        raise ValueError(f"too_soon:{remain}")

    # 同じ生徒に対して未使用の古いコードが残っていれば無効化しておく
    for c in [c for c, v in LINK_CODES.items()
              if v["guild_id"] == guild_id and v["student_id"] == student_id]:
        del LINK_CODES[c]

    code = _generate_link_code()
    LINK_CODES[code] = {"guild_id": guild_id, "student_id": student_id, "expires": now + LINK_CODE_TTL_SEC}
    LINK_CODE_LAST_ISSUED[key] = now
    return {"code": code, "expires_in_sec": LINK_CODE_TTL_SEC}

def consume_link_code(guild_id: int, code: str):
    """有効なコードなら student_id を返し、コードを消費（削除）する。無効なら None。"""
    entry = LINK_CODES.get(code)
    if not entry:
        return None
    if entry["guild_id"] != guild_id:
        return None
    if time.time() > entry["expires"]:
        del LINK_CODES[code]
        return None
    del LINK_CODES[code]  # ★ 1回使い切り（使い回し防止）
    return entry["student_id"]


# ================================
#  ★ Discord OAuth2 の一時state（CSRF対策 兼 「誰が認可画面に飛んだか」の記録）
#  ─────────────────────────────
#  生徒がStudyLog上のボタンから直接Discordの認可画面に飛ぶ方式。
#  「認可画面から戻ってきたリクエストが、確かにさっきログイン中の本人が
#   発行したものである」ことを保証するため、認可画面に飛ばす直前に
#   ランダムなstateを発行してguild_id/student_idと紐付けておき、
#   コールバック時にそのstateを検証する（1回使い切り・5分間有効）。
#   これが無いと、他人が作ったURLを踏まされて意図しない連携をさせられる
#   （CSRF）リスクがある。
# ================================
OAUTH_STATE_TTL_SEC = 5 * 60
OAUTH_STATES = {}  # state(str) -> {"guild_id", "student_id", "purpose", "expires"}

def issue_oauth_state(guild_id: int, student_id, purpose: str) -> str:
    """
    purpose:
      "link"  … 既にログイン中の本人が、追加でDiscordを連携する（従来の /discord_oauth_start）
      "login" … まだログインしていない状態で、Discordそのものでログインしようとしている
                （student_id はこの時点ではまだ分からないので None）
    """
    state = secrets.token_urlsafe(24)
    OAUTH_STATES[state] = {
        "guild_id": guild_id,
        "student_id": student_id,
        "purpose": purpose,
        "expires": time.time() + OAUTH_STATE_TTL_SEC,
    }
    return state

def consume_oauth_state(state: str):
    """有効なstateなら中身のdictを返し、消費（削除）する。無効ならNone。"""
    entry = OAUTH_STATES.get(state)
    if not entry:
        return None
    if time.time() > entry["expires"]:
        del OAUTH_STATES[state]
        return None
    del OAUTH_STATES[state]  # ★ 1回使い切り
    return entry


# ================================
#  ★ Discordログイン専用の紐付け（discord_login_links_{guild_id}.json）
#  ─────────────────────────────
#  DM通知用の discord_links とは意図的に別ファイルにしている。
#  「学籍番号+パスワードで既に登録している生徒／すでに /id連携（DM用）
#   だけ済ませている生徒であっても、Discordログイン機能そのものは
#   全員一度、新しい登録ステップ（既存アカウントならパスワードで本人確認）
#   を通してもらう」という運用方針のため、こちらは意図的に空の状態から
#   始まり、discord_links の中身を勝手に引き継がない。
# ================================
def load_discord_login_links(guild_id: int) -> dict:
    data, _ = local_get(f"discord_login_links_{guild_id}.json")
    return data or {}

def save_discord_login_links(guild_id: int, links: dict, sha=None):
    if sha is None:
        _, sha = local_get(f"discord_login_links_{guild_id}.json")
    local_put(f"discord_login_links_{guild_id}.json", links, sha)


# ================================
#  ★ Discordログイン：初回登録用の一時トークン
#  ─────────────────────────────
#  「Discordでログイン」を初めて使う生徒は、OAuth認可の直後に一度だけ
#  学籍番号（＋既存アカウントならパスワード）を入力する登録ステップを通る。
#  このトークンは「Discordの認可自体は既に済んでいる」ことの証明であり、
#  Login.html側の登録フォームからサーバーに送られてくる。
#  ・パスワード誤入力時など、登録が失敗しただけではトークンを消費しない
#    （何度かやり直せるようにするため）。ただし有効期限（10分）は切れる。
#  ・成功時のみ明示的に破棄する。
# ================================
DISCORD_REG_TOKEN_TTL_SEC = 10 * 60
DISCORD_REG_TOKENS = {}  # token(str) -> {"guild_id","discord_user_id","discord_username","expires"}

def issue_discord_reg_token(guild_id: int, discord_user_id: int, discord_username: str = "") -> str:
    token = secrets.token_urlsafe(24)
    DISCORD_REG_TOKENS[token] = {
        "guild_id": guild_id,
        "discord_user_id": discord_user_id,
        "discord_username": discord_username,
        "expires": time.time() + DISCORD_REG_TOKEN_TTL_SEC,
    }
    return token

def get_discord_reg_token(token):
    """有効なら中身のdictを返す（消費しない＝再試行可能）。無効・期限切れならNone。"""
    entry = DISCORD_REG_TOKENS.get(token)
    if not entry:
        return None
    if time.time() > entry["expires"]:
        del DISCORD_REG_TOKENS[token]
        return None
    return entry

def discard_discord_reg_token(token):
    DISCORD_REG_TOKENS.pop(token, None)

# ================================
#  科目チャンネルユーティリティ
# ================================
def get_subject_channels(guild: discord.Guild) -> list:
    if SUBJECT_CATEGORY_ID:
        for cat in guild.categories:
            if cat.id == int(SUBJECT_CATEGORY_ID):
                return list(cat.text_channels)
    if SUBJECT_CATEGORY:
        for cat in guild.categories:
            if cat.name == SUBJECT_CATEGORY:
                return list(cat.text_channels)
    return list(guild.text_channels)

def get_subject_channel_by_name(guild: discord.Guild, name: str):
    for ch in get_subject_channels(guild):
        if ch.name == name:
            return ch
    return None

# ================================
#  日付パース
# ================================
def parse_date(date: str):
    try:
        if "-" in date and len(date.split("-")[0]) == 4:
            parsed = datetime.strptime(date, "%Y-%m-%d")
        else:
            date = date.replace("/", "-")
            m, d = date.split("-")
            y = datetime.now().year
            parsed = datetime.strptime(f"{y}-{int(m):02d}-{int(d):02d}", "%Y-%m-%d")
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return None

# ================================
#  ポイントを付与すべきカテゴリかどうか
# ================================
POINT_CATEGORIES = ("提出", "宿題")
DEFAULT_TASK_POINTS = 5

# ================================
#  add 内部関数
# ================================
async def add_plan_internal(guild_id: int, subject: str, date: str, category: str, content: str, points=None):
    date_str = parse_date(date)
    if not date_str:
        return False, "日付の形式が正しくありません！", None
    today = datetime.now(JST).date()
    if datetime.strptime(date_str, "%Y-%m-%d").date() < today:
        return False, "過去の日付は登録できません！", None
    tagged_content = f"【{category}】{content}"

    plan = {"date": date_str, "subject": subject, "content": tagged_content}
    if category in POINT_CATEGORIES:
        plan["points"] = points if points is not None else DEFAULT_TASK_POINTS

    plans = load_plans(guild_id)
    old_plans_text = _plans_text(plans)  # ★ 運用ログでファイル全体の差分を見せるため、追加前に控えておく
    plans.append(plan)
    try:
        save_plans(guild_id, plans)
    except DataWriteError as e:
        return False, f"保存に失敗しました（データ保存エラー）。もう一度お試しください。\n{e}", None

    detail = f"{date_str} / {subject} / {tagged_content}"
    if "points" in plan:
        detail += f" ({plan['points']}pt)"
    write_log(guild_id, "add", detail=detail)

    msg = f"登録しました！\n{date_str} / {subject} / {tagged_content}"
    if "points" in plan:
        msg += f"\n⭐ {plan['points']}pt"
    # ★ 運用ログ（system_log）向けは plans_{guild_id}.json 全体をファイル差分として渡す
    #   （write_log側のdetail＝Plan.js等が別途表示するものとは別。形式は変えていない）。
    change = file_diff(f"plans_{guild_id}.json", old_plans_text, _plans_text(plans))
    return True, msg, [change] if change else None

# ================================
#  /add
# ================================
@bot.tree.command(name="add", description="予定を追加する")
@app_commands.describe(
    date="日付（例: 6-20, 2026-06-20）",
    subject="科目（省略するとこのチャンネル名を使用）",
    category="分類（宿題・提出・持ち物など）",
    content="内容",
    points="ポイント（提出・宿題のみ有効。省略時は5pt）"
)
async def add_plan(interaction: discord.Interaction, date: str, category: str, content: str, subject: str = None, points: int = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if not subject:
        subject = interaction.channel.name
    ok, msg, _detail = await add_plan_internal(guild.id, subject, date, category, content, points)
    if ok:
        target_channel = get_subject_channel_by_name(guild, subject)
        await (target_channel or interaction.channel).send(msg)
    else:
        await interaction.followup.send(msg, ephemeral=True)
        return
    await interaction.followup.send("完了しました！", ephemeral=True)

@add_plan.autocomplete("subject")
async def add_subject_autocomplete(interaction: discord.Interaction, current: str):
    channels = get_subject_channels(interaction.guild)
    return [
        app_commands.Choice(name=ch.name, value=ch.name)
        for ch in channels if current.lower() in ch.name.lower()
    ][:25]

@add_plan.autocomplete("category")
async def add_category_autocomplete(interaction: discord.Interaction, current: str):
    candidates = ["宿題", "提出", "持ち物", "テスト", "その他"]
    return [app_commands.Choice(name=c, value=c) for c in candidates if current in c][:25]

# ================================
#  /list
# ================================
@bot.tree.command(name="list", description="予定一覧を表示する")
@app_commands.describe(date="all または 日付（例: 6/15, 2026-06-15）")
async def list_plans(interaction: discord.Interaction, date: str):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    plans = await async_load_plans(guild_id)
    if date.lower() == "all":
        if not plans:
            await interaction.followup.send("予定はありません。", ephemeral=True)
            return
        sorted_plans = sorted(plans, key=lambda p: p["date"])
        msg = "📘 **すべての予定一覧**\n"
        for p in sorted_plans:
            pts_str = f" ⭐{p['points']}pt" if "points" in p else ""
            msg += f"- {p['date']}：{p['subject']} {p['content']}{pts_str}\n"
        await interaction.followup.send(msg, ephemeral=True)
        return
    date_str = parse_date(date)
    if not date_str:
        await interaction.followup.send("日付の形式が正しくありません！", ephemeral=True)
        return
    selected = [p for p in plans if p["date"] == date_str]
    if not selected:
        await interaction.followup.send(f"{date} の予定はありません。", ephemeral=True)
        return
    msg = f"📘 **{date_str} の予定**\n"
    for p in selected:
        pts_str = f" ⭐{p['points']}pt" if "points" in p else ""
        msg += f"- {p['subject']} {p['content']}{pts_str}\n"
    await interaction.followup.send(msg, ephemeral=True)

# ================================
#  /delete
# ================================
@bot.tree.command(name="delete", description="予定を削除する")
@app_commands.describe(target="削除したい予定")
async def delete_plan(interaction: discord.Interaction, target: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    plans = await async_load_plans(guild.id)
    deleted = None
    new_plans = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            deleted = p
        else:
            new_plans.append(p)
    if not deleted:
        await interaction.followup.send("その予定は見つかりませんでした。", ephemeral=True)
        return
    try:
        save_plans(guild.id, new_plans)
    except DataWriteError as e:
        await interaction.followup.send(f"保存に失敗しました（データ保存エラー）。もう一度お試しください。\n{e}", ephemeral=True)
        return
    write_log(guild.id, "delete", detail=f"{deleted['date']} / {deleted['subject']} / {deleted['content']}")
    msg = f"削除しました！\n{target}"
    target_channel = get_subject_channel_by_name(guild, deleted["subject"])
    await (target_channel or interaction.channel).send(msg)
    await interaction.followup.send("完了しました！", ephemeral=True)

@delete_plan.autocomplete("target")
async def delete_autocomplete(interaction: discord.Interaction, current: str):
    plans = load_plans(interaction.guild.id)
    choices = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if current in label:
            choices.append(app_commands.Choice(name=label, value=label))
    return choices[:25]

# ================================
#  /edit
# ================================
@bot.tree.command(name="edit", description="予定を編集する")
@app_commands.describe(
    target="編集したい予定",
    date="新しい日付",
    subject="新しい科目",
    category="新しい分類",
    content="新しい内容",
    points="新しいポイント（提出・宿題のみ有効）"
)
async def edit_plan(interaction: discord.Interaction, target: str, date: str = None, subject: str = None, category: str = None, content: str = None, points: int = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    plans = await async_load_plans(guild.id)
    found = None
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            found = p
            break
    if not found:
        await interaction.followup.send("その予定が見つかりませんでした。", ephemeral=True)
        return
    before_str = f"{found['date']} / {found['subject']} / {found['content']}"
    if date:
        date_str = parse_date(date)
        if not date_str:
            await interaction.followup.send("日付の形式が正しくありません！", ephemeral=True)
            return
        found["date"] = date_str
    if subject:
        found["subject"] = subject
    if category and content:
        found["content"] = f"【{category}】{content}"
    elif category:
        body = found["content"].split("】", 1)[1] if "】" in found["content"] else found["content"]
        found["content"] = f"【{category}】{body}"
    elif content:
        tag = found["content"].split("】", 1)[0] + "】" if "】" in found["content"] else ""
        found["content"] = f"{tag}{content}"

    # ★ ポイント更新
    current_category = found["content"].split("】", 1)[0].lstrip("【") if "】" in found["content"] else ""
    if points is not None:
        found["points"] = points
    if current_category not in POINT_CATEGORIES and "points" in found:
        # 提出・宿題以外に変更された場合はポイントを外す
        del found["points"]
    elif current_category in POINT_CATEGORIES and "points" not in found:
        found["points"] = DEFAULT_TASK_POINTS

    try:
        await async_save_plans(guild.id, plans)
    except DataWriteError as e:
        await interaction.followup.send(f"保存に失敗しました（データ保存エラー）。もう一度お試しください。\n{e}", ephemeral=True)
        return
    after_str = f"{found['date']} / {found['subject']} / {found['content']}"
    await async_write_log(guild.id, "edit", detail=f"{before_str} → {after_str}")
    msg = f"編集しました！\n\n【編集前】\n{before_str}\n\n【編集後】\n{after_str}"
    if "points" in found:
        msg += f"\n⭐ {found['points']}pt"
    target_channel = get_subject_channel_by_name(guild, found["subject"])
    await (target_channel or interaction.channel).send(msg)
    await interaction.followup.send("完了しました！", ephemeral=True)

@edit_plan.autocomplete("target")
async def edit_target_autocomplete(interaction: discord.Interaction, current: str):
    plans = load_plans(interaction.guild.id)
    choices = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if current in label:
            choices.append(app_commands.Choice(name=label, value=label))
    return choices[:25]

@edit_plan.autocomplete("subject")
async def edit_subject_autocomplete(interaction: discord.Interaction, current: str):
    channels = get_subject_channels(interaction.guild)
    return [
        app_commands.Choice(name=ch.name, value=ch.name)
        for ch in channels if current.lower() in ch.name.lower()
    ][:25]

@edit_plan.autocomplete("category")
async def edit_category_autocomplete(interaction: discord.Interaction, current: str):
    candidates = ["宿題", "提出", "持ち物", "テスト", "その他"]
    return [app_commands.Choice(name=c, value=c) for c in candidates if current in c][:25]

# ================================
#  /setchannel
# ================================

@bot.tree.command(name="setchannel", description="通知チャンネルを設定する")
@app_commands.describe(type="どの通知に使うチャンネルか（省略時は通生）")
@app_commands.choices(type=[
    app_commands.Choice(name="通生（朝5:30 / 夜20:00）", value="commute"),
    app_commands.Choice(name="寮生（朝7:20 / 夜20:00）", value="dorm"),
    app_commands.Choice(name="お知らせ用", value="main"),
])
async def setchannel(interaction: discord.Interaction, type: app_commands.Choice[str] = None):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    config = await async_load_config(guild_id)

    kind = type.value if type else "commute"
    if kind == "dorm":
        config["remind_channel_id_dorm"] = interaction.channel.id
        label = "寮生（朝7:20）"
    elif kind == "main":
        config["notice_channel_id"] = interaction.channel.id
        label = "お知らせ用"
    else:
        config["remind_channel_id"] = interaction.channel.id
        label = "通生（朝5:30・夜20:00）"

    try:
        await async_save_config(guild_id, config)
    except DataWriteError as e:
        await interaction.followup.send(f"保存に失敗しました（データ保存エラー）。もう一度お試しください。\n{e}", ephemeral=True)
        return
    await interaction.followup.send(
        f"{label} の通知チャンネルを **#{interaction.channel.name}** に設定しました！"
    )
# ================================
#  /setup_roles（通生/寮生 振り分けパネル）
# ================================
@bot.tree.command(name="setup_roles", description="通生/寮生 振り分けパネルを投稿します")
@app_commands.describe(通生ロール="通生に付与するロール", 寮生ロール="寮生に付与するロール")
@app_commands.checks.has_permissions(manage_roles=True)
async def setup_roles(
    interaction: discord.Interaction,
    通生ロール: discord.Role,
    寮生ロール: discord.Role,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Botより上位のロールは付与できないため確認
    if 通生ロール >= guild.me.top_role or 寮生ロール >= guild.me.top_role:
        await interaction.followup.send(
            "ロールの順序を確認してください。Botの役職を、通生・寮生ロールより上に配置する必要があります。",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="通生 / 寮生 登録",
        description=(
            f"{EMOJI_COMMUTER} → 通生\n"
            f"{EMOJI_DORM} → 寮生\n\n"
            "どちらか当てはまる方にリアクションしてください。"
        ),
        color=discord.Color.teal(),
    )
    msg = await interaction.channel.send(embed=embed)
    await msg.add_reaction(EMOJI_COMMUTER)
    await msg.add_reaction(EMOJI_DORM)

    config = await async_load_config(guild.id)
    config["role_panel_message_id"] = msg.id
    config["role_panel_channel_id"] = msg.channel.id
    config["commuter_role_id"] = 通生ロール.id
    config["dorm_role_id"] = 寮生ロール.id
    try:
        await async_save_config(guild.id, config)
    except DataWriteError as e:
        await interaction.followup.send(f"保存に失敗しました（データ保存エラー）。パネルは投稿済みですが、設定の保存に失敗しました。\n{e}", ephemeral=True)
        return

    await interaction.followup.send("パネルを投稿しました。", ephemeral=True)


@setup_roles.error
async def setup_roles_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "このコマンドには「ロールの管理」権限が必要です。", ephemeral=True
        )
    else:
        if interaction.response.is_done():
            await interaction.followup.send(f"エラー: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"エラー: {error}", ephemeral=True)


async def _handle_role_reaction(payload: discord.RawReactionActionEvent, add: bool):
    if payload.guild_id is None:
        return

    config = await async_load_config(payload.guild_id)
    panel_message_id = config.get("role_panel_message_id")
    if not panel_message_id or payload.message_id != panel_message_id:
        return

    emoji = str(payload.emoji)
    if emoji not in (EMOJI_COMMUTER, EMOJI_DORM):
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    member = guild.get_member(payload.user_id)
    if member is None or member.bot:
        return

    commuter_role = guild.get_role(config.get("commuter_role_id"))
    dorm_role = guild.get_role(config.get("dorm_role_id"))
    channel_id = config.get("role_panel_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None

    try:
        if add:
            if emoji == EMOJI_COMMUTER and commuter_role:
                await member.add_roles(commuter_role, reason="通生登録")
                if dorm_role and dorm_role in member.roles:
                    await member.remove_roles(dorm_role, reason="通生に変更のため")
                    if channel:
                        msg = await channel.fetch_message(panel_message_id)
                        await msg.remove_reaction(EMOJI_DORM, member)
            elif emoji == EMOJI_DORM and dorm_role:
                await member.add_roles(dorm_role, reason="寮生登録")
                if commuter_role and commuter_role in member.roles:
                    await member.remove_roles(commuter_role, reason="寮生に変更のため")
                    if channel:
                        msg = await channel.fetch_message(panel_message_id)
                        await msg.remove_reaction(EMOJI_COMMUTER, member)
        else:
            if emoji == EMOJI_COMMUTER and commuter_role:
                await member.remove_roles(commuter_role, reason="通生リアクション解除")
            elif emoji == EMOJI_DORM and dorm_role:
                await member.remove_roles(dorm_role, reason="寮生リアクション解除")
    except discord.Forbidden:
        pass


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await _handle_role_reaction(payload, add=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await _handle_role_reaction(payload, add=False)


# ================================
#  /id連携（StudyLogの生徒ID ⇔ Discordアカウント）
#  ★ これを一度実行してもらうことで、StudyLog側からのDM通知
#    （3時間タイマー超過など）を本人のDiscordに直接送れるようになる。
#    タブを閉じていても、他のサイトを見ていても、Discordアプリ側の
#    通知として届く（Discord自体の通知がオフの場合は届かない）。
#
#  ★ なりすまし対策：以前は生徒IDを直接指定するだけで誰でも連携でき
#    てしまっていたため、StudyLog側（Webアプリ）にログイン済みの状態
#    でのみ発行できるワンタイムコード（/generate_link_code, 5分間有効・
#    1回使い切り）を要求する方式に変更した。生徒IDを知っているだけの
#    第三者はコードを発行できないため、なりすまし連携はできない。
# ================================
@bot.tree.command(name="id連携", description="StudyLogで発行した連携コードを使って、DiscordアカウントをStudyLogと連携する")
@app_commands.describe(code="StudyLogにログインした状態で発行した連携コード（8桁・5分間有効）")
async def link_student_id(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id

    sid = consume_link_code(guild_id, code.strip().upper())
    if not sid:
        await interaction.followup.send(
            "連携コードが無効か、期限切れです。StudyLogに再度ログインして、もう一度コードを発行してから "
            "「/id連携 コード」を実行してください。",
            ephemeral=True
        )
        return

    users = load_users(guild_id)
    matched = next((u for u in users if u["id"] == sid), None)
    if not matched:
        await interaction.followup.send(
            "連携コードに対応する生徒データが見つかりませんでした。お手数ですがStudyLogでもう一度コードを発行してください。",
            ephemeral=True
        )
        return
    nickname = matched.get("nickname", sid)

    try:
        links = load_discord_links(guild_id)
        links[sid] = interaction.user.id
        save_discord_links(guild_id, links)
    except DataWriteError as e:
        await interaction.followup.send(f"連携の保存に失敗しました（データ保存エラー）: {e}", ephemeral=True)
        return

    # ★ 連携できたことをその場で本人に確認してもらうため、確認DMを試しに送る
    try:
        await interaction.user.send(f"{sid}の{nickname}さんの通知登録が完了しました。")
        await interaction.followup.send(
            f"連携が完了しました！ 確認のDMを送信しましたので届いているか確認してください。",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "連携情報は保存しましたが、確認DMを送れませんでした。\n"
            "サーバーアイコンを右クリック →「プライバシー設定」→「ダイレクトメッセージ」をオンにしてから、"
            "StudyLogでもう一度コードを発行し /id連携 を実行してください。",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(
            f"連携情報は保存しましたが、確認DMの送信中にエラーが発生しました: {e}",
            ephemeral=True
        )


# ================================
#  /help
# ================================
@bot.tree.command(name="help", description="使えるコマンド一覧")
async def help_command(interaction: discord.Interaction):
    msg = (
        "📘 **使えるコマンド一覧**\n\n"
        "**/add** — 予定を登録する\n"
        "**/list** — 予定を表示する\n"
        "**/delete** — 予定を削除する\n"
        "**/edit** — 予定を編集する\n"
        "**/setchannel** — 通知チャンネルを設定する（通生／寮生／お知らせ用を選択可）\n"
        "**/setup_roles** — 通生/寮生 振り分けパネルを投稿する\n"
        "**/id連携** — StudyLogにログインして発行した連携コードを使い、DiscordアカウントをStudyLogと連携する（DM通知を受け取れるようになる）\n"
        "**webページ** - https://1istudyweb.pages.dev/\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)

# ================================
#  自動通知
# ================================
TOMORROW_NOTIFY_CHANNEL_KEYS = ("remind_channel_id", "remind_channel_id_dorm")  # 通生・寮生 両方に送信

# ★ フロント側（plan.js）が備考をcontent文字列に埋め込む際の区切り文字列。
#   通知メッセージには備考を含めず、予定本文だけを送るためここで取り除く。
NOTE_SEP = "\n📝備考："

def strip_note(content: str) -> str:
    if not content:
        return content
    return content.split(NOTE_SEP)[0]


def is_holiday(guild_id: int, date_str: str) -> bool:
    """
    指定した日付が「休校」（時間割の holiday:YYYY-MM-DD オーバーライド）に
    設定されているかどうかを返す。
    """
    tt = load_timetable(guild_id)
    ov = tt.get(f"holiday:{date_str}")
    return bool(ov) and ov.get("type") == "holiday"


async def send_tomorrow_plans():
    # 実行日が金曜(4)・土曜(5) の場合は「金曜夜」「土曜夜」の通知にあたるため、
    # 予定が無ければ通知自体をスキップする
    now = datetime.now(JST)
    quiet_if_empty = now.weekday() in (4, 5)  # 4=金, 5=土
    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        config = load_config(guild_id)
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        plans = [p for p in load_plans(guild_id) if p["date"] == tomorrow]
        if plans:
            msg = "こんばんは！明日の予定です。\n"
            for p in plans:
                msg += f"・{p['subject']} {strip_note(p['content'])}\n"
        else:
            # 明日が休校で、かつ予定も入っていない場合は通知自体をスキップする
            if quiet_if_empty or is_holiday(guild_id, tomorrow):
                continue
            msg = "こんばんは！明日の予定はありません。\n"

        # 通生用・寮生用の両チャンネルへ、それぞれ設定されていれば送信
        for config_key in TOMORROW_NOTIFY_CHANNEL_KEYS:
            channel_id = config.get(config_key)
            if not channel_id:
                continue
            channel = bot.get_channel(channel_id)
            if not channel:
                continue
            await channel.send(msg + "@everyone")

async def send_today_plans_for(config_key: str):
    """
    朝の「今日の予定」通知を config_key で指定したチャンネル宛に送る。
    config_key: "remind_channel_id"（通生） または "remind_channel_id_dorm"（寮生）
    """
    now = datetime.now(JST)
    # 実行日が土曜(5)・日曜(6) の場合は「土曜朝」「日曜朝」の通知にあたるため、
    # 予定が無ければ通知自体をスキップする
    quiet_if_empty = now.weekday() in (5, 6)  # 5=土, 6=日
    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        config = load_config(guild_id)
        channel_id = config.get(config_key)
        if not channel_id:
            continue
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        today = now.strftime("%Y-%m-%d")
        plans = [p for p in load_plans(guild_id) if p["date"] == today]
        if plans:
            msg = "おはようございます！今日の予定です。\n"
            for p in plans:
                msg += f"・{p['subject']} {strip_note(p['content'])}\n"
        else:
            # 今日が休校で、かつ予定も入っていない場合は通知自体をスキップする
            if quiet_if_empty or is_holiday(guild_id, today):
                continue
            msg = "おはようございます！今日の予定はありません。\n"
        await channel.send(msg + "@everyone")

async def send_today_plans_commute():
    """通生向け：朝5:30の通知（既存のremind_channel_idを使用）"""
    await send_today_plans_for("remind_channel_id")

async def send_today_plans_dorm():
    """寮生向け：朝7:20の通知（remind_channel_id_dormを使用）"""
    await send_today_plans_for("remind_channel_id_dorm")

WEEKLY_NOTIFY_CHANNEL_KEYS = ("remind_channel_id", "remind_channel_id_dorm")  # 通生・寮生 両方に送信
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

async def send_weekly_plans():
    """
    毎週日曜14:00に、翌日（月曜）から日曜までの「今週の予定」をまとめて通知する。
    """
    now = datetime.now(JST)
    week_start = (now + timedelta(days=1)).date()   # 翌日の月曜
    week_end = week_start + timedelta(days=6)        # その週の日曜

    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        config = load_config(guild_id)
        plans = [
            p for p in load_plans(guild_id)
            if week_start <= datetime.strptime(p["date"], "%Y-%m-%d").date() <= week_end
        ]

        if plans:
            plans_by_date = {}
            for p in plans:
                plans_by_date.setdefault(p["date"], []).append(p)

            msg = f"📅 今週（{week_start.strftime('%m/%d')}〜{week_end.strftime('%m/%d')}）の予定です！\n"
            for date_str in sorted(plans_by_date.keys()):
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                msg += f"\n**{d.strftime('%m/%d')}（{WEEKDAY_JP[d.weekday()]}）**\n"
                for p in plans_by_date[date_str]:
                    msg += f"・{p['subject']} {strip_note(p['content'])}\n"
        else:
            msg = f"📅 今週（{week_start.strftime('%m/%d')}〜{week_end.strftime('%m/%d')}）の予定はありません。\n"

        for config_key in WEEKLY_NOTIFY_CHANNEL_KEYS:
            channel_id = config.get(config_key)
            if not channel_id:
                continue
            channel = bot.get_channel(channel_id)
            if not channel:
                continue
            await channel.send(msg + "\n@everyone")

# ================================
#  ★ 勉強タイマーの定期チェック（3時間経過ごとの自動休憩の検知）
#  ─────────────────────────────
#  クライアントのタブが開いているかどうかに関係なく、サーバー側で
#  一定間隔ごとに全ギルドの study_timers を評価し、累計3時間ごとの
#  自動休憩（一時停止）への切り替えを確実に検知してDM通知する。
#  （各APIエンドポイント側でもアクセス時に同じ判定を行っているため、
#    この定期ジョブは「誰もアプリを開かなかった場合」の保険として働く。
#    2重で判定しても _finalize_study_timer は冪等なので問題ない）
# ================================
async def check_study_timers():
    now_ms = int(time.time() * 1000)
    for filename in list_all_configs():
        try:
            guild_id = int(filename.replace("config_", "").replace(".json", ""))
        except ValueError:
            continue

        try:
            timers = await async_load_study_timers(guild_id)
        except Exception as e:
            print(f"[WARN] study_timers 読み込みに失敗しました（guild_id={guild_id}）: {e}")
            continue
        if not timers:
            continue

        new_timers = dict(timers)
        notifications = []
        changed = False
        for student_id, entry in timers.items():
            new_entry, notify_kind = _finalize_study_timer(entry, now_ms)
            if new_entry != entry:
                changed = True
                if new_entry is None:
                    new_timers.pop(student_id, None)
                else:
                    new_timers[student_id] = new_entry
            if notify_kind:
                notifications.append((student_id, notify_kind))

        if not changed:
            continue

        try:
            await async_save_study_timers(guild_id, new_timers)
        except DataWriteError as e:
            print(f"[WARN] study_timers 保存に失敗しました（guild_id={guild_id}）。次回のチェックで再試行します: {e}")
            continue

        for student_id, kind in notifications:
            _try_notify_timer(guild_id, student_id, MSG_TIMER_AUTO_PAUSED)

# ================================
#  Flask API — 予定管理
# ================================
@app.route("/channels", methods=["GET"])
def get_channels():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    # ★ Botがまだ準備完了していない（再接続中・起動直後など）場合、
    #    bot.get_guild() は必ず None を返してしまい、フロント側には
    #    「guild not found」という誤解を招くメッセージが出ていた。
    #    準備中であることを明示的に区別して返す。
    if not bot.is_ready():
        return jsonify({"ok": False, "error": "bot_not_ready", "message": "Botが起動中です。数秒後にもう一度お試しください。"})
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "guild not found"})
    channels = [{"id": str(ch.id), "name": ch.name} for ch in get_subject_channels(guild)]
    return jsonify({"ok": True, "channels": channels})

@app.route("/add_schedule", methods=["POST"])
def add_schedule():
    data     = request.json
    guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    date     = data.get("date")
    subject  = data.get("subject")
    category = data.get("category")
    content  = data.get("content")
    points   = data.get("points")  # ★ 追加（提出・宿題のみ有効。省略時は5pt）

    if not all([date, subject, category, content]):
        return jsonify({"ok": False, "error": "missing fields"})

    err = reject_if_bug_chars({"科目": subject, "カテゴリ": category, "内容": content})
    if err:
        return err

    if points is not None:
        try:
            points = int(points)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid points"})

    guild = bot.get_guild(int(guild_id))
    future = asyncio.run_coroutine_threadsafe(
        add_plan_internal(int(guild_id), subject, date, category, content, points),
        bot.loop
    )
    ok, msg, detail = future.result(timeout=30)
    if ok:
        log_event("schedule", f"予定「{subject}」を追加しました（{date}）。", actor=nickname, detail=detail)
    if ok and guild:
        target_channel = get_subject_channel_by_name(guild, subject)
        if target_channel:
            asyncio.run_coroutine_threadsafe(
                target_channel.send(msg), bot.loop
            ).result(timeout=10)
    return jsonify({"ok": ok, "message": msg})

BASE_MAX_LOG_MINUTES = 180  # ★ タイマーを使っていない場合（手入力等）の上限。タイマー使用時は自動休憩を挟むたびに+3時間される
MANUAL_COOLDOWN_SEC = 20  # ★ 手入力：連続記録は前回から20秒あける（連打対策）
MANUAL_DAILY_MAX_MINUTES = 960  # ★ 1日の記録合計の上限（16時間）。短時間の連投による水増し防止

def _parse_log_time(log):
    """ログの正確な時刻（"time"）をdatetimeに変換する。無ければNone。"""
    t = log.get("time")
    if not t:
        return None
    try:
        return datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    except Exception:
        return None

def _latest_log_time(candidates):
    """candidates（ログのリスト）の中で最も新しい時刻を返す。無ければNone。"""
    latest = None
    for l in candidates:
        t = _parse_log_time(l)
        if t and (latest is None or t > latest):
            latest = t
    return latest

@app.route("/add_study_log", methods=["POST"])
def add_study_log():
    data = request.json or {}
    guild_id = int(data.get("guild_id"))

    # --- ★ 本人確認：クライアントが自己申告する student_id は一切信用せず、
    #     /login で発行済みのセッショントークンから本人の student_id を
    #     特定する（なりすまし防止）。 ---
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    # --- ★ nickname もクライアントの自己申告ではなく、サーバー側のユーザー
    #     データから引き直す（表示名の詐称防止） ---
    user = find_user(guild_id, student_id)
    nickname = user["nickname"] if user else data.get("nickname")

    # --- ★ minutes の検証（不正な値・異常に大きい値を拒否） ---
    #   上限は固定180分ではなく、実際にタイマーで自動休憩（3時間ごとの
    #   チェックポイント）を何回挟んだかに応じて「3時間 → 6時間 → 9時間…」
    #   と自動的に引き上がる（study_timersのnext_checkpoint_secを参照）。
    #   タイマーを使っていない（手入力等）場合は基本の3時間が上限。
    minutes = data.get("minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        return jsonify({"ok": False, "error": "invalid minutes"})

    timer_entry = load_study_timers(guild_id).get(student_id)
    if timer_entry and timer_entry.get("next_checkpoint_sec"):
        max_log_minutes = int(timer_entry["next_checkpoint_sec"] // 60)
    else:
        max_log_minutes = BASE_MAX_LOG_MINUTES

    if minutes < 1 or minutes > max_log_minutes:
        return jsonify({"ok": False, "error": f"minutes must be between 1 and {max_log_minutes}"})

    subject = data.get("subject")
    memo    = data.get("memo")
    method  = data.get("method")  # ★ "timer" または "manual"（フロント側から送信）
    if not subject:
        return jsonify({"ok": False, "error": "missing fields"})

    # --- ★ 制御文字・不可視文字・壊れた符号位置を弾く ---
    err = reject_if_bug_chars({"科目": subject, "メモ": memo, "ニックネーム": nickname})
    if err:
        return err

    now_jst = datetime.now(JST)
    logs = load_study_logs(guild_id)

    # --- ★ 不正防止：前回の記録からの実経過時間チェック（サーバー側の最終防衛）---
    #   クライアント側（StudyLog.js）でも同様のチェックを行っているが、
    #   devtools等で直接APIを叩けば素通りしてしまうため、サーバー側でも
    #   独立して判定する。
    #   ・タイマー記録：本人の（教科を問わない）前回の記録から、今回記録
    #     しようとしている分数以上の実時間が経過していないと拒否する
    #     （タイマーの経過時間を改ざんして即座に長時間記録するのを防止）
    #   ・手入力：同じ教科での連続記録は、前回の記録から1分経過していないと拒否
    my_logs = [l for l in logs if l.get("student_id") == student_id]

    if method == "timer":
        last_time = _latest_log_time(my_logs)
        if last_time:
            elapsed_sec  = (now_jst - last_time).total_seconds()
            required_sec = minutes * 60
            if elapsed_sec < required_sec:
                remain_min = int((required_sec - elapsed_sec) // 60) + 1
                return jsonify({
                    "ok": False,
                    "error": f"前回の記録からまだ十分な時間が経過していません（あと約{remain_min}分待つ必要があります）"
                })
    elif method == "manual":
        # ★ 2026-08-17、教科名を毎回変えながら連投することで「同じ教科」判定の
        #   1分クールダウンを回避され、10分間に34件・180分ずつの水増し記録を
        #   入れられる被害が発生した。「同じ教科」に加えて「本人の直近ログ
        #   （教科不問）」からの経過時間もチェックすることで、教科を変える
        #   だけの連投そのものを防ぐ。
        last_time_any = _latest_log_time(my_logs)
        if last_time_any:
            elapsed_sec_any = (now_jst - last_time_any).total_seconds()
            if elapsed_sec_any < MANUAL_COOLDOWN_SEC:
                remain_sec = int(MANUAL_COOLDOWN_SEC - elapsed_sec_any) + 1
                return jsonify({
                    "ok": False,
                    "error": f"記録は、前回から{MANUAL_COOLDOWN_SEC}秒経ってから行えます（あと{remain_sec}秒）"
                })

        same_subject_logs = [l for l in my_logs if l.get("subject") == subject]
        last_time = _latest_log_time(same_subject_logs)
        if last_time:
            elapsed_sec = (now_jst - last_time).total_seconds()
            if elapsed_sec < MANUAL_COOLDOWN_SEC:
                remain_sec = int(MANUAL_COOLDOWN_SEC - elapsed_sec) + 1
                return jsonify({
                    "ok": False,
                    "error": f"同じ教科の記録は、前回から{MANUAL_COOLDOWN_SEC}秒経ってから行えます（あと{remain_sec}秒）"
                })

        # ★ 1分間隔さえ守れば教科を変えて延々と積み上げられてしまうため、
        #   1日（本人の全記録合計・教科不問）にも上限を設ける。
        today_str = now_jst.strftime("%Y-%m-%d")
        today_total = sum(l.get("minutes", 0) for l in my_logs if l.get("date") == today_str)
        if today_total + minutes > MANUAL_DAILY_MAX_MINUTES:
            return jsonify({
                "ok": False,
                "error": f"1日の記録合計の上限（{MANUAL_DAILY_MAX_MINUTES}分）を超えます"
            })

    # --- ★ date/time はクライアントの値を信用せず、サーバー（JST）の値を使う ---
    #     → PCの時計を進めても戻しても、記録される日時は実際の日時のまま変わらない
    entry = {
        "date": now_jst.strftime("%Y-%m-%d"),
        "time": now_jst.strftime("%Y-%m-%d %H:%M:%S"),  # ★ 不正防止チェック用の正確な時刻
        "subject": subject,
        "minutes": minutes,
        "memo": memo,
        "student_id": student_id,
        "nickname": nickname
    }

    # 30日以上前のログを削除
    now = now_jst.date()
    logs = [
        l for l in logs
        if (now - datetime.strptime(l["date"], "%Y-%m-%d").date()).days <= 30
    ]
    old_logs_text = _study_logs_text(logs)  # ★ 運用ログでファイル全体の差分を見せるため、
                                             #   30日以上前のログを間引いた"後"（＝今回の記録による
                                             #   変化だけが差分に出るように）に控えておく
    logs.append(entry)
    try:
        save_study_logs(guild_id, logs)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    # --- ポイント加算（5分ごとに1pt） ---
    earned = entry["minutes"] // 5
    pts = load_points(guild_id)
    pts[entry["student_id"]] = pts.get(entry["student_id"], 0) + earned
    try:
        save_points(guild_id, pts)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    change = file_diff(f"study_logs_{guild_id}.json", old_logs_text, _study_logs_text(logs))
    log_event(
        "study",
        f"学習ログ「{subject}」を記録しました（{entry['minutes']}分・{earned}pt加算）。",
        actor=nickname,
        detail=[change] if change else None,
    )
    return jsonify({"ok": True, "earned": earned, "total": pts[entry["student_id"]]})

@app.route("/delete_study_log", methods=["POST"])
def delete_study_log():
    """
    自分の勉強ログ1件を削除する（2026/08/19追加）。
    ・本人の記録以外は削除できない（student_idはセッションから特定、
      クライアント自己申告は信用しない）。
    ・記録済みのポイント（5分ごとに1pt）はその分だけ差し引く（0未満にはしない）。
    ・study_logs_{guild_id}.jsonは全生徒共有の1ファイルなので、ここから
      該当エントリを取り除くだけで「みんなの記録」からも自動的に消える
      （StudyLog.jsのrenderEveryone()は同じファイルを見ているため）。
    ・エントリを一意に特定するキーは、専用のidを新設せず既存の"time"
      （秒単位で記録される日時。本人の記録内で衝突する心配は実質無い）を使う。
      これにより、この機能を追加する前からあった過去のログもそのまま削除できる。
    """
    data = request.json or {}
    guild_id, student_id, nickname, err = require_login_json(data)
    if err:
        return err
    time_key = data.get("time")
    if not time_key:
        return jsonify({"ok": False, "error": "missing time"})

    logs = load_study_logs(guild_id)
    target = next((l for l in logs if l.get("student_id") == student_id and l.get("time") == time_key), None)
    if not target:
        return jsonify({"ok": False, "error": "log not found"})

    old_logs_text = _study_logs_text(logs)
    new_logs = [l for l in logs if l is not target]
    try:
        save_study_logs(guild_id, new_logs)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    # --- ポイント減算（記録時に加算した分だけ差し引く。0未満にはしない） ---
    earned = (target.get("minutes") or 0) // 5
    pts = load_points(guild_id)
    pts[student_id] = max(0, pts.get(student_id, 0) - earned)
    try:
        save_points(guild_id, pts)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    change = file_diff(f"study_logs_{guild_id}.json", old_logs_text, _study_logs_text(new_logs))
    log_event(
        "study",
        f"学習ログ「{target.get('subject')}」を削除しました（{target.get('minutes')}分・{earned}pt減算）。",
        actor=nickname,
        detail=[change] if change else None,
    )
    return jsonify({"ok": True, "total": pts[student_id]})


@app.route("/list_schedule", methods=["GET"])
def list_schedule():
    """
    ★ scope 未指定時は従来通り全件を返す（Timetable/StudyLogなど、
      過去分も含めた全件が必要な既存の呼び出し元との互換性を保つため）。
    ・scope=future : 今日以降の予定を全件（日付昇順）で返す。
      未来の予定は運用上そこまで多くならないため、ページングはしない。
    ・scope=past   : 今日より前の予定を、直近の過去から順（日付降順）に
      offset/limit でページングして返す。予定を自動削除しなくなった分、
      過去分は年月とともに増え続けるため、一覧画面はこちらを使って
      「これからの予定を先に表示 → 過去分は少しずつ追加読み込み」できるようにする。
      has_more が true の間は、まだ続きがあることを示す。
    """
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    plans = sorted(load_plans(int(guild_id)), key=lambda p: p["date"])

    scope = request.args.get("scope")
    if not scope:
        return jsonify({"ok": True, "plans": plans})

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    if scope == "future":
        future = [p for p in plans if p["date"] >= today_str]
        return jsonify({"ok": True, "plans": future})

    if scope == "past":
        past = [p for p in plans if p["date"] < today_str]
        past.reverse()  # 直近の過去から順
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit  = max(1, min(200, int(request.args.get("limit", 50))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid offset/limit"})
        page = past[offset: offset + limit]
        return jsonify({
            "ok": True,
            "plans": page,
            "total": len(past),
            "has_more": offset + limit < len(past),
        })

    return jsonify({"ok": False, "error": "invalid scope"})



@app.route("/edit_schedule", methods=["POST"])
def edit_schedule():
    data         = request.json
    guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    target       = data.get("target")
    new_date     = data.get("date")
    new_subject  = data.get("subject")
    new_category = data.get("category")
    new_content  = data.get("content")
    new_points   = data.get("points")  # ★ 追加

    if not target:
        return jsonify({"ok": False, "error": "missing fields"})

    err = reject_if_bug_chars({"科目": new_subject, "カテゴリ": new_category, "内容": new_content})
    if err:
        return err

    guild    = bot.get_guild(guild_id)
    plans    = load_plans(guild_id)
    old_plans_text = _plans_text(plans)  # ★ 運用ログでファイル全体の差分を見せるため、変更前に控えておく
    found = None
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            found = p
            break
    if not found:
        return jsonify({"ok": False, "error": "plan not found"})
    before_str = f"{found['date']} / {found['subject']} / {found['content']}"
    if new_date:
        date_str = parse_date(new_date)
        if not date_str:
            return jsonify({"ok": False, "error": "invalid date"})
        found["date"] = date_str
    if new_subject:
        found["subject"] = new_subject
    if new_category and new_content:
        found["content"] = f"【{new_category}】{new_content}"
    elif new_category:
        body = found["content"].split("】", 1)[1] if "】" in found["content"] else found["content"]
        found["content"] = f"【{new_category}】{body}"
    elif new_content:
        tag = found["content"].split("】", 1)[0] + "】" if "】" in found["content"] else ""
        found["content"] = f"{tag}{new_content}"

    # ★ ポイント更新
    if new_points is not None:
        try:
            found["points"] = int(new_points)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid points"})

    current_category = found["content"].split("】", 1)[0].lstrip("【") if "】" in found["content"] else ""
    if current_category not in POINT_CATEGORIES and "points" in found:
        # 提出・宿題以外に変更された場合はポイントを外す
        del found["points"]
    elif current_category in POINT_CATEGORIES and "points" not in found:
        found["points"] = DEFAULT_TASK_POINTS

    try:
        save_plans(guild_id, plans)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    after_str = f"{found['date']} / {found['subject']} / {found['content']}"
    write_log(guild_id, "edit", detail=f"{before_str} → {after_str}")
    change = file_diff(f"plans_{guild_id}.json", old_plans_text, _plans_text(plans))
    log_event(
        "schedule",
        f"予定「{found['subject']}」を編集しました（{found['date']}）。",
        actor=nickname,
        detail=[change] if change else None,
    )
    if guild:
        target_channel = get_subject_channel_by_name(guild, found["subject"])
        if target_channel:
            msg = f"編集しました！\n\n【編集前】\n{before_str}\n\n【編集後】\n{after_str}"
            if "points" in found:
                msg += f"\n⭐ {found['points']}pt"
            asyncio.run_coroutine_threadsafe(
                target_channel.send(msg), bot.loop
            ).result(timeout=10)
    return jsonify({"ok": True, "message": f"編集しました！\n{before_str} → {after_str}"})

@app.route("/delete_schedule", methods=["POST"])
def delete_schedule():
    data     = request.json
    guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    target   = data.get("target")
    if not target:
        return jsonify({"ok": False, "error": "missing fields"})
    guild     = bot.get_guild(guild_id)
    plans     = load_plans(guild_id)
    deleted   = None
    new_plans = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            deleted = p
        else:
            new_plans.append(p)
    if not deleted:
        return jsonify({"ok": False, "error": "plan not found"})
    try:
        save_plans(guild_id, new_plans)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    detail = f"{deleted['date']} / {deleted['subject']} / {deleted['content']}"
    write_log(guild_id, "delete", detail=detail)
    change = file_diff(f"plans_{guild_id}.json", _plans_text(plans), _plans_text(new_plans))
    log_event(
        "schedule",
        f"予定「{deleted['subject']}」を削除しました（{deleted['date']}）。",
        actor=nickname,
        detail=[change] if change else None,
    )
    if guild:
        target_channel = get_subject_channel_by_name(guild, deleted["subject"])
        if target_channel:
            asyncio.run_coroutine_threadsafe(
                target_channel.send(f"削除しました！\n{target}"), bot.loop
            ).result(timeout=10)
    return jsonify({"ok": True, "message": "削除しました！"})

@app.route("/list_logs", methods=["GET"])
def list_logs():
    """
    ★ offset/limit 未指定時は従来通り全件を返す（互換性維持）。
      指定された場合は、新しい順に並んだログを offset/limit でページングして
      返す（has_more で続きの有無を伝える）。ログ画面は「最近のログから
      少しずつ読み込み、必要になったら『もっと読み込む』で追加取得する」
      使い方を想定している。
    """
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    logs, _ = local_get(f"logs_{guild_id}.json")
    logs = sorted(logs or [], key=lambda l: l["time"], reverse=True)

    if request.args.get("offset") is None and request.args.get("limit") is None:
        return jsonify({"ok": True, "logs": logs})

    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit  = max(1, min(200, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid offset/limit"})
    page = logs[offset: offset + limit]
    return jsonify({
        "ok": True,
        "logs": page,
        "total": len(logs),
        "has_more": offset + limit < len(logs),
    })

# ================================
#  Flask API — 時間割
# ================================
def load_timetable(guild_id: int):
    data, _ = local_get(f"timetable_{guild_id}.json")
    return data or {}

def save_timetable(guild_id: int, data: dict):
    _, sha = local_get(f"timetable_{guild_id}.json")
    local_put(f"timetable_{guild_id}.json", data, sha)
    notify_change(guild_id)

_TIMETABLE_TYPE_LABELS = {"change": "授業変更", "holiday": "休校", "period_holiday": "1コマ休み"}

def _timetable_entry_lines(e):
    """運用ログ用：時間割の変更1件を { ... } のブロックにする。"""
    t = e.get("type")
    fields = [
        ("種別", _TIMETABLE_TYPE_LABELS.get(t, t)),
        ("日付", e.get("date")),
    ]
    if t in ("change", "period_holiday"):
        fields.append(("時限", f"{e.get('period')}限"))
    if t == "change":
        fields.append(("科目", e.get("subject")))
        fields.append(("持ち物", "、".join(e.get("items") or []) or "(なし)"))
    else:
        fields.append(("理由", e.get("reason")))
    fields.append(("備考", e.get("note") or "(なし)"))
    return _json_block(fields)

def _timetable_text(tt):
    lines = []
    for e in (tt or {}).values():
        lines.extend(_timetable_entry_lines(e))
    return "\n".join(lines)

@app.route("/list_timetable", methods=["GET"])
def list_timetable():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    data = load_timetable(int(guild_id))
    overrides = [{"key": k, **v} for k, v in data.items()]
    return jsonify({"ok": True, "overrides": overrides})

@app.route("/update_timetable", methods=["POST"])
def update_timetable():
    data     = request.json
    guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    key      = data.get("key")
    if not key:
        return jsonify({"ok": False, "error": "missing fields"})

    err = reject_if_bug_chars({"科目": data.get("subject"), "備考": data.get("note")})
    if err:
        return err

    tt = load_timetable(guild_id)
    old_tt_text = _timetable_text(tt)  # ★ 運用ログでファイル全体の差分を見せるため、上書き前に控えておく
    tt[key] = {
        "key":     key,
        "type":    "change",
        "date":    data.get("date"),
        "period":  data.get("period"),
        "subject": data.get("subject"),
        "items":   data.get("items", []),
        "note":    data.get("note", ""),
    }
    try:
        save_timetable(guild_id, tt)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    tt_detail = f"時間割変更: {key} → {data.get('subject')}"
    write_log(guild_id, "edit", detail=tt_detail)
    change = file_diff(f"timetable_{guild_id}.json", old_tt_text, _timetable_text(tt))
    log_event(
        "timetable",
        f"時間割「{data.get('subject')}」を更新しました（{data.get('date')}）。",
        actor=nickname,
        detail=[change] if change else None,
    )
    return jsonify({"ok": True})

@app.route("/set_holiday", methods=["POST"])
def set_holiday():
    data     = request.json
    guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    key      = data.get("key")
    if not key:
        return jsonify({"ok": False, "error": "missing fields"})
    tt = load_timetable(guild_id)
    old_tt_text = _timetable_text(tt)
    tt[key] = {
        "key":    key,
        "type":   "holiday",
        "date":   data.get("date"),
        "reason": data.get("reason", "休校"),
        "note":   data.get("note", ""),
    }
    try:
        save_timetable(guild_id, tt)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    holiday_detail = f"休校設定: {data.get('date')} {data.get('reason')}"
    write_log(guild_id, "edit", detail=holiday_detail)
    change = file_diff(f"timetable_{guild_id}.json", old_tt_text, _timetable_text(tt))
    log_event(
        "timetable",
        f"休校設定「{data.get('date')}」を更新しました。",
        actor=nickname,
        detail=[change] if change else None,
    )
    return jsonify({"ok": True})

@app.route("/set_period_holiday", methods=["POST"])
def set_period_holiday():
    """
    1コマだけの休み（period_holiday）。
    ★ これまでこのエンドポイントが未実装だったため、フロント側
       （Timetable.js）が保存に失敗してもエラーを握りつぶしてしまい、
       localStorageにしか残らず「他の端末では反映されない／たまに消える」
       原因になっていた。/set_holiday と同じ要領でサーバー側
       （timetable_{guild_id}.json）に保存する。
    """
    data     = request.json
    guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    key      = data.get("key")
    period   = data.get("period")
    if not key or period is None:
        return jsonify({"ok": False, "error": "missing fields"})
    tt = load_timetable(guild_id)
    old_tt_text = _timetable_text(tt)
    tt[key] = {
        "key":    key,
        "type":   "period_holiday",
        "date":   data.get("date"),
        "period": period,
        "reason": data.get("reason", "休み"),
        "note":   data.get("note", ""),
    }
    try:
        save_timetable(guild_id, tt)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    period_holiday_detail = f"1コマ休み設定: {data.get('date')} {period}限 {data.get('reason')}"
    write_log(guild_id, "edit", detail=period_holiday_detail)
    change = file_diff(f"timetable_{guild_id}.json", old_tt_text, _timetable_text(tt))
    log_event(
        "timetable",
        f"休み設定「{data.get('date')} {period}限」を更新しました。",
        actor=nickname,
        detail=[change] if change else None,
    )
    return jsonify({"ok": True})

@app.route("/delete_timetable", methods=["POST"])
def delete_timetable():
    data     = request.json
    guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    key      = data.get("key")
    if not key:
        return jsonify({"ok": False, "error": "missing fields"})
    tt = load_timetable(guild_id)
    if key in tt:
        old_entry = tt[key]
        old_tt_text = _timetable_text(tt)
        del tt[key]
        try:
            save_timetable(guild_id, tt)
        except DataWriteError as e:
            return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
        write_log(guild_id, "edit", detail=f"時間割変更削除: {key}")
        change = file_diff(f"timetable_{guild_id}.json", old_tt_text, _timetable_text(tt))
        log_event(
            "timetable",
            f"時間割の変更「{old_entry.get('date')}」を削除しました。",
            actor=nickname,
            detail=[change] if change else None,
        )
    return jsonify({"ok": True})

# ================================
#  Flask API — 学期ごとの基本時間割（前期・後期など）
# ================================
#  ・「前期」「後期」のように、期間ごとにまるごと違う基本時間割（曜日×時限の
#    科目・持ち物）を切り替えられるようにするための機能。
#  ・1件 = { id, name, start_date, end_date, timetable: {mon:[...],...} }
#  ・start_date〜end_date に対象日が入っていれば、その学期の時間割を
#    ベースとして使う（フロント側 Timetable.js の getTimetableForDate 参照）。
#  ・既存の change / holiday / period_holiday オーバーライドは、この学期の
#    ベース時間割の上にそのまま重ねて適用されるので、前期のデータをいじらずに
#    後期分を新規に追加・編集できる。
def load_terms(guild_id: int):
    data, _ = local_get(f"terms_{guild_id}.json")
    return data or {}

def save_terms(guild_id: int, terms: dict):
    _, sha = local_get(f"terms_{guild_id}.json")
    local_put(f"terms_{guild_id}.json", terms, sha)
    notify_change(guild_id)

_TERM_DAY_LABELS = {"mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金"}

def _term_lines(t):
    """運用ログ用：学期の基本時間割1件を { ... } のブロックにする。
    ★ 2026/08/19、以前はコマ数の合計だけを出していて、曜日・時限ごとの
    科目を入れ替えてもdiffに出ない（コマ数が変わらなければ検知できない）
    抜けがあったため、曜日ごとに実際の科目を1フィールドで並べるよう修正。"""
    fields = [
        ("学期名", t.get('name')),
        ("開始日", t.get('start_date')),
        ("終了日", t.get('end_date')),
    ]
    tt = t.get("timetable") or {}
    for day_key, label in _TERM_DAY_LABELS.items():
        periods = tt.get(day_key) or []
        if not periods:
            continue
        subjects = "、".join(
            f"{i+1}限:{(p.get('subject') if isinstance(p, dict) else None) or '(空きコマ)'}"
            for i, p in enumerate(periods)
        )
        fields.append((f"{label}曜", subjects))
    return _json_block(fields)

def _terms_text(terms):
    lines = []
    for t in (terms or {}).values():
        lines.extend(_term_lines(t))
    return "\n".join(lines)

@app.route("/list_terms", methods=["GET"])
def list_terms():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    terms = load_terms(int(guild_id))
    return jsonify({"ok": True, "terms": list(terms.values())})

@app.route("/save_term", methods=["POST"])
def save_term():
    data       = request.json or {}
    guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    name       = data.get("name")
    start_date = data.get("start_date")
    end_date   = data.get("end_date")
    timetable  = data.get("timetable")
    if not all([name, start_date, end_date]) or not isinstance(timetable, dict):
        return jsonify({"ok": False, "error": "missing fields"})
    if end_date < start_date:
        return jsonify({"ok": False, "error": "終了日は開始日以降にしてください"})

    err = reject_if_bug_chars({"学期名": name})
    if err:
        return err

    terms = load_terms(guild_id)
    term_id = data.get("id") or f"term_{time.time_ns()}"

    # ★ 期間の重複チェック（自分自身は除く）。前期・後期が重なると
    #   どちらの時間割を使うべきか曖昧になるため保存前に弾く。
    for tid, t in terms.items():
        if tid == term_id:
            continue
        if start_date <= t.get("end_date", "") and t.get("start_date", "") <= end_date:
            return jsonify({"ok": False, "error": f"「{t.get('name')}」（{t.get('start_date')}〜{t.get('end_date')}）と期間が重なっています"})

    old_terms_text = _terms_text(terms)  # ★ 運用ログでファイル全体の差分を見せるため、上書き前に控えておく
    terms[term_id] = {
        "id":         term_id,
        "name":       name,
        "start_date": start_date,
        "end_date":   end_date,
        "timetable":  timetable,
    }
    try:
        save_terms(guild_id, terms)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    term_detail = f"学期時間割保存: {name}（{start_date}〜{end_date}）"
    write_log(guild_id, "edit", detail=term_detail)
    change = file_diff(f"terms_{guild_id}.json", old_terms_text, _terms_text(terms))
    log_event(
        "timetable",
        f"学期時間割「{name}」を保存しました。",
        actor=nickname,
        detail=[change] if change else None,
    )
    return jsonify({"ok": True, "id": term_id})

@app.route("/delete_term", methods=["POST"])
def delete_term():
    data     = request.json or {}
    guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    term_id  = data.get("id")
    if not term_id:
        return jsonify({"ok": False, "error": "missing fields"})
    terms = load_terms(guild_id)
    if term_id in terms:
        name = terms[term_id].get("name", term_id)
        old_terms_text = _terms_text(terms)
        del terms[term_id]
        try:
            save_terms(guild_id, terms)
        except DataWriteError as e:
            return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
        write_log(guild_id, "edit", detail=f"学期時間割削除: {name}")
        change = file_diff(f"terms_{guild_id}.json", old_terms_text, _terms_text(terms))
        log_event(
            "timetable",
            f"学期時間割「{name}」を削除しました。",
            actor=nickname,
            detail=[change] if change else None,
        )
    return jsonify({"ok": True})

# ================================
#  Flask API — ユーザー認証
# ================================
@app.route("/get_users", methods=["GET"])
def get_users():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    try:
        users = load_users(int(guild_id))
        # ★ password_hash / password_salt は絶対に外部に出さない。
        #   このAPIは認証なしで誰でも呼べるので、公開してよい項目だけに絞る。
        public_users = [
            {"id": u.get("id"), "nickname": u.get("nickname"), "created_at": u.get("created_at"),
             "has_password": bool(u.get("password_hash"))}
            for u in users
        ]
        return jsonify({"ok": True, "users": public_users})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/check_student", methods=["GET"])
def check_student():
    """
    ログイン画面（Login.js）が「この学籍番号は登録済みか」だけを確認するための
    最小限API。
    ★ 以前は同じ目的でも /get_users を呼んでおり、学籍番号を1文字でも入力する
      （またはパスワード未入力でログインボタンを押す）たびに、認証なしで
      全生徒の学籍番号・ニックネーム一覧が丸ごとブラウザに返ってしまっていた。
      問い合わせた1件についてだけ最小限の情報を返すことで、全生徒名簿が
      漏れるのを防ぐ。
    """
    guild_id   = request.args.get("guild_id")
    student_id = (request.args.get("id") or "").strip().upper()
    if not guild_id or not student_id:
        return jsonify({"ok": False, "error": "missing fields"})
    try:
        guild_id = int(guild_id)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid guild_id"})
    try:
        user = find_user(guild_id, student_id)
        if not user:
            return jsonify({"ok": True, "exists": False})
        return jsonify({
            "ok": True,
            "exists": True,
            "nickname": user.get("nickname"),
            "has_password": bool(user.get("password_hash")),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/add_user", methods=["POST"])
def add_user():
    data     = request.json
    guild_id = data.get("guild_id")
    user_id  = data.get("id", "").strip().upper()
    nickname = data.get("nickname", "").strip()
    password = data.get("password") or ""
    created  = data.get("created_at") or datetime.now(JST).strftime("%Y-%m-%d")
    if not all([guild_id, user_id, nickname]):
        return jsonify({"ok": False, "error": "missing fields"})
    if len(nickname) > 16:
        return jsonify({"ok": False, "error": "nickname too long"})
    # ★ ログインにパスワードを必須化。新規登録時に必ず設定させる。
    if len(password) < 4:
        return jsonify({"ok": False, "error": "password must be at least 4 characters"})
    err = reject_if_bug_chars({"ニックネーム": nickname})
    if err:
        return err
    try:
        guild_id = int(guild_id)
        users = load_users(guild_id)
        if any(u["id"] == user_id for u in users):
            return jsonify({"ok": False, "error": "already_exists"})
        old_users_text = _users_text(users)  # ★ 追加前に控えておく（password関連は含まない）
        # ★ パスワードは平文で保存せず、ソルト付きハッシュのみ保存する
        #   （users_{guild_id}.json はサーバーのローカルディスクに保存されるが、
        #     万一ファイルが閲覧されても影響を抑えるため）
        pw_hash, pw_salt = hash_password(password)
        users.append({
            "id":            user_id,
            "nickname":      nickname,
            "created_at":    created,
            "password_hash": pw_hash,
            "password_salt": pw_salt,
        })
        save_users(guild_id, users)
        change = file_diff(f"users_{guild_id}.json", old_users_text, _users_text(users))
        log_event("user", "新しいユーザーが登録されました。", actor=nickname, detail=[change] if change else None)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/login", methods=["POST"])
def login():
    """
    body: { guild_id, id, password }
    成功時: { ok: true, session_token, student: {id, nickname} }
    ★ ここで発行する session_token が「本人確認の唯一の証明」になる。
      以後、勉強ログ追加や課題達成などポイントに関わる操作は、この
      トークンから student_id を特定する（クライアントが送ってくる
      student_id は信用しない）。
    """
    if rate_limited("login"):  # ★ 追加：パスワード総当たり対策
        return rate_limit_response()
    data       = request.json or {}
    guild_id   = data.get("guild_id")
    student_id = (data.get("id") or "").strip().upper()
    password   = data.get("password") or ""
    if not guild_id or not student_id or not password:
        return jsonify({"ok": False, "error": "missing fields"})
    try:
        guild_id = int(guild_id)
        user = find_user(guild_id, student_id)
        if not user:
            return jsonify({"ok": False, "error": "user_not_found"})
        if not user.get("password_hash"):
            # ★ このパスワード必須化より前に作られた既存アカウント。
            #   まだパスワードが設定されていないので、初回設定フローに誘導する。
            return jsonify({"ok": False, "error": "password_not_set"})
        if not verify_password(password, user.get("password_salt"), user.get("password_hash")):
            return jsonify({"ok": False, "error": "wrong_password"})
        token = create_session(guild_id, student_id)
        return jsonify({
            "ok": True,
            "session_token": token,
            "student": {"id": user["id"], "nickname": user["nickname"]},
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/set_password", methods=["POST"])
def set_password():
    """
    既存ユーザー（パスワード必須化より前に登録され、まだパスワードが
    設定されていない生徒）が、初めてパスワードを設定するための専用エンドポイント。
    ★ 既にパスワードが設定済みのアカウントには使えない
      （他人のIDを知っているだけで勝手にパスワードを上書き＝乗っ取り
        されるのを防ぐため。パスワードの変更は /change_password を使う）。
    """
    data       = request.json or {}
    guild_id   = data.get("guild_id")
    student_id = (data.get("id") or "").strip().upper()
    password   = data.get("password") or ""
    if not guild_id or not student_id:
        return jsonify({"ok": False, "error": "missing fields"})
    if len(password) < 4:
        return jsonify({"ok": False, "error": "password must be at least 4 characters"})
    try:
        guild_id = int(guild_id)
        users = load_users(guild_id)
        target = next((u for u in users if u.get("id") == student_id), None)
        if not target:
            return jsonify({"ok": False, "error": "user_not_found"})
        if target.get("password_hash"):
            return jsonify({"ok": False, "error": "already_set"})
        pw_hash, pw_salt = hash_password(password)
        target["password_hash"] = pw_hash
        target["password_salt"] = pw_salt
        save_users(guild_id, users)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/change_password", methods=["POST"])
def change_password():
    """
    ログイン済み（session_tokenを持っている）本人が、現在のパスワードを
    確認した上で新しいパスワードに変更する。
    """
    if rate_limited("change_password"):  # ★ 追加：現在のパスワードの総当たり対策
        return rate_limit_response()
    data         = request.json or {}
    guild_id     = data.get("guild_id")
    token        = data.get("session_token")
    old_password = data.get("old_password") or ""
    new_password = data.get("new_password") or ""
    if not guild_id or len(new_password) < 4:
        return jsonify({"ok": False, "error": "invalid_input"})
    try:
        guild_id = int(guild_id)
        student_id = resolve_session(token, guild_id)
        if not student_id:
            return jsonify({"ok": False, "error": "not_logged_in"})
        users = load_users(guild_id)
        target = next((u for u in users if u.get("id") == student_id), None)
        if not target:
            return jsonify({"ok": False, "error": "user_not_found"})
        if not verify_password(old_password, target.get("password_salt"), target.get("password_hash")):
            return jsonify({"ok": False, "error": "wrong_password"})
        pw_hash, pw_salt = hash_password(new_password)
        target["password_hash"] = pw_hash
        target["password_salt"] = pw_salt
        save_users(guild_id, users)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/generate_link_code", methods=["POST"])
def generate_link_code():
    """
    body: { guild_id, session_token }
    成功時: { ok: true, code, expires_in_sec }
    ★ StudyLogにログイン済み（session_token検証済み）の本人だけが、
      Discord連携用のワンタイムコードを発行できる。このコードを
      Discord上で `/id連携 <code>` に入力すると連携が完了する。
      生徒IDだけを知っている第三者はこのAPIを呼べない（session_tokenが
      無いため）ので、なりすまし連携はできない。
    """
    data     = request.json or {}
    guild_id = data.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing fields"})

    guild_id   = int(guild_id)
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    try:
        result = issue_link_code(guild_id, student_id)
        return jsonify({"ok": True, **result})
    except ValueError as e:
        msg = str(e)
        if msg.startswith("too_soon:"):
            return jsonify({"ok": False, "error": "too_soon", "retry_after_sec": int(msg.split(":", 1)[1])})
        return jsonify({"ok": False, "error": msg})


@app.route("/discord_login_start", methods=["GET"])
def discord_login_start():
    """
    ブラウザが直接GETするエンドポイント（ログイン前・session_token不要）。
    Discordの認可画面へリダイレクトする。
    ★ 学籍番号+パスワードでのログインとは完全に別経路。ここで発行するstateは
      「まだ誰でもない」ものに紐づく（CSRF対策のみが目的で、student_idは
      Discordの認可が終わって初めて分かる）。
    """
    if not DISCORD_CLIENT_SECRET:
        return _oauth_result_page(False, "現在Discordログインは準備中です。学籍番号でログインしてください。")

    guild_id = request.args.get("guild_id")
    if not guild_id:
        return _oauth_result_page(False, "不正なリクエストです。")

    guild_id = int(guild_id)
    state = issue_oauth_state(guild_id, None, purpose="login")
    authorize_url = "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "redirect_uri":  DISCORD_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
        "prompt":        "consent",
    })
    return redirect(authorize_url)


@app.route("/discord_oauth_start", methods=["POST"])
def discord_oauth_start():
    """
    body: { guild_id, session_token }
    成功時: { ok: true, authorize_url }
    ★ StudyLogにログイン済み（session_token検証済み）の本人だけが呼べる。
      返ってきた authorize_url にブラウザを移動させると、Discordの認可画面が
      表示され、許可すると /discord_oauth_callback に戻ってくる。
    """
    if not DISCORD_CLIENT_SECRET:
        return jsonify({"ok": False, "error": "oauth_not_configured"})

    data     = request.json or {}
    guild_id = data.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing fields"})

    guild_id   = int(guild_id)
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    state = issue_oauth_state(guild_id, student_id, purpose="link")
    authorize_url = "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "redirect_uri":  DISCORD_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
        "prompt":        "consent",
    })
    return jsonify({"ok": True, "authorize_url": authorize_url})


def _oauth_result_page(success: bool, message: str) -> str:
    """OAuthコールバック後にブラウザへ表示する簡易HTML（StudyLogへ自動で戻る）"""
    color = "#16a34a" if success else "#dc2626"
    icon  = "✓" if success else "✕"
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Discord連携</title></head>
<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;
             min-height:100vh;margin:0;background:#f1f5f9;">
  <div style="background:#fff;border-radius:16px;padding:32px;max-width:360px;width:90%;
              text-align:center;box-shadow:0 20px 50px rgba(0,0,0,.15);">
    <div style="font-size:40px;color:{color};margin-bottom:12px;">{icon}</div>
    <div style="font-size:15px;color:#334155;line-height:1.6;">{message}</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:16px;">3秒後にStudyLogへ戻ります…</div>
  </div>
  <script>setTimeout(function() {{ location.href = "https://1istudyweb.pages.dev/StudyLog"; }}, 3000);</script>
</body></html>"""


@app.route("/discord_oauth_callback", methods=["GET"])
def discord_oauth_callback():
    """
    Discordの認可画面から戻ってくるエンドポイント（ブラウザが直接GETする）。
    query: code, state（ユーザーが拒否した場合は error が入る）
    stateの purpose によって「連携（link）」「ログイン（login）」の
    どちらの処理をするかを振り分ける。
    """
    if not DISCORD_CLIENT_SECRET:
        return _oauth_result_page(False, "サーバー側でDiscord連携が設定されていません。管理者にお問い合わせください。")

    if request.args.get("error"):
        return _oauth_result_page(False, "連携がキャンセルされました。")

    code  = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return _oauth_result_page(False, "不正なリクエストです。もう一度最初からお試しください。")

    entry = consume_oauth_state(state)
    if not entry:
        return _oauth_result_page(False, "リンクの有効期限が切れました。もう一度最初からお試しください。")

    guild_id = entry["guild_id"]
    purpose  = entry.get("purpose", "link")

    # --- 認可コード → アクセストークン に交換（linkでもloginでも共通） ---
    try:
        token_res = requests.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id":     DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  DISCORD_OAUTH_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        token_res.raise_for_status()
        access_token = token_res.json()["access_token"]
    except Exception:
        return _oauth_result_page(False, "Discordとの認証に失敗しました。時間をおいてもう一度お試しください。")

    # --- 自分自身のDiscordユーザー情報を取得（linkでもloginでも共通） ---
    try:
        me_res = requests.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        me_res.raise_for_status()
        me_json = me_res.json()
        discord_user_id  = int(me_json["id"])
        discord_username = me_json.get("global_name") or me_json.get("username") or ""
    except Exception:
        return _oauth_result_page(False, "Discordのユーザー情報取得に失敗しました。時間をおいてもう一度お試しください。")

    if purpose == "login":
        return _handle_discord_login_callback(guild_id, discord_user_id, discord_username)
    else:
        return _handle_discord_link_callback(guild_id, entry["student_id"], discord_user_id)


def _handle_discord_link_callback(guild_id: int, student_id: str, discord_user_id: int) -> str:
    """既にログイン中の本人が、追加でDiscordを連携する（従来のフロー）。"""
    users = load_users(guild_id)
    matched = next((u for u in users if u["id"] == student_id), None)
    if not matched:
        return _oauth_result_page(False, "生徒データが見つかりませんでした。お手数ですがもう一度お試しください。")
    nickname = matched.get("nickname", student_id)

    try:
        links = load_discord_links(guild_id)
        links[student_id] = discord_user_id
        save_discord_links(guild_id, links)
    except DataWriteError:
        return _oauth_result_page(False, "連携の保存に失敗しました（サーバーエラー）。もう一度お試しください。")

    try:
        send_discord_dm(guild_id, student_id, "StudyLog", f"{student_id}の{nickname}さんの通知登録が完了しました。")
    except Exception:
        pass

    return _oauth_result_page(True, "Discordとの連携が完了しました！")


def _handle_discord_login_callback(guild_id: int, discord_user_id: int, discord_username: str) -> str:
    """
    Discordそのものでログインしようとしている（まだ未ログイン）。
    ★「全員に登録し直してもらう」方針のため、既存の discord_links
      （/id連携 のコード方式で作られたDM通知用の紐付け）はログイン用途では
      一切信用しない。discord_login_links（ログイン専用・別ファイル）に
      登録済みの場合のみ、そのままログインさせる。

    ★ このエンドポイント自体はAPIドメイン（python-bot-1istudy.onrender.com）
      で動いているため、ここで直接localStorageにセッションを書き込んでも
      フロントエンド（1istudyweb.pages.dev）からは見えない（ドメインが違うと
      localStorageは共有されない）。そのため、セッション情報はURLの
      クエリパラメータとしてフロントエンドへ渡し、フロントエンド自身の
      JS（Login.js）がそちらのドメイン上でlocalStorageに保存する。
    """
    login_links = load_discord_login_links(guild_id)
    student_id = next((sid for sid, did in login_links.items() if int(did) == discord_user_id), None)

    if student_id:
        user = find_user(guild_id, student_id)
        if user:
            token = create_session(guild_id, student_id)
            nickname = user.get("nickname", student_id)
            qs = urlencode({
                "discord_session_token": token,
                "student_id": student_id,
                "nickname": nickname,
            })
            return redirect(f"https://1istudyweb.pages.dev/Login?{qs}")
        # ユーザーデータが見つからない（削除された等）→ 登録し直しへフォールバック

    # 初回、またはデータ不整合 → 学籍番号入力（登録）ステップへ
    reg_token = issue_discord_reg_token(guild_id, discord_user_id, discord_username)
    return redirect(f"https://1istudyweb.pages.dev/Login?discord_reg={reg_token}")


@app.route("/discord_reg_info", methods=["GET"])
def discord_reg_info():
    """
    query: dtoken
    ★ Login.html側が、URLに付いてきた discord_reg トークンが有効かどうかを
      最初に確認するためのAPI。生徒IDはまだ分からない段階なので、
      参考情報としてDiscordの表示名（ニックネーム欄の初期値に使える）だけを返す。
      トークン自体は消費しない（この後の登録フォーム送信時に使うため）。
    """
    entry = get_discord_reg_token(request.args.get("dtoken"))
    if not entry:
        return jsonify({"ok": False, "error": "reg_token_invalid"})
    return jsonify({"ok": True, "discord_username": entry.get("discord_username", "")})


@app.route("/discord_complete_registration", methods=["POST"])
def discord_complete_registration():
    """
    body: { guild_id, dtoken, student_id, nickname(新規登録時のみ必須) }
    成功時: { ok: true, session_token, student: {id, nickname} }
    ★ dtoken は Discordの認可を実際に済ませていないと手に入らない
      （＝「このDiscordアカウントの持ち主である」ことは既に確認済み）。
      ・既に存在する学籍番号を指定した場合 → パスワード確認なしでそのまま紐付ける
      ・存在しない学籍番号なら、新規生徒として登録する
      成功時のみ dtoken を破棄する（失敗時は同じdtokenで再試行できる）。

      ⚠ 注意：学籍番号を知っているだけで既存アカウントに連携できてしまうため、
        他人の学籍番号を知っている第三者がなりすましてログインできてしまう
        リスクがある（本人確認手段はDiscordの認可のみで、学籍番号の所有権は
        検証していない）。運用上リスクがあると感じた場合は、パスワード確認や
        通知の追加を検討すること。
    """
    data       = request.json or {}
    guild_id   = data.get("guild_id")
    dtoken     = data.get("dtoken")
    student_id = (data.get("student_id") or "").strip().upper()
    nickname   = (data.get("nickname") or "").strip()

    if not guild_id or not dtoken or not student_id:
        return jsonify({"ok": False, "error": "missing fields"})

    guild_id = int(guild_id)
    entry = get_discord_reg_token(dtoken)
    if not entry or entry["guild_id"] != guild_id:
        return jsonify({"ok": False, "error": "reg_token_invalid"})

    discord_user_id = entry["discord_user_id"]

    try:
        users = load_users(guild_id)
        existing = next((u for u in users if u.get("id") == student_id), None)

        if existing:
            # --- 既存の学籍番号 → パスワード確認なしでそのまま紐付ける ---
            final_nickname = existing.get("nickname", student_id)
        else:
            # --- 新しい学籍番号 → 新規生徒として登録 ---
            if not nickname:
                return jsonify({"ok": False, "error": "nickname_required"})
            if len(nickname) > 16:
                return jsonify({"ok": False, "error": "nickname too long"})
            err = reject_if_bug_chars({"ニックネーム": nickname})
            if err:
                return err
            users.append({
                "id":         student_id,
                "nickname":   nickname,
                "created_at": datetime.now(JST).strftime("%Y-%m-%d"),
                # ★ Discordログイン専用アカウント。パスワードは未設定のまま保存する
                #   （後から使いたくなった場合は /set_password で追加設定できる）。
            })
            save_users(guild_id, users)
            final_nickname = nickname

        # --- ログイン用の紐付けを保存 ---
        login_links = load_discord_login_links(guild_id)
        login_links[student_id] = discord_user_id
        save_discord_login_links(guild_id, login_links)

        # --- DM通知用の紐付けも合わせて更新しておく（失敗してもログイン自体は成功扱い） ---
        try:
            dm_links = load_discord_links(guild_id)
            dm_links[student_id] = discord_user_id
            save_discord_links(guild_id, dm_links)
        except DataWriteError:
            pass

        discard_discord_reg_token(dtoken)  # ★ 成功時のみ破棄

        token = create_session(guild_id, student_id)
        return jsonify({
            "ok": True,
            "session_token": token,
            "student": {"id": student_id, "nickname": final_nickname},
        })
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_error: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})



def send_discord_dm(guild_id: int, student_id: str, title: str, message: str):
    """
    指定した生徒（Discord連携済み）に、botからDMを送る共通処理。
    ・連携していない場合は ValueError("not_linked") を送出する。
    ・送信自体に失敗した場合はその例外をそのまま送出する。
    """
    links = load_discord_links(guild_id)
    discord_user_id = links.get(str(student_id).strip().upper())
    if not discord_user_id:
        raise ValueError("not_linked")

    async def _send():
        user = bot.get_user(int(discord_user_id))
        if user is None:
            user = await bot.fetch_user(int(discord_user_id))
        await user.send(f"**{title}**\n{message}")

    future = asyncio.run_coroutine_threadsafe(_send(), bot.loop)
    future.result(timeout=10)


@app.route("/notify_dm", methods=["POST"])
def notify_dm():
    """
    body: { guild_id, title(省略可), message }
    ★ 生徒がDiscord上で /id連携 を済ませていれば、botから本人にDMを送る。
      ブラウザのタブを閉じていても、他のサイトを見ていても、
      Discordアプリ／PC版の通知として届く
      （Discord側の通知設定・DM許可がオフの場合は届かない）。
    """
    data    = request.json or {}
    guild_id = data.get("guild_id")
    title   = data.get("title") or "StudyLog"
    message = data.get("message")

    if not all([guild_id, message]):
        return jsonify({"ok": False, "error": "missing fields"})

    # --- ★ 本人確認：他人の student_id を指定して勝手にDMを送りつけられない
    #     ようにする（session_token から本人の student_id を特定する） ---
    guild_id   = int(guild_id)
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    try:
        send_discord_dm(guild_id, student_id, title, message)
        return jsonify({"ok": True})
    except ValueError:
        # まだ /id連携 していない生徒。呼び出し側（フロント）で
        # ブラウザ通知にフォールバックできるよう、専用のエラーコードを返す
        return jsonify({"ok": False, "error": "not_linked"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"dm_failed: {e}"})


# ================================
#  ★ 削除の作成者確認（カードデッキ／お知らせ）（2026/08/19）
#  ─────────────────────────────
#  背景：カードデッキ（words/*.json）・お知らせ（notices/*）は、作成者本人
#  以外の誰でも削除（お知らせは/delete_notice、デッキは/delete_cards＝
#  Cardmaker.jsの「非公開に戻す」もこのAPIを叩くため両方含む）できてしまい、
#  作成者が知らないうちに自分の作ったものが消えることがあった。
#  ここでは「本人以外は直接削除できないようにし、削除したい場合は理由付きで
#  作成者にDiscord DMを送って承認/拒否してもらう」フローを実装する。
#
#  ・作成者チェック本体は _delete_cards / _delete_notice 側（それぞれ
#    _deck_owner / _notice_owner を使用）に入れてある。ここにあるのは
#    「作成者以外からの削除依頼」を仲介する部分だけ。
#  ・依頼〜承認はDBを持たず、create_session()と同じ「署名付きトークンに
#    必要な情報を全部載せる」方式（ステートレス）にした。このアプリの
#    規模でトークン失効リストのような仕組みまで持つのはオーバーエンジニアと
#    判断したのは、SESSION_SECRETまわりの既存コメントと同じ考え方。
#    そのため：
#      - トークンは14日間で自然に失効する。
#      - 承認/拒否はDMのリンクを開くだけで完了する（本人のDiscordにしか
#        届かない前提で、あえてログインを要求していない）。
#      - 同じリンクを2回押す（例：承認後にもう一度承認）といった操作は
#        「その時点の実ファイルに対してもう一度実行する」だけなので、
#        既に消えていれば「ファイルが見つかりません」を返して実害はない
#        （＝厳密な二重実行防止は持たない）。
#  ・作成者がDiscord未連携（/id連携未実施）の場合は依頼を送れない
#    （＝安全側に倒して削除をブロックしたままにする。連携すれば解決する）。
#  ・作成者が記録されていない古いデッキ／お知らせ（この機能の導入前に
#    公開されたもの）は、従来通り誰でも直接削除できる（_deck_owner /
#    _notice_owner が (None, None) を返すため）。
# ================================
DELETE_APPROVAL_URL = "https://1istudyweb.pages.dev/DeleteApproval.html"
DELETE_REQUEST_TOKEN_TTL_SEC = 60 * 60 * 24 * 14  # 14日間有効

def create_delete_request_token(payload: dict) -> str:
    body = dict(payload)
    body["_t"] = int(time.time())
    payload_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()
    sig = hmac.new(SESSION_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"

def resolve_delete_request_token(token: str):
    """有効な署名付きトークンならペイロード(dict)を返す。無効・期限切れ・
    改ざんなら None。"""
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(SESSION_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
        if time.time() - payload.get("_t", 0) > DELETE_REQUEST_TOKEN_TTL_SEC:
            return None
        return payload
    except Exception:
        return None

def load_pending_delete_requests(guild_id):
    """Discord未連携のためDMを送れなかった削除依頼の控え（Web確認待ち）。
    guildごとに1ファイル（discord_links_{guild_id}.json等と同じ考え方）。"""
    data, sha = local_get(f"pending_delete_requests_{guild_id}.json")
    return (data or []), sha

def save_pending_delete_requests(guild_id, items, sha=None):
    if sha is None:
        _, sha = local_get(f"pending_delete_requests_{guild_id}.json")
    local_put(f"pending_delete_requests_{guild_id}.json", items, sha)

def _delete_target_summary(category, filename):
    """「今まさに削除されようとしている中身」を人が読める形にする。
    (表示名, 詳細行のリスト) を返す。読めない場合は (filename, [])。
    ★ 承認ページはリンクを開いた時点の最新の中身を都度取得して表示する
    （依頼を送った時点のスナップショットではない＝実際に消える中身と一致する）。"""
    if category == "deck":
        data, _ = get_card_file(filename)
        if not data:
            return filename, []
        name = data.get("name") or filename
        cards = data.get("cards") or []
        lines = [
            f"科目: {data.get('subject') or '（なし）'}",
            f"問題数: {len(cards)}問",
            "状態: " + ("未完成（作成中）" if data.get("incomplete") else "完成"),
        ]
        preview = [f"・{(c.get('question') or '').strip()[:60]}" for c in cards[:5] if (c.get("question") or "").strip()]
        if preview:
            lines.append("--- 内容（先頭" + str(len(preview)) + "問の問題文） ---")
            lines.extend(preview)
        return name, lines
    elif category == "notice":
        path = _data_path(f"{NOTICES_DIR}/{filename}")
        content = None
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                content = None
        return filename, [content if content is not None else "（内容を読み込めませんでした）"]
    return filename, []

@app.route("/request_delete", methods=["POST"])
def request_delete():
    """
    作成者本人以外がデッキ／お知らせを削除しようとしたときの入口。
    実際には削除せず、理由を添えて作成者にDiscord DMで確認を送るだけ。
    body: { guild_id, session_token, category: "deck"/"notice", filename, reason }
    """
    data = request.json or {}
    guild_id, requester_id, requester_nickname, err = require_login_json(data)
    if err:
        return err
    category = data.get("category")
    filename = data.get("filename")
    reason = (data.get("reason") or "").strip()
    if category not in ("deck", "notice"):
        return jsonify({"ok": False, "error": "invalid category"})
    if not filename:
        return jsonify({"ok": False, "error": "filename は必須です"})
    if not reason:
        return jsonify({"ok": False, "error": "理由を入力してください"})
    if len(reason) > 500:
        return jsonify({"ok": False, "error": "理由は500文字以内で入力してください"})
    err = reject_if_bug_chars({"削除理由": reason})
    if err:
        return err

    if category == "deck":
        if "/" in filename or "\\" in filename or ".." in filename:
            return jsonify({"ok": False, "error": "invalid filename"})
        owner_id, owner_nickname = _deck_owner(filename)
    else:
        if not _is_safe_notice_filename(filename):
            return jsonify({"ok": False, "error": "invalid filename"})
        owner_id, owner_nickname = _notice_owner(filename)

    if not owner_id:
        return jsonify({"ok": False, "error": "作成者が記録されていないため、確認を送れません。"})
    if str(owner_id) == str(requester_id):
        return jsonify({"ok": False, "error": "本人はこの手続きを使わず直接削除できます。"})

    target_name, _lines = _delete_target_summary(category, filename)

    token = create_delete_request_token({
        "guild_id": guild_id,
        "category": category,
        "filename": filename,
        "owner_id": owner_id,
        "owner_nickname": owner_nickname,
        "requester_id": requester_id,
        "requester_nickname": requester_nickname,
        "reason": reason,
    })
    review_url = f"{DELETE_APPROVAL_URL}?token={token}"
    category_label = "カードデッキ" if category == "deck" else "お知らせ"
    message = (
        f"{requester_nickname}さんが、あなたが作成した{category_label}\n"
        f"「{target_name}」の削除を依頼しています。\n\n"
        f"理由: {reason}\n\n"
        f"内容を確認してから、承認／拒否を選んでください。\n"
        f"{review_url}"
    )
    notified_via = "discord_dm"
    try:
        send_discord_dm(guild_id, owner_id, "🗑 削除の確認依頼", message)
    except ValueError:
        # ★ 作成者がDiscord未連携（/id連携未実施）でDMを送れないケースの
        #   受け皿。ここで諦めて削除依頼自体を失敗にすると、作成者に確認する
        #   手段が無いまま永久に削除できなくなってしまう。代わりにサーバー側
        #   （pending_delete_requests_{guild_id}.json）に依頼を控えておき、
        #   作成者が次にWebサイトのいずれかのページを開いたとき
        #   （PendingDeleteCheck.js）に確認モーダルを出す形でフォールバックする。
        try:
            items, sha = load_pending_delete_requests(guild_id)
            items.append({
                "token": token,
                "category": category,
                "target_name": target_name,
                "owner_id": owner_id,
                "requester_nickname": requester_nickname,
                "reason": reason,
                "created_at": int(time.time()),
            })
            save_pending_delete_requests(guild_id, items, sha)
        except DataWriteError as e:
            return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
        notified_via = "web_pending"
    except Exception as e:
        return jsonify({"ok": False, "error": f"dm_failed: {e}"})

    log_event(
        "card" if category == "deck" else "notice",
        f"「{target_name}」の削除を{requester_nickname}さんが{owner_nickname or '作成者'}さんに依頼しました（承認待ち）。",
        actor=requester_nickname,
        detail=[{"file": None, "diff": f"理由: {reason}"}],
    )
    return jsonify({"ok": True, "owner_nickname": owner_nickname, "notified_via": notified_via})

@app.route("/pending_delete_requests", methods=["GET"])
def pending_delete_requests():
    """ログイン中の本人宛の、Web確認待ちの削除依頼一覧を返す
    （/request_deleteがDiscord未連携でDMを送れなかったケースの受け皿）。
    PendingDeleteCheck.jsがサイトを開くたびに呼び、あれば確認モーダルを出す。"""
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    try:
        guild_id = int(guild_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid guild_id"})
    student_id = resolve_session(request.args.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    items, _ = load_pending_delete_requests(guild_id)
    now = time.time()
    mine = [
        it for it in items
        if str(it.get("owner_id")) == str(student_id)
        and now - it.get("created_at", 0) <= DELETE_REQUEST_TOKEN_TTL_SEC  # ★ トークンと同じ期限で表示から外す
    ]
    return jsonify({"ok": True, "requests": [
        {
            "token": it.get("token"),
            "category": it.get("category"),
            "target_name": it.get("target_name"),
            "requester_nickname": it.get("requester_nickname"),
            "reason": it.get("reason"),
        }
        for it in mine
    ]})

@app.route("/delete_request_info", methods=["GET"])
def delete_request_info():
    """削除承認ページ（DeleteApproval.html）が、リンクのtokenから
    「何を消そうとしているか」を表示するための情報を取得する。
    トークンさえ分かれば閲覧できる（DMで本人にだけ届く前提）ため、
    ログイン不要にしてある。"""
    token = request.args.get("token", "")
    payload = resolve_delete_request_token(token)
    if not payload:
        return jsonify({"ok": False, "error": "リンクが無効か、期限切れです。"})
    category = payload.get("category")
    filename = payload.get("filename")
    target_name, detail_lines = _delete_target_summary(category, filename)
    exists = (
        os.path.isfile(_data_path(f"{CARDS_DIR}/{filename}")) if category == "deck"
        else os.path.isfile(_data_path(f"{NOTICES_DIR}/{filename}"))
    )
    return jsonify({
        "ok": True,
        "category": category,
        "target_name": target_name,
        "detail_lines": detail_lines,
        "requester_nickname": payload.get("requester_nickname"),
        "owner_nickname": payload.get("owner_nickname"),
        "reason": payload.get("reason"),
        "requested_at": payload.get("_t"),
        "already_gone": not exists,  # 既に削除済み・取り下げ済みなど
    })

@app.route("/respond_delete_request", methods=["POST"])
def respond_delete_request():
    """削除承認ページからの承諾／拒否。ログイン不要（DMで本人にだけ届いた
    tokenの所持自体を本人確認の代わりにしている）。
    body: { token, action: "approve"/"reject" }"""
    data = request.json or {}
    payload = resolve_delete_request_token(data.get("token", ""))
    if not payload:
        return jsonify({"ok": False, "error": "リンクが無効か、期限切れです。"})
    action = data.get("action")
    if action not in ("approve", "reject"):
        return jsonify({"ok": False, "error": "invalid action"})

    category = payload.get("category")
    filename = payload.get("filename")
    owner_nickname = payload.get("owner_nickname") or "作成者"
    requester_id = payload.get("requester_id")
    requester_nickname = payload.get("requester_nickname") or "依頼者"
    guild_id = payload.get("guild_id")
    target_name, _lines = _delete_target_summary(category, filename)

    if action == "reject":
        log_event(
            "card" if category == "deck" else "notice",
            f"「{target_name}」の削除依頼を{owner_nickname}さんが却下しました。",
            actor=owner_nickname,
        )
        result = jsonify({"ok": True, "action": "reject"})
    else:
        note = f"（{requester_nickname}さんの削除依頼を{owner_nickname}さんが承認）"
        if category == "deck":
            result = _delete_card_deck_file(filename, owner_nickname, approval_note=note)
        else:
            result = _delete_notice_file(filename, owner_nickname, approval_note=note)

    # ★ pending_delete_requests（Discord未連携でWeb確認待ちになっていた控え）に
    #   このtokenのエントリが残っていれば取り除く。承認・拒否どちらの経路で
    #   応答されても、次にサイトを開いたときにもう出てこないようにするため
    #   （DM経由で応答された場合も、Web確認待ちに二重登録されていることは
    #   無いはずだが、念のため同じ処理でまとめて掃除する）。
    if guild_id:
        try:
            items, sha = load_pending_delete_requests(guild_id)
            new_items = [it for it in items if it.get("token") != data.get("token")]
            if len(new_items) != len(items):
                save_pending_delete_requests(guild_id, new_items, sha)
        except Exception:
            pass

    # ★ 依頼した本人にも結果を伝える（ベストエフォート：Discord未連携／
    #   送信失敗でも承認・拒否そのものは成立させたいので例外は握りつぶす）。
    if guild_id and requester_id:
        try:
            outcome = "承認され、削除されました" if action == "approve" else "却下されました"
            send_discord_dm(
                int(guild_id), requester_id, "🗑 削除依頼の結果",
                f"「{target_name}」の削除依頼は{owner_nickname}さんに{outcome}。",
            )
        except Exception:
            pass

    return result


# ================================
#  ★ アカウント設定（ニックネーム変更・パスワード変更）
#  ─────────────────────────────
#  ・ニックネーム変更はログイン済み（session_token）であれば即座に可能。
#  ・パスワード変更は、それより一段重要な操作なので、
#    「今この端末を触っている人が、本当にそのアカウントの持ち主の
#      Discordも操作できるか」を追加で確認する。
#    6桁の確認コードをDiscord DMで送り、それを入力させてから
#    初めて変更を反映する（Discord連携＝/id連携 が済んでいる生徒のみ使える）。
#  ・コードは短時間（10分）だけ有効なメモリ上の値。プロセス再起動で
#    消えても「もう一度コードを送ってもらう」だけで済むため、
#    ログインセッションと違って永続化の必要はないと判断した。
# ================================
PASSWORD_CHANGE_CODES = {}      # f"{guild_id}:{student_id}" -> {"code","expires","requested_at"}
PASSWORD_CHANGE_CODE_TTL_SEC = 10 * 60   # コードの有効期限：10分
PASSWORD_CHANGE_CODE_COOLDOWN_SEC = 60   # 連続でコードを要求できないようにする（DM連打防止）

def _pw_code_key(guild_id: int, student_id: str) -> str:
    return f"{guild_id}:{student_id}"

@app.route("/change_nickname", methods=["POST"])
def change_nickname():
    data     = request.json or {}
    guild_id = data.get("guild_id")
    nickname = (data.get("nickname") or "").strip()
    if not guild_id or not nickname:
        return jsonify({"ok": False, "error": "missing fields"})
    if len(nickname) > 16:
        return jsonify({"ok": False, "error": "nickname too long"})
    err = reject_if_bug_chars({"ニックネーム": nickname})
    if err:
        return err

    guild_id   = int(guild_id)
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    try:
        users  = load_users(guild_id)
        target = next((u for u in users if u.get("id") == student_id), None)
        if not target:
            return jsonify({"ok": False, "error": "user_not_found"})
        target["nickname"] = nickname
        save_users(guild_id, users)
        return jsonify({"ok": True, "nickname": nickname})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

@app.route("/request_password_change_code", methods=["POST"])
def request_password_change_code():
    """ログイン済み本人が、パスワード変更用の確認コードをDiscord DMで受け取る。"""
    if rate_limited("request_password_change_code"):  # ★ 追加：DM連打対策（本人ごとのクールダウンとは別に、IP単位でも制限）
        return rate_limit_response()
    data     = request.json or {}
    guild_id = data.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing fields"})

    guild_id   = int(guild_id)
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    key = _pw_code_key(guild_id, student_id)
    now = time.time()
    existing = PASSWORD_CHANGE_CODES.get(key)
    if existing and (now - existing["requested_at"] < PASSWORD_CHANGE_CODE_COOLDOWN_SEC):
        remain = int(PASSWORD_CHANGE_CODE_COOLDOWN_SEC - (now - existing["requested_at"])) + 1
        return jsonify({"ok": False, "error": "too_soon", "retry_after_sec": remain})

    code = f"{secrets.randbelow(1_000_000):06d}"
    PASSWORD_CHANGE_CODES[key] = {
        "code": code,
        "expires": now + PASSWORD_CHANGE_CODE_TTL_SEC,
        "requested_at": now,
    }

    try:
        send_discord_dm(
            guild_id, student_id,
            "パスワード変更の確認コード",
            f"確認コード: {code}\n（10分間有効です。心当たりが無ければこのメッセージは無視してください）",
        )
    except ValueError:
        del PASSWORD_CHANGE_CODES[key]
        return jsonify({"ok": False, "error": "not_linked"})
    except Exception as e:
        del PASSWORD_CHANGE_CODES[key]
        return jsonify({"ok": False, "error": f"dm_failed: {e}"})

    return jsonify({"ok": True})

@app.route("/confirm_password_change", methods=["POST"])
def confirm_password_change():
    """確認コード＋新しいパスワードを受け取り、一致していればパスワードを更新する。"""
    if rate_limited("confirm_password_change"):  # ★ 追加：6桁コード（100万通り）の総当たり対策
        return rate_limit_response()
    data         = request.json or {}
    guild_id     = data.get("guild_id")
    code         = (data.get("code") or "").strip()
    new_password = data.get("new_password") or ""
    if not guild_id or not code:
        return jsonify({"ok": False, "error": "missing fields"})
    if len(new_password) < 4:
        return jsonify({"ok": False, "error": "password must be at least 4 characters"})

    guild_id   = int(guild_id)
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    key   = _pw_code_key(guild_id, student_id)
    entry = PASSWORD_CHANGE_CODES.get(key)
    if not entry:
        return jsonify({"ok": False, "error": "code_not_requested"})
    if time.time() > entry["expires"]:
        del PASSWORD_CHANGE_CODES[key]
        return jsonify({"ok": False, "error": "code_expired"})
    if not hmac.compare_digest(code, entry["code"]):
        return jsonify({"ok": False, "error": "wrong_code"})

    try:
        users  = load_users(guild_id)
        target = next((u for u in users if u.get("id") == student_id), None)
        if not target:
            return jsonify({"ok": False, "error": "user_not_found"})
        pw_hash, pw_salt = hash_password(new_password)
        target["password_hash"] = pw_hash
        target["password_salt"] = pw_salt
        save_users(guild_id, users)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    del PASSWORD_CHANGE_CODES[key]  # ★ 使い切ったコードは即座に無効化（使い回し防止）
    return jsonify({"ok": True})

# ================================
#  ★ パスワードの再設定（パスワードを忘れた場合／未ログイン）
#  ─────────────────────────────
#  上の request_password_change_code / confirm_password_change は
#  「ログイン済み本人」が使う想定（session_tokenで本人確認）。
#  こちらはログインする手段（＝パスワード）自体を忘れた人向けなので、
#  session_token を要求できない。代わりに学籍番号(id)を渡してもらい、
#  同じくDiscord DMの確認コードで本人確認する（/id連携が必須）。
#  確認コードの保存先・有効期限・連打防止クールダウンは change 用と共有する
#  （どちらの経路でも「そのstudent_id宛にDMを送った」という事実は同じであり、
#    未使用のコードを両エンドポイントのどちらからでも消費できて問題ないため）。
# ================================
@app.route("/request_password_reset_code", methods=["POST"])
def request_password_reset_code():
    """
    body: { guild_id, id }
    未ログイン状態で、学籍番号だけを頼りに確認コードをDiscord DMで受け取る。
    """
    if rate_limited("request_password_reset_code"):  # ★ 追加：DM連打・学籍番号総当たり対策
        return rate_limit_response()
    data       = request.json or {}
    guild_id   = data.get("guild_id")
    student_id = (data.get("id") or "").strip().upper()
    if not guild_id or not student_id:
        return jsonify({"ok": False, "error": "missing fields"})

    guild_id = int(guild_id)
    user = find_user(guild_id, student_id)
    if not user:
        return jsonify({"ok": False, "error": "user_not_found"})

    key = _pw_code_key(guild_id, student_id)
    now = time.time()
    existing = PASSWORD_CHANGE_CODES.get(key)
    if existing and (now - existing["requested_at"] < PASSWORD_CHANGE_CODE_COOLDOWN_SEC):
        remain = int(PASSWORD_CHANGE_CODE_COOLDOWN_SEC - (now - existing["requested_at"])) + 1
        return jsonify({"ok": False, "error": "too_soon", "retry_after_sec": remain})

    code = f"{secrets.randbelow(1_000_000):06d}"
    PASSWORD_CHANGE_CODES[key] = {
        "code": code,
        "expires": now + PASSWORD_CHANGE_CODE_TTL_SEC,
        "requested_at": now,
    }

    try:
        send_discord_dm(
            guild_id, student_id,
            "パスワード再設定の確認コード",
            f"確認コード: {code}\n（10分間有効です。心当たりが無ければこのメッセージは無視してください）",
        )
    except ValueError:
        del PASSWORD_CHANGE_CODES[key]
        return jsonify({"ok": False, "error": "not_linked"})
    except Exception as e:
        del PASSWORD_CHANGE_CODES[key]
        return jsonify({"ok": False, "error": f"dm_failed: {e}"})

    return jsonify({"ok": True})

@app.route("/confirm_password_reset", methods=["POST"])
def confirm_password_reset():
    """
    body: { guild_id, id, code, new_password }
    確認コード＋新しいパスワードを受け取り、一致していればパスワードを更新する。
    ★ session_token は使わない（そもそも持っていないから困っている）ので、
      本人確認はこの確認コードだけが担う。
    """
    # ★ 追加：6桁コード（100万通り）の総当たり対策。session_tokenによる
    #   本人確認が無い分、ここが最も重要（成功すればアカウント乗っ取りになる）。
    if rate_limited("confirm_password_reset"):
        return rate_limit_response()
    data         = request.json or {}
    guild_id     = data.get("guild_id")
    student_id   = (data.get("id") or "").strip().upper()
    code         = (data.get("code") or "").strip()
    new_password = data.get("new_password") or ""
    if not guild_id or not student_id or not code:
        return jsonify({"ok": False, "error": "missing fields"})
    if len(new_password) < 4:
        return jsonify({"ok": False, "error": "password must be at least 4 characters"})

    guild_id = int(guild_id)

    key   = _pw_code_key(guild_id, student_id)
    entry = PASSWORD_CHANGE_CODES.get(key)
    if not entry:
        return jsonify({"ok": False, "error": "code_not_requested"})
    if time.time() > entry["expires"]:
        del PASSWORD_CHANGE_CODES[key]
        return jsonify({"ok": False, "error": "code_expired"})
    if not hmac.compare_digest(code, entry["code"]):
        return jsonify({"ok": False, "error": "wrong_code"})

    try:
        users  = load_users(guild_id)
        target = next((u for u in users if u.get("id") == student_id), None)
        if not target:
            return jsonify({"ok": False, "error": "user_not_found"})
        pw_hash, pw_salt = hash_password(new_password)
        target["password_hash"] = pw_hash
        target["password_salt"] = pw_salt
        save_users(guild_id, users)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    del PASSWORD_CHANGE_CODES[key]  # ★ 使い切ったコードは即座に無効化（使い回し防止）
    return jsonify({"ok": True})


@app.route("/list_study_logs", methods=["GET"])
def list_study_logs():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    logs = load_study_logs(int(guild_id))
    return jsonify({"ok": True, "logs": logs})

# ================================
#  Flask API — ポイント
# ================================
@app.route("/get_points", methods=["GET"])
def get_points():
    """全ユーザーのポイント合計を返す"""
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    pts = load_points(int(guild_id))
    return jsonify({"ok": True, "points": pts})

# ================================
#  Flask API — 課題達成
# ================================
@app.route("/get_completed_tasks", methods=["GET"])
def get_completed_tasks():
    """
    student_id を指定: そのユーザーの達成済み課題リストを返す（達成日・ポイント・ニックネーム付き）
    student_id を省略: 全ユーザー分を { student_id: [...] } の形でまとめて返す
                        （週間ランキングで全員の課題達成ポイントを集計するために使用）
    """
    guild_id   = request.args.get("guild_id")
    student_id = request.args.get("student_id")  # 省略可
    if not guild_id:
        return jsonify({"ok": False, "error": "missing params"})

    tasks = load_completed_tasks(int(guild_id))

    if student_id:
        raw = tasks.get(student_id, [])
        normalized = [_normalize_task_entry(e) for e in raw]
        return jsonify({"ok": True, "done": normalized})

    # student_id 省略 → 全員分をまとめて返す
    all_normalized = {
        sid: [_normalize_task_entry(e) for e in raw]
        for sid, raw in tasks.items()
    }
    return jsonify({"ok": True, "done": all_normalized})


@app.route("/complete_task", methods=["POST"])
def complete_task():
    data     = request.json or {}
    guild_id = int(data.get("guild_id"))

    # --- ★ 本人確認：session_token から student_id を特定する（なりすまし防止） ---
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"ok": False, "error": "missing fields"})

    # ★ ニックネームもクライアントの自己申告ではなく、サーバー側のユーザーデータから引く
    user     = find_user(guild_id, student_id)
    nickname = user["nickname"] if user else None

    # --- ★ points はクライアントから受け取らず、サーバー側の予定データから引き直す ---
    #     （クライアントが任意の points を送っても無視される）
    points = find_task_points(guild_id, task_id)
    if points is None:
        return jsonify({"ok": False, "error": "task not found"})

    # --- 達成済み課題保存（達成日・ポイント・ニックネーム付き） ---
    done = load_completed_tasks(guild_id)
    if student_id not in done:
        done[student_id] = []

    # 既存エントリを正規化したうえで重複チェック
    normalized = [_normalize_task_entry(e) for e in done[student_id]]
    existing_ids = [e["id"] for e in normalized]

    if task_id not in existing_ids:
        normalized.append({
            "id":       task_id,
            "date":     datetime.now(JST).strftime("%Y-%m-%d"),
            "points":   points,
            "nickname": nickname,  # ★ ニックネームを保存
        })

    done[student_id] = normalized
    try:
        save_completed_tasks(guild_id, done)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    # --- ポイント加算 ---
    pts = load_points(guild_id)
    pts[student_id] = pts.get(student_id, 0) + points
    try:
        save_points(guild_id, pts)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    # ★ 2026/08/19：課題の達成状況はStudyLog.js上「自分のみ」にしか表示され
    #   ない個人的な記録（他の生徒には見えない）なので、運用ログには残さない
    #   （運用ログはログインなしでも閲覧できる＝実質公開の場のため）。
    return jsonify({"ok": True, "total": pts[student_id]})


@app.route("/uncomplete_task", methods=["POST"])
def uncomplete_task():
    """
    /complete_task の逆操作。
    指定 student_id の達成済みリストから task_id を取り除き、
    そのタスクに付与されていたポイント分を累計ポイントから減算する。
    （ポイントが0未満にならないようガードする）
    """
    data     = request.json or {}
    guild_id = int(data.get("guild_id"))

    # --- ★ 本人確認：session_token から student_id を特定する（なりすまし防止） ---
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"ok": False, "error": "missing fields"})

    done = load_completed_tasks(guild_id)
    if student_id not in done:
        return jsonify({"ok": False, "error": "not completed"})

    normalized = [_normalize_task_entry(e) for e in done[student_id]]
    target = next((e for e in normalized if e["id"] == task_id), None)
    if target is None:
        return jsonify({"ok": False, "error": "task not found in completed list"})

    normalized = [e for e in normalized if e["id"] != task_id]
    done[student_id] = normalized
    try:
        save_completed_tasks(guild_id, done)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    # --- ポイント減算（0未満にはしない） ---
    removed_points = target.get("points") or 0
    pts = load_points(guild_id)
    pts[student_id] = max(0, pts.get(student_id, 0) - removed_points)
    try:
        save_points(guild_id, pts)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    # ★ 2026/08/19：課題の達成状況は「自分のみ」にしか表示されない個人的な
    #   記録なので、運用ログには残さない（complete_task側と同じ理由）。
    return jsonify({"ok": True, "total": pts[student_id]})

# ================================
#  Flask API — 単語カード
# ================================
CARDS_DIR = "words"
CARDS_INDEX_FILE = "cards_index.json"
# ★ 追加：CardMakerのフロントエンドURL。Discord通知に「該当デッキへ飛ぶリンク」を
#   付けるために使う（Cardmaker.js 側で ?deck=<filename> を見て自動で移動する）。
CARDMAKER_URL = "https://1istudyweb.pages.dev/Cardmaker.html"

def list_card_files():
    dir_path = _data_path(CARDS_DIR)
    if not os.path.isdir(dir_path):
        return []
    return [
        {"name": name}
        for name in sorted(os.listdir(dir_path))
        if name.endswith(".json") and os.path.isfile(os.path.join(dir_path, name))
    ]

def get_card_file(filename):
    data, sha = local_get(f"{CARDS_DIR}/{filename}")
    return data, sha

def put_card_file(filename, content_obj, sha=None):
    local_put(f"{CARDS_DIR}/{filename}", content_obj, sha)

# ================================
#  運用ログ用：ファイルの中身を「行単位のテキスト」に変換して差分を取る
#  ─────────────────────────────
#  ★ 追加（2026/08/19）：運用ログの詳細表示を、実際のGitHubのコミット
#    画面（変更されたファイル名 → +/- の行差分）にできるだけ近づけたい
#    という要望から、JSONファイルの中身をそのまま出すのではなく、
#    人が読んで分かる1行テキストに変換してから difflib で行差分を取る
#    方式にした。true/false・null・内部IDのような分かりにくい値は、
#    ここで日本語の言葉に置き換えてから比較する（例：
#    incomplete=true → "未完成（作成中）"、choice_mode=null → "暗記カード"）。
#    こうすることで、「その項目が変わった行だけ」が - と + の2行で
#    浮かび上がる（変化していない行は表示されない＝diff_cardsの考え方を
#    ファイル全体に拡張したもの）。
# ================================
def _clip_text(s, max_len=150):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= max_len else s[:max_len] + "…"

def _image_count_text(imgs):
    n = len(imgs) if isinstance(imgs, list) else 0
    return f"{n}枚" if n else "(なし)"

def _card_lines(i, c):
    """カード1件分を、GitHubのJSONファイル差分のような { ... } ブロックに
    して返す。★ 2026/08/19、ユーザーから実際のカードJSON（id/question/
    answer/explanation/imgs_q等）を示され、「idは要らない・英語のフィールド名
    （question等）は日本語（問題等）に・imgs_qは『問題の画像』のように
    書き換えて・JSONなら{}も書いて」と指定されたため、idを内部の突き合わせ
    用キーとしてのみ使い（表示しない）、それ以外のフィールドを日本語ラベルで
    _json_block()に渡している。画像本体（base64等）はログに出すには
    大きすぎるため、実データではなく枚数だけを表示する。"""
    fields = [
        ("問題", _clip_text(c.get('question')) or '(空)'),
        ("解答", _clip_text(c.get('answer')) or '(空)'),
        ("解説", _clip_text(c.get('explanation')) or '(空)'),
        ("問題の画像", _image_count_text(c.get('imgs_q'))),
        ("解答の画像", _image_count_text(c.get('imgs_a'))),
        ("解説の画像", _image_count_text(c.get('imgs_e'))),
    ]
    choices = c.get("choices")
    if choices:
        letters = ["A", "B", "C", "D", "E"]
        labeled = [f"{letters[idx]}. {choices[idx]}" if idx < len(letters) else str(choices[idx]) for idx in range(len(choices))]
        fields.append(("選択肢", " / ".join(labeled)))
        correct = c.get("correct_indices") or []
        correct_labels = [letters[idx] for idx in correct if idx < len(letters)]
        fields.append(("正解", ", ".join(correct_labels) if correct_labels else "(未設定)"))
    return _json_block(fields, label=f"カード{i}")

def _card_key(c, idx):
    """カードの突き合わせ用キー。idがあればそれを使う（Cardmaker.js側で
    発行される安定id）。無い場合は位置をキーにする（それでも一致すれば
    「変化なし」として扱われるので実害は小さい）。"""
    if isinstance(c, dict) and c.get("id"):
        return c["id"]
    return f"__pos_{idx}"

def _diff_deck_cards(old_cards, new_cards, max_lines=40):
    """カード配列の差分を、idで突き合わせてから作る。★ 2026/08/19、
    カード全体を1本のテキストにしてから行差分を取る方式だと、デッキの
    途中に1問挿入しただけで、それより後ろの無関係なカードまで「番号が
    ずれた」せいで変更されたように見えてしまう不具合があったため、
    diff_cards()（idベースの突き合わせ）の考え方に戻しつつ、
    _card_lines()によるフィールドごとの表示は維持した。"""
    old_cards = old_cards or []
    new_cards = new_cards or []
    old_by_key = {_card_key(c, i): c for i, c in enumerate(old_cards) if isinstance(c, dict)}
    new_by_key = {_card_key(c, i): c for i, c in enumerate(new_cards) if isinstance(c, dict)}

    lines = []
    for i, (key, c) in enumerate(new_by_key.items(), 1):
        if key not in old_by_key:
            lines.extend(f"+ {l}" for l in _card_lines(i, c))
    for i, (key, c) in enumerate(old_by_key.items(), 1):
        if key not in new_by_key:
            lines.extend(f"- {l}" for l in _card_lines(i, c))
    for i, (key, c) in enumerate(new_by_key.items(), 1):
        old_c = old_by_key.get(key)
        if old_c is None:
            continue
        # ★ 変わっていないカードは番号だけがずれても比較に出さないよう、
        #   新旧どちらも同じ表示番号(i)でラベルして比較する（内容差だけを検知）。
        old_text = "\n".join(_card_lines(i, old_c))
        new_text = "\n".join(_card_lines(i, c))
        if old_text != new_text:
            per_card_diff = _text_diff_lines(old_text, new_text, max_lines=12)
            if per_card_diff:
                lines.extend(per_card_diff.split("\n"))

    if not lines:
        return None
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"…ほか{len(lines) - max_lines}行"]
    return "\n".join(lines)

def _card_folder_name_map():
    """folder_id → フォルダ名 の対応表（ログ表示で内部IDを出さないため）。"""
    try:
        folders, _ = load_card_folders()
        return {f.get("id"): f.get("name") for f in (folders or [])}
    except Exception:
        return {}

def _deck_meta_text(data, folder_names=None):
    """カードデッキのメタ情報（カードそのものは含まない）を、人が読める
    行テキストに変換する。デッキ名・科目・状態・形式・フォルダを1行ずつ
    並べ、difflib で前後を比較すると「変わった行だけ」が浮かび上がる。"""
    if not data:
        return ""
    folder_names = folder_names or {}
    lines = []
    lines.append(f"デッキ名: {data.get('name') or ''}")
    lines.append(f"科目: {data.get('subject') or '(未設定)'}")
    lines.append(f"状態: {'未完成（作成中）' if data.get('incomplete') else '完成'}")
    lines.append(f"形式: {'選択式デッキ' if data.get('choice_mode') else '暗記カード（通常デッキ）'}")
    folder_id = data.get('folder_id')
    lines.append(f"フォルダ: {folder_names.get(folder_id, folder_id) if folder_id else '(フォルダなし)'}")
    return "\n".join(lines)

def deck_file_diff(file, old_data, new_data, folder_names=None):
    """カードデッキ1ファイル分の運用ログ差分エントリを作る（file_diff()と
    同じ {"file","diff","status"} の形）。メタ情報は単純な行差分、カードは
    idベースの突き合わせ（_diff_deck_cards）と使い分けている点が
    file_diff()とは異なる（デッキの途中への挿入・並び替えで無関係な
    カードまでdiffに出ないようにするため）。"""
    folder_names = folder_names if folder_names is not None else _card_folder_name_map()
    meta_diff = _text_diff_lines(_deck_meta_text(old_data, folder_names), _deck_meta_text(new_data, folder_names))
    cards_diff = _diff_deck_cards((old_data or {}).get("cards"), (new_data or {}).get("cards"))
    parts = [p for p in (meta_diff, cards_diff) if p]
    if not parts:
        return None
    if not old_data:
        status = "added"
    elif not new_data:
        status = "deleted"
    else:
        status = "modified"
    return {"file": file, "diff": "\n".join(parts), "status": status}

def _text_diff_lines(old_text, new_text, max_lines=60):
    """2つのテキストを行単位で比較し、GitHubのコミット差分のような
    「+ 追加された行」「- 削除された行」だけを抜き出して返す。
    変化が無ければ None（＝呼び出し側は detail を付けない＝ログには
    何も出さない。分からない/変化していないものを無理に表示しない）。"""
    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    out = [
        l for l in difflib.ndiff(old_lines, new_lines)
        if l.startswith("+ ") or l.startswith("- ")
    ]
    if not out:
        return None
    if len(out) > max_lines:
        out = out[:max_lines] + [f"…ほか{len(out) - max_lines}行"]
    return "\n".join(out)

def generate_card_filename():
    import string
    now   = datetime.now(JST)
    date  = now.strftime("%Y%m%d")
    time_ = now.strftime("%H%M")
    rand  = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"set_{date}_{time_}_{rand}.json"

def _meta_from_card_data(filename, data):
    cards = data.get("cards", [])
    return {
        "filename": filename,
        "name":     data.get("name", filename),
        "count":    len(cards),
        "subject":  data.get("subject"),
        "folder_id": data.get("folder_id"),
        "has_folder_id": "folder_id" in data,
        "published_by": (data.get("published_by") or {}).get("nickname"),
        "incomplete": bool(data.get("incomplete", False)),
        "choice_mode": data.get("choice_mode"),  # ★ null/false=通常デッキ / true=選択式デッキ（単一/複数は問題ごとに決まる。旧形式の"single"/"multi"文字列もtruthyとして扱う）
    }

def _card_index_entry_lines(entry, folder_names=None):
    """cards_index.json の1エントリ（1デッキ分の索引情報）を { ... } の
    ブロックにする。★ 2026/08/19、ユーザーの指定により：filenameは内部の
    識別子なので表示しない、name/count/subject/folder_idは日本語ラベルに
    置き換える、published_by/incompleteも日本語にし、incompleteの
    true/falseは分かりやすい言葉に書き換える。has_folder_idはfolder_idと
    実質同じ情報のため省略。"""
    if entry is None:
        return []
    folder_names = folder_names or {}
    folder_id = entry.get("folder_id")
    fields = [
        ("デッキ名", entry.get("name") or ""),
        ("問題数", f"{entry.get('count', 0)}問"),
        ("科目", entry.get("subject") or "(未設定)"),
        ("フォルダ", folder_names.get(folder_id, folder_id) if folder_id else "(フォルダなし)"),
        ("公開者", entry.get("published_by") or "(不明)"),
        ("状態", "未完成（作成中）" if entry.get("incomplete") else "完成"),
        ("形式", "選択式デッキ" if entry.get("choice_mode") else "暗記カード（通常デッキ）"),
    ]
    return _json_block(fields)

def _card_index_diff(old_entry, new_entry, folder_names=None):
    """索引ファイル内の1エントリの変更を、log_event の detail に渡す
    {"file","diff","status"} の形にする（無ければNone）。upsert/remove
    のたびに呼び、デッキ本体のファイルだけでなく索引ファイルも実際に
    書き換わっていることを運用ログに残す（2026/08/19追加）。"""
    folder_names = folder_names if folder_names is not None else _card_folder_name_map()
    old_text = "\n".join(_card_index_entry_lines(old_entry, folder_names))
    new_text = "\n".join(_card_index_entry_lines(new_entry, folder_names))
    diff = _text_diff_lines(old_text, new_text)
    if not diff:
        return None
    status = "added" if old_entry is None else ("deleted" if new_entry is None else "modified")
    return {"file": CARDS_INDEX_FILE, "diff": diff, "status": status}

def load_cards_index():
    """索引ファイルを取得する。存在しない場合は None を返す（呼び出し側で再構築する）。"""
    data, sha = local_get(CARDS_INDEX_FILE)
    return data, sha

def save_cards_index(index_list, sha=None):
    if sha is None:
        _, sha = local_get(CARDS_INDEX_FILE)
    local_put(CARDS_INDEX_FILE, index_list, sha)
    notify_change()  # ★ list_cards はguildをまたいで共有されるため全体に通知

def rebuild_cards_index():
    """
    索引ファイルが無い（初回・以前のデータ）場合に、wordsフォルダを
    スキャンして索引を作り直す。これは初回だけ発生する重い処理。
    """
    files = list_card_files()
    index = []
    for f in files:
        data, _ = local_get(f"{CARDS_DIR}/{f['name']}")
        if data is None:
            continue
        index.append(_meta_from_card_data(f["name"], data))
    try:
        save_cards_index(index, sha=None)
    except DataWriteError as e:
        print(f"[WARN] cards_index の再構築保存に失敗しました: {e}")
    return index

def upsert_cards_index_entry(filename, data):
    """save_cards のたびに呼び出し、索引ファイル内の該当エントリだけを更新する。
    運用ログ用に、このエントリの変更差分（無ければNone）を返す
    （2026/08/19追加。呼び出し側でdetailの2件目として渡す）。"""
    index, sha = load_cards_index()
    if index is None:
        index = rebuild_cards_index()
        index, sha = load_cards_index()
    meta = _meta_from_card_data(filename, data)
    old_entry = None
    found = False
    for i, entry in enumerate(index):
        if entry.get("filename") == filename:
            old_entry = entry
            index[i] = meta
            found = True
            break
    if not found:
        index.append(meta)
    save_cards_index(index, sha)
    return _card_index_diff(old_entry, meta)

def remove_cards_index_entry(filename):
    """delete_cards のたびに呼び出し、索引ファイルから該当エントリを削除する。
    運用ログ用に、削除されたエントリの差分（無ければNone）を返す
    （2026/08/19追加）。"""
    index, sha = load_cards_index()
    if index is None:
        index = rebuild_cards_index()
        index, sha = load_cards_index()
    removed_entry = next((e for e in index if e.get("filename") == filename), None)
    new_index = [e for e in index if e.get("filename") != filename]
    if len(new_index) != len(index):
        save_cards_index(new_index, sha)
    if removed_entry is None:
        return None
    return _card_index_diff(removed_entry, None)


@app.route("/list_cards", methods=["GET"])
def list_cards():
    try:
        index, _ = load_cards_index()
        if index is None:
            index = rebuild_cards_index()
        return jsonify({"ok": True, "sets": index})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/get_card_set", methods=["GET"])
def get_card_set():
    filename = request.args.get("filename")
    if not filename:
        return jsonify({"ok": False, "error": "filename は必須です"})
    try:
        data, _ = get_card_file(filename)
        if data is None:
            return jsonify({"ok": False, "error": "ファイルが見つかりません"})
        return jsonify({
            "ok": True,
            "filename": filename,
            "name": data.get("name", filename),
            "cards": data.get("cards", []),
            "subject": data.get("subject"),
            "folder_id": data.get("folder_id"),
            "published_by": (data.get("published_by") or {}).get("nickname"),
            # ★ カード本体を開いた際にも未完成フラグを返す
            "incomplete": bool(data.get("incomplete", False)),
            "choice_mode": data.get("choice_mode"),  # ★ null/false=通常デッキ / true=選択式デッキ（単一/複数は問題ごとに決まる。旧形式の"single"/"multi"文字列もtruthyとして扱う）
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/save_cards", methods=["POST"])
def save_cards():
    data     = request.json
    guild_id, _student_id, _nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    name     = data.get("name")
    cards    = data.get("cards")
    filename = data.get("filename")
    subject  = data.get("subject")
    folder_id = data.get("folder_id")
    # ★ 注意：publisher_id/publisher_nicknameはあえてクライアント自己申告のまま
    #   信用している（ここをログインセッションの値で強制的に上書きすると、
    #   「他の人が公開したデッキに自分がカードを追加/編集しても、公開者表示は
    #   元の公開者のまま変わらない」という共同編集の仕様（syncDeckToServer側で
    #   deck.published_byを維持する実装）が壊れてしまうため）。この関数の
    #   呼び出し自体にログインを必須にしたことで「誰もログインしていないのに
    #   保存できる」問題は解消されている。
    publisher_id       = data.get("publisher_id")
    publisher_nickname = data.get("publisher_nickname") or "匿名"
    silent   = data.get("silent", False)  # ★ 追加：trueなら通知しない
    incomplete = bool(data.get("incomplete", False))  # ★ 追加：未完成フラグ（みんなに表示するため保存する）
    # ★ 追加：フロント側（Cardmaker.js）が「これがこのデッキにとって初めての
    #   『公開して保存』かどうか」を明示的に伝えてくるフラグ。
    #   ・「作成中」として announceNewDeckToServer 経由で先にファイルだけ
    #     登録済みのデッキは、実際に公開したタイミングでも filename が
    #     既に存在するため、is_update（＝ファイルの有無）だけで判定すると
    #     「更新されました」という誤った通知文言になってしまう。
    #   ・first_publish が明示的に渡されていれば、通知文言の判定はそちらを優先する。
    first_publish = data.get("first_publish")
    # ★ 追加：選択式デッキかどうか（null=通常のフラッシュカードデッキ / "single" / "multi"）
    choice_mode = data.get("choice_mode")

    if not name or not isinstance(cards, list):
        return jsonify({"ok": False, "error": "name と cards は必須です"})

    # --- ★ 制御文字・不可視文字・壊れた符号位置を弾く（デッキ名・各カードの本文） ---
    check_fields = {"デッキ名": name, "公開者ニックネーム": publisher_nickname}
    for i, c in enumerate(cards):
        if not isinstance(c, dict):
            continue
        check_fields[f"カード{i+1}の問題文"] = c.get("question")
        check_fields[f"カード{i+1}の解答"]   = c.get("answer")
        check_fields[f"カード{i+1}の解説"]   = c.get("explanation")
    err = reject_if_bug_chars(check_fields)
    if err:
        return err

    is_update = bool(filename)
    if not filename:
        filename = generate_card_filename()

    sha = None
    existing_published_by = None
    if is_update:
        existing_data, sha = get_card_file(filename)
        # ★「クイズ過去問」フォルダの中身は、その外へ移動できない
        #   （フォルダ移動UIのガードと同じ考え方。ここが唯一のデッキfolder_id書き込み
        #   経路なので、サーバー側の実効的な強制はここで行う）。
        if existing_data is not None:
            old_folder_id = existing_data.get("folder_id")
            folders_for_check, _ = load_card_folders()
            if _is_in_archive_scope(folders_for_check, old_folder_id) and not _is_in_archive_scope(folders_for_check, folder_id):
                return jsonify({"ok": False, "error": "クイズ過去問フォルダの外には移動できません"})
            existing_published_by = existing_data.get("published_by")

    # ★ 追加：published_by.id は「デッキの作成者」を表す唯一の場所（削除の
    #   作成者確認機能で使う）。nicknameは元からクライアント側（deck.published_by
    #   キャッシュ）が「元の公開者のまま維持する」よう送ってきているが、idは
    #   syncDeckToServerが毎回“今ログインしている編集者自身”のstudent_idを
    #   送ってきてしまっており、他人のデッキを1回編集しただけで作成者IDが
    #   編集者にすり替わってしまっていた。既に published_by.id が記録されている
    #   更新（＝作成中の初回公開より後）では、クライアントの値を無視して
    #   元のidを維持する。初回公開時（記録がまだ無い場合）だけクライアントの
    #   publisher_idをそのまま採用する。
    final_publisher_id = (existing_published_by or {}).get("id") or publisher_id

    card_payload = {
        "name": name,
        "cards": cards,
        "subject": subject,
        "folder_id": folder_id,
        "published_by": {
            "id": final_publisher_id,
            "nickname": publisher_nickname,
        },
        "incomplete": incomplete,  # ★ 未完成フラグを保存（他人の端末にも同じ表示をするため）
        "choice_mode": choice_mode,  # ★ 選択式デッキかどうか（null/false=通常デッキ / true=選択式デッキ）
    }

    try:
        put_card_file(filename, card_payload, sha)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    # ★ 索引ファイルも合わせて更新する（list_cardsを軽く保つため）
    index_change = None
    try:
        index_change = upsert_cards_index_entry(filename, card_payload)
    except DataWriteError as e:
        # カード本体の保存自体は成功しているので、索引更新の失敗は警告に留める。
        # 次回 list_cards アクセス時に再構築されるので実害は小さい。
        print(f"[WARN] cards_index の更新に失敗しました: {e}")

    # --- Discord通知（silentがtrueならスキップ） ---
    if guild_id and not silent:
        try:
            guild_id_int = int(guild_id)
            guild = bot.get_guild(guild_id_int)
            if guild:
                # ★ 修正：is_update（＝ファイルが既に存在するか）だけで「更新」と
                #   判定すると、「作成中」として先に登録されていたデッキを
                #   初めて公開したときも「更新されました」と表示されてしまっていた。
                #   first_publish が明示的に true で渡されてきた場合は、
                #   filenameの有無に関わらず「公開（新規）」として扱う。
                is_actual_update = is_update and not bool(first_publish)
                action = "更新" if is_actual_update else "公開"
                # ★ 追加：通知から直接そのデッキの場所まで飛べるよう、CardMakerへの
                #   リンクに ?deck=<filename> を付与する。Cardmaker.js 側がこのパラメータを
                #   見て、該当デッキのあるフォルダまで自動的に移動しハイライト表示する。
                # ★ 修正：プレーンテキストの「[デッキ名](url)」はDiscordの通常メッセージでは
                #   マスクされたリンクとして描画されない（そのまま文字列として表示されてしまう）ため、
                #   埋め込み（Embed）のdescriptionにマスクリンクとして書く形に変更した。
                #   Embed内であれば [表示テキスト](url) がちゃんとクリック可能なリンクになる。
                deck_url = f"{CARDMAKER_URL}?deck={filename}"
                embed = discord.Embed(
                    title=f"📇 単語カードが{action}されました",
                    description=(
                        f"[{name}]({deck_url})\n"
                        f"{publisher_nickname}さんによって{action}（{len(cards)}問）"
                    ),
                    color=discord.Color.blue(),
                )

                target_channel = get_subject_channel_by_name(guild, subject) if subject else None
                if not target_channel:
                    config = load_config(guild_id_int)
                    channel_id = config.get("notice_channel_id")   #自分で変更
                    target_channel = bot.get_channel(channel_id) if channel_id else None

                if target_channel:
                    asyncio.run_coroutine_threadsafe(
                        target_channel.send(embed=embed), bot.loop
                    ).result(timeout=10)
        except Exception as e:
            print(f"[WARN] save_cards notify failed: {e}")

    is_actual_update = is_update and not bool(first_publish)
    old_deck_for_diff = existing_data if (is_update and existing_data) else None
    change = deck_file_diff(f"{CARDS_DIR}/{filename}", old_deck_for_diff, card_payload)
    detail = [c for c in (change, index_change) if c]  # ★ デッキ本体＋索引ファイルの両方の変更を載せる
    log_event(
        "card",
        f"カードデッキ「{name}」を{'更新' if is_actual_update else '公開'}しました（{len(cards)}問）。",
        actor=publisher_nickname,
        detail=detail if detail else None,
    )
    return jsonify({"ok": True, "filename": filename, "is_update": is_update})

def _deck_owner(filename):
    """デッキの作成者 (owner_id, owner_nickname) を返す。
    読めない／記録が無い（作成者確認機能より前に公開された古いデッキ等）
    場合は (None, None)。owner_id が None のときは「作成者不明」として
    従来通り誰でも削除できる扱いにする（過去のデッキを誰も削除できなくなる
    事態を避けるため）。"""
    data, _ = get_card_file(filename)
    if not data:
        return None, None
    pub = data.get("published_by") or {}
    return pub.get("id"), pub.get("nickname")

def _delete_card_deck_file(filename, actor_nickname, approval_note=None):
    """デッキファイル削除の実処理（本人による直接削除・削除依頼の承認の
    どちらからも呼ばれる共通処理）。作成者チェックは呼び出し側の責務。"""
    path = _data_path(f"{CARDS_DIR}/{filename}")
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "ファイルが見つかりません"})
    # ★ 削除前にデッキの中身を読んでおく（運用ログの表示用）。読めなかった
    #   場合はデッキ名・カードの内容が分からないので、無理に内部ファイル名
    #   （ハッシュ）などを代わりに出さず、それらの表示自体を省く。
    deleted_data, _ = get_card_file(filename)
    deleted_name = (deleted_data or {}).get("name")
    try:
        os.remove(path)
    except OSError as e:
        return jsonify({"ok": False, "error": f"local_delete_failed: {e}"})

    # ★ 索引ファイルからも削除する
    index_change = None
    try:
        index_change = remove_cards_index_entry(filename)
    except DataWriteError as e:
        print(f"[WARN] cards_index からの削除に失敗しました: {e}")

    # ★ 並び順（list_order.json）からも、このデッキのキーを取り除いておく
    cleanup_list_order(remove_keys={f"deck:{filename}"})

    change = deck_file_diff(f"{CARDS_DIR}/{filename}", deleted_data, None)
    detail = [c for c in (change, index_change) if c]
    summary = f"カードデッキ「{deleted_name}」を削除しました。" if deleted_name else "カードデッキを削除しました。"
    if approval_note:
        summary += approval_note
    log_event("card", summary, actor=actor_nickname, detail=detail if detail else None)
    return jsonify({"ok": True})

@app.route("/delete_cards", methods=["POST"])
def delete_cards():
    data     = request.json
    _guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    filename = data.get("filename")
    if not filename:
        return jsonify({"ok": False, "error": "filename は必須です"})
    # ★ パストラバーサル対策：filename はクライアントからの入力なので、
    #   "/" や ".." を含む値でCARDS_DIR外のファイルを操作されないようにする。
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"ok": False, "error": "invalid filename"})
    # ★ 追加：作成者本人以外は直接削除できない（非公開に戻す＝menuUnpublish()も
    #   このAPIを叩くため、ここを守るだけで「削除」「非公開に戻す」両方に効く）。
    #   作成者以外が削除したい場合は /request_delete で本人にDiscord確認を送る。
    owner_id, owner_nickname = _deck_owner(filename)
    if owner_id and str(owner_id) != str(_student_id):
        return jsonify({
            "ok": False,
            "error": "creator_approval_required",
            "owner_nickname": owner_nickname or "作成者",
        })
    return _delete_card_deck_file(filename, nickname)

# ================================
#  ★ Flask API — みんなでクイズ（オンライン早押し4択）
#  ─────────────────────────────
#  Quiz.js から呼ばれる。ルームの状態は「今まさに進行中のゲーム」にしか
#  意味を持たない一時的なデータなので、他のAPI（予定・単語カード等）の
#  ようにディスクへは永続化せず、プロセスのメモリ上（QUIZ_ROOMS）だけで
#  管理する（LINK_CODES / OAUTH_STATES と同じ考え方）。
#  ・code（5桁の招待コード）でルームを引く。
#  ・state は "lobby"（開始待ち）→ "countdown"（開始直後の5秒カウントダウン、
#    最初の問題の前だけ）→ "intro"（「第N問」を大きく見せる区間、毎問の前）
#    → "question"（出題中）→ "reveal"（正解発表）→ （次の問題があれば
#    "intro" に戻る／無ければ "ended"）と遷移する。
#    ホストの操作待ちにはせず、すべて自動で進行する：
#      "countdown" → "intro" … QUIZ_COUNTDOWN_DURATION_SEC秒経ったら。
#      "intro"     → "question" … QUIZ_INTRO_DURATION_SEC秒経ったら。
#      "question"  → "reveal" … 全員が回答し終わった、または制限時間
#                    （QUIZ_TIME_LIMIT_SEC）が経過したら自動的に切り替わる。
#      "reveal"    → 次の問題("intro") / "ended" … 発表から
#                    QUIZ_REVEAL_DURATION_SEC秒経ったら自動的に進む。
#    この判定は各APIリクエストのたびに _quiz_autoadvance_locked() で
#    その場評価する（study_timers の自動休憩判定と同じ「アクセスの
#    たびに評価する」方式。専用のバックグラウンドジョブは持たない）。
#    フロントは1秒ごとにポーリングしているので、実際の進行から
#    最大でも1秒程度の遅れで反映される。
#  ・ホスト（作成者）も1人のプレイヤーとして最初から参加者に含まれ、
#    他の参加者と同じように回答してスコアを競える。そのため、正解番号は
#    ホストにも「発表(reveal)されるまでは」一切渡さない（渡すとホストだけ
#    先に答えを知れてしまう）。
#  ・得点：正解 +10pt。そのうち、その問題で一番早く正解した1人だけ
#    さらに +2pt のボーナス（合計12pt）が付く。
#  ・プロセス再起動でルームは全て消える（＝進行中のクイズは失われる）が、
#    ゲームの性質上（せいぜい数十分で終わる遊び）これは許容している。
# ================================
QUIZ_ROOMS = {}
QUIZ_ROOMS_LOCK = Lock()
QUIZ_ROOM_CODE_LEN = 5
QUIZ_ROOM_IDLE_TTL_SEC = 3 * 60 * 60   # 3時間アクセスが無ければ破棄する（ホスト放置対策）
QUIZ_ROOM_ENDED_TTL_SEC = 15 * 60      # 終了後もしばらくは結果画面を見られるよう残しておく
QUIZ_TIME_LIMIT_SEC = 20        # 1問あたりの制限時間（固定）
QUIZ_REVEAL_DURATION_SEC = 5    # 正解発表から次の問題に自動で進むまでの待ち時間
QUIZ_COUNTDOWN_DURATION_SEC = 5 # ★ 追加：スタート直後の「5,4,3,2,1」カウントダウン（最初の問題の前だけ）
QUIZ_INTRO_DURATION_SEC = 2     # ★ 追加：各問題の直前に「第N問」を大きく表示しておく時間
QUIZ_MAX_QUESTIONS = 30
QUIZ_MAX_SOURCE_DECKS = 30  # ★ 追加：「デッキから自動作成」で一度に選べるデッキ数の上限（フォルダ選択で一気に増えうるため）
QUIZ_ANSWER_BASE_POINTS = 10
QUIZ_FIRST_CORRECT_BONUS = 2

# ★ フロント（Login.js の AVATAR_COLORS / paletteFor）と全く同じ規則で、
#   生徒IDから常に同じ色を選ぶ（クイズの参加者一覧・順位表の表示用）。
#   ログインセッション自体に色が入っているのは「自分」だけなので、
#   他の参加者の色はサーバー側でも同じ計算式で求める必要がある。
QUIZ_AVATAR_COLORS = [
    ("#dbeafe", "#1e40af"),
    ("#dcfce7", "#166534"),
    ("#fce7f3", "#9d174d"),
    ("#ffedd5", "#9a3412"),
    ("#fef9c3", "#854d0e"),
    ("#ede9fe", "#6d28d9"),
    ("#fee2e2", "#991b1b"),
    ("#f0fdf4", "#15803d"),
]

def _quiz_palette_for(student_id: str):
    h = 0
    for ch in str(student_id):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF  # JSの `hash = (hash*31+charCode) >>> 0` を再現
    return QUIZ_AVATAR_COLORS[h % len(QUIZ_AVATAR_COLORS)]

def _generate_quiz_code() -> str:
    # /id連携コードと同様、紛らわしい文字(0/O, 1/I等)を除いた大文字+数字。
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(QUIZ_ROOM_CODE_LEN))
        if code not in QUIZ_ROOMS:
            return code

def _quiz_gc_locked(now):
    """QUIZ_ROOMS_LOCK を保持している状態で呼び出すこと。期限切れのルームを間引く。"""
    stale = [
        code for code, room in QUIZ_ROOMS.items()
        if now - room["last_activity"] > QUIZ_ROOM_IDLE_TTL_SEC
        or (room["state"] == "ended" and room.get("ended_at") and now - room["ended_at"] > QUIZ_ROOM_ENDED_TTL_SEC)
    ]
    for code in stale:
        del QUIZ_ROOMS[code]

def _quiz_autoadvance_locked(room, now):
    """QUIZ_ROOMS_LOCK を保持している状態で呼び出すこと。
    ホストの操作を待たず、時間経過や全員の回答状況に応じてルームの
    stateを自動的に1段階（必要なら複数段階）進める。
      ・"countdown" → QUIZ_COUNTDOWN_DURATION_SEC秒経ったら → "intro"
      ・"intro"     → QUIZ_INTRO_DURATION_SEC秒経ったら → "question"
      ・"question"  → 全員が回答し終わった、または制限時間が過ぎたら → "reveal"
      ・"reveal"    → 発表からQUIZ_REVEAL_DURATION_SEC秒経ったら
                      → 次の問題("intro")（無ければ "ended"）
    """
    while True:
        if room["state"] == "countdown":
            if now - room["countdown_started_at"] < QUIZ_COUNTDOWN_DURATION_SEC:
                return
            room["state"] = "intro"
            room["intro_started_at"] = now
        elif room["state"] == "intro":
            if now - room["intro_started_at"] < QUIZ_INTRO_DURATION_SEC:
                return
            # ★ 「第N問」を見せ終えたこの瞬間から制限時間のカウントを始める
            #   （イントロ表示中の時間は回答時間から差し引かれない）。
            room["state"] = "question"
            room["question_started_at"] = now
            room["intro_started_at"] = None
        elif room["state"] == "question":
            total = len(room["players"])
            answered = sum(1 for p in room["players"].values() if p["cur_answer"] is not None)
            time_up = (now - room["question_started_at"]) >= room["time_limit_sec"]
            all_answered = total > 0 and answered >= total
            if not (time_up or all_answered):
                return
            room["state"] = "reveal"
            room["reveal_started_at"] = now
        elif room["state"] == "reveal":
            if now - room["reveal_started_at"] < QUIZ_REVEAL_DURATION_SEC:
                return
            if room["current_q"] + 1 >= len(room["questions"]):
                room["state"] = "ended"
                room["ended_at"] = now
                _archive_room_if_needed(room)
                return
            room["current_q"] += 1
            # ★ 変更：次の問題にすぐ切り替えず、まず"intro"（「第N問」表示）を挟む。
            room["state"] = "intro"
            room["intro_started_at"] = now
            room["question_started_at"] = None
            room["reveal_started_at"] = None
            room["first_correct_id"] = None
            room["first_correct_nickname"] = None
            for p in room["players"].values():
                p["cur_answer"] = None
                p["cur_correct"] = None
            # ★ 次の問題もすぐ制限時間切れ…という極端なケースは無いはずだが、
            #   万一に備えてループで再評価する（whileで継続）。
        else:
            return

QUIZ_SCHEDULER_TICK_SEC = 0.15  # ★ 追加：誰もポーリングしていなくても、この間隔で自動的に時間切れをチェックする

def _quiz_scheduler_loop():
    """
    ★ 追加：以前は「誰かがAPI（ポーリング等）を呼んだ瞬間にだけ」時間切れを
      チェックしていたため（_quiz_autoadvance_locked はそこでしか呼ばれて
      いなかった）、次に誰かがポーリングするまで状態の切り替わりが遅れて
      いた（ポーリング間隔ぶんの遅延）。
      この専用スレッドが誰のリクエストも待たずに一定間隔で全部屋を自動的に
      チェックし、状態が変わった瞬間にSSE(notify_change)で全員へ即座に
      知らせることで、「時間になったら勝手に切り替わる」ようにする。
    ★ Discord bot側の非同期ループ（AsyncIOScheduler）とは完全に別の、
      独立したスレッドで動かす。QUIZ_ROOMS_LOCK を握るのは一瞬（軽い辞書
      走査だけ）なので、Discord側や他のAPIリクエストをブロックする心配は
      ほぼ無い。
    """
    while True:
        time.sleep(QUIZ_SCHEDULER_TICK_SEC)
        try:
            now = time.time()
            changed_guild_ids = set()
            with QUIZ_ROOMS_LOCK:
                for room in QUIZ_ROOMS.values():
                    state_before = room["state"]
                    _quiz_autoadvance_locked(room, now)
                    if room["state"] != state_before:
                        changed_guild_ids.add(room["guild_id"])
            for guild_id in changed_guild_ids:
                notify_change(guild_id)
        except Exception as e:
            # ★ 1回のチェックで例外が起きても、このスレッド自体は止めない
            #   （止まるとクイズの自動進行がポーリング頼みに戻ってしまうため）。
            print(f"[ERROR] quiz scheduler tick failed: {e}")

def start_quiz_scheduler():
    Thread(target=_quiz_scheduler_loop, daemon=True).start()
    print("[INFO] Quiz scheduler thread started")

def _quiz_auth_from_json():
    """POST系クイズAPI共通：JSONボディからguild_id・session_tokenを検証し、
    (data, guild_id, student_id, nickname, error_response) を返す。
    ★ nickname はクライアントの自己申告ではなく、サーバー側のユーザーデータから
    引き直す（他の参加者への表示名の詐称防止）。"""
    data = request.json or {}
    guild_id = data.get("guild_id")
    if not guild_id:
        return None, None, None, None, jsonify({"ok": False, "error": "missing guild_id"})
    guild_id = int(guild_id)
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return None, None, None, None, jsonify({"ok": False, "error": "not_logged_in"})
    user = find_user(guild_id, student_id)
    nickname = (user or {}).get("nickname") or str(student_id)
    return data, guild_id, student_id, nickname, None

def _quiz_get_room_or_error(code):
    room = QUIZ_ROOMS.get((code or "").strip().upper())
    if room is None:
        return None, jsonify({"ok": False, "error": "room_not_found"})
    return room, None

def _quiz_room_players_json(room, include_correct=False):
    players = sorted(room["players"].values(), key=lambda p: -p["score"])
    result = []
    for p in players:
        entry = {
            "id": p["id"],
            "nickname": p["nickname"],
            "color": p["color"],
            "text_color": p["text_color"],
            "score": p["score"],
        }
        if include_correct:
            # ★ 正解発表(reveal)中だけ、他の参加者の今回の問題への正誤も見せる。
            #   出題中(question)にこれを渡すと、まだ発表前の正解がバレてしまうため、
            #   include_correct=True は reveal のときにしか呼ばない。
            answered = p["cur_answer"] is not None
            entry["answered"] = answered
            entry["correct"] = bool(p["cur_correct"]) if answered else None
        result.append(entry)
    return result

def _quiz_room_snapshot(room, student_id):
    snap = {
        "code": room["code"],
        "title": room["title"],
        "state": room["state"],
        "host_nickname": room["host_nickname"],
        "players": _quiz_room_players_json(room),
    }
    if room["state"] == "countdown":
        snap.update({
            "current_q": room["current_q"],
            "total_questions": len(room["questions"]),
            "countdown_started_at": room["countdown_started_at"],
            "countdown_duration_sec": QUIZ_COUNTDOWN_DURATION_SEC,
        })
    elif room["state"] == "intro":
        # ★ 「第N問」表示中は、まだ問題文・選択肢は渡さない
        #   （question状態になってから渡せば十分で、渡す情報は少ない方がよい）。
        snap.update({
            "current_q": room["current_q"],
            "total_questions": len(room["questions"]),
            "intro_started_at": room["intro_started_at"],
            "intro_duration_sec": QUIZ_INTRO_DURATION_SEC,
        })
    elif room["state"] in ("question", "reveal"):
        q = room["questions"][room["current_q"]]
        revealed = room["state"] == "reveal"
        question_payload = {"question": q["question"], "choices": q["choices"]}
        # ★ 正解番号は、発表(reveal)されるまでは誰にも渡さない（レスポンスを
        #   devtools等で覗かれてカンニングされるのを防ぐ）。ホストも今は
        #   1プレイヤーとして参加するため、ホストだけ特別扱いはしない。
        if revealed:
            question_payload["correct_index"] = q["correct_index"]
        snap.update({
            "current_q": room["current_q"],
            "total_questions": len(room["questions"]),
            "question": question_payload,
            "question_started_at": room["question_started_at"],
            "time_limit_sec": room["time_limit_sec"],
            "answered_count": sum(1 for p in room["players"].values() if p["cur_answer"] is not None),
            "total_players": len(room["players"]),
        })
        if revealed:
            snap["first_correct_nickname"] = room.get("first_correct_nickname")
            snap["reveal_started_at"] = room.get("reveal_started_at")
            snap["reveal_duration_sec"] = QUIZ_REVEAL_DURATION_SEC
            # ★ 発表中だけ、全員分の正誤(◯×)を含めて players を上書きする
            snap["players"] = _quiz_room_players_json(room, include_correct=True)
        player = room["players"].get(student_id)
        if player is not None and player["cur_answer"] is not None:
            snap["your_answer"] = player["cur_answer"]
            if revealed:
                snap["your_correct"] = bool(player["cur_correct"])
    return snap

def _pick_distractors(correct: str, pool: list, k: int) -> list:
    """
    正解(correct)に対する誤答をpoolからk個選ぶ。
    ★ 完全ランダムに選ぶと、他のカードの答えと文字数も内容もかけ離れた
      誤答ばかりになりがちで、見た目だけで消去法に正解できてしまっていた。
      正解と文字列として近い（綴り・字面が似ている）ものを優先候補にし、
      その中からランダムに選ぶことで、きちんと覚えていないと迷うような
      4択にする。
      ★ さらに、綴りの類似度（SequenceMatcher）だけだと「文字数が全然違う
      せいで一目で誤答と分かる」選択肢が紛れ込みやすかったため、文字数の
      近さもスコアに加味する。また、候補の絞り込みを従来（上位9件から
      ランダムに3件）よりタイトにする（上位6件から3件）ことで、
      「似ているが選ばれなかった」紛らわしい候補が混ざりにくくし、
      パッと見で消去法が効きにくい4択にする。
    """
    def _score(a):
        seq_ratio = difflib.SequenceMatcher(None, correct, a).ratio()
        longer = max(len(correct), len(a), 1)
        length_ratio = 1 - abs(len(correct) - len(a)) / longer
        return seq_ratio * 0.7 + length_ratio * 0.3

    scored = sorted(pool, key=_score, reverse=True)
    pool_size = max(k, min(len(scored), k * 2))
    return random.sample(scored[:pool_size], k)


def _build_deck_questions(deck_filenames, num_questions):
    """単語カードのデッキ（表面=question／裏面=answer）から、答えをシャッフルした
    4択問題を自動生成する。
    ★ deck_filenames は単一のファイル名（文字列）でも、複数のファイル名のリストでも
      受け取れる。複数指定した場合（Web側でフォルダごと選んだ場合を含む）は、
      それぞれのデッキのカードをまとめて1つの問題プールとして扱い、
      distractor（不正解の選択肢）も選んだデッキ全体から選ぶ。
    (questions, error_code) を返す（成功時 error_code は None）。"""
    if isinstance(deck_filenames, str):
        deck_filenames = [deck_filenames]
    if not isinstance(deck_filenames, list) or not deck_filenames:
        return None, "deck_not_found"
    # ★ 重複を除きつつ順序は維持する
    seen_filenames = set()
    deck_filenames = [f for f in deck_filenames if not (f in seen_filenames or seen_filenames.add(f))]
    if len(deck_filenames) > QUIZ_MAX_SOURCE_DECKS:
        return None, "too_many_decks"

    cards = []
    for deck_filename in deck_filenames:
        if not isinstance(deck_filename, str) or "/" in deck_filename or "\\" in deck_filename or ".." in deck_filename:
            return None, "deck_not_found"
        data, _ = get_card_file(deck_filename)
        if data is None:
            return None, "deck_not_found"
        cards.extend(
            c for c in data.get("cards", [])
            if isinstance(c, dict) and str(c.get("question") or "").strip() and str(c.get("answer") or "").strip()
        )
    unique_answers = {c["answer"].strip() for c in cards}
    # ★ 4択（正解1つ＋不正解3つ）を作るには、答えの異なり（ユニークな値）が
    #   最低4つ必要。html側の注意書き（「答えの種類が4つ以上あるデッキが必要」）と対応。
    if len(cards) < 4 or len(unique_answers) < 4:
        return None, "deck_too_small"

    try:
        num_questions = int(num_questions) if num_questions else None
    except (TypeError, ValueError):
        num_questions = None
    n = min(num_questions, len(cards)) if num_questions else len(cards)
    n = max(1, min(n, QUIZ_MAX_QUESTIONS, len(cards)))

    picked = random.sample(cards, n)
    questions = []
    for card in picked:
        correct = card["answer"].strip()
        pool = list(dict.fromkeys(  # 同じ答え文言の重複を除去しつつ順序を保つ
            c["answer"].strip() for c in cards if c is not card and c["answer"].strip() != correct
        ))
        if len(pool) < 3:
            return None, "deck_too_small"
        choices = _pick_distractors(correct, pool, 3) + [correct]
        random.shuffle(choices)
        questions.append({
            "question": card["question"].strip(),
            "choices": choices,
            "correct_index": choices.index(correct),
        })
    return questions, None

def _validate_manual_questions(raw_questions):
    """ホストが手入力した問題データを検証する。
    成功時: ((questions, check_fields), None) / 失敗時: (None, error_code)"""
    if not isinstance(raw_questions, list) or not raw_questions:
        return None, "invalid_questions"
    questions = []
    check_fields = {}
    for i, q in enumerate(raw_questions):
        if not isinstance(q, dict):
            return None, "invalid_questions"
        question_text = str(q.get("question") or "").strip()
        choices = q.get("choices")
        correct_index = q.get("correct_index")
        if not question_text or not isinstance(choices, list) or len(choices) != 4:
            return None, "invalid_questions"
        choices = [str(c or "").strip() for c in choices]
        if any(not c for c in choices):
            return None, "invalid_questions"
        if not isinstance(correct_index, int) or isinstance(correct_index, bool) or not (0 <= correct_index < 4):
            return None, "invalid_questions"
        check_fields[f"問題{i+1}の問題文"] = question_text
        for j, c in enumerate(choices):
            check_fields[f"問題{i+1}の選択肢{j+1}"] = c
        questions.append({"question": question_text, "choices": choices, "correct_index": correct_index})
    return (questions, check_fields), None

def _archive_manual_quiz(title, questions, student_id, nickname):
    """
    ★ ホストが「自分で問題を作る」（オリジナル4択）で作成したクイズを、
      CardMakerの「クイズ過去問」フォルダにデッキとして自動保存する。
      いつでも一人用選択式モードで遊べる「過去問」として残すため。
      呼び出し元は _archive_room_if_needed()（クイズが終了した時点で呼ばれる）。
    ・questions は _validate_manual_questions の戻り値そのもの
      （[{"question", "choices"(4件), "correct_index"}, ...]）。
    ・choice_mode/choices/correct_indices は、CardMaker側の選択式デッキ共通
      フォーマット。単一/複数正解はデッキ単位ではなく問題ごとに
      correct_indices の個数で決まる（CardMaker側の仕様）。Quiz.js自体は
      4択・単一正解の固定フォーマットのままで、ここでの変換にしか影響しない。
    ・answer（正解の選択肢文言）も入れておく。これにより単語検索・一覧表示・
      作成済みリストなど、「answerは文字列である」という前提の既存コードを
      一切変更せずに動かせる（choices/correct_indices は選択式UIだけが見る）。
    ・アーカイブに失敗しても、クイズ自体の進行は失敗させない（ベストエフォート）。
    """
    try:
        _ensure_quiz_archive_folder()
        cards = [{
            "id": secrets.token_hex(6),
            "question": q["question"],
            "answer": q["choices"][q["correct_index"]],
            "choices": q["choices"],
            "correct_indices": [q["correct_index"]],
            "explanation": "",
            "imgs_q": [], "imgs_a": [], "imgs_e": [],
        } for q in questions]
        filename = generate_card_filename()
        card_payload = {
            "name": title,
            "cards": cards,
            "subject": None,
            "folder_id": QUIZ_ARCHIVE_FOLDER_ID,
            "published_by": {"id": student_id, "nickname": nickname},
            "incomplete": False,
            "choice_mode": True,  # ★ 選択式デッキであることのマーカー（単一/複数は問題ごとにcorrect_indicesの個数で決まる）
        }
        put_card_file(filename, card_payload)
        index_change = upsert_cards_index_entry(filename, card_payload)
        change = deck_file_diff(f"{CARDS_DIR}/{filename}", None, card_payload)
        detail = [c for c in (change, index_change) if c]
        log_event(
            "card",
            f"みんなでクイズの結果を「{title}」として「クイズ過去問」に保存しました（{len(cards)}問）。",
            actor=nickname,
            detail=detail if detail else None,
        )
    except Exception as e:
        print(f"[WARN] クイズ過去問の保存に失敗しました（クイズの進行自体は続行）: {e}")

def _archive_room_if_needed(room):
    """
    ★ QUIZ_ROOMS_LOCK を保持している状態で呼び出すこと。
      ルームが終了(state=="ended")した瞬間に1回だけ呼ばれ、オリジナル4択
      （source=="manual"）だったクイズをCardMakerへアーカイブする。
      room["archived"] で二重登録を防ぐ（自然終了とホストの手動終了の
      両方から呼ばれ得るため）。
    """
    if room.get("source") != "manual" or room.get("archived"):
        return
    room["archived"] = True
    _archive_manual_quiz(room["title"], room["questions"], room["host_id"], room["host_nickname"])

@app.route("/quiz_create", methods=["POST"])
def quiz_create():
    data, guild_id, student_id, nickname, err = _quiz_auth_from_json()
    if err:
        return err

    title = str(data.get("title") or "").strip()[:40] or "みんなでクイズ"

    source = data.get("source")
    if source == "deck":
        # ★ deck_filenames（複数選択・フォルダ選択に対応）を優先し、
        #   後方互換として旧形式の単一 deck_filename も引き続き受け付ける。
        deck_filenames = data.get("deck_filenames")
        if not deck_filenames:
            single = data.get("deck_filename")
            deck_filenames = [single] if single else None
        if not deck_filenames:
            return jsonify({"ok": False, "error": "deck_not_found"})
        questions, q_err = _build_deck_questions(deck_filenames, data.get("num_questions"))
        if q_err:
            return jsonify({"ok": False, "error": q_err})
        err = reject_if_bug_chars({"タイトル": title})
        if err:
            return err
    elif source == "manual":
        result, q_err = _validate_manual_questions(data.get("questions"))
        if q_err:
            return jsonify({"ok": False, "error": q_err})
        questions, check_fields = result
        check_fields["タイトル"] = title
        err = reject_if_bug_chars(check_fields)
        if err:
            return err
        # ★ CardMakerへのアーカイブはここ（作成時）ではなく、クイズが終了した
        #   瞬間に行う（_archive_room_if_needed、state=="ended"になった時点）。
    else:
        return jsonify({"ok": False, "error": "invalid_source"})

    # ★ 途中参加を許可するかどうかは、ホストが作成時に選ぶ（デフォルトは不許可＝従来通り）。
    #   許可した場合、開始後（question/reveal中）でもルーム一覧に「プレイ中」として
    #   表示され続け、そこから参加できる。不許可の場合は従来通り開始と同時に
    #   一覧から実質参加不可になる（表示はされるが参加はできない）。
    allow_late_join = bool(data.get("allow_late_join"))

    now = time.time()
    with QUIZ_ROOMS_LOCK:
        _quiz_gc_locked(now)
        code = _generate_quiz_code()
        # ★ ホスト（作成者）自身も、最初から1人のプレイヤーとして参加者に含める
        #   （開催している本人も一緒に回答してスコアを競えるようにするため）。
        host_color, host_text_color = _quiz_palette_for(student_id)
        QUIZ_ROOMS[code] = {
            "code": code,
            "guild_id": guild_id,
            "title": title,
            "time_limit_sec": QUIZ_TIME_LIMIT_SEC,
            "questions": questions,
            "source": source,  # ★ "manual"のクイズだけ、終了時にCardMakerへアーカイブする
            "archived": False,
            "host_id": student_id,
            "host_nickname": nickname,
            "allow_late_join": allow_late_join,
            "players": {
                student_id: {
                    "id": student_id,
                    "nickname": nickname,
                    "color": host_color,
                    "text_color": host_text_color,
                    "score": 0,
                    "cur_answer": None,
                    "cur_correct": None,
                },
            },
            "state": "lobby",
            "current_q": 0,
            "countdown_started_at": None,
            "intro_started_at": None,
            "question_started_at": None,
            "reveal_started_at": None,
            "first_correct_id": None,
            "first_correct_nickname": None,
            "created_at": now,
            "last_activity": now,
            "ended_at": None,
        }
    # ★ クイズの開始は予定管理などと違い頻繁に行われる一時的な操作なので、
    #   write_log（予定の追加・編集・削除ログ）には残さない。
    return jsonify({"ok": True, "code": code})

# ================================
#  ★ クイズ過去問（CardMaker内の一人用4択モード）のランキング
#  ─────────────────────────────
#  「クイズ過去問」フォルダにアーカイブされたデッキ1つにつき1ファイル
#  （quiz_leaderboard_<デッキのfilename>.json）に、{student_id: {...}} の形で
#  各生徒のベストスコアだけを保持する。QUIZ_ROOMS（ライブルーム）とは異なり、
#  こちらはディスクに永続化する（いつ・誰が挑戦しても記録が残るランキングのため）。
# ================================
def _is_safe_deck_filename(filename):
    return bool(filename) and filename.endswith(".json") \
        and "/" not in filename and "\\" not in filename and ".." not in filename

def _update_quiz_leaderboard(deck_filename, mutate_fn, max_attempts=4):
    """mutate_fn(leaderboard_dict) は dict を直接書き換える関数。
    保存に失敗（sha競合）した場合は最新データを読み直して再適用する。"""
    lb_filename = f"quiz_leaderboard_{deck_filename}"
    last_err = None
    for _ in range(max_attempts):
        data, sha = local_get(lb_filename)
        data = data or {}
        mutate_fn(data)
        try:
            local_put(lb_filename, data, sha)
            return
        except DataWriteError as e:
            last_err = e
            continue
    raise last_err or DataWriteError("保存に失敗しました（リトライ上限）")

@app.route("/quiz_archive_submit_score", methods=["POST"])
def quiz_archive_submit_score():
    """一人用4択モードのスコアを記録する。ベストスコアだけを保持する。"""
    data = request.json or {}
    guild_id = data.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    guild_id = int(guild_id)
    student_id = resolve_session(data.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    filename = data.get("filename") or ""
    if not _is_safe_deck_filename(filename):
        return jsonify({"ok": False, "error": "invalid filename"})

    try:
        score = int(data.get("score"))
        total = int(data.get("total"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid score/total"})
    if score < 0 or total <= 0 or score > total:
        return jsonify({"ok": False, "error": "invalid score/total"})

    # ★ このデッキが実際に「クイズ過去問」フォルダの中にあるか確認する
    #   （でたらめなfilenameを指定してスコアを偽造されるのを防ぐ）
    #   ★ 以前は「クイズ過去問フォルダ内かどうか」で判定していたが、選択式
    #     デッキが汎用機能になったため、choice_modeの有無で判定するよう変更。
    #   ★ choice_modeはtruthyチェックにする（真偽値trueの新形式だけでなく、
    #     移行前に保存された旧形式の文字列"single"/"multi"も引き続き通す）。
    card_data, _ = get_card_file(filename)
    if card_data is None:
        return jsonify({"ok": False, "error": "deck_not_found"})
    if not card_data.get("choice_mode"):
        return jsonify({"ok": False, "error": "not_a_choice_deck"})

    user = find_user(guild_id, student_id)
    nickname = (user or {}).get("nickname") or str(student_id)

    try:
        def _mutate(lb):
            existing = lb.get(student_id)
            if existing is None or score > existing.get("score", -1):
                lb[student_id] = {
                    "nickname": nickname,
                    "score": score,
                    "total": total,
                    "played_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
                }
        _update_quiz_leaderboard(filename, _mutate)
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})

    return jsonify({"ok": True})

@app.route("/quiz_archive_leaderboard", methods=["GET"])
def quiz_archive_leaderboard():
    """指定デッキ（クイズ過去問）のランキングをスコア降順で返す。"""
    filename = request.args.get("filename") or ""
    if not _is_safe_deck_filename(filename):
        return jsonify({"ok": False, "error": "invalid filename"})
    data, _ = local_get(f"quiz_leaderboard_{filename}")
    data = data or {}
    rows = [
        {
            "student_id": sid,
            "nickname": e.get("nickname"),
            "score": e.get("score"),
            "total": e.get("total"),
            "played_at": e.get("played_at"),
        }
        for sid, e in data.items()
    ]
    rows.sort(key=lambda r: (-(r["score"] or 0), r["played_at"] or ""))
    return jsonify({"ok": True, "leaderboard": rows})

@app.route("/quiz_list_rooms", methods=["GET"])
def quiz_list_rooms():
    """
    ★ 参加者向け：コード入力の代わりに、クイズルーム一覧をタイトルで選べるように
      するためのAPI。コード自体は quiz_join の内部識別子として引き続き使うが、
      参加者が手入力する必要はなくなる（一覧の行をタップ→内部的にそのcodeで
      joinする）。
    ・"lobby"（開始待ち）だけでなく、"question"/"reveal"（進行中）のルームも
      「プレイ中」として一覧に出しっぱなしにする（終了するまで一覧から
      消えない）。ホストが作成時に途中参加を許可していれば（allow_late_join）
      進行中でもそこから参加できる。許可していなければ表示だけされ、
      タップしても参加はできない（フロント側で押せないようにする）。
    ・"ended"（終了）になったルームだけ一覧から外す。
    """
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    guild_id = int(guild_id)
    student_id = resolve_session(request.args.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    now = time.time()
    with QUIZ_ROOMS_LOCK:
        _quiz_gc_locked(now)
        rooms = []
        for room in QUIZ_ROOMS.values():
            if room["guild_id"] != guild_id or room["state"] == "ended":
                continue
            _quiz_autoadvance_locked(room, now)  # 一覧表示中もstateを最新化しておく
            if room["state"] == "ended":
                continue
            rooms.append({
                "code": room["code"],
                "title": room["title"],
                "host_nickname": room["host_nickname"],
                "player_count": len(room["players"]),
                "question_count": len(room["questions"]),
                "state": room["state"],  # "lobby" | "countdown" | "intro" | "question" | "reveal"
                "current_q": room["current_q"] if room["state"] != "lobby" else None,
                "allow_late_join": bool(room.get("allow_late_join")),
                "created_at": room["created_at"],
            })
    # ★ 新しく作られたルームほど上に来るようにする（参加者が今開催中のものを探しやすいように）
    rooms.sort(key=lambda r: r["created_at"], reverse=True)
    for r in rooms:
        del r["created_at"]  # フロントには不要な内部情報なので落とす
    return jsonify({"ok": True, "rooms": rooms})

@app.route("/quiz_join", methods=["POST"])
def quiz_join():
    data, guild_id, student_id, nickname, err = _quiz_auth_from_json()
    if err:
        return err
    code = (data.get("code") or "").strip().upper()

    now = time.time()
    with QUIZ_ROOMS_LOCK:
        _quiz_gc_locked(now)
        room, err = _quiz_get_room_or_error(code)
        if err:
            return err
        is_host = (student_id == room["host_id"])
        if not is_host and student_id not in room["players"]:
            # ★ lobby中は誰でも参加可能。開始後（question/reveal）は、
            #   ホストが作成時に途中参加を許可していた場合のみ参加できる。
            #   終了後（ended）はどちらの場合も参加不可。
            if room["state"] == "ended":
                return jsonify({"ok": False, "error": "quiz_already_started"})
            if room["state"] != "lobby" and not room.get("allow_late_join"):
                return jsonify({"ok": False, "error": "quiz_already_started"})
            color, text_color = _quiz_palette_for(student_id)
            room["players"][student_id] = {
                "id": student_id,
                "nickname": nickname,
                "color": color,
                "text_color": text_color,
                "score": 0,
                "cur_answer": None,
                "cur_correct": None,
            }
        room["last_activity"] = now
        _quiz_autoadvance_locked(room, now)
        snap = _quiz_room_snapshot(room, student_id)
    # ★ 遅延低減：参加者が増えたことを、他の端末のポーリング待ちなしで即座に知らせる
    notify_change(guild_id)
    return jsonify({"ok": True, "is_host": is_host, "room": snap, "server_now": int(now * 1000)})

@app.route("/quiz_state", methods=["GET"])
def quiz_state():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    guild_id = int(guild_id)
    student_id = resolve_session(request.args.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})
    code = (request.args.get("code") or "").strip().upper()

    now = time.time()
    with QUIZ_ROOMS_LOCK:
        _quiz_gc_locked(now)
        room, err = _quiz_get_room_or_error(code)
        if err:
            return err
        is_host = (student_id == room["host_id"])
        if not is_host and student_id not in room["players"]:
            # 参加したことのない部屋の状態は覗けないようにする（未参加なら「見つからない」扱い）
            return jsonify({"ok": False, "error": "room_not_found"})
        room["last_activity"] = now
        _quiz_autoadvance_locked(room, now)
        snap = _quiz_room_snapshot(room, student_id)
    return jsonify({"ok": True, "is_host": is_host, "room": snap, "server_now": int(now * 1000)})

@app.route("/quiz_start", methods=["POST"])
def quiz_start():
    data, guild_id, student_id, nickname, err = _quiz_auth_from_json()
    if err:
        return err
    code = (data.get("code") or "").strip().upper()

    now = time.time()
    with QUIZ_ROOMS_LOCK:
        room, err = _quiz_get_room_or_error(code)
        if err:
            return err
        if student_id != room["host_id"]:
            return jsonify({"ok": False, "error": "not_host"})
        if room["state"] != "lobby":
            return jsonify({"ok": False, "error": "quiz_already_started"})
        # ★ 変更：いきなり出題(question)にはせず、まず"countdown"
        #   （5,4,3,2,1のカウントダウン）→"intro"（「第1問」表示）を挟む。
        #   実際の制限時間のカウントは、intro表示が終わってから始まる
        #   （_quiz_autoadvance_locked参照）。
        room["state"] = "countdown"
        room["current_q"] = 0
        room["countdown_started_at"] = now
        room["intro_started_at"] = None
        room["question_started_at"] = None
        room["reveal_started_at"] = None
        room["first_correct_id"] = None
        room["first_correct_nickname"] = None
        for p in room["players"].values():
            p["cur_answer"] = None
            p["cur_correct"] = None
        room["last_activity"] = now
        snap = _quiz_room_snapshot(room, student_id)
    # ★ 遅延低減：カウントダウン開始を、他の端末のポーリング待ちなしで即座に知らせる
    notify_change(guild_id)
    return jsonify({"ok": True, "room": snap, "server_now": int(now * 1000)})

@app.route("/quiz_answer", methods=["POST"])
def quiz_answer():
    data, guild_id, student_id, nickname, err = _quiz_auth_from_json()
    if err:
        return err
    code = (data.get("code") or "").strip().upper()
    choice_index = data.get("choice_index")
    if not isinstance(choice_index, int) or isinstance(choice_index, bool) or not (0 <= choice_index < 4):
        return jsonify({"ok": False, "error": "invalid_choice"})

    now = time.time()
    with QUIZ_ROOMS_LOCK:
        room, err = _quiz_get_room_or_error(code)
        if err:
            return err
        if room["state"] != "question":
            return jsonify({"ok": False, "error": "not_answerable"})
        player = room["players"].get(student_id)
        if player is None:
            return jsonify({"ok": False, "error": "not_in_room"})
        if player["cur_answer"] is not None:
            return jsonify({"ok": False, "error": "already_answered"})

        q = room["questions"][room["current_q"]]
        correct = (choice_index == q["correct_index"])
        player["cur_answer"] = choice_index
        player["cur_correct"] = correct
        if correct:
            points = QUIZ_ANSWER_BASE_POINTS
            if room["first_correct_id"] is None:
                room["first_correct_id"] = student_id
                room["first_correct_nickname"] = player["nickname"]
                points += QUIZ_FIRST_CORRECT_BONUS
            player["score"] += points
        room["last_activity"] = now
        # ★ この回答で全員が回答し終わった場合、次のポーリングを待たずに
        #   その場で正解発表(reveal)へ進める（体感の速さのため）。
        _quiz_autoadvance_locked(room, now)
    # ★ 遅延低減：回答数の増加・reveal切り替わりを、他の端末のポーリング待ちなしで
    #   即座に知らせる（answer自体は毎秒送られるものではないので通知頻度も問題ない）。
    notify_change(guild_id)
    return jsonify({"ok": True})

@app.route("/quiz_end", methods=["POST"])
def quiz_end():
    data, guild_id, student_id, nickname, err = _quiz_auth_from_json()
    if err:
        return err
    code = (data.get("code") or "").strip().upper()

    now = time.time()
    with QUIZ_ROOMS_LOCK:
        room, err = _quiz_get_room_or_error(code)
        if err:
            return err
        if student_id != room["host_id"]:
            return jsonify({"ok": False, "error": "not_host"})
        room["state"] = "ended"
        room["ended_at"] = now
        room["last_activity"] = now
        _archive_room_if_needed(room)
    notify_change(guild_id)
    return jsonify({"ok": True})

@app.route("/quiz_leave", methods=["POST"])
def quiz_leave():
    data, guild_id, student_id, nickname, err = _quiz_auth_from_json()
    if err:
        return err
    code = (data.get("code") or "").strip().upper()

    with QUIZ_ROOMS_LOCK:
        room, err = _quiz_get_room_or_error(code)
        if err:
            return err
        room["players"].pop(student_id, None)
        room["last_activity"] = time.time()
    notify_change(guild_id)
    return jsonify({"ok": True})

# ================================
#  Flask API — 作成中デッキ（公開予定だがまだ未公開のもの）をみんなで共有表示する
# ================================
#  ・カード名だけ入力して「作成」を押した時点で登録し、他の人の一覧にも
#    「🟠 作成中（〇〇さん）」として表示できるようにする。
#  ・カード本体（問題・解答）はここには一切含めない（軽量なメタ情報のみ）。
#  ・実際に公開（save_cards）されたら、対応するエントリはここから取り除く。
#  ・登録から一定期間（IN_PROGRESS_STALE_DAYS）経っても公開されないものは、
#    作成を放棄したものとみなして list_in_progress を返す際に自動的に間引く。
IN_PROGRESS_FILE = "in_progress_decks.json"
IN_PROGRESS_STALE_DAYS = 14

def load_in_progress():
    data, sha = local_get(IN_PROGRESS_FILE)
    return (data or []), sha

def save_in_progress(items, sha=None):
    if sha is None:
        _, sha = local_get(IN_PROGRESS_FILE)
    local_put(IN_PROGRESS_FILE, items, sha)

def _prune_stale_in_progress(items):
    """登録から IN_PROGRESS_STALE_DAYS 日以上経過したエントリを取り除いた新しいリストを返す。
    （壊れた/古い形式の created_at は安全側に倒して除外しない）"""
    now_jst = datetime.now(JST)
    kept = []
    for it in items:
        created_at = it.get("created_at")
        try:
            created_dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
            if (now_jst - created_dt).days > IN_PROGRESS_STALE_DAYS:
                continue
        except Exception:
            pass
        kept.append(it)
    return kept

@app.route("/list_in_progress", methods=["GET"])
def list_in_progress():
    try:
        items, sha = load_in_progress()
        pruned = _prune_stale_in_progress(items)
        if len(pruned) != len(items):
            try:
                save_in_progress(pruned, sha)
            except DataWriteError as e:
                print(f"[WARN] in_progress の自動間引き保存に失敗しました: {e}")
        return jsonify({"ok": True, "items": pruned})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/register_in_progress", methods=["POST"])
def register_in_progress():
    """
    body: { id, name, subject, folder_id, creator_id, creator_nickname }
    ・id はフロント側で生成しているデッキのローカルID（他人と衝突しない前提）。
    ・同じ id で既にエントリがある場合は上書きする（念のため）。
    """
    data = request.json or {}
    draft_id = data.get("id")
    name     = data.get("name")
    if not draft_id or not name:
        return jsonify({"ok": False, "error": "id と name は必須です"})

    creator_nickname = data.get("creator_nickname") or "匿名"
    err = reject_if_bug_chars({
        "デッキ名": name,
        "科目": data.get("subject"),
        "作成者ニックネーム": creator_nickname,
    })
    if err:
        return err

    entry = {
        "id": draft_id,
        "name": name,
        "subject": data.get("subject"),
        "folder_id": data.get("folder_id"),
        "creator_id": data.get("creator_id"),
        "creator_nickname": creator_nickname,
        "created_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        items, sha = load_in_progress()
        items = [it for it in items if it.get("id") != draft_id]
        items.append(entry)
        save_in_progress(items, sha)
        return jsonify({"ok": True})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/update_in_progress", methods=["POST"])
def update_in_progress():
    """
    body: { id, name?, subject?, folder_id? }
    作成中デッキの名前変更・フォルダ移動をみんなの表示にも反映する。
    該当エントリが無ければ（既に公開済み・削除済みなど）何もせず ok:true を返す。
    """
    data     = request.json or {}
    draft_id = data.get("id")
    if not draft_id:
        return jsonify({"ok": False, "error": "id は必須です"})
    try:
        items, sha = load_in_progress()
        found = False
        for it in items:
            if it.get("id") == draft_id:
                if "name" in data:      it["name"]      = data["name"]
                if "subject" in data:   it["subject"]   = data["subject"]
                if "folder_id" in data: it["folder_id"] = data["folder_id"]
                found = True
                break
        if found:
            save_in_progress(items, sha)
        return jsonify({"ok": True, "found": found})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/remove_in_progress", methods=["POST"])
def remove_in_progress():
    """body: { id } — 公開された・削除された・非公開のまま維持することにした等で不要になったエントリを消す。"""
    data     = request.json or {}
    draft_id = data.get("id")
    if not draft_id:
        return jsonify({"ok": False, "error": "id は必須です"})
    try:
        items, sha = load_in_progress()
        new_items = [it for it in items if it.get("id") != draft_id]
        if len(new_items) != len(items):
            save_in_progress(new_items, sha)
        return jsonify({"ok": True})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
# ================================================================
#  Flask API — お知らせ（notices）
# ================================================================

NOTICES_DIR = "notices"
NOTICES_META_FILE = "notices_meta.json"
NOTICE_ALLOWED_EXT = (".md", ".txt")


def _is_safe_notice_filename(filename: str) -> bool:
    """パストラバーサル対策・拡張子チェック"""
    if not filename:
        return False
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    return filename.lower().endswith(NOTICE_ALLOWED_EXT)


def list_notice_files():
    dir_path = _data_path(NOTICES_DIR)
    if not os.path.isdir(dir_path):
        return []
    results = []
    for name in sorted(os.listdir(dir_path)):
        if not name.lower().endswith(NOTICE_ALLOWED_EXT):
            continue
        full_path = os.path.join(dir_path, name)
        if os.path.isfile(full_path):
            results.append({"name": name, "size": os.path.getsize(full_path)})
    return results


def load_notices_meta():
    data, sha = local_get(NOTICES_META_FILE)
    return (data or {}), sha


def save_notices_meta(meta, sha=None):
    if sha is None:
        _, sha = local_get(NOTICES_META_FILE)
    local_put(NOTICES_META_FILE, meta, sha)
    notify_change()  # ★ お知らせもguildをまたいで共有されるため全体に通知

def _notice_meta_entry_lines(filename, entry):
    """notices_meta.json内の1エントリを { ... } のブロックにする。
    ★ お知らせのfilenameはNotice.js上でそのままタイトルとして全員に
    表示されている情報なので、他のカテゴリの内部ファイル名とは違い、
    フィールドとして表示してよい。"""
    if not entry:
        return []
    fields = [
        ("お知らせ", filename),
        ("投稿者", entry.get('uploader')),
        ("投稿日時", entry.get('uploaded_at')),
        ("状態", "実行済み" if entry.get("done") else "未実行"),
    ]
    return _json_block(fields)

def _notices_meta_text(meta):
    """運用ログ用：notices_meta.json（投稿者・実行済みフラグ等）を
    { ... } のブロックの並びにする。"""
    lines = []
    for filename, entry in (meta or {}).items():
        lines.extend(_notice_meta_entry_lines(filename, entry))
    return "\n".join(lines)

def _notice_meta_entry_diff(filename, old_entry, new_entry):
    """notices_meta.json内の1エントリの変更を、log_event の detail に
    渡す {"file","diff","status"} の形にする（無ければNone）。
    ★ 追加（2026/08/19）：upload_notice/delete_noticeは、お知らせ本体の
    ファイルだけでなくnotices_meta.json（投稿者・投稿日時）も実際に
    書き換えているのに、これまで運用ログに出ていなかったため対応。"""
    old_text = "\n".join(_notice_meta_entry_lines(filename, old_entry))
    new_text = "\n".join(_notice_meta_entry_lines(filename, new_entry))
    diff = _text_diff_lines(old_text, new_text)
    if not diff:
        return None
    status = "added" if old_entry is None else ("deleted" if new_entry is None else "modified")
    return {"file": NOTICES_META_FILE, "diff": diff, "status": status}


@app.route("/list_notices", methods=["GET"])
def list_notices():
    """お知らせファイルの一覧を返す（中身は含まない、投稿者名つき）"""
    try:
        files = list_notice_files()
        meta, _ = load_notices_meta()
        notices = []
        for f in files:
            m = meta.get(f["name"], {})
            notices.append({
                "filename": f["name"],
                "size": f.get("size"),
                "ext": f["name"].rsplit(".", 1)[-1].lower(),
                "uploader": m.get("uploader"),
                "uploaded_at": m.get("uploaded_at"),
                "done": bool(m.get("done")),  # ★ 追加：実行済み（全員共有）
            })
        # ファイル名（先頭に日付を付ける運用を推奨）で新しい順に並べる
        notices.sort(key=lambda n: n["filename"], reverse=True)
        return jsonify({"ok": True, "notices": notices})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/get_notice", methods=["GET"])
def get_notice():
    """お知らせ1件の中身（テキスト本文）と投稿者名を返す"""
    filename = request.args.get("filename", "")
    if not _is_safe_notice_filename(filename):
        return jsonify({"ok": False, "error": "invalid filename"})
    try:
        path = _data_path(f"{NOTICES_DIR}/{filename}")
        if not os.path.isfile(path):
            return jsonify({"ok": False, "error": "not found"})
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        meta, _ = load_notices_meta()
        m = meta.get(filename, {})

        return jsonify({
            "ok": True,
            "filename": filename,
            "content": content,
            "uploader": m.get("uploader"),
            "uploaded_at": m.get("uploaded_at"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/upload_notice", methods=["POST"])
def upload_notice():
    """お知らせファイル（.md / .txt）をアップロード（新規 or 上書き）する"""
    data = request.json or {}
    guild_id, _student_id, resolved_nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    filename = (data.get("filename") or "").strip()
    content = data.get("content")
    uploader = resolved_nickname or "匿名"  # ★ クライアント自己申告ではなく、ログインセッションから引く

    if not _is_safe_notice_filename(filename):
        return jsonify({"ok": False, "error": ".md または .txt ファイルのみアップロードできます"})
    if content is None or not content.strip():
        return jsonify({"ok": False, "error": "内容が空です"})

    err = reject_if_bug_chars({"内容": content, "アップロード者": uploader})
    if err:
        return err

    path = _data_path(f"{NOTICES_DIR}/{filename}")
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    # 既存ファイルなら上書き
    is_update = os.path.isfile(path)
    # ★ 運用ログで内容の差分を見せるため、上書き前に旧内容を読んでおく。
    #   読めなくても（＝新規作成、または読み取り失敗）アップロード自体は続行する。
    old_content = None
    if is_update:
        try:
            with open(path, "r", encoding="utf-8") as f:
                old_content = f.read()
        except OSError:
            old_content = None
    try:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except OSError as e:
        return jsonify({
            "ok": False,
            "error": f"local_write_failed: {e}"
        })

    # --- 投稿者メタ情報を notices_meta.json に保存 ---
    meta_change = None
    try:
        meta, meta_sha = load_notices_meta()
        old_meta_entry = meta.get(filename)
        meta[filename] = {
            "uploader": uploader,
            # ★ 追加：削除の作成者確認機能で使うため、投稿者の学籍番号（student_id）も
            #   保存しておく。uploaderは表示名（ニックネーム）で改名され得るため、
            #   本人特定にはこちらを使う。運用ログの表示（_notice_meta_entry_lines）
            #   には出さない（Discord ID等と同じ扱いで、ニックネーム以上に個人を
            #   特定できる情報を公開の場に出さない方針のため）。
            "uploader_id": _student_id,
            "uploaded_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        }
        save_notices_meta(meta, meta_sha)
        meta_change = _notice_meta_entry_diff(filename, old_meta_entry, meta[filename])
    except DataWriteError as e:
        # 本体の保存自体は成功しているので、メタ情報の失敗は警告に留める
        print(f"[WARN] notices_meta の更新に失敗しました: {e}")

    # --- 任意：Discordの通知チャンネルに投稿 ---
    #     /setchannel main で設定した「お知らせ用」チャンネルを優先し、
    #     未設定の場合は通生用チャンネル（remind_channel_id）にフォールバックする
    if guild_id:
        try:
            guild_id_int = int(guild_id)
            guild = bot.get_guild(guild_id_int)
            if guild:
                config = load_config(guild_id_int)
                channel_id = config.get("notice_channel_id") or config.get("remind_channel_id")
                channel = bot.get_channel(channel_id) if channel_id else None
                if channel:
                    action = "更新" if is_update else "公開"
                    msg = f"📢 お知らせ「{filename}」が{uploader}さんによって{action}されました！"
                    asyncio.run_coroutine_threadsafe(
                        channel.send(msg), bot.loop
                    ).result(timeout=10)
        except Exception as e:
            print(f"[WARN] upload_notice notify failed: {e}")

    change = file_diff(f"{NOTICES_DIR}/{filename}", old_content, content)
    detail = [c for c in (change, meta_change) if c]
    log_event(
        "notice",
        f"お知らせ「{filename}」を{'更新' if is_update else '追加'}しました。",
        actor=uploader,
        detail=detail if detail else None,
    )
    return jsonify({"ok": True, "filename": filename, "is_update": is_update, "uploader": uploader})


def _notice_owner(filename):
    """お知らせの投稿者 (uploader_id, uploader_nickname) を返す。
    記録が無い（作成者確認機能より前に投稿された古いお知らせ等）場合は
    (None, None)＝作成者不明として従来通り誰でも削除できる扱いにする。"""
    meta, _ = load_notices_meta()
    entry = meta.get(filename) or {}
    return entry.get("uploader_id"), entry.get("uploader")

def _delete_notice_file(filename, actor_nickname, approval_note=None):
    """お知らせファイル削除の実処理（本人による直接削除・削除依頼の承認の
    どちらからも呼ばれる共通処理）。作成者チェックは呼び出し側の責務。"""
    path = _data_path(f"{NOTICES_DIR}/{filename}")
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "ファイルが見つかりません"})
    # ★ 削除前に内容を読んでおく（運用ログの詳細表示用）。読めなくても削除は続行する。
    try:
        with open(path, "r", encoding="utf-8") as f:
            deleted_content = f.read()
    except OSError:
        deleted_content = None
    try:
        os.remove(path)
    except OSError as e:
        return jsonify({
            "ok": False,
            "error": f"local_delete_failed: {e}"
        })

    # メタ情報からも削除
    meta_change = None
    try:
        meta, meta_sha = load_notices_meta()
        if filename in meta:
            old_meta_entry = meta[filename]
            del meta[filename]
            save_notices_meta(meta, meta_sha)
            meta_change = _notice_meta_entry_diff(filename, old_meta_entry, None)
    except DataWriteError as e:
        print(f"[WARN] notices_meta からの削除に失敗しました: {e}")

    change = file_diff(f"{NOTICES_DIR}/{filename}", deleted_content, None)
    detail = [c for c in (change, meta_change) if c]
    summary = f"お知らせ「{filename}」を削除しました。"
    if approval_note:
        summary += approval_note
    log_event("notice", summary, actor=actor_nickname, detail=detail if detail else None)
    return jsonify({"ok": True})

@app.route("/delete_notice", methods=["POST"])
def delete_notice():
    """お知らせファイルを削除する（メタ情報も合わせて削除）"""
    data = request.json or {}
    _guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    filename = data.get("filename", "")
    if not _is_safe_notice_filename(filename):
        return jsonify({"ok": False, "error": "invalid filename"})
    # ★ 追加：投稿者本人以外は直接削除できない。それ以外の人が削除したい
    #   場合は /request_delete で本人にDiscord確認を送る。
    owner_id, owner_nickname = _notice_owner(filename)
    if owner_id and str(owner_id) != str(_student_id):
        return jsonify({
            "ok": False,
            "error": "creator_approval_required",
            "owner_nickname": owner_nickname or "投稿者",
        })
    return _delete_notice_file(filename, nickname)


# ================================
#  Flask API — お知らせの「実行済み」（全員共有）
#  ─────────────────────────────
#  生徒ごとではなく、みんなで1つの状態を共有する
#  （誰か1人が実行済みにしたら、全員の一覧で下に移動・薄く表示される）。
#  既存の notices_meta.json（投稿者名など）に "done" フラグを
#  追加するだけで、新しい保存先は増やさない。
# ================================
@app.route("/set_notice_done", methods=["POST"])
def set_notice_done():
    """指定したお知らせの「実行済み」状態（true/false）を、全員共有で設定する。"""
    data = request.json or {}
    _guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    filename = data.get("filename")
    done = bool(data.get("done"))
    if not _is_safe_notice_filename(filename):
        return jsonify({"ok": False, "error": "invalid filename"})
    try:
        last_err = None
        for _ in range(4):
            meta, sha = load_notices_meta()
            old_meta_text = _notices_meta_text(meta)  # ★ 上書き前に控えておく
            entry = meta.get(filename, {})
            entry["done"] = done
            meta[filename] = entry
            try:
                save_notices_meta(meta, sha)
                change = file_diff(NOTICES_META_FILE, old_meta_text, _notices_meta_text(meta))
                log_event(
                    "notice",
                    f"お知らせ「{filename}」を{'実行済み' if done else '未実行'}にしました。",
                    actor=nickname,
                    detail=[change] if change else None,
                )
                return jsonify({"ok": True})
            except DataWriteError as e:
                last_err = e
                continue  # 他の端末の書き込みと競合した場合、最新を読み直してやり直す
        return jsonify({"ok": False, "error": f"local_write_failed: {last_err}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ================================
#  Flask API — カードのフォルダ（みんなで共有）
# ================================
FOLDERS_FILE = "folders.json"
MAX_FOLDER_DEPTH = 3

def load_card_folders():
    data, sha = local_get(FOLDERS_FILE)
    return (data or []), sha

def save_card_folders(folders, sha=None):
    if sha is None:
        _, sha = local_get(FOLDERS_FILE)
    local_put(FOLDERS_FILE, folders, sha)
    notify_change()  # ★ フォルダもguildをまたいで共有されるため全体に通知

def _folders_text(folders):
    """運用ログ用：folders.json（全フォルダ一覧）を { ... } のブロックの並びにする。"""
    folders = folders or []
    by_id = {f.get("id"): f.get("name") for f in folders}
    lines = []
    for f in folders:
        parent_id = f.get("parent_id")
        fields = [
            ("フォルダ名", f.get('name')),
            ("親フォルダ", by_id.get(parent_id, parent_id) if parent_id else "(なし・最上位)"),
        ]
        lines.extend(_json_block(fields))
    return "\n".join(lines)

def _folder_level(folders, folder_id):
    lvl = 0
    cur = next((f for f in folders if f["id"] == folder_id), None)
    while cur:
        lvl += 1
        cur = next((f for f in folders if f["id"] == cur.get("parent_id")), None)
    return lvl

def _folder_descendants(folders, folder_id):
    direct = [f for f in folders if f.get("parent_id") == folder_id]
    all_desc = list(direct)
    for f in direct:
        all_desc += _folder_descendants(folders, f["id"])
    return all_desc

def _max_level_in_subtree(folders, folder_id):
    desc = _folder_descendants(folders, folder_id)
    levels = [_folder_level(folders, folder_id)] + [_folder_level(folders, f["id"]) for f in desc]
    return max(levels)

def _can_move_folder_to(folders, folder_id, new_parent_id):
    if folder_id == new_parent_id:
        return False
    desc_ids = [f["id"] for f in _folder_descendants(folders, folder_id)]
    if new_parent_id and new_parent_id in desc_ids:
        return False
    old_level = _folder_level(folders, folder_id)
    new_level = _folder_level(folders, new_parent_id) + 1
    shift = new_level - old_level
    return (_max_level_in_subtree(folders, folder_id) + shift) <= MAX_FOLDER_DEPTH

def generate_folder_id():
    import string
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))

# ================================
#  ★ 「クイズ過去問」フォルダ（クイズ由来の4択アーカイブ用・システムフォルダ）
#  ─────────────────────────────
#  みんなでクイズ（Quiz.js）でホストが「自分で問題を作る」（deckからの自動生成
#  ではなく手入力のオリジナル4択）でクイズを作った瞬間、その問題セットを
#  CardMakerのこの固定フォルダの中にデッキとして自動保存する。
#  ・IDを固定にすることで、二重作成を防ぎ、どのルートからでも同じフォルダを指せる。
#  ・このフォルダ自体は名前変更・削除・移動を禁止する（save_folder/delete_folder側で
#    ガードする）。中身（サブフォルダ作成・デッキの移動・並び替え・編集）は
#    フォルダの中でなら自由だが、フォルダの外へ出すことは禁止する。
#  ・「このデッキが4択アーカイブかどうか」は専用フラグを持たせず、
#    folder_id がこのフォルダのスコープ内かどうかだけで判定する
#    （save_cardsのcard_payloadは固定6キーのため、任意のトップレベルフラグを
#    追加すると通常デッキの保存経路にも影響が及んでしまうのを避けるため）。
# ================================
QUIZ_ARCHIVE_FOLDER_ID   = "quiz_archive_root"
QUIZ_ARCHIVE_FOLDER_NAME = "クイズ過去問"

def _ensure_quiz_archive_folder():
    """「クイズ過去問」フォルダが無ければ作る（あれば何もしない）。"""
    folders, sha = load_card_folders()
    if not any(f.get("id") == QUIZ_ARCHIVE_FOLDER_ID for f in folders):
        folders.append({"id": QUIZ_ARCHIVE_FOLDER_ID, "name": QUIZ_ARCHIVE_FOLDER_NAME, "parent_id": None})
        save_card_folders(folders, sha)

def _is_in_archive_scope(folders, folder_id):
    """folder_id が「クイズ過去問」フォルダ自身、またはその子孫かどうか。"""
    if folder_id == QUIZ_ARCHIVE_FOLDER_ID:
        return True
    return any(f["id"] == folder_id for f in _folder_descendants(folders, QUIZ_ARCHIVE_FOLDER_ID))

@app.route("/list_folders", methods=["GET"])
def list_folders():
    try:
        folders, _ = load_card_folders()
        return jsonify({"ok": True, "folders": folders})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/save_folder", methods=["POST"])
def save_folder():
    """
    新規作成: { name, parent_id }
    改名／移動: { id, name, parent_id }
    """
    data      = request.json or {}
    _guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    folder_id = data.get("id")
    name      = (data.get("name") or "").strip()
    parent_id = data.get("parent_id")

    if not name:
        return jsonify({"ok": False, "error": "name は必須です"})

    err = reject_if_bug_chars({"フォルダ名": name})
    if err:
        return err

    try:
        folders, sha = load_card_folders()
        old_folders_text = _folders_text(folders)  # ★ 運用ログでファイル全体の差分を見せるため、変更前に控えておく

        if folder_id:
            target = next((f for f in folders if f["id"] == folder_id), None)
            if not target:
                return jsonify({"ok": False, "error": "folder not found"})
            # ★「クイズ過去問」フォルダ自身は改名・移動できないシステムフォルダ
            if folder_id == QUIZ_ARCHIVE_FOLDER_ID and (name != target.get("name") or parent_id != target.get("parent_id")):
                return jsonify({"ok": False, "error": "このフォルダは変更できません"})
            if parent_id != target.get("parent_id"):
                if not _can_move_folder_to(folders, folder_id, parent_id):
                    return jsonify({"ok": False, "error": "移動できません（3階層を超える、または循環参照）"})
                # ★「クイズ過去問」フォルダの中身は、その外へ移動できない
                if _is_in_archive_scope(folders, folder_id) and not _is_in_archive_scope(folders, parent_id):
                    return jsonify({"ok": False, "error": "クイズ過去問フォルダの外には移動できません"})
                target["parent_id"] = parent_id
            target["name"] = name
        else:
            if _folder_level(folders, parent_id) >= MAX_FOLDER_DEPTH:
                return jsonify({"ok": False, "error": f"フォルダは{MAX_FOLDER_DEPTH}階層までしか作成できません"})
            folder_id = generate_folder_id()
            folders.append({"id": folder_id, "name": name, "parent_id": parent_id})

        save_card_folders(folders, sha)
        change = file_diff(FOLDERS_FILE, old_folders_text, _folders_text(folders))
        log_event("card", f"フォルダ「{name}」を保存しました。", actor=nickname, detail=[change] if change else None)
        return jsonify({"ok": True, "id": folder_id})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/delete_folder", methods=["POST"])
def delete_folder():
    data      = request.json or {}
    _guild_id, _student_id, nickname, err = require_login_json(data)  # ★ 変更にはログイン必須
    if err:
        return err
    folder_id = data.get("id")
    if not folder_id:
        return jsonify({"ok": False, "error": "id は必須です"})
    # ★「クイズ過去問」フォルダ自身は削除できないシステムフォルダ
    if folder_id == QUIZ_ARCHIVE_FOLDER_ID:
        return jsonify({"ok": False, "error": "このフォルダは削除できません"})
    try:
        folders, sha = load_card_folders()
        # ★ 削除前に名前を控えておく（運用ログの詳細表示用。IDだけでは
        #   何のフォルダだったか分からないため）
        deleted_folder = next((f for f in folders if f["id"] == folder_id), None)
        desc_ids   = [f["id"] for f in _folder_descendants(folders, folder_id)]
        remove_ids = set([folder_id] + desc_ids)
        new_folders = [f for f in folders if f["id"] not in remove_ids]
        save_card_folders(new_folders, sha)

        # ★ 並び順（list_order.json）からも、削除したフォルダ自身のスコープと、
        #   他のフォルダ内に残っていた folder: キーの参照を取り除いておく
        cleanup_list_order(
            remove_keys=set(f"folder:{fid}" for fid in remove_ids),
            remove_scopes=remove_ids,
        )

        deleted_folder_name = (deleted_folder or {}).get("name")
        change = file_diff(FOLDERS_FILE, _folders_text(folders), _folders_text(new_folders))
        log_event(
            "card",
            f"フォルダ「{deleted_folder_name}」を削除しました。" if deleted_folder_name else "フォルダを削除しました。",
            actor=nickname,
            detail=[change] if change else None,
        )
        return jsonify({"ok": True, "deleted_ids": list(remove_ids)})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ================================
#  Flask API — 一覧（デッキ・フォルダ）の並び順（みんなで共有）
# ================================
#  ・フォルダを開いている場所（"__root__" またはフォルダid）ごとに、
#    その中でのフォルダ・公開済みデッキの並び順（data-keyの配列）を保存する。
#  ・未公開（各自の下書き）デッキは他人からは見えないデータなので、
#    ここには含めない（フロント側でも送らないようにフィルタしている）。
ORDER_FILE = "list_order.json"

def load_list_order():
    data, sha = local_get(ORDER_FILE)
    return (data or {}), sha

def save_list_order(order_map, sha=None):
    if sha is None:
        _, sha = local_get(ORDER_FILE)
    local_put(ORDER_FILE, order_map, sha)
    notify_change()  # ★ 並び順もguildをまたいで共有されるため全体に通知

def cleanup_list_order(remove_keys=None, remove_scopes=None):
    """
    フォルダ・デッキが削除された際に、list_order.json から
    もう存在しない項目のエントリを取り除いておく（放っておいても表示は壊れないが、
    ファイルが際限なく肥大化するのを防ぐための後片付け）。
    ・remove_keys:   各スコープの並び順配列から取り除く要素（例: {"folder:xxx", "deck:yyy.json"}）
    ・remove_scopes: まるごと削除するスコープ自体（フォルダそのものが削除された場合、
                     そのフォルダの中の並び順はもう意味がないのでスコープごと消す）
    ★ 並び順の掃除は本質的な機能ではない（古い項目が残っていても、フロント側の表示時に
       存在しないものとして自動的に無視されるだけ）ので、失敗しても警告に留め、
       呼び出し元の本来の削除処理自体は失敗させない。
    """
    remove_keys   = set(remove_keys or [])
    remove_scopes = set(remove_scopes or [])
    if not remove_keys and not remove_scopes:
        return
    try:
        order_map, sha = load_list_order()
        changed = False
        for scope in remove_scopes:
            if scope in order_map:
                del order_map[scope]
                changed = True
        if remove_keys:
            for scope, keys in list(order_map.items()):
                new_keys = [k for k in keys if k not in remove_keys]
                if len(new_keys) != len(keys):
                    order_map[scope] = new_keys
                    changed = True
        if changed:
            save_list_order(order_map, sha)
    except DataWriteError as e:
        print(f"[WARN] list_order のクリーンアップに失敗しました: {e}")
    except Exception as e:
        print(f"[WARN] list_order のクリーンアップ中に予期しないエラーが発生しました: {e}")

@app.route("/list_order", methods=["GET"])
def list_order():
    try:
        order_map, _ = load_list_order()
        return jsonify({"ok": True, "order": order_map})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/save_order", methods=["POST"])
def save_order():
    """
    body: { scope: "__root__" または フォルダid, keys: ["folder:xxx", "deck:yyy", ...] }
    指定したscope（フォルダの場所）の並び順だけを丸ごと置き換えて保存する。
    """
    data  = request.json or {}
    scope = data.get("scope")
    keys  = data.get("keys")
    if not scope or not isinstance(keys, list):
        return jsonify({"ok": False, "error": "scope と keys は必須です"})
    try:
        order_map, sha = load_list_order()
        order_map[scope] = keys
        save_list_order(order_map, sha)
        return jsonify({"ok": True})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ================================
#  Flask API — 学習データ（わからないマーク／続きから／完了記録）の
#              端末間共有
#  ─────────────────────────────
#  ・以前はブラウザの localStorage にしか保存しておらず、別の端末では
#    見えなかった。ログイン（student_id）に紐付けてサーバー（ローカルディスク）側
#    にも保存することで、同じアカウントでログインしていればどの端末からでも
#    同じ状態を見られるようにする。
#  ・student_id はタイマーAPI等と同様、クライアントの自己申告を信用せず、
#    必ず session_token から resolve_session() で解決したものを使う。
#  ・study_data_{guild_id}_{student_id}.json（生徒ごとに1ファイル）の中身:
#      {
#        "unsure":    { deck_id: [cardKey, ...] },
#        "progress":  { "deck:ID" または "folder:ID": {...} },
#        "completed": { "deck:ID" または "folder:ID": {...} },
#      }
#  ★ 全生徒分をまとめて1ファイルに保存する方式（study_data_{guild_id}.json）
#    も検討したが、「わからない」マークはカードをめくるたびに更新が
#    走りうるため、1ファイル共有だと生徒が増えるほどファイルへの書き込みが
#    競合（409）しやすくなる。そのため生徒ごとに別ファイルへ分け、
#    自分の学習データを保存するときは他の生徒の書き込みと衝突しないように
#    した（本人の複数端末からの同時書き込みだけがぶつかる可能性があり、
#    その場合は local_put() 内の409再試行ロジックでカバーする）。
# ================================
def _study_data_filename(guild_id: int, student_id: str) -> str:
    # ★ student_id はユーザー（先生）が自由な文字列で登録できてしまうため、
    #   そのままファイル名に使うと "/" 等でパスが変わってしまう恐れがある。
    #   英数字・ハイフン・アンダースコア以外は "_" に置き換えたうえで、
    #   末尾に短いハッシュを付けて衝突を防ぐ（安全性と可読性の両立）。
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(student_id))[:40]
    suffix = hashlib.sha1(str(student_id).encode()).hexdigest()[:8]
    return f"study_data_{guild_id}_{safe}_{suffix}.json"

def _empty_student_study_data():
    return {"unsure": {}, "progress": {}, "completed": {}, "seen": {}}

def load_student_study_data(guild_id: int, student_id: str):
    data, sha = local_get(_study_data_filename(guild_id, student_id))
    if not data:
        return _empty_student_study_data(), sha
    return {
        "unsure":    data.get("unsure", {}),
        "progress":  data.get("progress", {}),
        "completed": data.get("completed", {}),
        # ★ 追加：「わかる率」集計用に、実際に表示した（学習した）カードキーを
        #   デッキごとに記録しておく（"わからない"と違い、一度見たら外れない）。
        "seen":      data.get("seen", {}),
    }, sha

def save_student_study_data(guild_id: int, student_id: str, data: dict, sha=None):
    local_put(_study_data_filename(guild_id, student_id), data, sha)

# ★ 追加：読み込み→一部だけ書き換え→保存、を「保存直前に必ず最新内容を
#   読み直してから変更を適用する」形でやり直す安全な更新処理。
#   ─────────────────────────────────────────────
#   以前は各APIハンドラが「リクエストの最初に読み込んだ my_data」を
#   そのまま保存していたため、ほぼ同時に届いた別のリクエスト（例：
#   「わからない」マークの保存と、カード送りのたびに自動で走る
#   「続きから」進捗の保存）が競合すると、後から書き込んだ方が
#   前の変更を含まない古い中身でファイル全体を上書きしてしまい、
#   わからないマーク等の変更が消えてしまう不具合があった。
#   ここでは local_put が 409（sha衝突）を返したら、その都度
#   最新のデータを読み直して mutate_fn をもう一度適用してから
#   書き込み直す（＝変更そのものを失わない）ようにする。
def update_student_study_data(guild_id: int, student_id: str, mutate_fn, max_attempts: int = 4):
    """mutate_fn(my_data) は my_data を直接書き換える関数。
    保存に失敗（sha競合）した場合は最新データを読み直して再適用する。"""
    last_err = None
    for _ in range(max_attempts):
        my_data, sha = load_student_study_data(guild_id, student_id)
        mutate_fn(my_data)
        try:
            save_student_study_data(guild_id, student_id, my_data, sha)
            return
        except DataWriteError as e:
            last_err = e
            continue  # 最新のsha・内容を読み直してもう一度やり直す
    raise last_err or DataWriteError("保存に失敗しました（リトライ上限）")

@app.route("/get_study_data", methods=["GET"])
def get_study_data():
    """ログイン中の生徒の学習データ（わからない／続きから／完了記録）を返す。"""
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    guild_id = int(guild_id)
    student_id = resolve_session(request.args.get("session_token"), guild_id)
    if not student_id:
        return jsonify({"ok": False, "error": "not_logged_in"})

    my_data, _ = load_student_study_data(guild_id, student_id)
    return jsonify({"ok": True, "data": my_data})

@app.route("/save_unsure", methods=["POST"])
def save_unsure():
    """指定したデッキの「わからない」マーク（カードキーの配列）を丸ごと置き換える。
    ★ タイマーや下書き自動保存と同じ高頻度なUI状態なので、意図的に運用ログ
    （log_event）には出していない（カードをめくるたびに呼ばれるため）。"""
    guild_id, student_id, err = _timer_auth_from_json()
    if err:
        return err
    data     = request.json or {}
    deck_id  = data.get("deck_id")
    unsure   = data.get("unsure")
    if not deck_id or not isinstance(unsure, list):
        return jsonify({"ok": False, "error": "deck_id と unsure は必須です"})
    try:
        def _mutate(my_data):
            if unsure:
                my_data["unsure"][deck_id] = unsure
            else:
                my_data["unsure"].pop(deck_id, None)
        update_student_study_data(guild_id, student_id, _mutate)
        return jsonify({"ok": True})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/save_seen", methods=["POST"])
def save_seen():
    """指定したデッキで実際に表示した（学習した）カードキーの配列を丸ごと置き換える。
    「わかる率」（/deck_understanding）の分母（＝学習済みカード数）に使う。
    「わからない」と違い一度記録したカードキーが外れることはないので、
    クライアント側は毎回「今までに見た分＋今回のカード」の全体を送ってくる想定。"""
    guild_id, student_id, err = _timer_auth_from_json()
    if err:
        return err
    data    = request.json or {}
    deck_id = data.get("deck_id")
    seen    = data.get("seen")
    if not deck_id or not isinstance(seen, list):
        return jsonify({"ok": False, "error": "deck_id と seen は必須です"})
    try:
        def _mutate(my_data):
            if seen:
                my_data.setdefault("seen", {})[deck_id] = seen
            else:
                my_data.setdefault("seen", {}).pop(deck_id, None)
        update_student_study_data(guild_id, student_id, _mutate)
        return jsonify({"ok": True})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/deck_understanding", methods=["GET"])
def deck_understanding():
    """指定デッキ（複数可、カンマ区切り）について、自分だけでなく全生徒分を
    合算した「わかる率」を返す。
    ・「学習した（seen）カードのうち、今わからないマークが付いていない」割合。
    ・生徒ごとに別ファイル（study_data_{guild_id}_*.json）に分かれているため、
      対象ギルドの全ファイルを読んで合算する。ローカルディスクの読み込みなので
      生徒数が多くても軽い（GitHub API等を叩きに行くわけではない）。
    """
    guild_id = request.args.get("guild_id")
    filenames = request.args.get("filenames")  # カンマ区切りのdeck_id（=カードセットのfilename）
    if not guild_id or not filenames:
        return jsonify({"ok": False, "error": "missing guild_id or filenames"})
    guild_id = int(guild_id)
    if not resolve_session(request.args.get("session_token"), guild_id):
        return jsonify({"ok": False, "error": "not_logged_in"})
    target_filenames = [f for f in filenames.split(",") if f]
    if not target_filenames:
        return jsonify({"ok": False, "error": "missing filenames"})

    prefix = f"study_data_{guild_id}_"
    studied = 0
    understood = 0
    try:
        names = os.listdir(DATA_DIR)
    except OSError:
        names = []
    for name in names:
        if not (name.startswith(prefix) and name.endswith(".json")):
            continue
        data, _ = local_get(name)
        if not data:
            continue
        seen_map   = data.get("seen", {}) or {}
        unsure_map = data.get("unsure", {}) or {}
        for fn in target_filenames:
            seen_keys = seen_map.get(fn)
            if not seen_keys:
                continue
            unsure_keys = set(unsure_map.get(fn) or [])
            studied += len(seen_keys)
            understood += sum(1 for k in seen_keys if k not in unsure_keys)

    return jsonify({"ok": True, "studied": studied, "understood": understood})

@app.route("/save_study_progress", methods=["POST"])
def save_study_progress_api():
    """「続きから」の進捗を保存する。data に null を渡すとその項目を削除する。"""
    guild_id, student_id, err = _timer_auth_from_json()
    if err:
        return err
    data = request.json or {}
    key  = data.get("key")  # 例: "deck:xxxx" / "folder:xxxx"
    if not key:
        return jsonify({"ok": False, "error": "key は必須です"})
    progress_data = data.get("data")
    try:
        def _mutate(my_data):
            if progress_data is None:
                my_data["progress"].pop(key, None)
            else:
                my_data["progress"][key] = progress_data
        update_student_study_data(guild_id, student_id, _mutate)
        return jsonify({"ok": True})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/save_completion", methods=["POST"])
def save_completion_api():
    """学習の完了記録を保存する。"""
    guild_id, student_id, err = _timer_auth_from_json()
    if err:
        return err
    data = request.json or {}
    key  = data.get("key")  # 例: "deck:xxxx" / "folder:xxxx"
    completion_data = data.get("data")
    if not key or not completion_data:
        return jsonify({"ok": False, "error": "key と data は必須です"})
    try:
        def _mutate(my_data):
            my_data["completed"][key] = completion_data
        update_student_study_data(guild_id, student_id, _mutate)
        return jsonify({"ok": True})
    except DataWriteError as e:
        return jsonify({"ok": False, "error": f"local_write_failed: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ================================
#  ★ 追加：データの自動バックアップ（毎日0:00・GitHubのプライベートリポジトリへ）
#  ─────────────────────────────
#  DATA_DIR（実行時データ・生徒の個人情報を含む）はサーバーのローカル
#  ディスクにしか置いておらず、これまで自動バックアップが存在しなかった
#  （以前はGitHub Contents APIで直接読み書きしていたが、ローカルディスク
#  方式への移行でその経路ごと無くなっていた）。
#  ここでは「毎晩、DATA_DIRの中身をまるごと専用のプライベートリポジトリへ
#  git push する」だけのシンプルな仕組みを追加する。
#
#  ★ サーバー側で別途用意する必要があるもの：
#    ・git コマンドが使えること（コンテナ内に無ければ別途インストールが必要）
#    ・環境変数 BACKUP_GITHUB_TOKEN に、対象リポジトリへ push できる
#      GitHubのPersonal Access Token（対象リポジトリのContents:
#      Read and write 権限）を設定しておくこと
#  上記が揃っていない場合、バックアップは（Bot本体を止めずに）
#  スキップされ、理由だけがログに出力される。
#  ★ セキュリティ：トークンは .git/config 等のファイルに残さないよう、
#    リモートURLには埋め込まず、git実行時だけ一時的なHTTPヘッダーとして渡す。
# ================================
BACKUP_GITHUB_TOKEN = os.getenv("BACKUP_GITHUB_TOKEN")
BACKUP_REPO_URL = os.getenv(
    "BACKUP_REPO_URL", "https://github.com/yuichisana377/python.bot.1istudy-backup.git"
)
#   ★ 修正：以前はDATA_DIRの1つ上の階層を既定値にしていたが、DATA_DIRが
#     コンテナのアプリルート直下（例: /app）に設定されている環境だと、
#     その1つ上＝ファイルシステムのルート直下に作ろうとしてしまい、
#     権限エラー等で失敗する。DATA_DIRの場所に依存しない固定の既定値にする。
BACKUP_REPO_DIR = os.getenv("BACKUP_REPO_DIR", "/tmp/1istudy-backup-repo")

def _run_git(args, cwd, use_auth=False):
    cmd = ["git"]
    if use_auth:
        # ★ トークンをファイルに残さず、この1回のHTTPリクエストだけに使う
        #   ★ 修正：GitHubのgit HTTP認証は "bearer" スキームを受け付けず、
        #     Basic認証（x-access-token:<token> をbase64化）である必要がある。
        #     以前はbearerを使っていたため401→ユーザー名入力待ちで失敗していた。
        basic = base64.b64encode(f"x-access-token:{BACKUP_GITHUB_TOKEN}".encode()).decode()
        cmd += ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
    cmd += args
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, timeout=120)


def _redact_token(text):
    """ログにトークンを絶対に出さないための保険。万一 use_auth 付きコマンドが
    失敗して stderr や cmd の中に -c http.extraheader=... が含まれていても、
    トークン本体は必ず伏せ字にする。"""
    if not BACKUP_GITHUB_TOKEN:
        return text
    return text.replace(BACKUP_GITHUB_TOKEN, "***REDACTED***")

_BACKUP_CATEGORY_PREFIXES = [
    ("study_data_", "学習データ"),
    ("study_logs_", "学習ログ"),
    ("study_timers_", "学習タイマー"),
    ("points_", "ポイント"),
    ("users_", "ユーザー情報"),
    ("completed_tasks_", "達成課題"),
    ("config_", "設定"),
    ("discord_login_links_", "Discordログイン連携"),
    ("discord_links_", "Discordリンク"),
    ("timetable_", "時間割"),
    ("cards_index", "カード"),
    ("folders", "フォルダ"),
    ("list_order", "表示順"),
    ("notices_meta", "お知らせ"),
    ("in_progress_decks", "学習中デッキ"),
    ("plans", "予定"),
    ("logs_", "ログ"),
    ("system_log", "システムログ"),
]

def _backup_category(rel_path):
    """変更されたファイルのパスから、個人を特定できるID等を含まない
    大まかな種別ラベルへ変換する（生徒個人が特定できるファイル名を
    そのままログに出さないため）。"""
    name = os.path.basename(rel_path)
    if rel_path.startswith("notices/") or rel_path.startswith("notices\\"):
        return "お知らせ（ファイル）"
    if rel_path.startswith("words/") or rel_path.startswith("words\\"):
        return "単語セット"
    for prefix, label in _BACKUP_CATEGORY_PREFIXES:
        if name.startswith(prefix):
            return label
    return "その他データ"

def _backup_status_files(porcelain_output, max_files=30):
    """`git status --porcelain`の出力を、運用ログ用の「ファイルごと」の
    変更リストに変換する。
    ★ 注意：他のカテゴリ（カードデッキ・お知らせ等）とは異なり、ここでは
    実際のファイル名の代わりに `_backup_category()` の大まかな種別ラベルを
    使う。バックアップ対象には生徒ごとの学習データファイル
    （例: study_data_<guild_id>_<学籍番号>_*.json）が含まれており、実ファイル名を
    そのままログに出すと生徒個人が特定できてしまうため（_summarize_backup_changes
    と同じ配慮）。"""
    files = []
    for line in (porcelain_output or "").splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip()
        rel = line[3:].strip().split(" -> ")[-1]
        if rel.startswith("data/"):
            rel = rel[len("data/"):]
        label = _backup_category(rel)
        action = "削除" if code.upper() == "D" else ("新規追加" if code in ("??", "A") else "更新")
        sign = "-" if action == "削除" else "+"
        status = {"削除": "deleted", "新規追加": "added", "更新": "modified"}[action]
        files.append({"file": label, "diff": f"{sign} {action}", "status": status})
    if len(files) > max_files:
        files = files[:max_files] + [{"file": None, "diff": f"…ほか{len(files) - max_files}件"}]
    return files

def _summarize_backup_changes(porcelain_output):
    """`git status --porcelain`の出力から、種別ごとの変更件数を
    「学習データ2件、ポイント1件」のような短い日本語にまとめる。"""
    counts = {}
    for line in porcelain_output.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip().split(" -> ")[-1]
        if rel.startswith("data/"):
            rel = rel[len("data/"):]
        label = _backup_category(rel)
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    return "、".join(f"{label}{n}件" for label, n in counts.items())

def backup_data_to_github():
    """DATA_DIRの中身をまるごとバックアップ用リポジトリへコミット・pushする。
    失敗してもBot本体を止めないよう、例外は外に投げずログだけ出す。"""
    if not BACKUP_GITHUB_TOKEN:
        print("[backup] 環境変数 BACKUP_GITHUB_TOKEN が未設定のため、自動バックアップをスキップしました。")
        return
    if shutil.which("git") is None:
        print("[backup] git コマンドが見つからないため、自動バックアップをスキップしました。")
        return

    try:
        # 1) バックアップ用ローカルクローンを用意する（無ければclone、あれば最新に揃える）
        if not os.path.isdir(os.path.join(BACKUP_REPO_DIR, ".git")):
            parent = os.path.dirname(BACKUP_REPO_DIR)
            if parent:
                os.makedirs(parent, exist_ok=True)
            _run_git(["clone", BACKUP_REPO_URL, BACKUP_REPO_DIR], cwd=".", use_auth=True)
        else:
            _run_git(["fetch", "origin", "main"], cwd=BACKUP_REPO_DIR, use_auth=True)
            _run_git(["reset", "--hard", "origin/main"], cwd=BACKUP_REPO_DIR)

        # 2) data/ 以下を「実データだけ」で置き換える
        #    ★ 修正：環境によってはDATA_DIRがコード本体と同じディレクトリ
        #    （例: docker-composeで "./:/app" をマウントし、DATA_DIR=/app）
        #    になっている場合があり、DATA_DIR全体を丸ごとコピーすると
        #    bot.py・.git・.env（秘密情報）まで巻き込んでしまう。
        #    このリポジトリの .gitignore が「データ」とみなしている範囲
        #    （直下の*.jsonファイル、notices/・words/サブディレクトリ）
        #    だけを選んでコピーする。
        #    （shutil.rmtree→再作成にすることで、削除されたファイルも反映される）
        dest = os.path.join(BACKUP_REPO_DIR, "data")
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(DATA_DIR):
            src = os.path.join(DATA_DIR, name)
            if os.path.isfile(src) and name.endswith(".json"):
                shutil.copy2(src, os.path.join(dest, name))
        for sub in ("notices", "words"):
            src_sub = os.path.join(DATA_DIR, sub)
            if os.path.isdir(src_sub):
                shutil.copytree(src_sub, os.path.join(dest, sub))

        # 3) 前回から変化が無ければコミットしない（空コミットの量産を防ぐ）
        status = _run_git(["status", "--porcelain"], cwd=BACKUP_REPO_DIR)
        if not status.stdout.strip():
            print("[backup] 前回から変更が無いため、コミットはスキップしました。")
            return

        change_summary = _summarize_backup_changes(status.stdout)

        timestamp = datetime.now(timezone("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        _run_git(["add", "-A"], cwd=BACKUP_REPO_DIR)
        _run_git(
            ["-c", "user.email=backup@1istudy.local", "-c", "user.name=1istudy-backup",
             "commit", "-m", f"backup {timestamp}"],
            cwd=BACKUP_REPO_DIR,
        )
        _run_git(["push", "origin", "HEAD:main"], cwd=BACKUP_REPO_DIR, use_auth=True)
        print(f"[backup] {timestamp} のバックアップをpushしました。")
        log_event(
            "backup",
            f"データをバックアップしました（{change_summary}）" if change_summary else "データをバックアップしました。",
            detail=_backup_status_files(status.stdout),
        )
    except subprocess.CalledProcessError as e:
        # ★ 修正：e.cmd には use_auth=True 時の -c http.extraheader=...（トークン本体）
        #   がそのまま含まれるため、ログに出す前に必ず伏せ字にする。
        safe_cmd = _redact_token(" ".join(e.cmd))
        safe_stderr = _redact_token(e.stderr or "")
        print(f"[backup] 失敗しました（{safe_cmd}）: {safe_stderr}")
        log_event("backup", f"バックアップに失敗しました: {safe_stderr[:200]}", level="error", detail=f"{safe_cmd}\n{safe_stderr}")
    except Exception as e:
        safe_msg = _redact_token(str(e))
        print(f"[backup] 失敗しました: {safe_msg}")
        log_event("backup", f"バックアップに失敗しました: {safe_msg[:200]}", level="error", detail=safe_msg)

async def scheduled_backup_data_to_github():
    """★ backup_data_to_github() はブロッキングI/O（subprocess・ファイルコピー）
    を含む同期関数なので、asyncioのイベントループ（Discordの通信もここで
    動いている）を止めないよう、別スレッドで実行する。
    他のバックグラウンドジョブ（async_local_get等）と同じ考え方。"""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, backup_data_to_github)

# ================================
#  スケジューラー & 起動
# ================================
scheduler.add_job(scheduled_backup_data_to_github, "cron", hour=0, minute=0)  # ★ 追加：毎日0:00（JST）にデータを自動バックアップ
scheduler.add_job(send_tomorrow_plans,     "cron", hour=20, minute=0)
scheduler.add_job(send_today_plans_commute, "cron", hour=5,  minute=30)  # 通生（現行時間）
scheduler.add_job(send_today_plans_dorm,    "cron", hour=7,  minute=20)  # 寮生
scheduler.add_job(send_weekly_plans,        "cron", day_of_week="sun", hour=14, minute=0)  # 毎週日曜14:00に今週の予定
scheduler.add_job(check_study_timers,       "interval", minutes=1)  # ★ 勉強タイマーの3時間ごとの自動休憩チェック（最大1分遅れで検知）

started = False
synced_once = False

@bot.event
async def on_ready():
    global started, synced_once
    print(f"Bot is ready! {bot.user}")

    # ★ 429対策①：コマンド同期は起動後1回だけ行う。
    #    再接続（resume失敗などで on_ready が複数回呼ばれるケース）のたびに
    #    tree.sync() を叩くと、それ自体がAPI呼び出しの積み重ねになり
    #    レート制限を誘発しやすくなるため、初回のみに限定する。
    if not synced_once:
        try:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} commands")
        except discord.HTTPException as e:
            print(f"[WARN] tree.sync failed (will not retry until next process start): {e}")
        synced_once = True

    if not started:
        scheduler.start()
        started = True
        print("Scheduler started!")


@bot.event
async def on_disconnect():
    print("[WARN] Discord からの接続が切断されました。discord.py 内部で自動再接続を試みます。")


keep_alive()
start_quiz_scheduler()

print(f"[INFO] TOKEN set: {bool(TOKEN)}, length: {len(TOKEN) if TOKEN else 0}")
print(f"[INFO] Starting bot.run()...")

# ================================
#  ★ 429対策：プロセスを終了させず、同じプロセス内で
#     待ってから再ログインするループに変更
#
#  ・discord.py はゲートウェイ切断からの再接続（resume/reconnect）は
#    それ自体で自動的にバックオフしながら処理してくれる。
#    問題になりやすいのは「プロセスごと落ちて、ホスティング側
#    （Render等）がすぐ再起動 → 起動のたびに新規IDENTIFY」を繰り返すケースで、
#    これが Cloudflare 側の 429（1015 Too Many Requests）を招きやすい。
#  ・そのため、bot.run() が例外で終了しても "プロセスを終了させず"、
#    ここで指数バックオフしながら bot.run() をやり直す。
#  ・429（discord.HTTPException, status==429）の場合は
#    レスポンスの retry_after 秒だけ確実に待ってから再試行する。
# ================================
MAX_BACKOFF = 300  # 最大5分待機

def run_bot_forever():
    backoff = 5
    while True:
        try:
            # bot.run() は内部で asyncio.run() を呼ぶため、
            # ここが正常終了/例外終了するたびにイベントループは閉じられる。
            # discord.py の Client は close 後も再度 run() できる設計になっている。
            bot.run(TOKEN)
            # bot.run() が例外を投げずに戻ってきた場合（bot.close()等による正常終了）
            print("[INFO] bot.run() が正常終了しました。5秒後に再起動します。")
            time.sleep(5)
            backoff = 5
            continue

        except discord.HTTPException as e:
            # 429（レート制限）はここで最優先に処理する
            if e.status == 429:
                retry_after = None
                try:
                    retry_after = float(e.response.headers.get("Retry-After"))
                except Exception:
                    pass
                if not retry_after:
                    retry_after = backoff
                print(f"[WARN] Discordからレート制限(429)を受けました。{retry_after:.1f}秒待機して再接続します。")
                time.sleep(retry_after + 1)
                backoff = min(backoff * 2, MAX_BACKOFF)
            else:
                print(f"[ERROR] discord.HTTPException: {e}. {backoff}秒後に再試行します。")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

        except discord.LoginFailure as e:
            # トークンが無効な場合はリトライしても無駄なので停止する
            print(f"[FATAL] ログインに失敗しました。TOKENを確認してください: {e}")
            break

        except Exception as e:
            # ネットワーク瞬断やその他予期しない例外はプロセスを落とさず再試行
            print(f"[ERROR] 予期しないエラーが発生しました: {e}. {backoff}秒後に再試行します。")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


run_bot_forever()
