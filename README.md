# Telegram Business Assistant Bot

A personal Telegram Business bot that replies on your behalf using your writing style via Claude Code CLI (subscription token, not API key). Built with aiogram 3.x, designed for Railway deployment with Docker.

## Features

- **Telegram Business Integration**: Connects via Business "Chatbots" feature (business_connection + business_message updates)
- **Personalized Replies**: Uses your `profile.md` to match your writing style, tone, and knowledge
- **Claude Code CLI**: Uses `claude -p` with subscription token (`claude setup-token`), no Anthropic API key needed
- **Preview Mode** (default): Logs draft replies to `previews.jsonl` for review before sending
- **Auto-Send Mode**: Optionally sends replies automatically after connection re-verification
- **Owner Takeover**: Silences bot for 15 minutes (configurable) after you send a message
- **Rate Limiting**: Respects Claude limits with cooldown and reset-time awareness
- **Multi-language**: Detects Uzbek (Latin/Cyrillic), Russian, English; replies in same language
- **No Chat Allowlist**: Telegram Business settings control which chats the bot sees
- **Secure**: No hardcoded tokens, private file permissions (0600/0700), token redaction in logs

## Quick Start (Local Development)

### Prerequisites

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- Telegram Bot Token (from @BotFather)
- Your Telegram user ID (numeric)

### 1. Clone and Setup

```bash
git clone <your-private-repo>
cd telegram-business-assistant

# Install Python dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 2. Configure Environment

```bash
# Copy example and set permissions (Linux/macOS)
cp .env.example .env
chmod 600 .env

# Windows PowerShell
Copy-Item .env.example .env
# Then open .env in your editor and fill in values
```

**Required variables in `.env`:**
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `OWNER_ID` — your numeric Telegram user ID
- `CLAUDE_CODE_OAUTH_TOKEN` — from `claude setup-token` (see below)

### 3. Get Claude Subscription Token

```bash
# Run once to authenticate Claude Code CLI
claude setup-token

# This opens browser, authorizes, and prints a token like:
# sk-ant-oat-xxxxxxxxxxxxxxxxxxxxxxxx
# Copy this token into .env as CLAUDE_CODE_OAUTH_TOKEN
```

### 4. Customize Your Profile

Edit `profile.md` with your personal information, writing style, work details, interests, and sample messages. The bot uses this to sound like you.

### 5. Run Locally

```bash
# Preview mode (default) — logs drafts to previews.jsonl
python bot.py

# Check Claude CLI integration
python bot.py --check-claude

# Test prompt generation in three languages
python bot.py --try
```

## Configuration

All settings via environment variables (`.env` locally, Railway variables in production):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `OWNER_ID` | Yes | — | Your numeric Telegram user ID |
| `CLAUDE_CODE_OAUTH_TOKEN` | Yes | — | Subscription token from `claude setup-token` |
| `AUTO_SEND` | No | `false` | `true` = auto-send replies, `false` = preview only |
| `DATA_DIR` | No | `./data` | Directory for history, state, previews |
| `PROFILE_PATH` | No | `./profile.md` | Path to your profile markdown |
| `OWNER_TAKEOVER_MINUTES` | No | `15` | Minutes bot stays silent after you send a message |
| `LIMIT_COOLDOWN_MINUTES` | No | `30` | Cooldown when Claude limit hit without reset time |
| `CLAUDE_MODEL` | No | `claude-3-5-sonnet-20241022` | Model to use |
| `CLAUDE_TIMEOUT_SECONDS` | No | `60` | Timeout for Claude CLI calls |

**Priority**: Environment variables > `.env` file > defaults.

## Profile.md Guide

The `profile.md` is the bot's "brain" — it learns your style from here. Required sections:

```markdown
# Personal Info
Your name, location, timezone, languages you speak.

# Work
What you do, current projects, typical schedule.

# Daily Routine
When you're usually available, sleep schedule, habits.

# Interests & Hobbies
Topics you enjoy discussing.

# Writing Style
Tone, formality level, emoji usage, typical phrases, sentence length.

# Sample Messages
5-10 real messages you've sent (anonymized) showing your style.

# FAQ
Common questions people ask you and your standard answers.

# Important People
Names/roles of people who message you often (partner, boss, friends).

