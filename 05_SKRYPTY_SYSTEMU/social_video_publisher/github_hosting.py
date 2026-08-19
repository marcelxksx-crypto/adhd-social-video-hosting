# -*- coding: utf-8 -*-
"""Tymczasowy publiczny hosting plikow wideo przez GitHub Contents API.

Instagram Graph API dla kont zalogowanych metoda "Instagram Login" (bez
Strony na Facebooku - patrz README.md) NIE wspiera resumable upload
(wymaga "Facebook Login for Business", ktorego nie mamy) - wiec musimy
podac video_url wskazujacy na publicznie dostepny plik. Zamiast platnego
hostingu, wgrywamy plik do osobnego PUBLICZNEGO repo na GitHubie i uzywamy
raw.githubusercontent.com jako video_url - darmowe, bez nowych kont.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("social_video_publisher")

API_BASE = "https://api.github.com"


class GithubHostingError(Exception):
    """Blad podczas wgrywania/usuwania pliku w repo-hostingu."""


def upload_and_get_raw_url(
    local_path: str,
    repo: str,
    token: str,
    branch: str = "main",
    remote_path: str | None = None,
) -> tuple[str, str]:
    """Wgrywa plik do publicznego repo-hostingu i zwraca (raw_url, remote_path)
    do pozniejszego posprzatania (delete_file)."""
    file = Path(local_path)
    if not file.exists():
        raise GithubHostingError(f"Nie znaleziono pliku do wgrania: {local_path}")

    remote_path = remote_path or f"videos/{file.name}"
    content_b64 = base64.b64encode(file.read_bytes()).decode("ascii")

    resp = requests.put(
        f"{API_BASE}/repos/{repo}/contents/{remote_path}",
        json={"message": f"host: {file.name}", "content": content_b64, "branch": branch},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=60,
    )
    if not resp.ok:
        raise GithubHostingError(f"Wgrywanie do repo-hostingu nie powiodlo sie: {resp.text}")

    owner_repo = repo  # "owner/repo"
    raw_url = f"https://raw.githubusercontent.com/{owner_repo}/{branch}/{remote_path}"
    logger.info("github_hosting: wgrano %s -> %s", file.name, raw_url)
    return raw_url, remote_path


def delete_file(remote_path: str, repo: str, token: str, branch: str = "main") -> None:
    """Usuwa wczesniej wgrany plik z repo-hostingu (sprzatanie po publikacji)."""
    get_resp = requests.get(
        f"{API_BASE}/repos/{repo}/contents/{remote_path}",
        params={"ref": branch},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    if not get_resp.ok:
        logger.warning("github_hosting: nie znaleziono pliku do usuniecia %s (pomijam)", remote_path)
        return
    sha = get_resp.json()["sha"]

    del_resp = requests.delete(
        f"{API_BASE}/repos/{repo}/contents/{remote_path}",
        json={"message": f"cleanup: {remote_path}", "sha": sha, "branch": branch},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    if not del_resp.ok:
        logger.warning("github_hosting: usuwanie %s nie powiodlo sie (pomijam): %s", remote_path, del_resp.text)
    else:
        logger.info("github_hosting: posprzatano %s", remote_path)
