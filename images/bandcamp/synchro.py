"""Synchro Bandcamp : achats à venir seulement, MP3 320, dépôt à la racine de « À trier ».
Aucun secret ici : le cookie est lu dans /config/cookies.txt (dossier hors dépôt) ; les notifications (Pushover) sont faites par n8n via GET /evenements."""
import json, os, shutil, sys, threading, time, uuid, http.server, urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from bandcampsync.bandcamp import Bandcamp, BandcampError
from bandcampsync.ignores import Ignores
from bandcampsync.options import BandcampSyncOptions
from bandcampsync.sync import Syncer

CONF = Path(os.environ.get("CONFIG", "/config")); DATA = Path(os.environ.get("STATE", "/data")); TRIER = Path(os.environ.get("TRIER", "/atrier"))
COOKIE_F, IGN_F, EV_F, ST_F = CONF / "cookies.txt", DATA / "ignores.txt", DATA / "evenements.json", DATA / "etat.json"
STAGING, TMP = DATA / "staging", DATA / "tmp"
MAX_ALBUMS = 3            # garde-fou : au-delà, l'état est sans doute perdu -> rien n'est téléchargé
FORMAT = os.environ.get("FORMAT", "mp3-320")
TZ = ZoneInfo("Europe/Paris")
LOCK, EVLOCK, LOG = threading.Lock(), threading.Lock(), []

def log(*a):
    LOG.append(datetime.now(TZ).strftime("%d/%m %H:%M ") + " ".join(map(str, a))); del LOG[:-60]; print(*a, flush=True)
def jload(p, d):
    try: return json.loads(p.read_text())
    except Exception: return d
def evs(): return jload(EV_F, [])
def evenement(type_, texte, **kw):
    with EVLOCK: EV_F.write_text(json.dumps(evs() + [{"id": uuid.uuid4().hex[:10], "type": type_, "texte": texte, "date": datetime.now(TZ).isoformat(timespec="seconds"), **kw}], ensure_ascii=False))
def etat(**kw):
    s = jload(ST_F, {}); s.update(kw); ST_F.write_text(json.dumps(s, ensure_ascii=False)); return s

def cookie():
    try: c = COOKIE_F.read_text().strip()
    except OSError: return None
    return c if "=" in c else ("identity=" + c if c else None)   # accepte la valeur seule ou l'en-tête Cookie complet

def cookie_mort(raison):
    s = etat()
    if not s.get("cookie_mort"):   # une seule notification par épisode
        evenement("cookie", "Bandcamp : cookie expiré. Safari › Développement › Inspecteur web › Stockage › Cookies › bandcamp.com › identity, puis : security add-generic-password -U -s umbrel-bandcamp -a identity -w ; ~/Developer/Umbrel/Scripts/bandcamp/bandcamp_cookie.sh")
    etat(cookie_mort=True); log("cookie expiré/absent :", raison)

