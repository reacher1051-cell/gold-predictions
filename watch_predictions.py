#!/usr/bin/env python3
"""
Auto Prediction Watcher - gold-predictions repo ke liye
=========================================================
Ye script GitHub Actions par khud chalti hai (har 6 ghante):
  1. Instagram par @crypto_divyanshofficial ki nayi reels dekhti hai
  2. Jo reel kisi bari US news (CPI, NFP, FOMC, PPI...) ki prediction ho,
     us ki video download karke AWAZ ko text mein badalti hai
  3. Caption + awaz ke text se prediction (BUY / SELL) aur news ki date nikalti hai
  4. predictions.json khud update kar deti hai - app wahan se badge laga leti hai

Login: GitHub repo ke Secrets mein sirf aik cheez chahiye: IG_SESSIONID
(Instagram ki session cookie). Username/password ka rasta bhi rakha hai
(secrets IG_USERNAME / IG_PASSWORD) lekin session cookie zyada chalti hai.

Note: prediction sirf tab dali jati hai jab direction bilkul saaf ho
(bearish/bullish lafz ya sell/buy saaf mile). Dhundli surat mein reel
skip ho jati hai taaki app mein ghalat badge na lage.
"""

import json
import os
import re
import subprocess
import urllib.parse
import sys
from datetime import datetime, timedelta, timezone

TARGET_USERNAME = "crypto_divyanshofficial"
SOURCE_LABEL = "@crypto_divyanshofficial"
MAX_POST_AGE_DAYS = 5          # is se purani posts nahi parhi jati
MAX_POSTS_TO_CHECK = 12        # har run mein zyada se zyada itni posts

PRED_FILE = "predictions.json"
SEEN_FILE = "seen.json"

# ---------------------------------------------------------------- events ---
# Har news ke pehchane jane walay alfaaz -> app ke andar ke codes
EVENT_MAP = [
    (["nonfarm", "non-farm", "non farm", "nfp"], ["NFP"]),
    (["adp"], ["ADP"]),
    (["core cpi"], ["CORECPI_Y", "CORECPI_M"]),
    (["cpi", "inflation data", "inflation report"], ["CPI_Y", "CPI_M", "CORECPI_Y", "CORECPI_M"]),
    (["core ppi"], ["COREPPI_Y", "COREPPI_M"]),
    (["ppi", "producer price"], ["PPI_Y", "PPI_M", "COREPPI_Y", "COREPPI_M"]),
    (["core pce"], ["COREPCE_Y", "COREPCE_M"]),
    (["pce"], ["PCE_Y", "PCE_M", "COREPCE_Y", "COREPCE_M"]),
    (["fomc", "fed rate", "interest rate decision", "rate decision", "fed meeting"],
     ["FEDRATE"]),
    (["unemployment rate", "jobless rate"], ["UNRATE"]),
    (["jobless claims", "initial claims", "unemployment claims"], ["CLAIMS_I"]),
    (["retail sales"], ["RETAIL", "RETAIL_C"]),
    (["gdp"], ["GDP"]),
    (["ism manufacturing"], ["ISM_M"]),
    (["ism services", "ism non-manufacturing"], ["ISM_S"]),
    (["jolts", "job openings"], ["JOLTS"]),
    (["consumer confidence"], ["CONF"]),
    (["consumer sentiment"], ["SENT"]),
    (["durable goods"], ["DUR", "DUR_C"]),
]

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

BEARISH_WORDS = ["bearish", "sell", "gold down", "down jayega", "down jayga",
                 "neeche", "neechay", "gir sakta", "crash", "short"]
BULLISH_WORDS = ["bullish", "buy", "gold up", "up jayega", "up jayga",
                 "upar", "oopar", "long"]


def log(*a):
    print("[watch]", *a, flush=True)


# ------------------------------------------------------------ extraction ---
def find_event_codes(text):
    """Text mein news pehchano -> app codes ki list (na mile to khali list).
    Jo news sab se PEHLE zikr ho wahi asal mozu hai (caption ki headline);
    baad mein ane wala zikr (masalan 'NFP mazboot aya tha') wajah hoti hai, mozu nahi."""
    t = " " + text.lower() + " "
    best_pos, best_codes = None, []
    for keywords, codes in EVENT_MAP:
        for kw in keywords:
            pos = t.find(kw)
            if pos >= 0 and (best_pos is None or pos < best_pos):
                best_pos, best_codes = pos, codes
    return best_codes


