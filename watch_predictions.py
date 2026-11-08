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
def get_loader():
    """Instagram session banao: sirf sessionid cookie se login aksar adhoora
    rehta hai - Instaloader ke GraphQL sawalon ko csrftoken + ds_user_id bhi
    chahiye. Is liye pehle homepage khol kar server se baqi cookies lo."""
    import requests
    import instaloader
    L = instaloader.Instaloader(download_pictures=False, download_videos=False,
                                download_video_thumbnails=False,
                                save_metadata=False, compress_json=False,
                                quiet=True)
    raw = os.environ.get("IG_SESSIONID", "").strip()
    if not raw:
        raise RuntimeError("IG_SESSIONID secret khali hai - repo Secrets mein dalo.")
    sessionid = raw
    for _ in range(2):  # cookies mein value kabhi do-degree encoded hoti hai
        dec = urllib.parse.unquote(sessionid)
        if dec == sessionid:
            break
        sessionid = dec
    sess = L.context._session
    sess.cookies.set("sessionid", sessionid, domain=".instagram.com")
    # ds_user_id sessionid ke pehle hisse mein hota hai (userid:... wali shakal)
    m = re.match(r"(\d+)", sessionid)
    if m:
        sess.cookies.set("ds_user_id", m.group(1), domain=".instagram.com")
        log("ds_user_id mila:", m.group(1))
    # Homepage se csrftoken / mid cookies lo
    try:
        sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/126.0.0.0 Safari/537.36"})
        sess.get("https://www.instagram.com/", timeout=30)
    except Exception as e:
        log("homepage cookie fetch fail:", e)
    who = L.test_login()
    if who:
        log("Instagram login OK:", who)
        return L
    # Aakhri koshish: password wala rasta agar secrets diye hon
    username = os.environ.get("IG_USERNAME", "").strip()
    password = os.environ.get("IG_PASSWORD", "").strip()
    if username and password:
        L2 = instaloader.Instaloader(quiet=True)
        L2.login(username, password)
        log("Instagram login OK (password):", username)
        return L2
    raise RuntimeError("Login nahi hua: ya sessionid ghalat/expire hai, ya Instagram ne "
                       "GitHub ke server ko roka hai. Log ki pehli lines dekho.")


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

    L = get_loader()
    import instaloader
    profile = instaloader.Profile.from_username(L.context, TARGET_USERNAME)
    now = datetime.now(timezone.utc)
    changed = False
    checked = 0

    for post in profile.get_posts():
        if checked >= MAX_POSTS_TO_CHECK:
            break
        post_date = post.date_utc.date()
        if (now.date() - post_date).days > MAX_POST_AGE_DAYS:
            continue
        checked += 1
        code = post.shortcode
        if code in seen["shortcodes"]:
            continue
        if not post.is_video:
            seen["shortcodes"].append(code)
            changed = True
            continue
        caption = post.caption or ""
        codes_hint = find_event_codes(caption)
        if not codes_hint:
            log("skip (news wali reel nahi):", code)
            seen["shortcodes"].append(code)
            changed = True
            continue

        speech = transcribe(post.video_url, code) if post.video_url else ""
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
