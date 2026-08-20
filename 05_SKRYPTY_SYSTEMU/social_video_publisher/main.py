# -*- coding: utf-8 -*-
"""
Publikator wideo na TikTok / Instagram Reels / YouTube Shorts wedlug schedule.csv.

Uzycie:
    python main.py --dry-run                    # pokaz co jest "due" teraz, nic nie publikuje
    python main.py --run-due                     # opublikuj wszystkie wiersze, ktorych czas juz nadszedl
    python main.py --run-due --only youtube       # ogranicz do jednej platformy (np. test)

Zamierzony sposob uzycia: zarejestruj `python main.py --run-due` w Harmonogramie
Zadan Windows (co 15 min) - patrz README.md. Kazdy wiersz jest publikowany
DOKLADNIE RAZ (status w CSV zapobiega duplikatom), a blad jednej platformy nie
przerywa publikacji na pozostalych platformach tego samego wiersza ani kolejnych
wierszy w tym samym uruchomieniu.
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import BASE_DIR, ENV_PATH, Settings, load_settings
from schedule_store import ScheduleRow, is_due, load_rows, save_rows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "publisher.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("social_video_publisher")

# YouTube Short descriptions and Pinterest pins are the only two caption
# slots (of the 4 platforms) that support a real clickable outbound link -
# IG/TikTok captions render URLs as inert text, so those two instead rely on
# each profile's bio-link field. That field is edited by hand in each app's
# own settings (an account-settings change, not something this script does
# on its own) - use YOUTUBE_UTM_LINK/PINTEREST_UTM_LINK below as the pattern
# for the IG/TikTok bio links too (swap utm_source=instagram / tiktok).
ETSY_SHOP_URL = "https://www.etsy.com/shop/Shareyourself"
YOUTUBE_UTM_LINK = f"{ETSY_SHOP_URL}?utm_source=youtube&utm_medium=social&utm_campaign=shorts_organic"
# Pinterest pins have a dedicated `link` field (the actual outbound-click
# destination shown on the pin) separate from the description - unlike
# YouTube this isn't a text workaround, it's the real intended field.
PINTEREST_UTM_LINK = f"{ETSY_SHOP_URL}?utm_source=pinterest&utm_medium=social&utm_campaign=pin_organic"


def _notify_windows(title: str, message: str) -> None:
    """Pokazuje dymek powiadomienia Windows - jedyny sposob, w jaki ten skrypt
    (uruchamiany bez okna, z Harmonogramu Zadan) moze cokolwiek "krzyknac" na
    ekran, gdy publikacja sie nie powiedzie. Bez dodatkowych zaleznosci Python -
    korzysta z wbudowanego System.Windows.Forms przez PowerShell.

    No-op poza Windows (np. na runnerze GitHub Actions) - tam i tak nikt nie
    patrzy na ekran, a bledy trafiaja do logu joba."""
    if platform.system() != "Windows":
        return
    ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Warning
$notify.Visible = $true
$notify.BalloonTipTitle = "{title}"
$notify.BalloonTipText = "{message}"
$notify.ShowBalloonTip(15000)
Start-Sleep -Seconds 16
$notify.Dispose()
"""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        logger.exception("Nie udalo sie pokazac powiadomienia Windows (nieblokujace, publikacja i tak sie odbyla).")


def _resolve_video_path(video_path: str) -> Path:
    path = Path(video_path)
    if path.is_absolute():
        return path
    # sciezki w schedule.csv sa wzgledem korzenia calego projektu Etsy (2 poziomy nad tym folderem)
    return (BASE_DIR / ".." / ".." / video_path).resolve()


