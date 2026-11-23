"""Images shared by the desktop bridge, terminal, and provider sessions."""

import base64
from dataclasses import dataclass
from pathlib import Path

from .localization import tr


MAX_IMAGES = 4
MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_IMAGE_TOTAL = 10 * 1024 * 1024
MAX_IMAGE_BASE64 = 4 * ((MAX_IMAGE_SIZE + 2) // 3)
MESSAGE_LIMIT = 20 * 1024 * 1024
IMAGE_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def media_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError(tr("请选择 PNG、JPEG、GIF 或 WebP 图片", "Choose a PNG, JPEG, GIF, or WebP image"))


@dataclass(frozen=True)
class ImageAttachment:
    name: str
    media_type: str
    data: bytes

    @classmethod
    def from_data(cls, name, data):
        name = " ".join(Path(name).name.split())[:255] or tr("图片", "Image")
        if len(data) > MAX_IMAGE_SIZE:
            raise ValueError(tr(f"图片 {name} 超过 5 MiB", f"Image {name} exceeds 5 MiB"))
        return cls(name, media_type(data), data)

    @classmethod
    def from_path(cls, path):
        path = path.expanduser()
        if not path.is_file():
            raise ValueError(tr(f"找不到图片文件 {path}", f"Image file not found: {path}"))
        with path.open("rb") as file:
            return cls.from_data(path.name, file.read(MAX_IMAGE_SIZE + 1))

    @classmethod
    def from_wire(cls, value):
        encoded = value["data"]
        if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BASE64:
            raise ValueError(tr("单张图片最多支持 5 MiB", "Each image can be up to 5 MiB"))
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError(tr("无法解码图片", "Could not decode the image")) from exc
        return cls.from_data(value["name"], data)

    def base64(self):
        return base64.b64encode(self.data).decode("ascii")

    def wire(self):
        return {"name": self.name, "data": self.base64()}


def load_images(values, load):
    if len(values) > MAX_IMAGES:
        raise ValueError(tr(f"每轮最多支持 {MAX_IMAGES} 张图片", f"A turn can include up to {MAX_IMAGES} images"))
    images = []
    total = 0
    for value in values:
        image = load(value)
        total += len(image.data)
        if total > MAX_IMAGE_TOTAL:
            raise ValueError(tr("每轮图片合计最多支持 10 MiB", "Images can total up to 10 MiB per turn"))
        images.append(image)
    return tuple(images)
