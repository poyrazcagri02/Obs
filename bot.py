#!/usr/bin/env python3
"""YTÜ OBS not takip botu.

Modlar:
  discover : login/menü/not sayfalarının HTML + ekran görüntülerini out/discover'a kaydeder.
  run      : giriş yapar, notları çeker, önceki notlarla karşılaştırır, Telegram'a bildirir.

Gizli bilgiler env'den gelir: OBS_USER, OBS_PASS, TG_TOKEN, TG_CHAT_ID. Hiçbiri loglanmaz.
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from collections import Counter

from PIL import Image, ImageFilter, ImageOps

# --------------------------------------------------------------------------
# Ayarlar. Discover çıktısına bakarak buradaki SELECTORS kesinleşti.
# None = sezgisel (heuristic) bulma kullanılır.
# --------------------------------------------------------------------------
BASE_URL = "https://obs.yildiz.edu.tr/oibs/std/"
LOGIN_URL = BASE_URL

SELECTORS = {
    "username": "#txtParamT01",
    "password": "#txtParamT02",
    "captcha_img": "#imgCaptchaImg",
    "captcha_input": "#txtSecCode",
    "submit": "#btnLogin",
    "logged_in": None,  # giriş sonrası görünen bir element; None ise "şifre alanı yok" kontrolü
}


OUT_DIR = Path(os.environ.get("OUT_DIR", "out"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
GRADES_FILE = STATE_DIR / "grades.json"
STATUS_FILE = STATE_DIR / "status.json"

MAX_LOGIN_ATTEMPTS = 8
FAIL_ALERT_THRESHOLD = 3
NAV_TIMEOUT = 45_000


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def telegram(text, html=False):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        log("Telegram bilgileri yok, mesaj atlanıyor.")
        return False
    ok = True
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [""]
    for chunk in chunks:
        params = {"chat_id": chat, "text": chunk}
        if html:
            params["parse_mode"] = "HTML"
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.HTTPError as e:
            # URL token içerdiği için exception metnini değil, sadece Telegram'ın açıklamasını basıyoruz
            try:
                desc = json.loads(e.read().decode()).get("description", "")
            except Exception:
                desc = ""
            log(f"Telegram gönderimi başarısız: HTTP {e.code} {desc}")
            ok = False
        except Exception as e:
            log(f"Telegram gönderimi başarısız ({type(e).__name__}).")
            ok = False
    return ok


# --------------------------------------------------------------------------
# Captcha
# --------------------------------------------------------------------------
# OBS captcha'sı: gri "A + B = ?" yazısı, renkli gürültü çizgileri/daireler, sağda hoparlör ikonu.
CAPTCHA_CROP = 0.84          # sağdaki hoparlör ikonunu kes
CAPTCHA_MIN_VOTES = 6        # bu kadar varyant aynı sonucu vermezse gönderme, sayfayı yenile
CAPTCHA_MIN_MARGIN = 2       # birinci ile ikinci sonuç arasındaki en az oy farkı
CAPTCHA_MASKS = [(30, 170), (30, 185), (40, 170), (40, 185)]   # (max doygunluk, max parlaklık)
CAPTCHA_VARIANTS = [(sc, dil, bl) for sc in (3, 4) for dil in (0, 3) for bl in (0, 2)]


def captcha_mask(src: Path, sat: int, lum: int, min_size: int = 8):
    """Gri ton maske: sadece doygunluğu düşük ve koyu pikseller (yazı) siyah kalır,
    renkli gürültü atılır; küçük bağlı bileşenler (nokta gürültüsü) silinir."""
    im = Image.open(src).convert("RGB")
    im = im.crop((0, 0, int(im.width * CAPTCHA_CROP), im.height))
    w, h = im.size
    px = im.load()
    on = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            on[y][x] = max(r, g, b) - min(r, g, b) <= sat and (r + g + b) / 3 <= lum
    seen = [[False] * w for _ in range(h)]
    out = Image.new("L", (w, h), 255)
    o = out.load()
    for y in range(h):
        for x in range(w):
            if not on[y][x] or seen[y][x]:
                continue
            stack, comp = [(x, y)], []
            seen[y][x] = True
            while stack:
                cx, cy = stack.pop()
                comp.append((cx, cy))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < w and 0 <= ny < h and on[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
            if len(comp) >= min_size:
                for cx, cy in comp:
                    o[cx, cy] = 0
    return out


def captcha_variant(mask, scale, dilate, blur):
    """Büyütme (3x/4x), isteğe bağlı yumuşatma, threshold ve kalınlaştırma."""
    m = mask.resize((mask.width * scale, mask.height * scale),
                    Image.NEAREST if blur else Image.LANCZOS)
    if blur:
        m = m.filter(ImageFilter.GaussianBlur(blur))
    m = m.point(lambda p: 255 if p > 128 else 0)
    if dilate:
        m = m.filter(ImageFilter.MinFilter(dilate))
    return ImageOps.expand(m, border=20, fill=255)


def _tesseract(path: Path):
    r = subprocess.run(
        ["tesseract", str(path), "stdout", "--psm", "7",
         "-c", "tessedit_char_whitelist=0123456789+=?"],
        capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def solve_captcha(src: Path, work: Path, keep_debug=False):
    """Birden çok ön işleme varyantını OCR'lar, 'A+B' sonuçlarını oylar.
    (özet_metin, toplam|None) döndürür; oy yetersizse toplam None."""
    votes = Counter()
    tmp = work / "_variant.png"
    for i, (sat, lum) in enumerate(CAPTCHA_MASKS):
        mask = captcha_mask(src, sat, lum)
        for j, (sc, dil, bl) in enumerate(CAPTCHA_VARIANTS):
            captcha_variant(mask, sc, dil, bl).save(tmp)
            if keep_debug and i == 0 and j == 0:
                captcha_variant(mask, sc, dil, bl).save(src.with_name(src.stem + "_processed.png"))
            m = re.search(r"(\d{1,2})\+(\d{1,2})", _tesseract(tmp))
            if m:
                votes[(int(m.group(1)), int(m.group(2)))] += 1
    tmp.unlink(missing_ok=True)
    top = votes.most_common(2)
    desc = ", ".join(f"{a}+{b}:{n}" for (a, b), n in top) or "okunamadı"
    if not top or top[0][1] < CAPTCHA_MIN_VOTES or (len(top) > 1 and top[0][1] - top[1][1] < CAPTCHA_MIN_MARGIN):
        return desc, None
    a, b = top[0][0]
    return desc, a + b


# --------------------------------------------------------------------------
# Sayfa yardımcıları
# --------------------------------------------------------------------------
def save_page(page, name, folder):
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        log(f"{name}: html alınamadı ({type(e).__name__})")
    try:
        page.screenshot(path=str(folder / f"{name}.png"), full_page=True)
    except Exception as e:
        log(f"{name}: screenshot alınamadı ({type(e).__name__})")
    # iframe'ler (eski OBS sayfaları sık kullanır)
    for i, fr in enumerate(page.frames[1:], 1):
        try:
            (folder / f"{name}_frame{i}.html").write_text(
                f"<!-- frame url: {fr.url} -->\n" + fr.content(), encoding="utf-8")
        except Exception:
            pass


def _loc(page, key, fallback):
    return page.locator(SELECTORS[key] or fallback).first


def find_login_fields(page):
    pw = _loc(page, "password", "input[type=password]:visible")
    if SELECTORS["username"] and SELECTORS["captcha_input"]:
        return _loc(page, "username", ""), pw, _loc(page, "captcha_input", "")
    inputs = page.locator(
        "input:visible:not([type=hidden]):not([type=password]):not([type=submit])"
        ":not([type=button]):not([type=checkbox]):not([type=image])")
    n = inputs.count()
    if n == 0:
        raise RuntimeError("Login sayfasında text input bulunamadı")
    user = _loc(page, "username", "") if SELECTORS["username"] else inputs.nth(0)
    cap = None
    for i in range(n):
        attrs = " ".join(filter(None, [inputs.nth(i).get_attribute("id"),
                                       inputs.nth(i).get_attribute("name")]))
        if re.search(r"captcha|guvenlik|güvenlik|kod|code|security|image", attrs, re.I):
            cap = inputs.nth(i)
            break
    if SELECTORS["captcha_input"]:
        cap = _loc(page, "captcha_input", "")
    if cap is None:
        cap = inputs.nth(n - 1)
    return user, pw, cap


def find_captcha_img(page):
    if SELECTORS["captcha_img"]:
        return page.locator(SELECTORS["captcha_img"]).first
    imgs = page.locator("img:visible")
    best = None
    for i in range(imgs.count()):
        el = imgs.nth(i)
        desc = " ".join(filter(None, [el.get_attribute("src"), el.get_attribute("id"),
                                      el.get_attribute("alt"), el.get_attribute("class")]))
        if re.search(r"captcha|guvenlik|güvenlik|security|kod|code|image\.aspx|verify", desc, re.I):
            return el
        box = el.bounding_box()
        if box and box["width"] < 300 and box["height"] < 120 and best is None:
            best = el
    if best is None:
        raise RuntimeError("Captcha resmi bulunamadı")
    return best


def login_form_present(page):
    return page.locator(SELECTORS["password"] or "input[type=password]:visible").count() > 0


def is_logged_in(page):
    if SELECTORS["logged_in"]:
        return page.locator(SELECTORS["logged_in"]).count() > 0
    return not login_form_present(page)


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass


def fill_field(el, value):
    """OBS alanları onfocus'a kadar readonly; kilidi kaldırıp doldur."""
    el.evaluate("e => { e.removeAttribute('readonly'); e.readOnly = false; }")
    el.click()
    el.fill(value)


