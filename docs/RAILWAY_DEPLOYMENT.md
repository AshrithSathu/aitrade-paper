# Railway deployment

The Docker image runs `python3 -m infra.railway_start`. The supervisor starts `python3 -m backend.dashboard` and Cloudflare Tunnel; the dashboard listens only on loopback port 8765. Cloudflare Access protects the external hostname.

## Configuration

Infrastructure lives in `.railway/railway.ts`. Use `bun install` and `railway config plan` to review infrastructure changes separately from application deployment.

Existing app variables:
- `DATABASE_URL`: Railway reference to the PostgreSQL service.
- `PUBLIC_ORIGIN`: exact HTTPS dashboard origin.
- `TUNNEL_TOKEN`: Cloudflare Tunnel credential.

The persistent volume remains mounted at `/app/data`. Source-code moves must never move account files or Codex authentication. No secret belongs in Git or the image.

## Release checks

1. Run `bun run test` and `bun run check`.
2. Build the Docker image and smoke-test its module imports and static-file paths.
3. Check both running accounts before deployment. Record deadlines and pause active sessions before restarting.
4. Deploy the Dockerfile with `backend/`, `web/`, `feeds/` and `infra/`. The Docker allowlist excludes data, secrets and development files.
5. Verify deployment success, both account statuses, Codex login and the SSE stream through the running service.

Boot always pauses both accounts. Resume only a previously authorized run, preserving its deadline and profit baseline. Never use a deployment to implicitly start a new run.

A controlled reset marker under the paper data directory is destructive. It is only for an explicitly requested account/history reset, never a deployment step.

## Network cost

- Polymarket books and Chainlink prices enter the backend over WebSockets. They stay in memory and are never relayed raw to the browser.
- The dashboard receives a compact, one-way SSE view at most once per second. A WebSocket would carry the same bytes and add protocol code without helping this one-way UI.
- Start, Stop, settings, status and versioned history use ordinary HTTP. The browser reloads history only after its event version changes.
- PostgreSQL uses Railway private networking. Recent Chainlink context and contract history are cached by the existing feed process; adding Redis would duplicate small in-process caches.
- The persistent volume stores account state, review files and Codex login only. PostgreSQL stores the rolling price observations.

Railway publishes current usage and egress rates in its [pricing documentation](https://docs.railway.com/pricing). Re-measure `/api/events` after changes that add dashboard fields; transport choice cannot compensate for oversized repeated payloads.
