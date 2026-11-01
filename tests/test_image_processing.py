from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from predatory_beavers.modules.content.schemas import ContentCreate, ContentUpdate
from predatory_beavers.modules.media.errors import MediaTooLargeError, UnsupportedImageError
from predatory_beavers.modules.media.router import _read_limited
from predatory_beavers.modules.media.service import _process_image


@pytest.mark.parametrize("mode", ["P", "RGB"])
def test_png_color_key_transparency_is_preserved(mode: str) -> None:
    image = Image.new(mode, (8, 8), 0 if mode == "P" else (0, 0, 0))
    if mode == "P":
        image.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
        image.putpixel((7, 7), 1)
        transparency: int | tuple[int, int, int] = 0
    else:
        image.putpixel((7, 7), (255, 0, 0))
        transparency = (0, 0, 0)
    output = BytesIO()
    image.save(output, "PNG", transparency=transparency)
    processed = _process_image(output.getvalue(), 256, 85)
    with Image.open(BytesIO(processed.content)) as encoded:
        assert encoded.getpixel((0, 0))[3] == 0
        assert encoded.getpixel((7, 7))[3] == 255


def test_animation_is_rejected() -> None:
    output = BytesIO()
    first = Image.new("RGB", (8, 8), "red")
    second = Image.new("RGB", (8, 8), "blue")
    first.save(output, "PNG", save_all=True, append_images=[second], duration=100, loop=0)
    with pytest.raises(UnsupportedImageError):
        _process_image(output.getvalue(), 256, 85)


@pytest.mark.asyncio
async def test_upload_reader_enforces_limit_and_closes_the_file() -> None:
    exact = UploadFile(BytesIO(b"x" * 1024))
    assert await _read_limited(exact, 1024) == b"x" * 1024
    assert exact.file.closed
    oversized = UploadFile(BytesIO(b"x" * 1025))
    with pytest.raises(MediaTooLargeError):
        await _read_limited(oversized, 1024)
    assert oversized.file.closed


def test_content_sort_order_cannot_overflow_database_integer() -> None:
    with pytest.raises(ValidationError):
        ContentCreate(section="history", title="Title", body="Body", sort_order=2**100)
    with pytest.raises(ValidationError):
        ContentUpdate(sort_order=2**100)
