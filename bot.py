import asyncio
import discord
from discord.ext import commands, tasks
import yt_dlp
import os
import logging
import traceback
import sys
import json
import requests
import aiohttp
import random
import subprocess
import platform
import psutil
from datetime import datetime
from dotenv import load_dotenv

# =====================================================================
# 1. システムログ設定
# =====================================================================
load_dotenv()
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
logger.addHandler(handler)

# トレースバックのパスからユーザー名を隠す
def _masked_excepthook(exc_type, exc_value, exc_tb):
    import getpass, re
    lines = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        username = getpass.getuser()
        lines = lines.replace(username, "****")
    except Exception:
        pass
    # Windowsパス C:\Users\****\ の形も念のため正規表現でマスク
    lines = re.sub(r'(C:\\Users\\)[^\\]+', r'\1****', lines)
    sys.stderr.write(lines)
sys.excepthook = _masked_excepthook

# =====================================================================
# 2. Bot初期化 & インテント（権限）設定
# =====================================================================
TOKEN = os.environ.get("DISCORD_TOKEN", "")
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8000")
API_KEY = os.environ.get("API_KEY", "")
ERROR_WEBHOOK_URL = os.environ.get("ERROR_WEBHOOK_URL", "")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True          # メンバー情報取得（Webhook復元のアバターURLなど）
intents.moderation = True       # 監査ログ取得（削除者の特定）

bot = commands.Bot(command_prefix='!', intents=intents)





# =====================================================================
# エラーハンドリング
# =====================================================================
async def send_error_to_webhook(error_message, traceback_str=None):
    """エラーをDiscord Webhookに送信"""
    if not ERROR_WEBHOOK_URL:
        return
    
    try:
        embed = {
            "title": "❌ Botエラー",
            "description": error_message,
            "color": 0xff0000,
            "timestamp": datetime.now().isoformat(),
        }
        
        if traceback_str:
            embed["fields"] = [
                {
                    "name": "Traceback",
                    "value": f"```\n{traceback_str[:1000]}\n```",
                    "inline": False
                }
            ]
        
        data = {"embeds": [embed]}
        requests.post(ERROR_WEBHOOK_URL, json=data)
    except Exception as e:
        logger.error(f"Failed to send error to webhook: {e}")

# =====================================================================
# 3. グローバル変数
# =====================================================================
config = {}
config_update_interval = 60  # config更新間隔（秒）

# =====================================================================
# 4. Config管理クラス
# =====================================================================
class ConfigManager:
    def __init__(self, server_url, api_key):
        # SERVER_URLが末尾に?や/を含む場合があるので正規化する
        # 例: https://example.com/tool/bot/api/config.php? → そのまま使う
        self.server_url = server_url.rstrip('?').rstrip('/')
        self.api_key = api_key
        self.config = {}
        self.last_update = None
    
    async def load_config(self):
        """サーバーからconfigを読み込む"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"X-API-Key": self.api_key}
                url = self.server_url
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        # エンコーディング問題対策: バイト列で取得してからデコード
                        raw = await response.read()
                        # UTF-8 → Shift-JIS → CP932 の順で試みる
                        decoded = None
                        for enc in ("utf-8", "utf-8-sig", "shift_jis", "cp932", "euc-jp", "latin-1"):
                            try:
                                decoded = raw.decode(enc)
                                break
                            except (UnicodeDecodeError, LookupError):
                                continue
                        if decoded is None:
                            logger.error("Config: 文字コードの判定に失敗しました")
                            return False
                        self.config = json.loads(decoded)
                        self.last_update = datetime.now()
                        logger.info("Config loaded successfully from server")
                        return True
                    else:
                        text = await response.text()
                        logger.error(f"Failed to load config: HTTP {response.status} / {text[:200]}")
                        return False
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            return False
    
    async def save_config(self):
        """サーバーにconfigを保存する"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
                url = self.server_url
                async with session.post(url, headers=headers, json=self.config) as response:
                    if response.status == 200:
                        logger.info("Config saved successfully to server")
                        return True
                    else:
                        text = await response.text()
                        logger.error(f"Failed to save config: HTTP {response.status} / {text[:200]}")
                        return False
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            return False
    
    def get(self, key_path, default=None):
        """configから値を取得（ドット区切りのパス対応）"""
        keys = key_path.split('.')
        value = self.config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value
    
    def set(self, key_path, value):
        """configに値を設定（ドット区切りのパス対応）"""
        keys = key_path.split('.')
        config_dict = self.config
        for key in keys[:-1]:
            if key not in config_dict:
                config_dict[key] = {}
            config_dict = config_dict[key]
        config_dict[keys[-1]] = value

config_manager = ConfigManager(SERVER_URL, API_KEY)

# =====================================================================
# 5. TTS管理クラス（Voicevox）
# =====================================================================
class TTSManager:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.voicevox_api_url = ""
        self.api_keys = []
        self.current_api_key_index = 0
        self.speaker = 0
        self.speed = 1.0
        self.pitch = 0.0
        self.intonation_scale = 1.0
    
    async def update_settings(self):
        """configからTTS設定を更新"""
        self.voicevox_api_url = self.config_manager.get("tts.voicevox_api_url", "https://deprecatedapis.tts.quest/v2/voicevox/audio/")
        self.api_keys = self.config_manager.get("tts.voicevox_api_keys", [])
        self.speaker = self.config_manager.get("tts.speaker", 0)
        self.speed = self.config_manager.get("tts.speed", 1.0)
        self.pitch = self.config_manager.get("tts.pitch", 0.0)
        self.intonation_scale = self.config_manager.get("tts.intonation_scale", 1.0)
    
    def get_next_api_key(self):
        """次のAPIキーを取得（ローテーション）"""
        if not self.api_keys:
            return None
        key = self.api_keys[self.current_api_key_index]
        self.current_api_key_index = (self.current_api_key_index + 1) % len(self.api_keys)
        return key
    
    async def synthesize(self, text):
        """テキストから音声を合成"""
        if not self.config_manager.get("tts.enabled", False):
            return None
        
        try:
            # 辞書置換
            dictionary = self.config_manager.get("tts.dictionary", {})
            for word, replacement in dictionary.items():
                text = text.replace(word, replacement)
            
            # テキストが空なら無視
            text = text.strip()
            if not text:
                return None

            async with aiohttp.ClientSession() as session:
                for _ in range(len(self.api_keys) if self.api_keys else 1):
                    api_key = self.get_next_api_key()
                    if not api_key:
                        logger.error("No Voicevox API key configured")
                        return None
                    
                    # ★修正: GETパラメータではなくPOSTフォームデータで送信
                    # GETのURLパラメータに日本語・長文を入れるとAPIが400を返す
                    form_data = aiohttp.FormData()
                    form_data.add_field("text", text)
                    form_data.add_field("key", api_key)
                    form_data.add_field("speaker", str(self.speaker))
                    form_data.add_field("pitch", str(self.pitch))
                    form_data.add_field("intonationScale", str(self.intonation_scale))
                    form_data.add_field("speed", str(self.speed))

                    async with session.post(self.voicevox_api_url, data=form_data) as response:
                        if response.status == 200:
                            audio_data = await response.read()
                            # 最低限の音声データが返っているか確認
                            if len(audio_data) < 100:
                                logger.error(f"TTS: 音声データが短すぎます ({len(audio_data)} bytes)")
                                return None
                            temp_file = f"temp_tts_{datetime.now().timestamp()}.wav"
                            with open(temp_file, "wb") as f:
                                f.write(audio_data)
                            return temp_file
                        elif response.status == 400:
                            error_text = await response.text()
                            if "invalidApiKey" in error_text or "notEnoughPoints" in error_text:
                                logger.warning(f"API key failed: {error_text}, trying next key")
                                continue
                            else:
                                logger.error(f"Voicevox API 400 error: {error_text}")
                                return None
                        elif response.status == 405:
                            # POSTが405ならGETにフォールバック（旧APIへの対応）
                            logger.warning("POST 405、GETにフォールバック中...")
                            params = {
                                "text": text, "key": api_key,
                                "speaker": self.speaker, "pitch": self.pitch,
                                "intonationScale": self.intonation_scale, "speed": self.speed
                            }
                            async with session.get(self.voicevox_api_url, params=params) as r2:
                                if r2.status == 200:
                                    audio_data = await r2.read()
                                    temp_file = f"temp_tts_{datetime.now().timestamp()}.wav"
                                    with open(temp_file, "wb") as f:
                                        f.write(audio_data)
                                    return temp_file
                                else:
                                    logger.error(f"Voicevox GET fallback failed: HTTP {r2.status}")
                                    return None
                        else:
                            logger.error(f"Voicevox API failed: HTTP {response.status}")
                            return None
                
                logger.error("All Voicevox API keys failed")
                return None
        except Exception as e:
            logger.error(f"TTS synthesis error: {e}")
            return None
    
    async def add_dictionary(self, word, replacement):
        """辞書に単語を追加"""
        dictionary = self.config_manager.get("tts.dictionary", {})
        dictionary[word] = replacement
        self.config_manager.set("tts.dictionary", dictionary)
        await config_manager.save_config()
        return True
    
    async def get_api_points(self):
        """残りAPIポイントを確認"""
        if not self.api_keys:
            return None
        
        try:
            async with aiohttp.ClientSession() as session:
                points_info = []
                for i, api_key in enumerate(self.api_keys):
                    params = {"key": api_key}
                    async with session.get("https://deprecatedapis.tts.quest/v2/api/", params=params) as response:
                        if response.status == 200:
                            data = await response.json()
                            points_info.append(f"API Key {i+1}: {data.get('points', 'Unknown')} ポイント")
                        else:
                            points_info.append(f"API Key {i+1}: 取得失敗")
                return "\n".join(points_info)
        except Exception as e:
            logger.error(f"Error getting API points: {e}")
            return None

tts_manager = TTSManager(config_manager)


# =====================================================================
# TTSキュー管理クラス
# =====================================================================
class TTSQueue:
    def __init__(self):
        self.queue = []
        self.is_processing = False
    
    def add(self, text, vc, user_settings):
        """TTSキューに追加（追加と同時に合成を開始して遅延を削減）"""
        item = {
            'text': text,
            'vc': vc,
            'user_settings': user_settings,
        }
        # キューが空（= 処理中でない）なら、追加した瞬間に合成タスクを起動
        if not self.is_processing:
            item['_audio_task'] = asyncio.create_task(tts_manager.synthesize(text))
        self.queue.append(item)
        if not self.is_processing:
            asyncio.create_task(self.process_queue())
    
    async def process_queue(self):
        """TTSキューを処理（先読み合成で遅延を最小化）"""
        self.is_processing = True
        # 最初のアイテムの音声合成を即開始
        if self.queue:
            first_item = self.queue[0]
            first_item['_audio_task'] = asyncio.create_task(tts_manager.synthesize(first_item['text']))

        while self.queue:
            item = self.queue.pop(0)
            text = item['text']
            vc = item['vc']
            user_settings = item['user_settings'] or {}
            
            # ユーザー設定を一時的に適用
            original_speaker = tts_manager.speaker
            original_speed = tts_manager.speed
            original_pitch = tts_manager.pitch
            original_intonation = tts_manager.intonation_scale
            
            if 'speaker' in user_settings:
                tts_manager.speaker = user_settings['speaker']
            if 'speed' in user_settings:
                tts_manager.speed = user_settings['speed']
            if 'pitch' in user_settings:
                tts_manager.pitch = user_settings['pitch']
            if 'intonation_scale' in user_settings:
                tts_manager.intonation_scale = user_settings['intonation_scale']
            
            # 次のアイテムの合成を今すぐ並列で開始（待たずに）
            if self.queue:
                next_item = self.queue[0]
                if '_audio_task' not in next_item:
                    next_item['_audio_task'] = asyncio.create_task(
                        tts_manager.synthesize(next_item['text'])
                    )

            # 現在アイテムの音声を取得（既にタスクが走っていればawaitするだけ）
            if '_audio_task' in item:
                audio_file = await item['_audio_task']
            elif '_cached_audio' in item:
                audio_file = item['_cached_audio']
            else:
                audio_file = await tts_manager.synthesize(text)
            
            # 設定を元に戻す
            tts_manager.speaker = original_speaker
            tts_manager.speed = original_speed
            tts_manager.pitch = original_pitch
            tts_manager.intonation_scale = original_intonation
            
            if audio_file and isinstance(audio_file, str) and vc and vc.is_connected():
                music_vc = music_state.get('voice_client')
                music_was_playing = (
                    music_vc and music_vc.is_connected() and music_vc.is_playing()
                    and not music_state.get('is_tts_playing', False)
                )

                # TTS開始前に音楽の再開情報を記録
                if music_was_playing and music_state.get('current'):
                    # 再生開始時刻を music_state に記録しておく（play_next呼び出し時にセット）
                    import time
                    started_at = music_state.get('_play_started_at', time.time())
                    elapsed = time.time() - started_at
                    music_state['tts_resume_info'] = {
                        'url': music_state['current']['url'],
                        'seek_sec': elapsed,
                        'volume': music_state.get('original_volume', 0.5),
                        'loop': music_state.get('loop', False),
                        'track': music_state['current'],
                    }
                    print(f"[TTS DEBUG] 再開情報を記録: {music_state['current'].get('title')} / 経過={elapsed:.1f}秒")
                else:
                    music_state['tts_resume_info'] = None

                # TTS再生開始フラグを立てる
                music_state['is_tts_playing'] = True

                # 音楽が再生中なら停止（同一VCなのでpauseではなくstop、resume情報は上で保存済み）
                if music_was_playing and music_vc:
                    music_vc.stop()

                tts_finished = False

                def after_play(e):
                    nonlocal tts_finished
                    tts_finished = True
                    if os.path.exists(audio_file):
                        try:
                            os.remove(audio_file)
                        except Exception:
                            pass

                vc.play(discord.FFmpegPCMAudio(audio_file), after=after_play)
                # TTS再生が終わるまで待機
                while not tts_finished:
                    await asyncio.sleep(0.05)

                # TTS終了 → 音楽を再開
                music_state['is_tts_playing'] = False
                mv = music_state.get('voice_client')
                if mv and mv.is_connected():
                    resume_info = music_state.get('tts_resume_info')
                    if resume_info:
                        # 記録した位置から再生し直す
                        print(f"[TTS DEBUG] 位置から再開: seek={resume_info['seek_sec']:.1f}秒")
                        asyncio.create_task(_tts_resume_music(resume_info))
                    elif music_state['queue']:
                        asyncio.create_task(play_next())
            else:
                # 音声合成失敗時もフラグをリセット
                music_state['is_tts_playing'] = False

        self.is_processing = False

tts_queue = TTSQueue()

async def optimize_channel_bitrate(vc):
    """VCチャンネルのビットレートをサーバーブーストに応じた最大値に設定する"""
    try:
        channel = vc.channel
        guild = channel.guild
        
        # サーバーブーストレベルに応じた最大ビットレート
        boost_level = guild.premium_tier
        max_bitrates = {0: 96000, 1: 128000, 2: 256000, 3: 384000}
        max_bitrate = max_bitrates.get(boost_level, 96000)
        
        current_bitrate = channel.bitrate
        if current_bitrate < max_bitrate:
            await channel.edit(bitrate=max_bitrate)
            return f"🔊 ビットレートを最大値 **{max_bitrate // 1000}kbps** (Boostレベル{boost_level}) に最適化しました"
        else:
            return f"🔊 ビットレート: **{current_bitrate // 1000}kbps** (最大値を維持)"
    except discord.Forbidden:
        return "🔊 ビットレート最適化: スキップ（Bot にチャンネル編集権限がありません）"
    except Exception as e:
        return f"🔊 ビットレート最適化: スキップ（{e}）"

# =====================================================================
# 7. 音楽再生設定（main3.pyベース）
# =====================================================================
YTDL_OPTIONS = {
    'format': 'bestaudio/best', # シンプルに最高品質を指定
    'noplaylist': False,
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'default_search': 'ytsearch',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'opus', # Opusで抽出するとDiscordとの相性が最強
        'preferredquality': '160',
    }],
}

