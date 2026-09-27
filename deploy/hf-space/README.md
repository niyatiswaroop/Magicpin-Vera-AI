---
title: Vera Bot
emoji: 💬
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Vera — magicpin AI Challenge bot

Merchant-AI assistant endpoint for the magicpin challenge judge harness.

Endpoints (all under `/v1`): `healthz`, `metadata`, `context`, `tick`, `reply`, `teardown`.

Deterministic composer; no external API calls, no API keys, no telemetry. Context
pushed by the judge is held in-process and mirrored to a local SQLite file so a
container restart does not lose it.
