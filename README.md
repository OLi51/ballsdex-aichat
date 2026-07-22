# ballsdex-aichat

A chat companion for [Ballsdex](https://github.com/Ballsdex-Team/BallsDex-DiscordBot) v3, powered by
Google's Gemini API. Mention the bot or DM it to talk, or use `/chat`. It's not a dumb chatbot — it can
look up the player's own collection (and, if you allow it, collectible stats, search, and artwork)
through function calling, so it answers from your bot's real data instead of making things up.

## Features

- **Talk naturally** — mention the bot, DM it, or run `/chat`. `/forget` clears a channel's memory.
- **Knows your bot** — the bot's name and collectible name (and your `/about` description, if you've
  customized it) are baked into every request, so the AI stays in character as *your* bot.
- **Reads live game data** — the speaker's own collection is always available (it can never read
  anyone else's), at two levels of detail the AI picks between:
  - *Overview* — one entry per kind of collectible, with how many copies, rarity, attack/health and
    when it was last caught, plus overall totals and completion percentage.
  - *Individual copies* — one entry per specific collectible, with its own ID, its personal
    attack/health including that copy's bonus roll, its special and that special's emoji, plus
    favorite and tradeable flags.

  Either can be ranked by whatever the question calls for (rarest, most owned, strongest, best
  roll, most recent…) and filtered by name, specials-only or favorites-only — so "what's my
  rarest?", "what do I have most of?" and "what's my best-rolled one?" are all answered from real
  data. Collectible stats/search and artwork are separate, **off-by-default** tools so rare or
  unreleased collectibles can't be leaked; even when enabled, only released collectibles show.
- **Knows your events** — optionally (off by default), it can list your special events, each marked
  active or not, so "what's on right now?" and "did I miss that one?" both get real answers. Hidden
  events are never included.
- **Currency-aware** — if you've configured a currency, it can tell the speaker their balance in
  your currency's own name. If you haven't (most instances), the tool isn't offered at all — no
  setup, no stray mentions of a currency your game doesn't have.
- **Optional web search** — off by default; when on, uses Gemini's built-in Google Search grounding to
  answer with current information.
- **Always-on model fallback** — list backup models; if the primary hits its quota or errors, the next
  one is used automatically, so the bot keeps working. A model that has spent its daily quota is
  remembered and skipped to the back of the queue until it resets, rather than being retried into a
  rejection every turn.
- **Built for shared keys** — every Gemini call goes through a rate-limited queue, so one API key used
  across multiple servers never bursts past its quota. Requests wait their turn instead of dropping.
