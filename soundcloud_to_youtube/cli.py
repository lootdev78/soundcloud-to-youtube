#!/usr/bin/env python3
"""SoundCloud -> YouTube Uploader

Features:
  * Termux-tauglicher OAuth (kein Browser noetig, Token wird gecached)
  * State pro URL in ~/.sc2yt/state.json
      -> bereits heruntergeladene Tracks werden NICHT neu geladen
      -> bereits konvertierte Videos werden NICHT neu erstellt
  * Bis zu 3 Retries pro Upload mit exponential backoff
  * `--retry-pending` arbeitet alle haengenden Uploads ab
"""

import argparse
import os
import sys
import time
import subprocess
from pathlib import Path

from mutagen.mp3 import EasyMP3
from mutagen.id3 import ID3, APIC

from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from . import state
from .config import load_config, get_work_dir
from .youtube_auth import build_youtube_client, verify_channel


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd, cwd=None, check=True):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        if check and result.returncode != 0:
            print("Fehler:", result.stderr)
            return None
        return result.stdout.strip()
    except FileNotFoundError:
        print(f"Tool nicht gefunden: {cmd[0]} (scdl / ffmpeg installieren)")
        return None


# ---------------------------------------------------------------------------
# Stage 1: Download
# ---------------------------------------------------------------------------

def download_track(url: str, config: dict, work_dir: str) -> str | None:
    print("Lade Track herunter...")
    work = Path(work_dir)
    # Snapshot: welche MP3s lagen vor dem scdl-Lauf schon im Ordner?
    before = {p.name for p in work.glob("*.mp3")}

    cmd = [
        "scdl", "-l", url,
        "--no-playlist",
        "--name-format", "{title}",
        # SoundCloud-Cover in voller Aufloesung (JPG) zusaetzlich speichern.
        # Fehlt das Flag, ist das Artwork nur im ID3-APIC-Frame der MP3
        # eingebettet - wir extrahieren es dann in download_cover().
        "--original-art",
    ]
    if config.get("scdl", {}).get("onlymp3"):
        cmd.append("--onlymp3")
    if run_cmd(cmd, cwd=work_dir) is None:
        return None

    after = {p.name for p in work.glob("*.mp3")}
    new_files = [work / n for n in (after - before)]

    if new_files:
        new_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(new_files[0])

    # Kein neuer Download (scdl uebersprungen / Cache). Versuche, eine
    # bereits vorhandene MP3 ueber den URL-Slug zuzuordnen, statt blind
    # die zeitlich neueste MP3 im Ordner zu nehmen.
    slug = url.rstrip("/").split("/")[-1].split("?", 1)[0].lower()
    if slug:
        # Slug "no-hesitation" -> matche "No Hesitation.mp3" etc.
        slug_loose = slug.replace("-", "").replace("_", "")
        matches = []
        for p in work.glob("*.mp3"):
            stem_loose = p.stem.lower().replace(" ", "").replace("-", "").replace("_", "")
            if slug_loose and slug_loose in stem_loose:
                matches.append(p)
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            print(f"scdl hat nichts Neues geladen, nutze vorhandene Datei: {matches[0].name}")
            return str(matches[0])

    print("Keine zum Track passende MP3 gefunden (scdl hat nichts heruntergeladen).")
    return None


# ---------------------------------------------------------------------------
# Stage 2: Metadata
# ---------------------------------------------------------------------------

def get_metadata_and_tags(mp3_file: str):
    title = "Unknown Title"
    artist = "Unknown Artist"
    genre = ""
    tags: list[str] = []
    try:
        audio = EasyMP3(mp3_file)
        title = str(audio.get("title", ["Unknown Title"])[0])
        artist = str(audio.get("artist", ["Unknown Artist"])[0])
        genre = str(audio.get("genre", [""])[0])
        try:
            full = ID3(mp3_file)
            for frame in full.values():
                if hasattr(frame, "text"):
                    for value in frame.text:
                        value = str(value).strip()
                        if not value:
                            continue
                        tags.extend(
                            t.strip() for t in value.split(",") if t.strip()
                        )
        except Exception:
            pass
        if genre:
            tags.insert(0, genre)
    except Exception:
        title = Path(mp3_file).stem

    tags = list(dict.fromkeys(t for t in tags if t))[:15]
    return title, artist, genre, tags


