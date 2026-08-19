# -*- coding: utf-8 -*-
"""Klient Pinterest API v5: odczyt kont/boardow/pinow + aktualizacja tytulu/
opisu istniejacego pina (PATCH /v5/pins/{pin_id} - w oficjalnej dokumentacji
oznaczony jako "beta", moze nie byc dostepny dla kazdej aplikacji/poziomu
dostepu, stad graceful error handling zamiast zakladania ze zawsze zadziala).

Wymaga wczesniej uruchomionego pinterest_oauth_setup.py (App ID/Secret z
wlasnej aplikacji na developers.pinterest.com, patrz README.md)."""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

import requests

from config import Settings, get_pinterest_refresh_token, store_pinterest_tokens
from github_hosting import GithubHostingError, delete_file, upload_and_get_raw_url

logger = logging.getLogger("social_video_publisher")

API_BASE = "https://api.pinterest.com/v5"
TOKEN_URL = f"{API_BASE}/oauth/token"


class PinterestApiError(Exception):
    """Blad zwrocony przez Pinterest API."""


class PinterestClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._session = requests.Session()
        self._access_token = settings.pinterest_access_token

    def _refresh_access_token(self) -> None:
        """Pinterest access tokeny wygasaja po 30 dniach - odswieza sie
        automatycznie continuous refresh_tokenem (60 dni, sam sie rotuje przy
        kazdym uzyciu), bez ponownego logowania w przegladarce."""
        refresh_token = get_pinterest_refresh_token()
        if not refresh_token:
            return  # brak refresh_token (np. stara appka) - uzyj access_token as-is
        basic = base64.b64encode(
            f"{self._settings.pinterest_app_id}:{self._settings.pinterest_app_secret}".encode()
        ).decode()
        r = requests.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": "boards:read,boards:write,pins:read,pins:write,user_accounts:read"},
            timeout=30,
        )
        if not r.ok:
            logger.warning("Pinterest: odswiezenie tokena nie powiodlo sie (%s): %s", r.status_code, r.text[:300])
            return
        payload = r.json()
        self._access_token = payload["access_token"]
        store_pinterest_tokens(self._access_token, payload.get("refresh_token", refresh_token))

    def _headers(self) -> dict[str, str]:
        if not self._access_token:
            raise PinterestApiError(
                "Brak PINTEREST_ACCESS_TOKEN w .env. Uruchom najpierw: python pinterest_oauth_setup.py"
            )
        return {"Authorization": f"Bearer {self._access_token}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Wspolny punkt dla GET/PATCH/POST z retry-po-401: access tokeny
        Pinteresta wygasaja po 30 dniach (patrz _refresh_access_token), a to
        pole bylo zdefiniowane ale NIGDZIE wczesniej nie wywolywane w tym
        pliku - w praktyce Pinterest dzialal tylko przez pierwsze ~30 dni po
        OAuth, po czym kazde wywolanie zaczynalo cicho dostawac 401 (dokladnie
        to zaobserwowano przy weryfikacji tej zmiany: get_user_account() ->
        401 Authentication failed). Retry-on-401 zamiast bezwarunkowego
        odswiezania przed kazdym requestem, zeby nie dokladac zbednego
        round-tripu do kazdego wywolania API."""
        r = self._session.request(method, f"{API_BASE}{path}", headers=self._headers(), timeout=kwargs.pop("timeout", 30), **kwargs)
        if r.status_code == 401:
            self._refresh_access_token()
            r = self._session.request(method, f"{API_BASE}{path}", headers=self._headers(), timeout=30, **kwargs)
        if not r.ok:
            raise PinterestApiError(f"{method} {path} nie powiodlo sie ({r.status_code}): {r.text[:500]}")
        return r.json()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", path, json=body)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json=body)

    def get_user_account(self) -> dict[str, Any]:
        return self._get("/user_account")

    def create_board(self, name: str, description: str = "") -> dict[str, Any]:
        return self._post("/boards", {"name": name, "description": description})

    def get_or_create_board(self, name: str) -> str:
        """Zwraca board_id o podanej nazwie, tworzac go jesli jeszcze nie
        istnieje - ten sam wzorzec co EtsyClient.get_or_create_shop_section,
        zeby nie wymagac recznego wklejania PINTEREST_BOARD_ID zanim
        jakikolwiek pin moze isc na Pinterest."""
        for b in self.list_boards():
            if b.get("name", "").strip().lower() == name.strip().lower():
                return b["id"]
        created = self.create_board(name, description="ADHD-friendly digital planners & trackers")
        return created["id"]

    def list_boards(self, page_size: int = 25) -> list[dict[str, Any]]:
        boards, bookmark = [], None
        while True:
            params = {"page_size": page_size}
            if bookmark:
                params["bookmark"] = bookmark
            resp = self._get("/boards", params=params)
            boards.extend(resp.get("items", []))
            bookmark = resp.get("bookmark")
            if not bookmark:
                break
        return boards

    def list_pins(self, board_id: str | None = None, page_size: int = 25) -> list[dict[str, Any]]:
        path = f"/boards/{board_id}/pins" if board_id else "/pins"
        pins, bookmark = [], None
        while True:
            params = {"page_size": page_size}
            if bookmark:
                params["bookmark"] = bookmark
            resp = self._get(path, params=params)
            pins.extend(resp.get("items", []))
            bookmark = resp.get("bookmark")
            if not bookmark:
                break
        return pins

    def get_pin(self, pin_id: str) -> dict[str, Any]:
        return self._get(f"/pins/{pin_id}")

    def update_pin(self, pin_id: str, title: str | None = None, description: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        if not body:
            raise PinterestApiError("update_pin: brak title/description do ustawienia.")
        return self._patch(f"/pins/{pin_id}", body)

    def get_pin_analytics(self, pin_id: str, start_date: str, end_date: str) -> dict[str, Any]:
        """Metryki (impressions/saves/clicks) - wymaga konta biznesowego i
        moze nie byc dostepne dla kazdego poziomu dostepu aplikacji."""
        return self._get(
            f"/pins/{pin_id}/analytics",
            params={"start_date": start_date, "end_date": end_date, "metric_types": "IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK"},
        )

    # ------------------------------------------------------------------
    # Video pin creation - NOT present until this rollout (this file only
    # had read/update-existing-pin methods before). Implemented from
    # documented Pinterest API v5 behaviour (register media -> upload to
    # the returned presigned URL -> poll until processed -> create the pin
    # referencing the media_id) - the live docs site is a JS app that
    # neither WebFetch nor the in-app browser could render in this
    # environment, so this was NOT re-verified against a live request the
    # way every other endpoint in this file was. Treat the first real call
    # as the actual verification: if Pinterest's response shape has
    # drifted, the error message from _post/_get below will surface it
    # directly rather than failing silently.
    # ------------------------------------------------------------------
    def register_video_media(self) -> dict[str, Any]:
        return self._post("/media", {"media_type": "video"})

    def _upload_media_file(self, upload_url: str, upload_parameters: dict[str, str], file_path: str) -> None:
        path = Path(file_path)
        if not path.is_file():
            raise PinterestApiError(f"Nie znaleziono pliku do wgrania: {file_path}")
        with path.open("rb") as f:
            resp = requests.post(upload_url, data=upload_parameters, files={"file": f}, timeout=300)
        if not resp.ok:
            raise PinterestApiError(f"Wgrywanie wideo do Pinterest nie powiodlo sie ({resp.status_code}): {resp.text[:500]}")

    def _wait_for_media_ready(self, media_id: str, timeout_s: int = 180, poll_s: int = 5) -> dict[str, Any]:
        waited = 0
        while waited < timeout_s:
            info = self._get(f"/media/{media_id}")
            status = info.get("status")
            if status == "succeeded":
                return info
            if status == "failed":
                raise PinterestApiError(f"Pinterest media {media_id} status=failed: {info}")
            time.sleep(poll_s)
            waited += poll_s
        raise PinterestApiError(f"Pinterest media {media_id} nie osiagnal statusu 'succeeded' w {timeout_s}s")

    def create_video_pin(
        self,
        board_id: str,
        video_path: str,
        cover_image_path: str,
        title: str | None = None,
        description: str | None = None,
        link: str | None = None,
        alt_text: str | None = None,
    ) -> dict[str, Any]:
        """cover_image_path is a LOCAL file - Pinterest's cover_image_url must
        be a real publicly-fetchable URL (it fetches server-side), so this
        temporarily hosts the thumbnail via github_hosting.py, the same free
        relay already used for Instagram's video_url/cover_url, and cleans
        it up afterwards regardless of outcome (same try/finally pattern as
        InstagramClient.publish_reel)."""
        if not self._settings.github_hosting_token:
            raise PinterestApiError(
                "Brak GH_PAT w .env - potrzebny do tymczasowego publicznego hostingu okladki pina."
            )
        cover_file = Path(cover_image_path)
        if not cover_file.is_file():
            raise PinterestApiError(f"Nie znaleziono pliku okladki: {cover_image_path}")

        try:
            cover_url, cover_remote_path = upload_and_get_raw_url(
                str(cover_file),
                repo=self._settings.github_hosting_repo,
                token=self._settings.github_hosting_token,
                branch=self._settings.github_hosting_branch,
                remote_path=f"pin-covers/{cover_file.name}",
            )
        except GithubHostingError as exc:
            raise PinterestApiError(f"Nie udalo sie wgrac okladki pina do hostingu: {exc}") from exc

        try:
            reg = self.register_video_media()
            media_id = reg["media_id"]
            self._upload_media_file(reg["upload_url"], reg.get("upload_parameters", {}), video_path)
            self._wait_for_media_ready(media_id)

            body: dict[str, Any] = {
                "board_id": board_id,
                "media_source": {
                    "source_type": "video_id",
                    "cover_image_url": cover_url,
                    "media_id": media_id,
                },
            }
            if title:
                body["title"] = title[:100]
            if description:
                body["description"] = description[:800]
            if link:
                body["link"] = link
            if alt_text:
                body["alt_text"] = alt_text[:500]
            return self._post("/pins", body)
        finally:
            try:
                delete_file(
                    cover_remote_path,
                    repo=self._settings.github_hosting_repo,
                    token=self._settings.github_hosting_token,
                    branch=self._settings.github_hosting_branch,
                )
            except Exception:  # noqa: BLE001 - sprzatanie nie moze zepsuc wyniku publikacji
                logger.exception("Nie udalo sie posprzatac okladki pina %s", cover_remote_path)
