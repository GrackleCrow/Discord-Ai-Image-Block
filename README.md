# Discord AI Image Block

A self-hosted Discord bot that finds and removes AI-generated images, from Google Gemini and ChatGPT to Stable Diffusion, Midjourney and more. It tells the poster what caught their image, keeps a record for mods, and lets mods fully restore anything it got wrong.

Runs in Docker on any Linux box. Built and tested on ZimaOS.

> **Not affiliated with Google, OpenAI, or Discord.** SynthID is Google DeepMind's watermarking technology; this bot only reads markers that AI tools leave in images.

---

## Features

- **Layered detection:** free offline checks first, then optional online checks. The first check that fails stops the scan.
- **Explains every removal:** "…it was made with **Google Gemini**", "…it has **Stable Diffusion** settings saved in it (ComfyUI)". Messages are customizable.
- **Checks everywhere images show up:** uploads, forwarded messages, and direct image links, in PNG, JPEG, WebP, GIF, AVIF and HEIC, up to 500 MB.
- **Full restore:** removed posts are saved for 30 days, and mods restore them with one click. They come back under the member's own name and avatar.
- **Mod tools:** an offender leaderboard, per-member history, a log channel with Restore buttons, and a removal log file.
- **`/ai-check` for everyone:** members can privately test an image before posting it.
- **Fast:** offline checks read only metadata and never decode pixels, so even large images take under 1 ms.

## How it detects AI images

