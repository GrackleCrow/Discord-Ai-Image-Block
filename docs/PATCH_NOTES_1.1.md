# Discord AI Image Block 1.1 — Patch Notes & Upgrade Guide

**From:** 1.0 (initial release, 2026-11-03)
**To:** 1.1 (2026-11-06)

1.0 removed images carrying Gemini or ChatGPT AI credentials. 1.1 catches far more AI images in far more places, tells people *why* their image was removed, tracks offenders, logs everything, lets mods fully undo mistakes, and gives everyone a private way to check an image before posting.

---

## At a glance

| Area | 1.0 | 1.1 |
|---|---|---|
| What it catches | Gemini and ChatGPT credentials | Plus Firefly, Microsoft, Stability, Stable Diffusion tools, NovelAI, Midjourney/other tool metadata, AI tags, and an optional classifier |
| Where it looks | Uploads (PNG, JPEG, WebP, GIF) up to 50 MB | Plus forwarded messages, direct image links, AVIF and HEIC, up to 500 MB |
| Feedback on posts | A notice when deleted | ✅ ⚠️ 🤖 ❔ reactions, plus a notice that says what caught the image |
| Records and logs | Optional log channel | Numbered records, a removal log file, and a log channel set from Discord |
| Mistakes | Gone for good | Saved for 30 days; restored as the original member with one click |
| Commands | None | `/ai-help`, `/ai-check`, `/ai-offenders`, `/ai-history`, `/ai-restore`, `/ai-log-channel`, `/ai-reset` |
| Speed | Decoded every PNG's pixels | Reads metadata only; under 1 ms per image |

---

## New features

### Detection
- **AI credentials from any company.** The local check now catches signed AI credentials from Adobe Firefly, Microsoft, Stability and others, not just Google and OpenAI.
- **Generator metadata check (free, offline).** Catches:
  - Stable Diffusion tools: Automatic1111, Forge, SD.Next, ComfyUI, InvokeAI, Fooocus
  - NovelAI
  - IPTC "AI-generated" tags, including *AI-edited* images such as Photoshop generative fill
  - Files whose **Software** field names an AI tool: Midjourney, Leonardo, Ideogram, DALL·E, Adobe Firefly, and others. Names only count there and only as whole words, so a caption like "imagen de la playa" or "fireflies at dusk" isn't flagged.
- **Sightengine AI-image classifier (optional).** A machine-learning guess for images with no watermark or metadata, such as screenshots, Midjourney, Flux and Grok. By default it only flags images ⚠️ for mods to review (`CLASSIFIER_ACTION=review`); set it to `delete` to remove them automatically.
- **Set check order.** Free local checks → Sightengine → ChatGPT check (last). The first check that fails stops the scan, so later checks and API calls never run on that image.
- **One image at a time.** Each image in a post is downloaded and checked before the next; the first failed image removes the post.