def login(page, discover_dir=None):
    """Captcha çözerek giriş yapar. discover_dir verilirse her denemenin captcha'sını kaydeder."""
    user, pw_ = os.environ["OBS_USER"], os.environ["OBS_PASS"]
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        page.goto(LOGIN_URL, timeout=NAV_TIMEOUT)  # her denemede sayfayı yenile
        settle(page)
        if is_logged_in(page) and not login_form_present(page):
            log("Zaten giriş yapılmış görünüyor.")
            return True
        try:
            u_el, p_el, c_el = find_login_fields(page)
            img_el = find_captcha_img(page)
            work = (discover_dir or OUT_DIR / "tmp")
            work.mkdir(parents=True, exist_ok=True)
            raw_png = work / f"captcha_{attempt}.png"
            img_el.screenshot(path=str(raw_png))
            raw, total = solve_captcha(raw_png, work, keep_debug=bool(discover_dir))
        except Exception as e:
            log(f"Deneme {attempt}: login alanları/captcha hatası: {type(e).__name__}: {str(e).splitlines()[0]}")
            continue
        log(f"Deneme {attempt}: OCR oyları [{raw}] toplam={total}")
        if discover_dir:
            (discover_dir / f"captcha_{attempt}.txt").write_text(f"votes={raw} sum={total}\n")
        else:
            raw_png.unlink(missing_ok=True)
        if total is None:
            continue
        fill_field(u_el, user)
        fill_field(p_el, pw_)
        fill_field(c_el, str(total))
        if SELECTORS["submit"] or page.locator("button[type=submit], input[type=submit]").count():
            _loc(page, "submit", "button[type=submit], input[type=submit]").click()
        else:
            c_el.press("Enter")
        settle(page)
        if is_logged_in(page):
            log(f"Giriş başarılı (deneme {attempt}).")
            return True
        msg = login_error_text(page)
        log(f"Deneme {attempt}: giriş başarısız. Sayfa mesajı: {msg!r}")
        if discover_dir:
            save_page(page, f"login_fail_{attempt}", discover_dir)
        if re.search(r"şifre|parola|kullanıcı|kilit|bloke", msg, re.I) and \
                not re.search(r"güvenlik|kod", msg, re.I):
            log("Kimlik bilgisi hatası görünüyor; hesap kilitlenmesin diye tekrar denenmiyor.")
            return False
    return False


