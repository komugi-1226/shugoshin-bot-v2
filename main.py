import discord
from discord import app_commands, ui
import os
import threading
import logging
import datetime
from dotenv import load_dotenv
from flask import Flask
import database as db

# --- 初期設定 ---
load_dotenv()  # 環境変数（.env）からSupabaseデータベース接続情報を読み込み
logging.basicConfig(level=logging.INFO)

# --- 定数 ---
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
COOLDOWN_MINUTES = 5 # クールダウン時間（分）
REPORT_BUTTON_CHANNEL_ID = 1382351852825346048  # ボタン式報告専用チャンネルID（変更したい場合はここを修正）

# --- Discord Botの準備 ---
intents = discord.Intents.default()
intents.members = True  # サーバーメンバー情報の取得に必要
intents.guilds = True   # ギルド情報の取得に必要
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# --- スリープ対策Webサーバー ---
app = Flask(__name__)
@app.route('/')
def home(): return "Shugoshin Bot is watching over you."
@app.route('/health')
def health_check(): return "OK"
def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- Botのイベント ---
@client.event
async def on_ready():
    # Supabaseローカル環境で守護神ボット用テーブルを初期化
    await db.init_shugoshin_db()
    
    # 永続ビューを追加（ボット再起動後もボタンが動作するように）
    client.add_view(ReportStartView())
    
    await tree.sync()
    logging.info(f"✅ 守護神ボットが起動しました: {client.user}")
    
    # 報告用ボタンをチャンネルに送信
    await setup_report_button()

async def setup_report_button():
    """報告用ボタンを特定のチャンネルに設置する"""
    try:
        channel = client.get_channel(REPORT_BUTTON_CHANNEL_ID)
        if not channel:
            logging.error(f"チャンネルID {REPORT_BUTTON_CHANNEL_ID} が見つかりません")
            return
            
        logging.info(f"チャンネル '{channel.name}' (ID: {channel.id}) への報告ボタン設置を試行中...")
        
        # ボットの権限チェック
        permissions = channel.permissions_for(channel.guild.me)
        if not permissions.send_messages:
            logging.error(f"チャンネル '{channel.name}' にメッセージ送信権限がありません")
            return
            
        # 既存のボタンメッセージを探す（新しいメッセージを無限に作らないように）
        async for message in channel.history(limit=50):
            if message.author == client.user and message.embeds:
                embed = message.embeds[0]
                if embed.title and "報告システム" in embed.title:
                    # 既存のボタンメッセージがあるので、新しく作らない
                    logging.info(f"既存の報告ボタンが見つかりました (メッセージID: {message.id})")
                    return
        
        # 新しい報告ボタンメッセージを作成
        embed = discord.Embed(
            title="🛡️ 守護神ボット 報告システム",
            description="サーバーのルール違反を匿名で管理者に報告できます。\n下のボタンをクリックして報告を開始してください。",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="📋 報告の流れ", 
            value="① 報告開始ボタンをクリック\n② 対象者を選択\n③ 違反ルールを選択\n④ 緊急度を選択\n⑤ 詳細情報を入力\n⑥ 最終確認・送信", 
            inline=False
        )
        embed.set_footer(text="報告は完全に匿名で処理されます")
        
        view = ReportStartView()
        sent_message = await channel.send(embed=embed, view=view)
        logging.info(f"報告用ボタンを設置しました (メッセージID: {sent_message.id})")
        
    except discord.Forbidden:
        logging.error(f"チャンネルID {REPORT_BUTTON_CHANNEL_ID} にメッセージを送信する権限がありません")
    except Exception as e:
        logging.error(f"報告ボタンの設置に失敗: {e}", exc_info=True)

