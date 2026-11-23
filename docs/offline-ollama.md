# Running Notabit fully offline with Ollama

This page walks through a setup where your sources, your questions and the model's answers never leave your machine: Notabit on Docker, a local [Ollama](https://ollama.com) for the models, and a local speech server for voices.

> **Status of this guide.** It is written from the code, the provider list and the setup tour, and it has not yet been run end to end on a clean machine. The settings and commands are the ones the app uses, but the model names are examples, not tested recommendations, and the notes on which features need a hosted model come from how the app assigns models to work, not from timing every feature. If something here is wrong for you, please [open an issue](https://github.com/mahmoud-emad/notabit/issues/new/choose) with what you saw. That is the most useful thing you can add to this page.

## What stays local, and what does not

| Part | Local option | Notes |
| --- | --- | --- |
| Your sources and notes | Postgres in your Compose stack | Always local. |
| Answers, scripts, outlines, notes | Ollama | Any chat model Ollama can serve. |
| Search over your sources | Ollama, with an embedding model | Used to find the right passages, so a citation points at where it came from. |
| Narration and spoken questions | A local speech server (Speaches) | The default voices use Microsoft's online Read Aloud service, so offline narration needs the local server below. |
| Web research, image generation | A hosted model | A local Ollama does not offer these. The setup tour flags roles your provider cannot serve, and the feature says so in a sentence instead of failing partway through a build. |

## 1. Install Ollama and pull two models

Install Ollama from <https://ollama.com/download>, then pull a chat model and an embedding model:

```sh
ollama pull llama3.2           # a chat model; pick one that fits your RAM
ollama pull nomic-embed-text   # an embedding model, for search over your sources
ollama list                    # both should appear
```

Pulling needs the internet once. After that, Ollama runs from its local cache. Any chat model Ollama serves will work in principle. Bigger models write better decks and answer better, and small ones are faster. Start small, and move up if the output is thin.

## 2. Start Notabit

```sh
git clone https://github.com/mahmoud-emad/notabit.git
cd notabit
cp deploy/.env.example deploy/.env   # set POSTGRES_PASSWORD
```

Because Notabit runs in Docker, `localhost` inside its container is the container itself, not your machine. Point it at the Ollama on the host. In `deploy/.env`:

```sh
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
```

`compose.yaml` maps `host.docker.internal` for you, on Linux too. Ollama's address ends in `/v1`: that is its OpenAI-compatible API, and the app needs it.

Then start the stack:

```sh
make up
```

## 3. Connect Ollama in the setup tour

Open <http://localhost/ui/>. The first time, the setup tour asks for a provider:

1. Choose **Ollama**. It needs no key, only its address. If you set `OLLAMA_BASE_URL` above, it is already filled in.
2. Press **Test**. The tour asks Ollama for its model list. If Ollama answers but has no models, it tells you to pull one (`ollama pull llama3.2`) and test again.
3. In **Models**, the tour shows each kind of work and the model for it. Choose your chat model for the text roles and your embedding model for search. Roles Ollama cannot serve are flagged here.

You can change any of this later in **Settings › AI providers** and **Settings › Models**. A model id with a colon, such as `llama3.2:3b`, is read as an Ollama model on your primary connection, not as another provider.

## 4. Offline voices

Narration works with no key by default, but it uses Microsoft's online Read Aloud service, so it needs the internet. For narration and spoken questions that stay on your machine, start the bundled speech server (Speaches) and switch to it.

Start the stack with the speech profile:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile speech up -d
```

Then in `deploy/.env`:

```sh
NOTABIT_TTS_PROVIDER=openai
# These are the defaults when the speech profile is on:
# NOTABIT_TTS_MODEL=speaches-ai/Kokoro-82M-v1.0-ONNX
# NOTABIT_STT_MODEL=Systran/faster-whisper-small
```

Run `make up` again to pick up the change. The speech models download the first time they are used, into a Docker volume, so the first narration needs the internet once. Spoken questions are transcribed by the same server. You can pick a voice in **Settings › Voices**.

## 5. Try it

Make a small collection from a pasted note or a PDF, then:

1. Ask a question in **Ask**. This checks the chat model, the embedding model and the citations.
2. Make **study notes** or a **mind map**. These read every source whole, so a model with a longer context window helps.
3. Make a **narrated deck** or an **audio overview**. This also checks the speech server.

Local models are slower than hosted ones and may write plainer slides. If a build is stopped or comes out thin, try a larger chat model for the text roles in **Settings › Models**, and give a long source a model with a longer context.

## If something does not work

- **Ollama does not answer.** See [Ollama or another local provider does not answer](troubleshooting.md#ollama-or-another-local-provider-does-not-answer): the address must be reachable from inside the container, and end in `/v1`.
- **No models listed.** Run `ollama list`. If it is empty, `ollama pull` one.
- **Search finds nothing.** Check that an embedding model is set for the search role and that it is one Ollama has pulled.
- **Narration is silent or fails.** Check the speech container is running (`docker compose ... ps`) and that `NOTABIT_TTS_PROVIDER=openai` is set.

## Also works: LM Studio and other servers

The same steps work with [LM Studio](https://lmstudio.ai) (address `http://host.docker.internal:1234/v1`, set as `LMSTUDIO_BASE_URL`) and with any server that speaks the OpenAI API, such as vLLM or a LiteLLM proxy. Choose **Other (OpenAI-compatible)** in the setup tour and enter its address.
