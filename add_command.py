"""
add_command.py  — bot.py の拡張コマンド（Cog）
bot.py の on_ready で自動的に読み込まれます。
新しいコマンドはこのファイルに追加してください。bot.py は触らなくてOKです。

収録コマンド:
  /daifugo start  — 大富豪を開始
  /daifugo join   — ゲームに参加
  /daifugo begin  — ゲーム開始（カード配布）
  /daifugo play   — カードを出す
  /daifugo pass   — パスする
  /daifugo hand   — 自分の手札を確認（DMに送信）
  /daifugo status — 現在の場の状態を確認
  /daifugo end    — ゲームを強制終了
"""

import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
from typing import Optional

# =========================================================
# カード定義
# =========================================================
SUITS = ["♠", "♥", "♦", "♣"]
NUMBERS = ["3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2"]
# 強さ: 3=0 … 2=12, JOKER=13
STRENGTH = {n: i for i, n in enumerate(NUMBERS)}
STRENGTH["JOKER"] = 13

RANK_NAMES = {
    0: "大富豪 👑",
    1: "富豪 🥇",
    -2: "貧民 🥉",
    -1: "大貧民 💀",
}

def card_str(card: dict) -> str:
    if card["suit"] == "JOKER":
        return "🃏JOKER"
    return f"{card['suit']}{card['number']}"

def card_strength(card: dict, revolution: bool = False) -> int:
    s = STRENGTH[card["number"]] if card["suit"] != "JOKER" else 13
    return (12 - s) if revolution else s

def sort_hand(hand: list, revolution: bool = False) -> list:
    return sorted(hand, key=lambda c: card_strength(c, revolution))

def make_deck() -> list:
    deck = [{"suit": s, "number": n} for s in SUITS for n in NUMBERS]
    deck.append({"suit": "JOKER", "number": "JOKER"})
    random.shuffle(deck)
    return deck

