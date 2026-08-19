# -*- coding: utf-8 -*-
"""Klient Instagram Graph API: publikacja Reels przez video_url.

UWAGA: konto zalogowane metoda "Instagram Login" (bez posredniej Strony na
Facebooku - patrz README.md) NIE wspiera resumable upload - to wymaga
"Facebook Login for Business", ktorego nie mamy (potwierdzone bledem
"The parameter video_url is required" przy probie resumable). Dlatego
wgrywamy plik tymczasowo do publicznego repo-hostingu (github_hosting.py)
i podajemy jego raw URL jako video_url, tak jak klasyczny (nie-resumable)
przeplyw Instagram Graph API wymaga.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests

from config import Settings
from github_hosting import GithubHostingError, delete_file, upload_and_get_raw_url

logger = logging.getLogger("social_video_publisher")

STATUS_POLL_INTERVAL_SECONDS = 30
STATUS_POLL_MAX_ATTEMPTS = 10


class InstagramApiError(Exception):
    """Blad zwrocony przez Instagram Graph API."""


class InstagramClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._session = requests.Session()
        # graph.instagram.com (nie graph.facebook.com) - konto loguje sie bezposrednio
        # przez Instagram Login, bez posredniej Strony na Facebooku, patrz README.md
        self._base = f"https://graph.instagram.com/{settings.instagram_api_version}"

    @staticmethod
    def _ensure_jpg(image_path: Path) -> Path:
        """Instagram's cover_url requires a JPEG - convert on the fly if the
        source (e.g. a top1_cro-style PNG thumbnail) isn't already one."""
        if image_path.suffix.lower() in (".jpg", ".jpeg"):
            return image_path
        from PIL import Image

        jpg_path = image_path.with_suffix(".jpg")
        Image.open(image_path).convert("RGB").save(jpg_path, "JPEG", quality=92)
        return jpg_path

    def _access_token(self) -> str:
        token = self._settings.instagram_access_token
        if not token or not self._settings.instagram_ig_user_id:
            raise InstagramApiError(
                "Brak INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_IG_USER_ID w .env. "
                "Uruchom najpierw: python instagram_token_setup.py"
            )
        return token

    def publish_reel(self, video_path: str, caption: str, cover_path: str | None = None) -> dict[str, Any]:
        video_file = Path(video_path)
        if not video_file.exists():
            raise InstagramApiError(f"Nie znaleziono pliku wideo: {video_path}")
        if not self._settings.github_hosting_token:
            raise InstagramApiError(
                "Brak GH_PAT w .env - potrzebny do tymczasowego publicznego hostingu wideo "
                "(github_hosting.py). Patrz README.md."
            )

        token = self._access_token()
        ig_user_id = self._settings.instagram_ig_user_id

        # 1) wgraj plik do publicznego repo-hostingu, zeby miec video_url
        try:
            video_url, remote_path = upload_and_get_raw_url(
                str(video_file),
                repo=self._settings.github_hosting_repo,
                token=self._settings.github_hosting_token,
                branch=self._settings.github_hosting_branch,
            )
        except GithubHostingError as exc:
            raise InstagramApiError(f"Nie udalo sie wgrac wideo do hostingu: {exc}") from exc

        # 1b) opcjonalnie: wlasna okladka (cover_url) zamiast pozwalac Instagramowi
        # samemu wybrac klatke - bez tego IG czasem lapie klatke z polowy wideo,
        # a nie ten sam top1_cro-stylowy kadr co na miniaturce YouTube/Etsy.
        # cover_url wymaga JPG (nie PNG) - konwertuj w locie jesli trzeba.
        cover_url, cover_remote_path = None, None
        if cover_path and Path(cover_path).exists():
            try:
                jpg_cover_path = self._ensure_jpg(Path(cover_path))
                cover_url, cover_remote_path = upload_and_get_raw_url(
                    str(jpg_cover_path),
                    repo=self._settings.github_hosting_repo,
                    token=self._settings.github_hosting_token,
                    branch=self._settings.github_hosting_branch,
                    remote_path=f"covers/{jpg_cover_path.name}",
                )
            except Exception:  # noqa: BLE001 - okladka jest best-effort, nie moze zepsuc publikacji
                logger.exception("Nie udalo sie wgrac okladki - publikuje bez cover_url")

        try:
            container_id = self._create_and_publish(ig_user_id, token, video_url, caption, cover_url)
        finally:
            # sprzatanie niezaleznie od wyniku - nie zostawiamy plikow w publicznym repo
            try:
                delete_file(
                    remote_path,
                    repo=self._settings.github_hosting_repo,
                    token=self._settings.github_hosting_token,
                    branch=self._settings.github_hosting_branch,
                )
            except Exception:  # noqa: BLE001 - sprzatanie nie moze zepsuc wyniku publikacji
                logger.exception("Nie udalo sie posprzatac pliku hostingowego %s", remote_path)
            if cover_remote_path:
                try:
                    delete_file(
                        cover_remote_path,
                        repo=self._settings.github_hosting_repo,
                        token=self._settings.github_hosting_token,
                        branch=self._settings.github_hosting_branch,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Nie udalo sie posprzatac okladki %s", cover_remote_path)

        media_id = container_id
        permalink = media_id
        try:
            perma_resp = self._session.get(
                f"{self._base}/{media_id}",
                params={"fields": "permalink", "access_token": token},
                timeout=30,
            )
            if perma_resp.ok:
                permalink = perma_resp.json().get("permalink", media_id)
        except requests.RequestException:
            pass  # permalink to tylko wygoda w logu, brak nie blokuje sukcesu publikacji

        logger.info("Instagram: opublikowano media_id=%s (%s)", media_id, permalink)
        return {"media_id": media_id, "url": permalink}

    def _create_and_publish(
        self, ig_user_id: str, token: str, video_url: str, caption: str, cover_url: str | None = None
    ) -> str:
        # 2) utworz kontener na Reel wskazujac publiczny video_url
        payload = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": token,
        }
        if cover_url:
            payload["cover_url"] = cover_url
        create_resp = self._session.post(
            f"{self._base}/{ig_user_id}/media",
            data=payload,
            timeout=30,
        )
        if not create_resp.ok:
            raise InstagramApiError(f"Tworzenie kontenera Reels nie powiodlo sie: {create_resp.text}")
        container_id = create_resp.json()["id"]
        logger.info("Instagram: utworzono kontener %s (video_url=%s)", container_id, video_url)

        # 3) czekaj az Meta pobierze i przetworzy wideo spod video_url (status_code FINISHED)
        status_code = None
        for _ in range(STATUS_POLL_MAX_ATTEMPTS):
            time.sleep(STATUS_POLL_INTERVAL_SECONDS)
            status_resp = self._session.get(
                f"{self._base}/{container_id}",
                params={"fields": "status_code", "access_token": token},
                timeout=30,
            )
            status_resp.raise_for_status()
            status_code = status_resp.json().get("status_code")
            logger.info("Instagram: status kontenera %s = %s", container_id, status_code)
            if status_code == "FINISHED":
                break
            if status_code == "ERROR":
                raise InstagramApiError(f"Przetwarzanie wideo nie powiodlo sie (container {container_id}).")
        if status_code != "FINISHED":
            raise InstagramApiError(
                f"Timeout: kontener {container_id} nie osiagnal statusu FINISHED "
                f"w {STATUS_POLL_MAX_ATTEMPTS * STATUS_POLL_INTERVAL_SECONDS}s."
            )

        # 4) publikuj
        publish_resp = self._session.post(
            f"{self._base}/{ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
            timeout=30,
        )
        if not publish_resp.ok:
            raise InstagramApiError(f"Publikacja Reels nie powiodla sie: {publish_resp.text}")
        return publish_resp.json()["id"]
