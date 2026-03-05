# Discord Music Bot

A containerized Discord music bot built with Python, `discord.py`, and `yt-dlp`. It uses a modern slash command interface and is designed for easy setup and use with Docker.

## Features

-   Plays audio from YouTube, SoundCloud, and Spotify URLs.
-   Searches for songs on Spotify, YouTube, and SoundCloud.
-   Handles music queueing, song skipping, and volume controls.
-   Repeat functionality for both individual songs and the entire queue (`/repeat`).
-   Exclusive use of slash commands for all interactions.
-   Custom logging for all command usage, successes, and errors.
-   A special `/spil` command that responds with an embedded GIF.

## Prerequisites

-   Docker
-   Docker Compose

## Setup Instructions

### 1. Create a `.env` File

Create a file named `.env` in the root of the project directory. This file will store your secret credentials.

### 2. Configure Environment Variables

Populate the `.env` file with the following variables.

```env
# Get this from the Discord Developer Portal > Your Application > Bot
DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN

# Get these from the Spotify Developer Dashboard > Your Application
SPOTIPY_CLIENT_ID=YOUR_SPOTIFY_CLIENT_ID
SPOTIPY_CLIENT_SECRET=YOUR_SPOTIFY_CLIENT_SECRET

# (Optional) For instant command updates during development.
# Right-click your Discord Server icon > "Copy ID" (Developer Mode must be enabled in Discord settings)
GUILD_ID=YOUR_DISCORD_GUILD_ID

# (Optional) Proxy for yt-dlp and ffmpeg (e.g., http://user:pass@host:port)
YTDLP_PROXY=YOUR_PROXY_URL
```

### 3. Build and Run the Bot

With Docker running, execute the following command in your terminal:

```bash
docker-compose up --build -d
```

This command will:
-   Build the Docker image using the multi-stage `Dockerfile`.
-   Start the container in detached mode (`-d`).
-   Load the environment variables from your `.env` file.

## Available Commands

### Playback Controls
-   `/play <query>`: Plays a song from a URL or search query (YouTube, SoundCloud, Spotify).
-   `/youtube <query>`: Searches and plays a song specifically from YouTube.
-   `/soundcloud <query>`: Searches and plays a song specifically from SoundCloud.
-   `/spotify <query>`: Searches Spotify for a track and plays the best match from YouTube.
-   `/stop`: Stops the music and clears the queue.
-   `/skip`: Skips to the next song in the queue.
-   `/continue`: Resumes playback if paused, or starts the queue if stopped.
-   `/seek <timestamp>`: Seeks to a specific time in the current song (e.g., `1:23`).

### Queue Management
-   `/queue`: Shows the current song queue with interactive pages.
-   `/shuffle`: Shuffles the songs currently in the queue.
-   `/jump <position>`: Jumps to a specific song number in the queue.
-   `/remove <position>`: Removes a song from the queue by its number.
-   `/clear`: Clears all songs from the queue.
-   `/repeat <mode>`: Sets the repeat mode (`none`, `song`, `queue`).
-   `/persist_queue <enabled>`: Toggles whether the queue is saved when the bot restarts.

### Other Commands
-   `/volume <0-100>`: Changes the player's volume.
-   `/leave`: Disconnects the bot from the voice channel.
-   `/spil`: Sends a special GIF.

## Viewing Logs

To view the bot's logs in real-time, use the following command:

```bash
docker-compose logs -f
```