def download_cover(work_dir: str, mp3_file: str | None = None) -> str | None:
    """Findet das SoundCloud-Cover GENAU zum uebergebenen Track. Greift
    nur auf Dateien zurueck, die nachweislich zu dieser MP3 gehoeren
    (gleicher Stem) oder direkt aus deren APIC-Frame extrahiert werden.
    Generische cover/art-Globs gibt es nicht mehr - sie haben in der
    Vergangenheit Artwork anderer Tracks aus dem Work-Dir eingeschleppt."""
    if not mp3_file:
        return None

    work = Path(work_dir)
    stem = Path(mp3_file).stem

    # 1) Bilddatei mit gleichem Stem wie die MP3 (scdl --original-art)
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        cand = work / f"{stem}{ext}"
        if cand.exists():
            return str(cand)

    # 2) eingebettetes APIC-Frame aus DIESER MP3 extrahieren
    if os.path.exists(mp3_file):
        try:
            id3 = ID3(mp3_file)
            for frame in id3.values():
                if isinstance(frame, APIC):
                    mime = (frame.mime or "image/jpeg").lower()
                    ext = ".png" if "png" in mime else (
                        ".webp" if "webp" in mime else ".jpg"
                    )
                    out = work / f"{stem}_embedded{ext}"
                    with open(out, "wb") as f:
                        f.write(frame.data)
                    return str(out)
        except Exception as e:
            print(f"APIC-Extraktion fehlgeschlagen: {e}")

    return None


# ---------------------------------------------------------------------------
# Stage 3: Video
# ---------------------------------------------------------------------------

def create_video(audio_file: str, cover_file: str | None, config: dict,
                 output_path: str) -> str | None:
    print("Erstelle Video...")
    w = config.get("video", {}).get("width", 1280)
    h = config.get("video", {}).get("height", 720)

    if cover_file and os.path.exists(cover_file):
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", cover_file,
            "-i", audio_file,
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-vf",
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}",
            "-i", audio_file,
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            output_path,
        ]
    if run_cmd(cmd) is None:
        return None
    return output_path


# ---------------------------------------------------------------------------
# Stage 4: Upload mit Retry
# ---------------------------------------------------------------------------

_BAD_DESC_CHARS = {"<": "(", ">": ")"}


def _sanitize_for_youtube(text: str, max_len: int) -> str:
    """YouTube lehnt < und > komplett ab, ausserdem diverse Steuerzeichen
    und einige Unicode-Bereiche (Surrogates, Private Use, BiDi-Marks)."""
    if not text:
        return ""
    for bad, good in _BAD_DESC_CHARS.items():
        text = text.replace(bad, good)

    out = []
    for ch in text:
        cp = ord(ch)
        # erlaubte Whitespaces
        if ch in ("\n", "\t"):
            out.append(ch)
            continue
        # alles unter 0x20 raus (Steuerzeichen)
        if cp < 0x20:
            continue
        # 0x7F (DEL) raus
        if cp == 0x7F:
            continue
        # C1-Controls (0x80-0x9F) raus
        if 0x80 <= cp <= 0x9F:
            continue
        # Surrogates - duerfen in well-formed UTF-8 nicht vorkommen, sicherheitshalber
        if 0xD800 <= cp <= 0xDFFF:
            continue
        # BOM / ZeroWidth / BiDi-Marks - YouTube zickt damit gern
        if cp in (0xFEFF, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
                  0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                  0x2066, 0x2067, 0x2068, 0x2069):
            continue
        # Private Use Area
        if 0xE000 <= cp <= 0xF8FF:
            continue
        out.append(ch)

    cleaned = "".join(out).strip()
    return cleaned[:max_len]


def _sanitize_tag(tag: str) -> str | None:
    if not tag:
        return None
    cleaned = _sanitize_for_youtube(tag, 60)
    # YouTube-Tags: keine Anfuehrungszeichen
    cleaned = cleaned.replace('"', "'")
    return cleaned or None


