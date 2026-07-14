# Calmecac

## Discord gateway

Create a `.env` file in the project root (gitignored) with `DISCORD_BOT_TOKEN=<your bot token>`
and `DISCORD_ALLOWED_USER_ID=<your Discord user id>`. In `config.yaml`, set `discord.enabled: true`
and `discord.channel_id` to the single channel the bot should listen in. Run with
`python -m harness.discord_gateway`.

Each channel keeps short-term memory of its last few exchanges (in-process only — nothing is
written to disk, and it's lost on restart; restarting the gateway is the documented way to wipe
it). Send `!reset` in the channel to clear that channel's memory on demand.

`discord.verbosity` in `config.yaml` controls how much the bot posts while a task runs: `quiet`
(default) posts only reactions and the final answer, editing a single status message in place
every ~5 steps for runs longer than 4 steps; `steps` posts every tool call as its own message.
Send `!verbose` in the channel to flip a channel's verbosity at runtime (not persisted — resets
to the config default on restart).
