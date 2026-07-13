from __future__ import annotations

import mimetypes
from pathlib import Path

from .db import Database
from .errors import AppError
from .services import ProjectService
from .util.time import utc_now

MAX_READ_SIZE = 1024 * 1024
IMAGE_KINDS = {"png", "jpeg", "image"}
PREVIEW_TEXT_KINDS = {"text", "markdown"}


class FileService:
    def __init__(self, db: Database, projects: ProjectService) -> None:
        self.db = db
        self.projects = projects

    def list_files(self, project_id: str, rel_path: str = "") -> dict:
        root, target = self._resolve(project_id, rel_path)
        if not target.exists():
            raise AppError("file_not_found", "File path was not found.", 404)
        if not target.is_dir():
            raise AppError("file_path_invalid", "Path is not a directory.")
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            try:
                stat = child.stat()
            except OSError:
                continue
            child_rel = child.relative_to(root).as_posix()
            kind = "directory" if child.is_dir() else "file"
            viewer_kind = "directory" if child.is_dir() else self._viewer_kind(child, stat.st_size)
            entries.append(
                {
                    "name": child.name,
                    "path": child_rel,
                    "kind": kind,
                    "size": None if child.is_dir() else stat.st_size,
                    "modifiedAt": _mtime_to_iso(stat.st_mtime),
                    "isText": None if child.is_dir() else viewer_kind in PREVIEW_TEXT_KINDS,
                    "viewerKind": viewer_kind,
                }
            )
        return {"path": "" if target == root else target.relative_to(root).as_posix(), "entries": entries}

    def read_content(self, project_id: str, rel_path: str) -> dict:
        root, target = self._resolve(project_id, rel_path)
        if not target.exists():
            raise AppError("file_not_found", "File path was not found.", 404)
        if target.is_dir():
            raise AppError("file_path_invalid", "Path is a directory.")
        size = target.stat().st_size
        viewer_kind = self._viewer_kind(target, size)
        if viewer_kind not in PREVIEW_TEXT_KINDS:
            raise AppError("file_not_previewable", "This file type is not previewed as text.", 415, {"viewerKind": viewer_kind})
        if size > MAX_READ_SIZE:
            raise AppError("file_too_large", "File is too large to preview.", 413)
        content = target.read_bytes().decode("utf-8", errors="replace")
        return {
            "path": target.relative_to(root).as_posix(),
            "kind": viewer_kind,
            "encoding": "utf-8",
            "size": size,
            "content": content,
        }

    def _resolve(self, project_id: str, rel_path: str) -> tuple[Path, Path]:
        if "\x00" in rel_path:
            raise AppError("file_path_invalid", "File path contains a NUL character.")
        rel = Path(rel_path or ".")
        if rel.is_absolute() or "\\" in rel_path or any(part == ".." for part in rel.parts):
            raise AppError("file_path_invalid", "File path must stay inside the project.")
        project = self.projects.get_project_row(project_id)
        root = Path(project["path"]).resolve()
        candidate = root / rel
        self._reject_symlink_segments(root, candidate)
        target = candidate.resolve()
        if target != root and root not in target.parents:
            raise AppError("file_path_invalid", "File path escapes the project root.")
        return root, target

    def _reject_symlink_segments(self, root: Path, candidate: Path) -> None:
        try:
            rel_parts = candidate.relative_to(root).parts
        except ValueError:
            raise AppError("file_path_invalid", "File path escapes the project root.")
        current = root
        for part in rel_parts:
            current = current / part
            if current.is_symlink():
                raise AppError("file_path_invalid", "Symbolic links are not followed in MVP.")

    def _viewer_kind(self, path: Path, size: int) -> str:
        suffix = path.suffix.lower()
        if suffix in {".png"}:
            return "png"
        if suffix in {".jpg", ".jpeg"}:
            return "jpeg"
        if suffix in {".gif", ".webp", ".bmp", ".tif", ".tiff"}:
            return "image"
        if suffix == ".pdf":
            return "pdf"
        if suffix in {".doc", ".docx"}:
            return "word"
        if suffix in {".xls", ".xlsx", ".xlsm", ".csv"}:
            return "excel"
        if suffix in {".md", ".markdown"}:
            return "markdown"
        mime, _ = mimetypes.guess_type(path.name)
        if mime and mime.startswith("image/"):
            return "image"
        try:
            sample = path.read_bytes()[:4096]
        except OSError:
            return "unknown"
        if b"\x00" in sample:
            return "binary"
        if size > MAX_READ_SIZE:
            return "too_large"
        if mime and not (mime.startswith("text/") or mime in {"application/json", "application/xml"}):
            return "binary"
        return "text"


def _mtime_to_iso(mtime: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
