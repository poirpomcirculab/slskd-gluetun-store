#!/usr/bin/env python3
"""Circuit « À trier » : aiguilleur (racine de À trier -> Films / Séries / Musique) + moulinette musique
(À trier/Musique -> Musique : décompression, MusicBrainz, MP3 320, tags, pochette, paroles LRC, rangement).
Aucun secret ici : les notifications (Pushover) et la vérification Navidrome sont faites par n8n via GET /evenements."""
import difflib, hashlib, http.server, json, os, re, shutil, subprocess, sys, tempfile, threading, time, unicodedata, uuid, zipfile
import urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import mutagen
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TYER, TCMP, TXXX, UFID, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.flac import FLAC

MEDIAS = Path(os.environ.get("MEDIAS", "/medias"))
def res(parent, name):
    """Entrée existante de `parent` dont le nom est le même à la normalisation Unicode près (macOS écrit en NFD), sinon parent/name."""
    try:
        for x in os.listdir(parent):
            if unicodedata.normalize("NFC", x) == name: return Path(parent) / x
    except OSError: pass
    return Path(parent) / name
TRIER, MUS, ORIG, WORK = (None,) * 4
def init_paths():
    global TRIER, MUS, ORIG, WORK
    TRIER = res(MEDIAS, "À trier"); MUS = res(MEDIAS, "Musique"); ORIG = res(MEDIAS, "Musique-originaux-à-valider")
    WORK = res(TRIER, "Musique") / ".work"
STATE_F = Path(os.environ.get("STATE", "/data")) / "etat.json"
UA = "umbrel-trieur/1.0 (circuit personnel)"
AUDIO = {".flac", ".m4a", ".mp3", ".alac", ".wav", ".aiff", ".aif", ".ogg"}
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".mpg", ".mpeg", ".ts"}
ANNEXE = {".srt", ".jpg", ".jpeg", ".png", ".cue", ".log", ".nfo", ".txt", ".pdf", ".sub", ".idx", ".ass", ".m3u", ".accurip", ".md5", ".sfv", ".ffp"}
RESERVES = {"Films", "Séries", "Musique"}
SERIE_RE = re.compile(r"(?<![a-z0-9])(s\d{1,2}[ ._-]?e\d{1,3}|\d{1,2}x\d{2,3})(?![a-z0-9])", re.I)
SAISON_RE = re.compile(r"^(saison|season|série|serie)s?[ ._-]*\d+$", re.I)
FILM_RE = re.compile(r"^.+ \((19|20)\d\d\)$")
LOCK = threading.Lock()
LOG = []

def log(*a):
    m = datetime.now().strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(m, flush=True); LOG.append(m); del LOG[:-200]

# ---------- état ----------
def load():
    try: return json.loads(STATE_F.read_text())
    except Exception: return {"sig": {}, "notifie": [], "rapporte": {}}
def save(st):
    STATE_F.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_F.with_suffix(".tmp"); tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1)); tmp.replace(STATE_F)
EVLOCK = threading.Lock()
EV_F = STATE_F.with_name("evenements.json")
def evs():
    try: return json.loads(EV_F.read_text())
    except Exception: return []
def evenement(st, type_, texte, **kw):
    with EVLOCK:
        e = evs(); e.append(dict(id=uuid.uuid4().hex[:10], type=type_, texte=texte, **kw)); EV_F.parent.mkdir(parents=True, exist_ok=True); EV_F.write_text(json.dumps(e, ensure_ascii=False)); log("EVT", type_, texte)

# ---------- utilitaires ----------
def nfc(s): return unicodedata.normalize("NFC", s)
def norm(s, parens=True):
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    if parens: s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*", "", s)
    s = s.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()
