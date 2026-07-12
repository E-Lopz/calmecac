# Calmecac

## Discord gateway

Create a `.env` file in the project root (gitignored) with `DISCORD_BOT_TOKEN=<your bot token>`
and `DISCORD_ALLOWED_USER_ID=<your Discord user id>`. In `config.yaml`, set `discord.enabled: true`
and `discord.channel_id` to the single channel the bot should listen in. Run with
`python -m harness.discord_gateway`.
