# Changelog

All notable changes to Notabit. The format follows [Keep a Changelog](https://keepachangelog.com), and versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

### Changed

- The project is now called Notabit. The Python package, the `notabit` command, the images (`notabit-*`) and the repository links use the new name. Environment variables start with `NOTABIT_` instead of `OPENNOTEBOOK_`, and the default database name and user are `notabit`, so rename them in your `.env` and database when you upgrade.

## [0.3.0] - 2026-11-18

### Added

- Images for arm64 as well as amd64, so the published images run on Apple Silicon, Raspberry Pi and ARM servers. One tag pulls the right one.
- Switch a source off so nothing reads it, and search a collection's sources.
- Keep a chat answer as a note.
- Transformations: run a summary, key claims, flashcards, a timeline, or an instruction of your own with `/transform`, and manage them in Settings.
- A live log of what each job is doing, and a loading state for every request.
- Collection covers drawn as Aura and Relief.
- A project website at <https://mahmoud-emad.github.io/notabit/>, a demo, a changelog, and a sponsor button.

### Fixed

- Medium pages are read, and switching a source on or off applies at once.
- Search runs again when the sources change.
- A message typed while a turn is running is kept.
- The studio waits longer when a provider rate limits, and says so when Microsoft refuses its free voices.
- Security updates: aiohttp, pillow, certifi, minimatch and zod.
- The server image stays on Python 3.14.

## [0.2.0] - 2026-10-12

### Added

- A setup tour on first run: pick a provider, paste a key (or a local server's address), choose models.
- Each key is tested before it is saved: that it is accepted, the credit left where the provider says, and that it can answer a request.
- Any AI provider: OpenRouter, OpenAI, Anthropic, Gemini, Azure OpenAI, Mistral, Groq, DeepSeek, Cohere, Fireworks, Cerebras, Qwen, Together, xAI, Ollama, LM Studio, or any OpenAI-compatible server. Keys entered in the app are stored encrypted.
- A model per task, from any connected provider.
- Audio, video and YouTube sources, transcribed with time marks (recordings up to 500 MB and 4 hours).
- An MCP server at `/api/mcp`, with studio keys.
- CI checks for migration drift, API and client drift, dependency audits, the compose file, and browser tests of setup and providers.

## [0.1.0] - 2026-10-08

The first public release: narrated slide decks, video overviews, audio overviews, mind maps and study notes, with every claim linked to its source.

[Unreleased]: https://github.com/mahmoud-emad/notabit/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/mahmoud-emad/notabit/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/mahmoud-emad/notabit/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mahmoud-emad/notabit/releases/tag/v0.1.0
