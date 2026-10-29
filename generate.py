#!/usr/bin/env python3
"""Reads scripts.md (voiceover + video prompt for each video) and builds
vertical 9:16 videos: AI voice + AI images made from the video prompt +
slow zoom + word-by-word captions."""
import asyncio
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

W, H, FPS = 1080, 1920, 30
TTS_RATE_DEFAULT = os.getenv("TTS_RATE", "+0%")
NICHE_FILTER = (os.getenv("NICHE") or "").strip().lower()
LIMIT = int(os.getenv("LIMIT") or 0)
IMAGE_URL = os.getenv(
    "IMAGE_URL_TEMPLATE",
    "https://image.pollinations.ai/prompt/{prompt}?width=1080&height=1920&nologo=true&seed={seed}",
)

# Per-niche look, voice and hashtags (keys = niche names in scripts.md)
NICHES = {
    "Tiny Chaos Studio": dict(style="3D animated cartoon, colorful, family-friendly, soft studio lighting",
                              voice="en-US-GuyNeural", rate="+5%", tags="#shorts #funny #animation #comedy"),
    "Midnight Mystery Files": dict(style="dark cinematic photography, moody low light, film grain, eerie atmosphere",
                                   voice="en-GB-RyanNeural", rate="-8%", tags="#shorts #horror #mystery #scarystories"),
    "Auto Fix Explained": dict(style="realistic automotive photography, clean workshop lighting, macro detail",
                               voice="en-US-ChristopherNeural", rate="+0%", tags="#shorts #cars #mechanic #carcare"),
    "BlockWorld Stories": dict(style="blocky voxel game world, pixel textures, bright game lighting",
                               voice="en-US-AndrewNeural", rate="+5%", tags="#shorts #minecraft #gaming #survival"),
    "Cosmic What If": dict(style="cinematic space visualization, photorealistic, dramatic lighting",
                           voice="en-US-AndrewNeural", rate="+0%", tags="#shorts #space #science #whatif"),
    "AI Tool Radar": dict(style="clean modern tech illustration, soft gradients, minimal desk setup",
                          voice="en-US-JennyNeural", rate="+5%", tags="#shorts #ai #tech #freetools"),
    "Restore Lab": dict(style="satisfying restoration macro photography, rust and metal textures, warm workshop light",
                        voice="en-US-ChristopherNeural", rate="+0%", tags="#shorts #restoration #satisfying #beforeandafter"),
}
DEFAULT_CFG = dict(style="cinematic, detailed", voice="en-US-AndrewNeural", rate="+0%", tags="#shorts")
BASE_STYLE = "vertical composition, no text, no watermark, no logos"

ROOT = Path(__file__).parent
OUT = ROOT / "output"
TMP = ROOT / "tmp"

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,84,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# ---------- helpers ----------
def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{cmd[0]} failed:\n{r.stderr[-1500:]}")
    return r.stdout


def audio_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


# ---------- scripts.md parser ----------
def parse_scripts(md):
    niches, cur, vid, section = [], None, None, None
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower().strip("*# ").strip()
        if line.startswith("### "):
            title = re.sub(r"^(Відео|Video)\s*\d+\.\s*", "", line[4:]).strip()
            vid = {"title": title, "voiceover": "", "prompt": ""}
            cur["videos"].append(vid)
            section = None
        elif line.startswith("## "):
            name = re.sub(r"^\d+\.\s*", "", line[3:]).split(":")[0].strip()
            cur = {"name": name, "videos": []}
            niches.append(cur)
            vid = None
        elif vid and low.startswith("voiceover"):
            section = "voiceover"
        elif vid and low.startswith("video prompt"):
            section = "prompt"
        elif vid and section:
            vid[section] += (" " if vid[section] else "") + line
    return niches


# ---------- video prompt -> image prompts ----------
CONTENT = re.compile(
    r"^(show|scene \d+:|end with|final (?:shot|scene)[^:]*:|finish (?:by|with)|begin with|start with|"
    r"transition (?:through|to)|include|demonstrate|reveal|use realistic close-up footage or accurate 3D visualization of|"
    r"the final (?:scene|photograph) (?:shows|depicts)|macro shots of)\s*", re.I)
DROP = re.compile(
    r"^(no|do not|don't|leave|add|any|generate|if|keep|maintain|preserve|avoid|label|distinguish|use|"
    r"return|make|this is|the water-rise|the probe|the exact|fast but|a short|add no)\b", re.I)


def split_sentences(text):
    return [s.strip() for s in re.split(r'(?<=[.!?])"?\s+', text) if s.strip()]


