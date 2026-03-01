import discord
from discord import app_commands
import yt_dlp
import asyncio
import os
import logging
import functools
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import re
from dotenv import load_dotenv
from enum import Enum
import random

load_dotenv()


class RepeatMode(str, Enum):
    NONE = "none"
    SONG = "song"
    QUEUE = "queue"


class SearchPlatform(str, Enum):
    AUTO = "auto"
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"


# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger('discord.player').setLevel(logging.WARNING)
# Suppress noise about console usage from errors
yt_dlp.utils.bug_reports_message = lambda *args, **kwargs: ''

# --- Spotify API Setup ---
try:
    spotify = spotipy.Spotify(auth_manager=SpotifyClientCredentials())
except Exception as e:
    logging.error(f"Failed to initialize Spotify client: {e}")
    spotify = None

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',  # Default to YouTube search
    'source_address': '0.0.0.0',  # bind to ipv4 since ipv6 addresses cause issues sometimes
    'ignoreconfig': True,
    'no_cachedir': True
}

ffmpeg_options = {
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

def log_command(func):
    @functools.wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        logging.info(f"Command '{func.__name__}' invoked by {interaction.user} (ID: {interaction.user.id}) with args: {args} {kwargs}")
        try:
            await func(interaction, *args, **kwargs)
            logging.info(f"Command '{func.__name__}' executed successfully.")
        except Exception as e:
            logging.error(f"Command '{func.__name__}' failed with error: {e}", exc_info=True)
            if interaction.response.is_done():
                await interaction.followup.send("An error occurred while processing the command.", ephemeral=True)
            else:
                await interaction.response.send_message("An error occurred while processing the command.", ephemeral=True)
    return wrapper

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.platform = data.get('extractor_key')
        self.duration = int(data.get('duration')) if data.get('duration') else 0

    @property
    def duration_fmt(self):
        if self.duration == 0:
            return "N/A"
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02}:{minutes:02}:{seconds:02}"
        return f"{minutes:02}:{seconds:02}"

    def clone(self, seek: int = 0):
        """Creates a new FFmpegPCMAudio instance from the same URL, optionally seeking to a specific time."""
        options = ffmpeg_options.copy()
        if seek > 0:
            options['before_options'] = f"-ss {seek}"
        
        return self.__class__(discord.FFmpegPCMAudio(self.data['url'], **options), data=self.data)

    @classmethod
    async def from_search(cls, query, *, loop=None, stream=False, platform: SearchPlatform = SearchPlatform.AUTO, timeout: float = 10.0):
        loop = loop or asyncio.get_event_loop()

        if platform == SearchPlatform.YOUTUBE:
            logging.info(f"Searching on YouTube for: '{query}'")
            return await cls.from_url(f"ytsearch:{query}", loop=loop, stream=stream, timeout=timeout)
        
        if platform == SearchPlatform.SOUNDCLOUD:
            logging.info(f"Searching on SoundCloud for: '{query}'")
            return await cls.from_url(f"scsearch:{query}", loop=loop, stream=stream, timeout=timeout)

        # Prioritized search (auto)
        # 1. Spotify
        if spotify:
            try:
                result = spotify.search(q=query, type='track', limit=1)
                if result and result['tracks']['items']:
                    track = result['tracks']['items'][0]
                    artist = track['artists'][0]['name']
                    title = track['name']
                    search_query = f"{artist} - {title}"
                    logging.info(f"Found on Spotify: '{search_query}'. Searching on YouTube.")
                    return await cls.from_url(f"ytsearch:{search_query}", loop=loop, stream=stream, timeout=timeout)
            except Exception as e:
                logging.error(f"Spotify search failed: {e}")

        # 2. YouTube
        logging.info(f"Searching on YouTube for: '{query}'")
        results = await cls.from_url(f"ytsearch:{query}", loop=loop, stream=stream, timeout=timeout)
        if results:
            return results

        # 3. SoundCloud
        logging.info(f"Searching on SoundCloud for: '{query}'")
        return await cls.from_url(f"scsearch:{query}", loop=loop, stream=stream, timeout=timeout)


    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False, timeout: float = 10.0):
        loop = loop or asyncio.get_event_loop()
        try:
            data = await asyncio.wait_for(loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream)), timeout=timeout)
            if not data:
                return None
            if 'entries' in data:
                players = []
                for entry in data['entries']:
                    if entry:
                        logging.info(f"Found song on {entry.get('extractor_key')}: {entry.get('title')}")
                        players.append(cls(discord.FFmpegPCMAudio(entry['url'], **ffmpeg_options), data=entry))
                return players
            filename = data['url'] if stream else ytdl.prepare_filename(data)
            logging.info(f"Found song on {data.get('extractor_key')}: {data.get('title')}")
            return [cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)]
        except asyncio.TimeoutError:
            logging.error(f"Search for '{url}' timed out.")
            return None
        except Exception as e:
            logging.error(f"Failed to get song from URL '{url}': {e}")
            return None


