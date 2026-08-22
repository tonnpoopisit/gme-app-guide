# GME App Guide

Public customer-facing site teaching GME app customers how to register, verify with the 4-digit code, set up autodebit (3-digit code), add a receiver, send money, and understand limits — plus an admin CMS (single PIN) for managing topics and media.

Run locally:

```bash
py customer_guide_server.py
```

Opens at `http://127.0.0.1:5153`. Admin panel at `/admin` (PIN printed to console on first run).

For deploying to Google Cloud Run — including the full command runbook and every Windows/gcloud/ESET gotcha already solved — see [`.claude/skills/customer-guide-deploy/SKILL.md`](.claude/skills/customer-guide-deploy/SKILL.md).
