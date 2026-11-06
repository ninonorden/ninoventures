#!/usr/bin/env python3
"""Filing Alerts hosted runner.

1) Pull active Polar subscriptions for the Filing Alerts product (custom fields:
   telegram_chat_id, tickers).
2) Poll SEC EDGAR for new Form 4 / 8-K / 10-Q / 10-K.
3) Send Telegram messages when TELEGRAM_BOT_TOKEN is set.

Env:
  POLAR_ACCESS_TOKEN   organization token
  TELEGRAM_BOT_TOKEN   BotFather token (optional until Nino creates the bot)
  SEC_USER_AGENT       e.g. 'NinoVenturesFilingAlerts/1.0 nino.ventures@agentmail.to'
  PRODUCT_ID           default 4d76b65f-9c86-4ae5-9095-404f810fee7d
  STATE_PATH           default ./state.json
"""
from __future__ import annotations

import json, os, re, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

PRODUCT_ID = os.environ.get("PRODUCT_ID", "4d76b65f-9c86-4ae5-9095-404f810fee7d")
API = "https://api.polar.sh"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
FORMS = {"4", "4/A", "8-K", "8-K/A", "10-Q", "10-K"}
MAX_TICKERS = 25

def polar(path: str):
    tok = os.environ["POLAR_ACCESS_TOKEN"]
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/json",
            "User-Agent": "ninoventures-filing-alerts/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def sec_get(url: str, ua: str):
    import gzip
    req = urllib.request.Request(
        url,
        headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw)

def parse_tickers(raw: str) -> list[str]:
    parts = re.split(r"[\s,;]+", (raw or "").upper())
    out, seen = [], set()
    for p in parts:
        p = re.sub(r"[^A-Z0-9.\-]", "", p)
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= MAX_TICKERS:
            break
    return out

def load_subscribers() -> list[dict]:
    # Active subscriptions for our product; custom fields live on related orders/checkouts.
    subs = []
    page = 1
    while True:
        data = polar(f"/v1/subscriptions/?product_id={PRODUCT_ID}&active=true&limit=100&page={page}")
        items = data.get("items") or []
        if not items:
            break
        for s in items:
            # Prefer custom_field_data on the subscription; fall back to latest order
            cfd = s.get("custom_field_data") or {}
            if not cfd.get("tickers") or not cfd.get("telegram_chat_id"):
                # pull recent orders for this subscription
                oid = s.get("id")
                orders = polar(f"/v1/orders/?subscription_id={oid}&limit=5")
                for o in orders.get("items") or []:
                    cfd = {**cfd, **(o.get("custom_field_data") or {})}
            chat = str(cfd.get("telegram_chat_id") or "").strip()
            tickers = parse_tickers(str(cfd.get("tickers") or ""))
            if chat and tickers and s.get("status") in {"active", "trialing"}:
                subs.append({
                    "subscription_id": s["id"],
                    "email": (s.get("customer") or {}).get("email"),
                    "telegram_chat_id": chat,
                    "tickers": tickers,
                })
        if page >= (data.get("pagination") or {}).get("max_page", page):
            break
        page += 1
    return subs

def telegram_send(token: str, chat_id: str, text: str):
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": False}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def main():
    ua = os.environ.get("SEC_USER_AGENT") or "NinoVenturesFilingAlerts/1.0 nino.ventures@agentmail.to"
    state_path = Path(os.environ.get("STATE_PATH", "state.json"))
    raw = state_path.read_text() if state_path.exists() else ""
    if raw.strip():
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            state = {"seen": {}, "initialized": False}
    else:
        state = {"seen": {}, "initialized": False}
    if not isinstance(state, dict):
        state = {"seen": {}, "initialized": False}
    state.setdefault("seen", {})
    state.setdefault("initialized", False)
    bot = os.environ.get("TELEGRAM_BOT_TOKEN") or ""

    try:
        subscribers = load_subscribers()
    except Exception as e:
        print("polar_error", type(e).__name__, e)
        subscribers = []
    print("subscribers", len(subscribers))

    # Build ticker -> chats
    watch: dict[str, list[dict]] = {}
    for s in subscribers:
        for t in s["tickers"]:
            watch.setdefault(t, []).append(s)

    if not watch:
        print("nothing_to_watch")
        state_path.write_text(json.dumps(state))
        return

    tickers_map = sec_get(SEC_TICKERS, ua)
    by_ticker = {}
    for row in tickers_map.values() if isinstance(tickers_map, dict) else []:
        by_ticker[str(row["ticker"]).upper()] = row

    sent = 0
    for ticker, subs in watch.items():
        meta = by_ticker.get(ticker)
        if not meta:
            print("unknown_ticker", ticker)
            continue
        cik = str(meta["cik_str"]).zfill(10)
        time.sleep(0.25)
        try:
            data = sec_get(SEC_SUBMISSIONS.format(cik=cik), ua)
        except Exception as e:
            print("sec_error", ticker, e)
            continue
        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        for i, form in enumerate(forms):
            if form not in FORMS:
                continue
            accession = recent["accessionNumber"][i]
            if accession in state["seen"]:
                continue
            filed = recent["filingDate"][i]
            # first run: only last 3 days to avoid flood
            age = (time.time() - time.mktime(time.strptime(filed, "%Y-%m-%d"))) / 86400
            if not state.get("initialized") and age > 3:
                state["seen"][accession] = True
                continue
            state["seen"][accession] = True
            acc = accession.replace("-", "")
            primary = recent["primaryDocument"][i]
            index_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{accession}-index.html"
            text = f"{ticker} {form} · {filed}\n{data.get('name','')}\n{index_url}"
            for s in subs:
                if not bot:
                    print("would_send", s["telegram_chat_id"], text.replace("\n", " | "))
                    continue
                try:
                    telegram_send(bot, s["telegram_chat_id"], text)
                    sent += 1
                    time.sleep(0.3)
                except Exception as e:
                    print("tg_error", s["telegram_chat_id"], e)
    state["initialized"] = True
    # prune
    keys = list(state["seen"].keys())
    if len(keys) > 8000:
        for k in keys[: len(keys) - 6000]:
            del state["seen"][k]
    state_path.write_text(json.dumps(state))
    print("sent", sent)

if __name__ == "__main__":
    main()