| Order | Check | Catches | Cost |
|---|---|---|---|
| 1 | **C2PA Content Credentials** (offline) | Signed AI credentials from Gemini, ChatGPT, Adobe Firefly, Microsoft, Stability and others | Free |
| 2 | **Generator metadata** (offline) | Stable Diffusion settings (Automatic1111, Forge, ComfyUI, InvokeAI, Fooocus), NovelAI, IPTC "AI-generated" tags, AI tools named in the Software field | Free |
| 3 | **Sightengine classifier** (optional) | Images that just *look* AI-made: screenshots, Midjourney, Flux, Grok… | [Sightengine](https://sightengine.com) account |
| 4 | **OpenAI Content Provenance API** (optional) | The SynthID watermark in the *pixels* of ChatGPT images, even after screenshots | OpenAI API key |

The classifier is a guess, so by default it only flags images ⚠️ for mods to review instead of deleting them.

### Limitations
Please read these before relying on the bot:
- **Screenshots and re-saves remove the metadata** that checks 1 and 2 rely on. After that, only checks 3 and 4 can still catch an image.
- **Gemini screenshots get through.** Google has no public tool for detecting its watermark in the pixels.
- **AI-edited real photos are removed too.** For example, Photoshop generative fill adds an "AI-generated" tag. Mods can restore these.
- **Not checked:** videos, stickers, emojis, links to articles or social posts, messages from bots and webhooks (including PluralKit/Tupperbox), and posts made while the bot is offline.
- No detector is perfect. Treat the bot as a first line of defense, not a final verdict.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/ai-help` | Everyone | Lists the commands you can use |
| `/ai-check image:` | Everyone | Privately checks an image. Nothing is deleted, saved, or sent to mods |
| `/ai-offenders [days]` | Mods (Manage Messages) | Who has posted AI images, and how many |
| `/ai-history @member` | Mods | A member's detections and their numbers |
| `/ai-restore <number> <reason>` | Mods | Restore a removed post (see below) |
| `/ai-log-channel [#channel]` | Admins (Manage Server) | Choose where removals are posted |
| `/ai-reset [@member] [test_only]` | Admins | Clear records |

All replies are only visible to the person who ran the command.

**Reactions on posts:** ✅ passed · ⚠️ might be AI (mods review) · ❔ a check couldn't finish · 🤖 would be removed (test mode only)

### Restoring posts
Each removal in the log channel has two buttons, or mods can use `/ai-restore`:

| Reason | Meaning | Next time |
|---|---|---|
| **False positive** | Not AI | That exact file is remembered and never removed again |
| **Relevant** | AI, but allowed here | The same file is still checked as normal |

The post comes back under the member's own name and avatar with its original text and files, plus a small "Restored by @mod" line. Discord doesn't allow restoring other people's reactions or replies.

---

## Setup

### 1. Create the Discord bot
1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and open **Bot**.
2. Click **Reset Token** and copy the token.
3. Turn on **Message Content Intent**.
4. Under **OAuth2 → URL Generator**, tick the scopes **bot** and **applications.commands**, then these permissions:
   - View Channels
   - Send Messages
   - Send Messages in Threads
   - Manage Messages
   - Read Message History
   - Add Reactions
   - Attach Files
   - Manage Webhooks
5. Open the generated URL to add the bot to your server.

> To keep the bot private, set **Installation → Install Link** to **None** before turning off **Public Bot**. Discord won't let a private bot keep its own install link.

### 2. Install

On a machine with Docker and Docker Compose:

```bash
git clone https://github.com/GrackleCrow/Discord-Ai-Image-Block.git
cd Discord-Ai-Image-Block
cp .env.example .env
nano .env                    # paste your DISCORD_TOKEN; set DRY_RUN=true for a first test
docker compose up -d --build
docker logs -f discord-ai-image-block
```

Look for `Discord AI Image Block v1.1.1 | Logged in as …`. The bot needs about 10 seconds to connect after starting; commands sent before then get "The application did not respond".

**ZimaOS:** clone into `/DATA/AppData/discord-ai-image-block` (run `sudo mkdir -p /DATA/AppData` first if needed) and use `sudo` for the commands.

### 3. First run
1. In your server, run `/ai-log-channel #your-mod-channel`.
2. Test with `DRY_RUN=true`: the bot reacts 🤖 instead of deleting, and logs the message it would have posted.
3. Once you're happy, set `DRY_RUN=false`, run `docker compose up -d --force-recreate`, then run `/ai-reset test_only:True` to clear the test entries.

---

## Configuration

Everything goes in `.env`; see [`.env.example`](.env.example) for every option with explanations. Only `DISCORD_TOKEN` is required.

| Setting | Default | What it does |
|---|---|---|
| `DISCORD_TOKEN` | — | Bot token (required) |
| `OPENAI_API_KEY` | *(off)* | Turns on the ChatGPT pixel-watermark check |
| `SIGHTENGINE_API_USER` / `SIGHTENGINE_API_SECRET` | *(off)* | Turns on the AI-image classifier |
| `SIGHTENGINE_THRESHOLD` | `0.90` | How sure the classifier must be (0–1) |
| `CLASSIFIER_ACTION` | `review` | `review` = ⚠️ for mods, `delete` = remove |
| `DRY_RUN` | `false` | Test mode: react 🤖 instead of deleting |
| `LOG_CHANNEL_ID` | *(none)* | Default mod log channel (`/ai-log-channel` overrides it per server) |
| `REMOVAL_LOG_FILE` | `/app/data/removals.log` | Removal log file; blank turns it off |
| `QUARANTINE_DAYS` | `30` | How long removed posts can be restored |
| `MAX_FILE_MB` | `500` | Largest file to check |
| `SCAN_LINKS` / `SCAN_GIF_LINKS` | `true` / `false` | Check direct image links / Tenor and Giphy GIFs |
| `METADATA_CHECK` | `true` | Generator metadata check |
| `REACT_CLEAN` | `true` | React ✅ on images that pass |
| `NOTIFY_USER` | `true` | Post a short removal notice to the member |
| `EXEMPT_ROLE_IDS` | *(none)* | Comma-separated role IDs that are never checked |
| `WATCH_CHANNEL_IDS` | *(all)* | Only check these channels |
| `CHECK_COOLDOWN_SECONDS` | `30` | `/ai-check` cooldown per member |
| `MSG_GEMINI`, `MSG_CHATGPT`, … | *(built in)* | Custom removal messages, with `{user}` and `{detail}` |

### Data and privacy
Everything is stored locally in `data/` next to `.env`:
- `synthid.db`: detection records and settings
- `quarantine/`: copies of removed posts (text, attachments, the author's name and avatar link), deleted after `QUARANTINE_DAYS`
- `removals.log`: one line per removal, review and restore

Images that reach the optional online checks are sent to OpenAI and/or Sightengine. Nothing else leaves your server. Let your members know the bot is in use and what it stores.

---

## Updating

```bash
git pull
docker compose up -d --build
```

Your `.env` and `data/` folder are kept. See [CHANGELOG.md](CHANGELOG.md) for what changed, and [docs/PATCH_NOTES_1.1.md](docs/PATCH_NOTES_1.1.md) for upgrading from 1.0.

## Development

```bash
pip install -r requirements-dev.txt
pyflakes bot.py
pytest
```

## License

[MIT](LICENSE)
