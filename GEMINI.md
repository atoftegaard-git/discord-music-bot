# GEMINI Project Context: Discord Music Bot

This file provides a comprehensive overview of the Discord Music Bot project, its architecture, and development conventions to be used as instructional context for future interactions.

## Project Overview

This project is a containerized Discord music bot built with Python. It allows users to play music from various sources within a Discord voice channel, using a modern slash command interface. The bot is designed with security and efficiency in mind, running as a non-root user within a multi-stage Docker build.

**Core Features:**
*   Plays audio from YouTube, SoundCloud, and Spotify URLs.
*   Searches for songs on Spotify, YouTube, and SoundCloud.
*   Handles music queueing, song skipping, and volume controls.
*   Repeat functionality for both individual songs and the entire queue.
*   Exclusive use of slash commands for all interactions.
*   Custom logging for all command usage, successes, and errors.
*   A special `/spil` command that responds with an embedded GIF.

**Available Commands:**
*   `/play`
*   `/youtube`
*   `/soundcloud`
*   `/spotify`
*   `/stop`
*   `/skip`
*   `/continue`
*   `/seek`
*   `/queue`
*   `/shuffle`
*   `/jump`
*   `/remove`
*   `/clear`
*   `/repeat`
*   `/persist_queue`
*   `/volume`
*   `/leave`
*   `/spil`

**Key Technologies:**
*   **Language:** Python 3.10
*   **Discord API:** `discord.py`
*   **Audio Extraction:** `yt-dlp`
*   **Spotify Integration:** `spotipy`
*   **Voice:** `PyNaCl`
*   **Containerization:** Docker & Docker Compose
*   **System Dependencies:** `ffmpeg` (for audio processing)

## Building and Running

The application is designed to be run as a Docker container using Docker Compose, which handles building the image and running the container.

**Prerequisites:**
*   Docker must be installed and running.
*   Docker Compose must be installed.

**Configuration:**
The project uses a `.env` file to manage secret credentials and configuration. Ensure this file exists in the root of the project with the following variables:

```bash
# Your Discord bot's token from the Discord Developer Portal
DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN

# Your Spotify API credentials from the Spotify Developer Dashboard
SPOTIPY_CLIENT_ID=YOUR_SPOTIFY_CLIENT_ID
SPOTIPY_CLIENT_SECRET=YOUR_SPOTIFY_CLIENT_SECRET

# (Optional) Your Discord Server ID for instant command updates during development
GUILD_ID=YOUR_DISCORD_GUILD_ID

# (Optional) Proxy for yt-dlp and ffmpeg (e.g., http://user:pass@host:port)
YTDLP_PROXY=YOUR_PROXY_URL
```

**Running the Bot:**
To build and run the bot in detached mode, execute the following command in your terminal:

```bash
docker-compose up --build -d
```

To view the bot's logs:
```bash
docker-compose logs -f
```

## Development Conventions

*   **Containerization:** The `Dockerfile` uses a multi-stage build to create a slim, efficient final image. It runs the application as a non-root user (`appuser`) for enhanced security.
*   **Command Syncing:** For development, setting the `GUILD_ID` in the `.env` file will sync commands almost instantly to that specific server. If `GUILD_ID` is not set, commands are synced globally, which can take up to an hour to update.
*   **State Management:** A central `MusicBot` class instance is used to manage the bot's state, including the song queue, current song, repeat mode, and voice client.
*   **Logging:** A custom `@log_command` decorator provides uniform console logging for all slash commands. The log level for noisy libraries like `discord.player` has been adjusted to reduce log spam.
*   **Dependencies:** Python dependencies are managed in `requirements.txt`. System-level dependencies like `ffmpeg` are defined and installed in the `Dockerfile`.
*   **Configuration:** All secrets and environment-specific settings are loaded from a `.env` file using the `python-dotenv` library.