def find_event_date(text, post_date):
    """'10 NOVEMBER' / '10th Nov' / '10/11' jaisi date text se nikalo.
    Na mile to None - aisi reel skip hogi (ghalat date par badge nahi lagega)."""
    t = text.lower()
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")\b", t)
    if not m:
        m = re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
        if m:
            day, mon = int(m.group(2)), MONTHS[m.group(1)]
        else:
            return None
    else:
        day, mon = int(m.group(1)), MONTHS[m.group(2)]
    year = post_date.year
    try:
        d = datetime(year, mon, day).date()
    except ValueError:
        return None
    if d < post_date - timedelta(days=1):   # guzri hui date -> agla saal
        d = datetime(year + 1, mon, day).date()
    return d.isoformat()


def find_direction(text):
    """Direction sirf tab jab saaf ho: bearish/bullish lafz ko sab se zyada wazan.
    Returns 'BUY' / 'SELL' / None."""
    t = text.lower()
    bear = sum(3 if w in ("bearish",) else 1 for w in BEARISH_WORDS if w in t)
    bull = sum(3 if w in ("bullish",) else 1 for w in BULLISH_WORDS if w in t)
    # 'sell stop' jaisi chart entry bhi SELL ka mazboot ishara hai
    if "sell stop" in t or "sell limit" in t:
        bear += 3
    if "buy stop" in t or "buy limit" in t:
        bull += 3
    if bear >= 3 and bear > bull:
        return "SELL"
    if bull >= 3 and bull > bear:
        return "BUY"
    return None


# -------------------------------------------------------------- instagram --
def web_session():
    """Seedha Instagram ki website-wali API ke liye session: sessionid cookie
    decode karke, poori cookie set ke sath, homepage se csrftoken le kar."""
    import requests
    raw = os.environ.get("IG_SESSIONID", "").strip()
    if not raw:
        raise RuntimeError("IG_SESSIONID secret khali hai - repo Secrets mein dalo.")
    sessionid = raw
    for _ in range(2):  # cookie value kabhi do-degree URL-encoded hoti hai
        dec = urllib.parse.unquote(sessionid)
        if dec == sessionid:
            break
        sessionid = dec
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36",
        "X-IG-App-ID": "936619743392459",
        "Accept-Language": "en-US,en;q=0.9",
    })
    sess.cookies.set("sessionid", sessionid, domain=".instagram.com")
    m = re.match(r"(\d+)", sessionid)  # sessionid ki shakal: userid:...
    if m:
        sess.cookies.set("ds_user_id", m.group(1), domain=".instagram.com")
        log("ds_user_id mila")
    try:
        r = sess.get("https://www.instagram.com/", timeout=30)
        log("homepage status:", r.status_code,
            "| csrftoken mila:", bool(sess.cookies.get("csrftoken")))
    except Exception as e:
        log("homepage fail:", e)
    csrf = sess.cookies.get("csrftoken")
    if csrf:
        sess.headers.update({"X-CSRFToken": csrf})
    return sess


def fetch_posts_web(sess):
    """Web API se target ki taaza posts: list of dict(shortcode, date, caption,
    video_url). Login fail ho to saaf error."""
    import requests
    base = "https://www.instagram.com/api/v1"
    r = sess.get(f"{base}/users/web_profile_info/",
                 params={"username": TARGET_USERNAME}, timeout=30)
    log("profile status:", r.status_code)
    if r.status_code != 200:
        raise RuntimeError(f"Web API ne profile nahi di (status {r.status_code}) - "
                           "sessionid ghalat/expire hai ya Instagram ne GitHub "
                           "server ko roka hai.")
    try:
        user = r.json()["data"]["user"]
    except Exception:
        raise RuntimeError("Profile ka jawab samajh nahi aya (JSON nahi mila).")
    uid = user["id"]
    log("profile OK:", user.get("username"), "| id:", uid)
    r = sess.get(f"{base}/feed/user/{uid}/", params={"count": 12}, timeout=30)
    log("feed status:", r.status_code)
    if r.status_code != 200:
        raise RuntimeError(f"Feed nahi mili (status {r.status_code}).")
    items = r.json().get("items", [])
    posts = []
    for it in items:
        cap = ((it.get("caption") or {}).get("text")) or ""
        vurl = None
        vv = it.get("video_versions") or []
        if vv:
            vurl = max(vv, key=lambda v: v.get("height", 0)).get("url")
        posts.append({
            "shortcode": it.get("code"),
            "date": datetime.fromtimestamp(it.get("taken_at", 0),
                                           tz=timezone.utc).date(),
            "caption": cap,
            "video_url": vurl,
        })
    log("posts mili:", len(posts))
    return posts


