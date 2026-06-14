# soundcloud-to-youtube (`sc2yt`)

> Download a SoundCloud track, turn it into a YouTube-ready MP4 and upload it with cover art, metadata and tags â€” with a per-URL resumable state, retry logic and Termux-friendly OAuth.

[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Status](https://img.shields.io/badge/status-stable-brightgreen.svg)]()
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Termux-lightgrey.svg)]()

---

## Features

- **Four-stage pipeline**: `download â†’ metadata â†’ video â†’ upload`, every stage independently resumable.
- **Persistent state** per SoundCloud URL in `~/.sc2yt/state.json`
  - already-downloaded tracks are **not** fetched again
  - already-encoded videos are **not** re-rendered
  - already-uploaded tracks are **not** posted twice
- **Cover handling**: prefers `scdl --original-art`, falls back to the embedded `APIC` frame of the MP3, otherwise a black still.
- **Robust upload**: resumable upload in 1 MiB chunks, up to *n* retries with exponential backoff, 400/404 correctly treated as non-retryable.
- **YouTube sanitizer**: strips control chars, BiDi marks, surrogates, the Private Use Area and replaces forbidden `<`/`>` ” so `videos.insert` does not fail on Unicode garbage.
- **Brand-account guard**: optional `channel_id` check prevents accidental uploads to the wrong channel.
- **Metadata update** without re-upload via `videos().update()` (`--update-metadata`).
- **Batch recovery**: `--retry-pending` works through every pending upload in the state.
- **Termux-friendly**: OAuth without a browser, token is cached.

---

## Installation

### Requirements

