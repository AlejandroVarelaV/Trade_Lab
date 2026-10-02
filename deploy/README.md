# Deploying TradeLab

Same image cycle as STAIR: **build locally → push to ghcr.io → pull on the
server**. The server never builds and needs exactly two files in
`/home/avarela/tradelab`:

```
docker-compose.yml   # from this repo
.env                 # secrets + TRADELAB_IMAGE (chmod 600, never committed)
```

Everything else (code, migrations, `config/scenarios.yaml`) is inside the image.
A scenario change is therefore a commit + a new image, which also versions it.

Image: `ghcr.io/alejandrovarelav/tradelab:<git-sha>`, built for `linux/amd64`
(the Hetzner CX23 is x86_64). Each image carries the
`org.opencontainers.image.source` label
(`https://github.com/AlejandroVarelaV/Trade_Lab`, links the package to the repo)
and `org.opencontainers.image.revision=<git-sha>`. The SHA is also in the
container as `TRADELAB_GIT_SHA` and is recorded in every `job_runs` row.

## 1. Build and push (local machine)

One-time: create a GitHub personal access token (classic) with
`write:packages`, then:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u alejandrovarelav --password-stdin
```

Each release (from a clean, committed tree):

```bash
GIT_SHA=$(git rev-parse --short=12 HEAD)
IMAGE=ghcr.io/alejandrovarelav/tradelab

# The server (Hetzner CX23) is x86_64, so always build linux/amd64. Use the
# `default` builder: the WSL host is x86_64 too, so it builds natively (not
# through another builder such as an emulated arm one).
docker buildx build --builder default --platform linux/amd64 \
  --build-arg GIT_SHA="$GIT_SHA" \
  -t "$IMAGE:$GIT_SHA" -t "$IMAGE:latest" \
  --push .

# Confirm what was pushed
docker buildx imagetools inspect "$IMAGE:$GIT_SHA"
```

The first push creates a **private** package. Leave it private; the server
logs in with a read-only token (below).

## 2. Pull and run (server)

One-time on the server: a token with only `read:packages`:

```bash
echo "$GHCR_READ_TOKEN" | docker login ghcr.io -u alejandrovarelav --password-stdin
mkdir -p /home/avarela/tradelab && cd /home/avarela/tradelab
# copy docker-compose.yml here (scp from your machine), create .env from .env.example
chmod 600 .env
```

Each release: set the exact SHA in `.env`, then pull and restart:

```bash
cd /home/avarela/tradelab
sed -i "s|^TRADELAB_IMAGE=.*|TRADELAB_IMAGE=ghcr.io/alejandrovarelav/tradelab:<git-sha>|" .env
docker compose pull
docker compose up -d          # migrate runs first, then bot + jobs start
docker compose ps
docker compose logs --tail=20 migrate jobs
```

Pin the SHA, not `latest`, so the server runs exactly what you reviewed.
**Rollback** means putting the previous SHA back in `.env`, then
`docker compose pull && docker compose up -d`. Migrations only move forward,
so roll back code only across releases that added no migration.

## 3. Stop the local stack once the server runs

Telegram allows **one** long-polling client per bot token: if the local `bot`
runs at the same time as the server's, both get `409 Conflict` and updates go
to whichever polled last. The local `jobs` service would also run a second
22:30 UTC job against the local database and send its own Telegram status. As
soon as the server's `bot` and `jobs` are up, stop them locally:

```bash
# on your machine, in the repo
docker compose stop bot jobs
docker compose ps            # bot and jobs must not be running
```

Keep them stopped (or `docker compose down`) for as long as the server runs.

## 4. First deploy of Phase 1 (one-off backfill)

```bash
cd /home/avarela/tradelab
docker compose run --rm jobs python -m tradelab.jobs.daily --backfill --ingest-only
docker compose run --rm jobs python -m tradelab.jobs.daily     # first live run: creates
                                                               # scenarios and C orders
```

After that, the `jobs` service runs the daily job at 22:30 UTC and sends the
Telegram ✅/⚠️/🔴. If the container restarts after 22:30 and that day's run is
missing, it catches up immediately.

## Isolation from STAIR

The compose project is named `tradelab` (containers `tradelab-*`, network
`tradelab_internal`, volume `tradelab_pgdata`). It publishes no ports and
never references the `stair` project, its network or its volumes.