def fetch_posts_instaloader():
    """Purana rasta (backup): Instaloader GraphQL login."""
    import instaloader
    L = instaloader.Instaloader(download_pictures=False, download_videos=False,
                                download_video_thumbnails=False,
                                save_metadata=False, compress_json=False,
                                quiet=True)
    raw = os.environ.get("IG_SESSIONID", "").strip()
    sessionid = raw
    for _ in range(2):
        dec = urllib.parse.unquote(sessionid)
        if dec == sessionid:
            break
        sessionid = dec
    sess = L.context._session
    sess.cookies.set("sessionid", sessionid, domain=".instagram.com")
    m = re.match(r"(\d+)", sessionid)
    if m:
        sess.cookies.set("ds_user_id", m.group(1), domain=".instagram.com")
    who = L.test_login()
    if not who:
        raise RuntimeError("Instaloader login bhi fail.")
    log("Instaloader login OK:", who)
    profile = instaloader.Profile.from_username(L.context, TARGET_USERNAME)
    posts = []
    for post in profile.get_posts():
        posts.append({"shortcode": post.shortcode,
                      "date": post.date_utc.date(),
                      "caption": post.caption or "",
                      "video_url": post.video_url if post.is_video else None})
        if len(posts) >= MAX_POSTS_TO_CHECK:
            break
    return posts


def fetch_posts():
    """Pehle web API, fail ho to Instaloader - dono ki koshish ke baad error."""
    sess = web_session()
    try:
        return fetch_posts_web(sess)
    except Exception as e:
        log("web API fail:", e, "-> Instaloader wala rasta try kar raha hun")
    return fetch_posts_instaloader()


def transcribe(video_url, shortcode):
    """Video download -> audio -> Whisper (tiny) text. Fail ho to ''."""
    try:
        import requests
        mp4 = f"/tmp/{shortcode}.mp4"
        wav = f"/tmp/{shortcode}.wav"
        r = requests.get(video_url, timeout=120,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        with open(mp4, "wb") as f:
            f.write(r.content)
        subprocess.run(["ffmpeg", "-y", "-i", mp4, "-vn", "-ac", "1",
                        "-ar", "16000", wav],
                       check=True, capture_output=True)
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(wav, beam_size=1, vad_filter=True)
        return " ".join(s.text for s in segments)
    except Exception as e:
        log("transcribe fail", shortcode, "->", e)
        return ""


# ------------------------------------------------------------------- main --
def main():
    seen = {"shortcodes": []}
    if os.path.exists(SEEN_FILE):
        seen = json.load(open(SEEN_FILE))
    preds = {"source_name": "Divyansh Arora (@crypto_divyanshofficial)",
             "predictions": []}
    if os.path.exists(PRED_FILE):
        preds = json.load(open(PRED_FILE))

    posts = fetch_posts()
    now = datetime.now(timezone.utc)
    changed = False
    checked = 0

    for post in posts:
        if checked >= MAX_POSTS_TO_CHECK:
            break
        post_date = post["date"]
        if (now.date() - post_date).days > MAX_POST_AGE_DAYS:
            continue
        checked += 1
        code = post["shortcode"]
        if not code or code in seen["shortcodes"]:
            continue
        if not post["video_url"]:
            seen["shortcodes"].append(code)
            changed = True
            continue
        caption = post["caption"] or ""
        codes_hint = find_event_codes(caption)
        if not codes_hint:
            log("skip (news wali reel nahi):", code)
            seen["shortcodes"].append(code)
            changed = True
            continue

        speech = transcribe(post["video_url"], code) if post["video_url"] else ""
        full = caption + " " + speech
        codes = codes_hint or find_event_codes(full)
        date_str = find_event_date(full, post_date)
        direction = find_direction(full)
        log(code, "| codes:", codes, "| date:", date_str, "| dir:", direction)

        if codes and date_str and direction:
            preds["predictions"] = [
                p for p in preds["predictions"]
                if not (p["code"] in codes and p["date"] == date_str)]
            for c in codes:
                preds["predictions"].append(
                    {"code": c, "date": date_str, "direction": direction,
                     "source": SOURCE_LABEL})
            log("PREDICTION DALI:", codes, date_str, direction)
        else:
            log("skip (prediction saaf nahi):", code)
        seen["shortcodes"].append(code)
        changed = True

    if changed:
        json.dump(preds, open(PRED_FILE, "w"), indent=1)
        json.dump(seen, open(SEEN_FILE, "w"), indent=1)
        log("files update ho gayi")
    else:
        log("kuch naya nahi")


if __name__ == "__main__":
    main()