def livrer():
    """Albums terminés (staging/Artiste/Album) -> racine de À trier, via un dossier caché puis renommage (le trieur ignore les dossiers cachés)."""
    n = []
    for art in sorted(p for p in STAGING.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for alb in sorted(p for p in art.iterdir() if p.is_dir()):
            nom = f"{art.name} - {alb.name}".replace("/", "-"); dst = TRIER / nom; k = 2
            while dst.exists(): dst = TRIER / f"{nom} ({k})"; k += 1
            tmp = TRIER / f".bandcamp-{uuid.uuid4().hex[:8]}"; shutil.copytree(alb, tmp); os.rename(tmp, dst); shutil.rmtree(alb)
            n.append(dst.name)
        shutil.rmtree(art, ignore_errors=True)
    return n

def passe():
    if not LOCK.acquire(blocking=False): return "occupé"
    try:
        for d in (DATA, STAGING, TMP): d.mkdir(parents=True, exist_ok=True)
        c = cookie()
        if not c: return cookie_mort("fichier absent ou vide") or "cookie"
        try:
            b = Bandcamp(cookies=c); b.verify_authentication()
            if not b.is_authenticated: return cookie_mort("pas de compte reconnu") or "cookie"
            b.load_purchases()
        except BandcampError as e: return cookie_mort(str(e)[:120]) or "cookie"
        etat(cookie_mort=False, echecs=0)
        achats = [i for i in b.purchases if i.item_id]
        ign = Ignores(str(IGN_F), "")
        if not ST_F.exists() or not jload(ST_F, {}).get("initialise"):
            # premier passage : toute la collection actuelle (précommandes comprises) est marquée « déjà faite », rien n'est téléchargé
            for i in achats: ign.add(i)
            etat(initialise=datetime.now(TZ).isoformat(timespec="seconds"), marques=len(achats))
            evenement("initial", f"Bandcamp : {len(achats)} achats existants marqués « déjà faits » (aucun téléchargement)"); log("initialisation :", len(achats), "marqués")
            return "initialisé"
        nouveaux = [i for i in achats if i.item_id not in ign.ids and not i.is_preorder]
        if not nouveaux: etat(derniere=datetime.now(TZ).isoformat(timespec="seconds")); return "rien"
        if len(nouveaux) > MAX_ALBUMS:
            cle = ",".join(sorted(str(i.item_id) for i in nouveaux))
            if etat().get("garde_fou") != cle:
                evenement("garde_fou", f"Bandcamp : {len(nouveaux)} albums à télécharger d'un coup (limite {MAX_ALBUMS}) : rien n'a été téléchargé, l'état est peut-être perdu. À vérifier.")
                etat(garde_fou=cle)
            log("garde-fou :", len(nouveaux)); return "garde-fou"
        log("à télécharger :", [f"{i.band_name} / {i.item_title}" for i in nouveaux])
        s = Syncer(BandcampSyncOptions(cookies=c, dir_path=STAGING, media_format=FORMAT, temp_dir_root=TMP, ign_file_path=IGN_F, skip_item_index=True, max_retries=3, retry_wait=10), auto_run=True)
        for e in s.sync_errors: evenement("erreur", f"Bandcamp : échec de téléchargement ({e[:160]})")
        arrives = livrer()
        for a in arrives: log("livré dans À trier :", a)
        etat(derniere=datetime.now(TZ).isoformat(timespec="seconds"), derniers=arrives)
        return f"{len(arrives)} livré(s)"
    except Exception as e:   # réseau, Bandcamp indisponible : on réessaie à l'heure suivante ; notification après 3 échecs de suite
        n = etat().get("echecs", 0) + 1; etat(echecs=n); log("erreur", type(e).__name__, str(e)[:160])
        if n == 3: evenement("erreur", f"Bandcamp : 3 passages en échec de suite ({type(e).__name__}).")
        return "erreur"
    finally: LOCK.release()

class H(http.server.BaseHTTPRequestHandler):
    def rep(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode(); self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/evenements"): return self.rep(evs())
        self.rep({**jload(ST_F, {}), "evenements_en_attente": len(evs()), "journal": LOG[-30:]})
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/passe": return self.rep({"resultat": passe()})   # passage immédiat (tests, hors horaire)
        if u.path == "/evenements/ack":
            ids = set(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]"))
            with EVLOCK: EV_F.write_text(json.dumps([e for e in evs() if e["id"] not in ids], ensure_ascii=False))
            return self.rep({"ok": True})
        self.rep({"erreur": "inconnu"}, 404)
    def log_message(self, *a): pass

def boucle():
    fait = None
    while True:
        t = datetime.now(TZ); h = (t.date(), t.hour)
        if h != fait and (9 <= t.hour <= 21 or (t.hour == 22 and t.minute < 10)):   # toutes les heures de 9 h à 22 h, rien la nuit
            fait = h
            try: log("passe :", passe())
            except Exception as e: log("passe", type(e).__name__, e)
        time.sleep(60)

if __name__ == "__main__":
    if "--once" in sys.argv: print(passe()); sys.exit()
    threading.Thread(target=boucle, daemon=True).start()
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
