"""Recognize GIFs from transient message metadata without fetching media."""

import re
from urllib.parse import unquote, urlsplit

from lets_play.new_rules.contracts import AttachmentSnapshot

_GIF_PAGE_PATHS = {
    "giphy.com": re.compile(r"/(?:gifs|embed)/[^/]+/?\Z"),
    "klipy.com": re.compile(r"/gifs/[^/]+/?\Z"),
    "tenor.com": re.compile(r"/view/[^/]+/?\Z"),
}
_GIPHY_MEDIA_HOST = re.compile(r"media[0-9]*\.giphy\.com\Z")
_WEB_HOST = re.compile(r"[\w.-]+|[0-9a-f:.]+", re.IGNORECASE)
_GENERIC_ATTACHMENT_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})


def is_gif_attachment(attachment: AttachmentSnapshot) -> bool:
    mime = (attachment.content_type or "").partition(";")[0].strip().casefold()
    if mime in _GENERIC_ATTACHMENT_TYPES:
        return attachment.filename.casefold().endswith(".gif")
    return mime == "image/gif"


def is_gif_link(destination: str) -> bool:
    if destination.lower().startswith("www."):
        destination = f"https://{destination}"
    try:
        url = urlsplit(destination)
        host = (url.hostname or "").removesuffix(".")
    except ValueError:
        return False
    if url.scheme not in {"http", "https"} or not _WEB_HOST.fullmatch(host) or "@" in url.netloc:
        return False

    path = unquote(url.path)
    if path.casefold().endswith(".gif"):
        return True

    page_pattern = _GIF_PAGE_PATHS.get(host.removeprefix("www."))
    if page_pattern and page_pattern.fullmatch(path):
        return True

    # These provider media routes can encode a GIF as a looped video.
    lower_path = path.casefold()
    return lower_path.endswith(".mp4") and (
        (_GIPHY_MEDIA_HOST.fullmatch(host) is not None and lower_path.startswith("/media/"))
        or (host == "media.tenor.com" and lower_path.endswith("/tenor.mp4"))
    )