def propre(s, maxi=120):
    s = nfc(str(s)).replace("/", "-").replace("\\", "-").replace(":", " -")
    s = re.sub(r'[*?"<>|\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return s[:maxi].rstrip(". ") or "_"
def cache(p): return not p.name.startswith(".") and p.name != "_rapport.txt" and not p.name.endswith((".part", ".crdownload", ".tmp", "!qB"))
def fichiers(p):
    if p.is_file(): return [p] if cache(p) else []
    out = []
    for r, ds, fs in os.walk(p):
        ds[:] = [d for d in ds if not d.startswith(".")]
        out += [Path(r) / f for f in fs if cache(Path(f))]
    return out
def signature(p):
    fs = fichiers(p)
    return [len(fs), sum(f.stat().st_size for f in fs), max([f.stat().st_mtime for f in fs] or [0])]
def stable(st, cle, p, delai=60):
    """Vrai si la taille n'a pas bougé depuis le passage précédent et que le dernier fichier a plus de `delai` s."""
    sig = signature(p); prec = st["sig"].get(cle); st["sig"][cle] = sig
    return prec == sig and time.time() - sig[2] > delai
def heure_ok():
    return 9 <= datetime.now(ZoneInfo("Europe/Paris")).hour < 22
def run(cmd, **k):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=k.pop("timeout", 600), **k)

# ---------- 1. aiguilleur ----------
def classer(p):
    """-> (catégorie ou None, raison)."""
    if p.is_file():
        e = p.suffix.lower()
        if e in AUDIO: return "Musique", ""
        if e == ".zip":
            try: ex = {Path(n).suffix.lower() for n in zipfile.ZipFile(p).namelist() if not n.endswith("/")}
            except Exception: return None, "archive illisible"
            if ex & AUDIO and not ex & VIDEO: return "Musique", ""
            return None, "archive sans audio reconnu"
        if e in VIDEO:
            if SERIE_RE.search(p.stem): return "Séries", ""
            if FILM_RE.match(p.stem): return "Films", ""
            return None, "vidéo sans motif « Titre (Année) » ni SxxExx"
        return None, "fichier annexe seul ou type inconnu"
    ex = {f.suffix.lower() for f in fichiers(p)}
    audio, video = ex & AUDIO, ex & VIDEO
    if audio and video: return None, "mélange audio et vidéo"
    if audio: return "Musique", ""
    if video:
        vids = [f for f in fichiers(p) if f.suffix.lower() in VIDEO]
        if SERIE_RE.search(p.name) or any(SERIE_RE.search(f.stem) for f in vids) or any(SAISON_RE.match(d.name) for d in p.iterdir() if d.is_dir()) or SAISON_RE.match(p.name):
            return "Séries", ""
        if FILM_RE.match(p.name) or (len(vids) == 1 and FILM_RE.match(vids[0].stem)): return "Films", ""
        return None, "vidéo sans motif « Titre (Année) » ni SxxExx"
    if any(f.suffix.lower() == ".zip" for f in fichiers(p)): return None, "archive dans un dossier"
    return None, "contenu non reconnu"

def aiguiller(st, delai=60):
    for p in sorted(TRIER.iterdir()):
        if not cache(p) or nfc(p.name) in RESERVES: continue
        cle = "r:" + nfc(p.name)
        if not stable(st, cle, p, delai): continue
        cat, raison = classer(p)
        sig = json.dumps(st["sig"][cle])
        if cat:
            dest = res(TRIER, cat) / p.name
            if dest.exists(): cat, raison = None, f"« {p.name} » existe déjà dans {cat}"
            else:
                shutil.move(str(p), str(dest)); st["sig"].pop(cle, None); log("aiguillé", p.name, "->", cat); continue
        k = cle + sig
        if k not in st["notifie"]:
            st["notifie"].append(k); evenement(st, "douteux", f"À trier : « {nfc(p.name)} » reste à la racine ({raison}).", cle=cle)

# ---------- 2. moulinette musique ----------
def dossiers_audio(p):
    """Unités d'album : dossiers contenant de l'audio, regroupés si ce sont des CD1/CD2 d'un même parent."""
    audio = {f.parent for f in fichiers(p) if f.suffix.lower() in AUDIO}
    if not audio: return []
    res = set()
    for d in audio:
        if re.match(r"^(cd|disc|disque)[ ._-]*\d+", d.name, re.I) and d.parent != MUS:
            res.add(d.parent)
        else: res.add(d)
    return sorted(res)