def scenes_from_prompt(prompt, title, n_max=5):
    sents = split_sentences(prompt)
    genre, context, items = "", [], []
    for i, s in enumerate(sents):
        s = s.rstrip(".")
        if i == 0 and re.match(r"create an?", s, re.I):
            genre = re.sub(r"^create an?\s+\d+-second vertical 9:16\s*", "", s, flags=re.I)
            continue
        m = CONTENT.match(s)
        if m:
            rest = s[m.end():]
            for part in re.split(r",\s*(?:and\s+)?|;\s*", rest):
                part = re.sub(r"^(?:then|and|with|where)\s+", "", part.strip(), flags=re.I)
                if len(part) > 3:
                    items.append(part)
        elif DROP.match(s):
            continue
        else:
            context.append(s)
    ctx = ". ".join(context)[:240]
    if len(items) < 3:  # fall back to camera variations of the title
        base = items[0] if items else title
        items = [f"{base}, {v}" for v in ("wide establishing shot", "close-up detail",
                                          "medium shot", "dramatic angle", "final reveal")]
        items = items[:n_max]
    n = min(n_max, len(items))
    chunks, k = [], len(items)
    for j in range(n):
        a, b = round(j * k / n), round((j + 1) * k / n)
        chunks.append(", ".join(items[a:b])[:220])
    if len(chunks) < 4:  # too few scenes: show each one twice from different distances
        chunks = [f"{c}, {v}" for c in chunks for v in ("wide shot", "close-up detail")]
    return chunks, ctx, genre


