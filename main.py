import discord
from discord.ext import commands
import wavelink
from collections import deque
import re
import os
import asyncio
from aiohttp import web

# Создаём бота с префиксными командами
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=">", intents=intents)

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
        print("⚠️ LAVALINK_HOST не задан, использую http://localhost:2333")
    else:
        print(f"✅ LAVALINK_HOST: {lavalink_host}")

    if not lavalink_password:
        lavalink_password = "youshallnotpass"
        print("⚠️ LAVALINK_PASSWORD не задан, использую стандартный пароль")

    # Для Railway используем wss:// если это HTTPS
    if lavalink_host.startswith("https://"):
        # Заменяем https:// на wss:// для WebSocket соединения
        uri = lavalink_host.replace("https://", "wss://")
        print(f"🔒 Использую WebSocket Secure: {uri}")
    else:
        uri = lavalink_host.replace("http://", "ws://")
        print(f"🔓 Использую WebSocket: {uri}")

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


# Событие при запуске
@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user} запущен!")
    await connect_nodes()


# Событие при подключении Lavalink
@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"✅ Узел {payload.node.identifier} готов!")


# Событие при отключении Lavalink
@bot.event
async def on_wavelink_node_disconnected(payload: wavelink.NodeDisconnectedEventPayload):
    print(f"❌ Узел {payload.node.identifier} отключился!")


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
    embed = create_player_embed(vc, queue)

    guild_id = ctx.guild.id
    channel_id = ctx.channel.id

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
        except:
            pass

    msg = await ctx.send(embed=embed, view=PlayerView())
    player_messages[guild_id][channel_id] = msg


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


# Событие при окончании трека
@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    vc: wavelink.Player = payload.player

    if not vc:
        return

    guild_id = vc.guild.id
    queue = get_queue(guild_id)

    if queue:
        next_track = queue.popleft()
        try:
            await vc.play(next_track)
            print(f"▶️ Автоматически играет следующий трек: {next_track.title}")
        except Exception as e:
            print(f"Ошибка при воспроизведении следующего трека: {e}")

        if guild_id in player_messages:
            for channel_id, msg in player_messages[guild_id].items():
                try:
                    embed = create_player_embed(vc, queue)
                    await msg.edit(embed=embed, view=PlayerView())
                except:
                    pass
    else:
        print("⏹️ Очередь пуста")

        if guild_id in player_messages:
            for channel_id, msg in player_messages[guild_id].items():
                try:
                    embed = create_player_embed(vc, queue)
                    await msg.edit(embed=embed, view=PlayerView())
                except:
                    pass


# ========== КОМАНДЫ ==========

# >play
@bot.command(name="play", description="Воспроизвести музыку или добавить в очередь")
async def play(ctx: commands.Context, *, search: str):
    if not ctx.author.voice:
        return await ctx.send("❌ Вы не в голосовом канале!")

    channel = ctx.author.voice.channel
    vc: wavelink.Player = ctx.voice_client

    if not vc:
        vc = await channel.connect(cls=wavelink.Player)
    elif vc.channel.id != channel.id:
        await vc.move_to(channel)

    await ctx.send(f"🔍 Ищу: `{search}`...")

    tracks = await search_tracks(search)

    if tracks is None:
        return await ctx.send("❌ Ошибка при поиске! Попробуйте использовать ссылку или другой запрос.")

    if not tracks:
        return await ctx.send("❌ Ничего не найдено! Попробуйте другой запрос или используйте ссылку на YouTube.")

    track = tracks[0]
    queue = get_queue(ctx.guild.id)

    if vc.current:
        queue.append(track)
        position = len(queue)
        await ctx.send(f"✅ Добавлено в очередь (позиция {position}): `{track.title}`")
    else:
        try:
            await vc.play(track)
            await ctx.send(f"▶️ Сейчас играет: `{track.title}`")
            await update_player_message(ctx, vc, queue)
        except Exception as e:
            print(f"Ошибка воспроизведения: {e}")
            await ctx.send(f"❌ Ошибка воспроизведения: {e}")


# >queue
@bot.command(name="queue", description="Показать текущую очередь")
async def queue_command(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client

    if not vc:
        return await ctx.send("❌ Бот не в голосовом канале!")

    queue = get_queue(ctx.guild.id)
    embed = create_player_embed(vc, queue)
    await ctx.send(embed=embed, view=PlayerView())


# >now
@bot.command(name="now", description="Показать что играет сейчас")
async def now(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client

    if not vc or not vc.current:
        return await ctx.send("❌ Сейчас ничего не играет!")

    queue = get_queue(ctx.guild.id)
    embed = create_player_embed(vc, queue)
    await ctx.send(embed=embed, view=PlayerView())


# >skip
@bot.command(name="skip", description="Пропустить текущий трек")
async def skip(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client

    if not vc or not vc.current:
        return await ctx.send("❌ Сейчас ничего не играет!")

    queue = get_queue(ctx.guild.id)

    if queue:
        await vc.stop()
        await ctx.send("⏭️ Трек пропущен! Следующий в очереди...")
    else:
        await vc.stop()
        await ctx.send("⏭️ Трек пропущен! Очередь пуста.")

    await update_player_message(ctx, vc, queue)


# >stop
@bot.command(name="stop", description="Остановить музыку и очистить очередь")
async def stop(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client

    if not vc:
        return await ctx.send("❌ Бот не в голосовом канале!")

    if not vc.current:
        return await ctx.send("❌ Сейчас ничего не играет!")

    queue = get_queue(ctx.guild.id)
    queue.clear()

    await vc.stop()
    await ctx.send("⏹️ Музыка остановлена! Очередь очищена!")

    await update_player_message(ctx, vc, queue)


# >clear
@bot.command(name="clear", description="Очистить очередь без остановки текущей песни")
async def clear(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client

    if not vc:
        return await ctx.send("❌ Бот не в голосовом канале!")

    queue = get_queue(ctx.guild.id)
    count = len(queue)

    if count == 0:
        return await ctx.send("📭 Очередь уже пуста!")

    queue.clear()
    await ctx.send(f"🗑️ Очищено {count} треков из очереди!")

    await update_player_message(ctx, vc, queue)


# >pause
@bot.command(name="pause", description="Поставить на паузу")
async def pause(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client

    if not vc or not vc.current:
        return await ctx.send("❌ Сейчас ничего не играет!")

    if vc.paused:
        return await ctx.send("⏸️ Уже на паузе!")

    await vc.pause(True)
    await ctx.send("⏸️ На паузе!")

    queue = get_queue(ctx.guild.id)
    await update_player_message(ctx, vc, queue)


# >resume
@bot.command(name="resume", description="Продолжить воспроизведение")
async def resume(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client

    if not vc or not vc.current:
        return await ctx.send("❌ Сейчас ничего не играет!")

    if not vc.paused:
        return await ctx.send("▶️ Уже играет!")

    await vc.pause(False)
    await ctx.send("▶️ Продолжаю!")

    queue = get_queue(ctx.guild.id)
    await update_player_message(ctx, vc, queue)


# >leave
@bot.command(name="leave", description="Отключить бота и очистить очередь")
async def leave(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client

    if not vc:
        return await ctx.send("❌ Бот не в голосовом канале!")

    queue = get_queue(ctx.guild.id)
    queue.clear()

    await vc.disconnect()
    await ctx.send("👋 Бот отключён! Очередь очищена!")


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