def tags_locaux(f):
    t = dict(artist="", albumartist="", album="", title="", track=0, disc=1, date="", mbalbum="", length=0.0, lossless=False, codec="")
    try: m = mutagen.File(f, easy=True)
    except Exception: m = None
    if m is None or not getattr(m, "info", None): raise ValueError("fichier illisible : " + f.name)
    g = lambda k: (m.get(k) or [""])[0] if m.tags is not None else ""
    t.update(artist=g("artist"), albumartist=g("albumartist"), album=g("album"), title=g("title"), date=g("date")[:4], mbalbum=g("musicbrainz_albumid"))
    for k, key in (("track", "tracknumber"), ("disc", "discnumber")):
        mm = re.match(r"\d+", g(key) or "")
        if mm: t[k] = int(mm.group())
    t["disc"] = t["disc"] or 1
    t["length"] = float(m.info.length or 0)
    cls = type(m).__name__
    t["codec"] = getattr(m.info, "codec", "") or cls
    t["lossless"] = cls in ("EasyFLAC", "FLAC", "WAVE", "AIFF", "WavPack") or f.suffix.lower() in (".wav", ".aiff", ".aif", ".flac") or (cls.startswith("EasyMP4") and str(t["codec"]).startswith("alac"))
    if t["length"] < 1: raise ValueError("durée nulle : " + f.name)
    if not t["track"] or not t["title"]:
        mm = re.search(r"(?:^|[ -])(\d{1,3})[ ._-]+(.+)$", f.stem)
        if mm:
            t["track"] = t["track"] or int(mm.group(1)); t["title"] = t["title"] or mm.group(2).strip()
        t["title"] = t["title"] or f.stem
    return t

def mb(url, essais=6):
    for i in range(essais):
        time.sleep(1.2)
        try:
            with urllib.request.urlopen(urllib.request.Request("https://musicbrainz.org/ws/2/" + url, headers={"User-Agent": UA, "Accept": "application/json"}), timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503): raise
            time.sleep(4 * (i + 1))
    raise RuntimeError("MusicBrainz indisponible")

def pistes_mb(rel):
    out = []
    for m in rel.get("media", []):
        for t in m.get("tracks", []):
            out.append(dict(disc=m["position"], n=int(t["position"]), title=t["title"], length=(t.get("length") or 0) / 1000,
                            rid=t["recording"]["id"], tid=t["id"], credit="".join(c["name"] + c.get("joinphrase", "") for c in t.get("artist-credit", [])),
                            aid=[c["artist"]["id"] for c in t.get("artist-credit", [])][:1]))
    return out

def sim(a, b):
    f = lambda x, p: difflib.SequenceMatcher(None, norm(a, p) or "x", norm(b, p) or "y").ratio()
    return max(f(a, False), f(a, True))

def correspond(loc, rel):
    """Liste de pistes locales (triées disque/piste) contre une sortie MusicBrainz : même nombre, titres et durées concordants."""
    pm = pistes_mb(rel)
    if len(pm) != len(loc): return False
    titres = sum(sim(l["title"], p["title"]) for l, p in zip(loc, pm)) / len(loc)
    durs = [abs(l["length"] - p["length"]) <= 6 for l, p in zip(loc, pm) if p["length"]]
    return titres >= 0.85 and (not durs or sum(durs) / len(durs) >= 0.9)

def identifier(loc, artiste, album):
    """-> (sortie MusicBrainz complète, pistes) ou lève ValueError (raison)."""
    inc = "recordings+artist-credits+release-groups"
    ids = {l["mbalbum"] for l in loc if l["mbalbum"]}
    cands = []
    if len(ids) == 1:
        cands = [mb(f"release/{ids.pop()}?inc={inc}&fmt=json")]
    else:
        q = f'release:"{album}" AND artist:"{artiste}"' if artiste else f'release:"{album}"'
        for r in mb("release/?" + urllib.parse.urlencode({"query": q, "fmt": "json", "limit": 8})).get("releases", []):
            if r.get("score", 0) >= 85 and len(cands) < 6 and sim(r["title"], album) >= 0.85:
                cands.append(mb(f"release/{r['id']}?inc={inc}&fmt=json"))
    bons = [c for c in cands if correspond(loc, c)]
    if not bons: raise ValueError(f"aucune sortie MusicBrainz ne correspond avec certitude (« {artiste} » — « {album} », {len(loc)} pistes)")
    if len({b["release-group"]["id"] for b in bons}) > 1: raise ValueError("plusieurs albums MusicBrainz possibles : " + " | ".join(b["title"] + " " + b.get("date", "") for b in bons))
    an = next((l["date"] for l in loc if l["date"]), "")
    bons.sort(key=lambda b: (0 if an and b.get("date", "").startswith(an) else 1, b.get("date") or "9999"))
    return bons[0]

