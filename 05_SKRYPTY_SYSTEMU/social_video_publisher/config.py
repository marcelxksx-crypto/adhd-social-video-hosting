# -*- coding: utf-8 -*-
"""Konfiguracja: wczytywanie ustawien i tokenow z pliku .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import get_key, load_dotenv, set_key

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)


@dataclass(frozen=True)
class Settings:
    schedule_csv: str
    timezone: str

    youtube_client_secrets_file: str
    youtube_token_file: str
    youtube_category_id: str

    instagram_app_id: str
    instagram_app_secret: str
    instagram_api_version: str
    instagram_access_token: str
    instagram_page_id: str
    instagram_ig_user_id: str
    instagram_redirect_uri: str

    tiktok_client_key: str
    tiktok_client_secret: str
    tiktok_redirect_uri: str
    tiktok_privacy_level: str

    pinterest_app_id: str
    pinterest_app_secret: str
    pinterest_access_token: str
    pinterest_redirect_uri: str
    pinterest_board_id: str

    github_hosting_token: str
    github_hosting_repo: str
    github_hosting_branch: str


def load_settings() -> Settings:
    # uwaga: "os.getenv(x, default)" NIE dziala jak oczekiwane, gdy .env ustawia
    # zmienna na pusty string (a nie brak zmiennej) - stad wszedzie ponizej "or default"
    return Settings(
        schedule_csv=os.getenv("SCHEDULE_CSV") or str(BASE_DIR / "schedule.csv"),
        timezone=os.getenv("TIMEZONE") or "Europe/Warsaw",
        youtube_client_secrets_file=os.getenv("YOUTUBE_CLIENT_SECRETS_FILE") or str(BASE_DIR / "client_secret_youtube.json"),
        youtube_token_file=os.getenv("YOUTUBE_TOKEN_FILE") or str(BASE_DIR / "youtube_token.json"),
        youtube_category_id=os.getenv("YOUTUBE_CATEGORY_ID") or "22",
        instagram_app_id=os.getenv("INSTAGRAM_APP_ID") or "",
        instagram_app_secret=os.getenv("INSTAGRAM_APP_SECRET") or "",
        instagram_api_version=os.getenv("INSTAGRAM_API_VERSION") or "v21.0",
        instagram_access_token=os.getenv("INSTAGRAM_ACCESS_TOKEN") or "",
        instagram_page_id=os.getenv("INSTAGRAM_PAGE_ID") or "",
        instagram_ig_user_id=os.getenv("INSTAGRAM_IG_USER_ID") or "",
        instagram_redirect_uri=os.getenv("INSTAGRAM_REDIRECT_URI") or "https://localhost:3005/oauth/redirect",
        tiktok_client_key=os.getenv("TIKTOK_CLIENT_KEY") or "",
        tiktok_client_secret=os.getenv("TIKTOK_CLIENT_SECRET") or "",
        tiktok_redirect_uri=os.getenv("TIKTOK_REDIRECT_URI") or "http://localhost:3004/oauth/redirect",
        tiktok_privacy_level=os.getenv("TIKTOK_PRIVACY_LEVEL") or "SELF_ONLY",
        pinterest_app_id=os.getenv("PINTEREST_APP_ID") or "",
        pinterest_app_secret=os.getenv("PINTEREST_APP_SECRET") or "",
        pinterest_access_token=os.getenv("PINTEREST_ACCESS_TOKEN") or "",
        pinterest_redirect_uri=os.getenv("PINTEREST_REDIRECT_URI") or "http://localhost:8085/oauth/redirect",
        pinterest_board_id=os.getenv("PINTEREST_BOARD_ID") or "",
        github_hosting_token=os.getenv("GH_PAT") or "",
        github_hosting_repo=os.getenv("GITHUB_HOSTING_REPO") or "marcelxksx-crypto/adhd-social-video-hosting",
        github_hosting_branch=os.getenv("GITHUB_HOSTING_BRANCH") or "main",
    )


def get_tiktok_tokens() -> tuple[str | None, str | None]:
    access = get_key(str(ENV_PATH), "TIKTOK_ACCESS_TOKEN")
    refresh = get_key(str(ENV_PATH), "TIKTOK_REFRESH_TOKEN")
    return access, refresh


def store_tiktok_tokens(access_token: str, refresh_token: str) -> None:
    set_key(str(ENV_PATH), "TIKTOK_ACCESS_TOKEN", access_token)
    set_key(str(ENV_PATH), "TIKTOK_REFRESH_TOKEN", refresh_token)
    os.environ["TIKTOK_ACCESS_TOKEN"] = access_token
    os.environ["TIKTOK_REFRESH_TOKEN"] = refresh_token


def get_pinterest_refresh_token() -> str | None:
    return get_key(str(ENV_PATH), "PINTEREST_REFRESH_TOKEN")


def store_pinterest_tokens(access_token: str, refresh_token: str) -> None:
    set_key(str(ENV_PATH), "PINTEREST_ACCESS_TOKEN", access_token)
    set_key(str(ENV_PATH), "PINTEREST_REFRESH_TOKEN", refresh_token)
    os.environ["PINTEREST_ACCESS_TOKEN"] = access_token
    os.environ["PINTEREST_REFRESH_TOKEN"] = refresh_token
