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

# Создаём бота с поддержкой слэш-команд
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=None, intents=intents)

# Словарь для хранения очередей для каждого голосового канала
queues = {}

# Словарь для хранения сообщений плеера
player_messages = {}


# Подключение к Lavalink (с переменными окружения)
async def connect_nodes():
    await bot.wait_until_ready()

    # Читаем переменные окружения
    lavalink_host = os.getenv("LAVALINK_HOST")
    lavalink_password = os.getenv("LAVALINK_PASSWORD")

    # Если переменные не заданы - используем значения по умолчанию
    if not lavalink_host:
        lavalink_host = "http://localhost:2333"
        logger.warning("LAVALINK_HOST не задан, использую http://localhost:2333")
    else:
        logger.info(f"LAVALINK_HOST: {lavalink_host}")

    if not lavalink_password:
        lavalink_password = "youshallnotpass"
        logger.warning("LAVALINK_PASSWORD не задан, использую стандартный пароль")

    # Для Railway используем wss:// если это HTTPS
    if lavalink_host.startswith("https://"):
        uri = lavalink_host.replace("https://", "wss://")
        logger.info(f"Использую WebSocket Secure: {uri}")
    else:
        uri = lavalink_host.replace("http://", "ws://")
        logger.info(f"Использую WebSocket: {uri}")

    try:
        node = wavelink.Node(
            identifier="Node1",
            uri=uri,
            password=lavalink_password
        )

        await wavelink.Pool.connect(client=bot, nodes=[node])
        logger.info(f"Lavalink подключён к {lavalink_host}!")
    except Exception as e:
        logger.error(f"Ошибка подключения к Lavalink: {e}")
        # Не падаем, а просто логируем ошибку


# Событие при запуске
@bot.event
async def on_ready():
    logger.info(f"Бот {bot.user} запущен!")
    await connect_nodes()
    try:
        synced = await bot.tree.sync()
        logger.info(f"Синхронизировано {len(synced)} слэш-команд!")
    except Exception as e:
        logger.error(f"Ошибка синхронизации команд: {e}")


# Событие при подключении Lavalink
@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    logger.info(f"Узел {payload.node.identifier} готов!")


# Событие при отключении Lavalink
@bot.event
async def on_wavelink_node_disconnected(payload: wavelink.NodeDisconnectedEventPayload):
    logger.error(f"Узел {payload.node.identifier} отключился!")


# Функция для получения очереди по голосовому каналу
def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = deque()
    return queues[guild_id]


# Функция для проверки, является ли строка URL
def is_url(string):
    url_pattern = re.compile(r'^https?://')
    return bool(url_pattern.match(string))


# Функция для поиска треков
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
        logger.error(f"Ошибка поиска: {e}")
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
async def update_player_message(interaction, vc: wavelink.Player, queue):
    try:
        embed = create_player_embed(vc, queue)

        guild_id = interaction.guild.id
        channel_id = interaction.channel.id

        if guild_id not in player_messages:
            player_messages[guild_id] = {}

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
        msg = await interaction.followup.send(embed=embed, view=PlayerView(), wait=True)
        player_messages[guild_id][channel_id] = msg
    except Exception as e:
        logger.error(f"Критическая ошибка в update_player_message: {e}")


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
            await update_player_message(interaction, vc, queue)

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        vc: wavelink.Player = interaction.guild.voice_client
        if vc and vc.current:
            queue = get_queue(interaction.guild.id)
            queue.clear()
            await vc.stop()
            await update_player_message(interaction, vc, queue)

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.primary)
    async def skip_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        vc: wavelink.Player = interaction.guild.voice_client
        if vc and vc.current:
            queue = get_queue(interaction.guild.id)
            await vc.stop()
            await update_player_message(interaction, vc, queue)

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


# Событие при окончании трека
@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    vc: wavelink.Player = payload.player

    if not vc or not vc.guild:
        return

    if not vc.channel:
        return

    guild_id = vc.guild.id
    queue = get_queue(guild_id)

    if payload.reason == wavelink.TrackEndReason.STOPPED:
        return

    if queue:
        next_track = queue.popleft()
        try:
            await vc.play(next_track)
            logger.info(f"Автоматически играет следующий трек: {next_track.title}")
        except Exception as e:
            logger.error(f"Ошибка при воспроизведении следующего трека: {e}")
            return

        if vc and vc.channel:
            try:
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
                logger.error(f"Ошибка в on_wavelink_track_end: {e}")


# ========== СЛЭШ-КОМАНДЫ ==========

@bot.tree.command(name="play", description="Воспроизвести музыку или добавить в очередь")
async def play(interaction: discord.Interaction, search: str):
    await interaction.response.defer()

    try:
        if not interaction.user.voice:
            await interaction.followup.send("❌ Вы не в голосовом канале!", ephemeral=True)
            return

        channel = interaction.user.voice.channel
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc:
            vc = await channel.connect(cls=wavelink.Player)
        elif vc.channel.id != channel.id:
            await vc.move_to(channel)

        await interaction.followup.send(f"🔍 Ищу: `{search}`...")

        tracks = await search_tracks(search)

        if tracks is None:
            await interaction.edit_original_response(
                content="❌ Ошибка при поиске! Попробуйте использовать ссылку или другой запрос.")
            return

        if not tracks:
            await interaction.edit_original_response(
                content="❌ Ничего не найдено! Попробуйте другой запрос или используйте ссылку на YouTube.")
            return

        track = tracks[0]
        queue = get_queue(interaction.guild.id)

        if vc.current:
            queue.append(track)
            position = len(queue)
            await interaction.edit_original_response(
                content=f"✅ Добавлено в очередь (позиция {position}): `{track.title}`")
        else:
            try:
                await vc.play(track)
                await interaction.edit_original_response(content=f"▶️ Сейчас играет: `{track.title}`")
                await update_player_message(interaction, vc, queue)
            except Exception as e:
                logger.error(f"Ошибка воспроизведения: {e}")
                await interaction.edit_original_response(content=f"❌ Ошибка воспроизведения: {e}")
    except Exception as e:
        logger.error(f"Критическая ошибка в play: {e}")
        await interaction.followup.send(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)


