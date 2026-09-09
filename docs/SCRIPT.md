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
> So we built a crew of agents that makes the whole thing.
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
> Nobody sits watching this. Grafana re-evaluates every thirty seconds, and
> when a market fails, it calls the agents itself.

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

## VO 6 — marked on being right (over: repairs, autonomy, grafana_agent)

> Before an agent touches anything, it has to say what will happen. Which
> measurement, which direction, past which number.
>
> This repair worked. Three hundred and seventy five milliseconds, down to
> seventeen. It is still not marked a success — it said the drift would pass
> zero and it stopped short, so it is marked lucky, and lucky counts against
> it.
>
> And these failures are real. The fix went the wrong way and made it worse.
> Every one was caught before anything shipped.

## VO 7 — close (over: unrepairable, close)

> Two markets are clear.
>
> Three are not — and each can tell you exactly what it is waiting for.
>
> Nobody checked any of this by hand.

---

## Notes for generation

- One file per block, `vo1.mp3` … `vo7.mp3`. A bad read costs one block.
- Do not "tidy" the spelled-out numbers back into digits.
- `F F M peg` is deliberate.
