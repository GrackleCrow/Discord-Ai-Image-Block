# Discord AI Image Block — Patch Log

Each version lists what changed and, for manual installs, which files to copy when updating. Your `.env` and `data/` folder are always kept.

Standard update (from a git clone):

```bash
git pull
docker compose up -d --build
```

The running version is shown in the startup log line (`Discord AI Image Block v1.1.1 | Logged in as …`).

---

## 1.1.1 — 2026-11-07

**Changed**
- `/ai-offenders` shows a green **"✅ No AI image posters"** card when there's nobody to list ("Nobody has posted an AI image yet", or "…in the last 7 days" with a time range).
- A member with only ⚠️ reviews now shows "1 flagged for review" instead of "**0** AI images, 1 flagged for review".

**Fixed**
- Members whose only catches had been restored were still listed in `/ai-offenders` with "0 AI images", which also stopped the empty message from ever appearing. Restored posts no longer count at all.
- **Missing Manage Messages:** if the bot couldn't delete a post, it was still recorded as removed and counted against the member, and a mod "restore" would have posted a duplicate. Now nothing is recorded, the post gets a ⚠️, and the log channel says which channel is missing the permission.
- **Member deleted their own post first:** it was still recorded and counted against them, and could be "restored" by a mod. Now it's forgotten.
- **Same post scanned twice at once** (e.g. an attachment plus a link preview Discord adds a moment later): it was recorded as two removals. Now it's handled once.
- **Two mods restoring the same post at the same moment** reposted it twice. Now the second mod is told it's already being restored.