def login_error_text(page):
    parts = []
    for sel in ("#lblSonuclar", "#UtModal_message", "#UtModal_msgBox"):
        try:
            loc = page.locator(sel)
            if loc.count():
                t = loc.first.inner_text(timeout=2000).strip()
                if t:
                    parts.append(t)
        except Exception:
            pass
    return " | ".join(parts)[:300]


NOTES_MENU_TEXT = "Not Listesi"   # Ders ve Dönem İşlemleri > Not Listesi
NOTES_HREF_JS = """(label) => {
  for (const a of document.querySelectorAll('a.nav-link')) {
    const p = a.querySelector('p');
    if (p && p.textContent.trim() === label) {
      const m = (a.getAttribute('onclick') || '').match(/'(\\/oibs\\/[^']+)'/);
      if (m) return m[1];
    }
  }
  return null;
}"""


def notes_frame(page):
    return page.frame(name="IFRAME1")


def open_notes(page):
    """Menüdeki 'Not Listesi' linkinin (oturuma özel gkm token'lı) adresini alıp IFRAME1'e yükler.
    Not sayfasının frame'ini döndürür."""
    href = page.evaluate(NOTES_HREF_JS, NOTES_MENU_TEXT)
    if not href:
        raise RuntimeError("Menüde 'Not Listesi' linki bulunamadı")
    before = notes_frame(page).url if notes_frame(page) else ""
    page.evaluate("u => { document.getElementById('IFRAME1').src = u; }", href)
    deadline = time.time() + 40
    fr = None
    while time.time() < deadline:
        fr = notes_frame(page)
        if fr and fr.url != before and "start.aspx" not in fr.url and fr.url != "about:blank":
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError("Not sayfası iframe'e yüklenmedi")
    try:
        fr.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    log(f"Not sayfası açıldı: {urllib.parse.urlsplit(fr.url).path}")
    return fr


