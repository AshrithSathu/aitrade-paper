# Railway backend with Cloudflare Access

The backend runs as one Railway service, with no public Railway domain or TCP proxy. cloudflared and the Python dashboard run together; the dashboard listens only on loopback. Cloudflare Tunnel validates Access JWTs before forwarding requests.

- URL: https://aitrade.sathuashrith.com
- Allowed email: sathuashrith@gmail.com
- Railway project: aitrade-paper (`75e81864-86d2-4c44-a4fe-4e2563a5b5df`), Axion Labs workspace
- Service: paper (`818bdf7b-d93f-43b9-883c-21e4daa924bf`)
- Environment: production (`102a2a5b-c18d-41a8-b732-ad73910b246f`)
- Volume: paper-volume (`6a1d9019-f80a-4405-a047-6a4068b572b4`), mounted at `/app/data`
- Tunnel: aitrade-paper (`b6503490-c814-4202-aef5-5f1afedaaa7d`)

The volume stores `/app/data/polymarket` (paper account, history and reviews) and `/app/data/codex` (separate cloud login). No Mac credentials or local paper data were uploaded.

`railway_start.py` drops privileges to UID 1000, removes the tunnel credential from child environments, and runs the tunnel using a private temporary token file. If either process exits, the supervisor stops the other and exits for Railway to restart it. Every dashboard boot starts paused. A restart must never resume trading automatically.

Checks: `bun run test`; `python3 -m py_compile railway_start.py`. Deploy only the nine runtime/config files via a temporary source directory, never the local data or credentials. Run `railway up <source-dir> --path-as-root --project 75e81864-86d2-4c44-a4fe-4e2563a5b5df --environment production --service paper --detach --json`.

Before trading: verify the deployed process, tunnel, current public data and separate Codex CLI login; run a manual preview; restart and confirm persistent account/history and paused state. The Access redirect alone is not proof that the backend works. Configure volume backups before retaining important runs.

Current rollout: `bb92baca-adb1-4eed-8226-834c595bbf39` (cleanup deployed successfully). Verified after restart: ChatGPT CLI login valid, paused, $1000 cash, zero trades, saved preview restored, 3651 recorded ticks including pre-restart history, no worker/feed errors, and obsolete cloud storage module absent. The real cloud preview completed with execution disabled and returned WAIT because the current market opening tick was missing. Authenticated browser UI verification remains pending; the user requested no browser automation. Trading has not started.

Latest rollout: `b67ff520-54b1-409e-a200-3cc029506ef2`, source commit `b34d3d0`, adds one scheduled entry review during minute 3–4 and holds positions to settlement. Production engine SHA-256 matches local source (`a8b0767c80ed867cdb1f02ccc055de79f1a2b71b089047f7c59f7385977a7d85`). Verified paused, $1000, zero trades, no running Codex exec processes, no worker/feed errors; interval setting removed. No AI call or paper trade was started during this rollout. The new scheduled mode is covered by isolated tests; its first real scheduled run remains pending user start.

Private GitHub repository: https://github.com/AshrithSathu/aitrade-paper, branch `main`. History starts with the prior Railway baseline, followed by the single-review change. Account data and credentials are excluded from Git.