FFMPEG_OPTIONS = {
    'before_options': (
        '-reconnect 1 '
        '-reconnect_streamed 1 '
        '-reconnect_delay_max 5 '
        '-probesize 100M '
        '-analyzeduration 100M'
    ),
    'options': '-vn -f s16le -ar 48000 -ac 2'
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

# 音楽再生状態管理
music_state = {
    'loop': False,
    'shuffle': False,
    'queue': [],
    'current': None,
    'voice_client': None,
    'text_channel': None,
    'original_volume': 0.5,
    'is_tts_playing': False,
    'tts_resume_info': None   # TTS割り込み時の再開情報 {url, seek_sec, volume, loop}
}

# 寝落ち監視状態管理
sleep_monitors = {}  # {user_id: {'start_time': datetime, 'message_id': int, 'channel_id': int, 'guild_id': int}}

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_running_loop()
        print("[Bot INFO] 音源の解析を開始: " + str(url))
        
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False)),
                timeout=30.0
            )
        except asyncio.TimeoutError:
            raise RuntimeError("動画情報の取得がタイムアウトしました。もう一度試してください。")

        if 'entries' in data:
            data = data['entries'][0]

        filename = data['url']
        print("[Bot INFO] 最高音質ストリームの抽出に成功: " + str(data.get('title')))
        
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data)

async def _tts_resume_music(resume_info: dict):
    """TTS終了後に音楽を指定位置から再生し直す"""
    import time
    mv = music_state.get('voice_client')
    if not mv or not mv.is_connected():
        return
    music_state['tts_resume_info'] = None
    try:
        seek_sec = resume_info.get('seek_sec', 0)
        track = resume_info.get('track')
        original_url = track.get('url') if track else resume_info.get('url')
        volume = resume_info.get('volume', 0.5)

        # YouTubeストリームURLは時間で失効するので再取得する
        print(f"[TTS RESUME] URLを再取得中...")
        loop = asyncio.get_running_loop()
        fresh_data = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: ytdl.extract_info(original_url, download=False)),
            timeout=30.0
        )
        fresh_url = fresh_data.get('url', original_url)

        seek_opts = {
            'before_options': (
                f'-ss {seek_sec:.2f} '
                '-reconnect 1 '
                '-reconnect_streamed 1 '
                '-reconnect_delay_max 5 '
                '-probesize 100M '
                '-analyzeduration 100M'
            ),
            'options': '-vn -ar 48000 -ac 2'
        }
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(fresh_url, **seek_opts),
            volume=volume
        )
        music_state['current'] = track
        music_state['_play_started_at'] = time.time() - seek_sec

        def after_resume(e):
            if e:
                print(f"[TTS RESUME] 再生エラー: {e}")
            if not music_state.get('is_tts_playing'):
                asyncio.run_coroutine_threadsafe(play_next(), bot.loop)

        mv.play(source, after=after_resume)
        print(f"[TTS RESUME] {track.get('title', '?')} を {seek_sec:.1f}秒から再開しました")
    except Exception as e:
        print(f"[TTS RESUME] エラー、最初から再生し直します: {e}")
        # seekに失敗した場合はcurrentをキューの先頭に戻して最初から再生
        if resume_info.get('track'):
            music_state['queue'].insert(0, resume_info['track'])
            music_state['current'] = None
        await play_next()

async def play_next():
    """次の曲を再生"""
    import time
    print(f"[Bot INFO] play_next呼び出し / vc={music_state.get('voice_client')} / is_tts={music_state.get('is_tts_playing')} / queue={len(music_state.get('queue', []))}")
    if not music_state['voice_client'] or not music_state['voice_client'].is_connected():
        print("[Bot INFO] play_next: voice_clientなし/未接続のためreturn")
        return

    # TTS再生中は1秒ごとに終わるのを待つ
    while music_state.get('is_tts_playing', False):
        print("[Bot INFO] play_next: TTS再生中のため待機...")
        await asyncio.sleep(1.0)

    
    # ループが有効な場合
    if music_state['loop'] and music_state['current']:
        try:
            player = await YTDLSource.from_url(music_state['current']['url'], loop=bot.loop)
            default_volume = config_manager.get("music.default_volume", 50) / 100
            player.volume = default_volume
            
            def after_track(e):
                if e:
                    logger.error(f"Player error: {e}")
                asyncio.run_coroutine_threadsafe(play_next(), bot.loop)
            
            music_state['voice_client'].play(player, after=after_track)
            music_state['_play_started_at'] = time.time()
            
            # 音楽通知チャンネルを取得（設定されていない場合は元のチャンネルを使用）
            guild = music_state['voice_client'].guild
            music_notification_channel_id = config_manager.get("music.notification_channel")
            if music_notification_channel_id:
                notification_channel = guild.get_channel(int(music_notification_channel_id))
                if notification_channel:
                    music_state['text_channel'] = notification_channel
            

        except Exception as e:
            logger.error(f"Error looping track: {e}")
            await play_next()
        return
    
    # キューから次の曲を取得
    if music_state['queue']:
        if music_state['shuffle']:
            import random
            index = random.randint(0, len(music_state['queue']) - 1)
            track = music_state['queue'].pop(index)
        else:
            track = music_state['queue'].pop(0)
        
        music_state['current'] = track
        
        try:
            player = await YTDLSource.from_url(track['url'], loop=bot.loop)
            default_volume = config_manager.get("music.default_volume", 50) / 100
            player.volume = default_volume
            
            def after_track(e):
                if e:
                    logger.error(f"Player error: {e}")
                asyncio.run_coroutine_threadsafe(play_next(), bot.loop)
            
            music_state['voice_client'].play(player, after=after_track)
            music_state['_play_started_at'] = time.time()
            
            # TTS自動読み上げが有効な場合はアナウンス
            # 音楽通知チャンネルを取得（設定されていない場合は元のチャンネルを使用）
            music_notification_channel_id = config_manager.get("music.notification_channel")
            if music_notification_channel_id:
                notification_channel = guild.get_channel(int(music_notification_channel_id))
                if notification_channel:
                    music_state['text_channel'] = notification_channel
            
            if music_state['text_channel']:
                # 誰かがTTS自動読み上げを有効にしているかチェック
                tts_enabled = False
                for user_id, settings in config_manager.get("user_settings", {}).items():
                    if settings.get('auto_read', False):
                        tts_enabled = True
                        break
                
                if tts_enabled:
                    await music_state['text_channel'].send(f"🎵 `{player.title}`を再生開始します")
                else:
                    await music_state['text_channel'].send(f"🎵 再生開始: `{player.title}`")
        except Exception as e:
            logger.error(f"Error playing track: {e}")
            await play_next()
    else:
        # キューが空の場合
        music_state['current'] = None

# =====================================================================
# 7. ファイル出力管理クラス
# =====================================================================
class FileOutputManager:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.upload_dir = "uploads"
        if not os.path.exists(self.upload_dir):
            os.makedirs(self.upload_dir)
    
    # ----------------------------------------------------------------
    # 品質プリセット定義
    # ----------------------------------------------------------------
    AUDIO_PRESETS = {
        "best":   {"format": "bestaudio/best", "quality": "320", "codec": "mp3"},
        "high":   {"format": "bestaudio/best", "quality": "192", "codec": "mp3"},
        "medium": {"format": "bestaudio/best", "quality": "128", "codec": "mp3"},
        "low":    {"format": "bestaudio/best", "quality":  "96", "codec": "mp3"},
    }
    VIDEO_PRESETS = {
        "best":   "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "1080p":  "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
        "720p":   "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
        "480p":   "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
        "360p":   "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
    }

    async def download_and_upload(self, url_or_keyword,
                                   format_type="mp3",
                                   quality="best"):
        """
        URLまたはキーワードからファイルをダウンロードしてPHPサーバーにアップロード。
        format_type: "mp3" | "mp4"
        quality    : audio → "best"/"high"/"medium"/"low"
                     video → "best"/"1080p"/"720p"/"480p"/"360p"
        """
        if not self.config_manager.get("file_output.enabled", True):
            return None, None, "ファイル出力機能は無効です"

        try:
            # キーワード検索の場合は ytsearch: を付ける
            if not url_or_keyword.startswith(('http://', 'https://')):
                url_or_keyword = f"ytsearch:{url_or_keyword}"

            # -------- yt-dlp オプション構築 --------
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'outtmpl': os.path.join(self.upload_dir, '%(id)s.%(ext)s'),
                # メタデータ埋め込み
                'writethumbnail': False,
                'postprocessors': [],
            }

            if format_type == "mp3":
                preset = self.AUDIO_PRESETS.get(quality, self.AUDIO_PRESETS["best"])
                ydl_opts['format'] = preset['format']
                ydl_opts['postprocessors'] = [
                    {   # 音声抽出 → MP3変換
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': preset['quality'],
                    },
                    {   # ID3タグ埋め込み（タイトル・アーティスト・アルバム等）
                        'key': 'FFmpegMetadata',
                        'add_metadata': True,
                    },
                    {   # サムネをアルバムアートとして埋め込み
                        'key': 'EmbedThumbnail',
                    },
                ]
                ydl_opts['writethumbnail'] = True
            else:  # mp4
                fmt = self.VIDEO_PRESETS.get(quality, self.VIDEO_PRESETS["best"])
                ydl_opts['format'] = fmt
                ydl_opts['merge_output_format'] = 'mp4'
                ydl_opts['postprocessors'] = [
                    {   # メタデータ埋め込み
                        'key': 'FFmpegMetadata',
                        'add_metadata': True,
                    },
                ]

            loop = asyncio.get_running_loop()

            def do_download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url_or_keyword, download=True)
                    if info and 'entries' in info:
                        info = info['entries'][0]
                    # MP3変換後のファイル名を確定
                    filename = ydl.prepare_filename(info)
                    if format_type == "mp3":
                        filename = os.path.splitext(filename)[0] + ".mp3"
                    elif format_type == "mp4":
                        filename = os.path.splitext(filename)[0] + ".mp4"
                    meta = {
                        'title':    info.get('title', 'Unknown'),
                        'uploader': info.get('uploader', ''),
                        'duration': info.get('duration', 0),
                        'webpage':  info.get('webpage_url', ''),
                    }
                    return filename, meta

            filename, meta = await asyncio.wait_for(
                loop.run_in_executor(None, do_download),
                timeout=600.0
            )

            if not os.path.exists(filename):
                return None, None, "ファイルのダウンロードに失敗しました"

            filesize_mb = os.path.getsize(filename) / (1024 * 1024)

            # -------- PHPサーバーへアップロード --------
            upload_url = self.config_manager.get("file_output.upload_url", "")
            if not upload_url or upload_url == "http://localhost:8000/upload":
                base = SERVER_URL.rstrip('?').rstrip('/')
                if '/api/' in base:
                    upload_url = base.rsplit('/api/', 1)[0] + '/api/upload.php'
                else:
                    upload_url = base + '/api/upload.php'

            async with aiohttp.ClientSession() as session:
                with open(filename, 'rb') as f:
                    form = aiohttp.FormData()
                    form.add_field('file', f,
                                   filename=os.path.basename(filename),
                                   content_type='application/octet-stream')
                    form.add_field('title',    meta['title'])
                    form.add_field('uploader', meta['uploader'])
                    form.add_field('duration', str(meta['duration']))
                    form.add_field('webpage',  meta['webpage'])
                    form.add_field('format',   format_type)
                    form.add_field('quality',  quality)
                    headers = {"X-API-Key": API_KEY}
                    async with session.post(upload_url, data=form, headers=headers) as resp:
                        if resp.status == 200:
                            result = await resp.json(content_type=None)
                            download_url = result.get('download_url', result.get('url', ''))
                            preview_url  = result.get('preview_url', '')
                        else:
                            text_body = await resp.text()
                            logger.error(f"Upload failed: HTTP {resp.status} / {text_body[:200]}")
                            download_url = ''
                            preview_url  = ''

            # ローカルファイルを削除
            try:
                os.remove(filename)
            except Exception:
                pass
            # サムネファイルも削除
            for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                thumb = os.path.splitext(filename)[0] + ext
                if os.path.exists(thumb):
                    try:
                        os.remove(thumb)
                    except Exception:
                        pass

            if download_url:
                duration_str = f"{int(meta['duration'])//60}:{int(meta['duration'])%60:02d}" if meta['duration'] else "不明"
                summary = (
                    f"**{meta['title']}**\n"
                    f"アップロード者: {meta['uploader']}  |  長さ: {duration_str}  |  "
                    f"サイズ: {filesize_mb:.1f}MB  |  品質: {format_type.upper()} ({quality})"
                )
                return download_url, summary, "完了"
            else:
                return None, None, "サーバーへのアップロードに失敗しました（ダウンロード自体は成功）"

        except asyncio.TimeoutError:
            return None, None, "ダウンロードがタイムアウトしました（大きすぎるかも）"
        except Exception as e:
            logger.error(f"File output error: {e}")
            return None, None, f"エラー: {str(e)}"

file_output_manager = FileOutputManager(config_manager)

# =====================================================================
# 8. Botイベント
# =====================================================================
@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    # リマインダーは remind.py が load_extension で読み込まれる際に自動セットアップされる
    # remind.py の setup() が config_manager/logger を取得できるよう bot に属性セット
    bot.config_manager = config_manager
    bot.logger = logger
    
    # スラッシュコマンドの同期
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")
        
    # --- 起動時のキャッシュ読み込み ---
    

    print("==================================================")
    print("🤖 統合Discord Bot 起動完了: " + str(bot.user.name))
    print("==================================================")
    # 初回config読み込み
    success = await config_manager.load_config()
    # サーバーからconfig読み込みに失敗した場合はTTSをデフォルト有効にする
    if not success or not config_manager.config:
        logger.warning("Config読み込み失敗: TTS/機能をデフォルト有効で起動します")
        config_manager.config.setdefault("tts",         {"enabled": True})
        config_manager.config.setdefault("music",       {"enabled": True})
        config_manager.config.setdefault("file_output", {"enabled": True})
        config_manager.config.setdefault("omikuji",     {"enabled": True})
        config_manager.config.setdefault("random",      {"enabled": True})
    else:
        # 読み込み成功してもtts.enabledがNoneの場合はTrueにする
        if config_manager.get("tts.enabled") is None:
            config_manager.config.setdefault("tts", {})["enabled"] = True
    await tts_manager.update_settings()
    # 定期config更新タスク開始
    update_config_task.start()
    # 寝落ち監視タイムアウトチェックタスク開始
    check_sleep_timeout.start()
    # add_pyfile.pyを読み込んで、そこに書いてある全てのpyファイルを読み込む
    try:
        if os.path.exists("add_pyfile.py"):
            with open("add_pyfile.py", "r", encoding="utf-8") as f:
                lines = f.readlines()
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("#") and line.endswith(".py"):
                        try:
                            module_name = line[:-3]  # .pyを除去
                            await bot.load_extension(module_name)
                            print(f"✅ {module_name}.py を読み込みました")
                        except Exception as e:
                            print(f"⚠️ {line} の読み込みをスキップ: {e}")
        else:
            print("ℹ️ add_pyfile.py が存在しないため、追加コマンドファイルの読み込みをスキップ")
    except Exception as e:
        print(f"⚠️ add_pyfile.py の読み込みに失敗: {e}")
    # スラッシュコマンドを同期
    try:
        synced = await bot.tree.sync()
        print(f"📝 {len(synced)} 個のスラッシュコマンドを同期しました")
    except Exception as e:
        print(f"❌ スラッシュコマンドの同期に失敗しました: {e}")
        await send_error_to_webhook("スラッシュコマンドの同期に失敗しました", traceback.format_exc())

@bot.event
async def on_error(event, *args, **kwargs):
    """エラーが発生したときの処理"""
    error_info = traceback.format_exc()
    logger.error(f"Error in {event}: {error_info}")
    await send_error_to_webhook(f"エラーが発生しました: {event}", error_info)

