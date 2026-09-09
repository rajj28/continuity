# Voiceover script

Voice: **River** (ElevenLabs), stability 55, similarity 75, style 0.

## How this is built

The first version narrated one property per block while the picture showed one
panel: explain a beat, show a beat. Everything in it was true and none of it
went anywhere, because a list of good properties is not a story and there was
no subject moving through it.

This follows **one film through the pipeline, in order**. What goes in, what
the agents make, how it gets measured, who decides, what happens when it
breaks, and how the agents are marked. Each part is caused by the one before
it.

Two rules held throughout:

- **The first twenty seconds are the problem and what we built.** Nothing else.
  No features, no architecture, no numbers.
- **Name the thing before describing it.** The first cut said "a crew of
  agents that makes the whole thing" over a screen the viewer had had four
  seconds to read. "The whole thing" had no referent yet and the project had
  no name yet, so the one sentence carrying what was built said neither.
- **Never say what the picture already says.** If the screen shows the roster,
  the narration says why compliance has no repair tool -- not "watch the
  roster". Narration that describes the visual is the thing that makes a demo
  feel slow.

Numbers and units are spelled out because that is how they read correctly.

---

## VO 1 — the hook (over: board)

> A film does not ship once.
>
> It ships fifty times.
>
> Every territory wants its own dub, its own subtitles, its own described
> track, its own certificate, its own package.
>
> Any one of them can be wrong. All of them can be broken by a single change
> upstream.
>
> Today most of that is checked by hand, one file at a time. The checking
> takes longer than the making.
>
> So we built Continuity: a global release system. One film goes in, and a
> crew of agents prepares every market's release.
>
> And we gave them Grafana, to watch their own work.

## VO 2 — what the agents make (over: release, outputs, lineage)

> It starts with a video and a list of what is said in it.
>
> Drop those in, pick the markets, and the crew goes to work.
>
> Gemini rewrites every line to fit the gap it has to land in, and speaks it.
> The subtitles come from those same lines. Another agent watches the picture
> and describes it for people who cannot see it. Another writes the storefront
> copy.
>
> Then it is assembled into the file a platform actually receives. Picture,
> dubbed audio, a described track, subtitles.
>
> This is not a report about the work. It is the work — you can play it.

## VO 2b — listen to it (over: listen)

Not a narration block. `scripts/listen.py` builds `vo2b.mp3` by putting the
three cues below either side of the real audio files, so what plays under this
shot is the dub, and not a description of the dub. Every number on this screen
is a claim about a sound; this is the sound.

The cues are generated as VO 8, VO 9 and VO 10 and are never blocks of their
own.

## VO 2c — not a dubbing tool (over: dimensions)

Placed here on purpose. The film has just spent twenty-four seconds on audio,
which is the moment a viewer decides what kind of system this is, and the
answer is eight columns wide. Every figure named is on the screen while it is
said.

> This is not a dubbing tool.
>
> Across five markets: eighty-seven checks, in eight dimensions.
>
> Picture — resolution, frame rate, codec. Audio — loudness against the
> territory's own target, and true peak. Timed text — reading rate, and the
> on-screen signs a viewer has to see even with subtitles off.
>
> Rights, by territory and by window. Certification, by body and by cut. And
> the storefront record a viewer actually reads.
>
> Any one of them blocks a release. Each one says so by name.

## VO 5b — the ones nobody here can fix (over: compliance)

> Japan is blocked on four things, and not one of them is a file.
>
> Two rights — one never granted, one that does not open until the fifteenth.
> And a certificate nobody has submitted.

## VO 3 — measured, and judged (over: grafana_dash, grafana_verdict)

> None of it is taken on trust. F F M peg measures every piece — how far the
> dub drifts from the picture, how loud it is, whether the narration talks over
> the dialogue — and every number goes straight to Grafana.
>
> Grafana decides. Whether a market can ship is a recording rule: everything
> measured passed, everything owed was measured, nothing built from a stale
> source.
>
> The agents cannot write it. They read Grafana through a server started with
> disable-write, which hands them no write tools at all. Nothing here can talk
> itself green.

## VO 4 — when it breaks (over: coverage, grafana_rule)

> And a check nobody ran blocks just as hard as one that failed. Most
> monitoring goes quiet when it breaks. This goes red.
>
> Grafana re-evaluates every thirty seconds, and when a market fails, it calls
> the agents itself.

## VO 4b — the receiver answers (over: wakelog)

The one claim the film used to make without showing it. The alert rule page
proves a rule exists and is firing; it does not prove anything answered. This
is the deployed receiver's own log, read out of Cloud Logging by
`scripts/wakelog.py` rather than typed.

> Nobody presses anything. The rule fires, and the receiver's own log says
> which markets it woke the agents for.
>
> Three at once. And a second rule underneath, watching whether the last
> repair actually held.

## VO 5 — the crew (over: swarm)

> One specialist wakes for each thing that is broken. They work at the same
> time.
>
> Compliance is read-only.
>
> It was never handed a repair tool. An expired certificate is not a fault in a
> file, so it can only gather the facts and pass them to a person.
>
> Its limits are in what it can call. Not in what it was told.

## VO 5c — one gets fixed (over: repair)

> Brazil's dub is five decibels louder than its market allows.
>
> The audio specialist proposes a remix and, before it touches anything, says
> where the number will land. Minus eighteen point seven eight, to at or below
> minus twenty four.
>
> The localisation specialist looks at the same market and refuses. The drift
> is not systematic, so a retime would not fix it — and it says so, instead of
> trying.
>
> Approve one, and it runs on the real audio. Then the same probe measures the
> result.
>
> This one landed at minus twenty three point three two. Better than it was,
> and short of what it promised.
>
> So it is recorded as a failure.

## VO 6 — marked on being right (over: repairs, autonomy, grafana_agent)

> Improving a number is not the same as being right. Getting away with it is
> marked lucky, and lucky counts against the strategy rather than for it.
>
> A repair earns the authority to run without asking after three verified
> successes, and loses it again the moment the record stops holding.
>
> And the agents are not merely watched by this stack. They are watched in the
> same stack they read their instructions from.

## VO 6b — the wiring (over: architecture)

> Underneath: F F M peg measures, OpenTelemetry carries it, Prometheus stores
> it, and a Mimir recording rule decides.
>
> Gemini adapts and speaks every line. The Agent Development Kit runs the
> specialists. Two Cloud Run services share one image and one digest, so a
> repair started by an alert and a repair approved by a person are the same
> code.

## VO 7 — close (over: unrepairable, close)

> Two markets are clear.
>
> Three are not — and each can tell you exactly what it is waiting for.
>
> Nobody checked any of this by hand.
>
> A film ships fifty times. A series ships fifty times an episode, every week.
> A live event ships while it is still going out.
>
> Same loop. Same gate. Same evidence.

---

## The listen cues

Three short reads, spoken either side of the real files. Kept short on purpose:
the point of the block is the audio underneath them, and a sentence talking
over a dub is the exact failure this project measures.

## VO 8

> The original.

## VO 9

> The German dub. Same picture, same gaps.

## VO 10

> And the description track — written into the silences between lines.

---

## Notes for generation

- One file per block, `vo1.mp3` … `vo7.mp3`. A bad read costs one block.
- Do not "tidy" the spelled-out numbers back into digits.
- `F F M peg` is deliberate.