**Project**
- **Renamed from "SynthID Guard" to "Discord AI Image Block"** (repo, Docker container and log lines). The restore webhook is now called "AI Image Block Restore" (Discord doesn't allow "discord" in webhook names); webhooks from earlier versions are still reused, and Restore buttons on old log messages keep working.
  - **Existing installs:** the container name changed, so stop the old one **before** updating, or two copies of the bot will run at once: `docker compose down` in the old folder (or `docker rm -f synthid-guard`), then update and `docker compose up -d --build`. Your `.env` and `data/` carry over unchanged.
- Ready for GitHub: MIT license, `.gitignore` (keeps `.env` and `data/` out of the repo), `.dockerignore`, a test suite (`pytest`), and a GitHub Actions workflow that lints, tests and builds the Docker image.
- Upgrade notes moved to `docs/PATCH_NOTES_1.1.md`.

**Update from 1.1:** copy `bot.py`.

---

## 1.1 — 2026-11-06

A big update covering everything since the first build.

### Detection
- **Check order:** free local checks → Sightengine classifier → ChatGPT check (last). The first failure stops the scan; later checks never run.
- **One image at a time:** each image in a post is downloaded and checked before the next. The first failed image removes the post.
- **C2PA from any issuer:** AI credentials from Adobe Firefly, Microsoft, Stability and others are caught, not just Google and OpenAI.
- **Generator metadata check:** Stable Diffusion tools (Automatic1111, Forge, ComfyUI, InvokeAI, Fooocus), NovelAI, IPTC "AI-generated" tags, and a Software field naming an AI tool (Midjourney, Leonardo, Ideogram…). Tool names only count in the Software field and only as whole words, so ordinary captions like "imagen" (Spanish for "image") or "fireflies" aren't flagged.
- **Forwarded messages are checked.** Images inside a forwarded message were previously skipped.
- **Direct image links are checked** (e.g. `https://i.imgur.com/abc.png`), including previews Discord adds a moment after the message is sent. News articles, social posts and other page previews are skipped, since the picture belongs to the site, not the poster. Links are downloaded only through Discord's own media proxy, never from the linked site, so the bot can't be pointed at anything on your home network. Tenor/Giphy reaction GIFs are skipped unless `SCAN_GIF_LINKS=true`. Turn link checks off with `SCAN_LINKS=false`.
- **Larger files:** images up to 500 MB are checked (`MAX_FILE_MB`), up from 50 MB. Images over 45 MB are shrunk to a high-quality JPEG just for the online checks; the local checks always see the original.
- **AVIF and HEIC images are checked.** They used to be skipped silently. They're converted to PNG just for the online checks.
- **Sightengine AI-image classifier (optional):** `review` mode (⚠️ for mods) or `delete` mode. In review mode a ⚠️ image still goes on to the ChatGPT check, so a confirmed watermark gets deleted.
- **Fixed:** AI credentials the checking library calls "invalid" were ignored, letting some images through.

### Moderation
- **Reactions:** ✅ passed every check, ⚠️ classifier suspects AI, 🤖 would be deleted (`DRY_RUN`), ❔ a check couldn't finish. Turn off ✅ with `REACT_CLEAN=false`.
- **The removal notice names what caught the image:** Google Gemini, ChatGPT, SynthID, other AI credentials, Stable Diffusion settings, a named generator, an AI metadata tag, or the classifier. Customize any message with `MSG_GEMINI`, `MSG_CHATGPT`, `MSG_SYNTHID`, `MSG_C2PA`, `MSG_STABLE_DIFFUSION`, `MSG_GENERATOR`, `MSG_IPTC`, `MSG_CLASSIFIER`, `MSG_FOOTER`, using `{user}` and `{detail}`.
- In `DRY_RUN`, the message that *would* be posted is written to the log.

### Tracking and mod commands
- Every flagged image is recorded in `data/synthid.db`, with a detection number (`#12`) shown in the log channel.
- `/ai-offenders [days]` (Manage Messages): who has posted AI images and how many.
- `/ai-history @member` (Manage Messages): a member's total and recent detections, including restore reasons.
- `/ai-reset [@member] [test_only]` (Manage Server): clear records.

### Restore
- Before deleting, the bot saves the whole post (text, all attachments, the author's name and avatar, the original time, and what it was replying to) to `data/quarantine/` for `QUARANTINE_DAYS` (default 30), cleaned up daily.
- **Full restore:** restored posts reappear **as the original member**, with their name, avatar and original text unchanged, using a webhook (Discord marks it "APP"). A small line underneath shows who restored it, the reason, and the original time; if the post was a reply, a link to the message it replied to is shown above. Needs **Manage Webhooks**; without it, the bot reposts the post itself as before. Reactions and replies from others can't be brought back (Discord doesn't allow it), and files bigger than the server's upload limit are listed instead of reposted.
- Each removal in the log channel has two buttons, or mods can use `/ai-restore <number> <reason>`. The two reasons:
  - **False positive:** the image isn't AI. The bot reposts it and remembers that exact file, so it's never removed again.
  - **Relevant:** the image is AI but allowed here. The bot reposts it, but the same file posted again is still checked and removed as normal.
- Either way, the restored post stops counting against the user, and the repost says which reason the mod picked.
- Restore buttons on log messages from before 1.1 still work: they ask the mod for a reason first.
- Forwarded posts are restored with their images and text. Link posts are restored as their original text, so the link preview comes back; a "false positive" restore also remembers the linked image.

### Logging
- **Removal log file:** every removal, ⚠️ review, test-mode catch and restore is written as one line to `data/removals.log` (rotated at 5 MB, keeps 5 old files). Change the location with `REMOVAL_LOG_FILE`, or set it blank to turn it off.
- **`/ai-log-channel [#channel]`** (Manage Server): choose the mod log channel from Discord, per server. Run it with no channel to see the current setting, or with `turn_off:True` to stop posting. `LOG_CHANNEL_ID` in `.env` still works as the default.
- `.env.example` has a new Logging section explaining both.

### `/ai-check` (everyone)
- Privately test an image before posting: 🤖 would be removed (and why), ⚠️ unsure, ✅ no markers found, or ❔ couldn't finish.
- Nothing is deleted, saved, or counted, and mods are never contacted, even when the result is unsure.
- Per-user cooldown (`CHECK_COOLDOWN_SECONDS`, default 30) protects API quotas.
- **Lower priority:** real posts always go ahead of `/ai-check` requests in the line for the online checks, so members testing images never slow down moderation.

### Speed
- Local checks read only file metadata and never decode pixels: under 1 ms even for 12 MB images (previously 22–98 ms).
- The restore copy reuses image bytes already downloaded for scanning.
- The allowlist is kept in memory, and a file is only hashed if its size matches a remembered file.
- uvloop, `discord.py[speed]`, `aiohttp[speedups]`, keep-alive API connections, SQLite WAL mode.
- Logs show how long each post took to check.
- **Fixed:** PNG text stored after the image data, compressed PNG text, and WebP XMP are now read.
- **Fixed:** words in a photo's description could trigger a false "AI generator" match (for example "imagen", "fireflies" or "midjourney-inspired").

### Other
- `/ai-help` (everyone): lists the commands you can use. Mods also see the mod commands, and admins see `/ai-log-channel` and `/ai-reset`. The reply is private.
- A version number appears in the startup log.

**Update from 1.0:** copy `bot.py`, `requirements.txt` (adds HEIC support), **and** `docker-compose.yml` (adds the `data` folder for records, saved posts and the removal log). Give the bot **Attach Files** and **Manage Webhooks** (Server Settings → Roles → *your bot's role*) so it can fully restore posts. The new settings in `.env.example` are all optional.

---

## 1.0 — 2026-11-03 · Initial release

- Discord bot in Docker for ZimaOS.
- Deletes images carrying Gemini or ChatGPT AI credentials (local C2PA check).
- Optional OpenAI Content Provenance API check for the SynthID watermark in ChatGPT image pixels.
- `DRY_RUN` test mode, log channel, exempt roles, watched channels, and an auto-deleting notice to the poster.
