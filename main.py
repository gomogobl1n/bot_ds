import discord
from discord.ext import commands
import wavelink
from collections import deque
import re
import os
import asyncio
from aiohttp import web
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создаём бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=">", intents=intents)

# Словари для хранения данных
queues = {}
player_messages = {}
is_playing_next = {}
tree = bot.tree


async def connect_nodes():
    await bot.wait_until_ready()

    lavalink_host = os.getenv("LAVALINK_HOST", "http://78.154.103.11:15193/")
    lavalink_password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")

    print(f"🔗 Подключение к Lavalink: {lavalink_host}")

    if lavalink_host.startswith("https://"):
        uri = lavalink_host.replace("https://", "wss://")
    else:
        uri = lavalink_host.replace("http://", "ws://")

    try:
        node = wavelink.Node(
            identifier="Node1",
            uri=uri,
            password=lavalink_password
        )
        await wavelink.Pool.connect(client=bot, nodes=[node])
        print(f"✅ Lavalink подключён к {lavalink_host}!")
    except Exception as e:
        print(f"❌ Ошибка подключения к Lavalink: {e}")


@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user} запущен!")
    await connect_nodes()
    await tree.sync()
    print("✅ Слеш-команды синхронизированы!")


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"✅ Узел {payload.node.identifier} готов!")


@bot.event
async def on_wavelink_node_disconnected(payload: wavelink.NodeDisconnectedEventPayload):
    print(f"❌ Узел {payload.node.identifier} отключился!")


def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = deque()
    return queues[guild_id]


def is_url(string):
    url_pattern = re.compile(r'^https?://')
    return bool(url_pattern.match(string))


# ВОЗВРАЩАЕМ РАБОЧУЮ ФУНКЦИЮ ПОИСКА
async def search_tracks(search_query):
    try:
        if is_url(search_query):
            tracks = await wavelink.Playable.search(search_query)
            return tracks

        tracks = await wavelink.Playable.search(f"ytsearch:{search_query}")

        if not tracks:
            tracks = await wavelink.Playable.search(search_query)

        return tracks
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        return None


# Функция для создания эмбеда плеера
def create_player_embed(vc: wavelink.Player, queue):
    current = vc.current
    status = "⏸️ На паузе" if vc.paused else "▶️ Играет"

    embed = discord.Embed(
        title="🎵 Музыкальный плеер",
        description=f"**{status}**",
        color=discord.Color.blue()
    )

    if current:
        duration = f"{current.length // 60000}:{(current.length % 60000) // 1000:02d}"

        embed.add_field(
            name="🎶 Сейчас играет",
            value=f"**{current.title}**\n⏱️ {duration}",
            inline=False
        )
    else:
        embed.add_field(
            name="💤",
            value="Ничего не играет",
            inline=False
        )

    queue_list = list(queue)
    if queue_list:
        queue_text = ""
        for i, track in enumerate(queue_list[:5], 1):
            queue_text += f"`{i}.` {track.title}\n"
        if len(queue_list) > 5:
            queue_text += f"... и ещё {len(queue_list) - 5} треков"
        embed.add_field(
            name=f"📋 Очередь ({len(queue_list)} треков)",
            value=queue_text or "Пусто",
            inline=False
        )
    else:
        embed.add_field(
            name="📋 Очередь",
            value="Пусто",
            inline=False
        )

    return embed


# Функция для обновления сообщения плеера
async def update_player_message(ctx, vc: wavelink.Player, queue, interaction=None):
    try:
        embed = create_player_embed(vc, queue)

        guild_id = ctx.guild.id if hasattr(ctx, 'guild') else ctx.interaction.guild.id
        channel_id = ctx.channel.id if hasattr(ctx, 'channel') else ctx.interaction.channel.id

        if guild_id not in player_messages:
            player_messages[guild_id] = {}

        if interaction:
            try:
                await interaction.edit_original_response(embed=embed, view=PlayerView())
                return
            except:
                pass

        if channel_id in player_messages[guild_id]:
            try:
                msg = player_messages[guild_id][channel_id]
                await msg.edit(embed=embed, view=PlayerView())
                return
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"Ошибка обновления сообщения: {e}")

        if hasattr(ctx, 'send'):
            msg = await ctx.send(embed=embed, view=PlayerView())
            player_messages[guild_id][channel_id] = msg
        elif hasattr(ctx, 'followup'):
            msg = await ctx.followup.send(embed=embed, view=PlayerView())
            player_messages[guild_id][channel_id] = msg
    except Exception as e:
        print(f"Критическая ошибка в update_player_message: {e}")


