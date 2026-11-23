# First-run troubleshooting

Run the commands below from the Notabit checkout. The Docker deployment reads its settings from [`deploy/.env`](../deploy/.env.example).

## Compose asks for `POSTGRES_PASSWORD`

If `make up` says `deploy/.env` is missing, copy [`deploy/.env.example`](../deploy/.env.example) to `deploy/.env`. If Compose says `set POSTGRES_PASSWORD in deploy/.env`, the file is missing or its password field is empty. Set `POSTGRES_PASSWORD` there to a private value. You can generate a URL-safe value with `openssl rand -hex 24`.

Check that Compose can read the settings before trying `make up` again. The quiet form validates the configuration without printing values from your environment file.

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
```

## A port is already in use

If Docker says `address already in use` or `port is already allocated`, another process or container is listening on a host port Caddy tried to publish. The defaults are HTTP port 80 and HTTPS port 443. Set unused host ports in `deploy/.env`, for example:

```dotenv
HTTP_PORT=8080
HTTPS_PORT=8443
PUBLIC_URL=http://localhost:8080
```

For local HTTP with `DOMAIN` unset, run `make up` again and open <http://localhost:8080/ui/>. Keep `PUBLIC_URL` aligned with the browser address. A public deployment with automatic HTTPS has its own [domain setup](../README.md#try-it).

## The key works, but the provider has no credit

The setup tour or **Settings → AI providers** can report that it accepted the key but found no credit. Open the provider's account dashboard and check the balance or quota for the project tied to the key. Then test again, or choose another provider. If the app says the provider **refused** the key, check that you copied the complete key and used it with the matching provider.

## Ollama or another local provider does not answer

In Docker, `localhost` in a provider address refers to the Notabit API container. For Ollama on the Docker host, enter `http://host.docker.internal:11434/v1` in the setup tour or set `OLLAMA_BASE_URL` to that value in `deploy/.env` and rerun `make up`. The supplied Compose file maps that hostname on Linux as well.

Confirm Ollama is running and has a model installed with `ollama list`. On Linux, ensure Ollama listens on an address reachable from the Docker bridge. Restrict access to the host and its containers. If you get an API compatibility error instead of a connection error, check that the address includes `/v1`.

## The first start seems slow

`make up` builds the server and web images. The first run also starts Postgres and runs database migrations before the API starts. If a build is still printing progress, let it finish. If the browser is still unavailable afterward, check the service state and the latest startup logs:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml ps -a
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail 80 postgres migrate api caddy
```

An exited `migrate` service with code `0` is expected. Its logs are the first place to look if it exits with an error. If you prefer to skip local image builds, follow the [published-image instructions](../README.md#try-it).

If the problem persists, see [Getting help](../SUPPORT.md). Remove keys, passwords and database URLs from any logs you share.
