"""SRT + animated ASS caption generation (burned in later with FFmpeg)."""
import re
from .moments import HOOKS

RATIO_SIZE = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (720, 720)}
YELLOW, ORANGE = "&H00FFFF&", "&H00A5FF&"  # ASS colours are BGR


def clip_words(words, start, end):
    dur = end - start
    out = []
    for w in words:
        if w["e"] <= start or w["s"] >= end:
            continue
        s, e = max(0.0, w["s"] - start), min(dur, w["e"] - start)
        if e > s:
            out.append({"w": w["w"], "s": s, "e": e})
    return out


def phrases(words, max_words):
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > 0.7):
            out.append(cur)
            cur = []
        cur.append(w)
        if w["w"][-1:] in ",.?!;:…":
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _srt_t(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def _ass_t(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"


def write_srt(path, words, ratio):
    maxw = {"9:16": 4, "1:1": 5, "16:9": 7}[ratio]
    lines = []
    for n, p in enumerate(phrases(words, maxw), 1):
        lines += [str(n), f"{_srt_t(p[0]['s'])} --> {_srt_t(p[-1]['e'])}",
                  " ".join(w["w"] for w in p), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _is_keyword(token):
    t = re.sub(r"[^\w']", "", token.lower())
    return t in HOOKS or any(c.isdigit() for c in t) or (len(t) > 1 and token.isupper() and token.isascii())


def _esc(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def write_ass(path, words, ratio, font_size_pct=100, position="bottom", style="animated"):
    """Returns False if there is nothing to show."""
    w_px, h_px = RATIO_SIZE[ratio]
    maxw = {"9:16": 3, "1:1": 4, "16:9": 6}[ratio]
    size = round(min(w_px, h_px) * 0.075 * font_size_pct / 100)
    align = {"top": 8, "middle": 5, "bottom": 2}[position]
    margin_v = 0 if position == "middle" else round(h_px * 0.10)
    margin_x = round(w_px * 0.06)
    sinhala = any("\u0d80" <= c <= "\u0dff" for x in words for c in x["w"])
    font = "Noto Sans Sinhala" if sinhala else "DejaVu Sans"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w_px}
PlayResY: {h_px}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(2, size // 12)},1,{align},{margin_x},{margin_x},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for p in phrases(words, maxw):
        if style == "plain":
            text = _esc(" ".join(x["w"] for x in p))
            events.append(f"Dialogue: 0,{_ass_t(p[0]['s'])},{_ass_t(p[-1]['e'])},Default,,0,0,0,,{text}")
            continue
        for i, cur in enumerate(p):
            start = cur["s"]
            end = p[i + 1]["s"] if i + 1 < len(p) and p[i + 1]["s"] - cur["e"] < 0.3 else cur["e"] + 0.12
            if end <= start:
                end = start + 0.1
            parts = []
            for k, x in enumerate(p):
                t = _esc(x["w"])
                if k == i:  # spoken word: yellow + quick "pop"
                    parts.append(f"{{\\c{YELLOW}\\fscx100\\fscy100\\t(0,110,\\fscx122\\fscy122)}}{t}{{\\r}}")
                elif _is_keyword(x["w"]):
                    parts.append(f"{{\\c{ORANGE}}}{t}{{\\r}}")
                else:
                    parts.append(t)
            events.append(f"Dialogue: 0,{_ass_t(start)},{_ass_t(end)},Default,,0,0,0,,{' '.join(parts)}")
    if not events:
        return False
    path.write_text(head + "\n".join(events) + "\n", encoding="utf-8")
    return True
