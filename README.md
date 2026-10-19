# nicocli

A text-only niconico douga (ニコニコ動画) viewer for the terminal. Python standard library only.

```
python3 nicocli.py          # main menu
python3 nicocli.py sm9      # open a video directly
python3 nicocli.py 千本桜    # keyword search
```

## Features

- **Ranking** by genre and term (`t` cycles hourly / 24h / weekly / monthly / all time)
- **Search** by keyword or exact tag, with `s` to change the sort order
- **Video page** with stats, tags, description; jump to a tag (`t`) or the uploader's videos (`u`)
- **Danmaku player**: comments scroll across the terminal on the video's timeline, with
  ue/shita positions, colours and multi-line comment art
- **Comment list** with filter, sortable by video time or post date
- **Watch history**, saved to `~/.local/share/nicocli/history.json`
- **Optional playback** with mpv + yt-dlp: `p` plays in an mpv window with the danmaku synced
  to it, `T` plays the video as text art inside the terminal

## Player keys

| Key | Action |
| --- | --- |
| space | pause / resume |
| ← → | seek 5 s |
| ↑ ↓ | seek 30 s |
| 0–9 | jump to 0–90% |
| `[` `]` | slower / faster |
| `e` | toggle easy comments |
| `q` | back |

## Limits

Everything runs as a guest, so videos that need a login (paid or members-only channel videos)
and videos restricted to Japan can't be opened, and only the most recent comments are fetched.