def _ensure_pinterest_board(settings: Settings) -> Settings:
    """Rozwiazuje PINTEREST_BOARD_ID raz na cale uruchomienie (nie w kazdym
    wierszu publish_row) i zapisuje go do .env, zeby kolejne uruchomienia
    (co 15 min z Harmonogramu Zadan) nie tworzyly/szukaly boarda za kazdym
    razem. Bezpieczne do wywolania nawet gdy zaden wiersz nie celuje w
    Pinterest w tym uruchomieniu - to jednorazowy koszt jednego zapytania."""
    if settings.pinterest_board_id:
        return settings
    if not settings.pinterest_access_token:
        return settings  # Pinterest jeszcze nie skonfigurowany - nic do zrobienia
    try:
        from dotenv import set_key

        from pinterest_client import PinterestClient

        board_id = PinterestClient(settings).get_or_create_board("ADHD Planners & Trackers")
        set_key(str(ENV_PATH), "PINTEREST_BOARD_ID", board_id)
        logger.info("Pinterest: rozwiazano/zapisano PINTEREST_BOARD_ID=%s", board_id)
        return dataclasses.replace(settings, pinterest_board_id=board_id)
    except Exception:  # noqa: BLE001 - brak boarda nie moze zablokowac pozostalych platform
        logger.exception("Nie udalo sie rozwiazac PINTEREST_BOARD_ID - wiersze z Pinterest w tym uruchomieniu zawioda.")
        return settings


