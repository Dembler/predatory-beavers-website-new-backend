"""Verify or fetch the reviewed official ASB standings emblem cache.

With no arguments, all checks run offline. --fetch downloads missing cache entries;
--refresh --fetch replaces them. Source domains are explicit: the old files domain
and the new img domain use different IDs and must never be blindly rewritten.
The database and standings statistics are never changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import zlib
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps

from predatory_beavers.modules.standings.logos import LOGO_DIRECTORY, logo_key

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "asb-team-logos.json"
MAX_BYTES = 8 * 1024 * 1024


def official_logo_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and not parsed.query
        and not parsed.fragment
        and (
            (parsed.hostname == "files.infobasket.su" and parsed.path.startswith("/Logos/"))
            or (parsed.hostname == "img.infobasket.su" and parsed.path.startswith("/logo/"))
        )
        and parsed.path.rsplit("/", 1)[-1] not in {"0.png", "0.jpg", "0.webp"}
    )


def encode_logo(content: bytes) -> bytes:
    if len(content) > MAX_BYTES:
        raise ValueError("ASB emblem exceeds size limit")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        # The official VGAU PNGs contain a broken iCCP CRC. Strip ancillary
        # metadata before decoding, checking every image-data chunk intact.
        # Never enable Pillow's global permissive/truncated-image switches.
        output = bytearray(content[:8])
        position = 8
        image_chunks = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"}
        while position < len(content):
            if position + 12 > len(content):
                raise ValueError("Truncated ASB PNG chunk")
            length = struct.unpack(">I", content[position : position + 4])[0]
            end = position + length + 12
            if end > len(content):
                raise ValueError("Truncated ASB PNG data")
            chunk = content[position:end]
            kind = chunk[4:8]
            if kind in image_chunks:
                if zlib.crc32(chunk[4:-4]) != struct.unpack(">I", chunk[-4:])[0]:
                    raise ValueError("Corrupt ASB PNG image data")
                output.extend(chunk)
            elif not kind[0] & 32:
                raise ValueError("Unsupported ASB PNG critical chunk")
            position = end
        content = bytes(output)
    with Image.open(BytesIO(content)) as source:
        if source.format not in {"JPEG", "PNG", "WEBP"} or getattr(source, "is_animated", False):
            raise ValueError("ASB emblem must be a static supported raster image")
        if source.width * source.height > 20_000_000:
            raise ValueError("ASB emblem dimensions exceed limit")
        oriented = ImageOps.exif_transpose(source)
        image = oriented.convert("RGBA" if "A" in oriented.getbands() else "RGB")
        image.thumbnail((640, 640), Image.Resampling.LANCZOS)
        clean = Image.frombytes(image.mode, image.size, image.tobytes())
        output = BytesIO()
        clean.save(output, "WEBP", quality=94, method=6)
        return output.getvalue()


def cache_logos(*, fetch: bool = False, refresh: bool = False) -> dict[str, int]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("Unsupported ASB team emblem manifest")
    old_path = LOGO_DIRECTORY / "index.json"
    old_index = json.loads(old_path.read_text(encoding="utf-8")) if old_path.is_file() else {}
    entries: dict[str, dict] = {}
    teams: dict[str, dict] = {}
    downloaded = 0
    if fetch:
        LOGO_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
        for team in manifest["teams"]:
            team_id = team["external_team_id"]
            if team_id in teams or not team["names"]:
                raise ValueError("Duplicate identity or missing reviewed team names")
            source_url = team.get("source_url")
            if not source_url:
                teams[team_id] = {"names": team["names"], "key": None}
                continue
            if not official_logo_url(source_url):
                raise ValueError(f"Unexpected or placeholder ASB emblem URL: {source_url}")
            key = logo_key(source_url)
            target = LOGO_DIRECTORY / f"{key}.webp"
            old = old_index.get("images", {}).get(key, {})
            valid = (
                target.is_file()
                and old.get("source_url") == source_url
                and hashlib.sha256(target.read_bytes()).hexdigest() == old.get("sha256")
            )
            if key not in entries and (refresh or not valid):
                if not fetch:
                    raise ValueError(f"Missing or inconsistent bundled emblem: {key}")
                with client.stream("GET", source_url) as response:
                    response.raise_for_status()
                    if not response.headers.get("content-type", "").startswith("image/"):
                        raise ValueError("Official source did not return an image")
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_BYTES:
                            raise ValueError("ASB emblem exceeds size limit")
                content = encode_logo(bytes(chunks))
                temporary = target.with_suffix(".webp.tmp")
                temporary.write_bytes(content)
                temporary.replace(target)
                downloaded += 1
            content = target.read_bytes()
            with Image.open(BytesIO(content)) as image:
                if image.format != "WEBP" or image.getexif():
                    raise ValueError("Bundled emblem is not a clean WebP")
                entries[key] = {
                    "source_url": source_url,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "width": image.width,
                    "height": image.height,
                }
            teams[team_id] = {"names": team["names"], "key": key}
    index = {"version": 1, "teams": teams, "images": entries}
    if fetch:
        old_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    elif index != old_index:
        raise ValueError("Bundled emblem mapping differs from the reviewed manifest")
    return {
        "verified_images": len(entries),
        "teams_with_logo": sum(bool(team["key"]) for team in teams.values()),
        "teams_without_official_logo": sum(not team["key"] for team in teams.values()),
        "downloaded": downloaded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.refresh and not args.fetch:
        parser.error("--refresh requires --fetch")
    print(json.dumps(cache_logos(fetch=args.fetch, refresh=args.refresh)))


if __name__ == "__main__":
    main()
