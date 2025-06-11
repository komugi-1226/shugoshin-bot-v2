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
load_dotenv()
logging.basicConfig(level=logging.INFO)

# --- 定数 ---
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
COOLDOWN_MINUTES = 5 # クールダウン時間（分）

# --- Discord Botの準備 ---
intents = discord.Intents.default()
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
    await db.init_shugoshin_db()
    await tree.sync()
    logging.info(f"✅ 守護神ボットが起動しました: {client.user}")

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


# ★★★★★★★ ここからが日本語化された /report コマンド ★★★★★★★
@tree.command(name="report", description="サーバーのルール違反を匿名で管理者に報告します。")
@app_commands.describe(
    target_user="報告したい相手",
    violated_rule="違反したと思われるルール",
    urgency="報告の緊急度を選択してください。",
    keikoku_suru="対象者に警告を発行しますか？（管理者と対象者のみが見れる場所で行われます）",
    details="（「その他」を選んだ場合は必須）具体的な状況を教えてください。",
    message_link="証拠となるメッセージのリンク（任意）"
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
        app_commands.Choice(name="はい (※タイミングから通報者が推測される可能性があります)", value="yes"),
        app_commands.Choice(name="いいえ", value="no"),
    ]
)
async def report(
    interaction: discord.Interaction,
    target_user: discord.User,
    violated_rule: app_commands.Choice[str],
    urgency: app_commands.Choice[str],
    keikoku_suru: app_commands.Choice[str], # 引数名と型を変更
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
            return # キャンセルされたので処理を中断
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

        # 既に確認メッセージに edit_message を使っているので、最後の応答は followup.send で行う
        if interaction.is_expired():
             await interaction.followup.send(final_message, ephemeral=True)
        else:
             if issue_warning_confirmed:
                # view.wait()の後のinteractionは編集済みなので、followupを使う
                await interaction.followup.send(final_message, ephemeral=True)
             else:
                # 警告なしの場合は、最初のdeferを編集できる
                await interaction.edit_original_response(content=final_message, view=None)

    except Exception as e:
        logging.error(f"通報処理中にエラー: {e}", exc_info=True)
        if not interaction.is_expired():
            await interaction.edit_original_response(content=f"不明なエラーが発生しました: {e}", view=None)


# (/reportmanage グループとサブコマンドは変更なし)
report_manage_group = app_commands.Group(name="reportmanage", description="報告を管理します。")
# (status, list, stats のコードをここにペースト)

@report_manage_group.command(name="status", description="報告のステータスを変更します。")
@app_commands.describe(report_id="ステータスを変更したい報告のID", new_status="新しいステータス")
@app_commands.choices(new_status=[app_commands.Choice(name="対応中", value="対応中"), app_commands.Choice(name="解決済み", value="解決済み"), app_commands.Choice(name="却下", value="却下"),])
async def status(interaction: discord.Interaction, report_id: int, new_status: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
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