@bot.tree.command(name="queue", description="Показать текущую очередь")
async def queue_command(interaction: discord.Interaction):
    try:
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc:
            await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            return

        queue = get_queue(interaction.guild.id)
        embed = create_player_embed(vc, queue)
        await interaction.response.send_message(embed=embed, view=PlayerView())
    except Exception as e:
        logger.error(f"Ошибка в queue: {e}")
        await interaction.response.send_message(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)


@bot.tree.command(name="now", description="Показать что играет сейчас")
async def now(interaction: discord.Interaction):
    try:
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc or not vc.current:
            await interaction.response.send_message("❌ Сейчас ничего не играет!", ephemeral=True)
            return

        queue = get_queue(interaction.guild.id)
        embed = create_player_embed(vc, queue)
        await interaction.response.send_message(embed=embed, view=PlayerView())
    except Exception as e:
        logger.error(f"Ошибка в now: {e}")
        await interaction.response.send_message(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)


@bot.tree.command(name="skip", description="Пропустить текущий трек")
async def skip(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc or not vc.current:
            await interaction.followup.send("❌ Сейчас ничего не играет!", ephemeral=True)
            return

        queue = get_queue(interaction.guild.id)

        if queue:
            await vc.stop()
            await interaction.followup.send("⏭️ Трек пропущен! Следующий в очереди...")
        else:
            await vc.stop()
            await interaction.followup.send("⏭️ Трек пропущен! Очередь пуста.")

        await update_player_message(interaction, vc, queue)
    except Exception as e:
        logger.error(f"Ошибка в skip: {e}")
        await interaction.followup.send(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)


@bot.tree.command(name="stop", description="Остановить музыку и очистить очередь")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc:
            await interaction.followup.send("❌ Бот не в голосовом канале!", ephemeral=True)
            return

        if not vc.current:
            await interaction.followup.send("❌ Сейчас ничего не играет!", ephemeral=True)
            return

        queue = get_queue(interaction.guild.id)
        queue.clear()

        await vc.stop()
        await interaction.followup.send("⏹️ Музыка остановлена! Очередь очищена!")

        await update_player_message(interaction, vc, queue)
    except Exception as e:
        logger.error(f"Ошибка в stop: {e}")
        await interaction.followup.send(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)


@bot.tree.command(name="clear", description="Очистить очередь без остановки текущей песни")
async def clear(interaction: discord.Interaction):
    try:
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc:
            await interaction.response.send_message("❌ Бот не в голосовом канале!", ephemeral=True)
            return

        queue = get_queue(interaction.guild.id)
        count = len(queue)

        if count == 0:
            await interaction.response.send_message("📭 Очередь уже пуста!", ephemeral=True)
            return

        queue.clear()
        await interaction.response.send_message(f"🗑️ Очищено {count} треков из очереди!")

        await update_player_message(interaction, vc, queue)
    except Exception as e:
        logger.error(f"Ошибка в clear: {e}")
        await interaction.response.send_message(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)


@bot.tree.command(name="pause", description="Поставить на паузу")
async def pause(interaction: discord.Interaction):
    try:
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc or not vc.current:
            await interaction.response.send_message("❌ Сейчас ничего не играет!", ephemeral=True)
            return

        if vc.paused:
            await interaction.response.send_message("⏸️ Уже на паузе!", ephemeral=True)
            return

        await vc.pause(True)
        await interaction.response.send_message("⏸️ На паузе!")

        queue = get_queue(interaction.guild.id)
        await update_player_message(interaction, vc, queue)
    except Exception as e:
        logger.error(f"Ошибка в pause: {e}")
        await interaction.response.send_message(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)


@bot.tree.command(name="resume", description="Продолжить воспроизведение")
async def resume(interaction: discord.Interaction):
    try:
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc or not vc.current:
            await interaction.response.send_message("❌ Сейчас ничего не играет!", ephemeral=True)
            return

        if not vc.paused:
            await interaction.response.send_message("▶️ Уже играет!", ephemeral=True)
            return

        await vc.pause(False)
        await interaction.response.send_message("▶️ Продолжаю!")

        queue = get_queue(interaction.guild.id)
        await update_player_message(interaction, vc, queue)
    except Exception as e:
        logger.error(f"Ошибка в resume: {e}")
        await interaction.response.send_message(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)


@bot.tree.command(name="leave", description="Отключить бота и очистить очередь")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        vc: wavelink.Player = interaction.guild.voice_client

        if not vc:
            await interaction.followup.send("❌ Бот не в голосовом канале!", ephemeral=True)
            return

        queue = get_queue(interaction.guild.id)
        queue.clear()

        await vc.disconnect()
        await interaction.followup.send("👋 Бот отключён! Очередь очищена!")
    except Exception as e:
        logger.error(f"Ошибка в leave: {e}")
        await interaction.followup.send(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)


# ========== HTTP-сервер для Railway ==========

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
    # Запускаем веб-сервер в фоне
    asyncio.create_task(run_web_server())

    # Запускаем бота
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("❌ Ошибка: DISCORD_TOKEN не найден в переменных окружения!")
        return

    await bot.start(token)


# ========== ЗАПУСК ==========

if __name__ == "__main__":
    asyncio.run(main())