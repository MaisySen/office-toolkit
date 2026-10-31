"""Shapes shared by the engine and the P2 op modules (ops_p2a, ops_p2b).

An op module exposes OPERATIONS (slug -> Operation) and HANDLERS
(slug -> handler(ctx, inputs, opts, out) -> facts). engine.py loads every
module named in its P2_MODULES list, so a new module needs no engine edit.
This file imports nothing from the engine, so op modules can be tested on
their own.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

PDF = "application/pdf"
ZIP = "application/zip"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
JSON = "application/json"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV = "text/csv"
TXT = "text/plain"
MD = "text/markdown"
PNG = "image/png"

# Output file extension -> content type, for ops whose output depends on options.
OUTPUT_TYPES = {".pdf": PDF, ".zip": ZIP, ".docx": DOCX, ".json": JSON, ".xlsx": XLSX,
                ".csv": CSV, ".txt": TXT, ".md": MD, ".png": PNG}
# R86: video, audio and photo results.
OUTPUT_TYPES.update({".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
                     ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
                     ".gif": "image/gif", ".jpg": "image/jpeg"})


def content_type_for(name: str) -> str:
    return OUTPUT_TYPES.get(os.path.splitext(name or "")[1].lower(), "application/octet-stream")


@dataclass(frozen=True)
class Option:
    """One setting a tool takes, sent as a multipart field beside the files.

    kind: text, password, choice, int, ranges (1-3,5), phrases (one per
    line) or json (a JSON string up to max_len, parsed by the engine; the
    op checks its shape). The caller builds its options sheet from these."""
    name: str
    kind: str
    label: str
    required: bool = False
    default: str | int | None = None
    choices: tuple[str, ...] = ()
    min: int | None = None
    max: int | None = None
    max_len: int = 200

    def public(self) -> dict:
        out = {"name": self.name, "kind": self.kind, "label": self.label,
               "required": self.required}
        if self.default is not None:
            out["default"] = self.default
        if self.choices:
            out["choices"] = list(self.choices)
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.kind in ("text", "password", "ranges", "phrases", "json"):
            out["max_len"] = self.max_len
        return out


@dataclass(frozen=True)
class ExtraFiles:
    """A second multipart field of files beside "files" (for example the
    signature PNGs that sign takes next to its one PDF)."""
    field: str
    label: str
    min: int
    max: int
    exts: tuple[str, ...]
    max_bytes_each: int | None = None

    def public(self) -> dict:
        out = {"field": self.field, "label": self.label, "min": self.min, "max": self.max,
               "accepts_ext": list(self.exts)}
        if self.max_bytes_each:
            out["max_bytes_each"] = self.max_bytes_each
        return out


@dataclass(frozen=True)
class Operation:
    slug: str
    label: str
    kind: str          # pdf, office, image, or file (any extension in exts)
    min_files: int
    max_files: int
    output_name: str
    content_type: str = PDF
    options: tuple[Option, ...] = ()
    max_pages: int | None = None   # tighter than the engine wide page cap
    max_bytes: int | None = None   # tighter than the caller's plan cap
    slow: bool = False             # reports page by page progress
    # P2 additions; P1 entries leave them empty and list exactly as before.
    exts: tuple[str, ...] = ()               # kind "file": extensions taken
    extra: ExtraFiles | None = None          # a second field of files
    outputs: tuple[str, ...] = ()            # other output names an option can pick
    limits: tuple[tuple[str, int], ...] = ()  # further caps, published as stated

    def extra_public(self) -> dict:
        """Keys added to the operations listing only for ops that use them."""
        out: dict = {}
        if self.exts:
            out["accepts_ext"] = list(self.exts)
        if self.extra:
            out["extra_files"] = self.extra.public()
        if self.outputs:
            out["outputs"] = [{"name": n, "content_type": content_type_for(n)}
                              for n in (self.output_name,) + self.outputs]
        if self.limits:
            out["limits"] = dict(self.limits)
        return out


@dataclass
class Ctx:
    """What a P2 handler gets besides its inputs: a scratch dir (deleted
    after the job), the extra files, a progress callback and a way to
    pick another output name (one of the op's outputs)."""
    work: str
    out_dir: str
    extra: list[str] = field(default_factory=list)
    progress: Callable[[int, int], None] = lambda done, total: None
    output_name: str | None = None

    def output(self, name: str) -> str:
        self.output_name = name
        return os.path.join(self.out_dir, name)