def _do_upload(youtube, video_file: str, title: str, description: str,
               yt_tags: list, config: dict, debug: bool = False) -> str:
    """Ein einzelner Upload-Versuch. Wirft bei Fehler."""
    yt_cfg = config.get("youtube", {})
    privacy = yt_cfg.get("privacy_status", "private")
    base_tags = yt_cfg.get("tags", ["music"])

    safe_tags = []
    for t in list(dict.fromkeys(base_tags + yt_tags)):
        st = _sanitize_tag(t)
        if st:
            safe_tags.append(st)
    safe_tags = safe_tags[:30]

    safe_title = _sanitize_for_youtube(title, 100) or "Untitled"
    safe_desc = _sanitize_for_youtube(description, 4900)

    if debug:
        print("---- DEBUG: an YouTube gesendet ----")
        print(f"title  ({len(safe_title)} chars): {safe_title!r}")
        print(f"desc   ({len(safe_desc)} chars):")
        print(safe_desc)
        print(f"tags   ({len(safe_tags)}): {safe_tags!r}")
        print("------------------------------------")

    body = {
        "snippet": {
            "title": safe_title,
            "description": safe_desc,
            "tags": safe_tags,
            "categoryId": str(yt_cfg.get("category_id", "10")),
        },
        "status": {"privacyStatus": privacy},
    }

    media = MediaFileUpload(video_file, chunksize=1024 * 1024, resumable=True)
    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload: {int(status.progress() * 100)}%")
    video_id = response["id"]
    print(f"Hochgeladen! https://youtu.be/{video_id}")
    return video_id


def _do_update_metadata(youtube, video_id: str, title: str, description: str,
                        yt_tags: list, config: dict, debug: bool = False) -> str:
    """Aktualisiert Titel/Beschreibung/Tags/Privacy eines bereits hochgeladenen
    Videos via videos().update(). Lae(e)dt KEINE neue Datei hoch."""
    yt_cfg = config.get("youtube", {})
    privacy = yt_cfg.get("privacy_status", "private")
    base_tags = yt_cfg.get("tags", ["music"])

    safe_tags = []
    for t in list(dict.fromkeys(base_tags + yt_tags)):
        st = _sanitize_tag(t)
        if st:
            safe_tags.append(st)
    safe_tags = safe_tags[:30]

    safe_title = _sanitize_for_youtube(title, 100) or "Untitled"
    safe_desc = _sanitize_for_youtube(description, 4900)

    if debug:
        print("---- DEBUG: Metadaten-Update an YouTube ----")
        print(f"video_id: {video_id}")
        print(f"title  ({len(safe_title)} chars): {safe_title!r}")
        print(f"desc   ({len(safe_desc)} chars):")
        print(safe_desc)
        print(f"tags   ({len(safe_tags)}): {safe_tags!r}")
        print("--------------------------------------------")

    body = {
        "id": video_id,
        "snippet": {
            "title": safe_title,
            "description": safe_desc,
            "tags": safe_tags,
            "categoryId": str(yt_cfg.get("category_id", "10")),
        },
        "status": {"privacyStatus": privacy},
    }

    request = youtube.videos().update(
        part="snippet,status",
        body=body,
    )
    response = request.execute()
    print(f"Metadaten aktualisiert: https://youtu.be/{response['id']}")
    return response["id"]


def update_video_metadata(url: str, video_id: str, title: str, description: str,
                          yt_tags: list, config: dict, debug: bool = False) -> str | None:
    """Wrapper fuer Metadaten-Update mit denselben Retries wie Upload."""
    max_retries = int(config.get("upload", {}).get("max_retries", 3))
    base_delay = int(config.get("upload", {}).get("retry_delay_seconds", 5))

    youtube = build_youtube_client()
    required_channel = config.get("youtube", {}).get("channel_id") or None
    try:
        verify_channel(youtube, required_channel)
    except RuntimeError as e:
        print(f"Channel-Check fehlgeschlagen: {e}")
        state.upsert(url, last_error=str(e))
        return None

    last_err: str | None = None
    for attempt in range(1, max_retries + 1):
        print(f"Update-Versuch {attempt}/{max_retries}...")
        try:
            vid = _do_update_metadata(
                youtube, video_id, title, description, yt_tags, config, debug=debug
            )
            state.upsert(url, last_error=None)
            return vid
        except HttpError as e:
            last_err = f"HttpError {e.resp.status}: {e}"
            print(f"!!! {last_err}")
            if e.resp.status in (400, 404):
                print("Nicht-retryable Fehler - breche ab.")
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"!!! {last_err}")

        if attempt < max_retries:
            delay = base_delay * (2 ** (attempt - 1))
            print(f"Warte {delay}s vor naechstem Versuch...")
            time.sleep(delay)

    state.upsert(url, last_error=last_err)
    return None