class MusicBot:
    def __init__(self, bot):
        self.bot = bot
        self.queue = []
        self.current_song = None
        self.voice_client = None
        self.text_channel = None
        self.repeat_mode = RepeatMode.NONE

    async def ensure_voice_channel(self, interaction: discord.Interaction):
        if self.voice_client is None:
            if interaction.user.voice:
                self.voice_client = await interaction.user.voice.channel.connect()
                self.text_channel = interaction.channel
            else:
                await interaction.response.send_message("You are not connected to a voice channel.", ephemeral=True)
                return False
        return True

    def play_next(self, error=None):
        if error:
            logging.error(f'Player error: {error}', exc_info=True)
            if isinstance(error, discord.errors.ConnectionClosed):
                logging.warning("Connection closed, attempting to play next song.")
                return self.play_next()

        if self.repeat_mode == RepeatMode.SONG and self.current_song:
            to_play = self.current_song.clone()
            self.voice_client.play(to_play, after=self.play_next)
            return

        if self.repeat_mode == RepeatMode.QUEUE and self.current_song:
            self.queue.append(self.current_song)

        if not self.queue:
            self.current_song = None
            asyncio.run_coroutine_threadsafe(self.text_channel.send('Queue finished.'), self.bot.loop)
            if self.repeat_mode != RepeatMode.QUEUE and self.voice_client:
                asyncio.run_coroutine_threadsafe(self.voice_client.disconnect(), self.bot.loop)
                self.voice_client = None
            return

        self.current_song = self.queue.pop(0)
        to_play = self.current_song.clone()
        self.voice_client.play(to_play, after=self.play_next)
        asyncio.run_coroutine_threadsafe(self.text_channel.send(f"Now playing: **{self.current_song.title}** ({self.current_song.duration_fmt})"), self.bot.loop)

    async def seek(self, position: int):
        if not self.current_song or not self.voice_client.is_playing():
            return

        # Create the new player with the seek option
        new_player = self.current_song.clone(seek=position)
        
        # Stop the current player, wait briefly for the state to update, then play the new source
        self.voice_client.stop()
        await asyncio.sleep(0.1) # Small delay to allow the player to fully stop
        self.voice_client.play(new_player, after=self.play_next)

    def jump(self, position: int) -> bool:
        if not self.queue or not (1 <= position <= len(self.queue)):
            return False

        # The queue is 0-indexed, user position is 1-indexed
        target_index = position - 1

        # The song that was playing is now gone. The queue becomes
        # the song at the target position and everything after it.
        self.queue = self.queue[target_index:]

        # If a song is currently playing, stop it. The `after` callback (`play_next`)
        # will then play the song that is now at the start of the modified queue.
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        else:
            # If nothing was playing, we need to manually trigger the next song.
            self.play_next()
        
        return True


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
music_bot = MusicBot(client)


