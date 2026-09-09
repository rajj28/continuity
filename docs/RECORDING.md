# How to record the three minutes

The whole trick: **do not narrate live.** Record silent screen clips, write the
voiceover separately, generate it, then lay it under the clips. A fluffed
sentence then costs you thirty seconds instead of the whole take.

Order of work: record clips → generate voiceover → assemble → captions → export.

---

## Tools (all free, all Windows)

| Job | Use | Why |
|---|---|---|
| Screen recording | **OBS Studio** (obsproject.com) | Free, 1080p60, records a chosen window cleanly |
| — or, zero install | **Xbox Game Bar** (`Win`+`G`) | Already on your machine. Records one window. Fine if OBS feels like too much |
| Editing | **Clipchamp** | Already installed on Windows 11. Import, trim, add audio, auto-captions, export |
| — or | **CapCut Desktop** | Slightly nicer captions, small download |
| Voiceover | **ElevenLabs** free tier | 10,000 characters/month. This script is 2,473 — a quarter of it |
| Playing the deliverable | **VLC** | Shows the audio-track menu, which is the point of that shot |

OBS settings that matter, and nothing else: Output → Recording Quality
**High**, Format **MP4**. Video → Base and Output resolution both
**1920×1080**, **30fps**. Sources → **Window Capture**, pick Chrome.

---

## Before you press record

Five minutes here saves an hour later.

- **Set display scaling to 100%** (Settings → Display → Scale). At 125% the
  recording is soft and everything is oversized.
- Chrome: **new window, no other tabs**, hide the bookmarks bar
  (`Ctrl`+`Shift`+`B`), press **F11** for full screen.
- Browser zoom to **110%** so the matrix fills the frame. `Ctrl` + `+`.
- Set Grafana to its **dark theme** so it matches the control room.
- Terminal: dark background, font size up to ~16pt, window about half screen.
- Sign in to the control room first, with the demo token, so you are not
  filming yourself typing it.

---

## The eleven clips

Record each one separately. Silent. Don't talk. If a clip goes wrong, redo
just that clip. Aim for the durations shown — a little over is fine, you trim.

| # | Duration | What to record |
|---|---|---|
| 1 | 20s | Control room top: header counts, then slowly scroll the matrix |
| 2 | 15s | Click **de-DE** (green). Detail panel: the three verdict factors and the PromQL under each |
| 3 | 10s | Click **Open in Grafana ↗**. Let the Media QC dashboard load |
| 4 | 12s | Grafana → Alerting → Alert rules. Show **MarketNotReleaseReady** in **Firing**, expand to show the three markets |
| 5 | 12s | Terminal: run the log command below. Let the `accepted …` lines land |
| 6 | 35s | Control room: click **ja-JP**, press **Investigate now**. Let the roster and the specialists stream |
| 7 | 12s | Scroll to **Earned autonomy**: `2 understood · 1 lucky · 3 failed` |
| 8 | 12s | Scroll to **Recent repairs**: the LUCKY and FAILED tags |
| 9 | 10s | Back to the matrix. Hover a **hatched** pip on Rights or Certification |
| 10 | 15s | VLC playing `out/deliverables/de-DE/SINTEL_S03_de-DE.mp4`, open **Audio → Audio Track** to show the three tracks |
| 11 | 8s | Control room board, resting on **2 READY · 3 BLOCKED** |

The command for clip 5:

```
gcloud run services logs read continuity-wake --project=grafana-508011 --region=asia-south1 --limit=15
```

Grafana re-delivers about every 20 minutes, so there is always a recent
`accepted MarketNotReleaseReady[firing] SINTEL/ja-JP -> investigate` in there.
If you want it to land live on camera, start recording just after the hour or
the twenty-minute mark and wait.

---

## The voiceover script

**Paste each block into ElevenLabs separately** and save as `vo1.mp3` …
`vo4.mp3`. Separate files sync far more easily than one long one.

Numbers and units are already spelled out as words — that is deliberate, it is
the single biggest cause of bad AI narration. Do not "tidy" them back into
digits.

### VO 1 — over clips 1–2 (about 46 seconds)

> A film doesn't ship to the world once. It ships fifty times.
>
> Germany needs a German dub, German subtitles, an age rating, an audio
> description track, and a package built to one platform's exact spec. Japan
> needs all of that again, differently.
>
> Every one of those can be wrong. And every one can be made wrong again by a
> change upstream.
>
> The hard part isn't making them. It's knowing which of them are good right
> now.
>
> This is Continuity. Five markets, eighty-seven checks. Not one of these
> numbers came from a model — every one came out of F F M peg. And the verdict
> on the right isn't this screen's opinion.

### VO 2 — over clips 3–5 (about 40 seconds)

> It's a Grafana recording rule. Can we ship equals: everything measured
> passed, times everything we owed was measured, times nothing is stale.
>
> The agents read Grafana through an M C P server started with disable-write.
> It registers zero write tools. Nothing here can talk itself green — it would
> have to write to Grafana, and it holds no credential that can.
>
> And when a market goes red, Grafana calls the agents. Not the other way
> round. This alert is firing right now. It posts to a Cloud Run receiver —
> and here is that receiver accepting it.

### VO 3 — over clips 6–8 (about 52 seconds)

> One specialist wakes per failing dimension, and they work in parallel.
>
> Watch the roster. Packaging. Compliance. And compliance is marked read-only —
> it was never handed a repair tool. A right that was never cleared is not a
> defect in a file, so that agent has no mechanism for pretending otherwise.
>
> Every agent has to predict what its repair will do. Which series, which
> direction, past which value. Then the measurement decides.
>
> This one worked. Three hundred and seventy five milliseconds, down to
> seventeen. It is not recorded as a success. It predicted the drift would pass
> zero and it landed at seventeen — so it's recorded lucky. Lucky raises the
> denominator and not the numerator. You cannot earn trust here by getting away
> with things.