- **Quota protection** — a per-model rate limit that routes around a busy model, one per-user cooldown
  shared by `/chat` and mentions, and an optional daily request budget that makes the bot bow out
  gracefully rather than start failing. See [Quota protection](#quota-protection).
- **Free to run** — designed around Google AI Studio's permanent free tier (no credit card).

## Installation

Add this to your instance's `config/extra.toml`:

```toml
[[ballsdex.packages]]
location = "git+https://github.com/OLi51/ballsdex-aichat.git@1.5.0"
path = "aichat"
enabled = true
```

Then `docker compose build` and run migrations as usual (they run automatically on `docker compose up`).

### Updating

**The latest tag is always the stable one** — every tag is a release that ran on a live instance
before being cut. There is no unstable or pre-release channel; `main` between tags may be
mid-refactor, so pin a tag rather than a branch.

There is no auto-update: packages are installed when the Docker image is built, and the bot never
fetches anything at runtime. To move to a new version, **change the `@<version>` in the snippet
above**, then:

```
docker compose build
docker compose down && docker compose up -d
```

If you forget to bump the version in `extra.toml`, nothing changes — the pinned tag is what gets
installed. Check the [releases page](https://github.com/OLi51/ballsdex-aichat/releases) for what's
new; anything that needs a settings change or a migration is called out in the release notes.

## Setup

1. Get a free Google AI Studio API key at <https://aistudio.google.com/apikey> — no credit card
   required, just a Google account.
2. In the admin panel, open **AI chat settings**, paste the key into `api_key`, and tick `enabled`.
3. Optionally edit `personality` to give the bot a voice. You do **not** need to tell it its own name
   or what's collected — that's injected automatically from your core settings. Just describe the
   personality.
4. Optionally restrict it to specific **server** channels with `allowed_channel_ids` (DMs are never
   restricted by that list — see below).

### Direct messages

`/chat` and `/forget` work in DMs out of the box (they arrive as interactions, which don't need any
extra intent).

Free-text DM chat — just messaging the bot without a command, like old Shapes bots — additionally
requires the bot to receive DM message events, which **stock Ballsdex does not enable**. To turn it on,
add `dm_messages=True` to the `discord.Intents(...)` call in `ballsdex/core/bot.py`, then rebuild. Note
that's a core edit outside this package, and a `git pull` of Ballsdex may revert it.

### Choosing a model (and fallbacks)

The default is `gemini-3.5-flash-lite` — a meaningfully better model than 3.1 Flash-Lite (notably
stronger on coding/agentic and long-context benchmarks per Google's own release) while sharing the
exact same free-tier limits, roughly **500 requests/day at 15/min**, versus ~20/day for every other
free Flash model. Copy the exact model ID from Google AI Studio if it errors; limits and model IDs
vary by account and change over time.

`fallback_models` is a semicolon-separated list of backups, tried in order on any failure. Defaults to
`gemini-3.1-flash-lite`, which has the *exact same* free-tier limits as the default primary
(~500/day, 15/min) and its own separate daily quota — so out of the box this roughly doubles your
daily headroom for free, at the cost of slightly weaker responses once it's overflowing to the
backup. Clear the field if you don't want that. Only add IDs you've confirmed exist on your account:
Google retires model IDs over time (`gemini-2.5-flash-lite`, for instance, is already gone for new
projects), so a stale fallback just adds a dead entry.

**Web search caveat:** Gemini's Google Search grounding is free only on **Gemini 2.x** models
(~1,500/day) — *not* on Gemini 3.x. Confirmed empirically against both `gemini-3.1-flash-lite` and
`gemini-3.5-flash-lite`: plain chat calls succeed, but a grounded call immediately 429s on zero
search-grounding quota. So with the default 3.x chat models, `allow_web_search` gracefully falls back
to answering without
search. Free web search realistically isn't available alongside the high-throughput 3.x chat models.

### Data exposure (off by default)

These tools are disabled until you turn them on, so a curious user can't coax the AI into leaking things:

- **`allow_stats_lookup`** — look up and search released collectibles' stats (rarity, health, attack,
  capacity).
- **`allow_artwork`** — fetch and post a released collectible's artwork in chat.
- **`allow_special_events`** — list your special events (see below).
- **`allow_web_search`** — let the AI search the web via Gemini's Google Search grounding.

The player's own data — collection, individual copies, and balance — is always available and is
unaffected by these; it is bound to the speaker's Discord ID, so the AI can never be talked into
reading someone else's. Even with the tools on, only released (enabled) collectibles are ever
exposed — never rare, unreleased, or admin-only ones.

#### Special events

With `allow_special_events` on, the AI can list your events — name, emoji, catch phrase, whether
those cards are tradeable, start/end dates, and an `active` flag. `active` uses the *same* window
core uses to decide what can actually spawn, so it matches what players can really catch; an event
with no dates counts as permanently active.

**Events that aren't hidden are all returned**, finished and not-yet-started included, each flagged
accordingly — that's deliberate, so the bot can say "that ended last Tuesday" instead of pretending
an event never existed. It's also why the toggle defaults to off: a non-hidden event you've created
but not announced yet would show up. If you stage events in advance, either keep them `hidden` until
launch (they are *never* listed, toggle or not) or leave this off.

### Quota protection

Three settings, in the **Quota protection** section of the admin panel, control how hard the bot is
allowed to lean on your API key. What it has actually spent is visible under **Daily usage**.

**`requests_per_minute`** (default 12) spaces out the actual Gemini API calls across every server
this bot is in. Keep it just under your model's real per-minute quota (the free tier is 15/min);
raise it on a paid plan.

**It counts API requests, not replies.** A reply costs one request, plus one more for every round of
tool use — so a reply that looks something up in your data typically costs 2, and a question that
chains several lookups can cost 4 or 5. At the default of 12 that's roughly 6 lookup-style replies a
minute. Budget for the requests, not the conversations.

It is also **per model**. The free tier meters each model ID separately, so a primary plus one
fallback is two independent 15/min buckets rather than one. The bot prefers your primary and only
spills over to a fallback when the primary's bucket is actually busy — so a quiet instance always
gets your best model, and a busy one gets an answer instead of a wait.

**Daily exhaustion is handled separately**, because it's a different problem: a model that has spent
its ~500-a-day allowance will keep saying so until the quota resets, and no amount of pacing helps.
When a model reports its *daily* quota gone, the bot remembers and sorts it last for the rest of the
day, so the chain stops opening every turn with a request that can only be rejected. Per-minute
429s are deliberately not treated this way — those mean "wait a moment", not "come back tomorrow",
and benching a good model over one busy minute would be far worse than the wasted retry.

This is remembered in the database, so it survives a restart, and it expires by itself at Pacific
midnight — a new day is simply a new row. An exhausted model is *demoted, never dropped*: that
belief comes from a single API response, so it still gets tried as a last resort rather than risking
an empty chain and a silent bot. You can see which models are flagged under **Daily usage**.

**`user_cooldown_seconds`** (default 10) is the minimum gap between one person's messages, across
**both** `/chat` and mentions/DMs — one shared budget per person. Previously `/chat` had a hardcoded
5-second cooldown and the mention path had none at all, so mentioning the bot was the cheapest way
for one person to drain a shared key; splitting the limit per path just relocates that problem.
Mentions sent too soon get a ⏳ reaction, `/chat` gets a quiet ephemeral note — neither costs an API
request. Set to 0 to disable.

This one is about **fairness, not quota**. The rate limiter already stops the bot bursting past your
per-minute quota, and the daily budget covers the day; what the cooldown prevents is one fast talker
filling the queue so everyone else waits behind them. That's why 10s is a reasonable default and
going much higher buys little — see the note below.

**`daily_request_budget`** (default 0 = no budget) stops the bot after a set number of API requests
in a day, replying with a friendly "back tomorrow" line rather than failing. The count is stored in
the database, so it survives a restart, and it resets at midnight US Pacific — the same moment
Google's free daily quotas do. Set it if you'd rather the bot go quiet on its own terms than have
every request start failing mid-conversation when the real quota runs out. The free tier gives
roughly 500 requests/day *per model*, so a primary and a fallback is ~1,000.

A turn already in flight is allowed to finish, so the budget can overshoot slightly rather than
abandoning a reply it has already paid for.

The budget is **global to the bot, not per Discord server**. It protects your API key, and the key
is what all your servers share — so a busy server can spend the day's budget and a quiet one will
find the bot resting. It's also global to the *host*: the day boundary is US Pacific regardless of
where your server is or what timezone it's set to, because that's when Google's quota actually
resets. A budget counting your local midnight would either free up while the real quota was still
exhausted, or stay locked for hours after it had already refilled.

## License

MIT, see `LICENSE`.
