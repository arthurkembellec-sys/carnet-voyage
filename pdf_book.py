# -*- coding: utf-8 -*-
"""
pdf_book.py — Moteur PDF pro pour Notre Histoire (v3).

Cible : impression Blurb / Cewe / Saal Digital.
- Fond perdu 3 mm sur les pleines pages
- Reliure intelligente : marge intérieure 16 mm, extérieure 12 mm
- Pages recto/verso conscientes du sens (gouttière côté reliure)
- 3 modes par photo : normal / pleine page / double page (spread)
- Couverture configurable (cover_photo_id sur carnet)
- Lettres a/b/c reliant photos et légendes en marge
- Cartographie : carte d'ensemble + cartes de chapitre + mini-cartes en marge

API publique : render_carnet_pdf(...) -> BytesIO
"""

from __future__ import annotations
import io
import os
from typing import Callable, Optional


# ── Constantes ────────────────────────────────────────────────────────────
BLEED_MM = 3.0
GUTTER_INNER_MM = 16.0
GUTTER_OUTER_MM = 12.0
GUTTER_TOP_MM = 12.0
GUTTER_BOTTOM_MM = 14.0
SAFETY_MM = 5.0  # marge texte minimum

# Couleurs de la charte (sRGB 0..1)
CREAM_RGB = (0.988, 0.988, 0.985)  # v4.3 : blanc neutre (fini le parchemin)
INK_RGB = (0.110, 0.102, 0.090)
INK_SOFT_RGB = (0.240, 0.227, 0.207)
INK_FAINT_RGB = (0.420, 0.410, 0.380)
INK_GHOST_RGB = (0.640, 0.611, 0.572)
LINE_RGB = (0.880, 0.850, 0.800)
ACCENT_RGB = (0.769, 0.396, 0.290)  # terracotta #C4654A


def _full_bleed_mode(item):
    """Retourne 'normal' | 'full' | 'spread' pour un item donné."""
    v = item.get('full_bleed_override')
    if v == 1:
        return 'full'
    if v == 2:
        return 'spread'
    return 'normal'


def _item_ts(it):
    """Cle chrono normalisee 'YYYY-MM-DD HH:MM:SS' d'une page d'album
    (photo > video > date d'ajout). Neutralise les formats mixtes T/espace/Z."""
    s = str(it.get('photo_taken_at') or it.get('video_taken_at')
            or it.get('created_at') or '').strip().replace('T', ' ')
    if s.endswith('Z'):
        s = s[:-1]
    return s[:19]


# ── v4.7 : emoji dans le livre — police NotoEmoji (monochrome) embarquée ──
_EMOJI_FONT = 'NotoEmoji'
_EMOJI_OK = False

def _register_emoji_font():
    global _EMOJI_OK
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        import os as _os
        p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                          'static', 'vendor', 'fonts', 'NotoEmoji.ttf')
        if _os.path.exists(p):
            pdfmetrics.registerFont(TTFont(_EMOJI_FONT, p))
            _EMOJI_OK = True
    except Exception:
        _EMOJI_OK = False

_register_emoji_font()


def _is_emoji_ch(ch):
    o = ord(ch)
    return ((0x1F000 <= o <= 0x1FAFF) or (0x2600 <= o <= 0x27BF)
            or (0x2B00 <= o <= 0x2BFF) or (0x1F1E6 <= o <= 0x1F1FF)
            or o in (0x2764, 0x2728, 0x263A, 0x2B50))


def _strip_joiners(s):
    """Retire les selecteurs de variation / ZWJ (non rendus par la police statique)."""
    return ''.join(ch for ch in (s or '') if ord(ch) not in (0xFE0F, 0x200D))


def _emoji_runs(text):
    """Decoupe en [(est_emoji, morceau), ...] pour alterner les polices."""
    runs, cur, cur_e = [], '', None
    for ch in _strip_joiners(text):
        e = _is_emoji_ch(ch)
        if cur_e is None or e == cur_e:
            cur += ch
            cur_e = e
        else:
            runs.append((cur_e, cur))
            cur, cur_e = ch, e
    if cur:
        runs.append((cur_e, cur))
    return runs


def _page_day(it):
    """Jour 'YYYY-MM-DD' d'une page (date de prise, sinon date d'ajout)."""
    ts = _item_ts(it)
    return ts[:10] if ts and len(ts) >= 10 else ''


_JOURS_FR = ['LUNDI', 'MARDI', 'MERCREDI', 'JEUDI', 'VENDREDI', 'SAMEDI', 'DIMANCHE']
_MOIS_FR = ['JANVIER', 'FEVRIER', 'MARS', 'AVRIL', 'MAI', 'JUIN', 'JUILLET',
            'AOUT', 'SEPTEMBRE', 'OCTOBRE', 'NOVEMBRE', 'DECEMBRE']


def _compute_book_stats(pages):
    """v5.21 (chantier D) — les chiffres du voyage, depuis les pages memes :
    jours, photos, videos, villes traversees (ordre de rencontre) et
    kilometres parcourus (haversine sur les photos GPS chronologiques)."""
    import math
    photos = [p for p in pages if p.get('photo_path')]
    videos = [p for p in pages if p.get('video_path')]
    days = sorted({_page_day(p) for p in pages if _page_day(p)})
    villes, seen = [], set()
    for p in photos:
        v = (p.get('photo_city') or '').strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            villes.append(v)
    pts = [(p['photo_gps_lat'], p['photo_gps_lng']) for p in photos
           if p.get('photo_gps_lat') is not None
           and p.get('photo_gps_lng') is not None]
    km = 0.0
    for a, b in zip(pts, pts[1:]):
        la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
        h = (math.sin((la2 - la1) / 2) ** 2 +
             math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
        km += 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))
    return {'jours': len(days), 'photos': len(photos), 'videos': len(videos),
            'lieux': len(villes), 'villes': villes, 'km': km}


