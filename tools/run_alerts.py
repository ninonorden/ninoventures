#!/usr/bin/env python3
"""Filing Alerts hosted runner.

1) Process Telegram updates: /start <CHECKOUT_ID> links chat_id after payment
   (and /tickers to set/update the watch list). Old checkout custom-field
   telegram_chat_id still works in parallel.
2) Pull active/trialing Polar subscriptions for the Filing Alerts product.
3) Poll SEC EDGAR for new Form 4 / 8-K / 10-Q / 10-K.
4) Send Telegram messages when TELEGRAM_BOT_TOKEN is set.

Env:
  POLAR_ACCESS_TOKEN   organization token
  TELEGRAM_BOT_TOKEN   BotFather token
  SEC_USER_AGENT       e.g. 'NinoVenturesFilingAlerts/1.0 nino.ventures@agentmail.to'
  PRODUCT_ID           default 4d76b65f-9c86-4ae5-9095-404f810fee7d
  STATE_PATH           default ./state.json
"""
from __future__ import annotations

import json, os, re, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
from typing import Any, Callable

PRODUCT_ID = os.environ.get("PRODUCT_ID", "4d76b65f-9c86-4ae5-9095-404f810fee7d")
API = "https://api.polar.sh"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
FORMS = {"4", "4/A", "8-K", "8-K/A", "10-Q", "10-K"}
MAX_TICKERS = 25
ACTIVE_STATUSES = {"active", "trialing"}
PAID_CHECKOUT_STATUSES = {"succeeded"}
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
START_RE = re.compile(r"^/start(?:@\w+)?(?:\s+(.+))?$", re.I | re.S)
TICKERS_CMD_RE = re.compile(r"^/tickers(?:@\w+)?(?:\s+(.+))?$", re.I | re.S)
HELP_RE = re.compile(r"^/(help|status)(?:@\w+)?\s*$", re.I)

HttpFn = Callable[[str, bytes | None, dict[str, str] | None, str], Any]


def default_http(url: str, data: bytes | None = None, headers: dict[str, str] | None = None, method: str = "GET") -> Any:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        if not raw:
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "json" in ctype or raw[:1] in (b"{", b"["):
            return json.loads(raw)
        return raw


def polar(path: str, http: HttpFn = default_http):
    tok = os.environ["POLAR_ACCESS_TOKEN"]
    return http(
        API + path,
        None,
        {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/json",
            "User-Agent": "ninoventures-filing-alerts/1.0",
        },
        "GET",
    )


def sec_get(url: str, ua: str, http: HttpFn = default_http):
    import gzip

    raw_or_json = http(
        url,
        None,
        {"User-Agent": ua, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"},
        "GET",
    )
    if isinstance(raw_or_json, (dict, list)):
        return raw_or_json
    raw = raw_or_json
    if isinstance(raw, bytes) and (raw[:2] == b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    if isinstance(raw, bytes):
        return json.loads(raw)
    return raw_or_json


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


def normalize_checkout_id(payload: str) -> str | None:
    """Accept UUID with hyphens, or 32-hex (Telegram-safe) form."""
    p = (payload or "").strip()
    if not p:
        return None
    # Strip accidental query wrappers like checkout_id=...
    if "checkout_id=" in p.lower():
        p = re.split(r"checkout_id=", p, flags=re.I)[-1]
        p = re.split(r"[&\s]", p)[0]
    p = p.strip().strip("<>").strip()
    if UUID_RE.match(p):
        return p.lower()
    if HEX32_RE.match(p):
        p = p.lower()
        return f"{p[:8]}-{p[8:12]}-{p[12:16]}-{p[16:20]}-{p[20:]}"
    return None


def load_state(path: Path) -> dict:
    raw = path.read_text() if path.exists() else ""
    if raw.strip():
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("seen", {})
    state.setdefault("initialized", False)
    state.setdefault("links", {})  # subscription_id -> {telegram_chat_id, tickers?, checkout_id, ...}
    state.setdefault("checkout_links", {})  # checkout_id -> subscription_id
    state.setdefault("chat_subscriptions", {})  # chat_id -> subscription_id
    state.setdefault("telegram_offset", 0)
    return state


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state))


def telegram_api(token: str, method: str, payload: dict | None = None, http: HttpFn = default_http):
    body = json.dumps(payload or {}).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    return http(
        f"https://api.telegram.org/bot{token}/{method}",
        body,
        headers,
        "POST" if body is not None else "GET",
    )


def telegram_send(token: str, chat_id: str, text: str, http: HttpFn = default_http):
    return telegram_api(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        http=http,
    )


