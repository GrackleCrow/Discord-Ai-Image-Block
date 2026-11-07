import pytest

from helpers import encode, jpeg, png


def kind(bot, data, mime):
    res = bot.local_scan(data, mime, guild_id=1)
    return res[1][0] if res and res[1] else None


def test_plain_images_pass(bot):
    assert kind(bot, png(), "image/png") is None
    assert kind(bot, jpeg(software="Pixel 8 HDR+"), "image/jpeg") is None
    assert kind(bot, jpeg(software="Adobe Photoshop 26.0"), "image/jpeg") is None


@pytest.mark.parametrize("text,expected", [
    ({"parameters": "a cat\nSteps: 20, Sampler: Euler a, CFG scale: 7"}, "stable_diffusion"),  # Automatic1111
    ({"prompt": '{"3": {"class_type": "KSampler"}}'}, "stable_diffusion"),                    # ComfyUI
    ({"Software": "NovelAI"}, "generator"),
    ({"XML:com.adobe.xmp": "<x>http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia</x>"}, "iptc"),
])
def test_png_generators_detected(bot, text, expected):
    assert kind(bot, png(text), "image/png") == expected


def test_jpeg_generators_detected(bot):
    assert kind(bot, jpeg(software="Midjourney 6.1"), "image/jpeg") == "generator"
    assert kind(bot, jpeg(software="Adobe Firefly"), "image/jpeg") == "generator"
    assert kind(bot, jpeg(user_comment="cat Steps: 30, Sampler: DPM++ 2M, CFG scale: 5"), "image/jpeg") == "stable_diffusion"
    assert kind(bot, jpeg(xmp=b"<x>compositeWithTrainedAlgorithmicMedia</x>"), "image/jpeg") == "iptc"


@pytest.mark.parametrize("description", [
    "Imagen de la playa",            # Spanish for "image"
    "imagens do Rio",
    "Fireflies at dusk",
    "my midjourney-inspired painting",
    "10 Steps: an easy guide",
])
def test_ordinary_captions_not_flagged(bot, description):
    assert kind(bot, jpeg(description=description), "image/jpeg") is None


def test_generator_names_need_whole_words(bot):
    assert kind(bot, jpeg(software="NotMidjourneyX"), "image/jpeg") is None


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"),
                                      ("GIF", "image/gif"), ("AVIF", "image/avif")])
def test_sniff_mime(bot, fmt, mime):
    assert bot.sniff_mime(encode(fmt)) == mime


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"])
def test_malformed_files_never_crash(bot, mime):
    good = {"image/png": png({"parameters": "Steps: 20, Sampler: E"})}.get(mime) or encode(mime.split("/")[1].upper())
    for cut in range(0, len(good), max(1, len(good) // 25)):
        bot.local_scan(good[:cut], mime, guild_id=1)
    bot.local_scan(b"", mime, guild_id=1)
    bot.local_scan(b"\x00" * 5000, mime, guild_id=1)
