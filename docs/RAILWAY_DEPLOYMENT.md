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