# --- 確認ボタン付きView ---
class ConfirmWarningView(ui.View):
    def __init__(self, *, interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.original_interaction = interaction
        self.confirmed = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.original_interaction.user.id:
            await interaction.response.send_message("これはあなたのためのボタンではありません。", ephemeral=True)
            return False
        return True

    @ui.button(label="はい、警告を発行する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="処理中です...", view=self)
        self.stop()

    @ui.button(label="いいえ、やめておく", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.confirmed = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="警告の発行をキャンセルしました。", view=None)
        self.stop()

# --- ボタンベースの報告システム用View ---
class ReportStartView(ui.View):
    """報告を開始するボタン"""
    def __init__(self):
        super().__init__(timeout=None)  # 永続化

    @ui.button(label="📝 報告を開始する", style=discord.ButtonStyle.primary, emoji="🛡️", custom_id="report_start_button")
    async def start_report(self, interaction: discord.Interaction, button: ui.Button):
        # 最初に即座に応答して、その後でクールダウンチェックを行う
        await interaction.response.defer(ephemeral=True)
        
        try:
            # クールダウンチェック
            remaining_time = await db.check_cooldown(interaction.user.id, COOLDOWN_MINUTES * 60)
            if remaining_time > 0:
                await interaction.followup.send(
                    f"⏰ クールダウン中です。あと `{int(remaining_time // 60)}分 {int(remaining_time % 60)}秒` 待ってください。", 
                    ephemeral=True
                )
                return
            
            # 報告データを初期化
            report_data = ReportData()
            view = TargetUserSelectView(report_data)
            
            embed = discord.Embed(
                title="👤 報告対象者の選択",
                description="報告したい相手を選択してください。\n\n**使い方:**\n• 上のセレクトメニューから直接ユーザーを選択（最近アクティブなユーザーのみ表示）\n• または「🔍 ユーザーを検索」ボタンでユーザー名やIDを入力",
                color=discord.Color.orange()
            )
            embed.add_field(
                name="💡 ヒント",
                value="セレクトメニューに目的のユーザーが表示されない場合は、「🔍 ユーザーを検索」ボタンをご利用ください。",
                inline=False
            )
            embed.set_footer(text="ステップ 1/5 | 30秒でタイムアウトします")
            
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            
        except Exception as e:
            logging.error(f"報告開始ボタンでエラー: {e}", exc_info=True)
            await interaction.followup.send("❌ 報告システムでエラーが発生しました。しばらく待ってから再試行してください。", ephemeral=True)

class ReportData:
    """報告データを保持するクラス"""
    def __init__(self):
        self.target_user = None
        self.violated_rule = None
        self.urgency = None
        self.issue_warning = False
        self.details = None
        self.message_link = None

class TargetUserSelectView(ui.View):
    """対象ユーザー選択用のView"""
    def __init__(self, report_data: ReportData):
        super().__init__(timeout=30)
        self.report_data = report_data

    @ui.select(
        cls=ui.UserSelect,
        placeholder="報告対象のユーザーを選択してください",
        min_values=1,
        max_values=1
    )
    async def select_user(self, interaction: discord.Interaction, select: ui.UserSelect):
        """ユーザー選択時の処理"""
        selected_user = select.values[0]
        self.report_data.target_user = selected_user
        
        # 次のステップへ
        view = RuleSelectView(self.report_data)
        embed = discord.Embed(
            title="📜 違反ルールの選択",
            description=f"**報告対象者:** {selected_user.mention}\n\n違反したルールを選択してください:",
            color=discord.Color.orange()
        )
        embed.set_footer(text="ステップ 2/5")
        
        await interaction.response.edit_message(embed=embed, view=view)

    @ui.button(label="🔍 ユーザーを検索", style=discord.ButtonStyle.secondary)
    async def input_user_manually(self, interaction: discord.Interaction, button: ui.Button):
        """手動でユーザーIDやメンションを入力する場合"""
        modal = UserInputModal(self.report_data)
        await interaction.response.send_modal(modal)

class UserInputModal(ui.Modal):
    """ユーザー入力用のモーダル"""
    def __init__(self, report_data: ReportData):
        super().__init__(title="ユーザー検索")
        self.report_data = report_data

    user_input = ui.TextInput(
        label="報告対象者",
        placeholder="ユーザー名、表示名、@メンション、またはユーザーIDを入力",
        required=True,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_input_text = self.user_input.value.strip()
        
        try:
            target_user = None
            
            # 1. メンションからユーザーIDを抽出
            if user_input_text.startswith('<@') and user_input_text.endswith('>'):
                user_id_str = user_input_text[2:-1]
                if user_id_str.startswith('!'):
                    user_id_str = user_id_str[1:]
                try:
                    user_id = int(user_id_str)
                    target_user = await interaction.client.fetch_user(user_id)
                except (ValueError, discord.NotFound):
                    pass
            
            # 2. 数字のみの場合はユーザーIDとして処理
            elif user_input_text.isdigit():
                try:
                    user_id = int(user_input_text)
                    target_user = await interaction.client.fetch_user(user_id)
                except discord.NotFound:
                    pass
            
            # 3. ユーザー名や表示名で検索
            if not target_user:
                guild = interaction.guild
                search_term = user_input_text.lower()
                
                # 候補者を格納するリスト
                exact_matches = []    # 完全一致
                partial_matches = []  # 部分一致
                
                # サーバーメンバーから検索
                for member in guild.members:
                    member_name = member.name.lower()
                    member_display = member.display_name.lower()
                    
                    # 完全一致チェック（優先度最高）
                    if member_name == search_term or member_display == search_term:
                        exact_matches.append(member)
                        continue
                    
                    # 部分一致チェック
                    if (search_term in member_name or 
                        search_term in member_display or
                        member_name.startswith(search_term) or
                        member_display.startswith(search_term)):
                        partial_matches.append(member)
                
                # 結果の選択（完全一致 > 部分一致の順）
                if exact_matches:
                    target_user = exact_matches[0]
                elif partial_matches:
                    target_user = partial_matches[0]
                
                # デバッグ情報をログに出力
                logging.info(f"ユーザー検索: '{user_input_text}' -> 完全一致:{len(exact_matches)}件, 部分一致:{len(partial_matches)}件")
            
            if target_user:
                self.report_data.target_user = target_user
                
                # 次のステップへ
                view = RuleSelectView(self.report_data)
                embed = discord.Embed(
                    title="📜 違反ルールの選択",
                    description=f"**報告対象者:** {target_user.mention}\n\n違反したルールを選択してください:",
                    color=discord.Color.orange()
                )
                embed.set_footer(text="ステップ 2/5")
                
                await interaction.edit_original_response(embed=embed, view=view)
            else:
                # 検索に失敗した場合の詳細診断情報
                guild = interaction.guild
                member_count = guild.member_count  # Discord公式メンバー数
                member_list = [member for member in guild.members]  # 実際に取得できたメンバー
                member_list_count = len(member_list)
                
                # Intent設定の確認
                intents_status = f"members:{client.intents.members}, guilds:{client.intents.guilds}"
                
                # 類似ユーザー名を探す（最大5件）
                similar_users = []
                search_term = user_input_text.lower()
                
                for member in member_list:
                    member_name = member.name.lower()
                    member_display = member.display_name.lower()
                    
                    # より緩い条件で類似ユーザーを検索
                    if (any(char in member_name for char in search_term) or 
                        any(char in member_display for char in search_term)):
                        similar_users.append(f"• {member.display_name} (@{member.name})")
                        if len(similar_users) >= 5:
                            break
                
                error_message = f"❌ 「{user_input_text}」に一致するユーザーが見つかりませんでした。\n\n"
                error_message += f"**サーバー診断:**\n"
                error_message += f"• Discord公式メンバー数: {member_count}人\n"
                error_message += f"• 実際に取得できた数: {member_list_count}人\n"
                error_message += f"• Intent設定: {intents_status}\n\n"
                
                # メンバー数が異常に少ない場合の警告
                if member_list_count == 1:
                    error_message += "⚠️ **メンバー情報取得エラー**\n"
                    error_message += "Discord Developer Portalで以下を確認してください：\n"
                    error_message += "1. SERVER MEMBERS INTENTが有効か\n"
                    error_message += "2. GUILDS INTENTが有効か\n\n"
                elif member_list_count < member_count * 0.5:  # 半分以下の場合
                    error_message += "⚠️ **メンバー情報が不完全**\n"
                    error_message += "一部のメンバー情報が取得できていません。\n\n"
                
                if similar_users:
                    error_message += "**類似するユーザー名:**\n" + "\n".join(similar_users) + "\n\n"
                
                error_message += ("**検索のコツ:**\n"
                                "• 正確なユーザー名を入力してください\n"
                                "• ニックネーム（表示名）も検索対象です\n"
                                "• ユーザーIDを使用してください\n"
                                "• @メンションをコピーして貼り付けてください\n"
                                "• そのユーザーがこのサーバーのメンバーか確認してください")
                
                await interaction.followup.send(error_message, ephemeral=True)
                
        except Exception as e:
            logging.error(f"ユーザー検索エラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ ユーザー検索中にエラーが発生しました: {e}", ephemeral=True)

class RuleSelectView(ui.View):
    """ルール選択用のView"""
    def __init__(self, report_data: ReportData):
        super().__init__(timeout=60)
        self.report_data = report_data

    @ui.select(
        placeholder="違反したルールを選択してください",
        options=[
            discord.SelectOption(
                label="そのいち：ひとをきずつけない",
                description="他者への攻撃的な発言や行動",
                emoji="💔",
                value="そのいち：ひとをきずつけない 💔"
            ),
            discord.SelectOption(
                label="そのに：ひとのいやがることをしない",
                description="迷惑行為やハラスメント",
                emoji="🚫",
                value="そのに：ひとのいやがることをしない 🚫"
            ),
            discord.SelectOption(
                label="そのさん：かってにフレンドにならない",
                description="無断でのフレンド申請や個人情報の要求",
                emoji="👥",
                value="そのさん：かってにフレンドにならない 👥"
            ),
            discord.SelectOption(
                label="そのよん：くすりのなまえはかきません",
                description="薬物に関する不適切な発言",
                emoji="💊",
                value="そのよん：くすりのなまえはかきません 💊"
            ),
            discord.SelectOption(
                label="そのご：あきらかなせんでんこういはしません",
                description="宣伝や営利活動",
                emoji="📢",
                value="そのご：あきらかなせんでんこういはしません 📢"
            ),
            discord.SelectOption(
                label="その他の違反",
                description="上記以外のルール違反",
                emoji="❓",
                value="その他"
            ),
        ]
    )
    async def rule_select(self, interaction: discord.Interaction, select: ui.Select):
        self.report_data.violated_rule = select.values[0]
        
        # 次のステップへ
        view = UrgencySelectView(self.report_data)
        embed = discord.Embed(
            title="🔥 緊急度の選択",
            description=f"**報告対象者:** {self.report_data.target_user.mention}\n**違反ルール:** {self.report_data.violated_rule}\n\n緊急度を選択してください:",
            color=discord.Color.orange()
        )
        embed.set_footer(text="ステップ 3/5")
        
        await interaction.response.edit_message(embed=embed, view=view)

class UrgencySelectView(ui.View):
    """緊急度選択用のView"""
    def __init__(self, report_data: ReportData):
        super().__init__(timeout=60)
        self.report_data = report_data

    @ui.select(
        placeholder="緊急度を選択してください",
        options=[
            discord.SelectOption(
                label="低：通常の違反報告",
                description="通常の処理で問題ありません",
                emoji="🟢",
                value="低"
            ),
            discord.SelectOption(
                label="中：早めの対応が必要",
                description="早めの確認をお願いします",
                emoji="🟡",
                value="中"
            ),
            discord.SelectOption(
                label="高：即座の対応が必要",
                description="緊急で対応が必要です",
                emoji="🔴",
                value="高"
            ),
        ]
    )
    async def urgency_select(self, interaction: discord.Interaction, select: ui.Select):
        self.report_data.urgency = select.values[0]
        
        # 次のステップへ
        view = WarningSelectView(self.report_data)
        embed = discord.Embed(
            title="⚠️ 警告発行の選択",
            description=f"**報告対象者:** {self.report_data.target_user.mention}\n**違反ルール:** {self.report_data.violated_rule}\n**緊急度:** {self.report_data.urgency}\n\n対象者に警告を発行しますか？",
            color=discord.Color.orange()
        )
        embed.add_field(
            name="⚠️ 注意",
            value="警告を発行すると、報告チャンネルで対象者にメンションが送られます。\nタイミングから通報者が特定される可能性があります。",
            inline=False
        )
        embed.set_footer(text="ステップ 4/5")
        
        await interaction.response.edit_message(embed=embed, view=view)

class WarningSelectView(ui.View):
    """警告発行選択用のView"""
    def __init__(self, report_data: ReportData):
        super().__init__(timeout=60)
        self.report_data = report_data

    @ui.button(label="はい、警告を発行する", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def issue_warning(self, interaction: discord.Interaction, button: ui.Button):
        self.report_data.issue_warning = True
        await self._proceed_to_details(interaction)

    @ui.button(label="いいえ、管理者にのみ報告", style=discord.ButtonStyle.secondary, emoji="🤐")
    async def no_warning(self, interaction: discord.Interaction, button: ui.Button):
        self.report_data.issue_warning = False
        await self._proceed_to_details(interaction)

    async def _proceed_to_details(self, interaction: discord.Interaction):
        """詳細入力ステップへ進む"""
        modal = DetailsInputModal(self.report_data)
        await interaction.response.send_modal(modal)

class DetailsInputModal(ui.Modal):
    """詳細情報入力用のモーダル"""
    def __init__(self, report_data: ReportData):
        super().__init__(title="報告の詳細情報")
        self.report_data = report_data

    details = ui.TextInput(
        label="詳しい状況（任意）",
        placeholder="何があったのか、詳しく教えてください。「その他」を選んだ場合は必須です。",
        style=discord.TextStyle.long,
        required=False,
        max_length=1000
    )

    message_link = ui.TextInput(
        label="証拠となるメッセージのリンク（任意）",
        placeholder="https://discord.com/channels/...",
        required=False,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        self.report_data.details = self.details.value if self.details.value else None
        self.report_data.message_link = self.message_link.value if self.message_link.value else None
        
        # 「その他」を選んだ場合、詳細が必須
        if self.report_data.violated_rule == "その他" and not self.report_data.details:
            await interaction.response.send_message(
                "❌ 「その他」のルール違反を選んだ場合、詳細な状況の入力が必要です。", 
                ephemeral=True
            )
            return
        
        # 最終確認ステップへ
        view = FinalConfirmView(self.report_data)
        embed = discord.Embed(
            title="✅ 最終確認",
            description="以下の内容で報告を送信します。よろしいですか？",
            color=discord.Color.green()
        )
        embed.add_field(name="👤 報告対象者", value=self.report_data.target_user.mention, inline=False)
        embed.add_field(name="📜 違反ルール", value=self.report_data.violated_rule, inline=False)
        embed.add_field(name="🔥 緊急度", value=self.report_data.urgency, inline=False)
        embed.add_field(name="⚠️ 警告発行", value="はい" if self.report_data.issue_warning else "いいえ", inline=False)
        if self.report_data.details:
            embed.add_field(name="📝 詳細", value=self.report_data.details[:500] + ("..." if len(self.report_data.details) > 500 else ""), inline=False)
        if self.report_data.message_link:
            embed.add_field(name="🔗 証拠リンク", value=self.report_data.message_link, inline=False)
        embed.set_footer(text="ステップ 5/5 | この報告は匿名で送信されます")
        
        await interaction.response.edit_message(embed=embed, view=view)

class FinalConfirmView(ui.View):
    """最終確認用のView"""
    def __init__(self, report_data: ReportData):
        super().__init__(timeout=60)
        self.report_data = report_data

    @ui.button(label="📤 報告を送信する", style=discord.ButtonStyle.success, emoji="✅")
    async def submit_report(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 報告を送信（既存のreportコマンドのロジックを再利用）
            settings = await db.get_guild_settings(interaction.guild.id)
            if not settings or not settings.get('report_channel_id'):
                await interaction.followup.send("❌ ボットの初期設定が完了していません。管理者に連絡してください。", ephemeral=True)
                return

            report_id = await db.create_report(
                interaction.guild.id, 
                self.report_data.target_user.id, 
                self.report_data.violated_rule, 
                self.report_data.details, 
                self.report_data.message_link, 
                self.report_data.urgency
            )
            
            report_channel = client.get_channel(settings['report_channel_id'])
            
            # 埋め込みの色と絵文字を設定
            embed_color = discord.Color.greyple()
            title_prefix = "📝"
            content = None

            if self.report_data.urgency == "中":
                embed_color = discord.Color.orange()
                title_prefix = "⚠️"
            elif self.report_data.urgency == "高":
                embed_color = discord.Color.red()
                title_prefix = "🚨"
                if settings.get('urgent_role_id'):
                    role = interaction.guild.get_role(settings['urgent_role_id'])
                    if role: 
                        content = f"{role.mention} 緊急の報告です！"
            
            embed = discord.Embed(title=f"{title_prefix} 新規の匿名報告 (ID: {report_id})", color=embed_color)
            embed.add_field(name="👤 報告対象者", value=f"{self.report_data.target_user.mention} ({self.report_data.target_user.id})", inline=False)
            embed.add_field(name="📜 違反したルール", value=self.report_data.violated_rule, inline=False)
            embed.add_field(name="🔥 緊急度", value=self.report_data.urgency, inline=False)
            if self.report_data.details: 
                embed.add_field(name="📝 詳細", value=self.report_data.details, inline=False)
            if self.report_data.message_link: 
                embed.add_field(name="🔗 関連メッセージ", value=self.report_data.message_link, inline=False)
            embed.add_field(name="📊 ステータス", value="未対応", inline=False)
            embed.set_footer(text="この報告は匿名で送信されました（ボタン式報告）")

            sent_message = await report_channel.send(content=content, embed=embed)
            await db.update_report_message_id(report_id, sent_message.id)

            # 警告を発行する場合
            if self.report_data.issue_warning:
                warning_message = (
                    f"{self.report_data.target_user.mention}\n\n"
                    f"⚠️ **サーバー管理者からのお知らせです** ⚠️\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"あなたの行動について、サーバーのルールに関する報告が寄せられました。\n\n"
                    f"**該当ルール:** {self.report_data.violated_rule}\n\n"
                    f"みんなが楽しく過ごせるよう、今一度ルールの確認をお願いいたします。\n"
                    f"ご不明な点があれば、このチャンネルで返信するか、管理者にDMを送ってください。\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━"
                )
                await report_channel.send(warning_message)

            final_message = "✅ 報告を送信しました。ご協力ありがとうございます。"
            if self.report_data.issue_warning:
                final_message = "✅ 報告と警告発行を完了しました。ご協力ありがとうございます。"

            await interaction.followup.send(final_message, ephemeral=True)

        except Exception as e:
            logging.error(f"ボタン式報告処理中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 報告の送信中にエラーが発生しました: {e}", ephemeral=True)

    @ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_report(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="❌ 報告をキャンセルしました",
            description="報告は送信されませんでした。",
            color=discord.Color.red()
        )
        await interaction.response.edit_message(embed=embed, view=None)

# --- スラッシュコマンド ---

@tree.command(name="setup", description="【管理者用】守護神ボットの初期設定を行います。")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    report_channel="通報内容が投稿されるチャンネル",
    urgent_role="緊急度「高」の際にメンションするロール（任意）"
)
async def setup(interaction: discord.Interaction, report_channel: discord.TextChannel, urgent_role: discord.Role = None):
    await interaction.response.defer(ephemeral=True)
    role_id = urgent_role.id if urgent_role else None
    await db.setup_guild(interaction.guild.id, report_channel.id, role_id)
    role_mention = urgent_role.mention if urgent_role else "未設定"
    await interaction.followup.send(
        f"✅ 設定を保存しました。\n"
        f"報告用チャンネル: {report_channel.mention}\n"
        f"緊急メンション用ロール: {role_mention}",
        ephemeral=True
    )

@setup.error
async def setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("このコマンドはサーバーの管理者のみが実行できます。", ephemeral=True)
    else:
        await interaction.response.send_message(f"設定中にエラーが発生しました: {error}", ephemeral=True)

@tree.command(name="setup_report_button", description="【管理者用】報告ボタンを再設置します。")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="ボタンを設置するチャンネル（指定しない場合は現在のチャンネル）")
async def setup_report_button_command(interaction: discord.Interaction, channel: discord.TextChannel = None):
    """報告ボタンを手動で設置するコマンド"""
    await interaction.response.defer(ephemeral=True)
    
    # チャンネルが指定されていない場合は現在のチャンネルを使用
    target_channel = channel if channel else interaction.channel
    
    if not target_channel:
        await interaction.followup.send("❌ チャンネルが見つかりません。", ephemeral=True)
        return
    
    # ボットがメッセージを送信する権限があるかチェック
    if not target_channel.permissions_for(interaction.guild.me).send_messages:
        await interaction.followup.send(f"❌ {target_channel.mention} にメッセージを送信する権限がありません。", ephemeral=True)
        return
    
    try:
        embed = discord.Embed(
            title="🛡️ 守護神ボット 報告システム",
            description="サーバーのルール違反を匿名で管理者に報告できます。\n下のボタンをクリックして報告を開始してください。",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="📋 報告の流れ", 
            value="① 報告開始ボタンをクリック\n② 対象者を選択\n③ 違反ルールを選択\n④ 緊急度を選択\n⑤ 詳細情報を入力\n⑥ 最終確認・送信", 
            inline=False
        )
        embed.set_footer(text="報告は完全に匿名で処理されます")
        
        view = ReportStartView()
        sent_message = await target_channel.send(embed=embed, view=view)
        
        await interaction.followup.send(
            f"✅ 報告ボタンを {target_channel.mention} に設置しました。\n"
            f"**メッセージID:** {sent_message.id}\n"
            f"**チャンネルID:** {target_channel.id}", 
            ephemeral=True
        )
        
        # 設置されたチャンネルIDをログに出力
        logging.info(f"報告ボタンを設置: チャンネル={target_channel.name}({target_channel.id})")
        
    except discord.Forbidden:
        await interaction.followup.send(f"❌ {target_channel.mention} にメッセージを送信する権限がありません。", ephemeral=True)
    except Exception as e:
        logging.error(f"ボタン設置エラー: {e}")
        await interaction.followup.send(f"❌ ボタンの設置に失敗しました: {e}", ephemeral=True)

@setup_report_button_command.error
async def setup_report_button_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("このコマンドはサーバーの管理者のみが実行できます。", ephemeral=True)
    else:
        await interaction.response.send_message(f"ボタン設置中にエラーが発生しました: {error}", ephemeral=True)

@tree.command(name="debug_channel", description="【管理者用】現在のチャンネル情報を表示します。")
@app_commands.checks.has_permissions(administrator=True)
async def debug_channel(interaction: discord.Interaction):
    """チャンネル情報をデバッグ表示するコマンド"""
    await interaction.response.defer(ephemeral=True)
    
    channel = interaction.channel
    embed = discord.Embed(
        title="🔍 チャンネル情報",
        color=discord.Color.blue()
    )
    embed.add_field(name="チャンネル名", value=channel.name, inline=False)
    embed.add_field(name="チャンネルID", value=f"`{channel.id}`", inline=False)
    embed.add_field(name="設定済みID", value=f"`{REPORT_BUTTON_CHANNEL_ID}`", inline=False)
    embed.add_field(name="IDの一致", value="✅ 一致" if channel.id == REPORT_BUTTON_CHANNEL_ID else "❌ 不一致", inline=False)
    
    # ボットの権限チェック
    permissions = channel.permissions_for(interaction.guild.me)
    embed.add_field(
        name="ボットの権限",
        value=f"メッセージ送信: {'✅' if permissions.send_messages else '❌'}\n"
              f"埋め込みリンク: {'✅' if permissions.embed_links else '❌'}\n"
              f"ファイル添付: {'✅' if permissions.attach_files else '❌'}",
        inline=False
    )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@debug_channel.error
async def debug_channel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("このコマンドはサーバーの管理者のみが実行できます。", ephemeral=True)
    else:
        await interaction.response.send_message(f"デバッグ中にエラーが発生しました: {error}", ephemeral=True)

@tree.command(name="debug_members", description="【管理者用】サーバーメンバー情報をデバッグ表示します。")
@app_commands.checks.has_permissions(administrator=True)
async def debug_members(interaction: discord.Interaction):
    """サーバーメンバー情報をデバッグ表示するコマンド"""
    await interaction.response.defer(ephemeral=True)
    
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("❌ サーバー情報を取得できませんでした", ephemeral=True)
        return
    
    # サーバーメンバー情報を取得
    member_count = guild.member_count  # Discordが報告する公式メンバー数
    member_list = [member for member in guild.members]  # 実際に取得できたメンバーリスト
    member_list_count = len(member_list)
    
    # ボットとユーザーの分類
    bot_members = [member for member in member_list if member.bot]
    user_members = [member for member in member_list if not member.bot]
    
    # Intent設定の確認
    intents_info = f"members: {client.intents.members}, guilds: {client.intents.guilds}"
    
    embed = discord.Embed(
        title="🔍 サーバーメンバー情報デバッグ",
        color=discord.Color.green()
    )
    embed.add_field(name="サーバー名", value=guild.name, inline=False)
    embed.add_field(name="サーバーID", value=f"`{guild.id}`", inline=False)
    embed.add_field(name="Discord公式メンバー数", value=f"{member_count} 人", inline=True)
    embed.add_field(name="実際に取得できた数", value=f"{member_list_count} 人", inline=True)
    embed.add_field(name="　", value="　", inline=True)  # 空白で改行
    embed.add_field(name="ユーザー数", value=f"{len(user_members)} 人", inline=True)
    embed.add_field(name="ボット数", value=f"{len(bot_members)} 人", inline=True)
    embed.add_field(name="　", value="　", inline=True)  # 空白で改行
    embed.add_field(name="Intent設定", value=intents_info, inline=False)
    
    # 診断結果
    if member_list_count == 1:
        embed.add_field(
            name="⚠️ 診断結果",
            value="メンバー情報を正常に取得できていません。\n"
                  "Discord Developer Portalで以下を確認してください：\n"
                  "1. SERVER MEMBERS INTENTが有効になっているか\n"
                  "2. GUILDS INTENTが有効になっているか",
            inline=False
        )
    elif member_list_count < member_count:
        embed.add_field(
            name="⚠️ 診断結果",
            value="一部のメンバー情報が取得できていません。\n"
                  "大規模サーバーの場合、全メンバーの取得には時間がかかる場合があります。",
            inline=False
        )
    else:
        embed.add_field(
            name="✅ 診断結果",
            value="メンバー情報は正常に取得できています。",
            inline=False
        )
    
    # 最初の10人のメンバーリスト（デバッグ用）
    if member_list:
        member_names = []
        for i, member in enumerate(member_list[:10]):
            member_type = "🤖" if member.bot else "👤"
            member_names.append(f"{member_type} {member.display_name}")
            if i >= 9:  # 10人まで
                break
        
        embed.add_field(
            name=f"メンバー例（最初の{min(10, len(member_list))}人）",
            value="\n".join(member_names) if member_names else "なし",
            inline=False
        )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@debug_members.error
async def debug_members_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("このコマンドはサーバーの管理者のみが実行できます。", ephemeral=True)
    else:
        await interaction.response.send_message(f"メンバーデバッグ中にエラーが発生しました: {error}", ephemeral=True)
        
# ★★★★★★★ ここが超進化した /report コマンド ★★★★★★★
@tree.command(name="report", description="サーバーのルール違反を匿名で管理者に報告します。")
@app_commands.describe(
    target_user="① 報告したい相手を選んでね",
    violated_rule="② 違反したルールを選んでね",
    urgency="③ 緊急度を選んでね",
    keikoku_suru="④ 相手に警告する？（管理者と本人だけが見える場所でメンションします）",
    details="⑤ 詳しい状況を書いてね（『その他』を選んだ場合は必須だよ）",
    message_link="⑥ 証拠になるメッセージのリンクがあれば貼ってね"
)
@app_commands.choices(
    violated_rule=[
        app_commands.Choice(name="そのいち：ひとをきずつけない 💔", value="そのいち：ひとをきずつけない 💔"),
        app_commands.Choice(name="そのに：ひとのいやがることをしない 🚫", value="そのに：ひとのいやがることをしない 🚫"),
        app_commands.Choice(name="そのさん：かってにフレンドにならない 👥", value="そのさん：かってにフレンドにならない 👥"),
        app_commands.Choice(name="そのよん：くすりのなまえはかきません 💊", value="そのよん：くすりのなまえはかきません 💊"),
        app_commands.Choice(name="そのご：あきらかなせんでんこういはしません 📢", value="そのご：あきらかなせんでんこういはしません 📢"),
        app_commands.Choice(name="その他：上記以外の違反", value="その他"),
    ],
    urgency=[
        app_commands.Choice(name="低：通常の違反報告", value="低"),
        app_commands.Choice(name="中：早めの対応が必要", value="中"),
        app_commands.Choice(name="高：即座の対応が必要", value="高"),
    ],
    keikoku_suru=[
        app_commands.Choice(name="はい（相手に通知がいきます ※匿名性が少し下がります）", value="yes"),
        app_commands.Choice(name="いいえ（管理者だけに報告します）", value="no"),
    ]
)
async def report(
    interaction: discord.Interaction,
    target_user: discord.User,
    violated_rule: app_commands.Choice[str],
    urgency: app_commands.Choice[str],
    keikoku_suru: app_commands.Choice[str],
    details: str = None,
    message_link: str = None
):
    await interaction.response.defer(ephemeral=True)

    settings = await db.get_guild_settings(interaction.guild.id)
    if not settings or not settings.get('report_channel_id'):
        await interaction.followup.send("ボットの初期設定が完了していません。管理者が`/setup`で設定してください。", ephemeral=True)
        return

    remaining_time = await db.check_cooldown(interaction.user.id, COOLDOWN_MINUTES * 60)
    if remaining_time > 0:
        await interaction.followup.send(f"クールダウン中です。あと `{int(remaining_time // 60)}分 {int(remaining_time % 60)}秒` 待ってください。", ephemeral=True)
        return

    issue_warning_confirmed = False
    if keikoku_suru.value == "yes":
        view = ConfirmWarningView(interaction=interaction)
        await interaction.followup.send(
            "⚠️ **警告:** 対象者に報告用チャンネルでメンションして警告を発行します。"
            "タイミングから通報者が特定される可能性がありますが、続行しますか？",
            view=view, ephemeral=True
        )
        await view.wait()
        if not view.confirmed:
            return
        issue_warning_confirmed = True
    
    try:
        report_id = await db.create_report(
            interaction.guild.id, target_user.id, violated_rule.value, details, message_link, urgency.value
        )
        
        report_channel = client.get_channel(settings['report_channel_id'])
        
        embed_color = discord.Color.greyple()
        title_prefix = "📝"
        content = None

        if urgency.value == "中":
            embed_color = discord.Color.orange()
            title_prefix = "⚠️"
        elif urgency.value == "高":
            embed_color = discord.Color.red()
            title_prefix = "🚨"
            if settings.get('urgent_role_id'):
                role = interaction.guild.get_role(settings['urgent_role_id'])
                if role: content = f"{role.mention} 緊急の報告です！"
        
        embed = discord.Embed(title=f"{title_prefix} 新規の匿名報告 (ID: {report_id})", color=embed_color)
        embed.add_field(name="👤 報告対象者", value=f"{target_user.mention} ({target_user.id})", inline=False)
        embed.add_field(name="📜 違反したルール", value=violated_rule.value, inline=False)
        embed.add_field(name="🔥 緊急度", value=urgency.value, inline=False)
        if details: embed.add_field(name="📝 詳細", value=details, inline=False)
        if message_link: embed.add_field(name="🔗 関連メッセージ", value=message_link, inline=False)
        embed.add_field(name="📊 ステータス", value="未対応", inline=False)
        embed.set_footer(text="この報告は匿名で送信されました。")

        sent_message = await report_channel.send(content=content, embed=embed)
        await db.update_report_message_id(report_id, sent_message.id)

        final_message = "通報を受け付けました。ご協力ありがとうございます。"
        if issue_warning_confirmed:
            warning_message = (
                f"{target_user.mention}\n\n"
                f"⚠️ **サーバー管理者からのお知らせです** ⚠️\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"あなたの行動について、サーバーのルールに関する報告が寄せられました。\n\n"
                f"**該当ルール:** {violated_rule.value}\n\n"
                f"みんなが楽しく過ごせるよう、今一度ルールの確認をお願いいたします。\n"
                f"ご不明な点があれば、このチャンネルで返信するか、管理者にDMを送ってください。\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            await report_channel.send(warning_message)
            final_message = "通報と警告発行を受け付けました。ご協力ありがとうございます。"

        if interaction.is_expired():
             await interaction.followup.send(final_message, ephemeral=True)
        else:
             if issue_warning_confirmed:
                await interaction.edit_original_response(content=final_message, view=None)
             else:
                await interaction.followup.send(final_message, ephemeral=True)

    except Exception as e:
        logging.error(f"通報処理中にエラー: {e}", exc_info=True)
        if not interaction.is_expired():
            await interaction.edit_original_response(content=f"不明なエラーが発生しました: {e}", view=None)


# (/reportmanage グループとサブコマンドはVer1.2から変更なし)
report_manage_group = app_commands.Group(name="reportmanage", description="報告を管理します。")

@report_manage_group.command(name="status", description="報告のステータスを変更します。")
@app_commands.describe(report_id="ステータスを変更したい報告のID", new_status="新しいステータス")
@app_commands.choices(new_status=[app_commands.Choice(name="対応中", value="対応中"), app_commands.Choice(name="解決済み", value="解決済み"), app_commands.Choice(name="却下", value="却下"),])
async def status(interaction: discord.Interaction, report_id: int, new_status: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    settings = await db.get_guild_settings(interaction.guild.id)
    if not settings: return await interaction.followup.send("未設定です。`/setup`を実行してください。", ephemeral=True)
    try:
        report_data = await db.get_report(report_id)
        if not report_data:
            await interaction.followup.send(f"エラー: 報告ID `{report_id}` が見つかりません。", ephemeral=True)
            return
        report_channel = client.get_channel(settings['report_channel_id'])
        original_message = await report_channel.fetch_message(report_data['message_id'])
        original_embed = original_message.embeds[0]
        status_colors = {"対応中": discord.Color.yellow(), "解決済み": discord.Color.green(), "却下": discord.Color.greyple()}
        original_embed.color = status_colors.get(new_status.value)
        for i, field in enumerate(original_embed.fields):
            if field.name == "📊 ステータス":
                original_embed.set_field_at(i, name="📊 ステータス", value=new_status.value, inline=False)
                break
        await original_message.edit(embed=original_embed)
        await db.update_report_status(report_id, new_status.value)
        await interaction.followup.send(f"報告ID `{report_id}` のステータスを「{new_status.value}」に変更しました。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"ステータス更新中にエラー: {e}", ephemeral=True)

@report_manage_group.command(name="list", description="報告の一覧を表示します。")
@app_commands.describe(filter="表示するステータスで絞り込みます。")
@app_commands.choices(filter=[app_commands.Choice(name="すべて", value="all"), app_commands.Choice(name="未対応", value="未対応"), app_commands.Choice(name="対応中", value="対応中"),])
async def list_reports_cmd(interaction: discord.Interaction, filter: app_commands.Choice[str] = None):
    await interaction.response.defer(ephemeral=True)
    status_filter = filter.value if filter else None
    reports = await db.list_reports(status_filter)
    if not reports:
        await interaction.followup.send("該当する報告はありません。", ephemeral=True)
        return
    embed = discord.Embed(title=f"📜 報告リスト ({filter.name if filter else '最新'})", color=discord.Color.blue())
    description = ""
    for report in reports:
        try:
            target_user = await client.fetch_user(report['target_user_id'])
            user_name = target_user.name
        except discord.NotFound:
            user_name = "不明なユーザー"
        description += f"**ID: {report['report_id']}** | 対象: {user_name} | ステータス: `{report['status']}`\n"
    embed.description = description
    await interaction.followup.send(embed=embed, ephemeral=True)

@report_manage_group.command(name="stats", description="報告の統計情報を表示します。")
async def stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    stats_data = await db.get_report_stats()
    total = sum(stats_data.values())
    embed = discord.Embed(title="📈 報告統計", description=f"総報告数: **{total}** 件", color=discord.Color.purple())
    unhandled = stats_data.get('未対応', 0)
    in_progress = stats_data.get('対応中', 0)
    resolved = stats_data.get('解決済み', 0)
    rejected = stats_data.get('却下', 0)
    embed.add_field(name="未対応 🔴", value=f"**{unhandled}** 件", inline=True)
    embed.add_field(name="対応中 🟡", value=f"**{in_progress}** 件", inline=True)
    embed.add_field(name="解決済み 🟢", value=f"**{resolved}** 件", inline=True)
    embed.add_field(name="却下 ⚪", value=f"**{rejected}** 件", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


# --- 起動処理 ---
def main():
    tree.add_command(report_manage_group)
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    client.run(TOKEN)

if __name__ == "__main__":
    main()