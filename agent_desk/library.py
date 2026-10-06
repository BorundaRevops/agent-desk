"""Read-only index for a user's local HTML and Markdown reference library."""

from html import unescape
import json
import os
from pathlib import Path, PurePosixPath
import re

from .desk import STATE_DIR

CONFIG = STATE_DIR / "config.json"
SUFFIXES = {".html": "html", ".htm": "html", ".md": "markdown", ".markdown": "markdown"}
MAX_FILES = 500
MAX_FILE_BYTES = 4 * 1024 * 1024
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}
TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def default_root():
    desktop = Path.home() / "Desktop"
    base = desktop if desktop.is_dir() else Path.home()
    return base / "Agent Desk" / "Library"


def configured_root():
    try:
        config = json.loads(CONFIG.read_text())
        raw = config.get("library_root")
    except FileNotFoundError:
        raw = None
    if raw is not None and (not isinstance(raw, str) or not raw.strip()):
        raise ValueError("library_root must be a nonempty path")
    return Path(raw).expanduser().resolve() if raw else default_root().resolve()


def ensure_root(root):
    root = Path(root).expanduser()
    if root.is_symlink():
        raise ValueError("library root cannot be a symlink")
    if root.resolve().is_relative_to(STATE_DIR.resolve()):
        raise ValueError("library root must be separate from private Desk state")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise ValueError("library root must be a directory")
    return root.resolve()


def _title(path, kind):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        sample = handle.read(20000)
    if kind == "html":
        match = TITLE.search(sample)
        if match:
            value = re.sub(r"<[^>]*>", "", match.group(1))
            value = " ".join(unescape(value).split())
            if value:
                return value[:160]
    else:
        for line in sample.splitlines():
            if line.startswith("# "):
                return line[2:].strip()[:160]
    return path.stem.replace("-", " ").replace("_", " ")[:160]


def list_documents(root=None):
    root = Path(root).resolve() if root is not None else configured_root()
    if not root.is_dir():
        return {"root": str(root), "files": [], "truncated": False}
    found = []
    truncated = False
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and name not in SKIP_DIRS
                         and not (Path(base) / name).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            kind = SUFFIXES.get(path.suffix.lower())
            if name.startswith(".") or not kind or path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
                if stat.st_size > MAX_FILE_BYTES:
                    continue
                title = _title(path, kind)
            except OSError:
                continue
            if len(found) >= MAX_FILES:
                truncated = True
                break
            relative = path.relative_to(root).as_posix()
            found.append({"path": relative, "title": title, "kind": kind,
                          "folder": path.parent.relative_to(root).as_posix() if path.parent != root else "Library",
                          "size": stat.st_size, "modified_at": int(stat.st_mtime)})
        if truncated:
            break
    found.sort(key=lambda item: (item["folder"].casefold(), item["title"].casefold()))
    return {"root": str(root), "files": found, "truncated": truncated}


def read_document(relative, root=None):
    root = Path(root).resolve() if root is not None else configured_root()
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        raise FileNotFoundError("Invalid library path")
    path = root.joinpath(*parts)
    if any(root.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1)):
        raise FileNotFoundError("Invalid library path")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise FileNotFoundError("Document not found")
    kind = SUFFIXES.get(path.suffix.lower())
    if not kind or path.stat().st_size > MAX_FILE_BYTES:
        raise FileNotFoundError("Document not available")
    return kind, path.read_bytes()
