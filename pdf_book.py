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
CREAM_RGB = (0.980, 0.972, 0.957)
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

    # Bleed activé uniquement si quelques photos sont en pleine page
    pages_main = pages_data['main']
    needs_bleed = any(_full_bleed_mode(p) in ('full', 'spread') for p in pages_main)
    bleed = (BLEED_MM if needs_bleed else 0) * mm

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
            if stringWidth(test, font_name, font_size) <= max_width:
                cur.append(w)
            else:
                if cur:
                    lines.append(' '.join(cur))
                cur = [w]
        if cur:
            lines.append(' '.join(cur))
            
        # Si une ligne reste trop longue (mot ultra-long), elle est gardée
        # tronquée par stringWidth. On pourrait couper avec '...' mais on
        # laisse le mot pour ne pas perdre d'info.
        lines = lines[:max_lines]
        total_h = len(lines) * line_height
        y = cy + total_h / 2
        for line in lines:
            if align == 'left':
                pdf.drawString(cx, y, line)
            elif align == 'right':
                pdf.drawRightString(cx, y, line)
            else:
                pdf.drawCentredString(cx, y, line)
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
            if stringWidth(test, font_name, font_size) <= max_width:
                cur.append(w)
            else:
                if cur:
                    lines.append(' '.join(cur))
                cur = [w]
        if cur:
            lines.append(' '.join(cur))
        lines = lines[:max_lines]
        y = y_top
        for line in lines:
            pdf.drawString(x, y, line)
            y -= line_height
        return len(lines)

    def _draw_image_box(item, x, y, w, h, *, caption=None, with_letter=None):
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
        cap_h = 6 * mm if caption else 0
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
            pdf.setFillColorRGB(0.98, 0.972, 0.957, alpha=0.85)
            pdf.rect(cx + 2, cy + 2, 12, 12, fill=1, stroke=0)
            pdf.setFillColorRGB(*ACCENT_RGB)
            pdf.drawCentredString(cx + 8, cy + 5, with_letter)
        if caption:
            pdf.setFont('Times-Italic', 8.5)
            pdf.setFillColorRGB(*INK_SOFT_RGB)
            _wrap_text(caption, x + w / 2, y + 2 * mm,
                       max_width=w, line_height=10, max_lines=2)
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
        # Caption en bandeau bas
        cap = item.get('caption') or ''
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
        qr_size = min(w, h) * 0.20
        cap_h = 6 * mm if item.get('caption') else 0
        avail_h = h - cap_h - qr_size - 2 * mm
        ratio = min(w / iw, avail_h / ih)
        dw, dh = iw * ratio, ih * ratio
        cx = x + (w - dw) / 2
        cy = y + cap_h + qr_size + 2 * mm + (avail_h - dh)
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
        # QR
        if item.get('video_token') and qr_make and video_url_for:
            try:
                video_url = video_url_for(item['video_token'])
                qr_img = qr_make(video_url)
                qr_buf = io.BytesIO()
                qr_img.save(qr_buf, 'PNG')
                qr_buf.seek(0)
                qr_x = x + (w - qr_size) / 2
                qr_y = y + cap_h
                pdf.drawImage(ImageReader(qr_buf), qr_x, qr_y,
                              width=qr_size, height=qr_size, mask='auto')
            except Exception:
                pass
        if with_letter and show_letters:
            pdf.setFillColorRGB(0.98, 0.972, 0.957, alpha=0.85)
            pdf.rect(cx + 2, cy + 2, 12, 12, fill=1, stroke=0)
            pdf.setFillColorRGB(*ACCENT_RGB)
            pdf.setFont('Helvetica-Bold', 8)
            pdf.drawCentredString(cx + 8, cy + 5, with_letter)
        if item.get('caption'):
            pdf.setFont('Times-Italic', 8.5)
            pdf.setFillColorRGB(*INK_SOFT_RGB)
            _wrap_text(item['caption'], x + w / 2, y + 2 * mm,
                       max_width=w, line_height=10, max_lines=1)
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

    def _draw_in_box(item, x, y, w, h, *, with_letter=None,
                     skip_caption=False):
        cap = None if skip_caption else item.get('caption')
        if item.get('video_path'):
            return _draw_video_box(item, x, y, w, h, with_letter=with_letter)
        if item.get('photo_path'):
            return _draw_image_box(item, x, y, w, h, caption=cap,
                                   with_letter=with_letter)
        if item.get('type') == 'text':
            _draw_text_box(item, x, y, w, h)
        return None

    def _grid_layout(n, x, y, w, h, gap=3 * mm):
        """Grille adaptative pour 1..4 photos."""
        boxes = []
        if n == 1:
            boxes.append((x, y, w, h))
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

        # 3) Items
        for item in items:
            if y_top < area_y + 5 * mm:
                break
            letter = item.get('letter')
            text = item.get('text') or ''
            thumb = item.get('thumb_path')
            kind = item.get('kind')
            block_h_used = 0
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
                    pdf.drawImage(img, x, y_top - th_h,
                                  width=th_w, height=th_h, mask='auto')
                    y_top -= th_h + 2 * mm
                    block_h_used += th_h + 2 * mm
                except Exception:
                    pass
            # Lettre + texte
            if text or letter:
                if letter and show_letters:
                    pdf.setFont('Helvetica-Bold', 8)
                    pdf.setFillColorRGB(*ACCENT_RGB)
                    pdf.drawString(x, y_top - 8, letter)
                    text_x = x + 9
                else:
                    text_x = x
                pdf.setFont('Times-Italic', 8.5)
                pdf.setFillColorRGB(*INK_SOFT_RGB)
                used = _wrap_text_left(text, text_x, y_top - 8,
                                       max_width=area_w - (text_x - x) - 2,
                                       line_height=10, max_lines=4)
                y_top -= used * 10 + 4 * mm
            else:
                y_top -= 2 * mm
            # Petit séparateur ligne fine
            pdf.setStrokeColorRGB(*LINE_RGB)
            pdf.setLineWidth(0.2)
            pdf.line(x, y_top + 2, x + area_w * 0.4, y_top + 2)

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
    program.append({'kind': 'blank'})  # dos de couverture

    # Carte d'ensemble (page recto)
    if show_overview_map and geo_summary and fetch_static_map and compute_zoom:
        program.append({'kind': 'overview_map'})
        program.append({'kind': 'blank'})

    # Pages principales : structurer par chunks selon mode plein-page
    margin_items_pool = list(pages_data.get('margin') or []) if margin_pos != 'end' else []

    i = 0
    while i < len(pages_main):
        item = pages_main[i]
        mode = _full_bleed_mode(item)
        if mode == 'spread':
            program.append({'kind': 'spread', 'item': item})
            i += 1
        elif mode == 'full':
            program.append({'kind': 'full', 'item': item})
            i += 1
        else:
            # Composite : chunk de n_per_page items normaux jusqu'au prochain non-normal
            chunk = []
            while i < len(pages_main) and len(chunk) < n_per_page:
                if _full_bleed_mode(pages_main[i]) != 'normal':
                    break
                chunk.append(pages_main[i])
                i += 1
            if chunk:
                program.append({'kind': 'composite', 'chunk': chunk})

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

    # Page de fin
    program.append({'kind': 'colophon'})

    # Distribution des margin items au fil des pages composites ─────────────
    if margin_pos != 'end' and margin_items_pool:
        composite_count = sum(1 for e in program if e['kind'] == 'composite')
        per = max(1, (len(margin_items_pool) + composite_count - 1) // max(composite_count, 1))
        idx = 0
        for e in program:
            if e['kind'] == 'composite' and idx < len(margin_items_pool):
                e['margin_items'] = margin_items_pool[idx:idx + per]
                idx += len(e['margin_items'])

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
        pdf.setFont('Times-Italic', 36)
        pdf.setFillColorRGB(*INK_RGB)
        pdf.drawCentredString(page_w / 2, bleed + (h_mm * mm) * 0.30, carnet['title'])
        sub = []
        if carnet.get('location'):
            sub.append(carnet['location'])
        if carnet.get('date_start') and carnet.get('date_end') and carnet['date_start'] != carnet['date_end']:
            sub.append(f"{carnet['date_start']} → {carnet['date_end']}")
        elif carnet.get('date_start'):
            sub.append(carnet['date_start'])
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

    def _draw_overview_map(side, page_num):
        _fill_page_cream()
        if not (geo_summary and fetch_static_map and compute_zoom):
            return
        cx, cy, cw, ch = _content_box(side)
        pdf.setFont('Times-Italic', 24)
        pdf.setFillColorRGB(*INK_RGB)
        pdf.drawString(cx, cy + ch - 14 * mm, "Notre voyage")
        pdf.setFont('Helvetica', 9)
        pdf.setFillColorRGB(*INK_FAINT_RGB)
        pdf.drawString(cx, cy + ch - 22 * mm,
                       f"{geo_summary['count']} lieu(x) sur la carte")
        # Filet terracotta
        pdf.setStrokeColorRGB(*ACCENT_RGB)
        pdf.setLineWidth(0.8)
        pdf.line(cx, cy + ch - 17 * mm, cx + 20 * mm, cy + ch - 17 * mm)

        map_w_mm = (cw / mm) - 4
        map_h_mm = (ch / mm) - 40
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
                map_x = cx + 2 * mm
                map_y = cy + 4 * mm
                pdf.drawImage(ImageReader(io.BytesIO(png)),
                              map_x, map_y,
                              width=map_w_mm * mm, height=map_h_mm * mm,
                              mask='auto')
                pdf.setStrokeColorRGB(*LINE_RGB)
                pdf.setLineWidth(0.5)
                pdf.rect(map_x, map_y, map_w_mm * mm, map_h_mm * mm,
                         fill=0, stroke=1)
            except Exception:
                pdf.setFont('Helvetica', 10)
                pdf.setFillColorRGB(*INK_GHOST_RGB)
                pdf.drawCentredString(cx + cw / 2, cy + ch / 2,
                                      "Carte indisponible")
        pdf.setFont('Helvetica', 7)
        pdf.setFillColorRGB(*INK_GHOST_RGB)
        pdf.drawCentredString(page_w / 2, bleed + 6 * mm,
                              "© OpenStreetMap contributors")
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
        _draw_page_number(page_num, side)

    def _draw_composite(chunk, margin_items_for_page, side, page_num):
        _fill_page_cream()
        cx, cy, cw, ch = _content_box(side)

        # Découpe content-box en album-zone + margin-zone
        margin_w = 0
        margin_h = 0
        album_x, album_y, album_w, album_h = cx, cy, cw, ch
        mzone_x = mzone_y = mzone_w = mzone_h = 0

        if margin_pos_eff == 'outer':
            # Notes côté extérieur (loin de la reliure)
            margin_w = cw * 0.30
            album_w = cw - margin_w - 4 * mm
            if side == 'recto':
                album_x = cx
                mzone_x = cx + album_w + 4 * mm
            else:
                mzone_x = cx
                album_x = cx + margin_w + 4 * mm
            album_y = cy
            album_h = ch
            mzone_y = cy
            mzone_w = margin_w
            mzone_h = ch
        elif margin_pos_eff == 'inner':
            # Notes côté reliure (proche de la couture)
            margin_w = cw * 0.30
            album_w = cw - margin_w - 4 * mm
            if side == 'recto':
                mzone_x = cx
                album_x = cx + margin_w + 4 * mm
            else:
                album_x = cx
                mzone_x = cx + album_w + 4 * mm
            album_y = cy
            album_h = ch
            mzone_y = cy
            mzone_w = margin_w
            mzone_h = ch
        elif margin_pos_eff == 'bottom':
            margin_h = ch * 0.22
            album_h = ch - margin_h - 4 * mm
            album_y = cy + margin_h + 4 * mm
            mzone_x = cx
            mzone_y = cy
            mzone_w = cw
            mzone_h = margin_h
        # else 'end' : pas de zone marge (mzone_w=0)

        # 1) Album : disposition photos
        n = len(chunk)
        # Lettres a/b/c…
        letter_for = {}
        if show_letters:
            letters_seq = 'abcdefgh'
            for i, item in enumerate(chunk):
                if i < len(letters_seq):
                    letter_for[id(item)] = letters_seq[i]

        boxes = _grid_layout(n, album_x, album_y, album_w, album_h)
        # Si on a une zone marge, les légendes vont dans la marge.
        captions_to_margin = (mzone_w > 0)
        for box, item in zip(boxes, chunk):
            _draw_in_box(item, *box,
                         with_letter=letter_for.get(id(item)),
                         skip_caption=captions_to_margin)

        # 2) Zone marge : mini-carte + légendes (a/b/c) + notes en marge
        if mzone_w > 0:
            # Filet de séparation
            pdf.setStrokeColorRGB(*LINE_RGB)
            pdf.setDash(2, 2)
            pdf.setLineWidth(0.4)
            if margin_pos_eff in ('outer', 'inner'):
                # ligne verticale entre album et marge
                if (margin_pos_eff == 'outer' and side == 'recto') or \
                   (margin_pos_eff == 'inner' and side == 'verso'):
                    sep_x = mzone_x - 2 * mm
                else:
                    sep_x = mzone_x + mzone_w + 2 * mm
                pdf.line(sep_x, mzone_y, sep_x, mzone_y + mzone_h)
            else:  # bottom
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
                    })
            # 2) Notes en marge attribuées à cette page
            for m in (margin_items_for_page or []):
                margin_entries.append({
                    'kind': 'note',
                    'letter': None,
                    'text': m.get('caption') or m.get('text_content') or '',
                    'thumb_path': m.get('photo_thumb'),
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

            _draw_margin_zone(margin_entries, mzone_x, mzone_y,
                              mzone_w, mzone_h, side,
                              mini_map_png=mini_png,
                              mini_map_label=mini_label)

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
                                caption=m.get('caption'))
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
    page_num = 0    # le numéro affiché commence à 1 sur la 1re page de contenu

    # Forcer un kind à démarrer sur recto/verso ?
    def _need_recto(kind):
        return kind in ('chapter_open', 'overview_map')

    def _need_verso_start(kind):
        return kind == 'spread'

    i = 0
    while i < len(program):
        entry = program[i]
        kind = entry['kind']

        # Forcer alignement avec page blanche si nécessaire
        if _need_verso_start(kind) and side == 'recto':
            page_num += 1
            _draw_blank(side, page_num)
            pdf.showPage()
            side = 'verso'
        if _need_recto(kind) and side == 'verso':
            page_num += 1
            _draw_blank(side, page_num)
            pdf.showPage()
            side = 'recto'

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

        # Cas blank
        if kind == 'blank':
            _draw_blank(side, 0)  # pas de numéro sur blank initial
            pdf.showPage()
            side = 'recto' if side == 'verso' else 'verso'
            i += 1
            continue

        # Sinon : 1 page normale
        page_num += 1
        if kind == 'overview_map':
            _draw_overview_map(side, page_num)
        elif kind == 'full':
            _draw_full(entry['item'], side, page_num)
        elif kind == 'composite':
            _draw_composite(entry['chunk'],
                            entry.get('margin_items'),
                            side, page_num)
        elif kind == 'margin_intro':
            _draw_margin_intro(side, page_num)
        elif kind == 'margin_grid':
            _draw_margin_grid(entry['chunk'], side, page_num)
        elif kind == 'colophon':
            _draw_colophon(side, page_num)
        else:
            _draw_blank(side, page_num)

        pdf.showPage()
        side = 'verso' if side == 'recto' else 'recto'
        i += 1

    pdf.save()
    buf.seek(0)
    return buf