# =========================================================
# ゲーム状態
# =========================================================
class DaifugoGame:
    def __init__(self, channel_id: int, host_id: int):
        self.channel_id   = channel_id
        self.host_id      = host_id
        self.players: list[int] = []          # 参加ユーザーIDリスト（順番）
        self.hands: dict[int, list]  = {}     # player_id -> [card, ...]
        self.started      = False
        self.finished_order: list[int] = []   # あがった順

        # 場の状態
        self.field: list[dict]  = []          # 現在出されているカード
        self.field_owner: Optional[int] = None
        self.current_idx  = 0                 # 現在のプレイヤーインデックス
        self.pass_count   = 0
        self.revolution   = False             # 革命中か
        self.locked_count: Optional[int] = None  # 場のカード枚数（ロック）

    @property
    def current_player(self) -> int:
        active = self.active_players
        if not active:
            return -1
        return active[self.current_idx % len(active)]

    @property
    def active_players(self) -> list[int]:
        """まだ手札があるプレイヤー"""
        return [p for p in self.players if p not in self.finished_order]

    def deal(self):
        deck = make_deck()
        n = len(self.players)
        for i, pid in enumerate(self.players):
            self.hands[pid] = deck[i::n]
            self.hands[pid] = sort_hand(self.hands[pid], self.revolution)
        # 3♣持ちが最初
        for i, pid in enumerate(self.players):
            for c in self.hands[pid]:
                if c["suit"] == "♣" and c["number"] == "3":
                    self.current_idx = i
                    return
        self.current_idx = 0

    def can_play(self, cards: list[dict]) -> tuple[bool, str]:
        """出せるかチェック。(ok, reason)"""
        n = len(cards)
        if n == 0:
            return False, "カードを1枚以上指定してください"

        # ジョーカー単枚は常に出せる
        if n == 1 and cards[0]["suit"] == "JOKER":
            if self.field and self.locked_count and self.locked_count != 1:
                return False, f"場は{self.locked_count}枚出しです"
            return True, ""

        # 同じ数字かチェック
        nums = set(c["number"] for c in cards if c["suit"] != "JOKER")
        jokers = [c for c in cards if c["suit"] == "JOKER"]
        if len(nums) > 1:
            return False, "同じ数字のカードのみ出せます（ジョーカーは何にでも使えます）"

        # 場にカードがある場合の枚数チェック
        if self.field:
            if self.locked_count and n != self.locked_count:
                return False, f"場は{self.locked_count}枚出しです（{n}枚は出せません）"

            # 強さチェック
            my_strength    = card_strength(cards[0], self.revolution)
            field_strength = card_strength(self.field[0], self.revolution)
            if jokers and not nums:
                # ジョーカーのみ → 最強扱い
                my_strength = 99
            if my_strength <= field_strength:
                return False, "場のカードより強いカードを出してください"

        return True, ""

    def play_cards(self, player_id: int, cards: list[dict]) -> dict:
        """
        カードを出す。
        返り値: {"eight_cut": bool, "revolution": bool, "finished": bool, "game_over": bool}
        """
        result = {"eight_cut": False, "revolution": False, "finished": False, "game_over": False}

        # 手札から削除
        for c in cards:
            self.hands[player_id].remove(c)
        self.hands[player_id] = sort_hand(self.hands[player_id], self.revolution)

        self.field = cards
        self.field_owner = player_id
        self.pass_count = 0
        self.locked_count = len(cards)

        # 革命チェック（4枚以上同じ数字）
        nums = [c["number"] for c in cards if c["suit"] != "JOKER"]
        if len(cards) >= 4 and len(set(nums)) == 1:
            self.revolution = not self.revolution
            result["revolution"] = True

        # 8切りチェック
        if nums and nums[0] == "8":
            result["eight_cut"] = True
            self.field = []
            self.locked_count = None
            self.field_owner = None

        # あがりチェック
        if not self.hands[player_id]:
            self.finished_order.append(player_id)
            result["finished"] = True

        # 全員あがったかチェック
        if len(self.active_players) <= 1:
            # 最後の1人は大貧民
            for p in self.players:
                if p not in self.finished_order:
                    self.finished_order.append(p)
            result["game_over"] = True
            return result

        # 次のプレイヤーへ
        active = self.active_players
        if result["eight_cut"] or result["finished"]:
            # 8切りであがった人の次から、またはあがった人をスキップして次
            if result["eight_cut"]:
                # 8切り: 出した人が次の先行（場をリセット済み）
                self.current_idx = active.index(player_id) if player_id in active else 0
            else:
                idx = 0 if not active else (self.current_idx % len(active))
                self.current_idx = idx
        else:
            self.current_idx = (active.index(player_id) + 1) % len(active)

        return result

    def do_pass(self, player_id: int) -> bool:
        """パス。全員パスしたら場をリセット。Trueなら場リセット。"""
        self.pass_count += 1
        active = self.active_players
        # 自分以外の全員がパスした = 場のオーナーに戻ってきた
        if self.pass_count >= len(active) - 1:
            self.field = []
            self.field_owner = None
            self.locked_count = None
            self.pass_count = 0
            # 場を流した人（field_owner）が先行
            if self.field_owner and self.field_owner in active:
                self.current_idx = active.index(self.field_owner)
            else:
                # field_ownerがいなければ次
                self.current_idx = (active.index(player_id) + 1) % len(active)
            return True
        else:
            self.current_idx = (active.index(player_id) + 1) % len(active)
            return False

    def rank_result(self) -> str:
        n = len(self.finished_order)
        lines = []
        for i, pid in enumerate(self.finished_order):
            if i == 0:
                rank = "大富豪 👑"
            elif i == 1 and n >= 4:
                rank = "富豪 🥇"
            elif i == n - 2 and n >= 4:
                rank = "貧民 🥉"
            elif i == n - 1:
                rank = "大貧民 💀"
            else:
                rank = f"平民 ({i+1}位)"
            lines.append(f"{rank} — <@{pid}>")
        return "\n".join(lines)