@bot.event
async def on_voice_state_update(member, before, after):
    """ボイスチャンネルの入退室を検知してTTS再生"""
    # 寝落ち監視中のユーザーがVCから退室した場合、監視を終了
    user_id = str(member.id)
    if user_id in sleep_monitors:
        if before.channel is not None and after.channel is None:
            # VCから退室した場合
            monitor = sleep_monitors.pop(user_id)
            guild = bot.get_guild(monitor['guild_id'])
            if guild:
                channel = guild.get_channel(monitor['channel_id'])
                if channel:
                    await channel.send(f"✅ {member.display_name} さんがVCから退室したため、監視を終了しました")
            return
    
    if not config_manager.get("tts.enabled", False):
        return
    
    # Bot自身の入退室は無視
    if member.id == bot.user.id:
        return
    
    # ユーザーごとのTTS設定をチェック
    user_id = str(member.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    if user_settings is None:
        user_settings = {}
    
    # 入室検知
    if before.channel is None and after.channel is not None:
        # tts_auto_joinが有効な場合、BotをVCに参加させる
        if user_settings.get('auto_join_vc', False):
            vc = after.channel.guild.voice_client
            if vc is None:
                try:
                    await after.channel.connect()
                    print(f"[Bot INFO] {member.display_name}の入室に伴いVCに参加しました")
                except Exception as e:
                    print(f"[Bot ERROR] VC参加失敗: {e}")
            # 自動読み上げを有効にする
            user_settings['auto_read'] = True
            config_manager.set(f"user_settings.{user_id}", user_settings)
            await config_manager.save_config()
        
        # ユーザーが入退室時の読み上げを無効にしている場合はスキップ
        if user_settings.get('auto_tts') == False:
            return
        
        vc = after.channel.guild.voice_client
        if vc and vc.channel == after.channel:
            message_template = config_manager.get("tts.join_message", "{user}さんが入室しました")
            message = message_template.replace("{user}", member.display_name)
            
            # ユーザー設定を一時的に適用
            original_speaker = tts_manager.speaker
            original_speed = tts_manager.speed
            original_pitch = tts_manager.pitch
            original_intonation = tts_manager.intonation_scale
            
            if user_settings:
                if 'speaker' in user_settings:
                    tts_manager.speaker = user_settings['speaker']
                if 'speed' in user_settings:
                    tts_manager.speed = user_settings['speed']
                if 'pitch' in user_settings:
                    tts_manager.pitch = user_settings['pitch']
                if 'intonation_scale' in user_settings:
                    tts_manager.intonation_scale = user_settings['intonation_scale']
            
            audio_file = await tts_manager.synthesize(message)
            
            # 設定を元に戻す
            tts_manager.speaker = original_speaker
            tts_manager.speed = original_speed
            tts_manager.pitch = original_pitch
            tts_manager.intonation_scale = original_intonation
            
            if audio_file:
                if not vc.is_playing():
                    vc.play(discord.FFmpegPCMAudio(audio_file), after=lambda e: os.remove(audio_file) if os.path.exists(audio_file) else None)
    
    # 退室検知
    elif before.channel is not None and after.channel is None:
        # ユーザーが入退室時の読み上げを無効にしている場合はスキップ
        if user_settings.get('auto_tts') == False:
            return
        
        vc = before.channel.guild.voice_client
        if vc and vc.channel == before.channel:
            message_template = config_manager.get("tts.leave_message", "{user}さんが退室しました")
            message = message_template.replace("{user}", member.display_name)
            
            # ユーザー設定を一時的に適用
            original_speaker = tts_manager.speaker
            original_speed = tts_manager.speed
            original_pitch = tts_manager.pitch
            original_intonation = tts_manager.intonation_scale
            
            if user_settings:
                if 'speaker' in user_settings:
                    tts_manager.speaker = user_settings['speaker']
                if 'speed' in user_settings:
                    tts_manager.speed = user_settings['speed']
                if 'pitch' in user_settings:
                    tts_manager.pitch = user_settings['pitch']
                if 'intonation_scale' in user_settings:
                    tts_manager.intonation_scale = user_settings['intonation_scale']
            
            audio_file = await tts_manager.synthesize(message)
            
            # 設定を元に戻す
            tts_manager.speaker = original_speaker
            tts_manager.speed = original_speed
            tts_manager.pitch = original_pitch
            tts_manager.intonation_scale = original_intonation
            
            if audio_file:
                if not vc.is_playing():
                    vc.play(discord.FFmpegPCMAudio(audio_file), after=lambda e: os.remove(audio_file) if os.path.exists(audio_file) else None)
    
    # Bot以外の全員が退室したかチェック
    if before.channel and before.channel.guild.voice_client:
        non_bot_members = [m for m in before.channel.members if not m.bot]
        if not non_bot_members:
            # 音楽停止・キュー削除
            music_state['queue'].clear()
            music_state['current'] = None
            music_state['loop'] = False
            music_state['shuffle'] = False
            if before.channel.guild.voice_client:
                await before.channel.guild.voice_client.disconnect(force=True)
                print("[Bot INFO] 全員が退室したためVCから退出しました")
    
    # 入退室に関わらず、Bot以外の全員がいない場合は即時退出
    if after.channel and after.channel.guild.voice_client:
        non_bot_members = [m for m in after.channel.members if not m.bot]
        if not non_bot_members:
            # 音楽停止・キュー削除
            music_state['queue'].clear()
            music_state['current'] = None
            music_state['loop'] = False
            music_state['shuffle'] = False
            if after.channel.guild.voice_client:
                await after.channel.guild.voice_client.disconnect(force=True)
                print("[Bot INFO] 全員がいないためVCから退出しました")

@tasks.loop(seconds=config_update_interval)
async def update_config_task():
    """定期的にconfigを更新"""
    await config_manager.load_config()
    await tts_manager.update_settings()

# =====================================================================
# 9. 音楽Botコマンド（main3.pyベース - 拡張版）
# =====================================================================
@bot.command()
async def play(ctx, *, query):
    """音楽を再生する（URLまたはキーワード検索）"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    if not ctx.author.voice:
        await ctx.send("❌ エラー: 先にボイスチャンネルに入室してください。")
        return

    channel = ctx.author.voice.channel
    ticks = chr(96) * 3

    # キーワード検索の場合
    if not query.startswith(('http://', 'https://')):
        query = f"ytsearch:{query}"

    if ctx.voice_client is None:
        try:
            print("[Bot INFO] ボイスチャンネルへ接続しています...")
            vc = await channel.connect(timeout=10.0, reconnect=True)
            print("[Bot INFO] 接続成功！")
            music_state['voice_client'] = vc
            music_state['text_channel'] = ctx.channel
            # ビットレート最適化
            bitrate_msg = await optimize_channel_bitrate(vc)
            await ctx.send(bitrate_msg)
        except asyncio.TimeoutError:
            await ctx.send("❌ **接続タイムアウト:** UDP送信ポートがブロックされている可能性があります。")
            return
        except Exception as ce:
            error_trace = traceback.format_exc()
            msg = "⚠️ **接続エラーが発生しました。詳細:**\n" + ticks + "py\n" + error_trace[:1200] + "\n" + ticks
            await ctx.send(msg)
            return
    else:
        vc = ctx.voice_client
        # すでに接続済みの場合もmusic_stateを必ず更新する
        music_state['voice_client'] = vc
        music_state['text_channel'] = ctx.channel
        print(f"[Bot INFO] 既存VCを使用: {vc} / is_playing={vc.is_playing()} / is_tts={music_state.get('is_tts_playing')} / current={music_state.get('current')}")

    async with ctx.typing():
        try:
            loop = bot.loop
            print(f"[Bot INFO] 音源の解析を開始: {query}")
            data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False)),
                timeout=120.0
            )
            
            tracks = []
            if 'entries' in data:
                # プレイリスト or ytsearch結果の場合
                entries = [e for e in data['entries'] if e]
                for entry in entries:
                    tracks.append({
                        'url': entry.get('webpage_url') or entry.get('url'),
                        'title': entry.get('title', 'Unknown')
                    })
                if len(entries) == 1:
                    await ctx.send(f"🎵 **キューに追加:** `{tracks[0]['title']}`")
                else:
                    await ctx.send(f"� プレイリストから {len(tracks)} 曲をキューに追加しました")
            else:
                # 単曲の場合
                tracks.append({
                    'url': data.get('webpage_url') or data.get('url'),
                    'title': data.get('title', 'Unknown')
                })
            
            # キューに追加
            music_state['queue'].extend(tracks)
            
            # 音楽が再生中でない場合は再生開始
            # vc.is_playing()はTTS再生中もtrueになるため、music_state['current']で判断
            music_actually_playing = (
                music_state.get('current') is not None and
                vc.is_playing() and
                not music_state.get('is_tts_playing', False)
            )
            if not music_actually_playing:
                await play_next()
            else:
                await ctx.send(f"🎵 {len(tracks)} 曲をキューに追加しました")
            
        except Exception as e:
            error_trace = traceback.format_exc()
            msg = "❌ **再生処理でエラーが発生しました。詳細:**\n" + ticks + "py\n" + error_trace[:1200] + "\n" + ticks
            await ctx.send(msg)

@bot.tree.command(name="play", description="音楽を再生する（URLまたはキーワード検索）")
async def slash_play(interaction: discord.Interaction, query: str):
    """音楽を再生する（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    if not interaction.user.voice:
        await interaction.response.send_message("❌ エラー: 先にボイスチャンネルに入室してください。")
        return

    channel = interaction.user.voice.channel
    ticks = chr(96) * 3

    # キーワード検索の場合
    if not query.startswith(('http://', 'https://')):
        query = f"ytsearch:{query}"

    await interaction.response.defer()

    if interaction.guild.voice_client is None:
        try:
            print("[Bot INFO] ボイスチャンネルへ接続しています...")
            vc = await channel.connect(timeout=10.0, reconnect=True)
            print("[Bot INFO] 接続成功！")
            music_state['voice_client'] = vc
            music_state['text_channel'] = interaction.channel
            # ビットレート最適化
            bitrate_msg = await optimize_channel_bitrate(vc)
            await interaction.followup.send(bitrate_msg)
        except asyncio.TimeoutError:
            await interaction.followup.send("❌ **接続タイムアウト:** UDP送信ポートがブロックされている可能性があります。")
            return
        except Exception as ce:
            error_trace = traceback.format_exc()
            msg = "⚠️ **接続エラーが発生しました。詳細:**\n" + ticks + "py\n" + error_trace[:1200] + "\n" + ticks
            await interaction.followup.send(msg)
            return
    else:
        vc = interaction.guild.voice_client
        # すでに接続済みの場合もmusic_stateを必ず更新する
        music_state['voice_client'] = vc
        music_state['text_channel'] = interaction.channel
        print(f"[Bot INFO] 既存VCを使用: {vc} / is_playing={vc.is_playing()} / is_tts={music_state.get('is_tts_playing')} / current={music_state.get('current')}")

    try:
        loop = bot.loop
        print(f"[Bot INFO] 音源の解析を開始: {query}")

        # extract_info を最大3回リトライ
        data = None
        last_error = None
        for attempt in range(3):
            try:
                data = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False)),
                    timeout=120.0
                )
                break
            except Exception as e:
                last_error = e
                if attempt < 2:
                    await asyncio.sleep(2)
        if data is None:
            raise last_error
        
        tracks = []
        if 'entries' in data:
            # プレイリスト or ytsearch結果の場合
            entries = [e for e in data['entries'] if e]
            for entry in entries:
                tracks.append({
                    'url': entry.get('webpage_url') or entry.get('url'),
                    'title': entry.get('title', 'Unknown')
                })
            if len(entries) == 1:
                await interaction.followup.send(f"🎵 **キューに追加:** `{tracks[0]['title']}`")
            else:
                await interaction.followup.send(f"� プレイリストから {len(tracks)} 曲をキューに追加しました")
        else:
            # 単曲の場合
            tracks.append({
                'url': data.get('webpage_url') or data.get('url'),
                'title': data.get('title', 'Unknown')
            })
        
        # キューに追加
        music_state['queue'].extend(tracks)
        
        # 音楽が再生中でない場合は再生開始
        # vc.is_playing()はTTS再生中もtrueになるため、music_state['current']で判断
        music_actually_playing = (
            music_state.get('current') is not None and
            vc.is_playing() and
            not music_state.get('is_tts_playing', False)
        )
        if not music_actually_playing:
            await play_next()
        else:
            await interaction.followup.send(f"🎵 {len(tracks)} 曲をキューに追加しました")
        
    except Exception as e:
        error_trace = traceback.format_exc()
        msg = "❌ **再生処理でエラーが発生しました。詳細:**\n" + ticks + "py\n" + error_trace[:1200] + "\n" + ticks
        await interaction.followup.send(msg)

@bot.command()
async def pause(ctx):
    """音楽再生を一時停止するコマンド"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸️ 音楽の再生を一時停止しました。 `!resume` で再開します。")
    else:
        await ctx.send("❌ 現在、音楽は再生されていません。")

@bot.command()
async def resume(ctx):
    """一時停止した音楽再生を再開するコマンド"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ 音楽の再生を再開しました！")
    else:
        await ctx.send("❌ 一時停止中の音楽はありません。")

@bot.command()
async def volume(ctx, vol: int):
    """音量を調整するコマンド (0〜100の範囲)"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    vc = ctx.voice_client
    if not vc or not vc.source:
        await ctx.send("❌ 現在、何も再生されていません。")
        return

    if vol < 0 or vol > 100:
        await ctx.send("❌ 音量は 0 から 100 の間で指定してください。")
        return

    vc.source.volume = vol / 100
    await ctx.send("🔊 音量を **" + str(vol) + "%** に変更しました。")

@bot.command()
async def stop(ctx):
    """音楽再生を停止してチャンネルから退出するコマンド"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    music_state['queue'].clear()
    music_state['current'] = None
    music_state['loop'] = False
    music_state['shuffle'] = False
    
    if ctx.voice_client:
        await ctx.voice_client.disconnect(force=True)
        await ctx.send("👋 ボイスチャンネルから退出しました。またね！")
    else:
        await ctx.send("Botはどのボイスチャンネルにも参加していません。")

@bot.command()
async def loop(ctx):
    """現在の曲をループ再生"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    music_state['loop'] = not music_state['loop']
    status = "有効" if music_state['loop'] else "無効"
    await ctx.send(f"🔁 ループ再生: {status}")

@bot.command()
async def shuffle(ctx):
    """キューをシャッフル"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    music_state['shuffle'] = not music_state['shuffle']
    status = "有効" if music_state['shuffle'] else "無効"
    await ctx.send(f"🔀 シャッフル: {status}")

@bot.command()
async def skip(ctx):
    """現在の曲をスキップ"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await ctx.send("⏭️ 曲をスキップしました")
    else:
        await ctx.send("❌ 再生中の曲がありません")

@bot.command()
async def queue(ctx):
    """キューを表示"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    if not music_state['queue'] and not music_state['current']:
        await ctx.send("📋 キューは空です")
        return
    
    msg = ""
    if music_state['current']:
        msg += f"🎵 **現在再生中:** {music_state['current']['title']}\n\n"
    
    if music_state['queue']:
        msg += "📋 **再生キュー:**\n"
        for i, track in enumerate(music_state['queue'], 1):
            msg += f"{i}. {track['title']}\n"
    
    await ctx.send(msg)

@bot.command()
async def clear(ctx):
    """キューをクリア"""
    if not config_manager.get("music.enabled", True):
        await ctx.send("❌ 音楽機能は無効です")
        return
    
    music_state['queue'].clear()
    await ctx.send("🗑️ キューをクリアしました")

@bot.tree.command(name="pause", description="音楽再生を一時停止")
async def slash_pause(interaction: discord.Interaction):
    """音楽再生を一時停止（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ 音楽の再生を一時停止しました。")
    else:
        await interaction.response.send_message("❌ 現在、音楽は再生されていません。")

@bot.tree.command(name="resume", description="一時停止した音楽再生を再開")
async def slash_resume(interaction: discord.Interaction):
    """一時停止した音楽再生を再開（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ 音楽の再生を再開しました！")
    else:
        await interaction.response.send_message("❌ 一時停止中の音楽はありません。")

@bot.tree.command(name="volume", description="音量を調整 (0〜100)")
async def slash_volume(interaction: discord.Interaction, vol: int):
    """音量を調整（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    vc = interaction.guild.voice_client
    if not vc or not vc.source:
        await interaction.response.send_message("❌ 現在、何も再生されていません。")
        return

    if vol < 0 or vol > 100:
        await interaction.response.send_message("❌ 音量は 0 から 100 の間で指定してください。")
        return

    vc.source.volume = vol / 100
    await interaction.response.send_message(f"🔊 音量を **{vol}%** に変更しました。")

@bot.tree.command(name="stop", description="音楽再生を停止して退出")
async def slash_stop(interaction: discord.Interaction):
    """音楽再生を停止して退出（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    music_state['queue'].clear()
    music_state['current'] = None
    music_state['loop'] = False
    music_state['shuffle'] = False
    
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect(force=True)
        await interaction.response.send_message("👋 ボイスチャンネルから退出しました。またね！")
    else:
        await interaction.response.send_message("Botはどのボイスチャンネルにも参加していません。")

@bot.tree.command(name="loop", description="現在の曲をループ再生")
async def slash_loop(interaction: discord.Interaction):
    """現在の曲をループ再生（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    music_state['loop'] = not music_state['loop']
    status = "有効" if music_state['loop'] else "無効"
    await interaction.response.send_message(f"🔁 ループ再生: {status}")

@bot.tree.command(name="shuffle", description="キューをシャッフル")
async def slash_shuffle(interaction: discord.Interaction):
    """キューをシャッフル（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    music_state['shuffle'] = not music_state['shuffle']
    status = "有効" if music_state['shuffle'] else "無効"
    await interaction.response.send_message(f"🔀 シャッフル: {status}")