def checkout_product_ok(checkout: dict) -> bool:
    pid = checkout.get("product_id")
    if pid == PRODUCT_ID:
        return True
    product = checkout.get("product") or {}
    if isinstance(product, dict) and product.get("id") == PRODUCT_ID:
        return True
    for p in checkout.get("products") or []:
        if isinstance(p, dict) and p.get("id") == PRODUCT_ID:
            return True
    return False


def resolve_subscription_for_checkout(checkout: dict, http: HttpFn = default_http) -> dict | None:
    sid = checkout.get("subscription_id")
    if sid:
        try:
            return polar(f"/v1/subscriptions/{sid}", http=http)
        except Exception as e:
            print("polar_sub_get_error", sid, type(e).__name__, e)
    customer_id = checkout.get("customer_id")
    if not customer_id:
        return None
    try:
        data = polar(
            f"/v1/subscriptions/?product_id={PRODUCT_ID}&customer_id={customer_id}&limit=10",
            http=http,
        )
    except Exception as e:
        print("polar_sub_list_error", type(e).__name__, e)
        return None
    items = data.get("items") or []
    # Prefer active/trialing
    for s in items:
        if s.get("status") in ACTIVE_STATUSES:
            return s
    return items[0] if items else None


def merge_custom_fields(*sources: dict | None) -> dict:
    out: dict = {}
    for s in sources:
        if not s:
            continue
        cfd = s.get("custom_field_data") if "custom_field_data" in s else s
        if isinstance(cfd, dict):
            for k, v in cfd.items():
                if v is not None and v != "":
                    out[k] = v
    return out


def link_from_checkout(
    checkout_id: str,
    chat_id: str,
    state: dict,
    http: HttpFn = default_http,
) -> tuple[str, dict | None]:
    """Look up Polar checkout, store chat_id link. Returns (reply_text, link_record|None)."""
    try:
        checkout = polar(f"/v1/checkouts/{checkout_id}", http=http)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return (
                "I could not find that checkout. Open the success link from Polar again, "
                "or reply with /help.",
                None,
            )
        return (f"Polar lookup failed ({e.code}). Try again in a few minutes.", None)
    except Exception as e:
        return (f"Polar lookup failed ({type(e).__name__}). Try again in a few minutes.", None)

    if not checkout_product_ok(checkout):
        return ("That checkout is not for Filing Alerts.", None)

    status = checkout.get("status")
    if status not in PAID_CHECKOUT_STATUSES:
        return (
            f"Checkout status is '{status}'. Finish payment (or wait a moment for Polar to "
            "mark it succeeded), then tap the success link again.",
            None,
        )

    sub = resolve_subscription_for_checkout(checkout, http=http)
    if not sub:
        return (
            "Payment looks good, but I cannot see the subscription yet. Wait ~1 minute and "
            "tap Start on the success link again.",
            None,
        )

    if sub.get("status") not in ACTIVE_STATUSES:
        return (
            f"Subscription status is '{sub.get('status')}'. Alerts only run while active or trialing.",
            None,
        )

    cfd = merge_custom_fields(checkout, sub)
    # Also pull recent orders for tickers if needed
    if not cfd.get("tickers"):
        try:
            orders = polar(f"/v1/orders/?subscription_id={sub['id']}&limit=5", http=http)
            for o in orders.get("items") or []:
                cfd = merge_custom_fields({"custom_field_data": cfd}, o)
        except Exception as e:
            print("polar_orders_error", type(e).__name__, e)

    tickers = parse_tickers(str(cfd.get("tickers") or ""))
    # Prefer tickers already stored on this subscription link
    prev = (state.get("links") or {}).get(sub["id"]) or {}
    if not tickers and prev.get("tickers"):
        tickers = parse_tickers(
            ",".join(prev["tickers"]) if isinstance(prev["tickers"], list) else str(prev["tickers"])
        )

    record = {
        "telegram_chat_id": str(chat_id),
        "checkout_id": checkout_id,
        "subscription_id": sub["id"],
        "email": (sub.get("customer") or {}).get("email") or checkout.get("customer_email"),
        "tickers": tickers,
        "linked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": sub.get("status"),
    }
    state.setdefault("links", {})[sub["id"]] = record
    state.setdefault("checkout_links", {})[checkout_id] = sub["id"]
    state.setdefault("chat_subscriptions", {})[str(chat_id)] = sub["id"]

    if tickers:
        watching = ", ".join(tickers)
        return (
            f"Linked. Watching: {watching}\n"
            "Alerts run about every 10 minutes for Form 4 / 8-K / 10-Q / 10-K.\n"
            "Change tickers anytime with /tickers NVDA,AAPL (max 25).",
            record,
        )
    return (
        "Linked, but no tickers yet.\n"
        "Send /tickers NVDA,AAPL (comma-separated, max 25) to start alerts.",
        record,
    )