# =========================================================
# Cog
# =========================================================
class AddCommandCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, DaifugoGame] = {}  # channel_id -> game

    # -------------------------------------------------------
    # スラッシュコマンドグループ
    # -------------------------------------------------------
    daifugo = app_commands.Group(name="daifugo", description="🃏 大富豪ゲーム")

    @daifugo.command(name="start", description="大富豪を開始（募集開始）")
    async def daifugo_start(self, interaction: discord.Interaction):
        cid = interaction.channel_id
        if cid in self.games:
            await interaction.response.send_message("❌ このチャンネルでは既にゲームが進行中です。`/daifugo end` で終了してください。", ephemeral=True)
            return
        game = DaifugoGame(cid, interaction.user.id)
        game.players.append(interaction.user.id)
        self.games[cid] = game
        await interaction.response.send_message(
            f"🃏 **大富豪 — 参加者募集中！**\n"
            f"ホスト: {interaction.user.mention}\n\n"
            f"参加するには `/daifugo join` を実行してください。\n"
            f"2〜8人で遊べます。全員揃ったら `/daifugo begin` でゲーム開始！"
        )

    @daifugo.command(name="join", description="大富豪に参加する")
    async def daifugo_join(self, interaction: discord.Interaction):
        cid = interaction.channel_id
        game = self.games.get(cid)
        if not game:
            await interaction.response.send_message("❌ ゲームが見つかりません。`/daifugo start` で開始してください。", ephemeral=True)
            return
        if game.started:
            await interaction.response.send_message("❌ ゲームは既に始まっています。", ephemeral=True)
            return
        if interaction.user.id in game.players:
            await interaction.response.send_message("❌ 既に参加しています。", ephemeral=True)
            return
        if len(game.players) >= 8:
            await interaction.response.send_message("❌ 参加人数が上限（8人）に達しています。", ephemeral=True)
            return
        game.players.append(interaction.user.id)
        mentions = " / ".join(f"<@{p}>" for p in game.players)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} が参加しました！\n"
            f"現在の参加者 ({len(game.players)}人): {mentions}"
        )

    @daifugo.command(name="begin", description="カードを配ってゲームを開始する（ホストのみ）")
    async def daifugo_begin(self, interaction: discord.Interaction):
        cid = interaction.channel_id
        game = self.games.get(cid)
        if not game:
            await interaction.response.send_message("❌ ゲームが見つかりません。", ephemeral=True)
            return
        if game.host_id != interaction.user.id:
            await interaction.response.send_message("❌ ホストのみがゲームを開始できます。", ephemeral=True)
            return
        if game.started:
            await interaction.response.send_message("❌ ゲームは既に始まっています。", ephemeral=True)
            return
        if len(game.players) < 2:
            await interaction.response.send_message("❌ 2人以上必要です。", ephemeral=True)
            return

        game.started = True
        random.shuffle(game.players)
        game.deal()

        await interaction.response.send_message(
            f"🃏 **大富豪 スタート！** ({len(game.players)}人)\n"
            f"参加順: {' → '.join(f'<@{p}>' for p in game.players)}\n\n"
            f"🃏 手札はDMで確認してください（`/daifugo hand`）\n"
            f"**♣3 を持っている <@{game.current_player}> から開始！**"
        )
        # 全員にDMで手札を送る
        await self._dm_all_hands(interaction, game)

    @daifugo.command(name="hand", description="自分の手札をDMで確認する")
    async def daifugo_hand(self, interaction: discord.Interaction):
        cid = interaction.channel_id
        game = self.games.get(cid)
        if not game or not game.started:
            await interaction.response.send_message("❌ ゲームが進行中ではありません。", ephemeral=True)
            return
        if interaction.user.id not in game.players:
            await interaction.response.send_message("❌ あなたはこのゲームに参加していません。", ephemeral=True)
            return
        hand = game.hands.get(interaction.user.id, [])
        if not hand:
            await interaction.response.send_message("あなたはすでにあがっています！", ephemeral=True)
            return
        hand_str = self._hand_to_str(hand)
        try:
            await interaction.user.send(f"🃏 **あなたの手札** ({len(hand)}枚):\n{hand_str}")
            await interaction.response.send_message("📬 DMに手札を送りました！", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"🃏 **あなたの手札** ({len(hand)}枚):\n{hand_str}", ephemeral=True
            )

    @daifugo.command(name="play", description="カードを出す（例: ♠3 または ♠3,♥3）")
    @app_commands.describe(cards="出すカードをカンマ区切りで（例: ♠3,♥3 / JOKER）")
    async def daifugo_play(self, interaction: discord.Interaction, cards: str):
        cid = interaction.channel_id
        game = self.games.get(cid)
        if not game or not game.started:
            await interaction.response.send_message("❌ ゲームが進行中ではありません。", ephemeral=True)
            return
        if interaction.user.id not in game.players:
            await interaction.response.send_message("❌ あなたはこのゲームに参加していません。", ephemeral=True)
            return
        if interaction.user.id in game.finished_order:
            await interaction.response.send_message("✅ あなたはすでにあがっています。", ephemeral=True)
            return
        if game.current_player != interaction.user.id:
            await interaction.response.send_message(
                f"❌ 今は <@{game.current_player}> のターンです。", ephemeral=True
            )
            return

        # カードのパース
        parsed, err = self._parse_cards(cards, game.hands[interaction.user.id])
        if err:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return

        ok, reason = game.can_play(parsed)
        if not ok:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return

        result = game.play_cards(interaction.user.id, parsed)
        cards_str = " ".join(card_str(c) for c in parsed)

        msg = f"<@{interaction.user.id}> が **{cards_str}** を出しました！"
        if result["revolution"]:
            game.revolution = not game.revolution  # play_cardsで既にflipされているので戻す必要はない
            msg += "\n🔄 **革命！** カードの強さが逆転しました！"
        if result["eight_cut"]:
            msg += "\n✂️ **8切り！** 場をリセット。"
        if result["finished"]:
            rank_num = len(game.finished_order) - 1
            n = len(game.players)
            if rank_num == 0:
                rank_label = "大富豪 👑"
            elif rank_num == 1 and n >= 4:
                rank_label = "富豪 🥇"
            else:
                rank_label = f"あがり ({rank_num+1}位) 🎉"
            msg += f"\n🎉 <@{interaction.user.id}> が **{rank_label}** であがりました！"

        if result["game_over"]:
            msg += f"\n\n🏁 **ゲーム終了！**\n{game.rank_result()}"
            del self.games[cid]
            await interaction.response.send_message(msg)
            return

        # 次のプレイヤー情報を追加
        msg += f"\n\n▶️ 次は <@{game.current_player}> のターンです。"
        if game.field:
            field_str = " ".join(card_str(c) for c in game.field)
            rev_str = " 🔄革命中" if game.revolution else ""
            msg += f"\n📋 場: **{field_str}**{rev_str}"
        else:
            msg += "\n📋 場: **（なし）** — 自由に出せます"

        await interaction.response.send_message(msg)

    @daifugo.command(name="pass", description="このターンをパスする")
    async def daifugo_pass(self, interaction: discord.Interaction):
        cid = interaction.channel_id
        game = self.games.get(cid)
        if not game or not game.started:
            await interaction.response.send_message("❌ ゲームが進行中ではありません。", ephemeral=True)
            return
        if interaction.user.id not in game.players:
            await interaction.response.send_message("❌ あなたはこのゲームに参加していません。", ephemeral=True)
            return
        if interaction.user.id in game.finished_order:
            await interaction.response.send_message("✅ あなたはすでにあがっています。", ephemeral=True)
            return
        if game.current_player != interaction.user.id:
            await interaction.response.send_message(
                f"❌ 今は <@{game.current_player}> のターンです。", ephemeral=True
            )
            return
        if not game.field:
            await interaction.response.send_message("❌ 場が空のときはパスできません。必ずカードを出してください。", ephemeral=True)
            return

        reset = game.do_pass(interaction.user.id)
        msg = f"<@{interaction.user.id}> がパスしました。"
        if reset:
            msg += "\n🔄 全員パス！場をリセット。"
            msg += f"\n▶️ <@{game.current_player}> から再開です。"
        else:
            msg += f"\n▶️ 次は <@{game.current_player}> のターンです。"
            if game.field:
                field_str = " ".join(card_str(c) for c in game.field)
                msg += f"\n📋 場: **{field_str}**"
        await interaction.response.send_message(msg)

    @daifugo.command(name="status", description="現在の場の状態・プレイヤー情報を確認")
    async def daifugo_status(self, interaction: discord.Interaction):
        cid = interaction.channel_id
        game = self.games.get(cid)
        if not game:
            await interaction.response.send_message("❌ ゲームが見つかりません。", ephemeral=True)
            return
        if not game.started:
            participants = " / ".join(f"<@{p}>" for p in game.players)
            await interaction.response.send_message(
                f"📋 **大富豪 — 参加待ち**\n参加者 ({len(game.players)}人): {participants}",
                ephemeral=True
            )
            return

        # 場の情報
        field_str = " ".join(card_str(c) for c in game.field) if game.field else "（なし）"
        rev_str = " 🔄**革命中**" if game.revolution else ""

        # プレイヤーごとの手札枚数
        lines = []
        for pid in game.players:
            if pid in game.finished_order:
                rank_i = game.finished_order.index(pid)
                n = len(game.players)
                if rank_i == 0:       rl = "大富豪👑"
                elif rank_i == 1 and n >= 4: rl = "富豪🥇"
                else: rl = f"あがり{rank_i+1}位"
                lines.append(f"✅ <@{pid}> — {rl}")
            else:
                cnt = len(game.hands.get(pid, []))
                cur = " ◀️ 今のターン" if pid == game.current_player else ""
                lines.append(f"🃏 <@{pid}> — {cnt}枚{cur}")

        await interaction.response.send_message(
            f"📋 **大富豪 — ゲーム状況**\n"
            f"場: **{field_str}**{rev_str}\n\n"
            + "\n".join(lines),
            ephemeral=True
        )

    @daifugo.command(name="end", description="ゲームを強制終了する（ホストのみ）")
    async def daifugo_end(self, interaction: discord.Interaction):
        cid = interaction.channel_id
        game = self.games.get(cid)
        if not game:
            await interaction.response.send_message("❌ 進行中のゲームがありません。", ephemeral=True)
            return
        if game.host_id != interaction.user.id:
            # 管理者権限があれば許可
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("❌ ホストのみがゲームを終了できます。", ephemeral=True)
                return
        del self.games[cid]
        await interaction.response.send_message("🛑 ゲームを強制終了しました。")

    # -------------------------------------------------------
    # ユーティリティ
    # -------------------------------------------------------
    def _hand_to_str(self, hand: list) -> str:
        """手札を番号付きの文字列に変換"""
        lines = []
        for i, c in enumerate(hand, 1):
            lines.append(f"`{i:2d}.` {card_str(c)}")
        # 2列に並べる
        cols = []
        for i in range(0, len(lines), 2):
            row = lines[i]
            if i + 1 < len(lines):
                row += "　" + lines[i + 1]
            cols.append(row)
        return "\n".join(cols)

    def _parse_cards(self, text: str, hand: list) -> tuple[list, str]:
        """
        "♠3,♥3" や "1,2" (インデックス) や "JOKER" を手札と照合してパース。
        (cards, error_str) を返す。
        """
        tokens = [t.strip() for t in text.replace("、", ",").split(",")]
        result = []
        used_indices = set()

        for token in tokens:
            if not token:
                continue
            # インデックス指定（1始まり）
            if token.isdigit():
                idx = int(token) - 1
                if idx < 0 or idx >= len(hand):
                    return [], f"番号 {token} は手札にありません（1〜{len(hand)}で指定）"
                if idx in used_indices:
                    return [], f"番号 {token} を重複指定しています"
                used_indices.add(idx)
                result.append(hand[idx])
                continue

            # カード文字列指定（♠3, JOKER など）
            matched_idx = None
            token_upper = token.upper()
            for i, c in enumerate(hand):
                if i in used_indices:
                    continue
                cs = card_str(c).upper().replace(" ", "")
                if cs == token_upper.replace(" ", ""):
                    matched_idx = i
                    break
            if matched_idx is None:
                return [], f"カード「{token}」が手札に見つかりません\n手札の番号（例: 1,2）またはカード名（例: ♠3,JOKER）で指定してください"
            used_indices.add(matched_idx)
            result.append(hand[matched_idx])

        if not result:
            return [], "カードを指定してください"
        return result, ""

    async def _dm_all_hands(self, interaction: discord.Interaction, game: DaifugoGame):
        """全プレイヤーにDMで手札を送る"""
        failed = []
        for pid in game.players:
            hand = game.hands.get(pid, [])
            hand_str = self._hand_to_str(hand)
            try:
                user = await self.bot.fetch_user(pid)
                await user.send(
                    f"🃏 **大富豪 — あなたの手札** ({len(hand)}枚):\n{hand_str}\n\n"
                    f"カードを出すには `/daifugo play` で番号またはカード名を指定します\n"
                    f"例: `/daifugo play 1` または `/daifugo play ♠3,♥3`"
                )
            except discord.Forbidden:
                failed.append(f"<@{pid}>")

        if failed:
            ch = self.bot.get_channel(game.channel_id)
            if ch:
                await ch.send(
                    f"⚠️ 以下のユーザーへのDMが失敗しました: {', '.join(failed)}\n"
                    f"DMを許可するか、`/daifugo hand` で手札を確認してください。"
                )


# =========================================================
# Cog登録（bot.pyから呼ばれる）
# =========================================================
async def setup(bot: commands.Bot):
    await bot.add_cog(AddCommandCog(bot))
    print("✅ AddCommandCog (大富豪) を登録しました")