# --------------------------------------------------------------------------
# Not tablosu -> {ders: {sınav: not}}
# --------------------------------------------------------------------------
TABLES_JS = """() => Array.from(document.querySelectorAll('table')).map(t =>
  Array.from(t.rows).map(r => Array.from(r.cells).map(c => c.innerText.trim().replace(/\\s+/g,' '))))"""


COURSE_TITLE_RE = re.compile(
    r"^(?P<code>[A-ZÇĞİÖŞÜ]{2,}\d+[A-Z]?)\(\d+\)\s*-\s*(?P<name>.+?)\s*\|\s*"
    r"Ort:\s*(?P<ort>.*?)\s*Not:\s*(?P<harf>.*?)\s*Durumu:", re.S)
EMPTY_VALUES = {"", "-", "—", "–"}


def parse_tables(tables):
    """not_listesi_op.aspx yapısı: her ders için
       1) başlık tablosu: 'KOD(şube) - Ders Adı | Ort: x Not: y Durumu: ...'
       2) sınav tablosu: 'Sınav Adı | Not | İlan Tarihi | Sınıf Ort. | Detay'
    Dönüş: {ders: {sınav: not}}. Henüz notu olmayan dersler boş dict ile yer alır.
    Ders başlığı hiç bulunamazsa ValueError."""
    result, current, found = {}, None, False
    for rows in tables:
        if not rows:
            continue
        m = COURSE_TITLE_RE.match(rows[0][0]) if len(rows) == 1 and rows[0] else None
        if m:
            found = True
            name = m.group("name").strip()
            current = name if name not in result else f"{m.group('code')} {name}"
            result[current] = {}
            if m.group("ort").strip() not in EMPTY_VALUES:
                result[current]["Ortalama"] = m.group("ort").strip()
            if m.group("harf").strip() not in EMPTY_VALUES:
                result[current]["Harf Notu"] = m.group("harf").strip()
            continue
        header = [c.strip().lower() for c in rows[0]]
        if current is None or "sınav adı" not in header or "not" not in header:
            continue
        i_name, i_grade = header.index("sınav adı"), header.index("not")
        for row in rows[1:]:
            if len(row) <= max(i_name, i_grade):
                continue
            exam, grade = row[i_name].strip(), row[i_grade].strip()
            if exam and grade not in EMPTY_VALUES:
                result[current][exam] = grade
        current = None
    if not found:
        raise ValueError("Not sayfasında ders başlığı bulunamadı")
    return result


def scrape_grades(frame):
    tables = frame.evaluate(TABLES_JS)
    return parse_tables(tables)


def diff_grades(old, new):
    msgs = []
    for course, exams in new.items():
        for exam, val in exams.items():
            prev = old.get(course, {}).get(exam)
            if prev is None:
                msgs.append(f"📢 {course} – {exam}: {val}")
            elif prev != val:
                msgs.append(f"📢 {course} – {exam}: {val} (önceki: {prev})")
    return msgs