def handle_tickers_command(chat_id: str, raw: str, state: dict) -> str:
    sid = (state.get("chat_subscriptions") or {}).get(str(chat_id))
    if not sid or sid not in (state.get("links") or {}):
        return (
            "No linked subscription on this chat yet. Pay on Polar, then open the success "
            "link (t.me/FilingAlertsNV_bot?start=<CHECKOUT_ID>)."
        )
    tickers = parse_tickers(raw or "")
    if not tickers:
        return "Usage: /tickers NVDA,AAPL,MSFT (max 25)."
    state["links"][sid]["tickers"] = tickers
    return f"Updated. Watching: {', '.join(tickers)}"


def handle_status(chat_id: str, state: dict) -> str:
    sid = (state.get("chat_subscriptions") or {}).get(str(chat_id))
    link = (state.get("links") or {}).get(sid or "")
    if not link:
        return (
            "Not linked yet.\n"
            "1) Buy / start trial at ninonorden.github.io/ninoventures/filing-alerts.html\n"
            "2) Open the Polar success link to this bot (/start <CHECKOUT_ID>)."
        )
    tickers = link.get("tickers") or []
    watching = ", ".join(tickers) if tickers else "(none — send /tickers …)"
    return (
        f"Linked subscription: {link.get('subscription_id')}\n"
        f"Status: {link.get('status', 'unknown')}\n"
        f"Watching: {watching}"
    )


HELP_TEXT = (
    "Filing Alerts — SEC Form 4 / 8-K / 10-Q / 10-K → Telegram.\n\n"
    "After Polar checkout, open the success link so I get /start <CHECKOUT_ID> "
    "and bind this chat.\n\n"
    "Commands:\n"
    "/start <CHECKOUT_ID> — link after payment\n"
    "/tickers NVDA,AAPL — set or change tickers (max 25)\n"
    "/status — show link + tickers\n"
    "/help — this text"
)


def process_telegram_updates(token: str, state: dict, http: HttpFn = default_http) -> int:
    """Poll getUpdates and handle /start, /tickers, /help. Returns handled count."""
    if not token:
        return 0
    offset = int(state.get("telegram_offset") or 0)
    # Hosted poller uses getUpdates; clear any stale webhook so we are not blocked (409).
    try:
        telegram_api(token, "deleteWebhook", {"drop_pending_updates": False}, http=http)
    except Exception as e:
        print("tg_deleteWebhook_error", type(e).__name__, e)
    try:
        data = telegram_api(
            token,
            "getUpdates",
            {"offset": offset, "timeout": 0, "allowed_updates": ["message"]},
            http=http,
        )
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print("tg_getUpdates_error", e.code, body)
        if e.code == 409:
            print(
                "tg_webhook_conflict: deleteWebhook so the Actions poller can use getUpdates"
            )
        return 0
    except Exception as e:
        print("tg_getUpdates_error", type(e).__name__, e)
        return 0

    if not isinstance(data, dict) or not data.get("ok"):
        print("tg_getUpdates_bad", data)
        return 0

    handled = 0
    for upd in data.get("result") or []:
        upd_id = upd.get("update_id")
        if upd_id is not None:
            state["telegram_offset"] = int(upd_id) + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            continue
        chat_id_s = str(chat_id)

        try:
            if START_RE.match(text):
                m = START_RE.match(text)
                payload = (m.group(1) or "").strip() if m else ""
                if not payload:
                    telegram_send(token, chat_id_s, HELP_TEXT, http=http)
                    handled += 1
                    continue
                cid = normalize_checkout_id(payload)
                if not cid:
                    telegram_send(
                        token,
                        chat_id_s,
                        "That does not look like a Polar checkout id. "
                        "Use the success link from Polar (starts with a UUID).",
                        http=http,
                    )
                    handled += 1
                    continue
                reply, _ = link_from_checkout(cid, chat_id_s, state, http=http)
                telegram_send(token, chat_id_s, reply, http=http)
                handled += 1
            elif TICKERS_CMD_RE.match(text):
                m = TICKERS_CMD_RE.match(text)
                raw = (m.group(1) or "") if m else ""
                reply = handle_tickers_command(chat_id_s, raw, state)
                telegram_send(token, chat_id_s, reply, http=http)
                handled += 1
            elif HELP_RE.match(text):
                if text.lower().startswith("/status"):
                    telegram_send(token, chat_id_s, handle_status(chat_id_s, state), http=http)
                else:
                    telegram_send(token, chat_id_s, HELP_TEXT, http=http)
                handled += 1
        except Exception as e:
            print("tg_handle_error", chat_id_s, type(e).__name__, e)
            try:
                telegram_send(
                    token,
                    chat_id_s,
                    "Something went wrong handling that. Try again in a few minutes.",
                    http=http,
                )
            except Exception:
                pass
    return handled


