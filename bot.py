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
    'source_address': '0.0.0.0'  # bind to ipv4 since ipv6 addresses cause issues sometimes
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
    async def from_search(cls, query, *, loop=None, stream=False, platform: SearchPlatform = SearchPlatform.AUTO):
        loop = loop or asyncio.get_event_loop()

        if platform == SearchPlatform.YOUTUBE:
            logging.info(f"Searching on YouTube for: '{query}'")
            return await cls.from_url(f"ytsearch:{query}", loop=loop, stream=stream)
        
        if platform == SearchPlatform.SOUNDCLOUD:
            logging.info(f"Searching on SoundCloud for: '{query}'")
            return await cls.from_url(f"scsearch:{query}", loop=loop, stream=stream)

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
                    return await cls.from_url(f"ytsearch:{search_query}", loop=loop, stream=stream)
            except Exception as e:
                logging.error(f"Spotify search failed: {e}")

        # 2. YouTube
        logging.info(f"Searching on YouTube for: '{query}'")
        results = await cls.from_url(f"ytsearch:{query}", loop=loop, stream=stream)
        if results:
            return results

        # 3. SoundCloud
        logging.info(f"Searching on SoundCloud for: '{query}'")
        return await cls.from_url(f"scsearch:{query}", loop=loop, stream=stream)


    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        try:
            data = await asyncio.wait_for(loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream)), timeout=10.0)
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
                # Try to play the next song
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
        if not self.current_song:
            return

        # Stop the current player
        self.voice_client.stop()

        # Create a new player with the seek option
        new_player = self.current_song.clone(seek=position)
        
        # Play the new player
        self.voice_client.play(new_player, after=self.play_next)


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
                results = spotify.playlist_tracks(query)
                tracks = results['items']
                
                # Handle paginated results
                while results['next']:
                    results = spotify.next(results)
                    tracks.extend(results['items'])

                if not tracks:
                    await interaction.followup.send("Could not find any tracks in the playlist.")
                    return

                await interaction.followup.send(f"Adding {len(tracks)} songs from the playlist to the queue...")

                for item in tracks:
                    track = item['track']
                    if track:
                        artist = track['artists'][0]['name']
                        title = track['name']
                        search_query = f"{artist} - {title}"
                        
                        # We are searching for each song individually, so we can't use the normal play flow
                        players = await YTDLSource.from_search(search_query, loop=client.loop, stream=True)
                        if players:
                            music_bot.queue.extend(players)
                            if not music_bot.voice_client.is_playing() and not music_bot.voice_client.is_paused():
                                music_bot.play_next()
                
                await interaction.channel.send(f"Finished adding playlist to the queue.")
                return

        except Exception as e:
            logging.error(f"Failed to process Spotify URL: {e}", exc_info=True)
            await interaction.followup.send("Failed to process Spotify URL.")
            return

    # If the user provides a youtube link with a playlist, only play the video
    if 'youtube.com/watch' in query and '&list=' in query:
        query = query.split('&list=')[0]

    if re.match(r'https?://', query):
         players = await YTDLSource.from_url(query, loop=client.loop, stream=True)
    else:
        players = await YTDLSource.from_search(query, loop=client.loop, stream=True, platform=platform)


    if not players:
        await interaction.followup.send('Could not find any songs to play.')
        return

    if music_bot.voice_client.is_playing() or music_bot.voice_client.is_paused() or music_bot.queue:
        music_bot.queue.extend(players)
        if len(players) > 1:
            await interaction.followup.send(f'Added {len(players)} songs to the queue.')
        else:
            await interaction.followup.send(f'Added to queue: **{players[0].title}**')
    else:
        music_bot.current_song = players.pop(0)
        music_bot.queue.extend(players)
        music_bot.voice_client.play(music_bot.current_song.clone(), after=music_bot.play_next)
        await interaction.followup.send(f"Now playing: **{music_bot.current_song.title}** ({music_bot.current_song.duration_fmt})")


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


@tree.command(name="queue", description="Shows the current song queue")
@log_command
async def queue(interaction: discord.Interaction):
    if music_bot.current_song or music_bot.queue:
        queue_list = ""
        if music_bot.current_song:
            queue_list += f"**Now Playing:** {music_bot.current_song.title}\n\n"
        if music_bot.queue:
            queue_list += "**Up Next:**\n"
            for i, song in enumerate(music_bot.queue):
                queue_list += f"{i+1}. {song.title}\n"
        await interaction.response.send_message(queue_list)
    else:
        await interaction.response.send_message('The queue is empty.', ephemeral=True)


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