def sans_mb(loc, artiste, album, zip_):
    """Achat Bandcamp absent de MusicBrainz : on accepte si les tags sont complets et cohérents (pistes 1..N sans trou, un seul album/artiste)."""
    bc = zip_ and any("bandcamp" in str(mutagen.File(f).tags.get("COMM::eng", "") if mutagen.File(f).tags is not None else "").lower() for f, _ in loc[:3])
    if not bc or not artiste or not album: return None
    if len({t["album"] for _, t in loc}) != 1 or any(not t["title"] or not t["artist"] for _, t in loc): return None
    if [t["track"] for _, t in loc] != list(range(1, len(loc) + 1)) or {t["disc"] for _, t in loc} != {1}: return None
    ac = [{"name": artiste, "artist": {"id": ""}, "joinphrase": ""}]
    return {"id": "", "title": album, "date": "", "release-group": {"id": ""}, "artist-credit": ac,
            "media": [{"position": 1, "tracks": [{"position": t["track"], "title": t["title"], "length": int(t["length"] * 1000), "id": "", "recording": {"id": ""},
                                                   "artist-credit": [{"name": t["artist"], "artist": {"id": ""}}]} for _, t in loc]}]}

def nom_artiste(credit, ac):
    """Réutilise le dossier d'artiste existant dans Musique (casse/accents), sinon le nom MusicBrainz."""
    n = norm(credit, False)
    for d in MUS.iterdir():
        if d.is_dir() and norm(d.name, False) == n: return d.name
    return propre(credit)

def dimensions(img):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(img)])
    try: w, h = map(int, r.stdout.strip().split(",")[:2]); return min(w, h)
    except Exception: return 0

def pochette(unit, fs, rel, dest):
    """Écrit folder.jpg (>= 600 px si possible) et renvoie (octets, note)."""
    cands = []
    for f in fichiers(unit):
        if f.suffix.lower() in (".jpg", ".jpeg", ".png"): cands.append(f)
    cands.sort(key=lambda f: (0 if re.search(r"cover|folder|front", f.stem, re.I) else 1, -f.stat().st_size))
    tmp = dest / "_src"
    best, bd = None, 0
    for i, f in enumerate(cands[:3]):
        d = dimensions(f)
        if d > bd: best, bd = f, d
    if not best:   # pochette intégrée
        for f in fs:
            try:
                m = mutagen.File(f)
                data = None
                if isinstance(m, FLAC) and m.pictures: data = m.pictures[0].data
                elif isinstance(m.tags, ID3) if m is not None and m.tags is not None else False:
                    ap = m.tags.getall("APIC"); data = ap[0].data if ap else None
                elif isinstance(m, MP4) and m.tags and "covr" in m.tags: data = bytes(m.tags["covr"][0])
                if data: tmp.write_bytes(data); best, bd = tmp, dimensions(tmp); break
            except Exception: pass
    note = ""
    if bd < 600:
        for url in ([f"release/{rel['id']}/front-1200", f"release-group/{rel['release-group']['id']}/front-1200"] if rel["id"] else []):
            try:
                with urllib.request.urlopen(urllib.request.Request("https://coverartarchive.org/" + url, headers={"User-Agent": UA}), timeout=30) as r:
                    tmp.write_bytes(r.read())
                d = dimensions(tmp)
                if d > bd: best, bd = tmp, d
                if bd >= 600: break
            except Exception: pass
    if not best: return None, "pas de pochette trouvée"
    out = dest / "folder.jpg"
    r = run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(best), "-frames:v", "1", "-q:v", "2", str(out)])
    if r.returncode or not out.exists(): return None, "pochette illisible"
    if tmp.exists(): tmp.unlink()
    if bd < 600: note = f"pochette de {bd} px seulement"
    return out.read_bytes(), note

