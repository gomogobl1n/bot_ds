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


async def connect_nodes():
    """Подключение к Lavalink"""
    await bot.wait_until_ready()

    lavalink_host = os.getenv("LAVALINK_HOST", "http://78.154.103.11:15193/")
    lavalink_password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")

    # Преобразуем URL для WebSocket
    if lavalink_host.startswith("https://"):
        uri = lavalink_host.replace("https://", "wss://")
    else:
        uri = lavalink_host.replace("http://", "ws://")

    # Убираем порт из URI если он есть
    if ":" in uri and not uri.startswith("ws"):
        # Оставляем только хост и порт
        uri = uri.split(":")[0] + ":" + uri.split(":")[1]

    try:
        node = wavelink.Node(
            uri=uri,
            password=lavalink_password
        )
        await wavelink.Pool.connect(client=bot, nodes=[node])
        logger.info(f"✅ Lavalink подключён к {lavalink_host}!")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Lavalink: {e}")


@bot.event
async def on_ready():
    logger.info(f"✅ Бот {bot.user} запущен!")
    await connect_nodes()
    await bot.tree.sync()
    logger.info("✅ Слеш-команды синхронизированы!")


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    """Событие готовности узла (wavelink 3.5.2)"""
    logger.info(f"✅ Узел {payload.node.identifier} готов!")


@bot.event
async def on_wavelink_node_disconnected(payload: wavelink.NodeDisconnectedEventPayload):
    """Событие отключения узла"""
    logger.error(f"❌ Узел {payload.node.identifier} отключился!")


def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = deque()
    return queues[guild_id]


def is_url(string):
    url_pattern = re.compile(r'^https?://')
    return bool(url_pattern.match(string))


async def search_tracks(search_query):
    """Поиск треков"""
    try:
        if is_url(search_query):
            tracks = await wavelink.Playable.search(search_query)
            return tracks

        tracks = await wavelink.Playable.search(f"ytsearch:{search_query}")

        if not tracks:
            tracks = await wavelink.Playable.search(search_query)

        return tracks
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        return None


def create_player_embed(vc: wavelink.Player, queue):
    """Создание embed для плеера"""
    current = vc.current
    status = "⏸️ На паузе" if vc.paused else "▶️ Играет"

    embed = discord.Embed(
        title="🎵 Музыкальный плеер",
        description=f"**{status}**",
        color=discord.Color.blue()
    )

    if current:
        # Получаем длительность в минутах:секундах
        duration = current.length
        minutes = duration // 60000
        seconds = (duration % 60000) // 1000
        duration_str = f"{minutes}:{seconds:02d}"

        # Получаем название трека
        title = current.title if hasattr(current, 'title') else str(current)

        embed.add_field(
            name="🎶 Сейчас играет",
            value=f"**{title}**\n⏱️ {duration_str}",
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
            track_title = track.title if hasattr(track, 'title') else str(track)
            queue_text += f"`{i}.` {track_title}\n"
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


async def update_player_message(ctx, vc: wavelink.Player, queue, interaction=None):
    """Обновление сообщения плеера"""
    try:
        embed = create_player_embed(vc, queue)

        if hasattr(ctx, 'guild'):
            guild_id = ctx.guild.id
            channel_id = ctx.channel.id
        else:
            guild_id = interaction.guild.id
            channel_id = interaction.channel.id

        if guild_id not in player_messages:
            player_messages[guild_id] = {}

        # Если это взаимодействие (кнопка)
        if interaction:
            try:
                await interaction.edit_original_response(embed=embed, view=PlayerView())
                return
            except Exception as e:
                logger.error(f"Ошибка редактирования через interaction: {e}")

        # Если есть сохраненное сообщение
        if channel_id in player_messages[guild_id]:
            try:
                msg = player_messages[guild_id][channel_id]
                await msg.edit(embed=embed, view=PlayerView())
                return
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"Ошибка обновления сообщения: {e}")

        # Создаем новое сообщение
        if hasattr(ctx, 'send'):
            msg = await ctx.send(embed=embed, view=PlayerView())
            player_messages[guild_id][channel_id] = msg
        elif hasattr(ctx, 'followup'):
            msg = await ctx.followup.send(embed=embed, view=PlayerView())
            player_messages[guild_id][channel_id] = msg

    except Exception as e:
        logger.error(f"Критическая ошибка в update_player_message: {e}")