### Where it looks
- **Forwarded messages.** Images inside a message forwarded with Discord's Forward button are checked like any other post.
- **Direct image links** (e.g. `https://i.imgur.com/abc.png`), including previews Discord adds a moment after the message is sent. **News articles, social posts and other page previews are skipped**, since the picture belongs to the site, not the poster. Links are downloaded only through Discord's own media proxy, never from the linked site, so nobody can make the bot connect to something on your home network. Tenor/Giphy reaction GIFs are skipped by default.
- **AVIF and HEIC images.** Previously skipped without a reaction; now checked. They're converted to PNG just for the online checks.
- **Larger files.** Images up to **500 MB** are checked (Discord's Nitro upload limit), up from 50 MB. Images over 45 MB are shrunk to a high-quality JPEG just for the online checks; the local checks always see the original file.

### Moderation
- **Reactions on every checked post:**
  - ✅ passed every check
  - ⚠️ the classifier thinks it might be AI (sent to mods for review)
  - 🤖 would have been deleted (test mode only)
  - ❔ a check couldn't finish (API down or rate-limited)
- **Removal notices say what caught the image.** For example: "…it was made with **Google Gemini**", "…it has **Stable Diffusion** generation settings saved in it (ComfyUI)", "…its metadata says it was made with **Midjourney**." Every message can be reworded in `.env`.

### Logging
- **Removal log file.** Every removal, ⚠️ review, test-mode catch and restore is written as one line to `data/removals.log` on the Zima box, for example:
  ```
  2026-11-06 04:42:53 | REMOVED #12 | server=My Server (123…) | channel=#general (456…) | user=someuser (789…) | file=art.png | reason=Stable Diffusion generation parameters
  2026-11-06 05:10:02 | RESTORED (false positive) #12 | server=My Server (123…) | channel=#general (456…) | user=someuser (789…) | by=modname (321…)
  ```
  It rotates at 5 MB and keeps 5 old files. Change where it's saved with `REMOVAL_LOG_FILE`.
- **Mod log channel set from Discord.** `/ai-log-channel #channel` picks the channel where removals (with Restore buttons), ⚠️ reviews and restores are posted, per server. Run it with no channel to see the current setting and the log file's location.
- Every catch gets a detection number (`#12`) shown in the log channel, the log file, and `/ai-history`.

### Restoring removed posts
- Before deleting, the bot saves the whole post for 30 days: text, every attachment (including forwarded ones), the author's name and avatar, the original time, and what it was replying to.
- Each removal in the log channel gets two buttons, **Restore: false positive** and **Restore: relevant**:
  - **False positive** means it isn't AI. The bot reposts it and remembers that exact file, so it's never removed again.
  - **Relevant** means it's AI but allowed this time. The bot reposts it, but the same file posted again is removed as normal.
- **Full restore:** the post comes back **as the original member**, with their name, avatar, text and files, through a webhook (Discord marks it "APP"). A small line underneath shows who restored it, the reason, and the original time. If it was a reply, a link to the message it replied to appears above it.
- Either way, the restore no longer counts against the poster.
- **What can't come back:** other people's reactions and replies, and any file bigger than the server's upload limit (listed under the post instead). Discord doesn't let bots recreate those.

### Commands

| Command | Who | What it does |
|---|---|---|
| `/ai-help` | Everyone | Lists the commands you can use (mods and admins see theirs too) |
| `/ai-check image:` | Everyone | Privately checks an image before posting. Nothing is deleted, saved, or sent to mods. 30-second cooldown per person; always waits behind real posts |
| `/ai-offenders [days]` | Mods | Who has posted AI images, and how many |
| `/ai-history @member` | Mods | A member's detections, numbers, and restore reasons |
| `/ai-restore <number> <reason>` | Mods | Fully restore a removed post (false positive or relevant) |
| `/ai-log-channel [#channel]` | Admins | Choose where removals are posted for mods |
| `/ai-reset [@member] [test_only]` | Admins | Clear records for one member or the whole server |

All replies are private to the person who ran the command.

### Speed (tuned for the i7-4790)
- Local checks read only file metadata and never decode pixels: large images went from ~22–98 ms to under 1 ms.
- **Real posts go first.** `/ai-check` requests wait behind real posts for the online checks, so members testing images never slow down moderation.
- Removed posts are saved from the copy already downloaded for scanning, not downloaded twice.
- Faster networking and JSON libraries, uvloop, reused API connections, and a faster database mode.
- The log shows how long each post took, e.g. `(142 ms)`.

---

## Fixes
- **AI credentials marked "invalid" were ignored.** The checking library rejected some real Gemini/ChatGPT credentials, so the local check skipped them. They're now caught.
- PNG text stored after the image data, compressed PNG text, and WebP XMP metadata are now read.
- Ordinary words in a photo's description no longer trigger false "AI generator" matches.

---

## Behavior changes to know about
- **More images will be removed.** The new checks catch tools 1.0 ignored. That includes **AI-edited real photos** (e.g. Photoshop generative fill), which carry an "AI-generated" tag.
- **Direct image links and forwards are checked now.** A post with an AI image link, or a forwarded AI image, is removed like an upload. Links to articles and social posts are never checked.
- **Every checked image gets a reaction.** ✅ on normal images is on by default. Turn it off with `REACT_CLEAN=false`.
- **Restored posts appear as the original member.** They use a webhook, so the bot needs two new permissions: **Attach Files** and **Manage Webhooks**.
- **New `data/` folder** next to `.env` holds the records database, saved posts, and `removals.log`.

---

## Upgrading from 1.0

About 5 minutes. Your `.env` (token and settings) is kept.

### 1. Give the bot two new permissions (in Discord)
**Server Settings → Roles → *your bot's role* →** turn on:
- **Attach Files**, to repost restored images
- **Manage Webhooks**, to restore posts as the original member

You don't need to re-invite the bot.

### 2. Back up the 1.0 install
Copy the new files to your server, then log in to it. The paths below are the ZimaOS layout; use your own install folder on other systems.

```bash
scp -r ./discord-ai-image-block USER@YOUR-SERVER:/tmp/
ssh USER@YOUR-SERVER
sudo cp -r /DATA/AppData/discord-ai-image-block /DATA/AppData/discord-ai-image-block-1.0-backup
```

### 3. Install the new files
This copies everything **except** your `.env`:

```bash
cd /tmp/discord-ai-image-block
sudo cp bot.py requirements.txt docker-compose.yml Dockerfile .env.example README.md CHANGELOG.md /DATA/AppData/discord-ai-image-block/
cd /DATA/AppData/discord-ai-image-block
```

Three of these must be updated: `bot.py` (the bot), `requirements.txt` (new libraries, including HEIC support) and `docker-compose.yml` (adds the `data/` folder so records, saved posts and the log file survive restarts).

### 4. (Recommended) Turn on test mode for the first run
The new checks will catch things 1.0 didn't, so watch them first:

```bash
sudo nano .env
```

Set `DRY_RUN=true`, save (**Ctrl+O**, **Enter**) and exit (**Ctrl+X**).

### 5. (Optional) Add new settings to `.env`
All of these are optional and already default to the values shown. Only add the ones you want to change. `.env.example` has all of them with explanations, including a **Logging** section.

| Setting | Default | What it does |
|---|---|---|
| `REMOVAL_LOG_FILE` | `/app/data/removals.log` | Where the removal log is saved (blank = off). See *Choosing the log location* below |
| `LOG_CHANNEL_ID` | *(none)* | Default mod log channel. Easier: use `/ai-log-channel` in Discord |
| `SIGHTENGINE_API_USER` / `SIGHTENGINE_API_SECRET` | *(off)* | Turns on the AI-image classifier |
| `SIGHTENGINE_THRESHOLD` | `0.90` | How sure the classifier must be (0–1) |
| `CLASSIFIER_ACTION` | `review` | `review` = ⚠️ for mods, `delete` = remove |
| `METADATA_CHECK` | `true` | Stable Diffusion / generator metadata check |
| `REACT_CLEAN` | `true` | ✅ on images that pass |
| `SCAN_LINKS` | `true` | Check direct image links |
| `SCAN_GIF_LINKS` | `false` | Also check Tenor/Giphy reaction GIFs (uses API quota) |
| `MAX_FILE_MB` | `500` | Largest file to check |
| `QUARANTINE_DAYS` | `30` | How long removed posts can be restored |
| `CHECK_COOLDOWN_SECONDS` | `30` | How often one person can use `/ai-check` |
| `MSG_GEMINI`, `MSG_CHATGPT`, `MSG_SYNTHID`, `MSG_C2PA`, `MSG_STABLE_DIFFUSION`, `MSG_GENERATOR`, `MSG_IPTC`, `MSG_CLASSIFIER`, `MSG_FOOTER` | *(built in)* | Custom removal messages; see `.env.example` |

#### Choosing the log location
- **Default:** `/DATA/AppData/discord-ai-image-block/data/removals.log` on the Zima box. Nothing to do.
- **Somewhere else on the Zima box** (e.g. `/DATA/Logs/discord-ai-image-block`):
  1. In `docker-compose.yml`, uncomment the line `# - /DATA/Logs/discord-ai-image-block:/app/logs` and change the left side to the folder you want.
  2. In `.env`, set `REMOVAL_LOG_FILE=/app/logs/removals.log`.
- **Turn it off:** set `REMOVAL_LOG_FILE=` (blank).

To read it: `sudo tail -n 50 /DATA/AppData/discord-ai-image-block/data/removals.log`

### 6. Rebuild and start

```bash
sudo docker compose up -d --build
sudo docker logs -f discord-ai-image-block
```

The rebuild takes a couple of minutes because it installs new libraries. Look for:

```
Discord AI Image Block v1.1 | Logged in as YourBot#0000 | C2PA: on | metadata: on | OpenAI SynthID: on | classifier: off (review) | dry run: True
```

Press **Ctrl+C** to stop watching; the bot keeps running.

### 7. Set the log channel
In your server, run **`/ai-log-channel #your-mod-channel`**. (If you already had `LOG_CHANNEL_ID` in `.env`, it keeps working; this just lets you change it from Discord.)

### 8. Test it
1. Type `/ai-help` in your server. You should see the command list with the mod and admin sections. If no commands show up, press **Ctrl+R** to refresh Discord. If they still don't appear, re-invite the bot from **OAuth2 → URL Generator** with both **bot** and **applications.commands** ticked; you don't need to remove it first.
2. Run `/ai-check` on an image you downloaded from ChatGPT (the original file, not a screenshot). With an OpenAI key set you should get **🤖 This would be removed** (ChatGPT).
3. Post a normal photo. It should get a ✅.
4. Post an AI image. It should get a 🤖, and the log shows `Would post: …` with the removal message.
5. Forward that AI image to another channel, and post a direct link to an AI image. Both should get a 🤖. A link to a news article should get nothing.
6. Check that the log file is filling in: `sudo tail /DATA/AppData/discord-ai-image-block/data/removals.log` should show `TEST (would remove)` lines.

### 9. Turn deletion on and test a restore

```bash
sudo nano .env        # set DRY_RUN=false
sudo docker compose up -d --force-recreate
```

Post an AI image again. It should be removed and appear in your log channel with two Restore buttons. Click **Restore: false positive**: the post should come back under your own name and avatar, with a small "Restored by…" line underneath.

Then run **`/ai-reset test_only:True`** to clear the test entries from the records.

### 10. Clean up (once you're happy)

```bash
sudo rm -rf /DATA/AppData/discord-ai-image-block-1.0-backup /tmp/discord-ai-image-block
```

---

## Rolling back to 1.0
If something goes wrong:

```bash
cd /DATA/AppData
sudo docker compose -f discord-ai-image-block/docker-compose.yml down
sudo mv discord-ai-image-block discord-ai-image-block-1.1
sudo cp -r discord-ai-image-block-1.0-backup discord-ai-image-block
cd discord-ai-image-block
sudo docker compose up -d --build
```

Records, saved posts and the log file from 1.1 stay in `discord-ai-image-block-1.1/data/` if you upgrade again later. You can also delete the "Discord AI Image Block Restore" / "AI Image Block Restore" webhooks the bot created under each channel's **Integrations → Webhooks**.
