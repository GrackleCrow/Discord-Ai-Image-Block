import io

from PIL import Image, PngImagePlugin


def png(text: dict | None = None, size=(16, 16)) -> bytes:
    info = PngImagePlugin.PngInfo()
    for k, v in (text or {}).items():
        info.add_text(k, v)
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def jpeg(software=None, description=None, user_comment=None, xmp=None) -> bytes:
    im = Image.new("RGB", (32, 32), (10, 20, 30))
    exif = im.getexif()
    if software:
        exif[0x0131] = software
    if description:
        exif[0x010E] = description
    if user_comment:
        exif.get_ifd(0x8769)[0x9286] = b"UNICODE\x00" + user_comment.encode("utf-16-be")
    buf = io.BytesIO()
    kw = {"exif": exif}
    if xmp:
        kw["xmp"] = xmp
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def encode(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buf, fmt)
    return buf.getvalue()
