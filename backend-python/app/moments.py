"""Transcript-based moment detection (heuristic NLP scoring, no paid API, no LLM).

Words from Whisper are grouped into short "units" (sentence-like pieces). Every
possible run of units that fits the chosen clip length is scored, then the best
non-overlapping runs are chosen.
"""
import re

HOOKS = set("""secret secrets never always mistake mistakes truth how why best worst biggest free stop important
real actually amazing crazy insane money love hate wrong right first lesson tip tips trick warning problem
solution remember imagine incredible powerful simple easy hard fail failed win success danger shocking
unbelievable need must discover reveal proven fastest ultimate""".split())
STOP = set("""a an the and or but if so to of in on at for with is are was were be been it this that these those i you he
she we they my your his her our their me him them as by from not no yes do does did have has had will would can
could just very really like then than there here what when which who""".split())
TERMINAL = ".?!…"


def make_units(words, max_len=8.0):
    units, cur = [], []

    def flush():
        if cur:
            units.append({"s": cur[0]["s"], "e": cur[-1]["e"], "words": list(cur),
                          "ends_sentence": cur[-1]["w"][-1:] in TERMINAL})
            cur.clear()

    for w in words:
        if cur and (w["s"] - cur[-1]["e"] > 0.8 or w["e"] - cur[0]["s"] > max_len):
            flush()
        cur.append(w)
        if w["w"][-1:] in TERMINAL:
            flush()
    flush()
    for i, u in enumerate(units):
        u["starts_sentence"] = i == 0 or units[i - 1]["ends_sentence"]
    return units


def _score(units, dur):
    words = [w for u in units for w in u["words"]]
    n = max(1, len(words))
    text = " ".join(w["w"] for w in words)
    toks = [re.sub(r"[^\w']", "", w["w"].lower()) for w in words]
    hook = sum(t in HOOKS for t in toks) / n
    q, ex = text.count("?"), text.count("!")
    nums = sum(any(c.isdigit() for c in t) for t in toks)
    rate = n / dur
    rate_s = max(0.0, 1 - abs(rate - 2.8) / 2.8)
    content = [t for t in toks if t and t not in STOP]
    uniq = len(set(content)) / max(1, len(content))
    speech = sum(w["e"] - w["s"] for w in words) / dur
    parts = {
        "strong keywords": 3.0 * min(hook * 8, 1),
        "asks questions": 1.0 * min(q, 2) / 2,
        "emphasis": 0.6 * min(ex, 2) / 2,
        "numbers/facts": 0.8 * min(nums, 3) / 3,
        "good speaking pace": 1.5 * rate_s,
        "varied vocabulary": 1.0 * uniq,
        "dense speech": 1.5 * min(speech / 0.7, 1),
        "clean start": 0.7 if units[0]["starts_sentence"] else 0,
        "clean ending": 0.7 if units[-1]["ends_sentence"] else 0,
    }
    reasons = [k for k, v in sorted(parts.items(), key=lambda kv: -kv[1]) if v > 0.5][:3]
    return sum(parts.values()), reasons, text


def find_moments(words, video_duration, target, count):
    if not words:
        raise ValueError("No speech was detected in this video.")
    if video_duration <= target + 1:
        units = make_units(words)
        sc, reasons, text = _score(units, max(1.0, video_duration))
        return [{"start": 0.0, "end": round(video_duration, 2), "score": round(sc, 2),
                 "reason": ", ".join(reasons) or "whole video", "title": _title(text)}]
    units = make_units(words)
    limit = target - 0.6  # leave room for padding
    cands = []
    for i in range(len(units)):
        j = i
        while j < len(units) and units[j]["e"] - units[i]["s"] <= limit:
            j += 1
        if j == i:
            continue
        dur = units[j - 1]["e"] - units[i]["s"]
        if dur < 0.5 * target:
            continue
        sc, reasons, text = _score(units[i:j], dur)
        cands.append((sc, units[i]["s"], units[j - 1]["e"], reasons, text))
    cands.sort(key=lambda c: -c[0])
    chosen = []
    for sc, s, e, reasons, text in cands:
        s2, e2 = max(0.0, s - 0.25), min(video_duration, e + 0.35)
        if any(s2 < c["end"] + 1.0 and e2 > c["start"] - 1.0 for c in chosen):
            continue
        chosen.append({"start": round(s2, 2), "end": round(e2, 2), "score": round(sc, 2),
                       "reason": ", ".join(reasons) or "steady speech", "title": _title(text)})
        if len(chosen) >= count:
            break
    if not chosen:
        raise ValueError("Could not find a long enough stretch of speech for the chosen clip length.")
    return chosen


def _title(text):
    w = text.split()
    t = " ".join(w[:9]).strip(" ,;:-")
    return t + ("…" if len(w) > 9 else "")
