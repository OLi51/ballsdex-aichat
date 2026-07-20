# ballsdex-aichat

A chat companion for [Ballsdex](https://github.com/Ballsdex-Team/BallsDex-DiscordBot) v3, powered by
Google's Gemini API. Mention the bot or DM it to talk, or use `/chat`. It's not a dumb chatbot — it can
look up the player's own collection (and, if you allow it, collectible stats, search, and artwork)
through function calling, so it answers from your bot's real data instead of making things up.

## Features

- **Talk naturally** — mention the bot, DM it, or run `/chat`. `/forget` clears a channel's memory.
- **Knows your bot** — the bot's name and collectible name (and your `/about` description, if you've
  customized it) are baked into every request, so the AI stays in character as *your* bot.
- **Reads live game data** — the speaker's own collection summary is always available (it can never
  read anyone else's). Collectible stats/search and artwork are separate, **off-by-default** tools so
  rare or unreleased collectibles can't be leaked; even when enabled, only released collectibles show.
- **Optional web search** — off by default; when on, uses Gemini's built-in Google Search grounding to
  answer with current information.
- **Always-on model fallback** — list backup models; if the primary hits its quota or errors, the next
  one is used automatically, so the bot keeps working.
- **Built for shared keys** — every Gemini call goes through a single rate-limited queue, so one API key
  used across multiple servers never bursts past its quota. Requests wait their turn instead of dropping.
- **Free to run** — designed around Google AI Studio's permanent free tier (no credit card).

## Installation

Add this to your instance's `config/extra.toml`:

```toml
[[ballsdex.packages]]
location = "git+https://github.com/OLi51/ballsdex-aichat.git@1.2.0"
path = "aichat"
enabled = true
```

Then `docker compose build` and run migrations as usual (they run automatically on `docker compose up`).

## Setup

1. Get a free Google AI Studio API key at <https://aistudio.google.com/apikey> — no credit card
   required, just a Google account.
2. In the admin panel, open **AI chat settings**, paste the key into `api_key`, and tick `enabled`.
3. Optionally edit `personality` to give the bot a voice. You do **not** need to tell it its own name
   or what's collected — that's injected automatically from your core settings. Just describe the
   personality.
4. Optionally restrict it to specific **server** channels with `allowed_channel_ids`. Direct messages
   always work regardless of that list.

### Choosing a model (and fallbacks)

The default is `gemini-3.1-flash-lite` — the standout free-tier chat model at roughly **500 requests/day
at 15/min**, versus ~20/day for every other free Flash model. Copy the exact model ID from Google AI
Studio if it errors; limits vary by account and change over time.

`fallback_models` is an optional semicolon-separated list of backups, tried in order on any failure.
Empty by default — `gemini-3.1-flash-lite`'s ~500/day is plenty for most bots. Only add IDs you've
confirmed exist on your account: Google retires model IDs over time (`gemini-2.5-flash-lite`, for
instance, is already gone for new projects), so a stale fallback just adds a dead entry.

**Web search caveat:** Gemini's Google Search grounding is free only on **Gemini 2.x** models
(~1,500/day) — *not* on Gemini 3.x, which is confirmed to have a zero search-grounding quota on the free
tier. So with the default 3.x chat model, `allow_web_search` gracefully falls back to answering without
search. Free web search realistically isn't available alongside the high-throughput 3.x chat models.

### Data exposure (off by default)

Three tools are disabled until you turn them on, so a curious user can't coax the AI into leaking things:

- **`allow_stats_lookup`** — look up and search released collectibles' stats (rarity, health, attack,
  capacity).
- **`allow_artwork`** — fetch and post a released collectible's artwork in chat.
- **`allow_web_search`** — let the AI search the web via Gemini's Google Search grounding.

The player's own-collection summary is always available and is unaffected by these. Even with the tools
on, only released (enabled) collectibles are ever exposed — never rare, unreleased, or admin-only ones.

### Rate limiting

All chat requests share one queue. `requests_per_minute` (default 12) controls the minimum spacing
between Gemini calls across every server this bot is in — keep it just under your model's actual
per-minute quota (the free tier is 15/min). Raise it if you move to a paid plan.

## License

MIT, see `LICENSE`.