def ecrire_tags(f, p, rel, an, nbd, total, cover, comp):
    ids = dict(album=rel["id"], rg=rel["release-group"]["id"], rec=p["rid"], trk=p["tid"], art=p["aid"][0] if p["aid"] else "")
    aa = rel["artist-credit"][0]["artist"]["id"]
    albumartist = "".join(c["name"] + c.get("joinphrase", "") for c in rel["artist-credit"])
    if f.suffix == ".mp3":
        try: t = ID3(f); t.delete(f)
        except ID3NoHeaderError: pass
        t = ID3()
        t.add(TIT2(encoding=1, text=p["title"])); t.add(TPE1(encoding=1, text=p["credit"] or albumartist)); t.add(TPE2(encoding=1, text=albumartist))
        t.add(TALB(encoding=1, text=rel["title"])); t.add(TRCK(encoding=1, text=f"{p['n']}/{total}"))
        t.add(TPOS(encoding=1, text=f"{p['disc']}/{nbd}"))
        if an: t.add(TYER(encoding=1, text=an))
        if comp: t.add(TCMP(encoding=1, text="1"))
        for k, v in (("MusicBrainz Album Id", ids["album"]), ("MusicBrainz Artist Id", ids["art"]), ("MusicBrainz Album Artist Id", aa),
                     ("MusicBrainz Release Group Id", ids["rg"]), ("MusicBrainz Release Track Id", ids["trk"])):
            if v: t.add(TXXX(encoding=1, desc=k, text=v))
        if ids["rec"]: t.add(UFID(owner="http://musicbrainz.org", data=ids["rec"].encode()))
        if cover: t.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="Cover", data=cover))
        t.save(f, v2_version=3)
    else:  # m4a AAC conservé tel quel
        m = MP4(f); m.delete()
        m["\xa9nam"], m["\xa9ART"], m["aART"], m["\xa9alb"] = [p["title"]], [p["credit"] or albumartist], [albumartist], [rel["title"]]
        m["trkn"], m["disk"] = [(p["n"], total)], [(p["disc"], nbd)]
        if an: m["\xa9day"] = [an]
        if comp: m["cpil"] = True
        for k, v in (("MusicBrainz Album Id", ids["album"]), ("MusicBrainz Artist Id", ids["art"]), ("MusicBrainz Album Artist Id", aa),
                     ("MusicBrainz Release Group Id", ids["rg"]), ("MusicBrainz Track Id", ids["rec"]), ("MusicBrainz Release Track Id", ids["trk"])):
            if v: m["----:com.apple.iTunes:" + k] = [MP4FreeForm(v.encode())]
        if cover: m["covr"] = [MP4Cover(cover, MP4Cover.FORMAT_JPEG)]
        m.save()

def paroles(artiste, titre, album, duree):
    q = urllib.parse.urlencode({"artist_name": artiste, "track_name": titre, "album_name": album, "duration": int(round(duree))})
    try:
        with urllib.request.urlopen(urllib.request.Request("https://lrclib.net/api/get?" + q, headers={"User-Agent": UA}), timeout=12) as r:
            return json.loads(r.read()).get("syncedLyrics") or ""
    except Exception: return ""

def dossier_existant(artdir, album):
    if not artdir.exists(): return None
    n = norm(album)
    for d in artdir.iterdir():
        if d.is_dir() and (norm(d.name) == n or norm(d.name, False) == norm(album, False)): return d
    return None

