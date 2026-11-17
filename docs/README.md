# Documentation

How OpenNotebook is built and why. Start with the [design](design.md) for the look and feel of the app, and [stack-migration-plan.md](stack-migration-plan.md) for how the server, web app and database fit together.

## Features

| Feature | Spec |
| --- | --- |
| Sources, ask and chat | [phase1-spec.md](phase1-spec.md), [plans/study-with-your-sources.md](plans/study-with-your-sources.md) |
| Narrated decks you can interrupt | [phase2-spec.md](phase2-spec.md) |
| Audio overviews | [audio-overview-spec.md](audio-overview-spec.md) |
| Video overviews | [video-overview-spec.md](video-overview-spec.md), [plans/video-themes.md](plans/video-themes.md) |
| Mind maps | [mindmap-spec.md](mindmap-spec.md) |
| Study notes | [study-notes-spec.md](study-notes-spec.md) |
| AI providers and the setup tour | [ai-providers-plan.md](ai-providers-plan.md) |

## Project notes

These are working documents. They record decisions and the reasoning behind them, so some describe plans that have since been built.

- [open-questions.md](open-questions.md): decisions still to make, and what each one blocks.
- [stack-migration-plan.md](stack-migration-plan.md): the move to React, FastAPI and Postgres.

Running it yourself is covered in the [README](../README.md#try-it), and every setting is explained in [deploy/.env.example](../deploy/.env.example).
