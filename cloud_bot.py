import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

CREATEAD_URL = "https://api.rolimons.com/tradeads/v1/createad"
GITHUB_API = "https://api.github.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
DAILY_CAP = 60
MIN_INTERVAL_SECONDS = 23 * 60 + 30
MARK_SECONDS = 24 * 60
WORKER_SLEEP_LIMIT = 4 * 3600 + 50 * 60


def log(msg):
    print(msg, flush=True)


def now_utc():
    return datetime.now(timezone.utc)


def gh_request(method, path, data=None, pat=""):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(GITHUB_API + path, data=body, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "cloud-bot")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if pat:
        req.add_header("Authorization", "Bearer " + pat)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {}
    except Exception:
        return None, {}


def commit_state(full, state, pat):
    import base64
    data = {"message": "state sync (ad poster)", "content": base64.b64encode(
        json.dumps(state, indent=2).encode("utf-8")).decode("ascii")}
    st, cur = gh_request("GET", f"/repos/{full}/contents/state.json", pat=pat)
    if st == 200 and cur.get("sha"):
        data["sha"] = cur["sha"]
    st, _ = gh_request("PUT", f"/repos/{full}/contents/state.json", data, pat=pat)
    if st in (200, 201):
        log(f"State saved: day {state['date']} posts {state['count']}/60 last {state['last_post']}")
    else:
        log(f"WARNING: could not save state.json (HTTP {st}) - quota counting degraded")
    return st in (200, 201)


def load_state(full, pat):
    st, cur = gh_request("GET", f"/repos/{full}/contents/state.json", pat=pat)
    if st == 200:
        try:
            import base64
            return json.loads(base64.b64decode(cur["content"]).decode("utf-8"))
        except Exception:
            pass
    st, local = None, None
    try:
        with open("state.json", encoding="utf-8") as f:
            local = json.load(f)
    except Exception:
        local = None
    return local or {}


def fetch_entries(full, pat):
    st, cur = gh_request("GET", f"/repos/{full}/contents/playlist.json", pat=pat)
    if st == 200:
        try:
            import base64
            data = json.loads(base64.b64decode(cur["content"]).decode("utf-8"))
            keep = [e for e in data.get("entries", [])
                    if e.get("offer") or e.get("request") or e.get("tags")]
            if keep:
                return keep
        except Exception:
            return None
    return None


def fetch_trade(full, pat):
    st, cur = gh_request("GET", f"/repos/{full}/contents/trade.json", pat=pat)
    if st == 200:
        try:
            import base64
            return json.loads(base64.b64decode(cur["content"]).decode("utf-8"))
        except Exception:
            return None
    return None


def build_ids(items):
    ids = []
    for it in items:
        iid = it.get("item_id")
        if iid is None:
            continue
        ids.extend([iid] * max(1, int(it.get("quantity", 1))))
    return ids


