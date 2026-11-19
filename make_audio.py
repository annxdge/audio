#!/usr/bin/env python3
"""
Studio Anaar · automatic narration in the Fola voice.

What it does, every time it runs:
  1. Reads the blog's feed and finds the newest posts.
  2. For every post that has no <post-address>.mp3 in this folder yet, it sends the post's text
     to Google's Gemini text-to-speech with your Fola voice and saves the result as an MP3.
  3. Any text file in texts/ (for example texts/p-about-v2.txt) gets the same treatment.

The Listen button on the site already looks for these MP3s, by the post's address:
  anadegenaar.com/2026/11/some-post.html   ->   2026-11-some-post.mp3
so a new post gets the Fola voice as soon as its MP3 appears here (the workflow runs every hour).

Settings (environment variables, all optional except the key):
  GEMINI_API_KEY   the key from Google AI Studio (set it as a GitHub secret)
  VOICE_NAME       the name of the voice in AI Studio              default: Fola
  VOICE_ID         the voice's id (voice_...), if the name is not found
  TTS_MODEL        the text-to-speech model(s), separated by commas; the first that works is used
  TTS_STYLE        a style note, e.g. "calm, warm, unhurried"      default: none
  BLOG_FEED        the blog feed                                   default: https://www.anadegenaar.com/feeds/posts/default
  LOOKBACK         how many of the newest posts to check            default: 25   (0 = all)
  MAX_NEW          most recordings to make in one run               default: 4
  OUT_DIR          where the MP3s go                                default: the folder of this file
Run it by hand:  python make_audio.py --dry-run    (shows what it would make, spends nothing)
"""
import base64, io, json, os, re, subprocess, sys, tempfile, time, urllib.parse, urllib.request, wave
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.environ.get("BLOG_FEED", "https://www.anadegenaar.com/feeds/posts/default")
VOICE_NAME = os.environ.get("VOICE_NAME", "Fola")
VOICE_ID = os.environ.get("VOICE_ID", "").strip()
MODELS = [m.strip() for m in os.environ.get("TTS_MODEL",
          "gemini-3.8-flash-tts,gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts").split(",") if m.strip()]
STYLE = os.environ.get("TTS_STYLE", "").strip()
LOOKBACK = int(os.environ.get("LOOKBACK", "25"))
MAX_NEW = int(os.environ.get("MAX_NEW", "4"))
OUT_DIR = os.environ.get("OUT_DIR", HERE)
TEXT_DIR = os.path.join(HERE, "texts")
CHUNK = 2800            # characters per request: kept small to stay well inside the model's limits
RATE = 24000            # Gemini TTS returns 24 kHz, mono, 16-bit


# ---------------------------------------------------------------- the blog
def slug_from_url(url):
    """https://www.anadegenaar.com/2026/11/some-post.html  ->  2026-11-some-post  (the same rule the site uses)"""
    path = urllib.parse.urlparse(url).path.strip("/")
    path = re.sub(r"\.html$", "", path)
    return path.replace("/", "-")