- Python 3.8
- [`ffmpeg`](https://ffmpeg.org/) on `PATH`
- [`scdl`](https://github.com/flyingrub/scdl) (installed as a pip dependency, can also be updated separately)
- A Google Cloud project with the **YouTube Data API v3** enabled and a `client_secrets.json` (OAuth client of type *Desktop*)

### Install from source

```bash
git clone https://github.com/<user>/sc2yt.git
cd sc2yt
pip install .
```

Afterwards the `soundcloud-to-youtube` CLI is available globally.

---

## First-time setup

```bash
# create config + directories
soundcloud-to-youtube --config

# drop your client_secrets.json from Google Cloud Console here:
#   ~/.sc2yt/client_secrets.json

# run the OAuth login (token is cached in ~/.sc2yt/token.json)
soundcloud-to-youtube --login
```

The first `--login` runs the OAuth flow. On headless systems / Termux this works without a browser via the console.

### Config file `~/.sc2yt/config.json`

```json
{
  "youtube": {
    "privacy_status": "private",
    "category_id": "10",
    "tags": ["soundcloud", "music"],
    "channel_id": ""
  },
  "scdl":   { "onlymp3": true },
  "video":  { "width": 1280, "height": 720 },
  "upload": { "max_retries": 3, "retry_delay_seconds": 5 }
}
```

| Key | Meaning |
|---|---|
| `youtube.privacy_status` | `public` \| `unlisted` \| `private` |
| `youtube.category_id`    | YouTube category (e.g. `10` = Music) |
| `youtube.channel_id`     | Optional. Forces the target channel (Brand Account from YT Studio ’ Settings ’ Channel ' Advanced). |
| `scdl.onlymp3`           | only download MP3 instead of the original format |
| `video.width`/`height`   | resolution of the generated MP4 |
| `upload.max_retries`     | number of upload attempts per track |
| `upload.retry_delay_seconds` | base delay for exponential backoff |

---

## Usage

### Single track

```bash
soundcloud-to-youtube "https://soundcloud.com/<artist>/<track>"
```

The pipeline runs stage by stage; on abort (crash, power, network) just run the same command again â€” the state skips the already-completed steps.

### Common options

```bash
# override visibility
soundcloud-to-youtube <url> --privacy unlisted

# force a specific brand channel
soundcloud-to-youtube <url> --channel-id UCxxxxxxxxxxxxxxxxx

# custom tags + title prefix
soundcloud-to-youtube <url> --title-prefix "[Mix] " --custom-tags techno set 2024

# only build the video, do not upload
soundcloud-to-youtube <url> --no-upload

# delete MP3 + MP4 after a successful upload
soundcloud-to-youtube <url> --clean

# log the request body before every upload
soundcloud-to-youtube <url> --debug
```

### Managing the state

```bash
# show the state
soundcloud-to-youtube --list-state

# retry every pending upload
soundcloud-to-youtube --retry-pending

# remove an entry
soundcloud-to-youtube --forget "https://soundcloud.com/<artist>/<track>"
```

### Update metadata afterwards (no re-upload)

```bash
soundcloud-to-youtube <url> \
  --update-metadata \
  --title-prefix "[Remaster] " \
  --custom-tags new tag set
```

### Force a re-upload

```bash
soundcloud-to-youtube <url> --force-upload
```

---

## State schema

`~/.sc2yt/state.json` is a dict `{ url: entry }`. Each entry contains:

| Field         | Type     | Meaning |
|---------------|----------|---------|
| `url`         | string   | SoundCloud URL (primary key) |
| `stage`       | string   | `downloaded` \| `video_created` \| `uploaded` \| `failed` |
| `mp3`         | string   | path to the downloaded MP3 |
| `video`       | string   | path to the generated MP4 |
| `title`       | string   | final YouTube title incl. prefix |
| `sc_tags`     | string[] | tags extracted from ID3 |
| `genre`       | string   | ID3 genre |
| `youtube_id`  | string   | YouTube video id after upload |
| `youtube_url` | string   | `https://youtu.be/<id>` |
| `attempts`    | int      | number of upload attempts so far |
| `last_error`  | string   | last error message (or `null`) |

Writes are atomic (`tmp` + `os.replace`).

---

## Project layout

```
sc2yt/
├── setup.py
└── soundcloud_to_youtube/
    ├── __init__.py
    ├── cli.py            # pipeline + CLI entry (main)
    ├── config.py         # \~/.sc2yt layout, default config
    ├── state.py          # JSON state, atomic writes
    └── youtube_auth.py   # OAuth, token cache, channel verify
```

---

## CLI reference (short)

| Flag | Effect |
|---|---|
| `--config` | create config + directories |
| `--login` | only run OAuth login / refresh token |
| `--no-upload` | only build the video |
| `--clean` | delete files after upload |
| `--privacy {public,unlisted,private}` | override visibility |
| `--category-id <n>` | override YouTube category |
| `--channel-id <id>` | force target channel |
| `--yt-tags ...` | override base tags |
| `--custom-tags ...` | extra tags + hashtags in description |
| `--title-prefix <s>` | prefix in front of title |
| `--output <name>` | video filename |
| `--width <n>` / `--height <n>` | video resolution |
| `--onlymp3` / `--no-onlymp3` | control scdl format |
| `--max-retries <n>` | upload retries |
| `--retry-delay <s>` | base delay for backoff |
| `--retry-pending` | work through every pending upload |
| `--update-metadata` | only update metadata on YT |
| `--force-upload` | bypass the double-upload guard |
| `--list-state` | show the state |
| `--forget <url>` | remove a state entry |
| `--debug` | log request body before upload |

---

## Troubleshooting

- **`Tool nicht gefunden: scdl / ffmpeg`** â€” both binaries must be on `PATH`.
- **`Channel-Check fehlgeschlagen`** â€” `youtube.channel_id` points to a different account than the OAuth token; either regenerate the token with the correct account (`--login`) or fix `channel_id`.
- **`HttpError 400`** on upload â€” usually invalid metadata (special characters). `--debug` shows what was sent; the sanitizer catches most things, exotic code points possibly not.
- **`HttpError 403 quotaExceeded`** â€” daily YouTube API quota exhausted, retry the next day.
- **scdl downloads nothing new** â€” the tool uses the URL slug as a fallback to map an already-existing MP3 to the same track.

---

## Legal

This tool is intended for managing **your own** tracks or material you hold the rights to. Re-uploading other people's music to YouTube without permission violates copyright as well as the ToS of SoundCloud and YouTube. The responsibility lies with the user.

---

## Author

Psylooo
