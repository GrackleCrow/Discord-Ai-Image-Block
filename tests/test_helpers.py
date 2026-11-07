import asyncio
import types

import pytest


def embed(kind, url="https://media.discordapp.net/external/x.png"):
    media = types.SimpleNamespace(proxy_url=url, url="https://i.example.com/x.png")
    return types.SimpleNamespace(type=kind, image=media, thumbnail=media, url="https://example.com")


def test_only_direct_image_links_are_checked(bot):
    assert len(bot.link_images([embed("image")])) == 1
    for kind in ("article", "rich", "link", "video", "gifv"):
        assert bot.link_images([embed(kind)]) == [], kind


@pytest.mark.parametrize("url", [
    "http://192.168.0.1/admin.png",
    "https://evil.example.com/x.png",
    "http://media.discordapp.net/x.png",       # not https
    "https://discordapp.net.evil.com/x.png",
])
def test_link_downloads_only_through_discord(bot, url):
    with pytest.raises(ValueError):
        asyncio.run(bot.LinkImage(url, url).read())


def test_priority_lock_serves_posts_before_checks(bot):
    order = []

    async def job(lock, tag, prio, hold=0.05):
        await lock.acquire(prio)
        try:
            order.append(tag)
            await asyncio.sleep(hold)
        finally:
            lock.release()

    async def main():
        lock = bot.PriorityLock()
        first = asyncio.create_task(job(lock, "post-1", bot.PRIORITY_POST))
        await asyncio.sleep(0.01)
        waiting = [asyncio.create_task(job(lock, "check-A", bot.PRIORITY_CHECK)),
                   asyncio.create_task(job(lock, "check-B", bot.PRIORITY_CHECK))]
        await asyncio.sleep(0.01)
        late = asyncio.create_task(job(lock, "post-2", bot.PRIORITY_POST))
        cancelled = asyncio.create_task(job(lock, "post-3", bot.PRIORITY_POST))
        await asyncio.sleep(0.01)
        cancelled.cancel()
        await asyncio.gather(first, late, *waiting, cancelled, return_exceptions=True)

    asyncio.run(main())
    assert order == ["post-1", "post-2", "check-A", "check-B"]


def test_removal_notice_names_the_source(bot):
    msg = types.SimpleNamespace(author=types.SimpleNamespace(mention="@member"))
    assert "Google Gemini" in bot.removal_notice(msg, ("gemini", "", "Google LLC"))
    assert "Midjourney" in bot.removal_notice(msg, ("generator", "", "Midjourney"))
    assert bot.removal_notice(msg, ("chatgpt", "", "gpt-image")).startswith("@member")


def test_webhook_username_rules(bot):
    assert "discord" not in bot.webhook_username("discordfan").lower()
    assert bot.webhook_username("") == "Unknown member"
    assert len(bot.webhook_username("x" * 200)) == 80


def test_split_text_respects_discord_limit(bot):
    chunks = bot.split_text("line\n" * 1000)
    assert all(len(c) <= 2000 for c in chunks) and len(chunks) > 1