def upload_with_retries(url: str, video_file: str, title: str, description: str,
                        yt_tags: list, config: dict, debug: bool = False) -> str | None:
    """Wrapper mit 3x Retry + exponential backoff + State-Updates."""
    max_retries = int(config.get("upload", {}).get("max_retries", 3))
    base_delay = int(config.get("upload", {}).get("retry_delay_seconds", 5))

    youtube = build_youtube_client()
    required_channel = config.get("youtube", {}).get("channel_id") or None
    try:
        verify_channel(youtube, required_channel)
    except RuntimeError as e:
        print(f"Channel-Check fehlgeschlagen: {e}")
        state.upsert(url, stage="failed", last_error=str(e))
        return None

    last_err: str | None = None
    done_attempts = 0
    for attempt in range(1, max_retries + 1):
        done_attempts = attempt
        print(f"Upload-Versuch {attempt}/{max_retries}...")
        try:
            video_id = _do_upload(
                youtube, video_file, title, description, yt_tags, config, debug=debug
            )
            state.upsert(
                url,
                stage="uploaded",
                youtube_id=video_id,
                youtube_url=f"https://youtu.be/{video_id}",
                last_error=None,
            )
            state.reset_attempts(url)
            return video_id
        except HttpError as e:
            last_err = f"HttpError {e.resp.status}: {e}"
            print(f"!!! {last_err}")
            # 400/404 sind nicht-retryable Client-Fehler (z.B. ungueltige Metadaten).
            # Bei 401/403/429 dagegen lohnt der Retry.
            if e.resp.status in (400, 404):
                print("Nicht-retryable Fehler - breche ab.")
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"!!! {last_err}")

        state.increment_attempts(url)
        state.upsert(url, stage="video_created", last_error=last_err)

        if attempt < max_retries:
            delay = base_delay * (2 ** (attempt - 1))
            print(f"Warte {delay}s vor naechstem Versuch...")
            time.sleep(delay)

    print(f"Upload final fehlgeschlagen nach {done_attempts} Versuch(en).")
    state.upsert(url, stage="failed", last_error=last_err)
    return None


# ---------------------------------------------------------------------------
# Pipeline (resume-fahig)
# ---------------------------------------------------------------------------