# Функция для воспроизведения следующего трека
async def play_next_track(vc: wavelink.Player, guild_id: int):
    if is_playing_next.get(guild_id, False):
        return
    is_playing_next[guild_id] = True

    try:
        queue = get_queue(guild_id)

        if queue:
            next_track = queue.popleft()
            try:
                await vc.play(next_track)
                logger.info(f"▶️ Автоматически играет следующий трек: {next_track.title}")

                if guild_id in player_messages:
                    for channel_id, msg in list(player_messages[guild_id].items()):
                        try:
                            embed = create_player_embed(vc, queue)
                            await msg.edit(embed=embed, view=PlayerView())
                        except discord.NotFound:
                            if guild_id in player_messages and channel_id in player_messages[guild_id]:
                                del player_messages[guild_id][channel_id]
                        except Exception as e:
                            logger.error(f"Ошибка обновления сообщения: {e}")
            except Exception as e:
                logger.error(f"Ошибка при воспроизведении следующего трека: {e}")
                await play_next_track(vc, guild_id)
        else:
            if guild_id in player_messages:
                for channel_id, msg in list(player_messages[guild_id].items()):
                    try:
                        embed = create_player_embed(vc, queue)
                        await msg.edit(embed=embed, view=PlayerView())
                    except:
                        pass
    finally:
        is_playing_next[guild_id] = False


# Класс для обработки кнопок
class PlayerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⏸️", style=discord.ButtonStyle.primary)
    async def pause_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        vc: wavelink.Player = interaction.guild.voice_client
        if vc and vc.current:
            if vc.paused:
                await vc.pause(False)
                button.label = "⏸️"
            else:
                await vc.pause(True)
                button.label = "▶️"
            queue = get_queue(interaction.guild.id)
            await update_player_message(interaction, vc, queue, interaction)

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        vc: wavelink.Player = interaction.guild.voice_client
        if vc and vc.current:
            queue = get_queue(interaction.guild.id)
            queue.clear()
            await vc.stop()
            await update_player_message(interaction, vc, queue, interaction)

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.primary)
    async def skip_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        vc: wavelink.Player = interaction.guild.voice_client
        if vc and vc.current:
            queue = get_queue(interaction.guild.id)
            await vc.stop()
            await update_player_message(interaction, vc, queue, interaction)

    @discord.ui.button(label="⏏️", style=discord.ButtonStyle.secondary)
    async def leave_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        vc: wavelink.Player = interaction.guild.voice_client
        if vc:
            queue = get_queue(interaction.guild.id)
            queue.clear()
            await vc.disconnect()
            guild_id = interaction.guild.id
            if guild_id in player_messages:
                channel_id = interaction.channel.id
                if channel_id in player_messages[guild_id]:
                    try:
                        await player_messages[guild_id][channel_id].delete()
                    except:
                        pass
                    del player_messages[guild_id][channel_id]
            await interaction.followup.send("👋 Бот отключён!", ephemeral=True)


# ========== СЛЕШ-КОМАНДЫ ==========

@tree.command(name="play", description="Воспроизвести музыку или добавить в очередь")
async def play(interaction: discord.Interaction, search: str):
    await interaction.response.defer()

    try:
        if not interaction.user.voice:
            return await interaction.followup.send("❌ Вы не в голосовом канале!")

        channel = interaction.user.voice.channel
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc:
            vc = await channel.connect(cls=wavelink.Player)
        elif vc.channel.id != channel.id:
            await vc.move_to(channel)

        tracks = await search_tracks(search)

        if tracks is None:
            return await interaction.followup.send(
                "❌ Ошибка при поиске! Попробуйте использовать ссылку или другой запрос.")

        if not tracks:
            return await interaction.followup.send(
                "❌ Ничего не найдено! Попробуйте другой запрос или используйте ссылку на YouTube.")

        track = tracks[0]
        queue = get_queue(interaction.guild.id)

        if vc.current:
            queue.append(track)
            position = len(queue)
            await interaction.followup.send(f"✅ Добавлено в очередь (позиция {position}): `{track.title}`")
            await update_player_message(interaction, vc, queue, interaction)
        else:
            try:
                await vc.play(track)
                await interaction.followup.send(f"▶️ Сейчас играет: `{track.title}`")
                await update_player_message(interaction, vc, queue, interaction)
            except Exception as e:
                logger.error(f"Ошибка воспроизведения: {e}")
                await interaction.followup.send(f"❌ Ошибка воспроизведения: {e}")
    except Exception as e:
        logger.error(f"Ошибка в команде play: {e}")
        await interaction.followup.send(f"❌ Произошла ошибка: {e}")