def traiter(unit, st, zipsrc=None):
    """Un album. Lève ValueError(raison) si douteux ; renvoie (artiste, album, nb pistes, nb converties, notes) si rangé."""
    fs = sorted(f for f in fichiers(unit) if f.suffix.lower() in AUDIO)
    loc = [(f, tags_locaux(f)) for f in fs]
    loc.sort(key=lambda x: (x[1]["disc"], x[1]["track"], x[0].name))
    # artiste/album : tags majoritaires, sinon dossiers
    maj = lambda k: max({x[1][k] for x in loc if x[1][k]} or {""}, key=lambda v: sum(1 for x in loc if x[1][k] == v))
    artiste, album = maj("albumartist") or maj("artist"), maj("album")
    if not album: album = unit.name
    if not artiste: artiste = unit.parent.name if unit.parent != MUS else ""
    note_mb = ""
    try: rel = identifier([x[1] for x in loc], artiste, album)
    except ValueError as e:
        rel = sans_mb(loc, artiste, album, zipsrc is not None or unit.parent == WORK)
        if rel is None: raise
        note_mb = "absent de MusicBrainz : tags du fichier conservés (achat Bandcamp), sans identifiants MusicBrainz"
    pm = pistes_mb(rel)
    credit = "".join(c["name"] + c.get("joinphrase", "") for c in rel["artist-credit"])
    comp = rel["artist-credit"][0]["artist"]["id"] == "89ad4ac3-39f7-470e-963a-56509c546377"
    an = maj("date") if maj("date") else (rel.get("date") or "")[:4]
    nbd = len(rel["media"])
    artdir = res(MUS, "Compilations") if comp else MUS / nom_artiste(credit, rel)
    if dossier_existant(artdir, rel["title"]): raise ValueError(f"doublon : « {rel['title']} » existe déjà dans Musique/{artdir.name}")
    dest = WORK / hashlib.md5(str(unit).encode()).hexdigest()[:10]
    shutil.rmtree(dest, ignore_errors=True); dest.mkdir(parents=True)
    notes, conv = [], 0
    try:
        cover, n = pochette(unit, [x[0] for x in loc], rel, dest)
        if n: notes.append(n)
        for (f, t), p in zip(loc, pm):
            base = f"{p['n']:02d} {propre(p['title'])}" if nbd == 1 else f"{p['disc']}-{p['n']:02d} {propre(p['title'])}"
            garde = (f.suffix.lower() == ".mp3" and not t["lossless"]) or (f.suffix.lower() == ".m4a" and not t["lossless"])
            if garde:
                out = dest / (base + f.suffix.lower()); shutil.copy2(f, out)
                if f.suffix.lower() == ".mp3":
                    mi = MP3(out).info
                    if mi.bitrate < 300000: notes.append(f"« {p['title']} » en MP3 {mi.bitrate // 1000} kbit/s (non réencodé)")
            else:
                out = dest / (base + ".mp3")
                r = run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(f), "-vn", "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", "320k", str(out)], timeout=900)
                if r.returncode or not out.exists(): raise ValueError("conversion impossible (fichier abîmé ?) : " + f.name)
                conv += 1
            d = MP3(out).info.length if out.suffix == ".mp3" else MP4(out).info.length
            if abs(d - t["length"]) > 2: raise ValueError(f"durée anormale après traitement : {f.name} ({t['length']:.0f} s -> {d:.0f} s)")
            ecrire_tags(out, p, rel, an, nbd, len(pm) if nbd == 1 else sum(1 for q in pm if q["disc"] == p["disc"]), cover, comp)
            lrc = paroles(p["credit"] or credit, p["title"], rel["title"], d)
            if lrc: (dest / (base + ".lrc")).write_text(lrc, encoding="utf-8")
        if cover: (dest / "folder.jpg").write_bytes(cover)
        # contrôle final : relecture
        if len([x for x in dest.iterdir() if x.suffix in (".mp3", ".m4a")]) != len(pm): raise ValueError("nombre de pistes incohérent")
        artdir.mkdir(exist_ok=True)
        final = artdir / propre(rel["title"])
        if final.exists(): raise ValueError("doublon (dossier déjà présent)")
        os.rename(dest, final)
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    if note_mb: notes.append(note_mb)
    return artdir.name, rel["title"], len(pm), conv, notes

def vider_parents(p):
    base = res(TRIER, "Musique")
    while p != base and p.exists() and not [x for x in p.iterdir() if x.name != ".DS_Store"]:
        shutil.rmtree(p); p = p.parent

