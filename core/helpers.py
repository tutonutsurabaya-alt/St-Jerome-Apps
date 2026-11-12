"""Shared domain helpers (covers, citations, call numbers, barcodes, robot, WA notifications)."""

import datetime
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from flask import current_app, has_app_context
import database
from core.features import is_enabled

ALLOWED_EXTENSIONS_IMAGE = {'png', 'jpg', 'jpeg', 'webp'}
ANTREAN_FILE = 'antrian_stiker.json'
ROBOT_STATE_FILE = 'robot_state.json'


def allowed_image(filename):
    """Return True if filename has a supported cover image extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS_IMAGE


def _app_root():
    if has_app_context():
        return current_app.root_path
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_book_cover_url(img_val):
    """Resolve bibliografi.image value to an absolute URL or /static path."""
    if not img_val or img_val in ('tidak_ada', 'NOT_FOUND', 'RATE_LIMIT'):
        return None
    val = str(img_val).strip()
    if val.startswith('http') or val.startswith('/static/'):
        return val
    if val.startswith('uploads/'):
        return f"/static/{val}"
    root = _app_root()
    if os.path.exists(os.path.join(root, 'static', 'uploads', val)):
        return f"/static/uploads/{val}"
    if os.path.exists(os.path.join(root, 'static', val)):
        return f"/static/{val}"
    return f"/static/uploads/{val}"


def parse_author_name(author_str):
    """Parse bibliographic author string into Turabian/APA citation variants."""
    if not author_str:
        return {'full': 'Anonim', 'last': 'Anonim', 'first': '', 'citation_first_last': 'Anonim', 'citation_last_first': 'Anonim'}
    cleaned = author_str.replace('<', '').replace('>', '').strip()
    if ';' in cleaned:
        authors = [a.strip() for a in cleaned.split(';') if a.strip()]
    elif ',' in cleaned and not any(tag in author_str for tag in ['<', '>']):
        parts = [p.strip() for p in cleaned.split(',')]
        if len(parts) == 2:
            return {
                'full': f"{parts[1]} {parts[0]}",
                'last': parts[0],
                'first': parts[1],
                'citation_first_last': f"{parts[1]} {parts[0]}",
                'citation_last_first': f"{parts[0]}, {parts[1]}"
            }
        authors = parts
    else:
        authors = [cleaned]
    primary = authors[0]
    if ',' in primary:
        parts = [p.strip() for p in primary.split(',')]
        last = parts[0]
        first = ' '.join(parts[1:])
        return {
            'full': f"{first} {last}".strip(),
            'last': last,
            'first': first,
            'citation_first_last': f"{first} {last}".strip(),
            'citation_last_first': f"{last}, {first}".strip()
        }
    words = primary.split()
    if len(words) > 1:
        last = words[-1]
        first = ' '.join(words[:-1])
        return {
            'full': primary,
            'last': last,
            'first': first,
            'citation_first_last': primary,
            'citation_last_first': f"{last}, {first}"
        }
    return {'full': primary, 'last': primary, 'first': '', 'citation_first_last': primary, 'citation_last_first': primary}


def generate_citations(book):
    """Generate Turabian, APA, and RIS citation strings for a bibliografi record."""
    author_info = parse_author_name(book.get('pengarang', ''))
    title = (book.get('judul') or 'Tanpa Judul').strip()
    title_clean = re.sub(r'[\/:]$', '', title).strip()
    city = (book.get('tempat_terbit') or 't.k.').strip()
    publisher = (book.get('penerbit') or 't.p.').strip()
    year = (book.get('tahun_terbit') or 't.t.').strip()
    isbn = (book.get('isbn') or '').strip()

    turabian_footnote = f"{author_info['citation_first_last']}, _{title_clean}_ ({city}: {publisher}, {year}), hlm. [halaman]."
    turabian_biblio = f"{author_info['citation_last_first']}. _{title_clean}_. {city}: {publisher}, {year}."

    initial = (author_info['first'][:1] + '.') if author_info['first'] else ''
    apa_author = f"{author_info['last']}, {initial}".strip() if initial else author_info['last']
    apa = f"{apa_author} ({year}). _{title_clean}_. {publisher}."

    ris_lines = [
        "TY  - BOOK",
        f"TI  - {title_clean}",
        f"AU  - {author_info['citation_last_first']}",
        f"PY  - {year}",
        f"CY  - {city}",
        f"PB  - {publisher}",
        f"SN  - {isbn}",
        f"CN  - {book.get('nomor_panggil', '')}",
        "ER  - "
    ]
    ris = "\n".join(ris_lines)

    return {
        'turabian_footnote': turabian_footnote,
        'turabian_biblio': turabian_biblio,
        'apa': apa,
        'ris': ris
    }


def parse_slims_call_number(cn):
    """Split SLiMS call_number string into (klasifikasi, cutter, huruf_judul)."""
    cn = (cn or '').strip()
    if not cn:
        return '', '', ''
    parts = cn.split()
    if not parts:
        return '', '', ''
    is_r = (parts[0] == 'R')
    if is_r:
        r_prefix = 'R '
        tokens = parts[1:]
    else:
        r_prefix = ''
        tokens = parts
    if len(tokens) == 0:
        return 'R' if is_r else '', '', ''
    elif len(tokens) == 1:
        return f"{r_prefix}{tokens[0]}", '', ''
    elif len(tokens) == 2:
        if re.match(r'^[0-9]+$', tokens[1]):
            return f"{r_prefix}{tokens[0]} {tokens[1]}", '', ''
        else:
            return f"{r_prefix}{tokens[0]}", tokens[1], ''
    elif len(tokens) == 3:
        return f"{r_prefix}{tokens[0]}", tokens[1], tokens[2]
    else:
        huruf = tokens[-1]
        cutter = tokens[-2]
        ddc = " ".join(tokens[:-2])
        return f"{r_prefix}{ddc}", cutter, huruf


def calculate_working_days(start_date, end_date, conn):
    """Count weekday working days excluding holidays stored in hari_libur."""
    try:
        libur_rows = conn.execute("SELECT tanggal FROM hari_libur").fetchall()
        libur_set = set(row[0] for row in libur_rows)
    except Exception:
        libur_set = set()

    days = (end_date - start_date).days
    working_days = 0
    for i in range(1, days + 1):
        day = start_date + datetime.timedelta(days=i)
        if day.weekday() < 5 and day.strftime('%Y-%m-%d') not in libur_set:
            working_days += 1
    return working_days


def format_nomor_panggil(klasifikasi, cutter, huruf_judul, pengarang, judul):
    """Build normalized DDC call number label for display and spine labels."""
    klas = (klasifikasi or '').strip()
    cut = (cutter or '').strip()
    hj = (huruf_judul or '').strip()

    if klas == 'R' and any(c.isdigit() for c in cut):
        klas = f"R {cut}"
        cut = hj
        hj = ''

    if cut:
        cut = re.sub(r'[^a-zA-Z0-9\-]', '', cut)[:5].upper()
    elif pengarang:
        nama_utama = pengarang.split(',')[0].strip()
        nama_utama = re.sub(r'[^a-zA-Z]', '', nama_utama)
        if nama_utama:
            cut = nama_utama[:3].upper()

    if hj:
        hj_clean = re.sub(r'[^a-zA-Z0-9\-]', '', hj)
        if hj_clean:
            hj = hj_clean[:4].lower()
    elif judul:
        clean_j = re.sub(r'^(The|A|An|Si|Sang)\s+', '', judul, flags=re.IGNORECASE).strip()
        clean_j = re.sub(r'[^a-zA-Z]', '', clean_j)
        if clean_j:
            hj = clean_j[:1].lower()

    parts = []
    if klas:
        parts.append(klas)
    if cut:
        parts.append(cut)
    if hj:
        parts.append(hj)
    return ' '.join(parts) if parts else '-'


def get_next_majalah_barcode(prefix, year_str):
    """Compute next sequential magazine accession barcode (e.g., ``M 001/26``)."""
    year_suffix = str(year_str)[-2:] if len(str(year_str)) >= 2 else '26'
    pattern = f"{prefix} %/{year_suffix}"
    conn = database.get_db_connection()
    try:
        rows = conn.execute("SELECT no_induk FROM eksemplar WHERE no_induk LIKE ?", (pattern,)).fetchall()
    finally:
        conn.close()

    max_serial = 0
    regex = rf"^{re.escape(prefix)}\s+(\d+)/{re.escape(year_suffix)}$"
    for (barcode,) in rows:
        m = re.match(regex, barcode.strip())
        if m:
            try:
                val = int(m.group(1))
                if val > max_serial:
                    max_serial = val
            except Exception:
                pass

    next_serial = max_serial + 1
    barcode = f"{prefix} {next_serial:03d}/{year_suffix}"
    return next_serial, barcode


def get_robot_state():
    """Read background OpenLibrary cover-scraper state."""
    try:
        with open(ROBOT_STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"is_running": False, "progress": 0, "total": 0, "current_book": "", "results": [], "error": ""}


def save_robot_state(state):
    """Persist background OpenLibrary cover-scraper state."""
    with open(ROBOT_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f)


def robot_worker(limit=50):
    """Background worker fetching missing covers from OpenLibrary when online auto-cover is enabled."""
    if not is_enabled('ENABLE_AUTOCOVER_ONLINE'):
        save_robot_state({
            "is_running": False, "progress": 0, "total": 0,
            "current_book": "Dinonaktifkan", "results": [],
            "error": "Auto-cover online dinonaktifkan pada mode ini."
        })
        return

    state = {"is_running": True, "progress": 0, "total": limit, "current_book": "Memulai...", "results": [], "error": ""}
    save_robot_state(state)

    conn = database.get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT id, judul, pengarang, isbn FROM bibliografi "
            "WHERE image IS NULL OR image = '' OR image = 'NOT_FOUND' OR image = 'tidak_ada' OR image = 'RATE_LIMIT' "
            "LIMIT ?",
            (limit,)
        )
        books = cursor.fetchall()

        if not books:
            state["is_running"] = False
            state["current_book"] = "Selesai"
            state["error"] = "Luar biasa! Tidak ada lagi buku yang membutuhkan sampul."
            save_robot_state(state)
            return

        state["total"] = len(books)

        for row in books:
            current_state = get_robot_state()
            if not current_state.get("is_running"):
                break

            biblio_id = row['id']
            judul = row['judul']
            pengarang = row['pengarang']
            isbn = row['isbn'] if row['isbn'] else ''
            state["current_book"] = f"{judul} - {pengarang}"
            save_robot_state(state)

            image_url = 'NOT_FOUND'

            if isbn and isbn.strip() and isbn.strip() not in ('', 'None', '-'):
                clean_isbn = isbn.strip().replace('-', '').replace(' ', '')
                try:
                    check_url = "https://covers.openlibrary.org/b/isbn/" + clean_isbn + "-M.jpg?default=false"
                    req_chk = urllib.request.Request(check_url, headers={'User-Agent': 'StJeromeLib/1.0'})
                    with urllib.request.urlopen(req_chk, timeout=8) as r:
                        if r.status == 200:
                            image_url = "https://covers.openlibrary.org/b/isbn/" + clean_isbn + "-M.jpg"
                except Exception:
                    pass

            if image_url == 'NOT_FOUND':
                try:
                    time.sleep(0.3)
                    q = urllib.parse.quote((judul or '') + ' ' + (pengarang or ''))
                    ol_url = "https://openlibrary.org/search.json?q=" + q + "&fields=cover_i&limit=1"
                    req2 = urllib.request.Request(ol_url, headers={'User-Agent': 'StJeromeLib/1.0'})
                    with urllib.request.urlopen(req2, timeout=10) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        docs = data.get('docs', [])
                        if docs and docs[0].get('cover_i'):
                            cover_id = docs[0]['cover_i']
                            image_url = "https://covers.openlibrary.org/b/id/" + str(cover_id) + "-M.jpg"
                except Exception:
                    pass

            conn.execute("UPDATE bibliografi SET image = ? WHERE id = ?", (image_url, biblio_id))
            conn.commit()

            status_text = "Ditemukan" if image_url.startswith('http') else "Kosong"
            state["results"].insert(0, {
                "judul": judul,
                "status": status_text,
                "image_url": image_url if image_url.startswith('http') else None
            })
            state["progress"] += 1
            save_robot_state(state)
            time.sleep(0.2)

        state["is_running"] = False
        state["current_book"] = "Selesai"
        save_robot_state(state)
    finally:
        conn.close()


def send_wa_notification(member_id, message):
    """Send circulation notification via WA Bot when ENABLE_WA_BOT is active (no-op otherwise)."""
    if not is_enabled('ENABLE_WA_BOT'):
        return
    try:
        conn = database.get_db_connection()
        try:
            member = conn.execute("SELECT telepon FROM anggota WHERE member_id = ?", (member_id,)).fetchone()
        finally:
            conn.close()

        if member and member['telepon']:
            def _send():
                try:
                    data = json.dumps({'number': member['telepon'], 'message': message}).encode('utf-8')
                    req = urllib.request.Request('http://127.0.0.1:3005/api/send_message', data=data, headers={'Content-Type': 'application/json'})
                    urllib.request.urlopen(req, timeout=5)
                except Exception:
                    pass
            threading.Thread(target=_send, daemon=True).start()
    except Exception:
        pass


def send_daily_wa_reminders():
    """Send due-date WA reminders when ENABLE_WA_BOT is enabled; returns count sent (0 when disabled)."""
    if not is_enabled('ENABLE_WA_BOT'):
        return 0
    conn = None
    try:
        conn = database.get_db_connection()
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        tomorrow_str = (datetime.date.today() + datetime.timedelta(days=1)).strftime('%Y-%m-%d')

        loans = conn.execute('''
            SELECT s.id, s.member_id, a.nama, a.telepon, b.judul, s.due_date
            FROM sirkulasi s
            JOIN anggota a ON s.member_id = a.member_id
            JOIN buku b ON s.no_induk = b.no_induk
            WHERE s.return_date IS NULL
            AND a.telepon IS NOT NULL AND a.telepon != ''
        ''').fetchall()

        count = 0
        for loan in loans:
            due = str(loan['due_date']).split(' ')[0]
            if due == tomorrow_str:
                jenis = "Akan Jatuh Tempo Besok"
                salam = "Jangan lupa dikembalikan tepat waktu ya."
            elif due == today_str:
                jenis = "JATUH TEMPO HARI INI"
                salam = "Harap kembalikan ke perpustakaan hari ini."
            elif due < today_str:
                jenis = "TERLAMBAT"
                salam = "Harap SEGERA dikembalikan. Mohon perhatikan denda keterlambatan."
            else:
                continue

            msg = "*PENGINGAT PERPUSTAKAAN ST. JEROME*\n\n"
            msg += f"Halo {loan['nama']}, mengingatkan status buku yang Anda pinjam:\n"
            msg += f"*{loan['judul']}*\n"
            msg += f"Status: *{jenis}* ({due})\n\n"
            msg += f"{salam}\nTerima kasih!"

            send_wa_notification(loan['member_id'], msg)
            count += 1
            time.sleep(2)

        return count
    except Exception as e:
        print("Error sending WA reminders:", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