# ---------- TTS ----------
async def synth(text, mp3_path, voice, rate):
    import edge_tts
    words = []
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    with open(mp3_path, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                s = ch["offset"] / 1e7
                words.append((s, s + ch["duration"] / 1e7, ch["text"]))
    if not words:
        raise RuntimeError("No word timings received from TTS")
    return words


def estimate_words(text, total):
    """Rough word timings (used when the TTS service gives none)."""
    toks = text.split()
    weights = [len(t) + (6 if t[-1] in ".!?" else 3 if t[-1] in ",:;" else 0) for t in toks]
    s, t, out = sum(weights), 0.0, []
    for tok, w in zip(toks, weights):
        d = total * w / s
        out.append((t, t + d * 0.9, tok))
        t += d
    return out


def synth_gtts(text, mp3_path):
    from gtts import gTTS
    gTTS(text=text, lang="en", tld="com").save(str(mp3_path))
    return estimate_words(text, audio_duration(mp3_path))


def synth_with_fallback(text, mp3_path, voice, rate):
    """edge-tts first (exact word timings); if the service refuses, try other voices, then gTTS."""
    attempts = [(voice, rate), (voice, "+0%"), ("en-US-AriaNeural", "+0%"), ("en-GB-SoniaNeural", "+0%")]
    for v, r in attempts:
        try:
            return asyncio.run(synth(text, mp3_path, v, r))
        except Exception as e:
            print(f"    edge-tts ({v}, {r}) failed: {e}")
            time.sleep(2)
    print("    falling back to gTTS")
    return synth_gtts(text, mp3_path)


# ---------- images ----------
def placeholder(path):
    from PIL import Image, ImageOps
    grad = Image.linear_gradient("L").resize((W, H))
    ImageOps.colorize(grad, black="#02030a", white="#1b2a6b").save(path, quality=92)


_last_request = [0.0]
IMAGE_GAP = float(os.getenv("IMAGE_GAP", "15"))  # seconds between requests (free tier allows ~1 per 15 s)


def fetch_image(prompt, path, seed):
    """Download one image. Returns True on success, False if every attempt failed."""
    from PIL import Image, ImageOps
    url = IMAGE_URL.format(prompt=urllib.parse.quote(prompt), seed=seed)
    for attempt in range(6):
        wait = IMAGE_GAP - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            raw = path.with_suffix(".raw")
            raw.write_bytes(data)
            with Image.open(raw) as im:
                im = im.convert("RGB")
                # the free service stamps a small logo in the bottom corner: crop it off, then fill the frame
                im = im.crop((0, 0, im.width, int(im.height * 0.94)))
                ImageOps.fit(im, (W, H)).save(path, quality=92)
            raw.unlink()
            return True
        except Exception as e:
            print(f"    image attempt {attempt + 1}/6 failed: {e}")
            time.sleep(min(60, 15 * (attempt + 1)))
    return False


# ---------- captions ----------
def ts(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(words, path):
    lines = [ASS_HEADER]
    chunks = [words[i:i + 3] for i in range(0, len(words), 3)]
    for i, c in enumerate(chunks):
        start = c[0][0]
        end = chunks[i + 1][0][0] if i + 1 < len(chunks) else c[-1][1] + 0.3
        text = " ".join(w[2] for w in c).upper().replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{text}")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ---------- video ----------
def scene_starts(words, n_scenes, total):
    n = len(words)
    starts = [0.0]
    for i in range(1, n_scenes):
        starts.append(words[min(n - 1, round(i * n / n_scenes))][0])
    starts.append(total)
    return starts


def make_clip(img, dur, zoom_in, clip):
    frames = max(1, int(round(dur * FPS)))
    rate = 0.18 / frames
    z = f"1.0+{rate:.8f}*on" if zoom_in else f"max(1.0,1.18-{rate:.8f}*on)"
    vf = (f"scale=2160:3840,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
    run(["ffmpeg", "-y", "-i", str(img), "-vf", vf, "-frames:v", str(frames),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(clip)])


def build_video(video, num, cfg, out_dir, work, words, mp3):
    chunks, ctx, genre = scenes_from_prompt(video["prompt"], video["title"])
    total = audio_duration(mp3) + 0.4
    starts = scene_starts(words, len(chunks), total)
    clips = []
    last_good = None
    for i, chunk in enumerate(chunks):
        prompt = ". ".join(p for p in (chunk, ctx, genre, cfg["style"], BASE_STYLE) if p)[:700]
        img = work / f"scene{i}.jpg"
        print(f"    scene {i + 1}/{len(chunks)}: {chunk[:70]}")
        if not fetch_image(prompt, img, seed=num * 100 + i):
            if last_good:
                print("    reusing the previous image for this scene")
                shutil.copy(last_good, img)
            else:
                print("    using placeholder image")
                placeholder(img)
        last_good = img
        clip = work / f"clip{i}.mp4"
        make_clip(img, starts[i + 1] - starts[i], i % 2 == 0, clip)
        clips.append(clip)
    listfile = work / "clips.txt"
    listfile.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    joined = work / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(joined)])
    ass = work / "subs.ass"
    build_ass(words, ass)
    final = out_dir / f"{num:02d}_{slug(video['title'])}.mp4"
    run(["ffmpeg", "-y", "-i", str(joined), "-i", str(mp3),
         "-vf", f"ass={ass.as_posix()}", "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-shortest", str(final)])
    return final


def main():
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(exist_ok=True)
    niches = parse_scripts((ROOT / "scripts.md").read_text(encoding="utf-8"))
    print(f"Found {len(niches)} niches, {sum(len(n['videos']) for n in niches)} videos in scripts.md. "
          f"NICHE filter={NICHE_FILTER!r}, LIMIT={LIMIT}")
    print("Niche names:", [n["name"] for n in niches])
    num, done, made = 0, 0, 0
    for niche in niches:
        cfg = {**DEFAULT_CFG, **NICHES.get(niche["name"], {})}
        selected = (not NICHE_FILTER) or NICHE_FILTER in niche["name"].lower()
        out_dir = OUT / slug(niche["name"])
        meta = [f"# {niche['name']}\n",
                "Mark each upload as AI-generated content (YouTube and TikTok both ask for this).\n"]
        for video in niche["videos"]:
            num += 1
            if not selected or (LIMIT and made >= LIMIT):
                continue
            made += 1
            print(f"[{num:02d}] {niche['name']}: {video['title']}")
            try:
                out_dir.mkdir(exist_ok=True)
                work = TMP / f"{num:02d}"
                work.mkdir(exist_ok=True)
                mp3 = work / "voice.mp3"
                words = synth_with_fallback(video["voiceover"], mp3, cfg["voice"], cfg["rate"] or TTS_RATE_DEFAULT)
                final = build_video(video, num, cfg, out_dir, work, words, mp3)
                meta.append(f"\n## {final.name}\n- Title: {video['title']}\n- Description: {video['title']} {cfg['tags']}\n")
                done += 1
            except Exception as e:
                print(f"    FAILED: {e}")
        if out_dir.exists():
            (out_dir / "metadata.md").write_text("\n".join(meta), encoding="utf-8")
    if made == 0:
        print("NOTHING SELECTED. Check that scripts.md is the right file and that the niche filter matches a niche name (e.g. Cosmic, Tiny, Midnight, Auto, Block, Radar, Restore), or leave it empty.")
    print(f"Done: {done}/{made} videos in {OUT}")


if __name__ == "__main__":
    main()
