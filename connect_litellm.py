"""connect_litellm.py — minimal LiteLLM connector."""
import os, requests
from pathlib import Path

def _load_env():
    for line in (Path(__file__).parent / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return os.environ["LITELLM_BASE_URL"].rstrip("/"), os.environ["LITELLM_API_KEY"]

def list_models():
    base, key = _load_env()
    r = requests.get(f"{base}/v1/models",
                     headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", []) if m.get("id")]

def chat(prompt, model=None, max_tokens=256, temperature=0):
    base, key = _load_env()
    model = model or os.environ.get("MODEL_NAME", "gpt-4.1")
    r = requests.post(f"{base}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model,
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens, "temperature": temperature},
        timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def embed(text, model=None):
    base, key = _load_env()
    model = model or os.environ.get("EMBED_MODEL", "text-embedding-3-large")
    r = requests.post(f"{base}/v1/embeddings",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "input": text}, timeout=60)
    r.raise_for_status()
    return r.json()["data"][0]["embedding"]