def publish_row(row: ScheduleRow, settings, only: str | None) -> None:
    video_path = _resolve_video_path(row.video_path)
    results: list[str] = []
    had_error = False
    had_success = False

    platforms = row.platform_list()
    if only:
        platforms = [p for p in platforms if p == only]

    if "youtube" in platforms:
        try:
            from youtube_client import YouTubeClient

            client = YouTubeClient(settings)
            tags = [t.strip() for t in row.youtube_tags.split(",") if t.strip()]
            thumb_path = str(_resolve_video_path(row.thumbnail_path)) if row.thumbnail_path else None
            description = f"{row.caption}\n\n\U0001F449 Shop this printable: {YOUTUBE_UTM_LINK}"
            res = client.upload_short(
                str(video_path), title=row.title, description=description, tags=tags,
                thumbnail_path=thumb_path,
            )
            results.append(f"youtube:{res['url']}")
            had_success = True
        except Exception as exc:  # noqa: BLE001 - blad jednej platformy nie moze zabic calego wiersza
            logger.exception("YouTube: publikacja wiersza %s nie powiodla sie", row.id)
            results.append(f"youtube:ERROR:{exc}")
            had_error = True

    if "instagram" in platforms:
        try:
            from instagram_client import InstagramClient

            # UWAGA: Instagram Graph API nie ma prawdziwego trybu "Szkice" -
            # w przeciwienstwie do YouTube (privacyStatus=private) i TikToka
            # (Upload to Inbox), nie istnieje udokumentowany sposob wgrania
            # Reelsa do recznej recenzji w aplikacji. Jedyna alternatywa
            # (utworzyc kontener i NIE wolac media_publish) nie tworzy niczego
            # widocznego/zarzadzalnego w apce - kontener po prostu wygasa po
            # ~24h po cichu, wiec to gorsze niz publikacja na zywo, nie
            # bezpieczniejsze. Do czasu realnej alternatywy Instagram publikuje
            # od razu na zywo, tak jak wczesniej.
            client = InstagramClient(settings)
            caption = row.full_caption(row.hashtags_instagram)
            cover_path = str(_resolve_video_path(row.thumbnail_path)) if row.thumbnail_path else None
            res = client.publish_reel(str(video_path), caption=caption, cover_path=cover_path)
            results.append(f"instagram:{res['url']}")
            had_success = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Instagram: publikacja wiersza %s nie powiodla sie", row.id)
            results.append(f"instagram:ERROR:{exc}")
            had_error = True

    if "tiktok" in platforms:
        try:
            from tiktok_client import TikTokClient

            # Tryb "Szkice": wgrywa do Inbox TikToka zamiast publikowac na
            # zywo (Direct Post) - trzeba recznie otworzyc appke i kliknac
            # Post. Patrz TikTokClient.upload_to_inbox().
            client = TikTokClient(settings)
            res = client.upload_to_inbox(str(video_path))
            results.append(f"tiktok:{res['status']}")
            had_success = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("TikTok: publikacja wiersza %s nie powiodla sie", row.id)
            results.append(f"tiktok:ERROR:{exc}")
            had_error = True

    if "pinterest" in platforms:
        try:
            from pinterest_client import PinterestClient

            client = PinterestClient(settings)
            board_id = settings.pinterest_board_id or client.get_or_create_board("ADHD Planners & Trackers")
            cover_path = str(_resolve_video_path(row.thumbnail_path)) if row.thumbnail_path else None
            if not cover_path:
                raise RuntimeError("Brak thumbnail_path w wierszu - Pinterest wymaga okladki (cover_image).")
            res = client.create_video_pin(
                board_id=board_id,
                video_path=str(video_path),
                cover_image_path=cover_path,
                title=row.title,
                description=row.full_caption(row.hashtags_instagram),
                link=PINTEREST_UTM_LINK,
                alt_text=row.title,
            )
            results.append(f"pinterest:{res.get('id', 'ok')}")
            had_success = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pinterest: publikacja wiersza %s nie powiodla sie", row.id)
            results.append(f"pinterest:ERROR:{exc}")
            had_error = True

    if had_error and had_success:
        row.status = "PARTIAL"
    elif had_error:
        row.status = "ERROR"
    else:
        row.status = "DONE"
    row.result = "; ".join(results)
    row.updated_at = datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")

    if row.status in ("ERROR", "PARTIAL"):
        _notify_windows(
            f"Publikacja nie w pelni sie powiodla ({row.id})",
            row.result[:200],
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="pokaz wiersze 'due', nic nie publikuj")
    parser.add_argument("--run-due", action="store_true", help="opublikuj wszystkie wiersze 'due'")
    parser.add_argument("--only", choices=["youtube", "instagram", "tiktok", "pinterest"], default=None)
    args = parser.parse_args()

    settings = load_settings()
    csv_path = Path(settings.schedule_csv)
    rows = load_rows(csv_path)

    if not rows:
        logger.info("Brak wierszy w %s. Uruchom najpierw python seed_schedule.py albo dodaj wiersze recznie.", csv_path)
        return

    due_rows = [r for r in rows if is_due(r, settings.timezone)]
    if not due_rows:
        logger.info("Brak wierszy do publikacji w tej chwili (sprawdzono %s, strefa %s).", csv_path, settings.timezone)
        return

    # Rozwiazuj PINTEREST_BOARD_ID tylko gdy faktycznie jest tego potrzeba w tym
    # uruchomieniu - wczesniej wywolywane bezwarunkowo przy KAZDYM starcie (co 15
    # min z Harmonogramu Zadan), co przy zablokowanej apce Pinteresta (trial
    # pending) zaśmiecalo publisher.log identycznym 401 co 15 minut na pusto,
    # skoro zaden wiersz i tak nie celuje w Pinterest jeszcze.
    if any("pinterest" in r.platform_list() for r in due_rows):
        settings = _ensure_pinterest_board(settings)

    for row in due_rows:
        logger.info("Wiersz %s: %s -> [%s], zaplanowane na %s", row.id, row.video_path, row.platforms, row.scheduled_at)
        if args.run_due:
            publish_row(row, settings, args.only)
            # zapis od razu po KAZDYM wierszu, nie dopiero po calej paczce - jesli
            # skrypt padnie w trakcie kolejnego wiersza, juz opublikowane wczesniej
            # w tym samym uruchomieniu nie zostana przypadkiem wyslane drugi raz
            save_rows(csv_path, rows)
            logger.info("Zapisano status wiersza %s w %s.", row.id, csv_path)

    if not args.run_due and not args.dry_run:
        logger.info("Nic nie zrobiono - podaj --dry-run (podglad) albo --run-due (publikacja).")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - ostatnia linia obrony, zeby cisza nie ukryla awarii
        logger.exception("Nieoczekiwany blad w main.py")
        _notify_windows("Social Video Publisher: awaria skryptu", str(exc)[:200])
        raise
