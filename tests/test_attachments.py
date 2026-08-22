from __future__ import annotations

import pytest

from allpath_trade.agent.attachments import (
    ALLOWED_MIMES,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    AttachmentError,
    ImageAttachment,
    placeholders,
    sniff_mime,
    validate_images,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + (36).to_bytes(4, "little") + b"WEBP" + b"\x00" * 32


def test_sniff_mime_on_real_headers():
    assert sniff_mime(PNG) == "image/png"
    assert sniff_mime(JPEG) == "image/jpeg"
    assert sniff_mime(WEBP) == "image/webp"
    assert set(ALLOWED_MIMES) == {"image/png", "image/jpeg", "image/webp"}


def test_sniff_mime_on_a_renamed_text_file_is_none():
    # The web form and Telegram both hand us a *declared* type and a
    # filename; neither is trusted -- only the magic bytes are.
    assert sniff_mime(b"ticker,qty\nAAPL,10\n") is None
    assert sniff_mime(b"") is None
    assert sniff_mime(b"\x89PN") is None  # truncated header, not a PNG
    # RIFF container that is not WebP (e.g. a .wav) must not pass.
    assert sniff_mime(b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"\x00" * 8) is None


def test_placeholder_format_and_kb_rounding():
    a = ImageAttachment(data=b"x" * 319_488, mime="image/png", name="positions.png")
    assert a.size == 319_488
    assert a.placeholder() == "[image: positions.png, 312 KB]"
    # Half-up rounding, and never "0 KB" for a file that does exist.
    assert ImageAttachment(b"x" * 1_536, "image/png", "a.png").placeholder() == \
        "[image: a.png, 2 KB]"
    assert ImageAttachment(b"x" * 10, "image/png", "tiny.png").placeholder() == \
        "[image: tiny.png, 1 KB]"


def test_placeholders_joins_with_spaces():
    imgs = [ImageAttachment(b"x" * 1024, "image/png", "a.png"),
            ImageAttachment(b"x" * 2048, "image/png", "b.png")]
    assert placeholders(imgs) == "[image: a.png, 1 KB] [image: b.png, 2 KB]"
    assert placeholders([]) == ""


def test_validate_images_accepts_the_three_types():
    out = validate_images([(PNG, "p.png"), (JPEG, "j.jpg"), (WEBP, "w.webp")])
    assert [i.mime for i in out] == ["image/png", "image/jpeg", "image/webp"]
    assert [i.name for i in out] == ["p.png", "j.jpg", "w.webp"]
    assert validate_images([]) == []


def test_validate_images_rejects_more_than_four():
    with pytest.raises(AttachmentError) as exc:
        validate_images([(PNG, f"{i}.png") for i in range(MAX_IMAGES + 1)])
    assert str(exc.value) == "Up to 4 images per message."


def test_validate_images_rejects_oversized():
    with pytest.raises(AttachmentError) as exc:
        validate_images([(PNG + b"\x00" * MAX_IMAGE_BYTES, "big.png")])
    assert str(exc.value) == "Image too large (max 5 MB)."


def test_validate_images_rejects_a_non_image():
    with pytest.raises(AttachmentError) as exc:
        validate_images([(b"ticker,qty\nAAPL,10\n", "positions.csv")])
    assert str(exc.value) == "Only PNG, JPEG, or WebP images are supported."


def test_validate_images_ignores_the_declared_extension():
    # A PNG named .txt is still a PNG; a text file named .png is still not.
    [ok] = validate_images([(PNG, "screenshot.txt")])
    assert ok.mime == "image/png"
    with pytest.raises(AttachmentError):
        validate_images([(b"not an image at all", "screenshot.png")])


def test_validate_images_sanitizes_the_filename():
    # The name reaches the transcript and the model via placeholder(); a
    # newline or a 300-char name would break the one-line display.
    [a] = validate_images([(PNG, "my\npositions\ttable.png")])
    assert a.name == "my positions table.png"
    [b] = validate_images([(PNG, "x" * 300 + ".png")])
    assert len(b.name) <= 60
    [c] = validate_images([(PNG, "   ")])
    assert c.name == "image"
