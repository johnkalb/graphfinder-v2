# Social Network Graph Pathfinder

Deployment-ready FastAPI app with GDELT enrichment, SEC insider data, 
and Epstein Committee document analysis. Built for controlled-access 
collaborative investigation.

## Quick Start

```bash
git clone <repo-url> && cd graphfinder
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Build the graph data
python build_index.py

# Run locally
cd webapp && uvicorn pathfinder:app --host 0.0.0.0 --port 8000
```

## Architecture

```
User → Cloudflare (auth, rate-limit, SSL)
         → DigitalOcean App Platform (FastAPI)
           → SQLite (data)
```

## Deployment (DigitalOcean App Platform)

1. Push this repo to GitHub (private)
2. In DigitalOcean: Create App → Connect GitHub repo
3. Set these environment variables:

| Variable | Description |
|---|---|
| `APP_ENV` | Set to `production` |
| `SECRET_KEY` | Random string for session security |
| `SENDGRID_API_KEY` | API key for auth emails |
| `AUTH_DOMAIN` | `sixdegrees.net` |

4. Deploy — the `app.yaml` in this repo handles the config.

## Setup Guides

### Cloudflare (Free — auth + rate limiting)

1. Sign up at cloudflare.com
2. Add sixdegrees.net → Cloudflare scans your DNS records
3. Set nameservers at TierraNet to Cloudflare's (they'll show you which)
4. Go to **Zero Trust** → **Access** → **Applications**
5. Add an app → point to your DigitalOcean URL
6. Under **Policy**, add the 10 email addresses
7. Go to **Security** → **WAF** → **Rate Limiting Rules**
8. Add rule: requests per 5 seconds, block for 60 seconds

### DigitalOcean App Platform ($12/mo)

1. Sign up at cloudflare.com/digitalocean (often $200 credit)
2. Click **Apps** → **Create App** → link GitHub
3. Select repo → App Platform auto-detects the Dockerfile
4. Set env vars above → Deploy
5. Get your URL (e.g. `graphfinder-xxxxx.ondigitalocean.app`)

### SendGrid (Free — auth emails from your domain)

1. Sign up at sendgrid.com
2. Go to **Settings** → **Sender Authentication**
3. Choose **Domain** → enter `sixdegrees.net`
4. Add the DNS records they give you in Cloudflare's DNS dashboard
5. Create an API key at **Settings** → **API Keys**
6. Set `SENDGRID_API_KEY` in DigitalOcean

### Domain DNS

1. In Cloudflare → **DNS** → add these records:

| Type | Name | Value |
|---|---|---|
| A | `@` | `<DigitalOcean IP>` or CNAME to `.ondigitalocean.app` |
| CNAME | `www` | `<DigitalOcean URL>` |
| TXT | `@` | SendGrid verification record |

2. At TierraNet: set nameservers to Cloudflare's (from Cloudflare dashboard)

## Development

```bash
# Add new data sources
python harvest_level_a.py          # IRS foundations
python harvest_irs_bulk.py <query> # IRS bulk search
python gdelt_gkg_harvester.py      # GDELT news enrichment
python sec_bulk_harvest_fast.py    # SEC Form 4 full harvest

# Rebuild after data changes
python build_index.py
cd webapp && uvicorn pathfinder:app --reload
```
