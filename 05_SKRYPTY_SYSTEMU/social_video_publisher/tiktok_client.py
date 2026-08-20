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
# "Upload to Inbox" - a genuinely different TikTok flow from Direct Post: the
# video lands in the creator's TikTok app inbox/drafts, unpublished, for them
# to open, add a caption, and manually post or discard. No post_info/privacy_level
# needed at init time since nothing goes live automatically. Not re-verified
# against live docs (same caveat as pinterest_client.py's media endpoints) -
# built from the documented TikTok Content Posting API v2 shape.
INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
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

    @staticmethod
    def _chunk_plan(video_size: int) -> tuple[int, int]:
        """TikTok allows a single-chunk upload for anything up to 64MB - every
        reel in this pipeline is well under that, and splitting into fixed
        10MB pieces anyway triggered "The total chunk count is invalid"
        (TikTok's PUT-chunk validation is stricter than the ceil-division
        math here suggests). Single-chunk (chunk_size == video_size,
        total_chunk_count == 1) is the simplest form TikTok documents and
        sidesteps that validation entirely."""
        SINGLE_CHUNK_MAX = 64 * 1024 * 1024
        if video_size <= SINGLE_CHUNK_MAX:
            return video_size, 1
        chunk_size = CHUNK_SIZE
        total_chunks = max(1, (video_size + chunk_size - 1) // chunk_size)
        return chunk_size, total_chunks

    def _upload_chunks(self, upload_url: str, video_file: Path, video_size: int, chunk_size: int) -> None:
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

    def _poll_until_done(self, publish_id: str) -> str:
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
            # Direct Post konczy na PUBLISH_COMPLETE; Inbox (draft) konczy na
            # SEND_TO_USER_INBOX, zeby wideo pojawilo sie w aplikacji do recznej
            # dokonczenia - obie oznaczaja "nasza czesc roboty skonczona".
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return status
            if status == "FAILED":
                raise TikTokApiError(f"Publikacja TikTok nie powiodla sie (publish_id={publish_id}).")
        raise TikTokApiError(
            f"Timeout: TikTok nie potwierdzil publikacji {publish_id} "
            f"w {STATUS_POLL_MAX_ATTEMPTS * STATUS_POLL_INTERVAL_SECONDS}s (ostatni status: {status})."
        )

    def publish_video(self, video_path: str, title: str, privacy_level: str | None = None) -> dict[str, Any]:
        """Publikuje wideo bezposrednio na profil (scope video.publish, Direct Post).

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
        chunk_size, total_chunks = self._chunk_plan(video_size)

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

        self._upload_chunks(upload_url, video_file, video_size, chunk_size)
        status = self._poll_until_done(publish_id)
        return {"publish_id": publish_id, "status": status}

    def upload_to_inbox(self, video_path: str) -> dict[str, Any]:
        """Tryb 'Szkice': wysyla wideo do skrzynki odbiorczej aplikacji TikTok
        zamiast publikowac na zywo - trzeba recznie otworzyc appke, dodac
        opis/hashtagi i kliknac Post (albo odrzucic). Brak post_info/tytulu w
        tym wywolaniu, bo nic nie idzie na zywo automatycznie - to sam kreator
        uzupelnia w aplikacji. Wymaga scope video.upload (moze byc oddzielny
        od video.publish uzywanego przez Direct Post - jesli 403/scope-error,
        sprawdz uprawnienia appki w TikTok Developer Portal)."""
        self._refresh_access_token()

        video_file = Path(video_path)
        if not video_file.exists():
            raise TikTokApiError(f"Nie znaleziono pliku wideo: {video_path}")
        video_size = video_file.stat().st_size
        chunk_size, total_chunks = self._chunk_plan(video_size)

        init_resp = self._session.post(
            INBOX_INIT_URL,
            json={
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
            raise TikTokApiError(f"Inicjalizacja wgrania do Inbox TikTok nie powiodla sie: {init_resp.text}")
        init_data = init_resp.json()
        error_code = init_data.get("error", {}).get("code")
        if error_code not in (None, "ok"):
            raise TikTokApiError(f"TikTok zwrocil blad przy /inbox/video/init/: {init_data['error']}")
        publish_id = init_data["data"]["publish_id"]
        upload_url = init_data["data"]["upload_url"]
        logger.info("TikTok: zainicjowano wgranie do Inbox publish_id=%s", publish_id)

        self._upload_chunks(upload_url, video_file, video_size, chunk_size)
        status = self._poll_until_done(publish_id)
        return {"publish_id": publish_id, "status": status}

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