def summary(grades):
    lines = ["📋 OBS not takibi başladı. Mevcut notlar:"]
    for course, exams in grades.items():
        detail = ", ".join(f"{e} {v}" for e, v in exams.items()) or "henüz not yok"
        lines.append(f"• {course}: {detail}")
    return "\n".join(lines)


def _cut(text, width):
    return text if len(text) <= width else text[:width - 1] + "…"


def grades_table(grades):
    """Notu olan dersler için eş aralıklı (monospace) tablo, Telegram HTML <pre> içinde."""
    rows = [(c, e, v) for c, exams in grades.items() for e, v in exams.items()]
    if not rows:
        return ""
    wc = min(16, max(len(r[0]) for r in rows))
    we = min(11, max(len(r[1]) for r in rows))
    wv = max(3, max(len(r[2]) for r in rows))
    lines = [f"{'Ders':<{wc}} {'Sınav':<{we}} {'Not':>{wv}}", "-" * (wc + we + wv + 2)]
    prev = None
    for c, e, v in rows:
        name = _cut(c, wc) if c != prev else ""
        lines.append(f"{name:<{wc}} {_cut(e, we):<{we}} {v:>{wv}}")
        prev = c
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def new_grades_message(msgs, grades):
    text = "\n".join(html.escape(m) for m in msgs)
    table = grades_table(grades)
    if table and len(text) + len(table) < 3800:
        text += "\n\n📊 Tüm notlar:\n" + table
    return text


# --------------------------------------------------------------------------
# Durum (cache içinde tutulur, repoya commit edilmez)
# --------------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def record_failure(reason):
    st = load_json(STATUS_FILE, {})
    st["fails"] = st.get("fails", 0) + 1
    st.setdefault("alerted", False)
    log(f"Başarısız çalıştırma #{st['fails']} üst üste: {reason}")
    if st["fails"] >= FAIL_ALERT_THRESHOLD and not st["alerted"]:
        if telegram(f"⚠️ OBS botu {st['fails']} çalıştırmadır üst üste başarısız: {reason}"):
            st["alerted"] = True
    save_json(STATUS_FILE, st)


def record_success():
    st = load_json(STATUS_FILE, {})
    if st.get("alerted"):
        telegram("✅ OBS botu tekrar çalışıyor.")
    save_json(STATUS_FILE, {"fails": 0, "alerted": False, "initialized": True})


# --------------------------------------------------------------------------
# Modlar
# --------------------------------------------------------------------------
def launch(p):
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1366, "height": 900}, locale="tr-TR")
    ctx.set_default_timeout(20_000)
    return browser, ctx.new_page()