@tree.command(name="play", description="Plays a song from a URL or search query")
@app_commands.describe(
    query="The song URL or search query.",
    platform="The platform to search first (defaults to auto)."
)
@log_command
async def play(interaction: discord.Interaction, query: str, platform: SearchPlatform = SearchPlatform.AUTO):
    if not await music_bot.ensure_voice_channel(interaction):
        return

    await interaction.response.defer()

    # --- Spotify URL Handling ---
    if "spotify.com" in query:
        if not spotify:
            await interaction.followup.send("Spotify support is not configured.")
            return
        
        try:
            if "track" in query:
                track = spotify.track(query)
                artist = track['artists'][0]['name']
                title = track['name']
                query = f"{artist} - {title}"
                logging.info(f"Spotify track URL detected. Searching for '{query}' on YouTube.")

            elif "playlist" in query:
                await interaction.followup.send("Fetching playlist from Spotify...")
                results = spotify.playlist_tracks(query)
                tracks = results['items']
                
                # Handle paginated results from Spotify API
                while results['next']:
                    results = spotify.next(results)
                    tracks.extend(results['items'])

                if not tracks:
                    await interaction.edit_original_response(content="Could not find any tracks in that playlist.")
                    return

                # This function will run in the background
                async def add_playlist_songs_bg(initial_interaction: discord.Interaction):
                    search_tasks = []
                    for item in tracks:
                        track = item['track']
                        if track:
                            artist = track['artists'][0]['name']
                            title = track['name']
                            search_query = f"{artist} - {title}"
                            task = YTDLSource.from_search(search_query, loop=client.loop, stream=True, platform=SearchPlatform.YOUTUBE, timeout=30.0)
                            search_tasks.append(task)
                    
                    # If the queue is empty, find and play the first song immediately
                    if not music_bot.voice_client.is_playing() and not music_bot.voice_client.is_paused():
                        if search_tasks:
                            # Pop the first task to play it immediately
                            first_search_task = search_tasks.pop(0)
                            first_song_result = await first_search_task
                            if first_song_result:
                                music_bot.current_song = first_song_result[0]
                                music_bot.voice_client.play(music_bot.current_song.clone(), after=music_bot.play_next)
                                await initial_interaction.edit_original_response(content=f"Now playing: **{music_bot.current_song.title}** ({music_bot.current_song.duration_fmt})\n*Adding the rest of the playlist to the queue in the background...*")

                    # Process the rest of the songs concurrently
                    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

                    all_players = []
                    failed_count = 0
                    for result in search_results:
                        if isinstance(result, list) and result:
                            all_players.extend(result)
                        else:
                            failed_count += 1
                            if isinstance(result, Exception):
                                logging.warning(f"A song search failed during concurrent playlist processing: {result}")

                    music_bot.queue.extend(all_players)
                    
                    final_message = f"Finished adding {len(all_players)} more songs to the queue."
                    if failed_count > 0:
                        final_message += f" ({failed_count} songs failed to load)."
                    await initial_interaction.channel.send(final_message)

                # Start the background task
                client.loop.create_task(add_playlist_songs_bg(interaction))
                return

        except Exception as e:
            logging.error(f"Failed to process Spotify URL: {e}", exc_info=True)
            await interaction.edit_original_response(content="An error occurred while processing the Spotify URL.")
            return

    # --- Standard URL or Search Query Handling ---
    # If the user provides a youtube link with a playlist, only play the video
    if 'youtube.com/watch' in query and '&list=' in query:
        query = query.split('&list=')[0]

    if re.match(r'https?://', query):
         players = await YTDLSource.from_url(query, loop=client.loop, stream=True)
    else:
        players = await YTDLSource.from_search(query, loop=client.loop, stream=True, platform=platform)

    if not players:
        await interaction.edit_original_response(content='Could not find any songs to play.')
        return

    if music_bot.voice_client.is_playing() or music_bot.voice_client.is_paused() or music_bot.queue:
        music_bot.queue.extend(players)
        if len(players) > 1:
            await interaction.edit_original_response(content=f'Added {len(players)} songs to the queue.')
        else:
            await interaction.edit_original_response(content=f'Added to queue: **{players[0].title}**')
    else:
        # If the queue was empty, start playing the first song
        music_bot.current_song = players.pop(0)
        music_bot.queue.extend(players)
        music_bot.voice_client.play(music_bot.current_song.clone(), after=music_bot.play_next)
        await interaction.edit_original_response(content=f"Now playing: **{music_bot.current_song.title}** ({music_bot.current_song.duration_fmt})")




@tree.command(name="repeat", description="Sets the repeat mode.")
@app_commands.describe(mode="Choose repeat mode")
@log_command
async def repeat(interaction: discord.Interaction, mode: RepeatMode):
    music_bot.repeat_mode = mode
    await interaction.response.send_message(f"Repeat mode set to {mode.value}.")


@tree.command(name="spil", description="Plays a song from a URL or search query")
@log_command
async def spil(interaction: discord.Interaction):
    # Direct GIF link from the Tenor page provided
    gif_url = "https://c.tenor.com/mZZGULmGvRgAAAAd/gordon-ramsay-you-donkey-hells-kitchen.gif"

    embed = discord.Embed()
    embed.set_image(url=gif_url)

    await interaction.response.send_message(content=f"{interaction.user.mention}", embed=embed)