def load_subscribers(state: dict, http: HttpFn = default_http) -> list[dict]:
    """Active/trialing subs; chat id from state link OR Polar custom field (old flow)."""
    subs: list[dict] = []
    page = 1
    links = state.get("links") or {}
    while True:
        data = polar(
            f"/v1/subscriptions/?product_id={PRODUCT_ID}&active=true&limit=100&page={page}",
            http=http,
        )
        items = data.get("items") or []
        if not items:
            break
        for s in items:
            if s.get("status") not in ACTIVE_STATUSES:
                continue
            cfd = dict(s.get("custom_field_data") or {})
            if not cfd.get("tickers") or not cfd.get("telegram_chat_id"):
                oid = s.get("id")
                try:
                    orders = polar(f"/v1/orders/?subscription_id={oid}&limit=5", http=http)
                    for o in orders.get("items") or []:
                        cfd = {**cfd, **(o.get("custom_field_data") or {})}
                except Exception as e:
                    print("polar_orders_error", type(e).__name__, e)

            link = links.get(s["id"]) or {}
            chat = str(
                link.get("telegram_chat_id") or cfd.get("telegram_chat_id") or ""
            ).strip()
            tickers_raw = link.get("tickers")
            if isinstance(tickers_raw, list) and tickers_raw:
                tickers = [str(t).upper() for t in tickers_raw][:MAX_TICKERS]
            else:
                tickers = parse_tickers(str(tickers_raw or cfd.get("tickers") or ""))

            if chat and tickers:
                # Keep chat_subscriptions index fresh for /tickers
                state.setdefault("chat_subscriptions", {})[chat] = s["id"]
                # Refresh status on stored link
                if link:
                    link["status"] = s.get("status")
                    if not link.get("tickers"):
                        link["tickers"] = tickers
                    state["links"][s["id"]] = link
                subs.append(
                    {
                        "subscription_id": s["id"],
                        "email": (s.get("customer") or {}).get("email"),
                        "telegram_chat_id": chat,
                        "tickers": tickers,
                    }
                )
        pagination = data.get("pagination") or {}
        max_page = pagination.get("max_page", page)
        if page >= max_page:
            break
        page += 1
    return subs


def main():
    ua = os.environ.get("SEC_USER_AGENT") or "NinoVenturesFilingAlerts/1.0 nino.ventures@agentmail.to"
    state_path = Path(os.environ.get("STATE_PATH", "state.json"))
    state = load_state(state_path)
    bot = os.environ.get("TELEGRAM_BOT_TOKEN") or ""

    # 1) Link chats from Telegram /start <CHECKOUT_ID> (and /tickers)
    try:
        n = process_telegram_updates(bot, state)
        print("telegram_handled", n)
    except Exception as e:
        print("telegram_error", type(e).__name__, e)

    # 2) Subscribers (Polar custom fields + linked state)
    try:
        subscribers = load_subscribers(state)
    except Exception as e:
        print("polar_error", type(e).__name__, e)
        subscribers = []
    print("subscribers", len(subscribers))

    watch: dict[str, list[dict]] = {}
    for s in subscribers:
        for t in s["tickers"]:
            watch.setdefault(t, []).append(s)

    if not watch:
        print("nothing_to_watch")
        save_state(state_path, state)
        return

    tickers_map = sec_get(SEC_TICKERS, ua)
    by_ticker = {}
    for row in tickers_map.values() if isinstance(tickers_map, dict) else []:
        by_ticker[str(row["ticker"]).upper()] = row

    sent = 0
    for ticker, sub_list in watch.items():
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
            age = (time.time() - time.mktime(time.strptime(filed, "%Y-%m-%d"))) / 86400
            if not state.get("initialized") and age > 3:
                state["seen"][accession] = True
                continue
            state["seen"][accession] = True
            acc = accession.replace("-", "")
            index_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{accession}-index.html"
            text = f"{ticker} {form} · {filed}\n{data.get('name','')}\n{index_url}"
            for s in sub_list:
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
    keys = list(state["seen"].keys())
    if len(keys) > 8000:
        for k in keys[: len(keys) - 6000]:
            del state["seen"][k]
    save_state(state_path, state)
    print("sent", sent)


if __name__ == "__main__":
    main()
