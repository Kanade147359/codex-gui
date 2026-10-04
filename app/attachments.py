"""Immutable image uploads. Only server-issued IDs, never client file paths, are accepted."""
import io
import re
import uuid
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .models import now_iso

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_BYTES = 40 * 1024 * 1024
MAX_IMAGES = 8
MAX_DIMENSION = 8192
MAX_PIXELS = 32_000_000
FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}
ID_RE = re.compile(r"^[a-f0-9]{32}$")


class AttachmentError(ValueError):
    pass


def inspect_image(data: bytes) -> dict:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError("画像は空にできません。1枚の上限は10 MiBです。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                fmt, (width, height) = image.format, image.size
                if fmt not in FORMATS:
                    raise AttachmentError("対応形式はPNG、JPEG、WebPです（実データで判定）。")
                if max(width, height) > MAX_DIMENSION or width * height > MAX_PIXELS:
                    raise AttachmentError("画像寸法の上限は各辺8192 px、合計3200万画素です。")
                if getattr(image, "n_frames", 1) != 1:
                    raise AttachmentError("アニメーション画像には対応していません。")
                image.verify()
            # verify() checks structure; load() also checks compressed pixel data.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except AttachmentError:
        raise
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as e:
        raise AttachmentError("画像が破損しているか、画像として読み取れないか、寸法上限を超えています。") from e
    extension, media_type = FORMATS[fmt]
    return dict(extension=extension, media_type=media_type, width=width, height=height, size=len(data))


class AttachmentStore:
    def __init__(self, root: Path, db):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db

    def save(self, data: bytes, filename: str) -> dict:
        info = inspect_image(data)
        image_id = uuid.uuid4().hex
        # The name is display metadata only, and cannot influence the stored path.
        name = filename.replace("\\", "/").split("/")[-1]
        name = "".join(c for c in name if c.isprintable())[:200] or f"image.{info['extension']}"
        row = dict(id=image_id, filename=name, created_at=now_iso(), **info)
        path = self.path(row)
        created = False
        try:
            with path.open("xb") as f:
                created = True
                f.write(data)
            self.db.add_attachment(row)
        except Exception:
            if created:
                path.unlink(missing_ok=True)  # only this new, unreferenced upload
            raise
        return self.view(row)

    def get(self, image_id: str) -> dict:
        if not isinstance(image_id, str) or not ID_RE.fullmatch(image_id):
            raise AttachmentError("画像IDが無効です。")
        row = self.db.get_attachment(image_id)
        if row is None:
            raise AttachmentError("添付画像が見つかりません。再添付してください。")
        return row

    def path(self, row: dict) -> Path:
        path = self.root / f"{row['id']}.{row['extension']}"
        if path.resolve().parent != self.root or path.is_symlink():
            raise AttachmentError("添付画像の保存パスが無効です。")
        return path

    def resolve(self, ids) -> list[str]:
        if not isinstance(ids, (list, tuple)) or len(ids) > MAX_IMAGES:
            raise AttachmentError("添付画像は1メッセージにつき8枚までです。")
        if len(set(ids)) != len(ids):
            raise AttachmentError("同じ画像IDを重複して添付できません。")
        paths, total = [], 0
        for image_id in ids:
            row = self.get(image_id)
            path = self.path(row)
            try:
                with path.open("rb") as f:
                    info = inspect_image(f.read(MAX_IMAGE_BYTES + 1))
            except OSError as e:
                raise AttachmentError(f"添付画像「{row['filename']}」を読み取れません。再添付してください。") from e
            if any(info[k] != row[k] for k in info):
                raise AttachmentError(f"添付画像「{row['filename']}」の保存データが変更されています。")
            total += info["size"]
            paths.append(str(path))
        if total > MAX_MESSAGE_BYTES:
            raise AttachmentError("添付画像の合計上限は1メッセージにつき40 MiBです。")
        return paths

    def paths(self, ids) -> list[str]:
        """Paths after _check_images/_prepare has validated the bytes off the event loop."""
        return [str(self.path(self.get(image_id))) for image_id in ids]

    @staticmethod
    def view(row: dict) -> dict:
        return {k: row[k] for k in ("id", "filename", "media_type", "width", "height", "size")} | {
            "url": f"/api/attachments/{row['id']}"}

    def views(self, ids) -> list[dict]:
        return [self.view(self.get(image_id)) for image_id in ids]
