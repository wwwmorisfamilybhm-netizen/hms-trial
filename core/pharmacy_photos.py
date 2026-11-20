"""Approved medicine photos with bounded downloads and pinned public destinations."""
from datetime import timedelta
from html.parser import HTMLParser
from uuid import uuid4
import hashlib
import io
import ipaddress
import json
import socket
import time
from urllib.parse import urlencode, urljoin, urlsplit

import urllib3
from django.conf import settings
from django.utils import timezone
from patients.storage import private_report_storage
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError

MAX_PHOTO_BYTES = 5 * 1024 * 1024


def public_get(url, *, headers=None, limit=MAX_PHOTO_BYTES, return_url=False, timeout=15):
    """Resolve and pin each destination so redirects cannot reach private services."""
    deadline = time.monotonic() + timeout
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValidationError("Use a public HTTPS image source.")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        ips = [entry[4][0] for entry in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValidationError("This image source is unavailable.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValidationError("The image source timed out. Try another photo.")
        pool = urllib3.HTTPSConnectionPool(
            ips[0], port=443, server_hostname=parsed.hostname, assert_hostname=parsed.hostname,
            timeout=urllib3.Timeout(connect=min(5, remaining), read=min(5, remaining)), retries=False,
        )
        response = None
        try:
            response = pool.urlopen("GET", parsed.path + ("?" + parsed.query if parsed.query else ""),
                                    headers={"Host": parsed.hostname, "User-Agent": "CamsHMS/1.0", **(headers or {})},
                                    redirect=False, preload_content=False)
            if response.status in (301, 302, 303, 307, 308):
                # Subscription credentials are only sent to the original search host.
                headers = None
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status != 200:
                raise ValidationError("The photo service is unavailable. Retry or upload a photo.")
            body = bytearray()
            for chunk in response.stream(64 * 1024):
                body.extend(chunk)
                if len(body) > limit:
                    raise ValidationError("The image exceeds the 5 MB download limit.")
                if time.monotonic() > deadline:
                    raise ValidationError("The image source timed out.")
            return (bytes(body), url) if return_url else bytes(body)
        finally:
            if response:
                response.close()
            pool.close()
    raise ValidationError("The photo source redirected too many times.")


def validated_photo(data):
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise ValidationError("Choose a photo up to 5 MB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP") or image.width * image.height > 20_000_000:
                raise ValidationError("Use a JPEG, PNG or WebP photo up to 20 megapixels.")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((1200, 1200))
            output = io.BytesIO()
            image.save(output, "WEBP", quality=88)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError("This file is not a readable product photo.")


def photo_candidates(medicine):
    if not settings.BRAVE_SEARCH_API_KEY:
        raise ValidationError("Online suggestions need BRAVE_SEARCH_API_KEY configured on the server. You can upload a photo below.")
    query = medicine_image_query(medicine)
    key = "medicine-photo-" + hashlib.sha256(query.encode()).hexdigest()
    results = cache.get(key)
    if results is None:
        payload = public_get("https://api.search.brave.com/res/v1/images/search?" + urlencode({
            "q": query, "country": "IN", "search_lang": "en", "count": 12,
            "safesearch": "strict", "spellcheck": "false",
        }), headers={"X-Subscription-Token": settings.BRAVE_SEARCH_API_KEY, "Accept": "application/json"}, limit=2 * 1024 * 1024)
        results = []
        response = json.loads(payload)
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise ValidationError("The photo service returned an unreadable response. Retry or upload a photo.")
        for item in response["results"][:12]:
            if not isinstance(item, dict):
                continue
            image = (item.get("properties") or {}).get("url", "")
            source = item.get("url", "")
            thumbnail = (item.get("thumbnail") or {}).get("src", "")
            if len(source) <= 2000 and all(isinstance(url, str) and urlsplit(url).scheme == "https" for url in (image, source, thumbnail)):
                results.append({"image": image, "source": source, "thumbnail": thumbnail,
                                "title": str(item.get("title") or "Product photo")[:250]})
        cache.set(key, results, 86400)
    return [{**item, "token": signing.dumps({"medicine": medicine.pk, "candidate": item}, salt="medicine-photo")} for item in results]


class ProductPageImages(HTMLParser):
    """Read only published image metadata, without executing page scripts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open_graph = []
        self.twitter = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        attrs = {key.lower(): value for key, value in attrs if value is not None}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        image = attrs.get("content", "").strip()
        if key in ("og:image", "og:image:url", "og:image:secure_url") and image:
            self.open_graph.append(image)
        elif key in ("twitter:image", "twitter:image:src") and image:
            self.twitter.append(image)


def medicine_image_query(medicine):
    query = " ".join(filter(None, [
        medicine.name, medicine.strength, medicine.form, medicine.manufacturer,
        f"{medicine.pack_size} {medicine.unit} pack", medicine.barcode,
    ]))
    return " ".join(query.split()[:50])[:400]


def linked_photo(url):
    """Accept an image URL or extract the first usable product-page image."""
    deadline = time.monotonic() + 20
    payload, final_url = public_get(url, return_url=True, timeout=10)
    try:
        return validated_photo(payload), url
    except ValidationError:
        # Image downloads may be 5 MB, but page metadata is limited to 2 MB.
        if len(payload) > 2 * 1024 * 1024:
            raise ValidationError("This page is too large to preview. Copy its product image or upload a photo.")
    parser = ProductPageImages()
    parser.feed(payload.decode("utf-8", errors="replace"))
    candidates = list(dict.fromkeys(parser.open_graph + parser.twitter))
    for image in candidates[:4]:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            data = public_get(urljoin(final_url, image), timeout=min(10, remaining))
            return validated_photo(data), url
        except (ValidationError, OSError, ValueError, urllib3.exceptions.HTTPError):
            continue
    raise ValidationError("A usable product image could not be found on this page. Copy the image itself or upload a photo.")


def stage_photo(data, medicine, user, source=""):
    storage = private_report_storage()
    try:
        _, files = storage.listdir("medicine_previews")
    except FileNotFoundError:
        files = []
    cutoff = timezone.now() - timedelta(hours=1)
    for filename in files:
        path = "medicine_previews/" + filename
        try:
            if storage.get_modified_time(path) < cutoff:
                storage.delete(path)
        except FileNotFoundError:
            continue
    path = storage.save("medicine_previews/" + uuid4().hex + ".webp", ContentFile(data))
    return signing.dumps({"medicine": medicine.pk, "user": user.pk, "path": path, "source": source},
                         salt="medicine-photo-preview")


def read_photo_preview(token, medicine, user):
    try:
        preview = signing.loads(token, salt="medicine-photo-preview", max_age=3600)
        if preview["medicine"] != medicine.pk or preview["user"] != user.pk:
            raise ValidationError("This preview belongs to another medicine or staff member.")
        with private_report_storage().open(preview["path"], "rb") as image:
            data = image.read(MAX_PHOTO_BYTES + 1)
        return validated_photo(data), preview
    except (signing.BadSignature, FileNotFoundError, KeyError, TypeError):
        raise ValidationError("This photo preview expired. Paste or upload the photo again.")
