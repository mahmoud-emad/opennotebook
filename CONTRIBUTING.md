# Contributing

Thanks for wanting to help. Bug reports, ideas and pull requests are all welcome.

## Before you start

For anything bigger than a small fix, please open an issue first so we can agree on the approach. It saves you from writing code that won't fit.

## Setting up

The [README](README.md#working-on-it) covers what you need installed. Once that's in place, run `make dev` to start everything and `make check` to run what CI runs.

## Making a change

- Keep each pull request to one change. Small ones are reviewed faster.
- Add a test for anything new, and for any bug you fix.
- If you change the API, run `make api-client` and commit the regenerated client.
- Match the code around you. `make fmt` handles Python formatting; ESLint covers the web app.
- If you change how something behaves, update the doc in `docs/` that describes it.

## Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org): `type(scope): what changed`. Keep it in the imperative, and under about 50 characters:

```
fix(video): keep captions in step after a seek
feat(web): show the cost before making a video
```

## Reporting security issues

Please don't open a public issue. See [SECURITY.md](SECURITY.md).