@bot.tree.command(name="skip", description="現在の曲をスキップ")
async def slash_skip(interaction: discord.Interaction):
    """現在の曲をスキップ（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ 曲をスキップしました")
    else:
        await interaction.response.send_message("❌ 再生中の曲がありません")

@bot.tree.command(name="queue", description="キューを表示")
async def slash_queue(interaction: discord.Interaction):
    """キューを表示（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    if not music_state['queue'] and not music_state['current']:
        await interaction.response.send_message("📋 キューは空です")
        return
    
    msg = ""
    if music_state['current']:
        msg += f"🎵 **現在再生中:** {music_state['current']['title']}\n\n"
    
    if music_state['queue']:
        msg += "📋 **再生キュー:**\n"
        for i, track in enumerate(music_state['queue'], 1):
            msg += f"{i}. {track['title']}\n"
    
    await interaction.response.send_message(msg)

@bot.tree.command(name="clear", description="キューをクリア")
async def slash_clear(interaction: discord.Interaction):
    """キューをクリア（スラッシュコマンド）"""
    if not config_manager.get("music.enabled", True):
        await interaction.response.send_message("❌ 音楽機能は無効です")
        return
    
    music_state['queue'].clear()
    await interaction.response.send_message("🗑️ キューをクリアしました")

# =====================================================================
# 10. TTSコマンド
# =====================================================================
@bot.command()
async def tts_text(ctx, mode: str = None, setting: str = None):
    """TTS設定を管理"""
    if not config_manager.get("tts.enabled", False):
        await ctx.send("❌ TTS機能は無効です")
        return
    
    user_id = str(ctx.author.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    if user_settings is None:
        user_settings = {}
    
    if mode == "join":
        if setting == "auto":
            # 入退室時の読み上げ（入室アナウンス）のon/off
            current = user_settings.get('auto_tts', True)
            user_settings['auto_tts'] = not current
            config_manager.set(f"user_settings.{user_id}", user_settings)
            await config_manager.save_config()
            status = "有効" if not current else "無効"
            await ctx.send(f"✅ 入退室アナウンス読み上げ: {status}")
        else:
            # Botをボイスチャンネルに参加させ、このテキストチャンネルの発言を自動読み上げON
            if not ctx.author.voice:
                await ctx.send("❌ 先にボイスチャンネルに入室してください")
                return
            
            vc = ctx.guild.voice_client
            if vc is None:
                try:
                    vc = await ctx.author.voice.channel.connect(timeout=10.0, reconnect=True)
                except Exception as e:
                    await ctx.send(f"❌ ボイスチャンネルへの接続に失敗しました: {e}")
                    return
            elif vc.channel != ctx.author.voice.channel:
                await vc.move_to(ctx.author.voice.channel)
            
            # このユーザーの自動読み上げをON
            current = user_settings.get('auto_read', False)
            user_settings['auto_read'] = not current
            config_manager.set(f"user_settings.{user_id}", user_settings)
            await config_manager.save_config()
            status = "開始" if not current else "停止"
            await ctx.send(f"✅ テキストチャンネル自動読み上げ: {status}\n{'🔊 このチャンネルの発言を読み上げます' if not current else '🔇 読み上げを停止しました'}")
    else:
        await ctx.send(
            "📝 **TTSコマンド使用方法:**\n"
            "• `!tts join` — Botをボイスchに参加させ、自分の発言を自動読み上げ（再度実行でOFF）\n"
            "• `!tts join auto` — 入退室アナウンス読み上げのon/off切替（デフォルト=ON）\n"
            "• `!set_tts [話者ID] [速度] [ピッチ]` — 自分の声設定を変更\n"
            "• `!add_dict [単語] [置換]` — 読み上げ辞書に登録\n"
            "• `!list_dict` — 辞書一覧を表示"
        )

@bot.event
async def on_message(message):
    """メッセージを受信したときの処理"""
    if message.author.bot:
        return

    await bot.process_commands(message)

    if message.content.startswith(bot.command_prefix):
        return

    # TTSが有効でない場合は無視
    if not config_manager.get("tts.enabled", False):
        return
    
    # ユーザーごとのTTS設定を取得
    user_id = str(message.author.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    if user_settings is None:
        user_settings = {}
    
    # 自動読み上げが有効でない場合は無視
    if not user_settings.get('auto_read', False):
        return
    
    # 読み上げチャンネルチェック
    read_channel_id = user_settings.get('read_channel', str(message.channel.id))
    if str(message.channel.id) != read_channel_id:
        return
    
    # ボイスチャンネルに接続していない場合は無視
    if not message.author.voice:
        return
    
    # Botがボイスチャンネルに接続していない場合は無視
    vc = message.guild.voice_client
    if not vc or vc.channel != message.author.voice.channel:
        return
    
    # テキストを読み上げ
    text = message.content
    if not text:
        return
    
    # TTSキューに追加
    tts_queue.add(text, vc, user_settings)

@bot.command()
async def set_tts(ctx, speaker: int = None, speed: float = None, pitch: float = None):
    """自分のTTS設定を変更"""
    if not config_manager.get("tts.enabled", False):
        await ctx.send("❌ TTS機能は無効です")
        return
    
    user_id = str(ctx.author.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    if user_settings is None:
        user_settings = {}
    
    if speaker is not None:
        user_settings['speaker'] = speaker
    if speed is not None:
        user_settings['speed'] = speed
    if pitch is not None:
        user_settings['pitch'] = pitch
    
    config_manager.set(f"user_settings.{user_id}", user_settings)
    await config_manager.save_config()
    
    msg = "✅ TTS設定を更新しました:\n"
    msg += f"• 話者ID: {user_settings.get('speaker', 'デフォルト')}\n"
    msg += f"• 速度: {user_settings.get('speed', 'デフォルト')}\n"
    msg += f"• ピッチ: {user_settings.get('pitch', 'デフォルト')}"
    await ctx.send(msg)

@bot.command()
async def reset_tts(ctx):
    """自分のTTS設定をリセット"""
    if not config_manager.get("tts.enabled", False):
        await ctx.send("❌ TTS機能は無効です")
        return
    
    user_id = str(ctx.author.id)
    config_manager.set(f"user_settings.{user_id}", None)
    await config_manager.save_config()
    
    await ctx.send("✅ TTS設定をリセットしました")

@bot.command()
async def tts_enable(ctx):
    """自分のTTSを有効にする"""
    user_id = str(ctx.author.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    user_settings['tts_enabled'] = True
    config_manager.set(f"user_settings.{user_id}", user_settings)
    await config_manager.save_config()
    
    await ctx.send("✅ TTSを有効にしました")

@bot.command()
async def tts_disable(ctx):
    """自分のTTSを無効にする"""
    user_id = str(ctx.author.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    user_settings['tts_enabled'] = False
    config_manager.set(f"user_settings.{user_id}", user_settings)
    await config_manager.save_config()
    
    await ctx.send("✅ TTSを無効にしました")

@bot.command()
async def tts_quit(ctx):
    """Botをボイスチャンネルから退出（緊急用：全TTS機能を無効化）"""
    vc = ctx.guild.voice_client
    if vc:
        # TTSが再生中の場合は待機
        if tts_queue.is_processing:
            await ctx.send("⏳ TTS再生中です。終了までお待ちください...")
            while tts_queue.is_processing:
                await asyncio.sleep(0.1)
        
        # 音楽停止・キュー削除
        music_state['queue'].clear()
        music_state['current'] = None
        music_state['loop'] = False
        music_state['shuffle'] = False
        
        await vc.disconnect(force=True)
        
        # 全TTS機能を無効化
        user_id = str(ctx.author.id)
        user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
        user_settings['auto_read'] = False
        user_settings['tts_enabled'] = False
        config_manager.set(f"user_settings.{user_id}", user_settings)
        await config_manager.save_config()
        
        await ctx.send("👋 ボイスチャンネルから退出しました（TTS機能を全て無効化しました）")
    else:
        await ctx.send("❌ Botはボイスチャンネルに参加していません")

@bot.command()
async def tts_global_disable(ctx):
    """TTSをグローバルで無効にする（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("tts.enabled", False)
    await config_manager.save_config()
    await tts_manager.update_settings()
    await ctx.send("✅ TTSをグローバルで無効にしました")

@bot.command()
async def tts_global_enable(ctx):
    """TTSをグローバルで有効にする（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("tts.enabled", True)
    await config_manager.save_config()
    await tts_manager.update_settings()
    await ctx.send("✅ TTSをグローバルで有効にしました")

@bot.command()
async def add_dict(ctx, word: str, replacement: str):
    """TTS辞書に単語を追加（全員使用可）"""
    success = await tts_manager.add_dictionary(word, replacement)
    if success:
        await ctx.send(f"✅ 辞書に追加しました: 「{word}」→「{replacement}」")
    else:
        await ctx.send("❌ 辞書の追加に失敗しました")

@bot.command()
async def list_dict(ctx):
    """TTS辞書を表示"""
    dictionary = config_manager.get("tts.dictionary", {})
    if not dictionary:
        await ctx.send("辞書は空です")
        return
    
    msg = "📖 **TTS辞書:**\n"
    for word, replacement in dictionary.items():
        msg += f"• 「{word}」→「{replacement}」\n"
    await ctx.send(msg)

@bot.tree.command(name="tts_text", description="テキストを読み上げる")
async def slash_tts_text(interaction: discord.Interaction, text: str):
    """テキストを読み上げる（スラッシュコマンド）"""
    if not config_manager.get("tts.enabled", False):
        await interaction.response.send_message("❌ TTS機能は無効です")
        return
    
    if not interaction.user.voice:
        await interaction.response.send_message("❌ 先にボイスチャンネルに入室してください")
        return
    
    vc = interaction.guild.voice_client
    if not vc or vc.channel != interaction.user.voice.channel:
        try:
            vc = await interaction.user.voice.channel.connect(timeout=10.0, reconnect=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ ボイスチャンネルへの接続に失敗しました: {e}")
            return
    
    await interaction.response.defer()
    audio_file = await tts_manager.synthesize(text)
    if audio_file:
        if vc.is_playing():
            vc.stop()
        vc.play(discord.FFmpegPCMAudio(audio_file), after=lambda e: os.remove(audio_file) if os.path.exists(audio_file) else None)
        await interaction.followup.send(f"🔊 「{text}」を読み上げます")
    else:
        await interaction.followup.send("❌ 音声の生成に失敗しました")

@bot.tree.command(name="add_dict", description="TTS辞書に単語を追加（全員使用可）")
async def slash_add_dict(interaction: discord.Interaction, word: str, replacement: str):
    """TTS辞書に単語を追加（スラッシュコマンド）"""
    success = await tts_manager.add_dictionary(word, replacement)
    if success:
        await interaction.response.send_message(f"✅ 辞書に追加しました: 「{word}」→「{replacement}」")
    else:
        await interaction.response.send_message("❌ 辞書の追加に失敗しました")

@bot.tree.command(name="list_dict", description="TTS辞書を表示")
async def slash_list_dict(interaction: discord.Interaction):
    """TTS辞書を表示（スラッシュコマンド）"""
    dictionary = config_manager.get("tts.dictionary", {})
    if not dictionary:
        await interaction.response.send_message("辞書は空です")
        return
    
    msg = "📖 **TTS辞書:**\n"
    for word, replacement in dictionary.items():
        msg += f"• 「{word}」→「{replacement}」\n"
    await interaction.response.send_message(msg)

@bot.tree.command(name="tts", description="Botをボイスchに参加させ自分の発言を自動読み上げ")
@discord.app_commands.describe(channel="読み上げるチャンネル（デフォルト: 現在のチャンネル）")
async def slash_tts(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not config_manager.get("tts.enabled", False):
        await interaction.response.send_message("❌ TTS機能は無効です")
        return
    if not interaction.user.voice:
        await interaction.response.send_message("❌ 先にボイスチャンネルに入室してください")
        return
    
    vc = interaction.guild.voice_client
    if vc is None:
        try:
            vc = await interaction.user.voice.channel.connect(timeout=10.0, reconnect=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ 接続失敗: {e}")
            return
    elif vc.channel != interaction.user.voice.channel:
        await vc.move_to(interaction.user.voice.channel)
    
    user_id = str(interaction.user.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    # 自動読み上げを有効にする（トグルしない）
    user_settings['auto_read'] = True
    # 読み上げチャンネルを設定
    if channel:
        user_settings['read_channel'] = str(channel.id)
    else:
        user_settings['read_channel'] = str(interaction.channel.id)
    
    config_manager.set(f"user_settings.{user_id}", user_settings)
    await config_manager.save_config()
    
    await interaction.response.send_message("🔊 読み上げを開始します")

@bot.tree.command(name="set_tts", description="自分のTTS声設定を変更（話者ID / 速度 / ピッチ）")
@discord.app_commands.describe(
    speaker="話者ID（例: 0=四国めたん, 1=ずんだもん）",
    speed="読み上げ速度（0.5〜2.0、デフォルト1.0）",
    pitch="ピッチ（-0.15〜0.15、デフォルト0.0）",
)
async def slash_set_tts(interaction: discord.Interaction,
                         speaker: int = None, speed: float = None, pitch: float = None):
    if not config_manager.get("tts.enabled", False):
        await interaction.response.send_message("❌ TTS機能は無効です")
        return
    user_id = str(interaction.user.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    if user_settings is None:
        user_settings = {}
    if speaker is not None: user_settings['speaker'] = speaker
    if speed   is not None: user_settings['speed']   = speed
    if pitch   is not None: user_settings['pitch']   = pitch
    config_manager.set(f"user_settings.{user_id}", user_settings)
    await config_manager.save_config()
    msg = "✅ TTS設定を更新しました:\n"
    msg += f"• 話者ID: {user_settings.get('speaker', 'デフォルト')}\n"
    msg += f"• 速度: {user_settings.get('speed', 'デフォルト')}\n"
    msg += f"• ピッチ: {user_settings.get('pitch', 'デフォルト')}"
    await interaction.response.send_message(msg)

@bot.tree.command(name="reset_tts", description="自分のTTS設定をリセット")
async def slash_reset_tts(interaction: discord.Interaction):
    if not config_manager.get("tts.enabled", False):
        await interaction.response.send_message("❌ TTS機能は無効です")
        return
    config_manager.set(f"user_settings.{str(interaction.user.id)}", None)
    await config_manager.save_config()
    await interaction.response.send_message("✅ TTS設定をリセットしました")

@bot.tree.command(name="tts_enable", description="自分のTTSを有効にする")
async def slash_tts_enable(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    user_settings['tts_enabled'] = True
    config_manager.set(f"user_settings.{user_id}", user_settings)
    await config_manager.save_config()
    await interaction.response.send_message("✅ TTSを有効にしました")

@bot.tree.command(name="tts_disable", description="自分のTTSを無効にする")
async def slash_tts_disable(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
    user_settings['tts_enabled'] = False
    config_manager.set(f"user_settings.{user_id}", user_settings)
    await config_manager.save_config()
    await interaction.response.send_message("✅ TTSを無効にしました")

@bot.tree.command(name="tts_global_disable", description="TTSをグローバルで無効にする（管理者用）")
async def slash_tts_global_disable(interaction: discord.Interaction):
    """TTSをグローバルで無効にする（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("tts.enabled", False)
    await config_manager.save_config()
    await tts_manager.update_settings()
    await interaction.response.send_message("✅ TTSをグローバルで無効にしました")

@bot.tree.command(name="tts_global_enable", description="TTSをグローバルで有効にする（管理者用）")
async def slash_tts_global_enable(interaction: discord.Interaction):
    """TTSをグローバルで有効にする（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("tts.enabled", True)
    await config_manager.save_config()
    await tts_manager.update_settings()
    await interaction.response.send_message("✅ TTSをグローバルで有効にしました")

@bot.tree.command(name="tts_quit", description="Botをボイスチャンネルから退出（緊急用：全TTS機能を無効化）")
async def slash_tts_quit(interaction: discord.Interaction):
    """Botをボイスチャンネルから退出（スラッシュコマンド）"""
    vc = interaction.guild.voice_client
    if vc:
        # TTSが再生中の場合は待機
        if tts_queue.is_processing:
            await interaction.response.send_message("⏳ TTS再生中です。終了までお待ちください...")
            while tts_queue.is_processing:
                await asyncio.sleep(0.1)
        
        # 音楽停止・キュー削除
        music_state['queue'].clear()
        music_state['current'] = None
        music_state['loop'] = False
        music_state['shuffle'] = False
        
        await vc.disconnect(force=True)
        
        # 全TTS機能を無効化
        user_id = str(interaction.user.id)
        user_settings = config_manager.get(f"user_settings.{user_id}", {}) or {}
        user_settings['auto_read'] = False
        user_settings['tts_enabled'] = False
        config_manager.set(f"user_settings.{user_id}", user_settings)
        await config_manager.save_config()
        
        await interaction.response.send_message("👋 ボイスチャンネルから退出しました（TTS機能を全て無効化しました）")
    else:
        await interaction.response.send_message("❌ Botはボイスチャンネルに参加していません")


# =====================================================================
# 11. おみくじコマンド
# =====================================================================
import os as _os
import tempfile as _tempfile

# おみくじテンプレート画像パス（bot.pyと同じディレクトリに置く）
_OMIKUJI_TEMPLATE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "omikuji.png")

# config.json の omikuji.results から結果リストを取得して使用
# 画像には運勢名のみを大きく表示（1〜3文字）

def _generate_omikuji_image(result: str) -> str:
    """おみくじ画像を生成して一時ファイルパスを返す（結果名のみ大きく表示）"""
    from PIL import Image, ImageDraw, ImageFont

    def _find_font(candidates):
        import os
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def _load_font(path, size):
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    # 行書体・楷書体の優先候補（Ubuntu / Windows / macOS）
    SERIF_CANDIDATES = [
        # Ubuntu: noto-cjk 系（行書に近い明朝 Bold）
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSerifCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Black.ttc",
        # Ubuntu: IPAex明朝（行書風の毛筆感がある）
        "/usr/share/fonts/opentype/ipaexfont-mincho/ipaexm.ttf",
        "/usr/share/fonts/truetype/ipaexfont-mincho/ipaexm.ttf",
        # Ubuntu: fonts-hanazono（花園明朝、筆文字感が強い）
        "/usr/share/fonts/truetype/hanazono/HanaMinA.ttf",
        # Windows: 游明朝（行書風で最も自然）
        r"C:\Windows\Fonts\yumin.ttf",
        r"C:\Windows\Fonts\yuminl.ttf",
        r"C:\Windows\Fonts\YuMincho-Regular.ttf",
        # Windows: HGS行書体・HGP行書体（行書そのもの）
        r"C:\Windows\Fonts\HGRSGU.TTC",
        r"C:\Windows\Fonts\HGRSKP.TTC",
        r"C:\Windows\Fonts\HGGYOKU.TTC",
        # Windows: メイリオ・MS明朝（フォールバック）
        r"C:\Windows\Fonts\meiryo.ttc",
        r"C:\Windows\Fonts\msgothic.ttc",
        # macOS
        "/System/Library/Fonts/ヒラギノ明朝 ProN W6.otf",
    ]

    FONT_SERIF = _find_font(SERIF_CANDIDATES)
    RED = (196, 60, 40)

    img = Image.open(_OMIKUJI_TEMPLATE).copy()
    draw = ImageDraw.Draw(img)

    # 描画エリア（左, 上, 右, 下）
    AREA = (215, 330, 810, 1145)
    cx = (AREA[0] + AREA[2]) // 2          # 横中央
    area_h = AREA[3] - AREA[1]             # エリアの高さ
    area_center_y = (AREA[1] + AREA[3]) // 2  # 縦中央（修正: AREA[3]を使用）

    char_count = len(result)

    if char_count == 1:
        # 1文字: 横書きで中央に大きく
        font_size = 260
        font_title = _load_font(FONT_SERIF, font_size)
        bbox = draw.textbbox((0, 0), result, font=font_title)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = cx - tw // 2
        y = area_center_y - th // 2
        draw.text((x, y), result, font=font_title, fill=RED)

    else:
        # 2文字以上: 縦書きレイアウト（1文字ずつ縦に並べる）
        area_w = AREA[2] - AREA[0]
        if char_count == 2:
            font_size = 220
            line_gap_ratio = 0.8   # 字間 = 文字サイズの0.8倍
        elif char_count == 3:
            font_size = 200        # 2文字とほぼ同サイズを維持
            line_gap_ratio = 0.45  # 字間 = 0.45倍（1+0.45+1+0.45+1 = 3.9文字分）
        else:
            font_size = max(100, int(area_h * 0.75 / char_count))
            line_gap_ratio = 0.4

        font_title = _load_font(FONT_SERIF, font_size)

        # 各文字のサイズを測定して縦配置を計算
        char_sizes = []
        for ch in result:
            bbox = draw.textbbox((0, 0), ch, font=font_title)
            cw = bbox[2] - bbox[0]
            ch_h = bbox[3] - bbox[1]
            char_sizes.append((cw, ch_h))

        line_gap = int(font_size * line_gap_ratio)
        total_h = sum(h for _, h in char_sizes) + line_gap * (char_count - 1)

        # 縦方向の開始位置（エリア中央に揃える）
        y_start = area_center_y - total_h // 2

        y = y_start
        for i, ch in enumerate(result):
            cw, ch_h = char_sizes[i]
            x = cx - cw // 2
            draw.text((x, y), ch, font=font_title, fill=RED)
            y += ch_h + line_gap

    tmp = _tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=_tempfile.gettempdir())
    img.save(tmp.name)
    tmp.close()
    return tmp.name


async def _send_omikuji(send_func, result: str, is_interaction: bool = False):
    """「結果は…」送信 → 画像生成と並列待機 → 元メッセージを編集して画像追加"""
    # 画像生成を先行して非同期で開始（待機中も生成が進む）
    img_task = asyncio.get_running_loop().run_in_executor(
        None, _generate_omikuji_image, result
    )

    msg = await send_func("🎐 結果は…")

    # 画像生成完了 or 最低1.5秒待機（どちらか遅い方）
    await asyncio.sleep(1.5)
    img_path = await img_task

    try:
        with open(img_path, "rb") as f:
            file = discord.File(f, filename="omikuji_result.png")
            # 元のメッセージを編集して画像を追加（別メッセージにしない）
            if msg is not None:
                await msg.edit(content="🎐 結果は…", attachments=[file])
            else:
                await send_func(file=file)
    finally:
        try:
            _os.remove(img_path)
        except Exception:
            pass


@bot.command()
async def omikuji(ctx):
    """おみくじを引く"""
    if not config_manager.get("omikuji.enabled", True):
        await ctx.send("❌ おみくじ機能は無効です")
        return

    force_result = config_manager.get("omikuji.force_result")
    if force_result:
        result = force_result
        config_manager.set("omikuji.force_result", None)
        await config_manager.save_config()
    else:
        results = config_manager.get("omikuji.results", ["大吉", "中吉", "小吉", "吉", "末吉", "凶", "大凶"])
        result = random.choice(results)

    await _send_omikuji(ctx.send, result)


@bot.command()
async def set_omikuji(ctx, result: str = None):
    """おみくじの強制結果を設定（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return

    if result is None:
        config_manager.set("omikuji.force_result", None)
        await config_manager.save_config()
        await ctx.send("✅ 強制結果をクリアしました")
    else:
        config_manager.set("omikuji.force_result", result)
        await config_manager.save_config()
        await ctx.send(f"✅ 強制結果を「{result}」に設定しました")


@bot.tree.command(name="omikuji", description="おみくじを引く")
async def slash_omikuji(interaction: discord.Interaction):
    """おみくじを引く（スラッシュコマンド）"""
    if not config_manager.get("omikuji.enabled", True):
        await interaction.response.send_message("❌ おみくじ機能は無効です")
        return

    force_result = config_manager.get("omikuji.force_result")
    if force_result:
        result = force_result
        config_manager.set("omikuji.force_result", None)
        await config_manager.save_config()
    else:
        results = config_manager.get("omikuji.results", ["大吉", "中吉", "小吉", "吉", "末吉", "凶", "大凶"])
        result = random.choice(results)

    # 画像生成を先行して非同期で開始
    img_task = asyncio.get_running_loop().run_in_executor(
        None, _generate_omikuji_image, result
    )

    await interaction.response.send_message("🎐 結果は…")

    # 画像生成完了 or 最低1.5秒待機（どちらか遅い方）
    await asyncio.sleep(1.5)
    img_path = await img_task

    try:
        with open(img_path, "rb") as f:
            file = discord.File(f, filename="omikuji_result.png")
            # 元のメッセージを編集して画像を追加（別メッセージにしない）
            await interaction.edit_original_response(content="🎐 結果は…", attachments=[file])
    finally:
        try:
            _os.remove(img_path)
        except Exception:
            pass


@bot.tree.command(name="set_omikuji", description="おみくじの強制結果を設定（管理者用）")
async def slash_set_omikuji(interaction: discord.Interaction, result: str = None):
    """おみくじの強制結果を設定（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return

    if result is None:
        config_manager.set("omikuji.force_result", None)
        await config_manager.save_config()
        await interaction.response.send_message("✅ 強制結果をクリアしました")
    else:
        config_manager.set("omikuji.force_result", result)
        await config_manager.save_config()
        await interaction.response.send_message(f"✅ 強制結果を「{result}」に設定しました")

# =====================================================================
# 12. 寝落ち監視コマンド
# =====================================================================
@bot.command()
async def sleep(ctx, member: discord.Member):
    """寝落ち監視を開始"""
    if member.bot:
        await ctx.send("❌ Botを監視することはできません")
        return
    
    if not member.voice:
        await ctx.send("❌ その人はVCに入っていません")
        return
    
    if str(member.id) in sleep_monitors:
        await ctx.send("❌ このユーザーは既に監視中です")
        return
    
    # タイムアウト時間を取得（デフォルト10分）
    timeout_minutes = config_manager.get("sleep.timeout_minutes", 10)
    
    # 寝落ち監視メッセージチャンネルを取得（設定されていない場合は元のチャンネルを使用）
    sleep_message_channel_id = config_manager.get("sleep.message_channel")
    if sleep_message_channel_id:
        message_channel = ctx.guild.get_channel(int(sleep_message_channel_id))
        if message_channel:
            target_channel = message_channel
        else:
            target_channel = ctx.channel
    else:
        target_channel = ctx.channel
    
    # 監視メッセージを送信
    msg = await target_channel.send(f"⏰ {member.mention} さんが寝落ちしていないか監視中...\n"
                        f"{timeout_minutes}分以内にリアクションを返してください（反応がない場合はキックされます）")
    
    # リアクションを追加
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")
    
    # 監視情報を保存
    sleep_monitors[str(member.id)] = {
        'start_time': datetime.now(),
        'message_id': msg.id,
        'channel_id': target_channel.id,
        'guild_id': ctx.guild.id,
        'monitor_user_id': str(ctx.author.id)
    }
    
    await ctx.send("✅ 寝落ち監視を開始しました")
    
    # TTSアナウンス（半分の音量で）
    if member.voice and member.voice.channel:
        vc = ctx.guild.voice_client
        if vc is None:
            try:
                vc = await member.voice.channel.connect(timeout=10.0, reconnect=True)
            except Exception as e:
                logger.error(f"Failed to connect to voice channel for TTS: {e}")
                return
        elif vc.channel != member.voice.channel:
            await vc.move_to(member.voice.channel)
        
        # 音楽が再生中なら pause（TTS優先）
        music_was_playing = vc.is_playing()
        if music_was_playing:
            vc.pause()
            music_state['is_tts_playing'] = True

        tts_text = f"{member.display_name}さんの寝落ち監視始めるぜ。おきろー"
        audio_file = await tts_manager.synthesize(tts_text)

        if audio_file:
            def after_tts(e):
                if e:
                    logger.error(f"TTS playback error: {e}")
                if os.path.exists(audio_file):
                    try:
                        os.remove(audio_file)
                    except Exception:
                        pass

            vc.play(discord.FFmpegPCMAudio(audio_file), after=after_tts)

            # TTS再生が終わるまで待機
            while vc.is_playing():
                await asyncio.sleep(0.1)

        # 音楽を再開
        if music_was_playing and vc.is_paused():
            vc.resume()
        music_state['is_tts_playing'] = False

@bot.tree.command(name="sleep", description="寝落ち監視を開始")
async def slash_sleep(interaction: discord.Interaction, member: discord.Member):
    """寝落ち監視を開始（スラッシュコマンド）"""
    # 【追加】最初にDiscordへ「処理中」のサインを送り、制限時間を15分に延長する
    await interaction.response.defer()

    if member.bot:
        await interaction.followup.send("❌ Botを監視することはできません")
        return
    
    if not member.voice:
        await interaction.followup.send("❌ その人はVCに入っていません")
        return
    
    if str(member.id) in sleep_monitors:
        await interaction.followup.send("❌ このユーザーは既に監視中です")
        return
    
    # タイムアウト時間を取得（デフォルト10分）
    timeout_minutes = config_manager.get("sleep.timeout_minutes", 10)
    
    # 寝落ち監視メッセージチャンネルを取得（設定されていない場合は元のチャンネルを使用）
    sleep_message_channel_id = config_manager.get("sleep.message_channel")
    if sleep_message_channel_id:
        message_channel = interaction.guild.get_channel(int(sleep_message_channel_id))
        if message_channel:
            target_channel = message_channel
        else:
            target_channel = interaction.channel
    else:
        target_channel = interaction.channel
    
    # 【変更】最初の返答なので、response.send_message ではなく followup.send を使う
    msg = await interaction.followup.send(
        f"⏰ {member.mention} さんが寝落ちしていないか監視中...\n"
        f"{timeout_minutes}分以内にリアクションを返してください（反応がない場合はキックされます）"
    )
    
    # リアクションを追加
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")
    
    # 監視情報を保存
    sleep_monitors[str(member.id)] = {
        'start_time': datetime.now(),
        'message_id': msg.id,
        'channel_id': target_channel.id,
        'guild_id': interaction.guild.id,
        'monitor_user_id': str(interaction.user.id)
    }
    
    # 【変更】二回目以降のメッセージ送信も followup.send を使用
    await interaction.followup.send("✅ 寝落ち監視を開始しました")
    
    # TTSアナウンス（半分の音量で）
    if member.voice and member.voice.channel:
        vc = interaction.guild.voice_client
        if vc is None:
            try:
                vc = await member.voice.channel.connect(timeout=10.0, reconnect=True)
            except Exception as e:
                logger.error(f"Failed to connect to voice channel for TTS: {e}")
                return
        elif vc.channel != member.voice.channel:
            await vc.move_to(member.voice.channel)
        
        # 音楽が再生中なら pause（TTS優先）
        music_was_playing = vc.is_playing()
        if music_was_playing:
            vc.pause()
            music_state['is_tts_playing'] = True

        tts_text = f"{member.display_name}さんの寝落ち監視始めるぜ。おきろー"
        audio_file = await tts_manager.synthesize(tts_text)

        if audio_file:
            def after_tts(e):
                if e:
                    logger.error(f"TTS playback error: {e}")
                if os.path.exists(audio_file):
                    try:
                        os.remove(audio_file)
                    except Exception:
                        pass

            vc.play(discord.FFmpegPCMAudio(audio_file), after=after_tts)

            # TTS再生が終わるまで待機
            while vc.is_playing():
                await asyncio.sleep(0.1)

        # 音楽を再開
        if music_was_playing and vc.is_paused():
            vc.resume()
        music_state['is_tts_playing'] = False

@bot.command()
async def set_sleep_channel(ctx, channel: discord.TextChannel):
    """寝落ち通知チャンネルを設定"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("sleep.notification_channel", str(channel.id))
    await config_manager.save_config()
    await ctx.send(f"✅ 寝落ち通知チャンネルを {channel.mention} に設定しました")

@bot.command()
async def set_sleep_time(ctx, minutes: int):
    """寝落ち監視のタイムアウト時間を設定（分）"""
    admin_users = config_manager.get("bot.admin_users", [])
    # admin_usersが設定されている場合はチェック、設定されていない場合はDiscordの管理者権限でチェック
    if admin_users:
        if str(ctx.author.id) not in admin_users:
            await ctx.send("❌ このコマンドは管理者のみ使用可能です")
            return
    else:
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ このコマンドは管理者のみ使用可能です")
            return
    
    if minutes < 1:
        await ctx.send("❌ 1分以上を指定してください")
        return
    
    config_manager.set("sleep.timeout_minutes", minutes)
    await config_manager.save_config()
    await ctx.send(f"✅ 寝落ち監視のタイムアウト時間を {minutes}分 に設定しました")

@bot.command()
async def set_music_channel(ctx, channel: discord.TextChannel):
    """音楽通知チャンネルを設定"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("music.notification_channel", str(channel.id))
    await config_manager.save_config()
    await ctx.send(f"✅ 音楽通知チャンネルを {channel.mention} に設定しました")

@bot.command()
async def set_sleep_message_channel(ctx, channel: discord.TextChannel):
    """寝落ち監視メッセージチャンネルを設定"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("sleep.message_channel", str(channel.id))
    await config_manager.save_config()
    await ctx.send(f"✅ 寝落ち監視メッセージチャンネルを {channel.mention} に設定しました")

@bot.tree.command(name="set_sleep_channel", description="寝落ち通知チャンネルを設定（管理者用）")
async def slash_set_sleep_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    """寝落ち通知チャンネルを設定（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("sleep.notification_channel", str(channel.id))
    await config_manager.save_config()
    await interaction.response.send_message(f"✅ 寝落ち通知チャンネルを {channel.mention} に設定しました")

@bot.tree.command(name="set_music_channel", description="音楽通知チャンネルを設定（管理者用）")
async def slash_set_music_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    """音楽通知チャンネルを設定（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("music.notification_channel", str(channel.id))
    await config_manager.save_config()
    await interaction.response.send_message(f"✅ 音楽通知チャンネルを {channel.mention} に設定しました")

@bot.tree.command(name="set_sleep_message_channel", description="寝落ち監視メッセージチャンネルを設定（管理者用）")
async def slash_set_sleep_message_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    """寝落ち監視メッセージチャンネルを設定（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    config_manager.set("sleep.message_channel", str(channel.id))
    await config_manager.save_config()
    await interaction.response.send_message(f"✅ 寝落ち監視メッセージチャンネルを {channel.mention} に設定しました")




@bot.tree.command(name="set_sleep_time", description="寝落ち監視のタイムアウト時間を設定（管理者用）")
async def slash_set_sleep_time(interaction: discord.Interaction, minutes: int):
    """寝落ち監視のタイムアウト時間を設定（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    # admin_usersが設定されている場合はチェック、設定されていない場合はDiscordの管理者権限でチェック
    if admin_users:
        if str(interaction.user.id) not in admin_users:
            await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
            return
    else:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
            return
    
    if minutes < 1:
        await interaction.response.send_message("❌ 1分以上を指定してください")
        return
    
    config_manager.set("sleep.timeout_minutes", minutes)
    await config_manager.save_config()
    await interaction.response.send_message(f"✅ 寝落ち監視のタイムアウト時間を {minutes}分 に設定しました")

@bot.event
async def on_raw_reaction_add(payload):
    """リアクション追加イベント"""
    if payload.user_id == bot.user.id:
        return
    
    user_id = str(payload.user_id)
    
    # 寝落ち監視中のユーザーがリアクションした場合
    if user_id in sleep_monitors:
        monitor = sleep_monitors[user_id]
        
        # 監視メッセージへのリアクションか確認
        if monitor['message_id'] == payload.message_id:
            # 監視をキャンセル
            del sleep_monitors[user_id]
            
            guild = bot.get_guild(monitor['guild_id'])
            channel = guild.get_channel(monitor['channel_id'])
            if channel:
                await channel.send(f"✅ {payload.member.display_name} さんがリアクションしたため、監視をキャンセルしました")

# 削除ログのメッセージペアを記録 {notify_msg_id: webhook_msg_id}
_delete_log_pairs: dict[int, int] = {}

@bot.event
async def on_message_delete(message):
    """メッセージ削除イベント：記録・PHP送信・Webhook復元"""
    # DMは無視
    if not message.guild:
        return
    # bot自身の「削除しました」通知メッセージの削除は見逃す（救済措置削除で処理）
    if message.author == bot.user and message.id in _delete_log_pairs:
        return

    try:
        import json, os
        from datetime import datetime

        # 添付ファイル情報
        attachments = [
            {"filename": a.filename, "url": a.url, "proxy_url": a.proxy_url}
            for a in message.attachments
        ]

        deleted_message = {
            "id": str(message.id),
            "content": message.content or "",
            "author": {
                "id": str(message.author.id),
                "name": message.author.name,
                "display_name": message.author.display_name,
                "avatar_url": str(message.author.display_avatar.url) if message.author.display_avatar else "",
                "is_bot": message.author.bot,
            },
            "channel": {
                "id": str(message.channel.id),
                "name": message.channel.name,
            },
            "guild": {
                "id": str(message.guild.id),
                "name": message.guild.name,
            },
            "attachments": attachments,
            "deleted_at": datetime.now().isoformat(),
            "created_at": message.created_at.isoformat(),
        }

        # ── 1. ローカルJSONに保存 ──
        json_file = "delete_message.json"
        existing_data = []
        if os.path.exists(json_file):
            with open(json_file, 'r', encoding='utf-8') as f:
                try:
                    existing_data = json.load(f)
                except Exception:
                    existing_data = []
        existing_data.append(deleted_message)
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(existing_data, f, ensure_ascii=False, indent=2)

        # ── 2. PHPサーバーに送信 ──
        php_url = SERVER_URL.rstrip('/') + '/api/delete_message.php'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    php_url,
                    json=deleted_message,
                    headers={"X-API-Key": API_KEY} if API_KEY else {},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 201, 204):
                        body = await resp.text()
                        logger.warning(f"delete_message.php returned {resp.status}: {body[:200]}")
        except Exception as e:
            logger.error(f"Failed to send deleted message to PHP: {e}")

        # ── 3. 削除者を監査ログから取得（少し待ってから） ──
        await asyncio.sleep(1.5)
        deleter = None
        try:
            now_utc = datetime.utcnow()
            async for entry in message.guild.audit_logs(
                limit=10, action=discord.AuditLogAction.message_delete
            ):
                if entry.target.id != message.author.id:
                    continue
                age = (now_utc - entry.created_at.replace(tzinfo=None)).total_seconds()
                if age < 15:
                    deleter = entry.user
                    break
        except Exception:
            pass

        # ── 4. botが削除通知を送信 ──
        notify_text = f"🗑️ {deleter.mention} がメッセージを削除しました！" if deleter else "🗑️ メッセージが削除されました！"
        notify_msg = None
        try:
            notify_msg = await message.channel.send(
                notify_text,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except Exception as e:
            logger.error(f"Failed to send delete notification: {e}")

        # ── 5. Webhookで元メッセージを復元（通知へのreplyを試みる、無理なら返信なし） ──
        try:
            webhooks = await message.channel.webhooks()
            webhook = next(
                (w for w in webhooks if w.name == "DeleteLog" and w.user == bot.user),
                None
            )
            if webhook is None:
                webhook = await message.channel.create_webhook(name="DeleteLog")

            avatar_url = str(message.author.display_avatar.url) if message.author.display_avatar else None
            webhook_msg = await webhook.send(
                content=message.content or "*(本文なし)*",
                username=message.author.display_name,
                avatar_url=avatar_url,
                wait=True,
            )
            # 通知メッセージIDとWebhookメッセージIDをペアで記録（救済措置用）
            if notify_msg and webhook_msg:
                _delete_log_pairs[notify_msg.id] = webhook_msg.id
        except Exception as e:
            logger.error(f"Failed to restore deleted message via webhook: {e}")

        logger.info(f"Deleted message logged: {message.author.display_name} in #{message.channel.name}")

    except Exception as e:
        logger.error(f"Error in on_message_delete: {e}")

@bot.event
async def on_raw_message_delete(payload):
    """「削除しました」通知自体が削除されたら、対応するWebhookメッセージも削除（救済措置）"""
    msg_id = payload.message_id
    if msg_id not in _delete_log_pairs:
        return

    webhook_msg_id = _delete_log_pairs.pop(msg_id)
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        webhooks = await channel.webhooks()
        webhook = next(
            (w for w in webhooks if w.name == "DeleteLog" and w.user == bot.user),
            None
        )
        if webhook:
            await webhook.delete_message(webhook_msg_id)
    except Exception as e:
        logger.error(f"Failed to delete webhook message on rescue: {e}")

@tasks.loop(minutes=1)
async def check_sleep_timeout():
    """寝落ち監視のタイムアウトチェック"""
    current_time = datetime.now()
    timeout_users = []
    
    # タイムアウト時間を取得（デフォルト10分）
    timeout_minutes = config_manager.get("sleep.timeout_minutes", 10)
    timeout_seconds = timeout_minutes * 60
    
    print(f"[Bot INFO] 寝落ち監視チェック実行: 監視中ユーザー数={len(sleep_monitors)}, タイムアウト={timeout_minutes}分")
    
    for user_id, monitor in sleep_monitors.items():
        elapsed = (current_time - monitor['start_time']).total_seconds()
        print(f"[Bot INFO] ユーザーID={user_id}, 経過時間={elapsed}秒")
        if elapsed >= timeout_seconds:
            timeout_users.append(user_id)
            print(f"[Bot INFO] タイムアウト検知: ユーザーID={user_id}")
    
    for user_id in timeout_users:
        monitor = sleep_monitors.pop(user_id)
        guild = bot.get_guild(monitor['guild_id'])
        if not guild:
            continue
        
        member = guild.get_member(int(user_id))
        if not member:
            continue
        
        # VC切断実行
        try:
            # 権限チェック
            bot_member = guild.me
            if not bot_member.guild_permissions.move_members:
                print(f"[Bot ERROR] Botにメンバー移動権限がありません")
                channel = guild.get_channel(monitor['channel_id'])
                if channel:
                    await channel.send(f"⚠️ {member.display_name} さんが10分間反応しなかったためVC切断しようとしましたが、Botにメンバー移動権限がありません")
                continue
            
            # ロール階層チェック
            if member.top_role >= bot_member.top_role:
                print(f"[Bot ERROR] Botのロールが対象ユーザーより低いため切断できません")
                channel = guild.get_channel(monitor['channel_id'])
                if channel:
                    await channel.send(f"⚠️ {member.display_name} さんが10分間反応しなかったためVC切断しようとしましたが、Botのロールが対象ユーザーより低いため切断できません")
                continue
            
            # VCから切断（キックではなく切断）
            if member.voice:
                await member.move_to(None, reason="寝落ち（10分間反応なし）")
            
            # 寝落ち回数を記録
            sleep_count_key = f"sleep_count.{user_id}"
            sleep_count = config_manager.get(sleep_count_key, 0) + 1
            config_manager.set(sleep_count_key, sleep_count)
            
            # 寝落ち履歴を記録
            sleep_history_key = f"sleep_history.{user_id}"
            sleep_history = config_manager.get(sleep_history_key, [])
            sleep_history.append({
                'timestamp': datetime.now().isoformat(),
                'count': sleep_count,
                'monitor_user_id': monitor['monitor_user_id']
            })
            config_manager.set(sleep_history_key, sleep_history)
            await config_manager.save_config()
            
            # 通知チャンネルに送信
            notification_channel_id = config_manager.get("sleep.notification_channel")
            if notification_channel_id:
                notification_channel = guild.get_channel(int(notification_channel_id))
                if notification_channel:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    await notification_channel.send(f"⚠️ **寝落ち切断:** {member.display_name}\n"
                                                    f"📅 日時: {timestamp}\n"
                                                    f"🔢 回数: {sleep_count}回目")
            
            # 監視チャンネルにも送信
            channel = guild.get_channel(monitor['channel_id'])
            if channel:
                await channel.send(f"⚠️ {member.display_name} さんが10分間反応しなかったためVCから切断しました（{sleep_count}回目）")

            # TTSアナウンス（Botが今いるVCで読み上げ）
            if config_manager.get("tts.enabled", False):
                vc = guild.voice_client
                if vc and vc.is_connected():
                    tts_text = f"{member.display_name}さんは寝落ちたと判定してキックしました"
                    audio_file = await tts_manager.synthesize(tts_text)
                    if audio_file:
                        music_was_playing = vc.is_playing()
                        if music_was_playing:
                            vc.pause()
                            music_state['is_tts_playing'] = True

                        def after_sleep_tts(e):
                            if os.path.exists(audio_file):
                                try:
                                    os.remove(audio_file)
                                except Exception:
                                    pass

                        vc.play(discord.FFmpegPCMAudio(audio_file), after=after_sleep_tts)
                        while vc.is_playing():
                            await asyncio.sleep(0.1)

                        if music_was_playing and vc.is_paused():
                            vc.resume()
                        music_state['is_tts_playing'] = False

        except Exception as e:
            print(f"[Bot ERROR] キック失敗: {e}")

# =====================================================================
# 13. ランダムコマンド
# =====================================================================
@bot.command()
async def random_char(ctx, survivors: int, hunters: int):
    """サバイバーとハンターをランダムに選出"""
    if not config_manager.get("random.enabled", True):
        await ctx.send("❌ ランダム機能は無効です")
        return
    
    survivors_list = config_manager.get("random.survivors", [])
    hunters_list = config_manager.get("random.hunters", [])
    
    if len(survivors_list) < survivors:
        await ctx.send(f"❌ サバイバーのリストが不足しています（必要: {survivors}, 現在: {len(survivors_list)}）")
        return
    
    if len(hunters_list) < hunters:
        await ctx.send(f"❌ ハンターのリストが不足しています（必要: {hunters}, 現在: {len(hunters_list)}）")
        return
    
    selected_survivors = random.sample(survivors_list, survivors)
    selected_hunters = random.sample(hunters_list, hunters)
    
    msg = "🎲 **ランダム選出結果:**\n\n"
    msg += f"**サバイバー ({survivors}人):**\n"
    for i, survivor in enumerate(selected_survivors, 1):
        msg += f"{i}. {survivor}\n"
    msg += f"\n**ハンター ({hunters}人):**\n"
    for i, hunter in enumerate(selected_hunters, 1):
        msg += f"{i}. {hunter}\n"
    
    await ctx.send(msg)

@bot.command()
async def list_survivors(ctx):
    """サバイバーリストを表示"""
    survivors = config_manager.get("random.survivors", [])
    if not survivors:
        await ctx.send("サバイバーリストは空です")
        return
    
    msg = "👥 **サバイバーリスト:**\n"
    for i, survivor in enumerate(survivors, 1):
        msg += f"{i}. {survivor}\n"
    await ctx.send(msg)

@bot.command()
async def list_hunters(ctx):
    """ハンターリストを表示"""
    hunters = config_manager.get("random.hunters", [])
    if not hunters:
        await ctx.send("ハンターリストは空です")
        return
    
    msg = "🎯 **ハンターリスト:**\n"
    for i, hunter in enumerate(hunters, 1):
        msg += f"{i}. {hunter}\n"
    await ctx.send(msg)

@bot.command()
async def add_survivor(ctx, *, name: str):
    """サバイバーを追加（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    survivors = config_manager.get("random.survivors", [])
    if name not in survivors:
        survivors.append(name)
        config_manager.set("random.survivors", survivors)
        await config_manager.save_config()
        await ctx.send(f"✅ サバイバー「{name}」を追加しました")
    else:
        await ctx.send("⚠️ そのサバイバーは既に登録されています")

@bot.command()
async def add_hunter(ctx, *, name: str):
    """ハンターを追加（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    hunters = config_manager.get("random.hunters", [])
    if name not in hunters:
        hunters.append(name)
        config_manager.set("random.hunters", hunters)
        await config_manager.save_config()
        await ctx.send(f"✅ ハンター「{name}」を追加しました")
    else:
        await ctx.send("⚠️ そのハンターは既に登録されています")

@bot.tree.command(name="random_char", description="サバイバーとハンターをランダムに選出")
async def slash_random_char(interaction: discord.Interaction, survivors: int, hunters: int):
    """サバイバーとハンターをランダムに選出（スラッシュコマンド）"""
    if not config_manager.get("random.enabled", True):
        await interaction.response.send_message("❌ ランダム機能は無効です")
        return
    
    survivors_list = config_manager.get("random.survivors", [])
    hunters_list = config_manager.get("random.hunters", [])
    
    if len(survivors_list) < survivors:
        await interaction.response.send_message(f"❌ サバイバーのリストが不足しています（必要: {survivors}, 現在: {len(survivors_list)}）")
        return
    
    if len(hunters_list) < hunters:
        await interaction.response.send_message(f"❌ ハンターのリストが不足しています（必要: {hunters}, 現在: {len(hunters_list)}）")
        return
    
    selected_survivors = random.sample(survivors_list, survivors)
    selected_hunters = random.sample(hunters_list, hunters)
    
    msg = "🎲 **ランダム選出結果:**\n\n"
    msg += f"**サバイバー ({survivors}人):**\n"
    for i, survivor in enumerate(selected_survivors, 1):
        msg += f"{i}. {survivor}\n"
    msg += f"\n**ハンター ({hunters}人):**\n"
    for i, hunter in enumerate(selected_hunters, 1):
        msg += f"{i}. {hunter}\n"
    
    await interaction.response.send_message(msg)

@bot.tree.command(name="list_survivors", description="サバイバーリストを表示")
async def slash_list_survivors(interaction: discord.Interaction):
    """サバイバーリストを表示（スラッシュコマンド）"""
    survivors = config_manager.get("random.survivors", [])
    if not survivors:
        await interaction.response.send_message("サバイバーリストは空です")
        return
    
    msg = "👥 **サバイバーリスト:**\n"
    for i, survivor in enumerate(survivors, 1):
        msg += f"{i}. {survivor}\n"
    await interaction.response.send_message(msg)

@bot.tree.command(name="list_hunters", description="ハンターリストを表示")
async def slash_list_hunters(interaction: discord.Interaction):
    """ハンターリストを表示（スラッシュコマンド）"""
    hunters = config_manager.get("random.hunters", [])
    if not hunters:
        await interaction.response.send_message("ハンターリストは空です")
        return
    
    msg = "🎯 **ハンターリスト:**\n"
    for i, hunter in enumerate(hunters, 1):
        msg += f"{i}. {hunter}\n"
    await interaction.response.send_message(msg)

@bot.tree.command(name="add_survivor", description="サバイバーを追加（管理者用）")
async def slash_add_survivor(interaction: discord.Interaction, name: str):
    """サバイバーを追加（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    survivors = config_manager.get("random.survivors", [])
    if name not in survivors:
        survivors.append(name)
        config_manager.set("random.survivors", survivors)
        await config_manager.save_config()
        await interaction.response.send_message(f"✅ サバイバー「{name}」を追加しました")
    else:
        await interaction.response.send_message("⚠️ そのサバイバーは既に登録されています")

@bot.tree.command(name="add_hunter", description="ハンターを追加（管理者用）")
async def slash_add_hunter(interaction: discord.Interaction, name: str):
    """ハンターを追加（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    hunters = config_manager.get("random.hunters", [])
    if name not in hunters:
        hunters.append(name)
        config_manager.set("random.hunters", hunters)
        await config_manager.save_config()
        await interaction.response.send_message(f"✅ ハンター「{name}」を追加しました")
    else:
        await interaction.response.send_message("⚠️ そのハンターは既に登録されています")

# =====================================================================
# 13b. random_pers / random_char_with_pers コマンド
# =====================================================================
# 人格の選択肢
TOTSUSHI_OPTIONS = ["0", "3", "6", "9", "凸なし"]

def _pick_totsushi_for_group(member_count: int, pick_count: int, max_san_three: int = -1) -> list[list[str]]:
    """
    陣営全体（member_count人）分の人格リストを生成する。
    各プレイヤーはpick_count種（重複なし）を受け取る。
    max_san_three: -1=制限なし / 0=3はなし / N>0=陣営全体で3がN個

    アルゴリズム:
    1. まず全員に3なしで割り当てる
    2. 陣営全体に配置できる3の枠（残り人数）が0より大きければ、
       ランダムに選ばれた枠に3を1枚ずつ差し込む（差し込んだ先の既存値と交換）
    """
    pool_no3 = [x for x in TOTSUSHI_OPTIONS if x != "3"]

    # まず全員を3なしで生成
    results = []
    for _ in range(member_count):
        chosen = random.sample(pool_no3, min(pick_count, len(pool_no3)))
        results.append(chosen)

    if max_san_three != 0:
        # 3を差し込める全スロット一覧 (player_idx, slot_idx)
        all_slots = [(pi, si) for pi in range(member_count) for si in range(pick_count)]

        if max_san_three == -1:
            # 制限なし: 各スロット50%の確率で3に差し替え（各プレイヤー内重複チェック付き）
            random.shuffle(all_slots)
            for pi, si in all_slots:
                if "3" not in results[pi]:
                    if random.random() < 0.5:
                        results[pi][si] = "3"
        else:
            # N個まで: ランダムなスロットをmax_san_three個選んで3を差し込む
            random.shuffle(all_slots)
            placed = 0
            for pi, si in all_slots:
                if placed >= max_san_three:
                    break
                # そのプレイヤーにまだ3がない場合のみ差し込む（1人1枚まで）
                if "3" not in results[pi]:
                    results[pi][si] = "3"
                    placed += 1

    return results

@bot.command()
async def random_pers(ctx, survivors: int, hunters: int, max_san: int = -1, single: int = 0):
    """人格をランダムに選出（鯖狩の人数を指定）
    max_san: 陣営ごとの3の人数（-1=制限なし, 0=3なし, N=陣営内でN個）
    single: 0=1人につき2種(デフォルト), 1=1人につき1種
    例: !random_pers 4 1          (各自2種、3制限なし)
        !random_pers 4 1 1        (各自2種、陣営内3は1個)
        !random_pers 4 1 0 1      (各自1種、3なし)
    """
    if not config_manager.get("random.enabled", True):
        await ctx.send("❌ ランダム機能は無効です")
        return
    if survivors < 0 or hunters < 0:
        await ctx.send("❌ 人数は0以上で指定してください")
        return
    pick_count = 1 if single == 1 else 2
    san_label = f"（3の人数: {max_san}）" if max_san >= 0 else ""
    msg = f"🎲 **人格ランダム結果:**{san_label}\n\n"
    if survivors > 0:
        group = _pick_totsushi_for_group(survivors, pick_count, max_san)
        msg += f"**サバイバー ({survivors}人) — 各自{pick_count}種:**\n"
        for i, chosen in enumerate(group, 1):
            msg += f"{i}. [{' / '.join(chosen)}]\n"
        msg += "\n"
    if hunters > 0:
        group = _pick_totsushi_for_group(hunters, pick_count, max_san)
        msg += f"**ハンター ({hunters}人) — 各自{pick_count}種:**\n"
        for i, chosen in enumerate(group, 1):
            msg += f"{i}. [{' / '.join(chosen)}]\n"
    await ctx.send(msg)

@bot.tree.command(name="random_pers", description="人格をランダムに選出（鯖・狩の人数を指定）")
@discord.app_commands.describe(
    survivors="サバイバー人数",
    hunters="ハンター人数",
    max_san_three="3の人数（-1=制限なし, 0=3なし, N=陣営内でN個）",
    single="1人につきの種数",
)
@discord.app_commands.choices(
    single=[
        discord.app_commands.Choice(name="2種（デフォルト）", value=0),
        discord.app_commands.Choice(name="1種", value=1),
    ]
)
async def slash_random_pers(interaction: discord.Interaction, survivors: int, hunters: int, max_san_three: int = -1, single: int = 0):
    """人格をランダムに選出（スラッシュコマンド）"""
    if not config_manager.get("random.enabled", True):
        await interaction.response.send_message("❌ ランダム機能は無効です")
        return
    if survivors < 0 or hunters < 0:
        await interaction.response.send_message("❌ 人数は0以上で指定してください")
        return
    pick_count = 1 if single == 1 else 2
    san_label = f"（3の人数: {max_san_three}）" if max_san_three >= 0 else ""
    msg = f"🎲 **人格ランダム結果:**{san_label}\n\n"
    if survivors > 0:
        group = _pick_totsushi_for_group(survivors, pick_count, max_san_three)
        msg += f"**サバイバー ({survivors}人) — 各自{pick_count}種:**\n"
        for i, chosen in enumerate(group, 1):
            msg += f"{i}. [{' / '.join(chosen)}]\n"
        msg += "\n"
    if hunters > 0:
        group = _pick_totsushi_for_group(hunters, pick_count, max_san_three)
        msg += f"**ハンター ({hunters}人) — 各自{pick_count}種:**\n"
        for i, chosen in enumerate(group, 1):
            msg += f"{i}. [{' / '.join(chosen)}]\n"
    await interaction.response.send_message(msg)

@bot.command()
async def random_char_pers(ctx, survivors: int, hunters: int, max_san: int = -1, single: int = 0):
    """キャラ + 人格をランダムに選出（鯖狩の人数を指定）
    max_san: 陣営ごとの3の人数（-1=制限なし, 0=3なし, N=陣営内でN個）
    single: 0=人格2種(デフォルト), 1=人格1種
    例: !random_char_pers 4 1
        !random_char_pers 4 1 1
        !random_char_pers 4 1 0 1
    """
    if not config_manager.get("random.enabled", True):
        await ctx.send("❌ ランダム機能は無効です")
        return
    survivors_list = config_manager.get("random.survivors", [])
    hunters_list = config_manager.get("random.hunters", [])
    if len(survivors_list) < survivors:
        await ctx.send(f"❌ サバイバーのリストが不足しています（必要: {survivors}, 現在: {len(survivors_list)}）")
        return
    if len(hunters_list) < hunters:
        await ctx.send(f"❌ ハンターのリストが不足しています（必要: {hunters}, 現在: {len(hunters_list)}）")
        return
    pick_count = 1 if single == 1 else 2
    san_label = f"（3の人数: {max_san}）" if max_san >= 0 else ""
    selected_survivors = random.sample(survivors_list, survivors)
    selected_hunters = random.sample(hunters_list, hunters)
    surv_pers = _pick_totsushi_for_group(survivors, pick_count, max_san)
    hunt_pers = _pick_totsushi_for_group(hunters, pick_count, max_san)
    msg = f"🎲 **キャラ + 人格 ランダム選出結果:**{san_label}\n\n"
    msg += f"**サバイバー ({survivors}人) — 各自人格{pick_count}種:**\n"
    for i, (char, chosen) in enumerate(zip(selected_survivors, surv_pers), 1):
        msg += f"{i}. {char}  [{' / '.join(chosen)}]\n"
    msg += f"\n**ハンター ({hunters}人) — 各自人格{pick_count}種:**\n"
    for i, (char, chosen) in enumerate(zip(selected_hunters, hunt_pers), 1):
        msg += f"{i}. {char}  [{' / '.join(chosen)}]\n"
    await ctx.send(msg)

@bot.tree.command(name="random_char_pers", description="キャラ＋人格をランダムに選出（鯖・狩の人数を指定）")
@discord.app_commands.describe(
    survivors="サバイバー人数",
    hunters="ハンター人数",
    max_san_three="3の人数（-1=制限なし, 0=3なし, N=陣営内でN個）",
    single="1人につきの人格種数",
)
@discord.app_commands.choices(
    single=[
        discord.app_commands.Choice(name="人格2種（デフォルト）", value=0),
        discord.app_commands.Choice(name="人格1種", value=1),
    ]
)
async def slash_random_char_pers(interaction: discord.Interaction, survivors: int, hunters: int, max_san_three: int = -1, single: int = 0):
    """キャラ＋人格をランダムに選出（スラッシュコマンド）"""
    if not config_manager.get("random.enabled", True):
        await interaction.response.send_message("❌ ランダム機能は無効です")
        return
    survivors_list = config_manager.get("random.survivors", [])
    hunters_list = config_manager.get("random.hunters", [])
    if len(survivors_list) < survivors:
        await interaction.response.send_message(f"❌ サバイバーのリストが不足しています（必要: {survivors}, 現在: {len(survivors_list)}）")
        return
    if len(hunters_list) < hunters:
        await interaction.response.send_message(f"❌ ハンターのリストが不足しています（必要: {hunters}, 現在: {len(hunters_list)}）")
        return
    pick_count = 1 if single == 1 else 2
    san_label = f"（3の人数: {max_san_three}）" if max_san_three >= 0 else ""
    selected_survivors = random.sample(survivors_list, survivors)
    selected_hunters = random.sample(hunters_list, hunters)
    surv_pers = _pick_totsushi_for_group(survivors, pick_count, max_san_three)
    hunt_pers = _pick_totsushi_for_group(hunters, pick_count, max_san_three)
    msg = f"🎲 **キャラ + 人格 ランダム選出結果:**{san_label}\n\n"
    msg += f"**サバイバー ({survivors}人) — 各自人格{pick_count}種:**\n"
    for i, (char, chosen) in enumerate(zip(selected_survivors, surv_pers), 1):
        msg += f"{i}. {char}  [{' / '.join(chosen)}]\n"
    msg += f"\n**ハンター ({hunters}人) — 各自人格{pick_count}種:**\n"
    for i, (char, chosen) in enumerate(zip(selected_hunters, hunt_pers), 1):
        msg += f"{i}. {char}  [{' / '.join(chosen)}]\n"
    await interaction.response.send_message(msg)

# =====================================================================
# 13. ファイル出力コマンド（統合版）
# =====================================================================
def _download_embed(summary, download_url):
    """ダウンロード結果メッセージを生成"""
    return (
        f"✅ **ダウンロード完了！**\n"
        f"{summary}\n"
        f"🔗 **プレビュー & ダウンロード:** {download_url}"
    )

async def _run_download(send_fn, query, format_type, quality):
    """共通ダウンロード処理"""
    if not config_manager.get("file_output.enabled", True):
        await send_fn("❌ ファイル出力機能は無効です")
        return

    fmt_label = format_type.upper()
    await send_fn(f"📥 ダウンロード中… (`{fmt_label}` / 品質: `{quality}`)\n⏳ 大きなファイルは時間がかかります")

    download_url, summary, status = await file_output_manager.download_and_upload(
        query, format_type=format_type, quality=quality
    )

    if download_url:
        await send_fn(_download_embed(summary, download_url))
    else:
        await send_fn(f"❌ {status}")

# ---------- プレフィックスコマンド ----------
@bot.command(name="download")
async def cmd_download(ctx, *, args: str):
    """音楽/動画をダウンロード
    使い方: !download <URL or キーワード> [--mp4] [--quality best/high/medium/low/1080p/720p/480p/360p]
    例: !download never gonna give you up --mp3 --quality best
        !download https://youtu.be/xxx --mp4 --quality 1080p
    """
    query, fmt, quality = _parse_download_args(args)
    await _run_download(ctx.send, query, fmt, quality)

# ---------- スラッシュコマンド ----------
@bot.tree.command(name="download", description="音楽・動画をダウンロード（デフォルト: MP3最高品質）")
@discord.app_commands.describe(
    query="URLまたは検索キーワード",
    format_type="フォーマット: mp3（デフォルト）または mp4",
    quality="音質/画質: best(デフォルト) / high / medium / low / 1080p / 720p / 480p / 360p",
)
@discord.app_commands.choices(
    format_type=[
        discord.app_commands.Choice(name="MP3（音楽）", value="mp3"),
        discord.app_commands.Choice(name="MP4（動画）", value="mp4"),
    ],
    quality=[
        discord.app_commands.Choice(name="最高品質（best）",  value="best"),
        discord.app_commands.Choice(name="高品質（high）",    value="high"),
        discord.app_commands.Choice(name="中品質（medium）",  value="medium"),
        discord.app_commands.Choice(name="低品質（low）",     value="low"),
        discord.app_commands.Choice(name="1080p（動画用）",   value="1080p"),
        discord.app_commands.Choice(name="720p（動画用）",    value="720p"),
        discord.app_commands.Choice(name="480p（動画用）",    value="480p"),
        discord.app_commands.Choice(name="360p（動画用）",    value="360p"),
    ],
)
async def slash_download(
    interaction: discord.Interaction,
    query: str,
    format_type: str = "mp3",
    quality: str = "best",
):
    """音楽・動画をダウンロード（スラッシュコマンド）"""
    await interaction.response.defer()
    await _run_download(interaction.followup.send, query, format_type, quality)

def _parse_download_args(args: str):
    """!download のオプション引数をパース"""
    import re
    fmt = "mp3"
    quality = "best"
    # --mp4 / --mp3
    if "--mp4" in args:
        fmt = "mp4"
        args = args.replace("--mp4", "")
    elif "--mp3" in args:
        args = args.replace("--mp3", "")
    # --quality xxx
    m = re.search(r"--quality\s+(\S+)", args)
    if m:
        quality = m.group(1)
        args = args[:m.start()] + args[m.end():]
    return args.strip(), fmt, quality

# =====================================================================
# 14. Config管理コマンド
# =====================================================================
@bot.command()
async def reload_config(ctx):
    """configを再読み込み（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    success = await config_manager.load_config()
    await tts_manager.update_settings()
    
    if success:
        await ctx.send("✅ Configを再読み込みしました")
    else:
        await ctx.send("❌ Configの再読み込みに失敗しました")

@bot.command()
async def show_config(ctx):
    """現在のconfigを表示（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    ticks = chr(96) * 3
    config_json = json.dumps(config_manager.config, ensure_ascii=False, indent=2)
    await ctx.send(f"📋 **現在のConfig:**\n{ticks}json\n{config_json}\n{ticks}")

@bot.command()
async def api_points(ctx):
    """残りAPIポイントを確認（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    points_info = await tts_manager.get_api_points()
    if points_info:
        await ctx.send(f"📊 **Voicevox API残りポイント:**\n{points_info}")
    else:
        await ctx.send("❌ APIポイントの取得に失敗しました")

@bot.tree.command(name="reload_config", description="Configを再読み込み（管理者用）")
async def slash_reload_config(interaction: discord.Interaction):
    """configを再読み込み（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    success = await config_manager.load_config()
    await tts_manager.update_settings()
    
    if success:
        await interaction.response.send_message("✅ Configを再読み込みしました")
    else:
        await interaction.response.send_message("❌ Configの再読み込みに失敗しました")

@bot.tree.command(name="show_config", description="現在のConfigを表示（管理者用）")
async def slash_show_config(interaction: discord.Interaction):
    """現在のconfigを表示（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    ticks = chr(96) * 3
    config_json = json.dumps(config_manager.config, ensure_ascii=False, indent=2)
    
    # Configが長すぎる場合はファイルとして送信
    if len(config_json) > 1800:  # 余裕を持って1800文字
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json', encoding='utf-8') as f:
            f.write(config_json)
            temp_file = f.name
        
        await interaction.response.send_message("📋 **現在のConfig:** (ファイルとして送信)", file=discord.File(temp_file, 'config.json'))
        os.unlink(temp_file)
    else:
        await interaction.response.send_message(f"📋 **現在のConfig:**\n{ticks}json\n{config_json}\n{ticks}")

@bot.tree.command(name="api_points", description="Voicevox API残りポイントを確認（管理者用）")
async def slash_api_points(interaction: discord.Interaction):
    """残りAPIポイントを確認（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    points_info = await tts_manager.get_api_points()
    if points_info:
        await interaction.response.send_message(f"📊 **Voicevox API残りポイント:**\n{points_info}")
    else:
        await interaction.response.send_message("❌ APIポイントの取得に失敗しました")

@bot.command(name="commands")
async def cmd_commands(ctx):
    """ヘルプを表示（カテゴリ選択式）"""
    view = HelpView()
    await ctx.send(embed=HelpView.top_embed(), view=view)

@bot.tree.command(name="help", description="コマンド一覧をカテゴリ別に表示")
async def slash_help(interaction: discord.Interaction):
    """ヘルプを表示（スラッシュコマンド）"""
    view = HelpView()
    await interaction.response.send_message(embed=HelpView.top_embed(), view=view)

# =====================================================================
# Helpカテゴリ定義（SelectMenuビュー）
# =====================================================================
try:
    from help import HELP_CATEGORIES
except ImportError:
    HELP_CATEGORIES = {}

class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=v["label"],
                value=k,
                description=v["description"],
            )
            for k, v in HELP_CATEGORIES.items()
        ]
        super().__init__(
            placeholder="📂 カテゴリを選んでください…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        cat = HELP_CATEGORIES[key]
        embed = discord.Embed(
            title=f"{cat['label']} コマンド一覧",
            description=cat["description"],
            color=cat["color"],
        )
        for name, value in cat["fields"]:
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="カテゴリを再選択するとページが切り替わります")
        await interaction.response.edit_message(embed=embed, view=self.view)

class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(HelpSelect())

    @staticmethod
    def top_embed() -> discord.Embed:
        embed = discord.Embed(
            title="🤖 Bot コマンド一覧",
            description=(
                "[🔧 Botの設定はこちらからでもできます](https://kt69.f5.si/tool/bot)\n\n"
                "下のメニューからカテゴリを選ぶと詳細が表示されます。\n\n"
                + "\n".join(
                    f"**{v['label']}** — {v['description']}"
                    for v in HELP_CATEGORIES.values()
                )
            ),
            color=0x5865F2,
        )
        embed.set_footer(text="スラッシュコマンド（/）とプレフィックスコマンド（!）どちらでも使えます")
        return embed

# =====================================================================
# 16. システム管理コマンド
# =====================================================================
@bot.command()
async def sys_info(ctx):
    """システム情報を表示"""
    
    try:
        # CPU情報
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_count = psutil.cpu_count()
        
        # メモリ情報
        mem = psutil.virtual_memory()
        mem_total = mem.total / (1024 ** 3)  # GB
        mem_used = mem.used / (1024 ** 3)  # GB
        mem_percent = mem.percent
        
        # ディスク情報
        disk = psutil.disk_usage('/')
        disk_total = disk.total / (1024 ** 3)  # GB
        disk_used = disk.used / (1024 ** 3)  # GB
        disk_percent = disk.percent
        
        # システム情報
        system = platform.system()
        release = platform.release()
        machine = platform.machine()
        
        # Botの稼働時間
        uptime = datetime.now() - datetime.fromtimestamp(psutil.boot_time())
        uptime_str = str(uptime).split('.')[0]
        
        msg = f"""
📊 **システム情報**

💻 **OS:** {system} {release} ({machine})
⏱️ **稼働時間:** {uptime_str}

🔥 **CPU:**
• 使用率: {cpu_percent}%
• コア数: {cpu_count}

💾 **メモリ:**
• 使用量: {mem_used:.2f} GB / {mem_total:.2f} GB
• 使用率: {mem_percent}%

💿 **ディスク:**
• 使用量: {disk_used:.2f} GB / {disk_total:.2f} GB
• 使用率: {disk_percent}%
"""
        await ctx.send(msg)
    except Exception as e:
        await ctx.send(f"❌ システム情報の取得に失敗しました: {e}")

@bot.command()
async def reboot(ctx):
    """Botを再起動（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    await ctx.send("🔄 Botを再起動します...")
    await bot.close()
    # 再起動はsystemdやsupervisorなどで管理する必要があります
    # ここではプロセスを終了するだけです
    sys.exit(0)

@bot.command()
async def admin_add_user(ctx, member: discord.Member):
    """ユーザーを管理者に追加（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    user_id = str(member.id)
    if user_id in admin_users:
        await ctx.send(f"⚠️ {member.display_name} は既に管理者です")
        return
    
    admin_users.append(user_id)
    config_manager.set("bot.admin_users", admin_users)
    await config_manager.save_config()
    await ctx.send(f"✅ {member.display_name} を管理者に追加しました")

@bot.command()
async def admin_remove_user(ctx, member: discord.Member):
    """ユーザーを管理者から削除（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    user_id = str(member.id)
    if user_id not in admin_users:
        await ctx.send(f"⚠️ {member.display_name} は管理者ではありません")
        return
    
    admin_users.remove(user_id)
    config_manager.set("bot.admin_users", admin_users)
    await config_manager.save_config()
    await ctx.send(f"✅ {member.display_name} を管理者から削除しました")

@bot.command()
async def admin_list(ctx):
    """管理者一覧を表示（管理者用）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(ctx.author.id) not in admin_users:
        await ctx.send("❌ このコマンドは管理者のみ使用可能です")
        return
    
    if not admin_users:
        await ctx.send("📋 管理者は設定されていません")
        return
    
    msg = "📋 **管理者一覧:**\n"
    for user_id in admin_users:
        try:
            user = await bot.fetch_user(int(user_id))
            msg += f"• {user.display_name} (ID: {user_id})\n"
        except:
            msg += f"• 不明なユーザー (ID: {user_id})\n"
    await ctx.send(msg)

@bot.tree.command(name="admin_add_user", description="ユーザーを管理者に追加（管理者用）")
async def slash_admin_add_user(interaction: discord.Interaction, member: discord.Member):
    """ユーザーを管理者に追加（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    user_id = str(member.id)
    if user_id in admin_users:
        await interaction.response.send_message(f"⚠️ {member.display_name} は既に管理者です")
        return
    
    admin_users.append(user_id)
    config_manager.set("bot.admin_users", admin_users)
    await config_manager.save_config()
    await interaction.response.send_message(f"✅ {member.display_name} を管理者に追加しました")

@bot.tree.command(name="admin_remove_user", description="ユーザーを管理者から削除（管理者用）")
async def slash_admin_remove_user(interaction: discord.Interaction, member: discord.Member):
    """ユーザーを管理者から削除（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    user_id = str(member.id)
    if user_id not in admin_users:
        await interaction.response.send_message(f"⚠️ {member.display_name} は管理者ではありません")
        return
    
    admin_users.remove(user_id)
    config_manager.set("bot.admin_users", admin_users)
    await config_manager.save_config()
    await interaction.response.send_message(f"✅ {member.display_name} を管理者から削除しました")

@bot.tree.command(name="admin_list", description="管理者一覧を表示（管理者用）")
async def slash_admin_list(interaction: discord.Interaction):
    """管理者一覧を表示（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    if not admin_users:
        await interaction.response.send_message("📋 管理者は設定されていません")
        return
    
    msg = "📋 **管理者一覧:**\n"
    for user_id in admin_users:
        try:
            user = await bot.fetch_user(int(user_id))
            msg += f"• {user.display_name} (ID: {user_id})\n"
        except:
            msg += f"• 不明なユーザー (ID: {user_id})\n"
    await interaction.response.send_message(msg)

@bot.tree.command(name="sys_info", description="システム情報を表示")
async def slash_sys_info(interaction: discord.Interaction):
    """システム情報を表示（スラッシュコマンド）"""
    
    try:
        # CPU情報
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_count = psutil.cpu_count()
        
        # メモリ情報
        mem = psutil.virtual_memory()
        mem_total = mem.total / (1024 ** 3)  # GB
        mem_used = mem.used / (1024 ** 3)  # GB
        mem_percent = mem.percent
        
        # ディスク情報
        disk = psutil.disk_usage('/')
        disk_total = disk.total / (1024 ** 3)  # GB
        disk_used = disk.used / (1024 ** 3)  # GB
        disk_percent = disk.percent
        
        # システム情報
        system = platform.system()
        release = platform.release()
        machine = platform.machine()
        
        # Botの稼働時間
        uptime = datetime.now() - datetime.fromtimestamp(psutil.boot_time())
        uptime_str = str(uptime).split('.')[0]
        
        msg = f"""
📊 **システム情報**

💻 **OS:** {system} {release} ({machine})
⏱️ **稼働時間:** {uptime_str}

🔥 **CPU:**
• 使用率: {cpu_percent}%
• コア数: {cpu_count}

💾 **メモリ:**
• 使用量: {mem_used:.2f} GB / {mem_total:.2f} GB
• 使用率: {mem_percent}%

💿 **ディスク:**
• 使用量: {disk_used:.2f} GB / {disk_total:.2f} GB
• 使用率: {disk_percent}%
"""
        await interaction.response.send_message(msg)
    except Exception as e:
        await interaction.response.send_message(f"❌ システム情報の取得に失敗しました: {e}")

@bot.tree.command(name="reboot", description="Botを再起動（管理者用）")
async def slash_reboot(interaction: discord.Interaction):
    """Botを再起動（スラッシュコマンド）"""
    admin_users = config_manager.get("bot.admin_users", [])
    if admin_users and str(interaction.user.id) not in admin_users:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用可能です")
        return
    
    await interaction.response.send_message("🔄 Botを再起動します...")
    await bot.close()
    sys.exit(0)

@bot.command()
async def user_info(ctx, member: discord.Member = None):
    """ユーザー情報を表示"""
    if member is None:
        member = ctx.author
    
    # ステータス
    status_emoji = {
        discord.Status.online: "🟢 オンライン",
        discord.Status.idle: "🌴 退席中",
        discord.Status.dnd: "⛔ 取り込み中",
        discord.Status.offline: "⚫ オフライン",
    }
    status_text = status_emoji.get(member.status, "❓ 不明")
    
    # アクティビティ
    activity = member.activity
    if activity:
        if isinstance(activity, discord.Game):
            activity_text = f"🎮 {activity.name}"
        elif isinstance(activity, discord.Streaming):
            activity_text = f"📺 {activity.name} (配信中)"
        elif isinstance(activity, discord.CustomActivity):
            activity_text = f"📝 {activity.name}"
        elif isinstance(activity, discord.Spotify):
            activity_text = f"🎵 {activity.artist} - {activity.title}"
        else:
            activity_text = f"🎯 {activity.name}"
    else:
        activity_text = "なし"
    
    # ロール
    roles = [role.name for role in member.roles if role.name != "@everyone"]
    roles_text = ", ".join(roles) if roles else "なし"
    
    # サーバー参加日
    joined_at = member.joined_at.strftime("%Y年%m月%d日 %H:%M:%S") if member.joined_at else "不明"
    
    # アカウント作成日
    created_at = member.created_at.strftime("%Y年%m月%d日 %H:%M:%S")
    
    # ボイスチャンネル
    vc_state = member.voice
    if vc_state and vc_state.channel:
        vc_text = f"🔊 {vc_state.channel.name}"
    else:
        vc_text = "なし"
    
    # 寝落ち回数
    user_id = str(member.id)
    sleep_count = config_manager.get(f"sleep_count.{user_id}", 0)
    
    # Embed表示
    embed = discord.Embed(
        title=f"👤 {member.display_name} のユーザー情報",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
    embed.add_field(name="🆔 ユーザーID", value=member.id, inline=True)
    embed.add_field(name="📝 ユーザー名", value=member.name, inline=True)
    embed.add_field(name="🏷️ 表示名", value=member.display_name, inline=True)
    embed.add_field(name="📊 ステータス", value=status_text, inline=True)
    embed.add_field(name="🎯 アクティビティ", value=activity_text, inline=True)
    embed.add_field(name="🔊 ボイスチャンネル", value=vc_text, inline=True)
    embed.add_field(name="😴 寝落ち回数", value=f"{sleep_count}回", inline=True)
    embed.add_field(name="📅 サーバー参加日", value=joined_at, inline=False)
    embed.add_field(name="🌐 アカウント作成日", value=created_at, inline=False)
    embed.add_field(name="🎭 ロール", value=roles_text, inline=False)
    embed.set_footer(text=f"ID: {member.id}")
    embed.timestamp = datetime.now()
    
    await ctx.send(embed=embed)

@bot.tree.command(name="user_info", description="ユーザー情報を表示")
async def slash_user_info(interaction: discord.Interaction, member: discord.Member = None):
    """ユーザー情報を表示（スラッシュコマンド）"""
    if member is None:
        member = interaction.user
    
    # ステータス
    status_emoji = {
        discord.Status.online: "🟢 オンライン",
        discord.Status.idle: "🌴 退席中",
        discord.Status.dnd: "⛔ 取り込み中",
        discord.Status.offline: "⚫ オフライン",
    }
    status_text = status_emoji.get(member.status, "❓ 不明")
    
    # アクティビティ
    activity = member.activity
    if activity:
        if isinstance(activity, discord.Game):
            activity_text = f"🎮 {activity.name}"
        elif isinstance(activity, discord.Streaming):
            activity_text = f"📺 {activity.name} (配信中)"
        elif isinstance(activity, discord.CustomActivity):
            activity_text = f"📝 {activity.name}"
        elif isinstance(activity, discord.Spotify):
            activity_text = f"🎵 {activity.artist} - {activity.title}"
        else:
            activity_text = f"🎯 {activity.name}"
    else:
        activity_text = "なし"
    
    # ロール
    roles = [role.name for role in member.roles if role.name != "@everyone"]
    roles_text = ", ".join(roles) if roles else "なし"
    
    # サーバー参加日
    joined_at = member.joined_at.strftime("%Y年%m月%d日 %H:%M:%S") if member.joined_at else "不明"
    
    # アカウント作成日
    created_at = member.created_at.strftime("%Y年%m月%d日 %H:%M:%S")
    
    # ボイスチャンネル
    vc_state = member.voice
    if vc_state and vc_state.channel:
        vc_text = f"🔊 {vc_state.channel.name}"
    else:
        vc_text = "なし"
    
    # 寝落ち回数
    user_id = str(member.id)
    sleep_count = config_manager.get(f"sleep_count.{user_id}", 0)
    
    # Embed表示
    embed = discord.Embed(
        title=f"👤 {member.display_name} のユーザー情報",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
    embed.add_field(name="🆔 ユーザーID", value=member.id, inline=True)
    embed.add_field(name="📝 ユーザー名", value=member.name, inline=True)
    embed.add_field(name="🏷️ 表示名", value=member.display_name, inline=True)
    embed.add_field(name="📊 ステータス", value=status_text, inline=True)
    embed.add_field(name="🎯 アクティビティ", value=activity_text, inline=True)
    embed.add_field(name="🔊 ボイスチャンネル", value=vc_text, inline=True)
    embed.add_field(name="😴 寝落ち回数", value=f"{sleep_count}回", inline=True)
    embed.add_field(name="📅 サーバー参加日", value=joined_at, inline=False)
    embed.add_field(name="🌐 アカウント作成日", value=created_at, inline=False)
    embed.add_field(name="🎭 ロール", value=roles_text, inline=False)
    embed.set_footer(text=f"ID: {member.id}")
    embed.timestamp = datetime.now()
    
    await interaction.response.send_message(embed=embed)

# =====================================================================
# 15. Bot起動
# =====================================================================



if __name__ == "__main__":
    if not TOKEN:
        print("❌ エラー: DISCORD_TOKEN環境変数が設定されていません")
        sys.exit(1)
    
    bot.run(TOKEN)