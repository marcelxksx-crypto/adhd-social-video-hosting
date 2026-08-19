# -*- coding: utf-8 -*-
"""Klient TikTok Content Posting API v2: publikacja wideo przez FILE_UPLOAD (chunked)."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests

from config import Settings, get_tiktok_tokens, store_tiktok_tokens

logger = logging.getLogger("social_video_publisher")

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB - w widelkach 5-64MB wymaganych przez TikTok
STATUS_POLL_INTERVAL_SECONDS = 15
STATUS_POLL_MAX_ATTEMPTS = 10


class TikTokApiError(Exception):
    """Blad zwrocony przez TikTok Content Posting API."""


class TikTokClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._session = requests.Session()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._load_tokens()

    def _load_tokens(self) -> None:
        access, refresh = get_tiktok_tokens()
        if not refresh:
            raise TikTokApiError(
                "Brak refresh_token TikToka w .env. Uruchom najpierw: python tiktok_oauth_setup.py"
            )
        self._access_token = access
        self._refresh_token = refresh

    def _refresh_access_token(self) -> None:
        # tokeny dostepu TikToka wygasaja po ~24h, wiec odswiezamy przed kazda publikacja -
        # refresh_token sam sie rotuje przy kazdym uzyciu, dlatego od razu zapisujemy nowy
        resp = self._session.post(
            TOKEN_URL,
            data={
                "client_key": self._settings.tiktok_client_key,
                "client_secret": self._settings.tiktok_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if not resp.ok:
            raise TikTokApiError(f"Odswiezenie tokena TikToka nie powiodlo sie: {resp.text}")
        payload = resp.json()
        if "access_token" not in payload:
            raise TikTokApiError(f"Odswiezenie tokena TikToka zwrocilo nieoczekiwana odpowiedz: {payload}")
        self._access_token = payload["access_token"]
        self._refresh_token = payload["refresh_token"]
        store_tiktok_tokens(self._access_token, self._refresh_token)

    def publish_video(self, video_path: str, title: str, privacy_level: str | None = None) -> dict[str, Any]:
        """Publikuje wideo bezposrednio na profil (scope video.publish).

        Dopoki aplikacja nie przejdzie audytu TikToka dla Content Posting API,
        kazda publikacja jest wymuszona na SELF_ONLY (widoczna tylko dla Ciebie
        w aplikacji TikTok) - to ograniczenie TikToka, nie tego skryptu. Zobacz
        README.md.
        """
        self._refresh_access_token()

        video_file = Path(video_path)
        if not video_file.exists():
            raise TikTokApiError(f"Nie znaleziono pliku wideo: {video_path}")
        video_size = video_file.stat().st_size
        chunk_size = min(CHUNK_SIZE, video_size)
        total_chunks = max(1, (video_size + chunk_size - 1) // chunk_size)

        init_resp = self._session.post(
            INIT_URL,
            json={
                "post_info": {
                    "title": title,
                    "privacy_level": privacy_level or self._settings.tiktok_privacy_level,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "video_cover_timestamp_ms": 1000,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunks,
                },
            },
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            timeout=30,
        )
        if not init_resp.ok:
            raise TikTokApiError(f"Inicjalizacja publikacji TikTok nie powiodla sie: {init_resp.text}")
        init_data = init_resp.json()
        error_code = init_data.get("error", {}).get("code")
        if error_code not in (None, "ok"):
            raise TikTokApiError(f"TikTok zwrocil blad przy /init/: {init_data['error']}")
        publish_id = init_data["data"]["publish_id"]
        upload_url = init_data["data"]["upload_url"]
        logger.info("TikTok: zainicjowano publikacje publish_id=%s", publish_id)

        with video_file.open("rb") as fh:
            offset = 0
            chunk_index = 0
            while offset < video_size:
                fh.seek(offset)
                chunk = fh.read(chunk_size)
                end = offset + len(chunk) - 1
                put_resp = self._session.put(
                    upload_url,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{end}/{video_size}",
                    },
                    data=chunk,
                    timeout=300,
                )
                if put_resp.status_code not in (200, 201, 206):
                    raise TikTokApiError(
                        f"Blad wgrywania fragmentu {chunk_index} ({offset}-{end}): "
                        f"{put_resp.status_code} {put_resp.text}"
                    )
                offset += len(chunk)
                chunk_index += 1

        status = None
        for _ in range(STATUS_POLL_MAX_ATTEMPTS):
            time.sleep(STATUS_POLL_INTERVAL_SECONDS)
            status_resp = self._session.post(
                STATUS_URL,
                json={"publish_id": publish_id},
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                timeout=30,
            )
            status_resp.raise_for_status()
            status = status_resp.json()["data"]["status"]
            logger.info("TikTok: status publikacji %s = %s", publish_id, status)
            if status == "PUBLISH_COMPLETE":
                return {"publish_id": publish_id, "status": status}
            if status == "FAILED":
                raise TikTokApiError(f"Publikacja TikTok nie powiodla sie (publish_id={publish_id}).")

        raise TikTokApiError(
            f"Timeout: TikTok nie potwierdzil publikacji {publish_id} "
            f"w {STATUS_POLL_MAX_ATTEMPTS * STATUS_POLL_INTERVAL_SECONDS}s (ostatni status: {status})."
        )

    def list_recent_videos(self, max_count: int = 20) -> list[dict[str, Any]]:
        """Zwraca opublikowane wideo na tym koncie (tytul, opis, data) - wymaga scope video.list."""
        self._refresh_access_token()
        videos: list[dict[str, Any]] = []
        cursor = 0
        has_more = True
        while has_more and len(videos) < max_count:
            resp = self._session.post(
                "https://open.tiktokapis.com/v2/video/list/",
                params={"fields": "id,title,video_description,create_time,share_url"},
                json={"max_count": min(20, max_count - len(videos)), "cursor": cursor},
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                timeout=30,
            )
            if not resp.ok:
                raise TikTokApiError(f"Listowanie wideo TikTok nie powiodlo sie: {resp.text}")
            payload = resp.json()["data"]
            videos.extend(payload.get("videos", []))
            has_more = payload.get("has_more", False)
            cursor = payload.get("cursor", 0)
        return videos
