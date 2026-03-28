# AddRef

[English](README.md) | [简体中文](README_zh_cn.md)

AddRef is a self-hosted web application for automatically inserting PubMed references into biomedical and life science writing.

## Features

- Supports OpenAI-compatible APIs
- Supports `v1/chat/completions`, `v1/responses`, and automatic fallback
- Supports PubMed search, inline citation numbering, and RIS export
- Supports user registration, email verification, quota control, and job progress tracking
- Supports both direct deployment and Docker deployment

## Project Layout

```text
app/services/openai_compat.py      OpenAI-compatible request wrapper
app/services/ncbi.py               PubMed / NCBI search
app/services/citation_pipeline.py  Citation planning and insertion flow
app/services/user_store.py         Users, sessions, and quotas
app/utils/ris.py                   RIS export
app/web.py                         HTTP routes
static/                            Frontend pages, scripts, and styles
server.py                          Service entrypoint
Dockerfile                         Docker image build
docker-compose.yml                 Docker Compose deployment
deploy/systemd/                    Direct deployment examples
```

## Direct Deployment

Requirements:

- Python 3.12 or a compatible version
- Network access to your OpenAI-compatible endpoint and NCBI

Steps:

1. Clone the repository.
2. Install dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

3. Copy the example config and fill in real values:

```bash
cp auth.example.json auth.json
```

4. Start the server:

```bash
python3 server.py
```

The app listens on `0.0.0.0:14785` by default.

Open it in your browser:

```text
http://127.0.0.1:14785
```

If you need `systemd`, see [deploy/systemd/addref.service.example](deploy/systemd/addref.service.example).

## Docker Deployment

Prepare the config and data directory first:

```bash
cp auth.example.json auth.json
mkdir -p data
```

Build and start:

```bash
docker compose up -d --build
```

Stop:

```bash
docker compose down
```

Notes:

- The container exposes port `14785`
- `auth.json` is mounted read-only into the container
- `data/` is mounted for persistent database and runtime data

## License

This project is source-available and is not released under an OSI open source license.

- Non-commercial use is licensed under [PolyForm Noncommercial 1.0.0](LICENSE)
- Required notice is provided in [NOTICE](NOTICE)
- Commercial use: `yangzhuangqi@gmail.com`
- Commercial terms: [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md)