@tree.command(name="queue", description="Показать текущую очередь")
async def queue_command(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ Бот не в голосовом канале!")
    queue = get_queue(interaction.guild.id)
    embed = create_player_embed(vc, queue)
    await interaction.response.send_message(embed=embed, view=PlayerView())


@tree.command(name="now", description="Показать что играет сейчас")
async def now(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc or not vc.current:
        return await interaction.response.send_message("❌ Сейчас ничего не играет!")
    queue = get_queue(interaction.guild.id)
    embed = create_player_embed(vc, queue)
    await interaction.response.send_message(embed=embed, view=PlayerView())


@tree.command(name="skip", description="Пропустить текущий трек")
async def skip(interaction: discord.Interaction):
    await interaction.response.defer()
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc or not vc.current:
        return await interaction.followup.send("❌ Сейчас ничего не играет!")
    queue = get_queue(interaction.guild.id)
    if queue:
        await vc.stop()
        await interaction.followup.send("⏭️ Трек пропущен! Следующий в очереди...")
    else:
        await vc.stop()
        await interaction.followup.send("⏭️ Трек пропущен! Очередь пуста.")
    await update_player_message(interaction, vc, queue, interaction)


@tree.command(name="stop", description="Остановить музыку и очистить очередь")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer()
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc:
        return await interaction.followup.send("❌ Бот не в голосовом канале!")
    if not vc.current:
        return await interaction.followup.send("❌ Сейчас ничего не играет!")
    queue = get_queue(interaction.guild.id)
    queue.clear()
    await vc.stop()
    await interaction.followup.send("⏹️ Музыка остановлена! Очередь очищена!")
    await update_player_message(interaction, vc, queue, interaction)


@tree.command(name="clear", description="Очистить очередь без остановки текущей песни")
async def clear(interaction: discord.Interaction):
    await interaction.response.defer()
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc:
        return await interaction.followup.send("❌ Бот не в голосовом канале!")
    queue = get_queue(interaction.guild.id)
    count = len(queue)
    if count == 0:
        return await interaction.followup.send("📭 Очередь уже пуста!")
    queue.clear()
    await interaction.followup.send(f"🗑️ Очищено {count} треков из очереди!")
    await update_player_message(interaction, vc, queue, interaction)


@tree.command(name="pause", description="Поставить на паузу")
async def pause(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc or not vc.current:
        return await interaction.response.send_message("❌ Сейчас ничего не играет!")
    if vc.paused:
        return await interaction.response.send_message("⏸️ Уже на паузе!")
    await vc.pause(True)
    await interaction.response.send_message("⏸️ На паузе!")
    queue = get_queue(interaction.guild.id)
    await update_player_message(interaction, vc, queue, interaction)


@tree.command(name="resume", description="Продолжить воспроизведение")
async def resume(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc or not vc.current:
        return await interaction.response.send_message("❌ Сейчас ничего не играет!")
    if not vc.paused:
        return await interaction.response.send_message("▶️ Уже играет!")
    await vc.pause(False)
    await interaction.response.send_message("▶️ Продолжаю!")
    queue = get_queue(interaction.guild.id)
    await update_player_message(interaction, vc, queue, interaction)


@tree.command(name="leave", description="Отключить бота и очистить очередь")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer()
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc:
        return await interaction.followup.send("❌ Бот не в голосовом канале!")
    queue = get_queue(interaction.guild.id)
    queue.clear()
    await vc.disconnect()
    await interaction.followup.send("👋 Бот отключён! Очередь очищена!")


# ========== СОБЫТИЯ WAVELINK ==========

@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    vc: wavelink.Player = payload.player
    if not vc or not vc.guild or not vc.channel:
        return
    guild_id = vc.guild.id
    reason = str(payload.reason) if payload.reason else "unknown"
    if reason.lower() in ["stopped", "replaced"]:
        return
    await asyncio.sleep(0.5)
    await play_next_track(vc, guild_id)


@bot.event
async def on_wavelink_track_exception(payload: wavelink.TrackExceptionEventPayload):
    logger.error(f"❌ Ошибка воспроизведения: {payload.exception}")
    vc: wavelink.Player = payload.player
    if vc and vc.guild:
        guild_id = vc.guild.id
        await play_next_track(vc, guild_id)


@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    vc: wavelink.Player = payload.player
    if vc and vc.guild:
        guild_id = vc.guild.id
        queue = get_queue(guild_id)
        if guild_id in player_messages:
            for channel_id, msg in list(player_messages[guild_id].items()):
                try:
                    embed = create_player_embed(vc, queue)
                    await msg.edit(embed=embed, view=PlayerView())
                except:
                    pass


async def health_check(request):
    return web.Response(text="OK")


async def run_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    print("✅ Web server started on port 8080")


async def main():
    asyncio.create_task(run_web_server())
    token = os.getenv("DISCORD_TOKEN")
    await bot.start("-")


if __name__ == "__main__":
    asyncio.run(main())