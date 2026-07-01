<h1 align="center">
  🌐 VHA Translate Bot
</h1>

<p align="center">
  Lightweight Discord translation bot for the VHA Alliance.<br>
  Translates messages between German, French, Portuguese, and English using Google Gemini.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Discord-5865F2?style=flat&logo=discord&logoColor=white" alt="Discord" />
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/AI-Google%20Gemini-4285F4?style=flat&logo=google&logoColor=white" alt="Gemini" />
</p>

---

## 📋 Overview

A simple, single-file Discord bot that auto-translates messages in VHA Alliance channels. Lightweight alternative to the full VHA Assistant Bot — focused purely on translation.

---

## ⚙️ Tech Stack

| Component | Details |
|-----------|---------|
| **AI Model** | Google Gemini |
| **Database** | SQLite locally (default) or MongoDB automatically when `MONGODB_URI` is set (e.g. on Render) |
| **Discord Library** | discord.py |
| **Hosting** | Self-hosted on Linux (systemd) |

---

## 🌍 Translation

### Fixed languages (always active)
- 🇩🇪 Deutsch
- 🇫🇷 Français
- 🇬🇧 English
- 🇧🇷 Português

### Translation rules
- Always **du-form** — never "Sie" or "Vous"
- Game terms are **never** translated: R1–R5, coordinates, player names, @mentions
- Emojis stay unchanged

---

## 📁 File Structure

```
translator_bot.py       — Main bot logic (single file)
tsprachen.py            — Language settings management
traumsprachen.py        — Room-specific language settings
requirements.txt        — Python dependencies
processed_msgs.db       — SQLite database (message deduplication, auto-created)
```

---

## 🔑 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN_TRANSLATOR` | ✅ | Bot token from [Discord Developer Portal](https://discord.com/developers/applications) |
| `GEMINI_API_KEY_TRANSLATOR` | ✅ | API key from [Google AI Studio](https://aistudio.google.com/apikey) |
| `MONGODB_URI` | ✅ | Connection string from MongoDB Atlas (Connect → Drivers) |
| `MONGODB_DB_NAME` | ❌ | DB name inside the cluster (default: `vha_translate_bot`) |
| `PORT` | ❌ | Set automatically by Render; enables the Flask keepalive server |

---

## ☁️ Deploying on Render

The bot automatically picks its storage backend: if `MONGODB_URI` is set, it uses MongoDB (needed on Render, since the filesystem isn't persistent there). If it's **not** set, it falls back to local SQLite — exactly like on your own server. So on your own server, just don't set `MONGODB_URI` and nothing changes.

1. Push this repo to GitHub.
2. On Render: **New → Web Service** → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python translator_bot.py`
5. Add the environment variables above under **Environment** (never commit real secrets to the repo).
6. Render sets `PORT` automatically → the bot starts a small Flask keepalive endpoint at `/` so it can run as a Web Service.
7. Free tier only: the service sleeps after 15 min without HTTP traffic. Use a free pinger like [UptimeRobot](https://uptimerobot.com) or [cron-job.org](https://cron-job.org) to hit your Render URL every 5–10 minutes and keep it awake.
8. For guaranteed 24/7 uptime without pinging tricks, use a paid Render instance or a Background Worker instead.

**Important:** rotate your MongoDB password if it was ever shared in plaintext (chat, screenshot, etc.) — set the new one only as the `MONGODB_URI` environment variable on Render, never in code.

---

## 💬 Commands

| Command | Description | Permission |
|---------|-------------|------------|
| `!sprachen` | Toggle target languages | R4, R5, DEV |
| `!ping` | Bot status and latency | Everyone |

---

## 📦 Installation

```bash
pip install -r requirements.txt
```

### Requirements

```
discord.py
google-genai
python-dotenv
```

---

## 🚀 Setup & Start

```bash
# 1. Set environment variables
export DISCORD_TOKEN_TRANSLATOR=your_token_here
export GEMINI_API_KEY=your_key_here

# 2. Start the bot
python translator_bot.py
```

For production, run as systemd service for auto-restart.
