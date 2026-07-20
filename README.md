# ballsdex-aichat

A chat companion for [Ballsdex](https://github.com/Ballsdex-Team/BallsDex-DiscordBot) v3, powered by
Google's Gemini API. Mention the bot or DM it to talk, or use `/chat`. It's not a dumb chatbot — it can
look up the player's own collection and any collectible's stats and artwork through function calling,
so it answers from your bot's real data instead of making things up.

## Features

- **Talk naturally** — mention the bot, DM it, or run `/chat`. `/forget` clears a channel's memory.
- **Knows your bot** — the bot's name, collectible name, and `/about` description from your core
  Ballsdex settings are baked into every request, so the AI always stays in character as *your* bot.
- **Reads live game data** — function-calling tools let it fetch the speaker's collection summary, a
  collectible's stats/capacity, and its artwork (sent as an attachment). It can only ever read the
  collection of the person talking, never someone else's.
- **Built for shared keys** — every Gemini call goes through a single rate-limited queue, so one API
  key used across multiple servers never bursts past its quota. Requests wait their turn instead of
  being dropped.
- **Free to run** — designed around Google AI Studio's permanent free tier (no credit card).

## Installation

Add this to your instance's `config/extra.toml`:

```toml
[[ballsdex.packages]]
location = "git+https://github.com/OLi51/ballsdex-aichat.git@1.0.0"
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
4. Optionally restrict it to specific channels with `allowed_channel_ids`.

### Choosing a model

The default is `gemini-2.5-flash`. On the free tier, `gemini-2.5-flash-lite` gives you far more
requests per day (~1,000 vs ~250) at the same per-minute cap — a good choice for a busy bot. Set the
`model` field to whichever you prefer.

### Rate limiting

All chat requests share one queue. `requests_per_minute` (default 12) controls the minimum spacing
between Gemini calls across every server this bot is in — keep it just under your key's actual quota
(the free tier is 15/min). Raise it if you move to a paid plan.

### Music generation (optional, bring your own API)

`generate_music` is a **stub tool**: no music-generation provider is bundled. If you set `music_api_url`
and `music_api_key` in AI chat settings, the tool will `POST {"prompt": ...}` to that URL with the key
as a Bearer token and relay the JSON response. Without a provider configured, the bot honestly tells the
user it isn't set up — it never pretends to generate anything.

## License

MIT, see `LICENSE`.
