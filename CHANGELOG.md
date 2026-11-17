# Changelog

All notable changes to OpenNotebook. The format follows [Keep a Changelog](https://keepachangelog.com), and versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

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

[Unreleased]: https://github.com/mahmoud-emad/opennotebook/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/mahmoud-emad/opennotebook/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mahmoud-emad/opennotebook/releases/tag/v0.1.0