def musique(st, delai=60):
    base = res(TRIER, "Musique")
    WORK.mkdir(parents=True, exist_ok=True)
    unites = []   # (clé, dossier, zip d'origine)
    for p in sorted(base.iterdir()):
        if not cache(p) or p.name.startswith("_"): continue
        if p.is_file() and p.suffix.lower() == ".zip":
            if not stable(st, "m:" + nfc(p.name), p, delai): continue
            ex = WORK / ("zip-" + hashlib.md5(p.name.encode()).hexdigest()[:8])
            if not ex.exists():
                ex.mkdir(parents=True)
                with zipfile.ZipFile(p) as z:
                    for i in z.infolist():
                        t = (ex / i.filename).resolve()
                        if str(t).startswith(str(ex.resolve())) and not i.filename.endswith("/"):
                            t.parent.mkdir(parents=True, exist_ok=True); t.write_bytes(z.read(i))
            unites += [("m:" + nfc(p.name), u, p) for u in dossiers_audio(ex)]
        elif p.is_file():
            continue   # fichier audio isolé : pas un album
        elif p.name != ".work":
            for u in dossiers_audio(p):
                rel = nfc(str(u.relative_to(base)))
                if stable(st, "m:" + rel, u, delai): unites.append(("m:" + rel, u, None))
    for cle, u, z in unites:
        sig = json.dumps(signature(z or u))
        if st["rapporte"].get(cle) == sig: continue
        try:
            artiste, album, n, conv, notes = traiter(u, st)
        except Exception as e:
            raison = str(e) if isinstance(e, ValueError) else f"erreur : {type(e).__name__} {e}"
            cible = (z.parent if z else u)
            nom = cible / ((z.stem + "_rapport.txt") if z else "_rapport.txt")
            nom.write_text(f"Album : {u.name}\nSource : {z.name if z else u}\nDate : {datetime.now():%d/%m/%Y %H:%M}\nRaison : {raison}\n", encoding="utf-8")
            st["rapporte"][cle] = json.dumps(signature(z or u))
            nom_alb = nfc(z.stem if z else u.name); evenement(st, "bloque", f"Bloqué : {nom_alb} ({raison})", album=nom_alb)
            if z: shutil.rmtree(u, ignore_errors=True)
            continue
        # succès : les originaux partent hors bibliothèque, à valider
        dst = ORIG / propre(f"{artiste} - {album}"); dst.mkdir(parents=True, exist_ok=True)
        if z:
            shutil.move(str(z), str(dst / z.name)); shutil.rmtree(u if u.parent == WORK else u.parent, ignore_errors=True)
        else:
            for f in u.iterdir(): shutil.move(str(f), str(dst / f.name))
            vider_parents(u)
        st["rapporte"].pop(cle, None); st["sig"].pop(cle, None)
        evenement(st, "ajoute", f"Ajouté : {artiste} — {album}", artiste=artiste, album=album, pistes=n, converties=conv, notes=notes)

# ---------- boucle et serveur ----------
def passe(force=False, delai=60):
    if not LOCK.acquire(blocking=False): return "déjà en cours"
    try:
        if not force and not heure_ok(): return "hors plage"
        init_paths(); st = load()
        try: aiguiller(st, delai)
        except Exception as e: log("aiguilleur", type(e).__name__, e)
        try: musique(st, delai)
        except Exception as e: log("musique", type(e).__name__, e)
        st["derniere"] = datetime.now().isoformat(timespec="seconds"); save(st)
        return "ok"
    finally: LOCK.release()

class H(http.server.BaseHTTPRequestHandler):
    def rep(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode(); self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        st = load()
        if self.path.startswith("/evenements"): return self.rep(evs())
        self.rep({"derniere_passe": st.get("derniere"), "evenements_en_attente": len(evs()), "journal": LOG[-30:]})
    def do_POST(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/passe":
            force, delai = q.get("force") == ["1"], int(q.get("delai", ["60"])[0])
            if q.get("sync") == ["1"]: return self.rep({"resultat": passe(force, delai)})
            threading.Thread(target=passe, args=(force, delai), daemon=True).start(); return self.rep({"lance": True})
        if u.path == "/evenements/ack":
            ids = set(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]"))
            with EVLOCK: EV_F.write_text(json.dumps([e for e in evs() if e["id"] not in ids], ensure_ascii=False))
            return self.rep({"ok": True})
        self.rep({"erreur": "inconnu"}, 404)
    def log_message(self, *a): pass

def boucle():
    while True:
        try: passe()
        except Exception as e: log("passe", type(e).__name__, e)
        time.sleep(300)

if __name__ == "__main__":
    if "--once" in sys.argv: print(passe(True, int(os.environ.get("DELAI", "60")))); sys.exit()
    threading.Thread(target=boucle, daemon=True).start()
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