# Forbidden Topics
Things you never discuss or opinions you never share.
```

**Tips:**
- Be specific — "I use lowercase, short sentences, 😊 emoji" beats "casual style"
- Include 5+ real message examples
- Update periodically as your style evolves

## Modes

### Preview Mode (Default, `AUTO_SEND=false`)

- Incoming business message → Claude generates reply
- Reply logged to `data/previews.jsonl` with connection, chat, language
- **No message sent to Telegram**
- You review previews manually, then copy/paste or use `/resume` to enable auto-send temporarily
- Safe for testing and daily use

### Auto-Send Mode (`AUTO_SEND=true`)

- Incoming business message → re-verify connection (`get_business_connection`) → send reply
- Checks `can_reply` and `is_enabled` before each send
- Owner notified of rate limits/errors via DM (rate-limited to 1 per 10 min)
- Long messages auto-split at 4000 characters
- Aborts if you send a message during generation (takeover)

## Owner Commands (DM to Bot)

| Command | Description |
|---------|-------------|
| `/start` | Shows mode, connection status, pause state, and full instructions |
| `/pause` | Pauses bot (persists across restarts) |
| `/resume` | Resumes bot, clears rate limit |
| `/status` | Shows paused, limit_until, active connections |

**Strangers** (non-owner) who DM the bot get a brief one-time explanation that this is a personal assistant and only the owner can control it. Their messages are ignored.

## Telegram Business Setup

1. **Create bot** via @BotFather → get token
2. **Enable Business** in Telegram Settings → Business → Chatbots
3. **Add your bot** by username → select which chats it can access
4. **No code allowlist needed** — Telegram Business settings control access

The bot handles:
- `business_connection` — connect/disable events
- `business_message` — incoming messages in connected chats
- Ignores: your own messages, bot messages, service messages, duplicates

## Architecture

```
bot.py                 # Entry point, argparse, modes
assistant/
├── config.py          # Settings, validation, dual source (env file + env vars)
├── language.py        # Uzbek (Lat/Cyr), Russian, English detection
├── prompts.py         # System prompt (profile + rules), user prompt (history + lang)
├── store.py           # Atomic JSON (0600), History, SeenMessages, RuntimeState, PreviewStore
├── claude_cli.py      # Subprocess wrapper: generate, inspect, parse, classify, redact
├── service.py         # Assistant class: 11-step pipeline, factories
└── handlers.py        # aiogram router: business updates, owner commands
tests/
├── conftest.py        # Harness: Clock, FakeGenerator, RecordingBot, helpers
├── test_business_updates.py
├── test_claude_cli.py
└── test_config_and_language.py
```

### Key Design Decisions

- **No Anthropic SDK** — uses `claude -p` subprocess with strict isolation (`--no-network`, `--no-auto-updates`, `--dangerously-skip-permissions`)
- **Subscription token** via `CLAUDE_CODE_OAUTH_TOKEN` env var (not API key)
- **Atomic file writes** with private permissions for all data files
- **Per-connection, per-chat history** isolation
- **Deterministic tests** with fake clock, fake generator, recording bot

## Testing

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_business_updates.py -v
pytest tests/test_claude_cli.py -v
pytest tests/test_config_and_language.py -v

# With coverage
pytest --cov=assistant
```

### Test Infrastructure

- `Clock` — controllable time for testing timeouts, takeovers, limit resets
- `FakeGenerator` — programmable responses/exceptions for Claude
- `RecordingBot` — records API calls instead of network requests
- `Harness` — integrates all fakes, helpers: `connect()`, `business_message()`, `direct_message()`, `restart()`

## Deployment (Railway)

### 1. Push to Private GitHub Repo

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### 2. Create Railway Project

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Select your private repo
3. Railway detects `Dockerfile` automatically

### 3. Set Environment Variables in Railway

Go to Variables tab and add:

```
TELEGRAM_BOT_TOKEN=your_token
OWNER_ID=your_id
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-xxx
AUTO_SEND=true
DATA_DIR=/app/data
PROFILE_PATH=/app/profile.md
```

**Never commit real tokens.** Railway variables are encrypted.

### 4. Deploy

Railway builds from `Dockerfile`:
- Python 3.12 slim
- Installs Claude Code CLI via official installer
- Caches pip dependencies
- Runs as non-root user
- Healthcheck via `claude --version`

`railway.json` configures restart policy (max 5 retries on failure).

### 5. Verify

Check Railway logs for:
```
INFO: Bot started in AUTO-SEND mode
INFO: Business connection established: <conn_id>
```

Test by messaging your business-connected chats.

## Development Workflow

```bash
# 1. Make changes
# 2. Test locally
python bot.py --check-claude
python bot.py --try
pytest

# 3. Commit and push
git add .
git commit -m "feat: description"
git push

# 4. Railway auto-deploys on push to main
```

## Security Checklist

- [ ] `.env` never committed (in `.gitignore`)
- [ ] Real tokens only in Railway variables / local `.env` (chmod 600)
- [ ] No `cat .env`, `grep .env`, `Read .env` in code or logs
- [ ] Token redaction in all logs (`sk-ant-oat-***`)
- [ ] Private file permissions (0600 files, 0700 dirs)
- [ ] Subprocess isolation flags enabled
- [ ] No shell=True, no user input in subprocess args
- [ ] Owner-only commands verified by numeric ID

## Troubleshooting

### `python bot.py --check-claude` fails

- Run `claude setup-token` again
- Verify `CLAUDE_CODE_OAUTH_TOKEN` in `.env` matches
- Check `claude --version` works in terminal

### Bot doesn't reply

- Check `business_connection` established in logs
- Verify chat is selected in Telegram Business → Chatbots settings
- Check `AUTO_SEND` mode matches expectation
- Verify not paused (`/status`)

### Rate limited

- Wait for `resets_at` time or cooldown period
- Use `/resume` to clear limit immediately (owner only)
- Check `LIMIT_COOLDOWN_MINUTES` setting

### "Connection not found" errors

- Reconnect bot in Telegram Business settings
- Check bot has `can_reply` permission

## License

MIT — use freely for personal projects.