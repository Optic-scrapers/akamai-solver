# Akamai Solver

Redis Streams-backed worker that solves browser-gated origin sessions and publishes
terminal session results to a reply stream.

This repo is single-backend by design:

- `akamai` - headless Chromium + CloakBrowser with origin warmup derived from `challenge_url`

## Quick Start

1. Build the image:

```bash
make build
```

2. Copy the env file:

```bash
cp .env.example .env
```

3. Set your Redis password in `redis.conf`:

```conf
requirepass <your-password>
```

4. Set `.env`:

```bash
SOLVER_NAME=akamai

REDIS_PASS=123
MAGENT_RUNNING=false
```

5. Start the stack:

```bash
docker compose up -d
```

Stop it with:

```bash
docker compose down -v
```

## Streams

Requests are consumed in priority order from Redis Streams using the
`akamai:solvers` consumer group by default:

1. `akamai:requests:high`
2. `akamai:requests:normal`
3. `akamai:requests:low`

The worker writes exactly one terminal result to `reply_to` before `XACK`ing the
original request stream entry. If a solver dies after claiming a request, another
solver can reclaim the pending entry with `XAUTOCLAIM` after
`REQUEST_CLAIM_IDLE_SECONDS`.

## Request

```json
{
  "reply_to": "my-project:my-spider:node:results",
  "request_id": "job-123",
  "challenge_url": "https://example.com/robots.txt",
  "proxy": "http://user:pass@host:port"
}
```

- `reply_to` - required Redis result stream
- `request_id` - optional, echoed back in the response
- `challenge_url` - required target URL
- `proxy` - optional proxy

## Response

Success:

```json
{
  "status": "ok",
  "request_id": "job-123",
  "cookies": {"session_cookie": "..."},
  "headers": {"user-agent": "..."},
  "proxy": "http://user:pass@host:port",
  "extra": {"created_at": "1760922345123", "solver": "akamai"}
}
```

Failure:

```json
{
  "status": "error",
  "request_id": "job-123",
  "error": "solver_error",
  "message": "failed to create session"
}
```
