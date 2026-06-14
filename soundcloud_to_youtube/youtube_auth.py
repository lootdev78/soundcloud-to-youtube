"""YouTube OAuth - Termux/headless tauglich.

Probleme der urspruenglichen Version:
  flow.run_local_server(port=0)
laesst webbrowser.open() laufen -> auf Termux gibt es keinen Browser
-> "could not locate runnable browser".

Loesung:
  1. Token persistent in ~/.sc2yt/token.json speichern (nur 1x einloggen).
  2. Beim ersten Mal: lokalen Server starten, Browser NICHT automatisch oeffnen,
     URL ausgeben - User oeffnet sie manuell (auch im Mobil-Browser auf demselben
     Geraet -> Redirect auf 127.0.0.1:PORT klappt in Termux).
  3. Falls trotzdem unmoeglich (echtes headless): manueller Console-Flow mit
     redirect_uri=urn:ietf:wg:oauth:2.0:oob als Fallback - User kopiert den
     Auth-Code von Hand.
  4. channel_id-Pruefung: falls in der Config gesetzt wird verifiziert, dass
     der eingeloggte Account Zugriff auf genau diesen Kanal hat
     (onBehalfOfContentOwner ist nur fuer Partner-API; fuer normale Brand
     Accounts muss man sich beim OAuth-Consent mit dem richtigen Brand Account
     anmelden - die Pruefung warnt sonst).
"""

import json
import os
import socket
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import get_client_secrets_path, get_token_path

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _load_cached_credentials() -> Credentials | None:
    path = get_token_path()
    if not os.path.exists(path):
        return None
    try:
        creds = Credentials.from_authorized_user_file(path, SCOPES)
    except (ValueError, json.JSONDecodeError):
        return None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)
        except Exception as e:
            print(f"!!! Token-Refresh fehlgeschlagen: {e}")
            return None
    if creds and creds.valid:
        return creds
    return None


def _save_credentials(creds: Credentials) -> None:
    with open(get_token_path(), "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def _interactive_login() -> Credentials:
    secrets = get_client_secrets_path()
    if not os.path.exists(secrets):
        raise FileNotFoundError(
            f"client_secrets.json fehlt: {secrets}\n"
            "Lege die OAuth-Client-Datei (Typ: Desktop App) aus der "
            "Google Cloud Console dort ab."
        )

    flow = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)

    # Versuch 1: lokaler Loopback-Server OHNE automatisch Browser zu oeffnen.
    # Funktioniert auf Termux, wenn der User die URL manuell im Browser oeffnet.
    try:
        port = _free_port()
        print("\n=== YouTube Login ===")
        print(f"Starte lokalen Auth-Server auf Port {port}.")
        print("Oeffne die folgende URL im Browser (auf demselben Geraet oder")
        print("kopiere sie auf einen PC und logge dich mit dem YouTube-Kanal ein):\n")
        creds = flow.run_local_server(
            host="127.0.0.1",
            port=port,
            open_browser=False,
            authorization_prompt_message="--> {url}\n",
            success_message="Login OK. Du kannst diesen Tab schliessen.",
        )
        _save_credentials(creds)
        return creds
    except Exception as e:
        print(f"!!! Lokaler Server-Flow fehlgeschlagen ({e}). Wechsle zu manuellem Flow...")

    # Versuch 2: manueller Code-Flow (Out-of-Band) als letzter Rettungsanker.
    flow = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print("\nOeffne diese URL im Browser:\n")
    print(auth_url)
    print()
    code = input("Code aus dem Browser einfuegen: ").strip()
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_credentials(creds)
    return creds


def get_credentials() -> Credentials:
    creds = _load_cached_credentials()
    if creds:
        return creds
    return _interactive_login()


def build_youtube_client():
    creds = get_credentials()
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def verify_channel(youtube, required_channel_id: str | None = None) -> dict:
    """Liefert Info ueber den eingeloggten Kanal. Warnt wenn falscher Kanal."""
    resp = youtube.channels().list(part="snippet,id", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(
            "Kein YouTube-Kanal mit diesem Login verbunden. "
            "Logge dich mit dem Google-Account ein, der den Zielkanal besitzt. "
            "Loesche ~/.sc2yt/token.json um neu einzuloggen."
        )
    channel = items[0]
    cid = channel["id"]
    cname = channel["snippet"]["title"]
    print(f"Eingeloggt als Kanal: {cname} ({cid})")
    if required_channel_id and required_channel_id != cid:
        raise RuntimeError(
            f"Falscher Kanal! In der Config steht channel_id={required_channel_id}, "
            f"eingeloggt ist aber {cid}. Loesche ~/.sc2yt/token.json und logge "
            f"dich erneut ein - achte beim Google-Login darauf den richtigen "
            f"Brand-Account auszuwaehlen."
        )
    return channel
