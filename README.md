# BTC Autotrade + Paytm Wallet System

## 📦 Contents
- Streamlit app (`python_project_paytm.py`)
- Flask webhook handler (`webhook.py`)
- `.env` config with secrets
- Deployment-ready Render templates

## 🚀 Deploy on Render

### 1. Streamlit App (UI)
- Go to [Render Dashboard](https://dashboard.render.com)
- New Web Service → from this repo
- Use `render.yaml` for build settings
- Start command: `streamlit run python_project_paytm.py --server.port 10000 --server.enableCORS false`

### 2. Webhook (Paytm)
- New Web Service → use `webhook.py`
- Use `render_webhook.yaml`
- Start command: `python webhook.py`

## 🛠 Requirements
```bash
pip install -r requirements.txt
```

## ✅ Environment
Use `.env.example` as reference for your secrets.