async def play_next_track(vc: wavelink.Player, guild_id: int):
    """Воспроизведение следующего трека"""
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
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Кнопка паузы/возобновления"""
        try:
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
            else:
                await interaction.followup.send("❌ Нет активного плеера", ephemeral=True)
        except Exception as e:
            logger.error(f"Ошибка в pause_button: {e}")
            try:
                await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Кнопка остановки"""
        try:
            await interaction.response.defer()

            vc: wavelink.Player = interaction.guild.voice_client
            if vc and vc.current:
                queue = get_queue(interaction.guild.id)
                queue.clear()
                await vc.stop()
                await update_player_message(interaction, vc, queue, interaction)
                await interaction.followup.send("⏹️ Музыка остановлена! Очередь очищена!", ephemeral=True)
            else:
                await interaction.followup.send("❌ Нет активного плеера", ephemeral=True)
        except Exception as e:
            logger.error(f"Ошибка в stop_button: {e}")
            try:
                await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.primary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Кнопка пропуска"""
        try:
            await interaction.response.defer()

            vc: wavelink.Player = interaction.guild.voice_client
            if vc and vc.current:
                queue = get_queue(interaction.guild.id)

                if queue:
                    await vc.stop()
                    await interaction.followup.send("⏭️ Трек пропущен! Следующий в очереди...", ephemeral=True)
                else:
                    await vc.stop()
                    await interaction.followup.send("⏭️ Трек пропущен! Очередь пуста.", ephemeral=True)

                await update_player_message(interaction, vc, queue, interaction)
            else:
                await interaction.followup.send("❌ Нет активного плеера", ephemeral=True)
        except Exception as e:
            logger.error(f"Ошибка в skip_button: {e}")
            try:
                await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="⏏️", style=discord.ButtonStyle.secondary)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Кнопка отключения"""
        try:
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
            else:
                await interaction.followup.send("❌ Бот не в голосовом канале", ephemeral=True)
        except Exception as e:
            logger.error(f"Ошибка в leave_button: {e}")
            try:
                await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            except:
                pass


# ========== СЛЕШ-КОМАНДЫ ==========

@bot.tree.command(name="play", description="Воспроизвести музыку или добавить в очередь")
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
            return await interaction.followup.send("❌ Ошибка при поиске!")

        if not tracks:
            return await interaction.followup.send("❌ Ничего не найдено!")

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
                await interaction.followup.send(f"❌ Ошибка воспроизведения: {e}")
    except Exception as e:
        await interaction.followup.send(f"❌ Произошла ошибка: {e}")


@bot.tree.command(name="queue", description="Показать текущую очередь")
async def queue_command(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ Бот не в голосовом канале!")
    queue = get_queue(interaction.guild.id)
    embed = create_player_embed(vc, queue)
    await interaction.response.send_message(embed=embed, view=PlayerView())


@bot.tree.command(name="now", description="Показать что играет сейчас")
async def now(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc or not vc.current:
        return await interaction.response.send_message("❌ Сейчас ничего не играет!")
    queue = get_queue(interaction.guild.id)
    embed = create_player_embed(vc, queue)
    await interaction.response.send_message(embed=embed, view=PlayerView())


@bot.tree.command(name="skip", description="Пропустить текущий трек")
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


@bot.tree.command(name="stop", description="Остановить музыку и очистить очередь")
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


@bot.tree.command(name="clear", description="Очистить очередь без остановки текущей песни")
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


@bot.tree.command(name="pause", description="Поставить на паузу")
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


@bot.tree.command(name="resume", description="Продолжить воспроизведение")
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


@bot.tree.command(name="leave", description="Отключить бота и очистить очередь")
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
    """Событие окончания трека"""
    vc: wavelink.Player = payload.player
    if not vc or not vc.guild or not vc.channel:
        return

    guild_id = vc.guild.id
    reason = str(payload.reason) if payload.reason else "unknown"

    # Если трек был остановлен вручную, но в очереди есть треки
    if reason.lower() == "stopped":
        queue = get_queue(guild_id)
        if queue:
            await asyncio.sleep(0.5)
            await play_next_track(vc, guild_id)
        return

    if reason.lower() == "replaced":
        return

    await asyncio.sleep(0.5)
    await play_next_track(vc, guild_id)


@bot.event
async def on_wavelink_track_exception(payload: wavelink.TrackExceptionEventPayload):
    """Событие ошибки воспроизведения"""
    logger.error(f"❌ Ошибка воспроизведения: {payload.exception}")
    vc: wavelink.Player = payload.player
    if vc and vc.guild:
        guild_id = vc.guild.id
        await play_next_track(vc, guild_id)


@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    """Событие начала воспроизведения трека"""
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


# ========== WEB СЕРВЕР ДЛЯ HEALTH CHECK ==========

async def health_check(request):
    return web.Response(text="OK")


async def run_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("✅ Web server started on port 8080")


# ========== ЗАПУСК БОТА ==========

async def main():
    asyncio.create_task(run_web_server())
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("❌ DISCORD_TOKEN не установлен!")
        return
    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())