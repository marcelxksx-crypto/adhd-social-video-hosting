# -*- coding: utf-8 -*-
"""Harmonogram publikacji przechowywany w prostym pliku CSV (schedule.csv)."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

FIELDNAMES = [
    "id",
    "video_path",
    "thumbnail_path",
    "platforms",
    "title",
    "caption",
    "hashtags_tiktok",
    "hashtags_instagram",
    "youtube_tags",
    "scheduled_at",
    "status",
    "result",
    "updated_at",
]


@dataclass
class ScheduleRow:
    id: str
    video_path: str
    platforms: str
    title: str
    caption: str
    hashtags_tiktok: str
    hashtags_instagram: str
    youtube_tags: str
    scheduled_at: str
    status: str
    result: str
    updated_at: str
    thumbnail_path: str = ""

    def platform_list(self) -> list[str]:
        return [p.strip() for p in self.platforms.split(";") if p.strip()]

    def full_caption(self, hashtags: str) -> str:
        tags = " ".join(f"#{t.strip().lstrip('#')}" for t in hashtags.split() if t.strip())
        return f"{self.caption}\n\n{tags}".strip()


def load_rows(csv_path: Path) -> list[ScheduleRow]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return [ScheduleRow(**{name: (row.get(name) or "") for name in FIELDNAMES}) for row in reader]


def save_rows(csv_path: Path, rows: list[ScheduleRow]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: getattr(row, name) for name in FIELDNAMES})


def is_due(row: ScheduleRow, tz_name: str) -> bool:
    """Wiersz jest 'do publikacji teraz', jesli nie ma jeszcze statusu i jego
    scheduled_at (czas lokalny w strefie tz_name) juz nadszedl."""
    if row.status:
        return False
    if not row.scheduled_at:
        return False
    try:
        naive = datetime.fromisoformat(row.scheduled_at)
    except ValueError:
        return False
    tz = ZoneInfo(tz_name)
    local = naive if naive.tzinfo else naive.replace(tzinfo=tz)
    return local <= datetime.now(tz)
