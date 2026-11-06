"""Cache only official portrait URLs in the reviewed ASB roster manifest.

Without --fetch, verify the bundled offline assets. --fetch downloads missing images;
--refresh explicitly replaces existing files. Images are decoded and re-encoded as
WebP without EXIF, ICC or source metadata. Database rows are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from io import BytesIO

import httpx
from PIL import Image, ImageOps
from sync_asb_roster import load_manifest

from predatory_beavers.modules.roster.photos import PHOTO_DIRECTORY, photo_key

MAX_BYTES = 8 * 1024 * 1024


def encode_portrait(content: bytes) -> bytes:
    if len(content) > MAX_BYTES:
        raise ValueError("ASB portrait exceeds size limit")
    with Image.open(BytesIO(content)) as source:
        if source.format not in {"JPEG", "PNG", "WEBP"} or getattr(source, "is_animated", False):
            raise ValueError("ASB portrait must be a static supported raster image")
        if source.width * source.height > 20_000_000:
            raise ValueError("ASB portrait dimensions exceed limit")
        oriented = ImageOps.exif_transpose(source)
        image = oriented.convert("RGBA" if "A" in oriented.getbands() else "RGB")
        image.thumbnail((1000, 1200), Image.Resampling.LANCZOS)
        # A fresh pixel buffer carries no EXIF, ICC, XMP or Pillow source info.
        clean = Image.frombytes(image.mode, image.size, image.tobytes())
        output = BytesIO()
        clean.save(output, "WEBP", quality=88, method=6)
        return output.getvalue()


def cache_photos(*, fetch: bool = False, refresh: bool = False) -> dict[str, int]:
    urls = sorted(
        {
            str(player.photo_url)
            for snapshot in load_manifest()
            for player in snapshot.players
            if player.photo_url is not None
        }
    )
    index_file = PHOTO_DIRECTORY / "index.json"
    old_index = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {}
    entries = {}
    downloaded = 0
    if fetch:
        PHOTO_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
        for url in urls:
            key = photo_key(url)
            target = PHOTO_DIRECTORY / f"{key}.webp"
            old = old_index.get(key, {})
            valid = (
                target.is_file()
                and old.get("source_url") == url
                and hashlib.sha256(target.read_bytes()).hexdigest() == old.get("sha256")
            )
            if refresh or not valid:
                if not fetch:
                    raise ValueError(f"Missing or inconsistent bundled portrait: {key}")
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    if not response.headers.get("content-type", "").startswith("image/"):
                        raise ValueError("ASB did not return an image")
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_BYTES:
                            raise ValueError("ASB portrait exceeds size limit")
                content = encode_portrait(bytes(chunks))
                temporary = target.with_suffix(".webp.tmp")
                temporary.write_bytes(content)
                temporary.replace(target)
                downloaded += 1
            content = target.read_bytes()
            with Image.open(BytesIO(content)) as image:
                if image.format != "WEBP" or image.getexif():
                    raise ValueError("Bundled portrait is not a clean WebP")
                entries[key] = {
                    "source_url": url,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "width": image.width,
                    "height": image.height,
                }
    if fetch:
        index_file.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return {"verified": len(entries), "downloaded": downloaded}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.refresh and not args.fetch:
        parser.error("--refresh requires --fetch")
    print(json.dumps(cache_photos(fetch=args.fetch, refresh=args.refresh)))


if __name__ == "__main__":
    main()
