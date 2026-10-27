# Tapedeck

Save YouTube audio and video on your Mac. No ads, no pop-ups, nothing uploaded:
Tapedeck runs on your own computer and opens in your browser.

**Website:** <https://songbachhoang.github.io/tapedeck/>

- **One click from YouTube.** Drag the ⬇ Tapedeck button to your bookmarks bar, then click it on any video.
- **Best quality.** MP3, M4A, or the original audio with no re-encoding. Video as MP4 (1080p, plays anywhere) or up to 4K. Cover art and metadata are embedded.
- **Playlists, your pick.** Paste a playlist and tick the tracks you want. They come back as one .zip, numbered in order.
- **Fast.** Three tracks download at once.
- **Keeps working.** Tapedeck keeps its own copy of [yt-dlp](https://github.com/yt-dlp/yt-dlp) and updates it daily, so YouTube changes don't break it.
- **Cut sponsor segments** with [SponsorBlock](https://sponsor.ajay.app), if you want.

## Install

With [Homebrew](https://brew.sh):

```bash
brew install songbachhoang/tap/tapedeck
tapedeck
```

To keep Tapedeck running in the background, so the one-click button always works:

```bash
brew services start tapedeck
```

Without Homebrew, you need Python 3.9+, [ffmpeg](https://ffmpeg.org) and, for YouTube, [Deno](https://deno.com):

```bash
git clone https://github.com/songbachhoang/tapedeck
python3 tapedeck/tapedeck.py
```

Tapedeck opens at <http://127.0.0.1:8765>. The first launch takes about a minute while it sets up yt-dlp.

## How it works

`tapedeck.py` is a small web server using only the Python standard library. It listens on
127.0.0.1, so nothing else on your network can reach it. Downloads are done by yt-dlp and
ffmpeg on your machine; files go to a temporary folder until your browser saves them, and the
folder is removed when Tapedeck stops.

Tapedeck's own yt-dlp lives in `~/Library/Application Support/Tapedeck/yt-dlp-env` (on Linux,
`~/.local/share/tapedeck`). It is checked for updates at launch and every few hours after.

The one-click bookmark carries a random token stored in that same folder. Links opened
by any other website don't have it, so they can fill in the link box but can't start a download.

## Use it responsibly

Only download videos you own, that are Creative Commons or public domain, or that you have
permission to download. Downloading may be against YouTube's Terms of Service.
Tapedeck is not affiliated with YouTube or Google.

## License

MIT