class _Text(HTMLParser):
    """Keeps the words a reader would read: paragraphs, headings, quotes, list items. Skips pictures, captions, scripts."""
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "div", "br"}
    SKIP = {"script", "style", "figcaption", "iframe", "noscript", "svg", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [""], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("")

    def handle_data(self, data):
        if not self.skip:
            self.out[-1] += data


def html_to_paragraphs(html):
    p = _Text()
    p.feed(html or "")
    paras = [re.sub(r"\s+", " ", t).strip() for t in p.out]
    return [t for t in paras if t]


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "studioanaar-audio/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def blog_posts():
    """Newest first: [{slug, title, paragraphs}]"""
    posts, start = [], 1
    while True:
        url = "%s?alt=json&max-results=50&start-index=%d" % (FEED, start)
        feed = fetch_json(url).get("feed", {})
        entries = feed.get("entry", [])
        for e in entries:
            link = next((l["href"] for l in e.get("link", []) if l.get("rel") == "alternate"), None)
            if not link:
                continue
            slug = slug_from_url(link)
            if not re.match(r"^\d{4}-\d{2}-", slug):
                continue
            body = (e.get("content") or e.get("summary") or {}).get("$t", "")
            title = (e.get("title") or {}).get("$t", "").strip()
            paras = html_to_paragraphs(body)
            if title:
                paras.insert(0, title + ".")
            posts.append({"slug": slug, "title": title, "paragraphs": paras})
        if len(entries) < 50 or (LOOKBACK and len(posts) >= LOOKBACK):
            break
        start += 50
    return posts[:LOOKBACK] if LOOKBACK else posts


def text_files():
    out = []
    if os.path.isdir(TEXT_DIR):
        for name in sorted(os.listdir(TEXT_DIR)):
            if name.endswith(".txt"):
                raw = open(os.path.join(TEXT_DIR, name), encoding="utf-8").read()
                paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", raw)]
                out.append({"slug": name[:-4], "title": name, "paragraphs": [p for p in paras if p]})
    return out


# ---------------------------------------------------------------- the voice
def chunks(paragraphs):
    """Groups paragraphs into pieces of up to CHUNK characters; a long paragraph is split at sentence ends."""
    pieces, cur = [], ""
    for para in paragraphs:
        sentences = re.findall(r"[^.!?]+[.!?]+[\"'”’)]*\s*|[^.!?]+$", para) or [para]
        for s in sentences:
            if cur and len(cur) + len(s) > CHUNK:
                pieces.append(cur.strip()); cur = ""
            cur += s
        cur += "\n\n"
    if cur.strip():
        pieces.append(cur.strip())
    return pieces


def find_voice(client):
    """The voice to speak with: VOICE_ID when set, otherwise the voice named VOICE_NAME."""
    if VOICE_ID:
        return VOICE_ID
    try:
        resp = client.voices.list(type_=["prompted", "replicated"])
        seen = []
        for v in (getattr(resp, "voices", None) or []):
            name = (getattr(v, "display_name", "") or "").strip()
            vid = getattr(v, "id", "") or ""
            seen.append("%s (%s)" % (name, vid))
            if name.lower() == VOICE_NAME.lower() or vid == VOICE_NAME:
                print("Using the voice %r (%s)" % (name, vid))
                return vid
        print("No voice called %r. Voices on this key: %s" % (VOICE_NAME, ", ".join(seen[:40]) or "none"))
    except Exception as err:
        print("Could not list the voices (%s)" % str(err)[:200])
    print("Using %r as the voice itself." % VOICE_NAME)
    return VOICE_NAME


_chosen = {"i": 0}            # which model in MODELS is working


def synthesize(client, voice, text):
    """One piece of text -> raw 16-bit mono PCM at 24 kHz. Tries another model if one is unavailable."""
    content = {"type": "text", "text": text}
    if STYLE:
        content["annotations"] = [{"type": "speech_metadata", "style": STYLE}]
    last = ""
    while _chosen["i"] < len(MODELS):
        model, moved = MODELS[_chosen["i"]], False
        for attempt in range(4):
            try:
                interaction = client.interactions.create(
                    model=model,
                    input=[{"type": "user_input", "content": [content]}],
                    response_format={"type": "audio"},
                    generation_config={"speech_config": [{"voice": voice}]},
                )
                data = interaction.output_audio.data
                if isinstance(data, str):
                    data = base64.b64decode(data)
                if data[:4] == b"RIFF":                  # a complete WAV: take the samples out of it
                    with wave.open(io.BytesIO(data)) as w:
                        return w.readframes(w.getnframes())
                return data                              # already raw samples
            except Exception as err:
                last = str(err)
                low = last.lower()
                if "not found" in low or "404" in low[:80]:
                    print("   %s is not available on this key; trying the next model" % model)
                    moved = True
                    break
                if ("invalid_argument" in low or "permission" in low or "unauthorized" in low
                        or "api key" in low or "400" in low[:80]):
                    raise RuntimeError("Google refused the request: %s" % last[:400])
                wait = 8 * (2 ** attempt)
                print("   attempt %d failed (%s); waiting %ds" % (attempt + 1, last[:160], wait))
                time.sleep(wait)
        if not moved:
            raise RuntimeError("Google's text-to-speech kept failing: %s" % last[:300])
        _chosen["i"] += 1
    raise RuntimeError("None of these models worked (%s). Last answer: %s" % (", ".join(MODELS), last[:300]))


def to_mp3(pcm, path):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    with wave.open(tmp.name, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp.name, "-ac", "1", "-b:a", "64k",
                        "-codec:a", "libmp3lame", path], check=True)
    finally:
        os.unlink(tmp.name)


def narrate(client, voice, item, path):
    silence = b"\x00\x00" * int(RATE * 0.5)
    pcm = b""
    for i, piece in enumerate(chunks(item["paragraphs"])):
        print("   part %d (%d characters)" % (i + 1, len(piece)))
        pcm += (silence if pcm else b"") + synthesize(client, voice, piece)
    to_mp3(pcm, path)


# ---------------------------------------------------------------- main
def main():
    dry = "--dry-run" in sys.argv
    wanted = []
    try:
        wanted += text_files()
        wanted += blog_posts()
    except Exception as err:
        print("Could not read the blog feed:", err)
        if not wanted:
            return 1
    todo, seen = [], set()
    for item in wanted:
        if item["slug"] in seen:
            continue
        seen.add(item["slug"])
        if os.path.exists(os.path.join(OUT_DIR, item["slug"] + ".mp3")):
            continue
        if len(" ".join(item["paragraphs"])) < 80:
            print("skipping %s: almost no text" % item["slug"])
            continue
        todo.append(item)
    print("%d recording(s) missing: %s" % (len(todo), ", ".join(i["slug"] for i in todo) or "none"))
    if dry or not todo:
        return 0
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY is not set.")
    from google import genai
    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    voice = find_voice(client)
    made = 0
    for item in todo[:MAX_NEW]:
        print("Recording", item["slug"])
        try:
            narrate(client, voice, item, os.path.join(OUT_DIR, item["slug"] + ".mp3"))
            made += 1
        except Exception as err:
            print("   failed:", err)
    print("made %d recording(s)" % made)
    return 0 if made or not todo else 1


if __name__ == "__main__":
    sys.exit(main())