@tree.command(name="seek", description="Seeks to a specific time in the current song.")
@app_commands.describe(timestamp="The time to seek to (e.g., 01:32 or 1:32:05).")
@log_command
async def seek(interaction: discord.Interaction, timestamp: str):
    if not music_bot.current_song:
        await interaction.response.send_message("Not playing any song.", ephemeral=True)
        return

    try:
        parts = list(map(int, timestamp.split(':')))
        if len(parts) > 3:
            raise ValueError("Invalid timestamp format.")
        
        seconds = 0
        for i, part in enumerate(reversed(parts)):
            seconds += part * (60**i)

        if seconds > music_bot.current_song.duration:
            await interaction.response.send_message("Cannot seek beyond the song's duration.", ephemeral=True)
            return

        await music_bot.seek(seconds)
        await interaction.response.send_message(f"Seeked to {timestamp}.")

    except ValueError:
        await interaction.response.send_message("Invalid timestamp format. Please use HH:MM:SS, MM:SS, or SS.", ephemeral=True)


@tree.command(name="shuffle", description="Shuffles the current song queue.")
@log_command
async def shuffle(interaction: discord.Interaction):
    if not music_bot.queue:
        await interaction.response.send_message("The queue is empty, nothing to shuffle.", ephemeral=True)
        return
    
    random.shuffle(music_bot.queue)
    await interaction.response.send_message("The queue has been shuffled!")


@tree.command(name="jump", description="Jumps to a specific song in the queue.")
@app_commands.describe(position="The position of the song to jump to in the queue.")
@log_command
async def jump(interaction: discord.Interaction, position: int):
    if music_bot.jump(position):
        await interaction.response.send_message(f"Jumped to position **{position}** in the queue.")
    else:
        await interaction.response.send_message(f"Invalid position. The queue currently has {len(music_bot.queue)} songs.", ephemeral=True)


@tree.command(name="remove", description="Removes a song from the queue.")
@app_commands.describe(position="The position of the song to remove from the queue.")
@log_command
async def remove(interaction: discord.Interaction, position: int):
    if not music_bot.queue or not (1 <= position <= len(music_bot.queue)):
        await interaction.response.send_message("Invalid position.", ephemeral=True)
        return
        
    removed_song = music_bot.queue.pop(position - 1)
    await interaction.response.send_message(f"Removed **{removed_song.title}** from the queue.")


@tree.command(name="skip", description="Skips the current song")
@log_command
async def skip(interaction: discord.Interaction):
    if music_bot.voice_client and music_bot.voice_client.is_playing():
        music_bot.voice_client.stop()
        await interaction.response.send_message('Skipped the current song.')
    else:
        await interaction.response.send_message('Not playing any song.', ephemeral=True)


@tree.command(name="stop", description="Stops the music and clears the queue")
@log_command
async def stop(interaction: discord.Interaction):
    music_bot.queue = []
    if music_bot.voice_client:
        music_bot.voice_client.stop()
    music_bot.current_song = None
    await interaction.response.send_message("Stopped the music and cleared the queue.")