def mode_discover():
    from playwright.sync_api import sync_playwright
    d = OUT_DIR / "discover"
    d.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser, page = launch(p)
        try:
            page.goto(LOGIN_URL, timeout=NAV_TIMEOUT)
            settle(page)
            log(f"Login sayfası URL: {page.url}")
            save_page(page, "01_login", d)
            summ = {
                "url": page.url,
                "inputs": page.eval_on_selector_all(
                    "input,button,select",
                    "els => els.map(e => ({tag:e.tagName,type:e.type,id:e.id,name:e.name,cls:e.className,value:e.type==='password'?'':e.value}))"),
                "images": page.eval_on_selector_all(
                    "img", "els => els.map(e => ({src:e.src,id:e.id,alt:e.alt,w:e.width,h:e.height}))"),
                "frames": [f.url for f in page.frames],
            }
            save_json(d / "01_login_summary.json", summ)
            # Hassas bilgi içermeyen özet; uzun değerler (ViewState vb.) kısaltılır.
            for x in summ["inputs"]:
                if x.get("value") and len(x["value"]) > 40:
                    x["value"] = x["value"][:40] + "..."
            log("LOGIN_SUMMARY " + json.dumps(summ, ensure_ascii=False))
            try:
                find_captcha_img(page).screenshot(path=str(d / "captcha_page_load.png"))
            except Exception as e:
                log(f"Captcha resmi alınamadı: {e}")

            ok = False
            if os.environ.get("OBS_USER") and os.environ.get("OBS_PASS"):
                try:
                    ok = login(page, discover_dir=d)
                except Exception as e:
                    log(f"Login hatası: {type(e).__name__}: {str(e).splitlines()[0]}")
            else:
                log("OBS_USER/OBS_PASS yok; sadece login sayfası kaydedildi.")
            log(f"Giriş sonucu: {ok}")
            if not ok:
                save_page(page, "02_after_failed_login", d)
                return
            save_page(page, "02_menu", d)
            links = page.eval_on_selector_all(
                "a", "els => els.map(e => ({text:e.innerText.trim(),href:e.getAttribute('href'),id:e.id,target:e.target}))")
            save_json(d / "02_links.json", links)
            try:
                fr = open_notes(page)
                save_page(page, "03_notes", d)
                (d / "03_notes_iframe.html").write_text(fr.content(), encoding="utf-8")
                tables = fr.evaluate(TABLES_JS)
                save_json(d / "03_notes_tables.json", tables)
                # Log'a sadece yapı: tablo boyutları ve ilk satır (başlık) - not değerleri yok
                log(f"{len(tables)} tablo bulundu (ayrıntı artifact'te).")
                try:
                    g = parse_tables(tables)
                    log(f"Ayrıştırıcı {len(g)} ders, {sum(len(v) for v in g.values())} not buldu.")
                    # Satır yapısı (değerler yerine tipler): ilk sınav tablosunun 2. satırı
                    for t in tables:
                        if len(t) > 1 and t[0] and t[0][0] == "Sınav Adı":
                            log("ORNEK_SATIR_YAPISI " + json.dumps(
                                [("sayı" if re.fullmatch(r"[\d.,]+", c) else ("boş" if not c else c[:20]))
                                 for c in t[1]], ensure_ascii=False))
                            break
                except Exception as e:
                    log(f"Ayrıştırma hatası: {type(e).__name__}: {e}")
                selects = fr.eval_on_selector_all(
                    "select", "els => els.map(e => ({id:e.id,name:e.name,options:Array.from(e.options).map(o=>o.text+(o.selected?' *':''))}))")
                log("SELECTS " + json.dumps(selects, ensure_ascii=False))
            except Exception as e:
                log(f"Not sayfası açılamadı: {type(e).__name__}: {e}")
                save_page(page, "03_notes_failed", d)
        finally:
            browser.close()


def mode_run():
    from playwright.sync_api import sync_playwright
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser, page = launch(p)
        try:
            if not login(page):
                save_page(page, "run_login_failed", OUT_DIR / "error")
                record_failure("giriş yapılamadı (captcha/kimlik bilgisi)")
                return 1
            fr = open_notes(page)
            try:
                grades = scrape_grades(fr)
            except Exception:
                save_page(page, "run_notes_parse_failed", OUT_DIR / "error")
                raise
        except Exception as e:
            try:
                save_page(page, "run_error", OUT_DIR / "error")
            except Exception:
                pass
            record_failure(f"{type(e).__name__}: {e}")
            return 1
        finally:
            browser.close()

    old = load_json(GRADES_FILE, None)
    status = load_json(STATUS_FILE, {})
    if old is None or not status.get("initialized"):
        sent = telegram(summary(grades))
    else:
        msgs = diff_grades(old, grades)
        log(f"{len(msgs)} yeni/değişen not.")
        sent = telegram(new_grades_message(msgs, grades), html=True) if msgs else True
    if not sent:
        # Mesaj gitmediyse durumu güncelleme; bir sonraki çalıştırma tekrar dener.
        record_failure("Telegram mesajı gönderilemedi")
        return 1
    save_json(GRADES_FILE, grades)
    record_success()
    return 0


def mode_test():
    """Örnek notlarla mesaj biçimini Telegram'a gönderir; OBS'ye girmez, durumu değiştirmez."""
    old = {"Matematik 1": {"Vize 1": "70"}, "Medical Informatics": {}}
    new = {"Matematik 1": {"Vize 1": "78", "Ödev 1": "90"},
           "Medical Informatics": {"Ara Sınav": "65"},
           "Sosyal Politikalar İktisadı": {"Vize": "82"}}
    msg = "🧪 DENEME MESAJI (gerçek notlar değil)\n\n" + new_grades_message(diff_grades(old, new), new)
    return 0 if telegram(msg, html=True) else 1


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "run")).strip()
    if mode == "discover":
        mode_discover()
        return 0
    if mode == "run":
        return mode_run()
    if mode == "test":
        return mode_test()
    log(f"Bilinmeyen mod: {mode}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