### VO 4 — over clips 9–11 (about 42 seconds)

> And those three failures are real. The sync measurement is a magnitude — it
> says how far off the dub is, never which way — so the repair shifted the
> wrong direction and made it worse. Verification caught every one. Nothing
> shipped.
>
> That's the difference between a system that measures its own work, and one
> that reports what it intended to do.
>
> The hatched checks are the ones nobody can fix. An expired certificate isn't
> a defect in a file.
>
> Two markets can ship. This is what Germany actually receives. Three cannot —
> and every one of them tells you exactly what it's waiting for.

432 words, 2,473 characters. At the pace ElevenLabs actually reads (about 150
words a minute) that lands near **2 minutes 50**, and the clip budget above adds
to 3:00 — so you have roughly ten seconds of slack.

Three minutes is a hard limit. Check the exported runtime before you upload, and
if it creeps over, cut the second sentence of VO 4 rather than speeding anything
up.

---

## ElevenLabs settings

- Voice: pick a calm, mid-range narrator. **Adam**, **Daniel** or **Rachel**.
  Avoid anything described as "excited" or "narrative trailer" — the rules
  explicitly say this is not a cinematic trailer.
- **Stability 55%**, **Similarity 75%**, Style 0%. Higher stability stops it
  putting odd emphasis on technical words.
- Generate each block separately, listen once, regenerate if a term sounds
  wrong.
- If something still reads badly, spell it differently rather than fighting it:
  `PromQL` → `Prom Q L`, `LUFS` → `loofs`, `ja-JP` → `Japan`, `ms` →
  `milliseconds`.

---

## Assembling in Clipchamp

1. Open Clipchamp → **Create a new video**.
2. Drag in all eleven clips, in order, on the video track.
3. Drag `vo1`–`vo4` onto the audio track underneath.
4. Trim each clip so it ends when its narration does. **Let the picture follow
   the voice**, not the other way round — stretch a clip by slowing it slightly
   rather than rushing the words.
5. Add three or four text overlays only, big and brief:
   - `Grafana computes the verdict`
   - `--disable-write: zero write tools`
   - `predicted ≤ 0 · measured 17 ms · recorded LUCKY`
   - `compliance has no repair tool`
6. **Captions**: Clipchamp → Audio → **Auto-captions**. Turn them on and skim
   for mangled technical words. The rules require English or English subtitles,
   so this is worth the five minutes.
7. Export **1080p**.

---

## Things that quietly ruin a demo

- **Don't speed up the agent stream.** Its actual pace is the proof it is
  really running. If it feels slow, cut to the autonomy panel and back.
- **Don't hide the failures.** The three `failed` repairs are the strongest
  thirty seconds in the video for this audience.
- **Don't zoom past the PromQL.** Hold on it for two seconds. Being able to
  check a number is the whole argument.
- **Don't show the operator token on screen.** Sign in before you record.
- Watch it once at full size before uploading. Then upload to YouTube as
  **Public** or **Unlisted** — not Private, or the judges cannot open it.

## The shot list as it stands

Thirteen blocks of narration, seventeen shots, 5:30. Regenerate the whole film
with:

```bash
python scripts/wakelog.py        # the receiver's log, read from Cloud Logging
python scripts/voice.py          # narration; existing takes are kept
python scripts/listen.py         # the block whose soundtrack is the product
python scripts/record.py --clip all
python scripts/assemble.py
```

| Block | Shots | What it has to establish |
|---|---|---|
| vo1 | board | A film ships fifty times; this is Continuity; the checking costs more than the making |
| vo2 | release, lineage, outputs | A master goes in and the seven stages run |
| vo2b | listen | The dub, heard rather than described |
| vo2c | dimensions | Not a dubbing tool — eight columns, named one at a time |
| vo3 | verdict, grafana_dash, grafana_verdict | Grafana decides, and nothing can talk it green |
| vo4 | coverage, grafana_rule | A check nobody ran blocks as hard as one that failed |
| vo4b | wakelog | The receiver answering, in its own log |
| vo5 | swarm | One specialist per failing dimension, in parallel |
| vo5c | repair_ask, repair_done | A prediction, an approval, and a failure recorded |
| vo5b | compliance | The dimensions nothing here can repair |
| vo6 | repairs, autonomy, grafana_agent | Lucky counts against; authority is earned |
| vo6b | architecture | The wiring, after every part of it has been seen working |
| vo7 | close, endcard | Where it goes next, and where to find it |

### Things that cost a re-shoot, so they are written down

- **The sticky header covers a scrolled-to heading.** `scrollIntoView` puts an
  element at the top of the viewport and the 56px header sits on top of it.
  Every shot that scrolls to a heading follows it with `scrollBy(0, -72)`.
- **The detail pane is a skeleton for forty seconds** when the control room
  runs locally — one round trip per figure to Grafana Cloud, which is fast from
  Cloud Run in the same region and is not fast from a laptop in India. Any shot
  framing something *below* the detail pane has to wait for `#measured` first,
  or it measures the page at the wrong height.
- **A group is trimmed from its end.** Clips are cut long on purpose, so the
  last shot in a block is the one that loses seconds. Put nothing there that
  the film cannot afford to lose — the end card was silently cut off entirely
  the first time it was added behind two other shots.
- **`play()` in the same tick as a `currentTime` that has not resolved does
  nothing, silently.** The third transport in the listen block seeked and
  stopped. Nudge it again half a second later.
- **Class names collide.** `.mk` already meant a 9px status square, so reusing
  it on the build rail painted a green block over every market name.