class QueuePaginator(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, queue, current_song, music_bot):
        super().__init__(timeout=120)
        self.interaction = interaction
        self.queue = queue
        self.current_song = current_song
        self.music_bot = music_bot
        self.current_page = 0
        self.songs_per_page = 10
        self.total_pages = -(-len(self.queue) // self.songs_per_page) if self.queue else 1

        # Disable buttons based on initial state
        self.previous_page.disabled = self.current_page == 0
        self.next_page.disabled = self.total_pages <= 1
        self.shuffle_queue.disabled = not self.queue
        self.skip_song.disabled = not self.current_song
        self.stop_playback.disabled = not self.current_song

    async def on_timeout(self):
        # Remove buttons on timeout
        message = await self.interaction.original_response()
        await message.edit(view=None)

    async def create_embed_for_page(self):
        embed = discord.Embed(title="Song Queue", color=discord.Color.blurple())

        if self.current_song:
            embed.add_field(name="Now Playing", value=f"**{self.current_song.title}** ({self.current_song.duration_fmt})", inline=False)
        
        if not self.queue:
            embed.description = "The queue is empty."
        else:
            start_index = self.current_page * self.songs_per_page
            end_index = start_index + self.songs_per_page
            queue_slice = self.queue[start_index:end_index]

            queue_text = ""
            for i, song in enumerate(queue_slice, start=start_index + 1):
                queue_text += f"`{i}.` {song.title}\n"
            
            if queue_text:
                embed.add_field(name="Up Next", value=queue_text, inline=False)
        
        if self.total_pages > 1:
            embed.set_footer(text=f"Page {self.current_page + 1}/{self.total_pages}")
            
        return embed

    async def update_view(self, interaction: discord.Interaction):
        # Defer the interaction response to prevent timeouts
        await interaction.response.defer()

        # Update button states
        self.previous_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page >= self.total_pages - 1
        self.shuffle_queue.disabled = not self.queue

        embed = await self.create_embed_for_page()
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="<<", style=discord.ButtonStyle.primary, row=0)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        await self.update_view(interaction)

    @discord.ui.button(label=">>", style=discord.ButtonStyle.primary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        await self.update_view(interaction)
        
    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.queue:
            random.shuffle(self.queue)
            # Defer and then update the view
            await self.update_view(interaction)
            await interaction.followup.send("Queue shuffled!", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to shuffle.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, row=1)
    async def skip_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.music_bot.voice_client and self.music_bot.voice_client.is_playing():
            self.music_bot.voice_client.stop()
            await interaction.response.send_message("Song skipped.", ephemeral=True)
            # Stop the view since the queue state is now different
            self.stop()
        else:
            await interaction.response.send_message("Not playing any song.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, row=1)
    async def stop_playback(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.music_bot.queue.clear()
        if self.music_bot.voice_client:
            self.music_bot.voice_client.stop()
        self.music_bot.current_song = None
        await interaction.response.send_message("Playback stopped and queue cleared.", ephemeral=True)
        self.stop()


@tree.command(name="queue", description="Shows the current song queue with interactive pages.")
@log_command
async def queue(interaction: discord.Interaction):
    if not music_bot.current_song and not music_bot.queue:
        await interaction.response.send_message('The queue is empty.', ephemeral=True)
        return

    view = QueuePaginator(interaction, music_bot.queue, music_bot.current_song, music_bot)
    embed = await view.create_embed_for_page()

    await interaction.response.send_message(embed=embed, view=view)


@tree.command(name="clear", description="Clears the queue")
@log_command
async def clear(interaction: discord.Interaction):
    music_bot.queue = []
    await interaction.response.send_message('Queue cleared.')


@tree.command(name="volume", description="Changes the player's volume")
@app_commands.describe(volume='The volume to set (0-100)')
@log_command
async def volume(interaction: discord.Interaction, volume: int):
    if music_bot.voice_client and music_bot.voice_client.source:
        music_bot.voice_client.source.volume = volume / 100
        await interaction.response.send_message(f"Changed volume to {volume}%")
    elif not music_bot.voice_client:
        await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)
    else:
        await interaction.response.send_message("Not currently playing anything.", ephemeral=True)


@tree.command(name="leave", description="Stops and disconnects the bot from voice")
@log_command
async def leave(interaction: discord.Interaction):
    await interaction.response.send_message("Leaving the voice channel...")
    music_bot.queue = []
    music_bot.current_song = None
    if music_bot.voice_client:
        # Disconnect in the background to avoid blocking the response
        asyncio.create_task(music_bot.voice_client.disconnect())
        music_bot.voice_client = None


@client.event
async def on_ready():
    if os.getenv('CLEAR_GLOBALS') == 'true':
        logging.info("--- COMMAND CLEANUP MODE ---")
        logging.info("Clearing all global commands. This may take a minute...")
        tree.clear_commands(guild=None)
        await tree.sync()
        logging.info("Global commands have been cleared.")
        logging.info("Please remove 'CLEAR_GLOBALS=true' from your .env file and restart the bot normally.")
        await client.close()
        return

    guild_id = os.getenv("GUILD_ID")
    if guild_id:
        guild = discord.Object(id=int(guild_id))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        logging.info(f'Synced commands to guild {guild_id}.')
    else:
        await tree.sync()
        logging.info('Synced commands globally.')
    logging.info(f'Logged in as {client.user} (ID: {client.user.id})')
    logging.info('------')

client.run(os.getenv("DISCORD_TOKEN"))
