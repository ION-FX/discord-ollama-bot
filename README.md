# discord-ollama-bot
Discord AI bot with multi-model chat, council/debate reasoning, usage limits, and admin controls.


Multiple chat modes: Light, Medium, and Heavy model routing
Council mode: Queries multiple AI agents at once and selects a final answer
Debate mode: Runs multi-round agent debate and synthesizes a final response
Per-user daily limits: Tracks usage by mode with configurable defaults
Custom user limits: Supports admin-defined overrides per user and mode
Admin command suite: Add/remove users, set limits, reset limits, view status
Daily automatic reset: Resets usage counters every 24 hours
Discord slash commands: Clean interaction flow using application commands
DM support: Commands work in servers, DMs, and private channels
Ollama-compatible backend: Connects to local or remote Ollama-style chat APIs
Response metadata: Shows model speed / tokens-per-second style stats
Persistent JSON storage: Saves users, limits, and reset state locally


Nobody will use this anyways so I didn't bother making use .env.
If you do use it, thank you :).