def process_url(url: str, config: dict, args) -> bool:
    work_dir = get_work_dir()
    entry = state.get(url) or {}

    # --- Stage 1: Download ---
    mp3 = entry.get("mp3")
    if mp3 and os.path.exists(mp3):
        print(f"MP3 bereits vorhanden, ueberspringe Download: {mp3}")
    else:
        mp3 = download_track(url, config, work_dir)
        if not mp3:
            state.upsert(url, stage="failed", last_error="Download fehlgeschlagen")
            return False
        state.upsert(url, mp3=mp3, stage="downloaded")

    # --- Stage 2: Metadata + Video ---
    title, artist, genre, sc_tags = get_metadata_and_tags(mp3)
    full_title = (args.title_prefix or "") + f"{title} - {artist}"

    video = entry.get("video")
    if video and os.path.exists(video):
        print(f"Video bereits vorhanden, ueberspringe Konvertierung: {video}")
    else:
        cover = download_cover(work_dir, mp3)
        if cover:
            print(f"Cover gefunden: {cover}")
        else:
            print("Kein Cover gefunden - Video wird mit schwarzem Hintergrund erstellt.")
        output_name = args.output or f"{Path(mp3).stem}.mp4"
        output_path = (
            output_name
            if os.path.isabs(output_name)
            else os.path.join(work_dir, output_name)
        )
        video = create_video(mp3, cover, config, output_path)
        if not video:
            state.upsert(url, stage="failed", last_error="Video-Erstellung fehlgeschlagen")
            return False
        state.upsert(
            url,
            video=video,
            stage="video_created",
            title=full_title,
            sc_tags=sc_tags,
            genre=genre,
        )

    if args.no_upload:
        print("--no-upload: stoppe vor Upload.")
        return True

    # --- Stage 3: Upload ---
    # SoundCloud-Link sauber aufbereiten (Tracking-Parameter wie ?in=...,
    # ?si=... oder utm_* abschneiden). Falls dabei nichts Brauchbares
    # uebrigbleibt, fallen wir auf die Roh-URL zurueck, damit der Link
    # in der Description nicht verschwindet.
    sc_link = (url or "").split("?", 1)[0].rstrip("/")
    if not sc_link.startswith(("http://", "https://")):
        sc_link = url or ""

    desc_lines = [
        f"{title}",
        f"by {artist}",
        "",
    ]
    if genre:
        desc_lines.append(f"Genre: {genre}")
        desc_lines.append("")
    desc_lines += [
        f"Original on SoundCloud: {sc_link}",
    ]
    if args.custom_tags:
        desc_lines.append("")
        desc_lines.append("#" + " #".join(t.lstrip("#") for t in args.custom_tags))
    description = "\n".join(desc_lines)

    yt_tags = sc_tags + (args.custom_tags or [])

    # --- Guard gegen Doppel-Upload + optionales Metadaten-Update ---
    existing_video_id = entry.get("youtube_id")
    already_uploaded = entry.get("stage") == "uploaded" and existing_video_id

    if already_uploaded and not getattr(args, "force_upload", False):
        if getattr(args, "update_metadata", False):
            print(f"Video bereits hochgeladen ({existing_video_id}) - aktualisiere nur Metadaten.")
            vid = update_video_metadata(
                url, existing_video_id, full_title, description, yt_tags, config,
                debug=args.debug,
            )
            return vid is not None
        print(f"Video bereits hochgeladen: {entry.get('youtube_url')}")
        print("Nutze --update-metadata zum Aktualisieren oder --force-upload fuer Neu-Upload.")
        return True

    video_id = upload_with_retries(
        url, video, full_title, description, yt_tags, config, debug=args.debug
    )
    if not video_id:
        return False

    if args.clean:
        for f in [mp3, video]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        print("Temporaere Dateien geloescht.")
    return True


def retry_pending(config: dict, debug: bool = False) -> None:
    items = state.pending()
    if not items:
        print("Keine ausstehenden Uploads.")
        return
    print(f"Arbeite {len(items)} ausstehende Uploads ab...")
    for entry in items:
        url = entry["url"]
        video = entry.get("video")
        title = entry.get("title") or Path(video).stem
        sc_tags = entry.get("sc_tags") or []
        genre = entry.get("genre") or ""
        sc_link = (url or "").split("?", 1)[0].rstrip("/")
        if not sc_link.startswith(("http://", "https://")):
            sc_link = url or ""
        desc_lines = [title, ""]
        if genre:
            desc_lines += [f"Genre: {genre}", ""]
        desc_lines += [f"Original on SoundCloud: {sc_link}"]
        description = "\n".join(desc_lines)
        print(f"\n>>> Retry: {url}")
        upload_with_retries(url, video, title, description, sc_tags, config, debug=debug)


