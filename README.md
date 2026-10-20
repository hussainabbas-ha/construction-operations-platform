# Construction Operations Platform

Assistant project-manager backend for construction operations. It verifies timecards, monitors schedule and quantity progress, tracks budget variance, and sends operational email reports.

## Overview

Python/FastAPI system with modules for:

- Time-log verification against field operations data
- Schedule monitoring and delay alerts
- Quantity and cost tracking
- Billing draft support
- JWT-authenticated API and scheduled jobs

## Getting started

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn api.main:app --reload
```

## Configuration

Copy `.env.example` and provide database, JWT, SMTP, and field-system credentials. Do not commit `.env`, credential JSON files, or operational data dumps.

## Security notes

- `.env`, `*_credentials.json`, `token.json`, and `vertex.json` are gitignored
- Uploads, verification output, and large response dumps are gitignored
