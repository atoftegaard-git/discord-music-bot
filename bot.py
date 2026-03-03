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
import json


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
    'cookiefile': '/data/cookies.txt',
    'force_ipv4': True,
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    },
    'ignoreconfig': True,
    'no_cachedir': True
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2',
    'options': '-vn -fflags nobuffer -hide_banner -loglevel error'
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
            options['before_options'] = f"-analyzeduration 0 -probesize 32K -ss {seek}"

        return self.__class__(discord.FFmpegPCMAudio(self.data['url'], **options), data=self.data)

    @classmethod
    async def from_search(cls, query, *, loop=None, stream=False, platform: SearchPlatform, timeout: float = 10.0):
        loop = loop or asyncio.get_event_loop()

        if platform == SearchPlatform.YOUTUBE:
            logging.info(f"Searching on YouTube for: '{query}'")
            return await cls.from_url(f"ytsearch:{query}", loop=loop, stream=stream, timeout=timeout)

        if platform == SearchPlatform.SOUNDCLOUD:
            logging.info(f"Searching on SoundCloud for: '{query}'")
            return await cls.from_url(f"scsearch:{query}", loop=loop, stream=stream, timeout=timeout)

        # This should not be reached if called from _play_logic
        raise NotImplementedError("AUTO platform search is handled in _play_logic")


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


QUEUE_FORMAT_VERSION = 1


