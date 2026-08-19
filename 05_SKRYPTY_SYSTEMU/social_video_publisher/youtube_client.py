# -*- coding: utf-8 -*-
"""Klient YouTube Data API v3: upload wideo jako Short (upload resumable)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import Settings

logger = logging.getLogger("social_video_publisher")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",  # needed for videos().update() (privacyStatus changes)
]


class YouTubeApiError(Exception):
    """Blad zwrocony przez YouTube Data API."""


class YouTubeClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._service = None

    def _load_credentials(self) -> Credentials:
        token_path = Path(self._settings.youtube_token_file)
        if not token_path.exists():
            raise YouTubeApiError(
                "Brak pliku tokena YouTube. Uruchom najpierw: python youtube_oauth_setup.py"
            )
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _get_service(self):
        if self._service is None:
            creds = self._load_credentials()
            self._service = build("youtube", "v3", credentials=creds)
        return self._service

    def set_thumbnail(self, video_id: str, thumbnail_path: str) -> None:
        """Uploads a custom thumbnail for an already-published video. Without
        this, YouTube auto-picks a frame from the video itself - which, for
        the video-generator pipeline before its 2026-08 intro fix, risked
        picking the dead, empty-background moment as the channel-grid
        thumbnail. Needs the youtube.upload scope (already requested)."""
        thumb_file = Path(thumbnail_path)
        if not thumb_file.exists():
            raise YouTubeApiError(f"Nie znaleziono pliku miniaturki: {thumbnail_path}")
        service = self._get_service()
        media = MediaFileUpload(str(thumb_file), mimetype="image/png")
        service.thumbnails().set(videoId=video_id, media_body=media).execute()
        logger.info("YouTube: ustawiono custom miniaturke dla video_id=%s", video_id)

    def upload_short(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list[str],
        publish_at_iso_utc: str | None = None,
        thumbnail_path: str | None = None,
    ) -> dict[str, Any]:
        """Wgrywa pionowe wideo <=60s jako YouTube Short.

        YouTube samo wykrywa "Short" po proporcjach (pion) i dlugosci (<=60s) -
        nie ma osobnego "typu" do ustawienia, wystarczy wrzucic wlasciwy plik.
        Wideo zawsze trafia jako prywatne (privacyStatus=private) - widoczne
        tylko dla wlasciciela konta. publish_at_iso_utc pozwala dodatkowo
        zaplanowac AUTOMATYCZNE przejscie na public o danej godzinie (YouTube
        samo je opublikuje) - zostaw None, zeby wideo zostalo prywatne na stale.
        """
        video_file = Path(video_path)
        if not video_file.exists():
            raise YouTubeApiError(f"Nie znaleziono pliku wideo: {video_path}")

        service = self._get_service()

        status: dict[str, Any] = {
            "selfDeclaredMadeForKids": False,
            "privacyStatus": "private",
        }
        if publish_at_iso_utc:
            status["publishAt"] = publish_at_iso_utc

        body = {
            "snippet": {
                "title": title[:100],
                "description": description,
                "tags": tags,
                "categoryId": self._settings.youtube_category_id,
            },
            "status": status,
        }
        media = MediaFileUpload(str(video_file), chunksize=-1, resumable=True, mimetype="video/mp4")
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            progress, response = request.next_chunk()
            if progress:
                logger.info("YouTube upload postep: %d%%", int(progress.progress() * 100))

        video_id = response["id"]
        logger.info("YouTube: opublikowano video_id=%s", video_id)

        if thumbnail_path:
            try:
                self.set_thumbnail(video_id, thumbnail_path)
            except YouTubeApiError:
                # a missing/bad thumbnail shouldn't undo an otherwise-
                # successful upload - the video is already live either way
                logger.exception("YouTube: nie udalo sie ustawic miniaturki dla video_id=%s", video_id)

        return {"video_id": video_id, "url": f"https://youtube.com/shorts/{video_id}"}

    def list_recent_uploads(self, max_results: int = 25) -> list[dict[str, Any]]:
        """Zwraca ostatnio opublikowane wideo na tym kanale (tytul, opis, data)."""
        service = self._get_service()
        channels_resp = service.channels().list(part="contentDetails", mine=True).execute()
        items = channels_resp.get("items", [])
        if not items:
            return []
        uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        videos: list[dict[str, Any]] = []
        page_token = None
        while len(videos) < max_results:
            resp = service.playlistItems().list(
                part="snippet",
                playlistId=uploads_playlist_id,
                maxResults=min(50, max_results - len(videos)),
                pageToken=page_token,
            ).execute()
            for item in resp.get("items", []):
                snippet = item["snippet"]
                videos.append(
                    {
                        "title": snippet["title"],
                        "description": snippet.get("description", ""),
                        "publishedAt": snippet["publishedAt"],
                        "videoId": snippet["resourceId"]["videoId"],
                    }
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return videos