def _date_longue_fr(day):
    """'Samedi 11 juillet 2026' — jamais d'ISO brut sur un objet offert."""
    from datetime import datetime as _dt
    try:
        d = _dt.strptime(str(day)[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return str(day)
    return (f"{_JOURS_FR[d.weekday()].capitalize()} {d.day} "
            f"{_MOIS_FR[d.month - 1].lower()} {d.year}")


def _day_label_fr(day, chunk=None):
    """v5.17 — 'SAMEDI 11 JUILLET · SALERS' : le livre marque les changements
    de date et de lieu, comme l'album (retour d'Arthur du 2026-08-15)."""
    from datetime import datetime as _dt
    try:
        d = _dt.strptime(day, '%Y-%m-%d')
    except (ValueError, TypeError):
        return ''
    lbl = f"{_JOURS_FR[d.weekday()]} {d.day} {_MOIS_FR[d.month - 1]}"
    villes = [it.get('photo_city') for it in (chunk or []) if it.get('photo_city')]
    if villes:
        top = max(set(villes), key=villes.count)
        lbl += '  ·  ' + str(top).upper()
    return lbl


def _program_chunks(pages_main, n_per_page):
    """v5.17 — LA segmentation de reference du livre, partagee entre le
    program PDF, build_margin_plan et l'apercu (rester synchrones est vital :
    les notes de marge sont distribuees par index de page composite).

    Retourne [('chunk', [pages]) | ('full', page) | ('spread', page)].
    Une page composite ne melange JAMAIS deux journees : le chunk se coupe
    au changement de jour, la ou l'album coupe ses sections.
    """
    out = []
    cur = []
    cur_day = None

    def flush():
        nonlocal cur, cur_day
        if cur:
            out.append(('chunk', cur))
        cur = []
        cur_day = None

    for p in pages_main:
        mode = _full_bleed_mode(p)
        if mode != 'normal':
            flush()
            out.append((mode, p))
            continue
        d = _page_day(p)
        if cur and d and cur_day and d != cur_day:
            flush()
        cur.append(p)
        if d:
            cur_day = d
        if len(cur) >= n_per_page:
            flush()
    flush()
    return out


def build_margin_plan(pages_main, margin_pool, n_per_page):
    """v3.4.1 : distribution des notes de marge par page composite, alignee
    sur les dates (fusion chronologique) au lieu de la repartition uniforme.

    Une note est placee sur la premiere page composite dont les photos
    couvrent sa date/heure. Plafond par page (lisibilite de la zone marge) ;
    l'excedent deborde sur les pages suivantes ; le reliquat va sur la
    derniere page composite. Retourne plan[i] = notes de la i-eme page
    composite — meme decoupage en chunks que le program PDF et que
    apercu.html (chunks de n_per_page, coupes par les pleines pages/spreads).
    """
    # v5.17 : segmentation partagee (coupe aussi aux changements de journee)
    chunks = [c for k, c in _program_chunks(pages_main, n_per_page) if k == 'chunk']
    n = len(chunks)
    plan = [[] for _ in range(n)]
    pool = list(margin_pool or [])
    if not n or not pool:
        return plan
    # v4.2 : une note ancree (anchor_page_id) va sur la page composite qui
    # contient sa photo — hors plafond, c'est un choix explicite de l'auteur.
    page_to_chunk = {}
    for k, chunk in enumerate(chunks):
        for p in chunk:
            pid = p.get('id') if isinstance(p, dict) else None
            if pid is not None:
                page_to_chunk[pid] = k
    rest = []
    for m in pool:
        aid = m.get('anchor_page_id') if isinstance(m, dict) else None
        k = page_to_chunk.get(aid) if aid else None
        if k is not None:
            plan[k].append(m)
        else:
            rest.append(m)
    pool = rest
    if not pool:
        return plan
    cap = max(3, -(-len(pool) // n))  # au moins l'ancien "per" uniforme
    idx = 0
    for k, chunk in enumerate(chunks):
        if idx >= len(pool):
            break
        chunk_max = max((_item_ts(p) for p in chunk), default='')
        while idx < len(pool) and len(plan[k]) < cap:
            m_ts = _item_ts(pool[idx])
            # Note plus tardive que les photos de la page -> page suivante
            # (sauf derniere page : on y pose tout ce qui reste)
            if k < n - 1 and m_ts and chunk_max and m_ts > chunk_max:
                break
            plan[k].append(pool[idx])
            idx += 1
    if idx < len(pool):
        plan[-1].extend(pool[idx:])
    return plan


def render_carnet_pdf(
    *,
    carnet,
    pages_data,
    fmt_info,                # tuple (label, w_mm, h_mm)
    layout,                  # '1'|'2'|'3'|'4'
    margin_pos,              # 'outer'|'inner'|'right'|'left'|'bottom'|'end'
    upload_dir,
    show_overview_map=True,
    show_section_maps=True,
    show_letters=True,
    cover_photo_id=None,
    geo_summary=None,
    fetch_static_map=None,    # callable(lat,lng,zoom,wpx,hpx,markers=...) -> bytes|None
    compute_zoom=None,        # callable(min_lat,max_lat,min_lng,max_lng,wpx,hpx) -> int
    section_zone_map_resolver=None,  # callable(items_chunk) -> dict|None
    book_map=None,            # v5.20 : callable(w_px, h_px) -> (png|None, timeline)
    map_timeline_side='right',  # 'left' | 'right' | 'none'
    book_meta=None,           # v5.21 : {'members': ['Arthur', 'Laurie'], ...}

    qr_make=None,             # qrcode.make
    video_url_for=None,       # callable(token) -> str
):
    """Rend le PDF complet et retourne un BytesIO."""
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader

    label_fmt, w_mm, h_mm = fmt_info
    n_per_page = int(layout) if layout in ('1', '2', '3', '4') else 1

    # Compatibilité : les anciens 'right'/'left' deviennent 'outer'/'inner'
    if margin_pos == 'right':
        margin_pos_eff = 'outer'
    elif margin_pos == 'left':
        margin_pos_eff = 'inner'
    else:
        margin_pos_eff = margin_pos

    # v5.18 (audit DA §1) : fond perdu PERMANENT — deux carnets du meme
    # format doivent produire le meme gabarit de document chez l'imprimeur.
    pages_main = pages_data['main']
    bleed = BLEED_MM * mm

    # Dimensions totales (avec bleed) et trim (zone finale après coupe)
    page_w = w_mm * mm + 2 * bleed
    page_h = h_mm * mm + 2 * bleed

    buf = io.BytesIO()
    pdf = pdf_canvas.Canvas(buf, pagesize=(page_w, page_h))
    pdf.setTitle(carnet['title'] or 'Notre Histoire')
    pdf.setAuthor("Notre Histoire")

    # Helpers ───────────────────────────────────────────────────────────────
    def _fill_page_cream():
        pdf.setFillColorRGB(*CREAM_RGB)
        pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    def _trim_box():
        """Retourne (x, y, w, h) de la zone trim (sans bleed)."""
        return (bleed, bleed, page_w - 2 * bleed, page_h - 2 * bleed)

    def _content_box(side):
        """Retourne (x, y, w, h) de la zone contenu utile selon recto/verso.

        side ∈ {'recto','verso'}. Recto = page de droite (impaire),
        gouttière à gauche. Verso = page de gauche (paire), gouttière à droite.
        """
        tx, ty, tw, th = _trim_box()
        if side == 'recto':
            x = tx + GUTTER_INNER_MM * mm
            w = tw - (GUTTER_INNER_MM + GUTTER_OUTER_MM) * mm
        else:
            x = tx + GUTTER_OUTER_MM * mm
            w = tw - (GUTTER_INNER_MM + GUTTER_OUTER_MM) * mm
        y = ty + GUTTER_BOTTOM_MM * mm
        h = th - (GUTTER_TOP_MM + GUTTER_BOTTOM_MM) * mm
        return (x, y, w, h)

    def _mixed_width(text, font_name, font_size):
        from reportlab.pdfbase.pdfmetrics import stringWidth
        if not _EMOJI_OK:
            return stringWidth(text, font_name, font_size)
        return sum(stringWidth(t, _EMOJI_FONT if e else font_name, font_size)
                   for e, t in _emoji_runs(text))

    def _draw_mixed(x, y, text, align='left'):
        """v4.7 : dessine `text` avec la police courante, emojis via NotoEmoji."""
        from reportlab.pdfbase.pdfmetrics import stringWidth
        font_name, font_size = pdf._fontname, pdf._fontsize
        if not _EMOJI_OK or not any(_is_emoji_ch(c) for c in _strip_joiners(text)):
            if align == 'center':
                pdf.drawCentredString(x, y, text)
            elif align == 'right':
                pdf.drawRightString(x, y, text)
            else:
                pdf.drawString(x, y, text)
            return
        text2 = _strip_joiners(text)
        total = _mixed_width(text2, font_name, font_size)
        if align == 'center':
            sx = x - total / 2
        elif align == 'right':
            sx = x - total
        else:
            sx = x
        for e, t in _emoji_runs(text2):
            f = _EMOJI_FONT if e else font_name
            pdf.setFont(f, font_size)
            pdf.drawString(sx, y, t)
            sx += stringWidth(t, f, font_size)
        pdf.setFont(font_name, font_size)

    def _wrap_text(text, cx, cy, max_width, line_height=14, max_lines=99,
                   align='center'):
        """Wrap basique multi-ligne."""
        from reportlab.pdfbase.pdfmetrics import stringWidth
        font_name = pdf._fontname
        font_size = pdf._fontsize
        words = (text or '').split()
        lines, cur = [], []
        for w in words:
            test = ' '.join(cur + [w])
            if _mixed_width(test, font_name, font_size) <= max_width:
                cur.append(w)
            else:
                if cur:
                    lines.append(' '.join(cur))
                cur = [w]
        if cur:
            lines.append(' '.join(cur))
            
        # Chantier C (fuite n°4) : une troncature se VOIT — ellipse sur la
        # derniere ligne gardee au lieu d'une coupe muette.
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            if lines:
                lines[-1] = lines[-1].rstrip('.,;: ') + chr(8230)
        total_h = len(lines) * line_height
        y = cy + total_h / 2
        for line in lines:
            if align == 'left':
                _draw_mixed(cx, y, line, 'left')
            elif align == 'right':
                _draw_mixed(cx, y, line, 'right')
            else:
                _draw_mixed(cx, y, line, 'center')
            y -= line_height

    def _wrap_text_left(text, x, y_top, max_width, line_height=10, max_lines=99):
        """Wrap left-aligned, ancré au haut. Retourne nb de lignes utilisées."""
        from reportlab.pdfbase.pdfmetrics import stringWidth
        font_name = pdf._fontname
        font_size = pdf._fontsize
        words = (text or '').split()
        lines, cur = [], []
        for w in words:
            test = ' '.join(cur + [w])
            if _mixed_width(test, font_name, font_size) <= max_width:
                cur.append(w)
            else:
                if cur:
                    lines.append(' '.join(cur))
                cur = [w]
        if cur:
            lines.append(' '.join(cur))
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            if lines:
                lines[-1] = lines[-1].rstrip('.,;: ') + chr(8230)
        y = y_top
        for line in lines:
            _draw_mixed(x, y, line, 'left')
            y -= line_height
        return len(lines)

    def _draw_image_box(item, x, y, w, h, *, caption=None, with_letter=None,
                        cap_lines=2, note_start=None):
        """Dessine une photo dans une boîte (cover ou contain).

        Retourne (placed_x, placed_y, placed_w, placed_h) ou None si raté.
        Si caption est fourni, réserve 6mm en bas pour la légende inline.
        """
        if not item.get('photo_path'):
            return None
        try:
            img = ImageReader(os.path.join(upload_dir, item['photo_path']))
            iw, ih = img.getSize()
        except Exception:
            return None
        cap_h = (3 * mm * cap_lines) if caption else 0
        avail_h = h - cap_h
        ratio = min(w / iw, avail_h / ih)
        dw, dh = iw * ratio, ih * ratio
        cx = x + (w - dw) / 2
        cy = y + cap_h + (avail_h - dh)
        try:
            pdf.drawImage(img, cx, cy, width=dw, height=dh, mask='auto')
        except Exception:
            return None
        # Lettre a/b/c en bas-gauche de l'image
        if with_letter and show_letters:
            pdf.setFont('Helvetica-Bold', 8)
            pdf.setFillColorRGB(*ACCENT_RGB)
            # petit cartouche crème pour lisibilité sur photo sombre
            pdf.setFillColorRGB(0.988, 0.988, 0.985, alpha=0.85)
            pdf.rect(cx + 2, cy + 2, 12, 12, fill=1, stroke=0)
            pdf.setFillColorRGB(*ACCENT_RGB)
            pdf.drawCentredString(cx + 8, cy + 5, with_letter)
        # Chantier C (audit livre §2.1) : APPELS DE NOTE — les epingles
        # photo_notes de l'app s'impriment en pastilles numerotees sur la
        # photo ; le texte attribue part en marge. Convention du metier :
        # jamais de bulle par-dessus l'image. x,y app = origine haut-gauche,
        # ReportLab = bas-gauche -> inversion de y.
        notes = item.get('photo_notes') or []
        if notes and note_start:
            r_pt = 4.6
            groupees = len(notes) > 4
            a_dessiner = notes[:1] if groupees else notes
            for k, note in enumerate(a_dessiner):
                nx = cx + float(note.get('x') or 0.5) * dw
                ny = cy + (1.0 - float(note.get('y') or 0.5)) * dh
                nx = min(max(nx, cx + 6), cx + dw - 6)
                ny = min(max(ny, cy + 6), cy + dh - 6)
                pdf.setFillColorRGB(0.988, 0.988, 0.985, alpha=0.88)
                pdf.circle(nx, ny, r_pt, fill=1, stroke=0)
                pdf.setStrokeColorRGB(*ACCENT_RGB)
                pdf.setLineWidth(0.6)
                pdf.circle(nx, ny, r_pt, fill=0, stroke=1)
                pdf.setFont('Helvetica-Bold', 6.2)
                pdf.setFillColorRGB(*ACCENT_RGB)
                lbl = (str(note_start) + '-' + str(note_start + len(notes) - 1)
                       if groupees else str(note_start + k))
                pdf.drawCentredString(nx, ny - 2.1, lbl)
        if caption:
            pdf.setFont('Times-Italic', 8.5)
            pdf.setFillColorRGB(*INK_SOFT_RGB)
            _wrap_text(caption, x + w / 2, y + cap_h / 2,
                       max_width=w, line_height=10, max_lines=cap_lines)
        return (cx, cy, dw, dh)

    def _draw_image_full_bleed(item, side):
        """Dessine une photo qui couvre toute la page, débord inclus (cover crop).

        Coupe pour remplir, pas de bandes blanches. Légende éventuelle
        en bandeau crème semi-transparent en bas.
        """
        if not item.get('photo_path'):
            return
        try:
            img = ImageReader(os.path.join(upload_dir, item['photo_path']))
            iw, ih = img.getSize()
        except Exception:
            return
        # Cover crop : on remplit page_w × page_h, on déborde le moins joli côté
        ratio = max(page_w / iw, page_h / ih)
        dw, dh = iw * ratio, ih * ratio
        cx = (page_w - dw) / 2
        cy = (page_h - dh) / 2
        try:
            pdf.drawImage(img, cx, cy, width=dw, height=dh, mask='auto')
        except Exception:
            return
        # Caption en bandeau bas — Chantier C : notes epinglees incluses
        cap = item.get('caption') or ''
        notes_fb = item.get('photo_notes') or []
        if notes_fb and not cap:
            n0 = notes_fb[0]
            aut = (n0.get('auteur') or '').strip()
            cap = (aut + ' - ' if aut else '') + chr(171) + ' ' + (n0.get('texte') or '') + ' ' + chr(187)
        if cap:
            tx, ty, tw, th = _trim_box()
            band_h = 14 * mm
            pdf.setFillColorRGB(*CREAM_RGB, alpha=0.92)
            pdf.rect(tx, ty, tw, band_h, fill=1, stroke=0)
            pdf.setFont('Times-Italic', 10)
            pdf.setFillColorRGB(*INK_RGB)
            inner_w = tw - (GUTTER_INNER_MM + GUTTER_OUTER_MM) * mm
            inner_x = tx + (GUTTER_INNER_MM if side == 'recto' else GUTTER_OUTER_MM) * mm
            _wrap_text_left(cap, inner_x, ty + band_h - 5 * mm,
                            max_width=inner_w, line_height=12, max_lines=2)

    def _draw_video_box(item, x, y, w, h, *, with_letter=None):
        """Vidéo : poster + bouton play + QR petit."""
        if not item.get('video_poster'):
            return None
        try:
            img = ImageReader(os.path.join(upload_dir, item['video_poster']))
            iw, ih = img.getSize()
        except Exception:
            return None
        # v5.6 : le QR prend la LARGEUR de la vignette, plus un timbre perdu
        # dessous. On resout le ratio pour que l'image et son QR carre de meme
        # largeur tiennent ensemble dans la case : ih*r + iw*r + legende <= h.
        cap_h = 6 * mm if item.get('caption') else 0
        gap = 2 * mm
        a_un_qr = bool(item.get('video_token') and qr_make and video_url_for)
        if a_un_qr:
            ratio = min(w / iw, (h - cap_h - gap) / (ih + iw))
        else:
            ratio = min(w / iw, (h - cap_h) / ih)
        dw, dh = iw * ratio, ih * ratio
        qr_size = dw if a_un_qr else 0
        cx = x + (w - dw) / 2
        cy = y + cap_h + (qr_size + gap if a_un_qr else 0)
        try:
            pdf.drawImage(img, cx, cy, width=dw, height=dh, mask='auto')
        except Exception:
            return None
        # Play overlay
        ccx, ccy = cx + dw / 2, cy + dh / 2
        r = min(dw, dh) * 0.07
        pdf.setFillColorRGB(0, 0, 0, alpha=0.5)
        pdf.circle(ccx, ccy, r, stroke=0, fill=1)
        pdf.setFillColorRGB(1, 1, 1)
        p = pdf.beginPath()
        p.moveTo(ccx - r * 0.4, ccy - r * 0.6)
        p.lineTo(ccx - r * 0.4, ccy + r * 0.6)
        p.lineTo(ccx + r * 0.6, ccy)
        p.close()
        pdf.drawPath(p, stroke=0, fill=1)
        # QR — meme largeur que la vignette, aligne dessous
        if a_un_qr:
            try:
                video_url = video_url_for(item['video_token'])
                qr_img = qr_make(video_url)
                qr_buf = io.BytesIO()
                qr_img.save(qr_buf, 'PNG')
                qr_buf.seek(0)
                qr_x = cx
                qr_y = y + cap_h
                pdf.drawImage(ImageReader(qr_buf), qr_x, qr_y,
                              width=qr_size, height=qr_size, mask='auto')
            except Exception:
                pass
        if with_letter and show_letters:
            pdf.setFillColorRGB(0.988, 0.988, 0.985, alpha=0.85)
            pdf.rect(cx + 2, cy + 2, 12, 12, fill=1, stroke=0)
            pdf.setFillColorRGB(*ACCENT_RGB)
            pdf.setFont('Helvetica-Bold', 8)
            pdf.drawCentredString(cx + 8, cy + 5, with_letter)
        if item.get('caption'):
            pdf.setFont('Times-Italic', 8.5)
            pdf.setFillColorRGB(*INK_SOFT_RGB)
            _wrap_text(item['caption'], x + w / 2, y + 2 * mm,
                       max_width=w, line_height=10, max_lines=2)
        return (cx, cy, dw, dh)

    def _draw_text_box(item, x, y, w, h):
        text = item.get('text_content') or ''
        if not text:
            return
        font_size = 11 if (w < 100 * mm) else 16
        pdf.setFont('Times-Italic', font_size)
        pdf.setFillColorRGB(*INK_RGB)
        _wrap_text(text, x + w / 2, y + h / 2,
                   max_width=w - 4 * mm, line_height=font_size * 1.3,
                   max_lines=12)

    def _draw_in_box(item, x, y, w, h, *, with_letter=None, note_start=None,
                     skip_caption=False):
        cap = None if skip_caption else item.get('caption')
        if item.get('video_path'):
            return _draw_video_box(item, x, y, w, h, with_letter=with_letter)
        if item.get('photo_path'):
            return _draw_image_box(item, x, y, w, h, caption=cap,
                                   with_letter=with_letter,
                                   note_start=note_start)
        if item.get('type') == 'text':
            _draw_text_box(item, x, y, w, h)
        return None

    def _item_ar(item):
        """Ratio largeur/hauteur d'une case : dimensions reelles de la photo,
        3:2 pour une video, 3:4 pour la case de marge ou un bloc texte."""
        if isinstance(item, dict):
            if item.get('_marge_case'):
                return 0.75
            w_, h_ = item.get('photo_width'), item.get('photo_height')
            if w_ and h_:
                return max(0.3, min(3.2, w_ / h_))
            if item.get('video_path'):
                return 1.5
        return 1.333

    def _justified_boxes(items, x, y, w, h, gap=3 * mm):
        """v5.17 — Calepinage aux RATIOS REELS (retour d'Arthur : les
        portraits sous/sur les paysages doivent occuper l'espace).

        Rangees a la Flickr : chaque rangee prend toute la largeur, sa
        hauteur = largeur / somme des ratios -> les portraits d'une meme
        rangee sont GRANDS, plus aucune bande blanche dans les cases.
        On enumere les partitions ordonnees (n<=7 -> 64 max) et on garde
        celle qui remplit le mieux la hauteur disponible.
        """
        n = len(items)
        if n == 0:
            return []
        ars = [_item_ar(it) for it in items]

        def rows_metrics(partition):
            rows = []
            k = 0
            for taille in partition:
                grp = ars[k:k + taille]
                k += taille
                rh = (w - gap * (taille - 1)) / sum(grp)
                rows.append((grp, rh))
            total = sum(rh for _, rh in rows) + gap * (len(rows) - 1)
            return rows, total

        # toutes les compositions ordonnees en rangees de 1 a 3
        def partitions(reste):
            if reste == 0:
                yield []
                return
            for taille in (1, 2, 3):
                if taille <= reste:
                    for suite in partitions(reste - taille):
                        yield [taille] + suite

        # v5.18 (audit DA §5) : le filtre de hauteur s'evalue APRES mise a
        # l'echelle, le score mesure l'AIRE couverte (pas la hauteur), le
        # secours est une grille equilibree — la « colonne de portraits »
        # (4 portraits de 34 mm empiles en A4 paysage) est morte.
        import math as _math
        best = None
        for part in partitions(n):
            rows, total = rows_metrics(part)
            if total <= 0:
                continue
            scale = min(1.0, h / total)
            # filtre 72 % sur la hauteur APRES echelle
            if len(rows) > 1 and any(rh * scale > h * 0.72 for _, rh in rows):
                continue
            # anti-colonne : hors bande etroite, pas plus de ceil(n/2) rangees
            if w / h >= 0.6 and len(rows) > -(-n // 2) and n > 1:
                continue
            # score = COUVERTURE EN AIRE (bw*bh = ar*(rh*s)^2)
            aire = sum((rh * scale) ** 2 * ar for grp, rh in rows for ar in grp)
            score = aire / (w * h)
            score -= 0.20 * max(0.0, 1.0 - scale)
            if n > 1 and min(rh * scale for _, rh in rows) < 30 * mm:
                score -= 0.15          # rangee timbre-poste, illisible imprimee
            if best is None or score > best[0]:
                best = (score, rows, total, scale)
        if best is None:
            # secours : grille equilibree (jamais la colonne)
            par_rangee = max(1, int(_math.ceil(_math.sqrt(n))))
            part = []
            reste = n
            while reste > 0:
                t = min(par_rangee, reste)
                part.append(t)
                reste -= t
            rows, total = rows_metrics(part)
            scale = min(1.0, h / total) if total > 0 else 1.0
            best = (0, rows, total, scale)
        _, rows, total, scale = best

        # hauteur reelle apres echelle ; le reliquat aere les inter-rangees
        # (plafonne) puis centre le bloc verticalement
        total_scaled = total * scale
        extra = max(0.0, h - total_scaled)
        n_inter = max(1, len(rows) - 1)
        gap_extra = min(extra / n_inter, 5 * mm) if len(rows) > 1 else 0
        reste_v = extra - gap_extra * (len(rows) - 1)
        boxes = []
        y_cur = y + h - reste_v / 2
        for grp, rh in rows:
            rh_s = rh * scale
            row_w = sum(a * rh_s for a in grp) + gap * (len(grp) - 1)
            x_cur = x + (w - row_w) / 2
            y_cur -= rh_s
            for a in grp:
                bw = a * rh_s
                boxes.append((x_cur, y_cur, bw, rh_s))
                x_cur += bw + gap
            y_cur -= (gap + gap_extra)
        return boxes

    def _grid_layout(n, x, y, w, h, gap=3 * mm):
        """Grille adaptative pour 1..6 cases.
        v5.6 : 5 et 6 sont apparus quand la note en marge est devenue une case
        de la grille comme une photo (avant, elle mangeait une bande sur toute
        la hauteur). Sans eux, la 5e case n'existait pas et son contenu
        disparaissait en silence."""
        boxes = []
        if n <= 0:
            return boxes
        if n == 1:
            boxes.append((x, y, w, h))
        elif n >= 5:
            # 5 : deux en haut, trois en bas — 6 : deux rangees de trois
            haut = 2 if n == 5 else 3
            bas = n - haut
            top_h = (h - gap) * (0.52 if n == 5 else 0.5)
            bot_h = h - top_h - gap
            cw_top = (w - gap * (haut - 1)) / haut
            cw_bot = (w - gap * (bas - 1)) / bas
            for i in range(haut):
                boxes.append((x + i * (cw_top + gap), y + bot_h + gap, cw_top, top_h))
            for i in range(bas):
                boxes.append((x + i * (cw_bot + gap), y, cw_bot, bot_h))
        elif n == 2:
            if h > w:
                cell_h = (h - gap) / 2
                boxes.append((x, y + cell_h + gap, w, cell_h))
                boxes.append((x, y, w, cell_h))
            else:
                cell_w = (w - gap) / 2
                boxes.append((x, y, cell_w, h))
                boxes.append((x + cell_w + gap, y, cell_w, h))
        elif n == 3:
            top_h = h * 0.55
            bot_h = h - top_h - gap
            half_w = (w - gap) / 2
            boxes.append((x, y + bot_h + gap, w, top_h))
            boxes.append((x, y, half_w, bot_h))
            boxes.append((x + half_w + gap, y, half_w, bot_h))
        else:  # 4
            half_w = (w - gap) / 2
            half_h = (h - gap) / 2
            boxes.append((x, y + half_h + gap, half_w, half_h))
            boxes.append((x + half_w + gap, y + half_h + gap, half_w, half_h))
            boxes.append((x, y, half_w, half_h))
            boxes.append((x + half_w + gap, y, half_w, half_h))
        return boxes

    def _draw_page_number(num, side):
        """Numéro de page en bas, du côté extérieur."""
        if num <= 0:
            return
        tx, ty, tw, th = _trim_box()
        pdf.setFont('Helvetica', 7)
        pdf.setFillColorRGB(*INK_GHOST_RGB)
        if side == 'recto':
            pdf.drawRightString(tx + tw - GUTTER_OUTER_MM * mm,
                                ty + 6 * mm, str(num))
        else:
            pdf.drawString(tx + GUTTER_OUTER_MM * mm,
                           ty + 6 * mm, str(num))

    def _draw_margin_zone(items, area_x, area_y, area_w, area_h, side,
                          letters=None, mini_map_png=None,
                          mini_map_label=None):
        """Zone marge : mini-carte optionnelle + items (notes, légendes inline).

        items : liste de dicts avec keys : 'kind' ('photo'|'text'|'caption'),
                'letter' (str ou None), 'text' (str), 'thumb_path' (str ou None).
        """
        x = area_x
        y_top = area_y + area_h
        gap = 4 * mm

        # 1) Mini-carte en haut
        if mini_map_png:
            mm_h = min(area_w * 0.85, 35 * mm)
            mm_w = min(area_w - 2 * mm, mm_h * 1.3)
            mm_x = area_x + (area_w - mm_w) / 2
            mm_y = y_top - mm_h - 2 * mm
            try:
                pdf.drawImage(ImageReader(io.BytesIO(mini_map_png)),
                              mm_x, mm_y, width=mm_w, height=mm_h, mask='auto')
                pdf.setStrokeColorRGB(*LINE_RGB)
                pdf.setLineWidth(0.3)
                pdf.rect(mm_x, mm_y, mm_w, mm_h, fill=0, stroke=1)
                if mini_map_label:
                    pdf.setFont('Helvetica', 6)
                    pdf.setFillColorRGB(*INK_FAINT_RGB)
                    pdf.drawCentredString(mm_x + mm_w / 2, mm_y - 3 * mm,
                                          mini_map_label[:30].upper())
                y_top = mm_y - 7 * mm
            except Exception:
                pass

        # 2) Étiquette (LÉGENDES si captions, NOTES sinon)
        has_caption_items = any(it.get('kind') == 'caption' for it in items)
        if items:
            label = "LÉGENDES" if has_caption_items else "NOTES"
            pdf.setFont('Helvetica-Bold', 6.5)
            pdf.setFillColorRGB(*INK_GHOST_RGB)
            pdf.drawString(x, y_top, label)
            y_top -= 6
            # filet terracotta court
            pdf.setStrokeColorRGB(*ACCENT_RGB)
            pdf.setLineWidth(0.6)
            pdf.line(x, y_top, x + 12 * mm, y_top)
            y_top -= 3 * mm

        # 3) Items — v5.6 : la marge est une CASE, pas une colonne infinie.
        # Rien ne doit deborder hors de sa case : chaque bloc verifie la place
        # qui reste AVANT de se dessiner, et on s'arrete des qu'il n'y en a plus.
        bas = area_y + 3 * mm
        non_dessines = []
        for pos_item, item in enumerate(items):
            if y_top - bas < 12:               # meme pas une ligne de texte
                non_dessines = list(items[pos_item:])
                break
            letter = item.get('letter')
            text = item.get('text') or ''
            thumb = item.get('thumb_path')
            kind = item.get('kind')
            # Thumb si photo en marge
            if thumb:
                try:
                    img = ImageReader(os.path.join(upload_dir, thumb))
                    iw, ih = img.getSize()
                    th_w = min(area_w - 2 * mm, 28 * mm)
                    th_h = th_w * ih / iw
                    if th_h > 22 * mm:
                        th_h = 22 * mm
                        th_w = th_h * iw / ih
                    place = y_top - bas - (14 if (text or letter) else 0)
                    if th_h > place:            # on retrecit plutot que deborder
                        th_h = place
                        th_w = th_h * iw / ih
                    if th_h >= 8 * mm:
                        pdf.drawImage(img, x, y_top - th_h,
                                      width=th_w, height=th_h, mask='auto')
                        y_top -= th_h + 2 * mm
                except Exception:
                    pass
            # Lettre + texte
            if text or letter:
                if y_top - bas < 12:
                    non_dessines = list(items[pos_item:])
                    break
                if letter and show_letters:
                    pdf.setFont('Helvetica-Bold', 8)
                    pdf.setFillColorRGB(*ACCENT_RGB)
                    pdf.drawString(x, y_top - 8, letter)
                    text_x = x + 9
                else:
                    text_x = x
                pdf.setFont('Times-Italic', 8.5)
                pdf.setFillColorRGB(*INK_SOFT_RGB)
                lignes_possibles = max(1, int((y_top - bas - 4) // 10))
                # v5.6 : la case peut desormais faire toute la largeur de la
                # page. On borne quand meme la mesure : au-dela, l'oeil perd
                # la ligne suivante. C'est la regle typographique, pas la place
                # disponible, qui decide.
                mesure = min(area_w - (text_x - x) - 2, 95 * mm)
                used = _wrap_text_left(text, text_x, y_top - 8,
                                       max_width=mesure,
                                       line_height=10,
                                       max_lines=min(6, lignes_possibles))
                y_top -= used * 10 + 4 * mm
            else:
                y_top -= 2 * mm
            # Petit séparateur ligne fine
            if y_top - bas > 4:
                pdf.setStrokeColorRGB(*LINE_RGB)
                pdf.setLineWidth(0.2)
                pdf.line(x, y_top + 2, x + area_w * 0.4, y_top + 2)
        return non_dessines

    # Page program (recto/verso) ────────────────────────────────────────────
    program = []  # list of dicts {kind, ...}

    # Couverture
    cover_item = None
    if cover_photo_id:
        for p in pages_main:
            if p.get('photo_id') == cover_photo_id:
                cover_item = p
                break
    if cover_item is None:
        cover_item = next((p for p in pages_main if p.get('photo_path')), None)
    program.append({'kind': 'cover', 'item': cover_item})
    # v5.21 (chantier D) : fini les deux pages blanches d'entree — page de
    # garde teintee puis page de titre, comme un livre qui se respecte.
    program.append({'kind': 'garde'})
    program.append({'kind': 'title'})

    # Carte d'ensemble (page recto)
    if show_overview_map and (book_map is not None or
                              (geo_summary and fetch_static_map and compute_zoom)):
        program.append({'kind': 'overview_map'})
        program.append({'kind': 'blank'})

    # Pages principales : structurer par chunks selon mode plein-page
    margin_items_pool = list(pages_data.get('margin') or []) if margin_pos != 'end' else []

    # v5.17 : segmentation partagee (_program_chunks) + bandeau de jour sur
    # la premiere page de chaque journee — le livre marque les transitions
    # de date/lieu comme l'album.
    # v5.21 (chantier D) : ouverture de chapitre par jour — une pleine page
    # JOUR n / date en toutes lettres / lieu dominant / meilleure photo du
    # jour, inseree avant le premier chunk de chaque journee. Elle remplace
    # le bandeau v5.17 (qui reste le secours des pages non datees).
    jours_sorted = sorted({_page_day(p) for p in pages_main if _page_day(p)})
    num_of_day = {d: k + 1 for k, d in enumerate(jours_sorted)}
    best_by_day = {}
    villes_by_day = {}
    for p in pages_main:
        d = _page_day(p)
        if not d:
            continue
        if p.get('photo_city'):
            villes_by_day.setdefault(d, []).append(p['photo_city'])
        if p.get('photo_path'):
            w0 = p.get('photo_width') or 0
            h0 = p.get('photo_height') or 0
            score = (1 if w0 >= h0 else 0, w0 * h0)
            if d not in best_by_day or score > best_by_day[d][0]:
                best_by_day[d] = (score, p)

    def _chapter_entry(d):
        vs = villes_by_day.get(d) or []
        lieu = max(set(vs), key=vs.count) if vs else ''
        bb = best_by_day.get(d)
        return {'kind': 'chapter_open', 'num': num_of_day[d], 'day': d,
                'lieu': lieu, 'item': bb[1] if bb else None}

    prev_day = None
    for seg_kind, seg in _program_chunks(pages_main, n_per_page):
        if seg_kind == 'spread':
            d = _page_day(seg)
            if d and d != prev_day:
                program.append(_chapter_entry(d))
                prev_day = d
            program.append({'kind': 'spread', 'item': seg})
        elif seg_kind == 'full':
            d = _page_day(seg)
            if d and d != prev_day:
                program.append(_chapter_entry(d))
                prev_day = d
            program.append({'kind': 'full', 'item': seg})
        else:
            entry = {'kind': 'composite', 'chunk': seg}
            jours = [d for d in (_page_day(p) for p in seg) if d]
            d = jours[0] if jours else ''
            if d and d != prev_day:
                program.append(_chapter_entry(d))
                prev_day = d
            program.append(entry)

    # Notes en marge restantes en fin de livre
    end_margin_items = []
    if margin_pos == 'end':
        end_margin_items = list(pages_data.get('margin') or [])
    if end_margin_items:
        program.append({'kind': 'margin_intro'})
        # Chunks de 4
        for s in range(0, len(end_margin_items), 4):
            program.append({'kind': 'margin_grid',
                            'chunk': end_margin_items[s:s + 4]})

    # v5.21 (chantier D) : la page de statistiques — LA page que les couples
    # montrent — puis le colophon, puis la 4e de couverture.
    book_stats = _compute_book_stats(pages_main)
    if book_stats['photos'] or book_stats['videos']:
        program.append({'kind': 'stats'})

    # Page de fin
    program.append({'kind': 'colophon'})
    program.append({'kind': 'back_cover'})

    # Distribution des margin items au fil des pages composites ─────────────
    # v3.4.1 : alignement par date (build_margin_plan) — une note tombe sur
    # la page dont les photos couvrent sa date, plus de repartition aveugle.
    if margin_pos != 'end' and margin_items_pool:
        plan = build_margin_plan(pages_main, margin_items_pool, n_per_page)
        ci = 0
        for e in program:
            if e['kind'] == 'composite':
                if ci < len(plan) and plan[ci]:
                    e['margin_items'] = plan[ci]
                ci += 1

    # Drawers ───────────────────────────────────────────────────────────────
    def _draw_cover():
        _fill_page_cream()
        item = cover_item
        if item:
            try:
                ph = os.path.join(upload_dir, item['photo_path'])
                img = ImageReader(ph)
                iw, ih = img.getSize()
                tx, ty, tw, th = _trim_box()
                avail_w = tw - 30 * mm
                avail_h = th * 0.55
                ratio = min(avail_w / iw, avail_h / ih)
                dw, dh = iw * ratio, ih * ratio
                pdf.drawImage(img, (page_w - dw) / 2,
                              ty + th * 0.40,
                              width=dw, height=dh, mask='auto')
            except Exception:
                pass
        # Titre auto-fit + wrap 2 lignes max
        from reportlab.pdfbase.pdfmetrics import stringWidth
        title = carnet['title'] or ''
        title_max_w = (w_mm * mm) - 30 * mm
        title_size = 36
        title_lines = [title]
        while title_size > 22 and stringWidth(title, 'Times-Italic', title_size) > title_max_w:
            title_size -= 1
        if stringWidth(title, 'Times-Italic', title_size) > title_max_w and ' ' in title:
            words = title.split()
            best_split = len(words) // 2
            best_diff = float('inf')
            for k in range(1, len(words)):
                w1 = stringWidth(' '.join(words[:k]), 'Times-Italic', title_size)
                w2 = stringWidth(' '.join(words[k:]), 'Times-Italic', title_size)
                if max(w1, w2) <= title_max_w and abs(w1 - w2) < best_diff:
                    best_diff = abs(w1 - w2)
                    best_split = k
            title_lines = [' '.join(words[:best_split]), ' '.join(words[best_split:])]
        pdf.setFont('Times-Italic', title_size)
        pdf.setFillColorRGB(*INK_RGB)
        line_h = title_size * 1.15
        y_title = bleed + (h_mm * mm) * 0.30 + (len(title_lines) - 1) * line_h / 2
        for line in title_lines:
            _draw_mixed(page_w / 2, y_title, line, 'center')
            y_title -= line_h
        # v5.18 (audit DA §1) : dates en toutes lettres sur la couverture —
        # « 11 – 13 juillet 2026 », jamais d'ISO brut sur un objet offert.
        def _date_fr(iso):
            from datetime import datetime as _dt
            try:
                d = _dt.strptime(str(iso)[:10], '%Y-%m-%d')
            except (ValueError, TypeError):
                return str(iso)
            return f"{d.day} {_MOIS_FR[d.month - 1].lower()} {d.year}"

        def _periode_fr(d1, d2):
            from datetime import datetime as _dt
            try:
                a = _dt.strptime(str(d1)[:10], '%Y-%m-%d')
                b = _dt.strptime(str(d2)[:10], '%Y-%m-%d')
            except (ValueError, TypeError):
                return f"{d1} – {d2}"
            if (a.year, a.month) == (b.year, b.month):
                return f"{a.day} – {b.day} {_MOIS_FR[a.month - 1].lower()} {a.year}"
            if a.year == b.year:
                return (f"{a.day} {_MOIS_FR[a.month - 1].lower()} – "
                        f"{b.day} {_MOIS_FR[b.month - 1].lower()} {a.year}")
            return f"{_date_fr(d1)} – {_date_fr(d2)}"

        sub = []
        if carnet.get('location'):
            sub.append(carnet['location'])
        if carnet.get('date_start') and carnet.get('date_end') and carnet['date_start'] != carnet['date_end']:
            sub.append(_periode_fr(carnet['date_start'], carnet['date_end']))
        elif carnet.get('date_start'):
            sub.append(_date_fr(carnet['date_start']))
        if sub:
            pdf.setFont('Helvetica', 11)
            pdf.setFillColorRGB(*INK_FAINT_RGB)
            pdf.drawCentredString(page_w / 2, bleed + (h_mm * mm) * 0.24,
                                  ' · '.join(sub))
        # Filet terracotta sous le titre
        pdf.setStrokeColorRGB(*ACCENT_RGB)
        pdf.setLineWidth(0.8)
        cy = bleed + (h_mm * mm) * 0.21
        pdf.line(page_w / 2 - 20 * mm, cy, page_w / 2 + 20 * mm, cy)
        pdf.setFont('Helvetica', 8)
        pdf.setFillColorRGB(*INK_GHOST_RGB)
        pdf.drawCentredString(page_w / 2, bleed + 8 * mm, "NOTRE HISTOIRE")

    def _draw_blank(side, page_num):
        _fill_page_cream()
        _draw_page_number(page_num, side)

    # ── v5.21 (chantier D) : l'architecture du livre ──────────────────────
    GARDE_RGB = (0.962, 0.946, 0.918)   # creme profond de la page de garde

    def _draw_coeur(cx, cy, s, rgb):
        """Petit coeur terracotta (le motif du logo) en courbes de Bezier."""
        pdf.setFillColorRGB(*rgb)
        p = pdf.beginPath()
        p.moveTo(cx, cy - s * 0.62)
        p.curveTo(cx - s * 1.15, cy + s * 0.30, cx - s * 0.52, cy + s * 0.92,
                  cx, cy + s * 0.30)
        p.curveTo(cx + s * 0.52, cy + s * 0.92, cx + s * 1.15, cy + s * 0.30,
                  cx, cy - s * 0.62)
        pdf.drawPath(p, fill=1, stroke=0)

    def _draw_garde(side):
        """Page de garde : un aplat creme profond, un coeur discret."""
        pdf.setFillColorRGB(*GARDE_RGB)
        pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        _draw_coeur(page_w / 2, page_h / 2, 5 * mm, ACCENT_RGB)

    def _dates_fr_carnet():
        """Periode du carnet en toutes lettres (ou '')."""
        from datetime import datetime as _dt
        d1, d2 = carnet.get('date_start'), carnet.get('date_end')

        def _f(iso):
            try:
                d = _dt.strptime(str(iso)[:10], '%Y-%m-%d')
            except (ValueError, TypeError):
                return str(iso or '')
            return f"{d.day} {_MOIS_FR[d.month - 1].lower()} {d.year}"
        if d1 and d2 and d1 != d2:
            try:
                a = _dt.strptime(str(d1)[:10], '%Y-%m-%d')
                b = _dt.strptime(str(d2)[:10], '%Y-%m-%d')
                if (a.year, a.month) == (b.year, b.month):
                    return (f"{a.day} – {b.day} "
                            f"{_MOIS_FR[a.month - 1].lower()} {a.year}")
                if a.year == b.year:
                    return (f"{a.day} {_MOIS_FR[a.month - 1].lower()} – "
                            f"{b.day} {_MOIS_FR[b.month - 1].lower()} {a.year}")
            except (ValueError, TypeError):
                pass
            return f"{_f(d1)} – {_f(d2)}"
        if d1:
            return _f(d1)
        return ''

    def _draw_title_page(side):
        """Page de titre : titre, prenoms, dates en toutes lettres."""
        from reportlab.pdfbase.pdfmetrics import stringWidth
        _fill_page_cream()
        title = carnet['title'] or 'Notre voyage'
        size = 30
        max_w = (w_mm * mm) - 44 * mm
        while size > 18 and stringWidth(title, 'Times-Italic', size) > max_w:
            size -= 1
        pdf.setFont('Times-Italic', size)
        pdf.setFillColorRGB(*INK_RGB)
        _draw_mixed(page_w / 2, bleed + (h_mm * mm) * 0.60, title, 'center')
        pdf.setStrokeColorRGB(*ACCENT_RGB)
        pdf.setLineWidth(0.8)
        cy = bleed + (h_mm * mm) * 0.55
        pdf.line(page_w / 2 - 16 * mm, cy, page_w / 2 + 16 * mm, cy)
        membres = [str(m) for m in ((book_meta or {}).get('members') or []) if m]
        if membres:
            pdf.setFont('Helvetica', 12)
            pdf.setFillColorRGB(*INK_SOFT_RGB)
            pdf.drawCentredString(page_w / 2, bleed + (h_mm * mm) * 0.49,
                                  ' & '.join(membres))
        dates = _dates_fr_carnet()
        if dates:
            pdf.setFont('Helvetica', 10)
            pdf.setFillColorRGB(*INK_FAINT_RGB)
            pdf.drawCentredString(page_w / 2, bleed + (h_mm * mm) * 0.44, dates)
        pdf.setFont('Helvetica', 8)
        pdf.setFillColorRGB(*INK_GHOST_RGB)
        pdf.drawCentredString(page_w / 2, bleed + 8 * mm, "NOTRE HISTOIRE")

    def _draw_chapter_open(entry, side):
        """Ouverture de chapitre : JOUR n, date en toutes lettres, lieu
        dominant, meilleure photo de la journee. Pas de folio ici."""
        _fill_page_cream()
        cx, cy, cw, ch = _content_box(side)
        y = cy + ch * 0.88
        pdf.setFont('Helvetica-Bold', 11)
        pdf.setFillColorRGB(*ACCENT_RGB)
        lbl = f"J O U R   {entry['num']}"
        pdf.drawCentredString(cx + cw / 2, y, lbl)
        pdf.setFont('Times-Italic', 22)
        pdf.setFillColorRGB(*INK_RGB)
        pdf.drawCentredString(cx + cw / 2, y - 11 * mm,
                              _date_longue_fr(entry['day']))
        yy = y - 17 * mm
        if entry.get('lieu'):
            pdf.setFont('Helvetica', 10)
            pdf.setFillColorRGB(*INK_FAINT_RGB)
            pdf.drawCentredString(cx + cw / 2, yy, str(entry['lieu']))
            yy -= 5 * mm
        pdf.setStrokeColorRGB(*ACCENT_RGB)
        pdf.setLineWidth(0.7)
        pdf.line(cx + cw / 2 - 14 * mm, yy - 2 * mm,
                 cx + cw / 2 + 14 * mm, yy - 2 * mm)
        item = entry.get('item')
        if item:
            try:
                ph = os.path.join(upload_dir, item['photo_path'])
                img = ImageReader(ph)
                iw, ih = img.getSize()
                avail_w = cw * 0.80
                avail_h = (yy - 10 * mm) - (cy + ch * 0.08)
                ratio = min(avail_w / iw, avail_h / ih)
                dw, dh = iw * ratio, ih * ratio
                ix = cx + (cw - dw) / 2
                iy = cy + ch * 0.08 + (avail_h - dh) / 2
                pdf.drawImage(img, ix, iy, width=dw, height=dh, mask='auto')
                pdf.setStrokeColorRGB(*LINE_RGB)
                pdf.setLineWidth(0.5)
                pdf.rect(ix, iy, dw, dh, fill=0, stroke=1)
            except Exception:
                pass

    def _draw_stats(side, page_num):
        """Le voyage en chiffres : jours, photos, videos, lieux, km."""
        _fill_page_cream()
        cx, cy, cw, ch = _content_box(side)
        pdf.setFont('Times-Italic', 20)
        pdf.setFillColorRGB(*INK_RGB)
        pdf.drawCentredString(cx + cw / 2, cy + ch * 0.82,
                              "Le voyage en chiffres")
        pdf.setStrokeColorRGB(*ACCENT_RGB)
        pdf.setLineWidth(0.7)
        pdf.line(cx + cw / 2 - 16 * mm, cy + ch * 0.79,
                 cx + cw / 2 + 16 * mm, cy + ch * 0.79)
        entries = []
        if book_stats['jours']:
            entries.append((str(book_stats['jours']),
                            'JOUR' + ('S' if book_stats['jours'] > 1 else '')))
        if book_stats['photos']:
            entries.append((str(book_stats['photos']), 'PHOTOS'))
        if book_stats['videos']:
            entries.append((str(book_stats['videos']),
                            'VIDEO' + ('S' if book_stats['videos'] > 1 else '')))
        if book_stats['lieux']:
            entries.append((str(book_stats['lieux']),
                            'LIEU' + ('X' if book_stats['lieux'] > 1 else '')))
        if book_stats['km'] >= 1:
            entries.append((f"~{book_stats['km']:.0f}", 'KM PARCOURUS'))
        n_col = min(3, max(1, len(entries)))
        col_w = cw / n_col
        row_h = 30 * mm
        top = cy + ch * 0.66
        for k, (val, lab) in enumerate(entries):
            col, row = k % n_col, k // n_col
            ex = cx + col * col_w + col_w / 2
            ey = top - row * row_h
            pdf.setFont('Helvetica-Bold', 27)
            pdf.setFillColorRGB(*ACCENT_RGB)
            pdf.drawCentredString(ex, ey, val)
            pdf.setFont('Helvetica', 7.5)
            pdf.setFillColorRGB(*INK_FAINT_RGB)
            pdf.drawCentredString(ex, ey - 6.5 * mm, lab)
        if book_stats['villes']:
            from reportlab.pdfbase.pdfmetrics import stringWidth
            pdf.setFont('Helvetica', 7.5)
            pdf.setFillColorRGB(*INK_GHOST_RGB)
            pdf.drawCentredString(cx + cw / 2, cy + ch * 0.30,
                                  "VILLES TRAVERSEES")
            texte = '  ·  '.join(book_stats['villes'])
            pdf.setFont('Helvetica', 9)
            pdf.setFillColorRGB(*INK_SOFT_RGB)
            max_w = cw * 0.86
            lignes, cur = [], ''
            for part in book_stats['villes']:
                cand = (cur + '  ·  ' + part) if cur else part
                if stringWidth(cand, 'Helvetica', 9) <= max_w or not cur:
                    cur = cand
                else:
                    lignes.append(cur)
                    cur = part
            if cur:
                lignes.append(cur)
            yv = cy + ch * 0.30 - 6 * mm
            for lg in lignes[:4]:
                pdf.drawCentredString(cx + cw / 2, yv, lg)
                yv -= 5 * mm
        _draw_page_number(page_num, side)

    def _draw_back_cover():
        """4e de couverture : aplat de garde, photo secondaire, baseline."""
        pdf.setFillColorRGB(*GARDE_RGB)
        pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        back_item = None
        cov_pid = cover_item.get('photo_id') if cover_item else None
        for p in reversed(pages_main):
            if p.get('photo_path') and p.get('photo_id') != cov_pid:
                back_item = p
                break
        if back_item:
            try:
                ph = os.path.join(upload_dir, back_item['photo_path'])
                img = ImageReader(ph)
                iw, ih = img.getSize()
                avail = (w_mm * mm) * 0.34
                ratio = min(avail / iw, avail / ih)
                dw, dh = iw * ratio, ih * ratio
                ix, iy = (page_w - dw) / 2, bleed + (h_mm * mm) * 0.52
                pdf.drawImage(img, ix, iy, width=dw, height=dh, mask='auto')
                pdf.setStrokeColorRGB(*INK_GHOST_RGB)
                pdf.setLineWidth(0.5)
                pdf.rect(ix, iy, dw, dh, fill=0, stroke=1)
            except Exception:
                back_item = None
        if not back_item:
            _draw_coeur(page_w / 2, bleed + (h_mm * mm) * 0.58,
                        6 * mm, ACCENT_RGB)
        pdf.setFont('Times-Italic', 13)
        pdf.setFillColorRGB(*INK_SOFT_RGB)
        _draw_mixed(page_w / 2, bleed + (h_mm * mm) * 0.40,
                    carnet['title'] or '', 'center')
        pdf.setStrokeColorRGB(*ACCENT_RGB)
        pdf.setLineWidth(0.6)
        pdf.line(page_w / 2 - 10 * mm, bleed + (h_mm * mm) * 0.365,
                 page_w / 2 + 10 * mm, bleed + (h_mm * mm) * 0.365)
        pdf.setFont('Helvetica', 8)
        pdf.setFillColorRGB(*INK_GHOST_RGB)
        pdf.drawCentredString(page_w / 2, bleed + 10 * mm,
                              "NOTRE HISTOIRE · histoire.aqgk.fr")

    def _draw_overview_map(side, page_num):
        """Chantier A+B (audit DA §4) : LA page carte du livre — le fond de
        l'app (voyager), les epingles-vignettes photo, les etapes numerotees,
        le trace jour par jour, et la colonne TIMELINE du planning a gauche
        ou a droite selon le reglage. Texte de la timeline en ReportLab :
        net et selectionnable. Si les tuiles manquent, la page le DIT."""
        _fill_page_cream()
        cx, cy, cw, ch = _content_box(side)
        pdf.setFont('Times-Italic', 24)
        pdf.setFillColorRGB(*INK_RGB)
        pdf.drawString(cx, cy + ch - 12 * mm, "Notre voyage")
        pdf.setStrokeColorRGB(*ACCENT_RGB)
        pdf.setLineWidth(0.8)
        pdf.line(cx, cy + ch - 15 * mm, cx + 20 * mm, cy + ch - 15 * mm)
        zone_y = cy + 2 * mm
        zone_h = ch - 22 * mm

        png = None
        timeline = []
        if book_map is not None:
            # cote timeline : jamais cote reliure (comme outer/inner)
            tl_side = map_timeline_side if map_timeline_side in ('left', 'right', 'none') else 'right'
            tl_w = 48 * mm if tl_side != 'none' else 0
            map_w = cw - (tl_w + 4 * mm if tl_w else 0)
            map_x = cx + (tl_w + 4 * mm if (tl_w and tl_side == 'left') else 0)
            tl_x = cx if tl_side == 'left' else cx + map_w + 4 * mm
            # 300 dpi : 11.81 px/mm, plafonne pour la memoire
            w_px = min(int((map_w / mm) * 11.81), 2400)
            h_px = min(int((zone_h / mm) * 11.81), 2400)
            try:
                png, timeline = book_map(w_px, h_px)
            except Exception as e:
                png, timeline = None, []
            if tl_w and not timeline:
                # pas de timeline a montrer : la carte prend tout
                tl_w = 0
                map_w = cw
                map_x = cx
                w_px = min(int((map_w / mm) * 11.81), 2400)
                try:
                    png2, _ = book_map(w_px, h_px)
                    if png2:
                        png = png2
                except Exception:
                    pass
            if png:
                try:
                    pdf.drawImage(ImageReader(io.BytesIO(png)), map_x, zone_y,
                                  width=map_w, height=zone_h, mask='auto')
                    pdf.setStrokeColorRGB(*LINE_RGB)
                    pdf.setLineWidth(0.5)
                    pdf.rect(map_x, zone_y, map_w, zone_h, fill=0, stroke=1)
                except Exception:
                    png = None
            if not png:
                # regle R4 : un echec se VOIT — cartouche explicite
                pdf.setStrokeColorRGB(*LINE_RGB)
                pdf.setLineWidth(0.5)
                pdf.rect(map_x, zone_y, map_w, zone_h, fill=0, stroke=1)
                pdf.setFont('Helvetica', 10)
                pdf.setFillColorRGB(*INK_GHOST_RGB)
                pdf.drawCentredString(map_x + map_w / 2, zone_y + zone_h / 2 + 6,
                                      "Carte indisponible")
                pdf.setFont('Helvetica', 8)
                pdf.drawCentredString(map_x + map_w / 2, zone_y + zone_h / 2 - 6,
                                      "Regenerez le livre avec une connexion reseau")
            # ── colonne timeline (planning du voyage) ──
            if tl_w and timeline:
                y_t = zone_y + zone_h - 4
                PIN_HEX = {'dormir': (0.541, 0.478, 0.710), 'manger': (0.851, 0.557, 0.290),
                           'rando': (0.431, 0.620, 0.459), 'plage': (0.373, 0.659, 0.769),
                           'visite': (0.769, 0.396, 0.290), 'autre': (0.545, 0.514, 0.471),
                           '': (0.545, 0.514, 0.471)}
                for jour in timeline:
                    if y_t < zone_y + 14:
                        pdf.setFont('Helvetica', 6.5)
                        pdf.setFillColorRGB(*INK_GHOST_RGB)
                        pdf.drawString(tl_x, zone_y + 4, "(suite du planning dans l'album)")
                        break
                    pdf.setFont('Helvetica-Bold', 8.5)
                    pdf.setFillColorRGB(*ACCENT_RGB)
                    pdf.drawString(tl_x, y_t - 8, f"JOUR {jour['num']}")
                    pdf.setFont('Helvetica', 7)
                    pdf.setFillColorRGB(*INK_FAINT_RGB)
                    entete = jour['date_label'] + ((' - ' + jour['ville']) if jour['ville'] else '')
                    pdf.drawString(tl_x + 16 * mm, y_t - 8, entete[:34])
                    y_t -= 13
                    for et in jour['etapes']:
                        if y_t < zone_y + 12:
                            break
                        col = PIN_HEX.get(et.get('pin_kind') or '', PIN_HEX[''])
                        pdf.setFillColorRGB(*col)
                        pdf.circle(tl_x + 4, y_t - 4, 4.2, fill=1, stroke=0)
                        pdf.setFillColorRGB(1, 1, 1)
                        pdf.setFont('Helvetica-Bold', 5.5)
                        pdf.drawCentredString(tl_x + 4, y_t - 6, str(et['num']))
                        pdf.setFont('Helvetica', 8)
                        pdf.setFillColorRGB(*INK_SOFT_RGB)
                        pdf.drawString(tl_x + 11, y_t - 7, (et['titre'] or '')[:30])
                        y_t -= 11
                    if jour['n_photos']:
                        pdf.setFont('Helvetica', 6.5)
                        pdf.setFillColorRGB(*INK_GHOST_RGB)
                        pdf.drawString(tl_x + 11, y_t - 6,
                                       f"{jour['n_photos']} photo" + ('s' if jour['n_photos'] > 1 else ''))
                        y_t -= 10
                    y_t -= 6
                # filet entre timeline et carte
                pdf.setStrokeColorRGB(*LINE_RGB)
                pdf.setLineWidth(0.4)
                fx = (tl_x + tl_w + 2 * mm - 1) if tl_side == 'left' else (tl_x - 2 * mm)
                pdf.line(fx, zone_y, fx, zone_y + zone_h)
            _draw_page_number(page_num, side)
            return

        # ── repli historique (staticmap) si book_map absent ──
        if not (geo_summary and fetch_static_map and compute_zoom):
            return
        map_w_mm = (cw / mm) - 4
        map_h_mm = (zone_h / mm)
        map_w_px = min(int(map_w_mm * 4), 1024)
        map_h_px = min(int(map_h_mm * 4), 1024)
        zoom = compute_zoom(
            geo_summary['min_lat'], geo_summary['max_lat'],
            geo_summary['min_lng'], geo_summary['max_lng'],
            map_w_px, map_h_px,
        )
        png = fetch_static_map(geo_summary['center_lat'], geo_summary['center_lng'],
                               zoom, map_w_px, map_h_px,
                               markers=geo_summary['markers'])
        if png:
            try:
                pdf.drawImage(ImageReader(io.BytesIO(png)), cx + 2 * mm, zone_y,
                              width=map_w_mm * mm, height=map_h_mm * mm, mask='auto')
            except Exception:
                pass
        _draw_page_number(page_num, side)

    def _draw_full(item, side, page_num):
        """Pleine page avec fond perdu, débord 3mm."""
        _fill_page_cream()
        _draw_image_full_bleed(item, side)
        _draw_page_number(page_num, side)

    def _draw_spread_half(item, side, page_num, half):
        """Une moitié d'un spread (verso ou recto). half ∈ {'left','right'}."""
        _fill_page_cream()
        if not item.get('photo_path'):
            return
        try:
            img = ImageReader(os.path.join(upload_dir, item['photo_path']))
            iw, ih = img.getSize()
        except Exception:
            return
        # On fait comme si la photo couvrait 2 page_w × page_h
        # L'image cover-fill dans 2*page_w × page_h, puis on prend la moitié
        total_w = 2 * page_w
        total_h = page_h
        ratio = max(total_w / iw, total_h / ih)
        dw, dh = iw * ratio, ih * ratio
        full_x = (total_w - dw) / 2
        full_y = (total_h - dh) / 2
        # On dessine sur la page courante, en décalant l'image selon half
        offset_x = full_x - (0 if half == 'left' else page_w)
        try:
            pdf.drawImage(img, offset_x, full_y,
                          width=dw, height=dh, mask='auto')
        except Exception:
            return
        # Chantier C (fuite n°2) : une photo promue en double page gardait
        # sa legende pour elle. Bandeau sur la moitie DROITE, comme le mode
        # pleine page — notes epinglees incluses.
        if half == 'right':
            cap = item.get('caption') or ''
            notes = item.get('photo_notes') or []
            lignes = []
            if cap:
                lignes.append(cap)
            for note in notes[:3]:
                aut = (note.get('auteur') or '').strip()
                lignes.append((aut + ' - ' if aut else '') +
                              chr(171) + ' ' + (note.get('texte') or '') + ' ' + chr(187))
            if lignes:
                tx, ty, tw, th = _trim_box()
                band_h = (8 + 4 * min(3, len(lignes))) * mm
                pdf.setFillColorRGB(*CREAM_RGB, alpha=0.92)
                pdf.rect(tx, ty, tw, band_h, fill=1, stroke=0)
                pdf.setFont('Times-Italic', 9)
                pdf.setFillColorRGB(*INK_RGB)
                inner_w = tw - (GUTTER_INNER_MM + GUTTER_OUTER_MM) * mm
                inner_x = tx + (GUTTER_INNER_MM if side == 'recto' else GUTTER_OUTER_MM) * mm
                y_l = ty + band_h - 5 * mm
                for ligne in lignes[:3]:
                    used = _wrap_text_left(ligne, inner_x, y_l,
                                           max_width=inner_w, line_height=10, max_lines=2)
                    y_l -= used * 10 + 3
        _draw_page_number(page_num, side)

    marge_reportee = []      # v5.6 : ce qui n'a pas tenu dans la case passe
                             # a la page composite suivante, jamais a la trappe

    def _draw_composite(chunk, margin_items_for_page, side, page_num,
                        day_label=None):
        _fill_page_cream()
        cx, cy, cw, ch = _content_box(side)

        # v5.17 : bandeau de jour — le livre marque le changement de date
        # et de lieu, comme les en-tetes de sections de l'album
        if day_label:
            pdf.setFont('Helvetica-Bold', 8.5)
            pdf.setFillColorRGB(*ACCENT_RGB)
            pdf.drawString(cx, cy + ch - 8, day_label)
            pdf.setStrokeColorRGB(*LINE_RGB)
            pdf.setLineWidth(0.4)
            pdf.line(cx, cy + ch - 12, cx + cw, cy + ch - 12)
            ch -= 8 * mm

        # Découpe content-box en album-zone + margin-zone
        margin_w = 0
        margin_h = 0
        album_x, album_y, album_w, album_h = cx, cy, cw, ch
        mzone_x = mzone_y = mzone_w = mzone_h = 0

        # Marge dynamique : si rien à mettre en marge, l'album prend toute la place
        # v5.6 : ce qui n'a pas tenu dans la case de la page precedente passe
        # en tete ici — la note attend son tour, elle ne disparait pas.
        if marge_reportee:
            margin_items_for_page = list(marge_reportee) + list(margin_items_for_page or [])
            marge_reportee[:] = []
        has_caption = any(it.get('caption') for it in chunk)
        has_photo_notes = any(it.get('photo_notes') for it in chunk)
        has_margin_notes = bool(margin_items_for_page) or has_photo_notes
        has_section_map = (show_section_maps
                           and section_zone_map_resolver is not None
                           and section_zone_map_resolver(chunk) is not None)
        margin_has_content = has_caption or has_margin_notes or has_section_map
        margin_pos_local = margin_pos_eff if margin_has_content else 'end'

        # v5.6 : la marge n'est plus une bande sur toute la hauteur — elle
        # occupe UNE CASE de la grille, comme une photo. Avant, trois lignes de
        # légende réservaient 30 % de la page sur toute sa hauteur, et le reste
        # de la colonne restait blanc. Le calepinage récupère cet espace.
        marge_en_case = margin_pos_local in ('outer', 'inner')
        if marge_en_case:
            album_x, album_y, album_w, album_h = cx, cy, cw, ch
        elif margin_pos_local == 'bottom':
            margin_h = ch * 0.22
            album_h = ch - margin_h - 4 * mm
            album_y = cy + margin_h + 4 * mm
            mzone_x = cx
            mzone_y = cy
            mzone_w = cw
            mzone_h = margin_h
        # else 'end' : pas de zone marge (mzone_w=0)

        # 1) Album : calepinage aux ratios reels (v5.17). La case de marge
        # participe au calepinage comme une case 3:4, cote exterieur/interieur.
        n = len(chunk)
        if marge_en_case:
            cote_gauche = ((margin_pos_local == 'outer' and side == 'verso') or
                           (margin_pos_local == 'inner' and side == 'recto'))
            cellules = ([{'_marge_case': True}] + list(chunk)) if cote_gauche \
                       else (list(chunk) + [{'_marge_case': True}])
            cases = _justified_boxes(cellules, album_x, album_y, album_w, album_h)
            if cote_gauche:
                mzone_x, mzone_y, mzone_w, mzone_h = cases[0]
                boxes = cases[1:]
            else:
                mzone_x, mzone_y, mzone_w, mzone_h = cases[-1]
                boxes = cases[:-1]
        else:
            boxes = _justified_boxes(list(chunk), album_x, album_y, album_w, album_h)
        # Si on a une zone marge, les légendes vont dans la marge.
        captions_to_margin = (mzone_w > 0)
        # v5.17 : regle simple — une lettre SEULEMENT si une legende part en
        # marge et s'y refere. Pas de note, pas de repere (retour d'Arthur).
        letter_for = {}
        if show_letters and captions_to_margin:
            letters_seq = 'abcdefgh'
            j = 0
            for item in chunk:
                if item.get('caption') and j < len(letters_seq):
                    letter_for[id(item)] = letters_seq[j]
                    j += 1
        # Chantier C : numerotation des APPELS DE NOTE de la page (1, 2, ...)
        # — continue d'une photo a l'autre, le texte attribue part en marge.
        note_start_for = {}
        num = 1
        for item in chunk:
            notes_item = item.get('photo_notes') or []
            if notes_item:
                note_start_for[id(item)] = num
                num += len(notes_item)
        for box, item in zip(boxes, chunk):
            _draw_in_box(item, *box,
                         with_letter=letter_for.get(id(item)),
                         note_start=note_start_for.get(id(item)),
                         skip_caption=captions_to_margin)

        # 2) Zone marge : mini-carte + légendes (a/b/c) + notes en marge
        if mzone_w > 0:
            # Filet de séparation. En case de grille, un trait vertical
            # couperait la page en deux au milieu des photos : c'est
            # l'étiquette LÉGENDES qui distingue la case, pas un filet.
            if not marge_en_case:
                pdf.setStrokeColorRGB(*LINE_RGB)
                pdf.setDash(2, 2)
                pdf.setLineWidth(0.4)
                pdf.line(mzone_x, mzone_y + mzone_h + 2 * mm,
                         mzone_x + mzone_w, mzone_y + mzone_h + 2 * mm)
                pdf.setDash()

            # Construction des items à mettre dans la marge :
            # 1) Légendes des photos principales (avec lettre)
            margin_entries = []
            for item in chunk:
                cap = item.get('caption')
                letter = letter_for.get(id(item))
                if cap and (captions_to_margin):
                    margin_entries.append({
                        'kind': 'caption',
                        'letter': letter,
                        'text': cap,
                        'thumb_path': None,
                        'src': item,   # fuite n°1 : une legende non placee se
                                       # reporte en fin de livre AVEC sa photo
                    })
            # Chantier C : les textes des epingles, attribues a leur auteur
            for item in chunk:
                notes_item = item.get('photo_notes') or []
                start = note_start_for.get(id(item))
                if not notes_item or not start:
                    continue
                for j, note in enumerate(notes_item):
                    aut = (note.get('auteur') or '').strip()
                    margin_entries.append({
                        'kind': 'photo_note',
                        'letter': str(start + j),
                        'text': (aut.upper() + ' - ' if aut else '') +
                                chr(171) + ' ' + (note.get('texte') or '') + ' ' + chr(187),
                        'thumb_path': None,
                    })
            # 2) Notes en marge attribuées à cette page
            for m in (margin_items_for_page or []):
                margin_entries.append({
                    'kind': 'note',
                    'letter': None,
                    'text': m.get('caption') or m.get('text_content') or '',
                    'thumb_path': m.get('photo_thumb'),
                    'src': m,          # la page d'album d'origine, pour le report
                })

            # 3) Mini-carte de section
            mini_png = None
            mini_label = None
            if show_section_maps and section_zone_map_resolver:
                resolved = section_zone_map_resolver(chunk)
                if resolved:
                    try:
                        mini_w_px = min(int((mzone_w / mm) * 5), 512)
                        mini_h_px = min(int(mini_w_px * 0.75), 512)
                        mini_png = fetch_static_map(
                            resolved['lat'], resolved['lng'],
                            resolved.get('zoom', 12),
                            mini_w_px, mini_h_px,
                            markers=[(resolved['lat'], resolved['lng'])],
                        )
                        mini_label = resolved.get('label') or None
                    except Exception:
                        mini_png = None

            reste = _draw_margin_zone(margin_entries, mzone_x, mzone_y,
                                      mzone_w, mzone_h, side,
                                      mini_map_png=mini_png,
                                      mini_map_label=mini_label) or []
            # on ne reporte que les NOTES : une legende suit sa photo, la
            # deplacer sur une autre page la rendrait incomprehensible
            # on reporte les NOTES (une legende suit sa photo, la deplacer la
            # rendrait incomprehensible) — sous leur forme d'origine, la seule
            # que sachent dessiner les pages de fin.
            # Chantier C (fuite n°1) : les legendes non placees se reportent
            # AUSSI (avec leur photo) au lieu de disparaitre en silence.
            marge_reportee[:] = [it['src'] for it in reste if it.get('src')]

        _draw_page_number(page_num, side)

    def _draw_margin_intro(side, page_num):
        _fill_page_cream()
        cx, cy, cw, ch = _content_box(side)
        pdf.setFont('Times-Italic', 28)
        pdf.setFillColorRGB(*INK_RGB)
        pdf.drawCentredString(cx + cw / 2, cy + ch / 2 + 6 * mm,
                              "Notes en marge")
        pdf.setStrokeColorRGB(*ACCENT_RGB)
        pdf.setLineWidth(0.8)
        pdf.line(cx + cw / 2 - 20 * mm, cy + ch / 2 + 1 * mm,
                 cx + cw / 2 + 20 * mm, cy + ch / 2 + 1 * mm)
        pdf.setFont('Helvetica', 9)
        pdf.setFillColorRGB(*INK_GHOST_RGB)
        pdf.drawCentredString(cx + cw / 2, cy + ch / 2 - 6 * mm,
                              "PHOTOS DE CONTEXTE · LIEUX · BILLETS")
        _draw_page_number(page_num, side)

    def _draw_margin_grid(chunk, side, page_num):
        _fill_page_cream()
        cx, cy, cw, ch = _content_box(side)
        cell_w = (cw - 6 * mm) / 2
        cell_h = (ch - 6 * mm) / 2
        for i, m in enumerate(chunk):
            col, row = i % 2, i // 2
            mx = cx + col * (cell_w + 6 * mm)
            my = cy + (1 - row) * (cell_h + 6 * mm)
            if m.get('photo_path'):
                _draw_image_box(m, mx, my, cell_w, cell_h,
                                caption=m.get('caption'), cap_lines=5)
            elif m.get('text_content'):
                _draw_text_box(m, mx, my, cell_w, cell_h)
        _draw_page_number(page_num, side)

    def _draw_colophon(side, page_num):
        _fill_page_cream()
        cx, cy, cw, ch = _content_box(side)
        pdf.setFont('Times-Italic', 16)
        pdf.setFillColorRGB(*INK_FAINT_RGB)
        pdf.drawCentredString(cx + cw / 2, cy + ch / 2, "Fin")
        pdf.setStrokeColorRGB(*ACCENT_RGB)
        pdf.setLineWidth(0.6)
        pdf.line(cx + cw / 2 - 12 * mm, cy + ch / 2 - 6 * mm,
                 cx + cw / 2 + 12 * mm, cy + ch / 2 - 6 * mm)
        pdf.setFont('Helvetica', 7)
        pdf.setFillColorRGB(*INK_GHOST_RGB)
        pdf.drawCentredString(page_w / 2, bleed + 8 * mm,
                              "NOTRE HISTOIRE · histoire.aqgk.fr")

    # Boucle principale ─────────────────────────────────────────────────────
    side = 'recto'  # page 1 = recto
    page_num = 0
    # v5.21 (chantier D, audit DA §1.6) : pagination belle-page — le folio
    # « 1 » tombe sur la PREMIERE PAGE DE CONTENU, qui est un recto (les
    # belles pages sont impaires). Garde, titre et blanches d'alignement
    # comptent physiquement (la parite recto-impair tient) mais ne portent
    # jamais de numero.
    numbering = False
    NUM_KINDS = ('overview_map', 'full', 'composite', 'margin_intro',
                 'margin_grid', 'colophon', 'stats', 'spread', 'chapter_open')

    # Forcer un kind à démarrer sur recto/verso ?
    def _need_recto(kind):
        return kind in ('chapter_open', 'overview_map', 'title', 'stats')

    def _need_verso_start(kind):
        return kind in ('spread', 'back_cover')

    i = 0
    while i < len(program):
        entry = program[i]
        kind = entry['kind']

        # Forcer alignement avec page blanche si nécessaire (jamais de folio
        # sur une blanche, mais elle compte dans la parite)
        if _need_verso_start(kind) and side == 'recto':
            if numbering:
                page_num += 1
            _draw_blank(side, 0)
            pdf.showPage()
            side = 'verso'
        if _need_recto(kind) and side == 'verso':
            if numbering:
                page_num += 1
            _draw_blank(side, 0)
            pdf.showPage()
            side = 'recto'
        if not numbering and kind in NUM_KINDS:
            numbering = True

        # Cas spread : 2 pages
        if kind == 'spread':
            page_num += 1
            _draw_spread_half(entry['item'], 'verso', page_num, 'left')
            pdf.showPage()
            page_num += 1
            _draw_spread_half(entry['item'], 'recto', page_num, 'right')
            pdf.showPage()
            side = 'verso'  # page suivante = verso
            i += 1
            continue

        # Cas spécial : couverture
        if kind == 'cover':
            _draw_cover()
            pdf.showPage()
            side = 'verso'
            i += 1
            continue

        # Cas blank / garde / titre / 4e de couverture : jamais de folio
        if kind == 'blank':
            if numbering:
                page_num += 1
            _draw_blank(side, 0)
            pdf.showPage()
            side = 'recto' if side == 'verso' else 'verso'
            i += 1
            continue
        if kind in ('garde', 'title', 'back_cover'):
            if numbering:
                page_num += 1
            if kind == 'garde':
                _draw_garde(side)
            elif kind == 'title':
                _draw_title_page(side)
            else:
                _draw_back_cover()
            pdf.showPage()
            side = 'recto' if side == 'verso' else 'verso'
            i += 1
            continue

        # Sinon : 1 page normale
        page_num += 1
        if kind == 'chapter_open':
            _draw_chapter_open(entry, side)   # compte mais pas de folio
        elif kind == 'stats':
            _draw_stats(side, page_num)
        elif kind == 'overview_map':
            _draw_overview_map(side, page_num)
        elif kind == 'full':
            _draw_full(entry['item'], side, page_num)
        elif kind == 'composite':
            _draw_composite(entry['chunk'],
                            entry.get('margin_items'),
                            side, page_num,
                            day_label=entry.get('day_label'))
        elif kind == 'margin_intro':
            _draw_margin_intro(side, page_num)
        elif kind == 'margin_grid':
            _draw_margin_grid(entry['chunk'], side, page_num)
        elif kind == 'colophon':
            # v5.6.1 : AVANT de fermer le livre, on regarde ce qui n'a jamais
            # trouvé sa place dans une case et on lui donne des pages a la fin.
            # Une note ecrite par la main de quelqu'un ne se perd pas parce que
            # la mise en page manquait de hauteur. (5 notes sur 22 tombaient.)
            if marge_reportee:
                restes = list(marge_reportee)
                marge_reportee[:] = []
                page_num -= 1                 # on rendra le colophon apres
                suite = [{'kind': 'margin_intro'}]
                for s in range(0, len(restes), 4):
                    suite.append({'kind': 'margin_grid', 'chunk': restes[s:s + 4]})
                suite.append({'kind': 'colophon'})
                program[i + 1:i + 1] = suite
                i += 1
                continue
            _draw_colophon(side, page_num)
        else:
            _draw_blank(side, page_num)

        pdf.showPage()
        side = 'verso' if side == 'recto' else 'recto'
        i += 1

    pdf.save()
    buf.seek(0)
    return buf