class MusicBot:
    def __init__(self, bot):
        self.bot = bot
        self.queue = []
        self.current_song = None
        self.voice_client = None
        self.text_channel = None
        self.repeat_mode = RepeatMode.NONE

        self.loader_semaphore = asyncio.Semaphore(10)

        self.data_dir = "/data"
        os.makedirs(self.data_dir, exist_ok=True)

        self.settings_file = os.path.join(self.data_dir, "settings.json")
        self.queue_file = os.path.join(self.data_dir, "queue.json")

        settings = self._load_settings()
        self.persist_queue = settings.get('persist_queue', False)

    async def handle_disconnect(self):
        """Cleans up resources when the bot disconnects from voice."""
        logging.info("Handling disconnect.")
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

        self.voice_client = None
        self.current_song = None
        # Don't clear queue, so users can use /continue if they rejoin
        self._save_queue()
        logging.info("Voice state cleaned up after disconnect.")

    def _load_settings(self):
        if not os.path.exists(self.settings_file):
            return {}
        try:
            with open(self.settings_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logging.error(f"Failed to load settings: {e}")
            return {}

    def _save_settings(self):
        try:
            with open(self.settings_file, 'w') as f:
                json.dump({
                    'persist_queue': self.persist_queue
                }, f)
        except OSError as e:
            logging.error(f"Failed to save settings: {e}")

    def _save_queue(self):
        if not self.persist_queue:
            if os.path.exists(self.queue_file):
                try:
                    os.remove(self.queue_file)
                except OSError as e:
                    logging.error(f"Error removing queue file: {e}")
            return

        urls_to_save = []
        if self.current_song:
            url = self.current_song.data.get('webpage_url', self.current_song.data.get('url'))
            if url:
                urls_to_save.append(url)

        urls_to_save.extend([
            song.data.get('webpage_url', song.data.get('url'))
            for song in self.queue
            if song.data.get('webpage_url', song.data.get('url'))
        ])

        try:
            queue_data = {
                "version": QUEUE_FORMAT_VERSION,
                "urls": urls_to_save
            }
            with open(self.queue_file, 'w') as f:
                json.dump(queue_data, f)
            logging.info(f"Saved {len(urls_to_save)} songs to {self.queue_file}")
        except Exception as e:
            logging.error(f"Failed to save queue: {e}")

    async def _load_url_with_semaphore(self, url: str):
        async with self.loader_semaphore:
            return await YTDLSource.from_url(url, loop=self.bot.loop, stream=True, timeout=20.0)

    async def _concurrent_load_urls(self, urls: list) -> tuple[list, int]:
        """Takes a list of URLs and loads them concurrently."""
        tasks = [self._load_url_with_semaphore(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        loaded_songs = []
        failed_count = 0
        for result in results:
            if isinstance(result, list) and result:
                loaded_songs.extend(result)
            else:
                failed_count += 1
                if isinstance(result, Exception):
                    # Log the specific exception, but don't dump the whole stack trace to avoid spam
                    logging.warning(f"Failed to load a song during concurrent load: {result}")

        return loaded_songs, failed_count

    async def _load_queue_on_startup(self):
        # Only load from file if the in-memory queue is currently empty
        if self.queue or self.current_song:
            return

        if not self.persist_queue or not os.path.exists(self.queue_file):
            return

        try:
            with open(self.queue_file, 'r') as f:
                queue_data = json.load(f)

            urls = []
            if isinstance(queue_data, dict) and 'version' in queue_data:
                if queue_data.get('version') == QUEUE_FORMAT_VERSION:
                    urls = queue_data.get('urls', [])
                else:
                    logging.warning(f"Queue file version mismatch! Expected {QUEUE_FORMAT_VERSION}, found {queue_data.get('version')}. Discarding queue.")
            elif isinstance(queue_data, list): # Handle old format
                logging.info("Old queue file format detected. Loading as is.")
                urls = queue_data

        except Exception as e:
            logging.error(f"Failed to load queue from file: {e}")
            return

        if not urls:
            return

        logging.info(f"Loading {len(urls)} songs from persisted queue on startup...")
        
        restored_songs, failed_count = await self._concurrent_load_urls(urls)
        
        self.queue.extend(restored_songs)
        
        logging.info(f"Restored {len(restored_songs)} songs to the queue.")
        if failed_count > 0:
            logging.warning(f"{failed_count} songs from the persisted queue failed to load.")

    async def _process_urls_bg(self, urls):
        logging.info(f"Starting background restore of {len(urls)} songs.")
        
        restored_songs, failed_count = await self._concurrent_load_urls(urls)

        self.queue.extend(restored_songs)
        self._save_queue()
        
        logging.info(f"Finished background restore of {len(restored_songs)} songs.")
        if self.text_channel:
            final_message = f"✅ Finished restoring {len(restored_songs)} songs to the queue."
            if failed_count > 0:
                final_message += f" ({failed_count} songs failed to load)."
            await self.text_channel.send(final_message)

    def start_background_load(self):
        """
        Synchronously reads the queue file and starts a background task to process it.
        This avoids a race condition where the queue file could be overwritten before being read.
        """
        if not self.persist_queue or not os.path.exists(self.queue_file):
            return

        try:
            with open(self.queue_file, 'r') as f:
                urls = json.load(f)
        except Exception:
            urls = []

        if urls:
            self.bot.loop.create_task(self._process_urls_bg(urls))

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
                # The connection is closed, let the disconnect handler clean up
                return

        if self.repeat_mode == RepeatMode.SONG and self.current_song:
            # Need a valid voice client to play
            if not self.voice_client:
                logging.warning("play_next called with no voice client, likely after a disconnect. Aborting.")
                return
            to_play = self.current_song.clone()
            self.voice_client.play(to_play, after=self.play_next)
            return

        if self.repeat_mode == RepeatMode.QUEUE and self.current_song:
            self.queue.append(self.current_song)

        if not self.queue:
            self.current_song = None
            asyncio.run_coroutine_threadsafe(self.text_channel.send('Queue finished.'), self.bot.loop)
            if self.repeat_mode != RepeatMode.QUEUE and self.voice_client:
                logging.info("Queue is empty and repeat is off, disconnecting.")
                # Disconnect, the on_voice_state_update event will handle cleanup
                asyncio.run_coroutine_threadsafe(self.voice_client.disconnect(), self.bot.loop)
            return

        self.current_song = self.queue.pop(0)
        to_play = self.current_song.clone()

        # Need a valid voice client to play
        if not self.voice_client or not self.voice_client.is_connected():
            logging.warning("play_next called with no voice client or a disconnected one, likely after a disconnect. Aborting play.")
            # Put the song back at the front of the queue
            self.queue.insert(0, self.current_song)
            self.current_song = None
            return

        self.voice_client.play(to_play, after=self.play_next)
        self._save_queue()
        logging.info(f"Starting playback of '{self.current_song.title}' from {self.current_song.platform}.")
        asyncio.run_coroutine_threadsafe(self.text_channel.send(f"Now playing: **{self.current_song.title}** ({self.current_song.duration_fmt})"), self.bot.loop)

    async def seek(self, position: int):
        if not self.voice_client or not self.voice_client.is_playing() or not self.current_song:
            return

        # Store the current volume from the existing source
        current_volume = self.voice_client.source.volume if self.voice_client.source else 0.5

        # Create a new audio source starting at the given position
        new_source = self.current_song.clone(seek=position)

        # Set the volume on the new source before replacing
        new_source.volume = current_volume

        # Replace the currently playing source with the new one
        self.voice_client.source = new_source

    def jump(self, position: int) -> bool:
        if not self.queue or not (1 <= position <= len(self.queue)):
            return False

        # The queue is 0-indexed, user position is 1-indexed
        target_index = position - 1

        # The song that was playing is now gone. The queue becomes
        # the song at the target position and everything after it.
        self.queue = self.queue[target_index:]
        self._save_queue()

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


async def _search_with_preference(query: str) -> list | None:
    """Performs a search on YouTube with retries, then falls back to SoundCloud."""
    
    # 1. Try YouTube with retries
    players = None
    retries = 3
    retry_delay = 2 # in seconds

    logging.info(f"Searching YouTube for: '{query}'")
    for attempt in range(retries):
        players = await YTDLSource.from_search(query, loop=client.loop, stream=True, platform=SearchPlatform.YOUTUBE)
        if players:
            logging.info(f"Found on YouTube after {attempt + 1} attempt(s).")
            return players # Found it, so we're done.
        
        logging.warning(f"YouTube search attempt {attempt + 1} of {retries} failed. Retrying in {retry_delay}s...")
        await asyncio.sleep(retry_delay)

    # 2. Fallback to SoundCloud if YouTube fails after all retries
    logging.info(f"Not found on YouTube after {retries} retries, falling back to SoundCloud for: '{query}'")
    players = await YTDLSource.from_search(query, loop=client.loop, stream=True, platform=SearchPlatform.SOUNDCLOUD)
    
    return players


async def _play_logic(interaction: discord.Interaction, query: str, platform: SearchPlatform = None):
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
                logging.info(f"Spotify track URL detected. Searching for '{query}' with preference.")
                # Unset platform to allow preference search
                platform = None

            elif "playlist" in query:
                try:
                    await interaction.followup.send("Fetching playlist from Spotify...")
                    results = spotify.playlist_tracks(query)
                except spotipy.SpotifyException as e:
                    if e.http_status == 404:
                        logging.warning(f"Could not find Spotify playlist (404): {query}")
                        await interaction.edit_original_response(content="Could not find that playlist. It might be private, a personal mix, or deleted. I can only access public playlists.")
                    else:
                        logging.error(f"An error occurred with Spotify API: {e}", exc_info=True)
                        await interaction.edit_original_response(content="An error occurred while fetching the Spotify playlist.")
                    return

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
                            # Use the new helper to respect preference
                            task = _search_with_preference(search_query)
                            search_tasks.append(task)

                    # If the queue is empty, find and play the first song immediately
                    if music_bot.voice_client and not music_bot.voice_client.is_playing() and not music_bot.voice_client.is_paused():
                        if search_tasks:
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
                    music_bot._save_queue()

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
    players = None
    # If a specific platform is provided (from /youtube, /soundcloud, etc.), use it
    if platform:
        players = await YTDLSource.from_search(query, loop=client.loop, stream=True, platform=platform)
    # If it's a URL, use from_url
    elif re.match(r'https?://', query):
        # If the user provides a youtube link with a playlist, only play the video
        if 'youtube.com/watch' in query and '&list=' in query:
            query = query.split('&list=')[0]
        players = await YTDLSource.from_url(query, loop=client.loop, stream=True)
    # Otherwise (it's a /play search), perform search with preference
    else:
        players = await _search_with_preference(query)

    if not players:
        await interaction.edit_original_response(content='Could not find any songs to play.')
        return

    # --- Logic to handle queueing and starting playback ---

    # Check if we should start a background load.
    # This is true if the bot isn't playing and the in-memory queue is empty.
    should_bg_load = (
        music_bot.voice_client and
        not music_bot.voice_client.is_playing() and
        not music_bot.voice_client.is_paused() and
        not music_bot.queue and not music_bot.current_song and
        music_bot.persist_queue and
        os.path.exists(music_bot.queue_file)
    )

    if should_bg_load:
        # This reads the file and starts the background processing task
        await interaction.followup.send("⏳ Loading persistent queue in the background...", ephemeral=True)
        music_bot.start_background_load()

    # Add the new song(s) to the in-memory queue and save the new state.
    music_bot.queue.extend(players)
    music_bot._save_queue()

    # Announce what was added, then start playback if not already active
    if music_bot.voice_client and not music_bot.voice_client.is_playing() and not music_bot.voice_client.is_paused():
        # Not playing, so start the queue.
        # play_next() will send the "Now playing" message.
        if len(players) > 1:
            await interaction.followup.send(content=f'Added {len(players)} songs to the queue. Starting playback...')
        else:
            await interaction.followup.send(content=f'Added to queue: **{players[0].title}**. Starting playback...')
        music_bot.play_next()
    else:
        # Already playing, just confirm the addition
        if len(players) > 1:
            await interaction.followup.send(content=f'Added {len(players)} songs to the queue.')
        else:
            await interaction.followup.send(content=f'Added to queue: **{players[0].title}**')


@tree.command(name="play", description="Plays a song from a URL or search query")
@app_commands.describe(
    query="The song URL or search query."
)
@log_command
async def play(interaction: discord.Interaction, query: str):
    await _play_logic(interaction, query)


@tree.command(name="youtube", description="Searches and plays a song from YouTube.")
@app_commands.describe(query="The search query.")
@log_command
async def youtube(interaction: discord.Interaction, query: str):
    await _play_logic(interaction, query, SearchPlatform.YOUTUBE)


@tree.command(name="soundcloud", description="Searches and plays a song from SoundCloud.")
@app_commands.describe(query="The search query.")
@log_command
async def soundcloud(interaction: discord.Interaction, query: str):
    await _play_logic(interaction, query, SearchPlatform.SOUNDCLOUD)


@tree.command(name="spotify", description="Searches Spotify for a track and plays it from YouTube.")
@app_commands.describe(query="The search query.")
@log_command
async def spotify_command(interaction: discord.Interaction, query: str):
    if not spotify:
        await interaction.response.send_message("Spotify support is not configured.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        result = spotify.search(q=query, type='track', limit=1)
        if result and result['tracks']['items']:
            track = result['tracks']['items'][0]
            artist = track['artists'][0]['name']
            title = track['name']
            spotify_youtube_query = f"{artist} - {title}"
            logging.info(f"Found on Spotify: '{spotify_youtube_query}'. Passing to _play_logic for YouTube search.")
            await _play_logic(interaction, spotify_youtube_query, SearchPlatform.YOUTUBE)
        else:
            await interaction.edit_original_response(content=f"Could not find any results for '{query}' on Spotify.")
    except Exception as e:
        logging.error(f"Spotify search for '{query}' failed: {e}", exc_info=True)
        await interaction.edit_original_response(content="An error occurred while searching Spotify.")


@tree.command(name="repeat", description="Sets the repeat mode.")
@app_commands.describe(mode="Choose repeat mode")
@log_command
async def repeat(interaction: discord.Interaction, mode: RepeatMode):
    music_bot.repeat_mode = mode
    await interaction.response.send_message(f"Repeat mode set to {mode.value}.")


@tree.command(name="persist_queue", description="Toggles queue persistence across bot restarts.")
@app_commands.describe(enabled="Set to True to enable, False to disable.")
@log_command
async def persist_queue(interaction: discord.Interaction, enabled: bool):
    music_bot.persist_queue = enabled
    music_bot._save_settings()
    music_bot._save_queue() # Immediately save or clear the queue file

    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"Queue persistence has been {status}.")





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
    music_bot._save_queue()
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
    music_bot._save_queue()
    await interaction.response.send_message(f"Removed **{removed_song.title}** from the queue.")


@tree.command(name="skip", description="Skips the current song")
@log_command
async def skip(interaction: discord.Interaction):
    if music_bot.voice_client and music_bot.voice_client.is_playing():
        music_bot.voice_client.stop()
        await interaction.response.send_message('Skipped the current song.')
    else:
        await interaction.response.send_message('Not playing any song.', ephemeral=True)


@tree.command(name="stop", description="Stops the music and clears the in-memory queue")
@log_command
async def stop(interaction: discord.Interaction):
    music_bot.queue = []
    if music_bot.voice_client:
        music_bot.voice_client.stop()
    music_bot.current_song = None
    await interaction.response.send_message("Stopped the music and cleared the in-memory queue.")


@tree.command(name="continue", description="Starts playing from the queue.")
@log_command
async def continue_command(interaction: discord.Interaction):
    if not music_bot.queue and not music_bot.current_song:
        await interaction.response.send_message("The queue is empty. Nothing to continue.", ephemeral=True)
        return

    if music_bot.voice_client and music_bot.voice_client.is_playing() and not music_bot.voice_client.is_paused():
        await interaction.response.send_message("Already playing music.", ephemeral=True)
        return

    if not await music_bot.ensure_voice_channel(interaction):
        return

    await interaction.response.defer() # Defer the response as play_next might take a moment or send followups

    if not music_bot.current_song and music_bot.queue:
        # If nothing is playing but there's a queue, start playing the first song
        music_bot.play_next()
        await interaction.followup.send("Continuing playback from queue.")
    elif music_bot.current_song and music_bot.voice_client and music_bot.voice_client.is_paused():
        # If there's a current song and it's paused, just resume
        music_bot.voice_client.resume()
        await interaction.followup.send("Resuming playback.")
    elif music_bot.current_song and music_bot.voice_client and not music_bot.voice_client.is_playing():
        # Edge case: current_song is set but not playing (e.g., bot just started)
        # This can happen if bot restarts and current_song is restored but not actively playing.
        # Ensure voice_client is connected first.
        music_bot.play_next()
        await interaction.followup.send("Starting playback from current song.")
    else:
        # Should ideally not be reached if previous checks are correct
        await interaction.followup.send("Could not determine how to continue playback. The queue might be empty or playback is already active.", ephemeral=True)

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
            self.music_bot._save_queue()
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
        await interaction.response.send_message("Playback stopped and in-memory queue cleared.", ephemeral=True)
        self.stop()


@tree.command(name="queue", description="Shows the current song queue with interactive pages.")
@log_command
async def queue(interaction: discord.Interaction):
    await interaction.response.defer()

    if not music_bot.current_song and not music_bot.queue:
        await interaction.followup.send('The queue is empty.', ephemeral=True)
        return

    view = QueuePaginator(interaction, music_bot.queue, music_bot.current_song, music_bot)
    embed = await view.create_embed_for_page()

    await interaction.followup.send(embed=embed, view=view)


@tree.command(name="clear", description="Clears the queue")
@log_command
async def clear(interaction: discord.Interaction):
    music_bot.queue = []
    music_bot._save_queue()
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
    
    if music_bot.voice_client:
        # Stop playback first to prevent the 'after' callback from firing on disconnect
        if music_bot.voice_client.is_playing():
            music_bot.voice_client.stop()
        
        # Now clear the state
        music_bot.queue = []
        music_bot.current_song = None
        
        # And finally, disconnect
        await music_bot.voice_client.disconnect()
    else:
        # If not in a voice channel, just make sure the queue is clear
        music_bot.queue = []
        music_bot.current_song = None


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

    await music_bot._load_queue_on_startup()
    logging.info(f'Logged in as {client.user} (ID: {client.user.id})')
    logging.info('------')

client.run(os.getenv("DISCORD_TOKEN"))