def list_state() -> None:
    items = state.all_entries()
    if not items:
        print("State leer.")
        return
    for e in items:
        print(f"- {e.get('url')}")
        print(f"    stage    : {e.get('stage')}")
        print(f"    attempts : {e.get('attempts', 0)}")
        if e.get("youtube_url"):
            print(f"    youtube  : {e['youtube_url']}")
        if e.get("last_error"):
            print(f"    error    : {e['last_error']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="SoundCloud -> YouTube Uploader")
    parser.add_argument("url", nargs="?", help="SoundCloud Track URL")
    parser.add_argument("--privacy", choices=["public", "unlisted", "private"],
                        help="Sichtbarkeit (ueberschreibt config)")
    parser.add_argument("--category-id",
                        help="YouTube Kategorie-ID (ueberschreibt config, z.B. 10=Music)")
    parser.add_argument("--channel-id",
                        help="Ziel-Channel-ID (Brand Account, ueberschreibt config)")
    parser.add_argument("--yt-tags", nargs="*",
                        help="Basis-Tags fuer YouTube (ueberschreibt config.youtube.tags)")
    parser.add_argument("--width", type=int,
                        help="Video-Breite in px (ueberschreibt config)")
    parser.add_argument("--height", type=int,
                        help="Video-Hoehe in px (ueberschreibt config)")
    parser.add_argument("--onlymp3", dest="onlymp3", action="store_true", default=None,
                        help="scdl: nur MP3 herunterladen (ueberschreibt config)")
    parser.add_argument("--no-onlymp3", dest="onlymp3", action="store_false",
                        help="scdl: nicht auf MP3 beschraenken (ueberschreibt config)")
    parser.add_argument("--max-retries", type=int,
                        help="Max. Upload-Retries (ueberschreibt config)")
    parser.add_argument("--retry-delay", type=int,
                        help="Basisdelay zwischen Retries in Sek. (ueberschreibt config)")
    parser.add_argument("--config", action="store_true", help="Config anlegen")
    parser.add_argument("--login", action="store_true",
                        help="Nur OAuth-Login durchfuehren / Token erneuern")
    parser.add_argument("--no-upload", action="store_true",
                        help="Nur Video erstellen, nicht hochladen")
    parser.add_argument("--clean", action="store_true",
                        help="Dateien nach erfolgreichem Upload loeschen")
    parser.add_argument("--title-prefix", default="")
    parser.add_argument("--custom-tags", nargs="*")
    parser.add_argument("--output", default=None,
                        help="Video-Dateiname (default: <track>.mp4 im Work-Dir)")
    parser.add_argument("--retry-pending", action="store_true",
                        help="Alle haengenden Uploads erneut versuchen")
    parser.add_argument("--list-state", action="store_true",
                        help="Aktuellen State anzeigen")
    parser.add_argument("--forget", metavar="URL",
                        help="State-Eintrag fuer URL loeschen")
    parser.add_argument("--debug", action="store_true",
                        help="Sende-Body vor jedem Upload ausgeben")
    parser.add_argument("--update-metadata", action="store_true",
                        help="Wenn URL bereits hochgeladen: nur Titel/Beschreibung/Tags "
                             "auf YouTube aktualisieren (kein neuer Upload)")
    parser.add_argument("--force-upload", action="store_true",
                        help="Erzwingt erneuten Upload, auch wenn URL schon hochgeladen wurde")

    args = parser.parse_args()

    if args.config:
        from .config import ensure_config_dir
        ensure_config_dir()
        return

    config = load_config()

    # Per-Song-Overrides aus CLI auf die geladene Config anwenden.
    yt_cfg = config.setdefault("youtube", {})
    if args.privacy:
        yt_cfg["privacy_status"] = args.privacy
    if args.category_id:
        yt_cfg["category_id"] = args.category_id
    if args.channel_id is not None:
        yt_cfg["channel_id"] = args.channel_id
    if args.yt_tags is not None:
        yt_cfg["tags"] = args.yt_tags

    vid_cfg = config.setdefault("video", {})
    if args.width:
        vid_cfg["width"] = args.width
    if args.height:
        vid_cfg["height"] = args.height

    scdl_cfg = config.setdefault("scdl", {})
    if args.onlymp3 is not None:
        scdl_cfg["onlymp3"] = args.onlymp3

    up_cfg = config.setdefault("upload", {})
    if args.max_retries is not None:
        up_cfg["max_retries"] = args.max_retries
    if args.retry_delay is not None:
        up_cfg["retry_delay_seconds"] = args.retry_delay

    if args.login:
        yt = build_youtube_client()
        verify_channel(yt, config.get("youtube", {}).get("channel_id") or None)
        return

    if args.list_state:
        list_state()
        return

    if args.forget:
        state.remove(args.forget)
        print(f"Geloescht: {args.forget}")
        return

    if args.retry_pending:
        retry_pending(config, debug=args.debug)
        return

    if not args.url:
        parser.print_help()
        sys.exit(1)

    ok = process_url(args.url, config, args)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()