def next_mark(now):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    k = int((now - midnight).total_seconds() // MARK_SECONDS) + 1
    return midnight + timedelta(seconds=MARK_SECONDS * k)


def chain_enabled():
    return (os.environ.get("CHAIN") == "true"
            and bool(os.environ.get("CRON_REPO"))
            and bool(os.environ.get("GH_PAT")))


def self_dispatch(cron_full, pat):
    branch = os.environ.get("CHAIN_REF", "main")
    st, _ = gh_request("POST", f"/repos/{cron_full}/actions/workflows/overnight.yml/dispatches",
                       {"ref": branch}, pat=pat)
    if st == 204:
        log("Chain: next slot dispatched")
        return True
    log(f"Chain dispatch failed (HTTP {st})")
    return False


def midnight_after(now):
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return nxt


def park_until_midnight(chain):
    if not chain:
        return
    remaining = (midnight_after(now_utc()) - now_utc()).total_seconds()
    if remaining > WORKER_SLEEP_LIMIT:
        remaining = WORKER_SLEEP_LIMIT
    log(f"Cap parked: runner idles {(remaining/60):.1f} min, then chain continues")
    time.sleep(max(remaining, 0) + 3)


def run_sequence():
    token = os.environ.get("ROLI_TOKEN", "").strip()
    pat = os.environ.get("GH_PAT", "").strip()
    cron_full = os.environ.get("CRON_REPO", "")
    try:
        player_id = int(os.environ.get("PLAYER_ID", "0"))
    except ValueError:
        player_id = 0
    full = os.environ.get("GH_REPO", "")
    chain = chain_enabled()

    if chain:
        nxt = next_mark(now_utc())
        wait = (nxt - now_utc()).total_seconds() + 5
        if wait > 15:
            log(f"Sleeping {wait/60:.1f} min until {nxt:%H:%M}z mark (exact 24-min cadence)")
            time.sleep(wait)
            log(f"Mark reached: posting at {now_utc():%H:%M:%S}z")
        elif wait > 0:
            time.sleep(wait)

    state = {"date": now_utc().strftime("%Y-%m-%d"), "count": 0, "last_post": ""}
    if pat and full:
        state = load_state(full, pat) or {}

    today = now_utc().strftime("%Y-%m-%d")
    if state.get("date") != today or not isinstance(state.get("count"), int):
        state = {"date": today, "count": 0, "last_post": state.get("last_post", "")}

    entries = []
    if pat and full:
        entries = fetch_entries(full, pat) or []

    trade = None
    idx = 0
    if entries:
        raw_idx = state.get("index", 0)
        idx = raw_idx if isinstance(raw_idx, int) and raw_idx >= 0 else 0
        idx %= len(entries)
        chosen = entries[idx]
        log(f"Playlist set {idx + 1}/{len(entries)}")
        trade = {"offer": chosen.get("offer", []), "request": chosen.get("request", []),
                 "tags": chosen.get("tags", []), "offer_robux": chosen.get("offer_robux") or 0}
    else:
        if pat and full:
            trade = fetch_trade(full, pat)
            if trade is None:
                log("WARNING: no trade.json in the private repo yet (sync it from the app).")
        if trade is None:
            try:
                with open("trade.json", encoding="utf-8") as f:
                    trade = json.load(f)
            except FileNotFoundError:
                log("FATAL: trade.json not found (private repo or local file).")
                return 2

    offer_ids = build_ids(trade.get("offer", []))
    request_ids = build_ids(trade.get("request", []))
    tags = [t for t in trade.get("tags", []) if t]
    try:
        robux = int(trade.get("offer_robux", 0) or 0)
    except (TypeError, ValueError):
        robux = 0

    log(f"Offer items: {offer_ids} | Request items: {request_ids} | Tags: {tags or '-'}")
    if len(offer_ids) > 4:
        log("FATAL: more than 4 offer items.")
        return 2
    if len(request_ids) + len(tags) > 4:
        log("FATAL: request items + tags must be <= 4 (they share the 4 request slots).")
        return 2
    if not (offer_ids or request_ids or tags):
        log("FATAL: nothing requested.")
        return 2
    if not token or not player_id:
        missing = [n for n, v in (("ROLI_TOKEN", token), ("PLAYER_ID", player_id)) if not v]
        log(f"FATAL: missing secret(s): {', '.join(missing)}.")
        return 2

    if state["count"] >= DAILY_CAP:
        log(f"Daily cap of {DAILY_CAP} posts reached ({state['count']} today) - skipping, quota resets at UTC midnight.")
        park_until_midnight(chain)
        return 0

    if state.get("last_post"):
        try:
            last = datetime.fromisoformat(state["last_post"])
            skipped = (now_utc() - last).total_seconds()
            if 0 <= skipped < MIN_INTERVAL_SECONDS:
                log(f"Only {skipped/60:.1f} min since last post - skipping this slot (cooldown safety).")
                return 0
        except Exception:
            pass

    payload = {
        "player_id": player_id,
        "offer_item_ids": offer_ids,
        "request_item_ids": request_ids,
        "request_tags": tags,
    }
    if robux > 0:
        payload["offer_robux"] = robux
    log("Payload: " + json.dumps(payload))

    if os.environ.get("DRY_RUN"):
        log("DRY RUN - not posting.")
        return 0

    req = urllib.request.Request(CREATEAD_URL, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Cookie", "_RoliVerification=" + token)
    req.add_header("User-Agent", UA)

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
            data["http_error"] = e.code
        except Exception:
            log(f"HTTP error {e.code}")
            data = {"success": False, "http_error": e.code}
    except Exception as e:
        log(f"Network error: {e}")
        return 1

    if data.get("success"):
        state["count"] += 1
        state["last_post"] = now_utc().isoformat()
        if entries:
            state["index"] = (idx + 1) % len(entries)
        log(f"AD POSTED TO ROLIMONS OK ({state['count']}/{DAILY_CAP} today)")
        if pat and full:
            commit_state(full, state, pat)
        return 0

    code = data.get("code")
    message = str(data.get("message", ""))
    low = message.lower()
    if code == 7105 or "cooldown" in low:
        log("Cooldown active - nothing to do.")
        return 0
    if "daily" in low or "limit" in low or "too many" in low:
        log(f"Rolimons limit reply: {message}. Not parking - retrying next slot.")
        return 1
    log(f"Ad rejected (code {code}): {message}.")
    if entries:
        state["index"] = (idx + 1) % len(entries)
        log("Playlist advanced to the next set for the next slot.")
        if pat and full:
            commit_state(full, state, pat)
    return 1


def main():
    try:
        return run_sequence()
    finally:
        if chain_enabled():
            self_dispatch(os.environ["CRON_REPO"], os.environ["GH_PAT"])


if __name__ == "__main__":
    sys.exit(main())
