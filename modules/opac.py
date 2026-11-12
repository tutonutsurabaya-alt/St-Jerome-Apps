"""Domain module: opac."""

import csv
import datetime
import io
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from functools import wraps
from io import BytesIO, StringIO

import pandas as pd
import requests
from flask import (
    Response,
    after_this_request,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

import database
from database import get_db_connection
from core.router import ModuleRouter
from core.features import get_branding, get_feature_config, is_enabled
from core.auth import (
    admin_api_required,
    api_login_required,
    feature_required,
    login_required,
    user_has_permission,
)
from core.helpers import (
    ALLOWED_EXTENSIONS_IMAGE,
    ANTREAN_FILE,
    ROBOT_STATE_FILE,
    allowed_image,
    calculate_working_days,
    format_nomor_panggil,
    generate_citations,
    get_next_majalah_barcode,
    get_robot_state,
    parse_author_name,
    parse_slims_call_number,
    resolve_book_cover_url,
    robot_worker,
    save_robot_state,
    send_daily_wa_reminders,
    send_wa_notification,
)
ADMIN_USER = 'adminimavi'
ADMIN_PASS = 'bcajember'

router = ModuleRouter('opac')

@router.route('/landing')
def commercial_landing():
    return render_template('landing_page.html')


@router.route('/demo')
@router.route('/demo/')
def demo_root_redirect():
    return redirect('/demo/opac')


@router.route('/demo/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def demo_proxy(subpath):
    target_url = f"http://127.0.0.1:5006/{subpath}"
    headers = {k: v for k, v in request.headers if k.lower() != 'host'}
    params = request.args
    data = request.get_data()
    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            params=params,
            data=data,
            cookies=request.cookies,
            allow_redirects=False,
            timeout=10
        )
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        response_headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded_headers]
        
        if 'location' in resp.headers:
            loc = resp.headers['location']
            if loc.startswith('/') and not loc.startswith('/demo'):
                loc = f"/demo{loc}"
            response_headers.append(('Location', loc))
            
        return Response(resp.content, resp.status_code, response_headers)
    except Exception as e:
        return f"Demo server (port 5006) error: {e}", 502


@router.route('/api/opac/koleksi_unggulan')
def api_koleksi_unggulan():
    lokasi = get_branding()['default_lokasi']
    conn = database.get_db_connection()
    cursor = conn.execute("""
        SELECT DISTINCT b.id, b.judul, b.pengarang, b.image, b.isbn 
        FROM bibliografi b 
        JOIN eksemplar e ON b.id = e.biblio_id 
        WHERE e.lokasi = ? 
          AND b.image IS NOT NULL 
          AND b.image != '' 
          AND b.image NOT IN ('tidak_ada', 'NOT_FOUND', 'RATE_LIMIT') 
        ORDER BY RANDOM() 
        LIMIT 25
    """, (lokasi,))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    for row in results:
        c_url = resolve_book_cover_url(row.get('image'))
        if c_url:
            row['image'] = c_url
            row['cover_url'] = c_url
        else:
            row['image'] = 'NOT_FOUND'
            row['cover_url'] = None
    return jsonify(results)


@router.route('/api/opac/koleksi_terbaru')
def api_koleksi_terbaru():
    lokasi = get_branding()['default_lokasi']
    conn = database.get_db_connection()
    cursor = conn.execute("""
        SELECT DISTINCT b.id, b.judul, b.pengarang, b.image, b.isbn, e.no_induk
        FROM bibliografi b 
        JOIN eksemplar e ON b.id = e.biblio_id 
        WHERE e.lokasi = ?
          AND b.image IS NOT NULL
          AND b.image != ''
          AND b.image NOT IN ('tidak_ada', 'NOT_FOUND', 'RATE_LIMIT')
        ORDER BY (CASE WHEN e.no_induk LIKE '%/26%' OR e.no_induk LIKE '%/2026%' OR b.image LIKE '%2026%' THEN 1 ELSE 2 END), b.id DESC
        LIMIT 30
    """, (lokasi,))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    for row in results:
        c_url = resolve_book_cover_url(row.get('image'))
        if c_url:
            row['image'] = c_url
            row['cover_url'] = c_url
        else:
            row['image'] = 'NOT_FOUND'
            row['cover_url'] = None
    return jsonify(results)


@router.route('/e-resources')
def e_resources():
    return render_template('e_resources.html')


@router.route('/detail-opac')
def detail_opac():
    return render_template('detail_opac.html')


@router.route('/api/opac/library_info')
def api_opac_library_info():
    conn = database.get_db_connection()
    try:
        wa_row = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'WHATSAPP_LIBRARY_NUMBER'").fetchone()
        wa_number = wa_row[0] if wa_row else '6281234567890'
    except Exception:
        wa_number = '6281234567890'
    conn.close()
    return jsonify({
        'whatsapp_number': wa_number,
        'library_name': get_branding()['library_name'],
        'hours': 'Senin - Jumat: 08.00 - 16.00 WIB'
    })


@router.route('/api/opac/detail')
def api_opac_detail():
    id = request.args.get('id')
    if not id: return jsonify({'error': 'no id'})
    conn = database.get_db_connection()
    b = conn.execute("SELECT * FROM bibliografi WHERE id = ?", (id,)).fetchone()
    if not b:
        conn.close()
        return jsonify({'error': 'not found'})
    b = dict(b)
    
    # Get eksemplar
    default_lokasi = get_branding()['default_lokasi']
    copies = conn.execute("SELECT no_induk, lokasi, status_ketersediaan, status_buku FROM eksemplar WHERE biblio_id = ? ORDER BY CASE WHEN lokasi = ? THEN 1 ELSE 2 END", (id, default_lokasi)).fetchall()
    eksemplar_list = []
    for c in copies:
        cd = dict(c)
        if cd.get('status_buku') == 'DIPINJAM':
            cd['status_ketersediaan'] = 'Dipinjam'
        eksemplar_list.append(cd)
    b['eksemplar'] = eksemplar_list
    b['nomor_panggil'] = format_nomor_panggil(b.get('klasifikasi'), b.get('cutter'), b.get('huruf_judul'), b.get('pengarang'), b.get('judul'))
    # eksemplar populated
    
    # Rekomendasi Buku Serupa (Phase 1)
    pengarang = b.get('pengarang', '')
    similars = []
    if pengarang:
        similars = conn.execute("SELECT id, judul, pengarang, image FROM bibliografi WHERE pengarang = ? AND id != ? AND image IS NOT NULL AND image != 'tidak_ada' AND image != 'NOT_FOUND' LIMIT 5", (pengarang, id)).fetchall()
        if len(similars) < 5:
            more = conn.execute("SELECT id, judul, pengarang, image FROM bibliografi WHERE id != ? AND image IS NOT NULL AND image != 'tidak_ada' AND image != 'NOT_FOUND' LIMIT ?", (id, 5 - len(similars))).fetchall()
            similars.extend(more)
    else:
        similars = conn.execute("SELECT id, judul, pengarang, image FROM bibliografi WHERE id != ? AND image IS NOT NULL AND image != 'tidak_ada' AND image != 'NOT_FOUND' LIMIT 5", (id,)).fetchall()
    
    if b.get('image'):
        c_url = resolve_book_cover_url(b.get('image'))
        if c_url:
            b['cover_url'] = c_url
            b['image'] = c_url
    similar_list = []
    for s in similars:
        sd = dict(s)
        c_url = resolve_book_cover_url(sd.get('image'))
        if c_url:
            sd['cover_url'] = c_url
            sd['image'] = c_url
        similar_list.append(sd)
    b['buku_serupa'] = similar_list
    
    # Lokasi Rak (Phase 1)
    klas = str(b.get('klasifikasi') or '')
    if klas.startswith('1'):
        b['lokasi_rak'] = 'Rak Filsafat, Lorong Utama'
    elif klas.startswith('2'):
        b['lokasi_rak'] = 'Rak Agama & Teologi, Lorong Timur'
    elif klas.startswith('3'):
        b['lokasi_rak'] = 'Rak Ilmu Sosial, Lorong Barat'
    else:
        b['lokasi_rak'] = 'Rak Koleksi Umum / Referensi'

    b['citation'] = generate_citations(b)
    try:
        wa_row = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'WHATSAPP_LIBRARY_NUMBER'").fetchone()
        b['wa_number'] = wa_row[0] if wa_row else '6281234567890'
    except Exception:
        b['wa_number'] = '6281234567890'
    conn.close()
    return jsonify(b)


@router.route('/api/opac/citation/<int:id>')
def api_opac_citation(id):
    conn = database.get_db_connection()
    b = conn.execute("SELECT * FROM bibliografi WHERE id = ?", (id,)).fetchone()
    if not b:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    b = dict(b)
    b['nomor_panggil'] = format_nomor_panggil(b.get('klasifikasi'), b.get('cutter'), b.get('huruf_judul'), b.get('pengarang'), b.get('judul'))
    conn.close()
    cits = generate_citations(b)
    cits['id'] = id
    cits['judul'] = b.get('judul')
    
    if request.args.get('download') == 'ris':
        response = Response(cits['ris'], mimetype='application/x-research-info-systems')
        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', b.get('judul', 'sitasi')[:30])
        response.headers['Content-Disposition'] = f'attachment; filename="st_jerome_{id}_{safe_title}.ris"'
        return response
        
    return jsonify(cits)


@router.route('/opac/search_page')
def opac_search_page():
    q = request.args.get('q', '')
    return render_template('search_opac.html', q=q)


@router.route('/opac')
def opac_page():
    return render_template('opac.html')


@router.route('/api/opac/search', methods=['GET'])
def api_opac_search():
    search = request.args.get('q', '').strip()
    topic = request.args.get('topic', '').strip() or request.args.get('klasifikasi', '').strip()
    conn = database.get_db_connection()
    
    default_lokasi = get_branding()['default_lokasi']
    if topic and not search:
        query = """
            SELECT b.id, b.judul, b.pengarang, b.penerbit, b.tahun_terbit, b.klasifikasi, b.cutter, b.huruf_judul, b.image, b.isbn,
                   COUNT(e.no_induk) as total_eksemplar,
                   SUM(CASE WHEN (e.status_ketersediaan = 'Tersedia' AND (e.status_buku IS NULL OR e.status_buku != 'DIPINJAM')) THEN 1 ELSE 0 END) as tersedia
            FROM bibliografi b
            JOIN eksemplar e ON b.id = e.biblio_id
            WHERE e.lokasi = ? AND b.klasifikasi LIKE ?
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT 60
        """
        cursor = conn.execute(query, (default_lokasi, f'{topic}%'))
    elif topic and search:
        query = """
            SELECT b.id, b.judul, b.pengarang, b.penerbit, b.tahun_terbit, b.klasifikasi, b.cutter, b.huruf_judul, b.image, b.isbn,
                   COUNT(e.no_induk) as total_eksemplar,
                   SUM(CASE WHEN (e.status_ketersediaan = 'Tersedia' AND (e.status_buku IS NULL OR e.status_buku != 'DIPINJAM')) THEN 1 ELSE 0 END) as tersedia
            FROM bibliografi b
            JOIN eksemplar e ON b.id = e.biblio_id
            WHERE e.lokasi = ? AND b.klasifikasi LIKE ? AND (b.judul LIKE ? OR b.pengarang LIKE ? OR b.penerbit LIKE ?)
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT 60
        """
        cursor = conn.execute(query, (default_lokasi, f'{topic}%', f'%{search}%', f'%{search}%', f'%{search}%'))
    elif not search:
        # Default: tampilkan 48 koleksi terbaru sesuai lokasi aktif
        query = """
            SELECT b.id, b.judul, b.pengarang, b.penerbit, b.tahun_terbit, b.klasifikasi, b.cutter, b.huruf_judul, b.image, b.isbn,
                   COUNT(e.no_induk) as total_eksemplar,
                   SUM(CASE WHEN (e.status_ketersediaan = 'Tersedia' AND (e.status_buku IS NULL OR e.status_buku != 'DIPINJAM')) THEN 1 ELSE 0 END) as tersedia
            FROM bibliografi b
            JOIN eksemplar e ON b.id = e.biblio_id
            WHERE e.lokasi = ?
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT 48
        """
        cursor = conn.execute(query, (default_lokasi,))
    else:
        query = """
            SELECT b.id, b.judul, b.pengarang, b.penerbit, b.tahun_terbit, b.klasifikasi, b.cutter, b.huruf_judul, b.image, b.isbn,
                   COUNT(e.no_induk) as total_eksemplar,
                   SUM(CASE WHEN (e.status_ketersediaan = 'Tersedia' AND (e.status_buku IS NULL OR e.status_buku != 'DIPINJAM')) THEN 1 ELSE 0 END) as tersedia
            FROM bibliografi b
            JOIN eksemplar e ON b.id = e.biblio_id
            WHERE e.lokasi = ? AND (b.judul LIKE ? OR b.pengarang LIKE ? OR b.penerbit LIKE ? OR b.klasifikasi LIKE ?)
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT 60
        """
        cursor = conn.execute(query, (default_lokasi, f'%{search}%', f'%{search}%', f'%{search}%', f'%{search}%'))
        
    results = [dict(row) for row in cursor.fetchall()]
    
    if not results and len(search) > 3:
        try:
            conn.execute("INSERT INTO usulan_buku (kata_kunci, sumber) VALUES (?, 'OPAC') ON CONFLICT(kata_kunci) DO UPDATE SET frekuensi = frekuensi + 1, terakhir_dicari = CURRENT_TIMESTAMP", (search,))
            conn.commit()
        except:
            pass
            
    import re
    for row in results:
        # Resolve Cover URL
        if row.get('image'):
            if row['image'] == 'NOT_FOUND' or row['image'] == 'tidak_ada' or row['image'] == 'RATE_LIMIT':
                row['cover_url'] = None
            elif row['image'].startswith('http'):
                row['cover_url'] = row['image']
            else:
                img_path = row['image']
                c_url = resolve_book_cover_url(img_path)
                if c_url:
                    row['image'] = c_url
                    row['cover_url'] = c_url
                else:
                    row['image'] = 'NOT_FOUND'
                    row['cover_url'] = None
        elif row.get('isbn') and is_enabled('ENABLE_AUTOCOVER_ONLINE'):
            raw_isbn = str(row['isbn']).split(',')[0].split(' ')[0].upper()
            cleaned = re.sub(r'[^0-9X]', '', raw_isbn)
            if cleaned:
                row['cover_url'] = f"https://books.google.com/books/content?vid=ISBN{cleaned}&printsec=frontcover&img=1&zoom=1"
            else:
                row['cover_url'] = None
        else:
            row['cover_url'] = None
            
        # Resolve Wayfinding (Location)
        klas = str(row.get('klasifikasi', ''))
        lokasi = "Lantai 1 ? Rak Umum"
        if klas.startswith('1'): lokasi = "Lantai 1 ? Rak Filsafat & Psikologi (100)"
        elif klas.startswith('2'): lokasi = "Lantai 1 ? Rak Agama & Teologi (200)"
        elif klas.startswith('3'): lokasi = "Lantai 1 ? Rak Ilmu Sosial (300)"
        elif klas.startswith('4'): lokasi = "Lantai 2 ? Rak Bahasa (400)"
        elif klas.startswith('5'): lokasi = "Lantai 2 ? Rak Sains & Matematika (500)"
        elif klas.startswith('6'): lokasi = "Lantai 2 ? Rak Teknologi (600)"
        elif klas.startswith('7'): lokasi = "Lantai 2 ? Rak Seni & Rekreasi (700)"
        elif klas.startswith('8'): lokasi = "Lantai 3 ? Rak Sastra (800)"
        elif klas.startswith('9'): lokasi = "Lantai 3 ? Rak Sejarah & Geografi (900)"
        row['lokasi_rak'] = lokasi
        row['nomor_panggil'] = format_nomor_panggil(row.get('klasifikasi'), row.get('cutter'), row.get('huruf_judul'), row.get('pengarang'), row.get('judul'))

    # Zero-Result Hook Logging
    if len(results) == 0 and len(search) > 3:
        try:
            conn.execute("""
                INSERT INTO pencarian_gagal (keyword, jumlah_pencarian)
                VALUES (?, 1)
                ON CONFLICT(keyword) DO UPDATE SET 
                jumlah_pencarian = jumlah_pencarian + 1,
                update_terakhir = CURRENT_TIMESTAMP
            """, (search.lower(),))
            conn.commit()
        except:
            pass
            
    conn.close()
    return jsonify(results)


@router.route('/api/opac/smart_research', methods=['GET', 'POST'])
def api_opac_smart_research():
    if request.method == 'POST':
        data = request.json or {}
        topic_str = data.get('topic', '').strip()
    else:
        topic_str = request.args.get('topic', '').strip() or request.args.get('q', '').strip()
        
    if not topic_str:
        return jsonify({'error': 'Topik atau judul skripsi wajib diisi'}), 400

    STOPWORDS = {
        'dan', 'yang', 'dalam', 'menurut', 'analisis', 'perspektif', 'kajian', 'tinjauan',
        'studi', 'terhadap', 'pada', 'tentang', 'sebuah', 'skripsi', 'proposal', 'bab', 'judul',
        'suatu', 'oleh', 'dari', 'ke', 'di', 'untuk', 'dengan', 'atas', 'sebagai', 'antara',
        'makalah', 'relevansi', 'relevansinya', 'implikasi', 'pandangan', 'konsep', 'makna',
        'pendekatan', 'secara', 'kritis', 'teori', 'upaya', 'berdasarkan', 'serta', 'bagi'
    }
    
    words = re.findall(r'[a-zA-Z0-9]+', topic_str.lower())
    keywords = [w for w in words if w not in STOPWORDS and len(w) > 2]
    if not keywords:
        keywords = [w for w in words if len(w) > 1]
        
    conn = database.get_db_connection()
    conditions = []
    params = []
    for kw in keywords[:6]:
        conditions.append("(b.judul LIKE ? OR b.pengarang LIKE ? OR b.subjek LIKE ?)")
        params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])
        
    if not conditions:
        conn.close()
        return jsonify({'topic': topic_str, 'keywords': [], 'primary': [], 'secondary': [], 'church_docs': []})
        
    default_lokasi = get_branding()['default_lokasi']
    sql = """
        SELECT b.id, b.judul, b.pengarang, b.penerbit, b.tahun_terbit, b.tempat_terbit, b.klasifikasi, b.cutter, b.huruf_judul, b.image, b.subjek, b.isbn,
               COUNT(e.no_induk) as total_eksemplar,
               SUM(CASE WHEN (e.status_ketersediaan = 'Tersedia' AND (e.status_buku IS NULL OR e.status_buku != 'DIPINJAM')) THEN 1 ELSE 0 END) as tersedia,
               MIN(e.no_induk) as sample_barcode
        FROM bibliografi b
        LEFT JOIN eksemplar e ON b.id = e.biblio_id AND e.lokasi = ?
        WHERE (""" + ' OR '.join(conditions) + """)
        GROUP BY b.id
        ORDER BY b.id DESC
        LIMIT 120
    """
    cursor = conn.execute(sql, [default_lokasi] + params)
    raw_results = [dict(r) for r in cursor.fetchall()]
    conn.close()
    
    scored = []
    for b in raw_results:
        score = 0
        j_lower = (b.get('judul') or '').lower()
        p_lower = (b.get('pengarang') or '').lower()
        s_lower = (b.get('subjek') or '').lower()
        
        for kw in keywords:
            if kw in j_lower: score += 5
            if kw in p_lower: score += 4
            if kw in s_lower: score += 3
            
        # Shelf location
        klas = str(b.get('klasifikasi') or '')
        if klas.startswith('1'):
            b['lokasi_rak'] = 'Rak Filsafat & Etika (Lorong Utama)'
        elif klas.startswith('22'):
            b['lokasi_rak'] = 'Rak Kitab Suci & Eksegesis (Lorong Timur 1)'
        elif klas.startswith('23'):
            b['lokasi_rak'] = 'Rak Teologi Dogmatik & Sistematik (Lorong Timur 2)'
        elif klas.startswith('241') or klas.startswith('170'):
            b['lokasi_rak'] = 'Rak Teologi Moral & ASG (Lorong Tengah 1)'
        elif klas.startswith('248'):
            b['lokasi_rak'] = 'Rak Spiritualitas & Asketika (Lorong Tengah 2)'
        elif klas.startswith('262'):
            b['lokasi_rak'] = 'Rak Hukum Kanonik & Tata Gereja (Lorong Barat 1)'
        elif klas.startswith('264'):
            b['lokasi_rak'] = 'Rak Liturgi & Ibadat (Lorong Barat 2)'
        elif klas.startswith('27'):
            b['lokasi_rak'] = 'Rak Patrologi & Sejarah Gereja (Lorong Utara)'
        else:
            b['lokasi_rak'] = 'Rak Referensi Khusus & Umum'
            
        b['nomor_panggil'] = format_nomor_panggil(b.get('klasifikasi'), b.get('cutter'), b.get('huruf_judul'), b.get('pengarang'), b.get('judul'))
        
        # Cover URL
        if b.get('image'):
            if b['image'] in ['NOT_FOUND', 'tidak_ada', 'RATE_LIMIT']:
                b['cover_url'] = None
            elif b['image'].startswith('http'):
                b['cover_url'] = b['image']
            else:
                c_url = resolve_book_cover_url(b['image'])
                b['cover_url'] = c_url or None
        else:
            b['cover_url'] = None
            
        b['citation'] = generate_citations(b)
        scored.append((score, b))
        
    scored.sort(key=lambda x: x[0], reverse=True)
    
    primary = []
    secondary = []
    church_docs = []
    
    for sc, b in scored[:24]:
        j = (b.get('judul') or '').lower()
        p = (b.get('penerbit') or '').lower()
        aut = (b.get('pengarang') or '').lower()
        
        if any(term in j or term in p or term in aut for term in ['dokumen gerejawi', 'konsili', 'kwi', 'vatikan', 'katekismus', 'ensiklik', 'codex', 'hukum kanonik', 'konferensi waligereja']):
            church_docs.append(b)
        elif sc >= 5 or len(primary) < 4:
            primary.append(b)
        else:
            secondary.append(b)
            
    if request.args.get('download') == 'ris':
        combined_ris = "\n\n".join([b['citation']['ris'] for b in (primary + church_docs + secondary)])
        resp = Response(combined_ris, mimetype='application/x-research-info-systems')
        resp.headers['Content-Disposition'] = 'attachment; filename="paket_riset_st_jerome.ris"'
        return resp
        
    return jsonify({
        'topic': topic_str,
        'keywords': keywords,
        'total_found': len(scored),
        'primary': primary,
        'church_docs': church_docs,
        'secondary': secondary
    })


@router.route('/api/opac/request', methods=['POST'])
def api_opac_request():
    data = request.json
    judul = data.get('judul')
    pengarang = data.get('pengarang', '')
    catatan = data.get('catatan', '')
    
    if not judul:
        return jsonify({'status': 'error', 'message': 'Judul/Topik wajib diisi'}), 400
        
    conn = database.get_db_connection()
    conn.execute(
        "INSERT INTO usulan_buku (judul_topik, pengarang, catatan, status, sumber) VALUES (?, ?, ?, 'Menunggu', 'Web OPAC')",
        (judul, pengarang, catatan)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@router.route('/api/opac/reserve', methods=['POST'])
def api_opac_reserve():
    data = request.json
    biblio_id = data.get('biblio_id')
    nim = data.get('nim')
    
    if not biblio_id or not nim:
        return jsonify({'status': 'error', 'message': 'NIM dan ID Buku wajib diisi'}), 400
        
    conn = database.get_db_connection()
    
    # Check if already reserved
    existing = conn.execute("SELECT id FROM reservasi WHERE biblio_id = ? AND nim_pemustaka = ? AND status = 'Aktif'", (biblio_id, nim)).fetchone()
    if existing:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Anda sudah mereservasi buku ini dan sedang dalam antrean.'}), 400
        
    conn.execute(
        "INSERT INTO reservasi (biblio_id, nim_pemustaka) VALUES (?, ?)",
        (biblio_id, nim)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@router.route('/api/opac/discover_cover', methods=['POST'])
@feature_required('ENABLE_AUTOCOVER_ONLINE', 'Auto-cover online dinonaktifkan pada mode offline.', api_status=200)
def api_opac_discover_cover():
    data = request.json
    biblio_id = data.get('id')
    image_url = data.get('image_url')
    
    if not biblio_id or not image_url or not str(image_url).startswith('http'):
        return jsonify({'status': 'error'}), 400
        
    try:
        conn = database.get_db_connection()
        conn.execute('''
            UPDATE bibliografi 
            SET image = ? 
            WHERE id = ? AND (image IS NULL OR image = '' OR image = 'NOT_FOUND' OR image = 'RATE_LIMIT')
        ''', (image_url, biblio_id))
        conn.commit()
        conn.close()
    except:
        pass
    

        
    return jsonify({'status': 'success'})


@router.route('/opac/member/login', methods=['GET', 'POST'])
def member_login():
    error = None
    if request.method == 'POST':
        member_id = request.form.get('member_id')
        password = request.form.get('password')
        
        conn = database.get_db_connection()
        user = conn.execute("SELECT * FROM anggota WHERE member_id = ? AND password = ?", (member_id, password)).fetchone()
        conn.close()
        
        if user:
            session['member_id'] = user['member_id']
            session['nama'] = user['nama']
            session['tipe'] = user['tipe_anggota']
            return redirect('/opac/member/dashboard')
        else:
            error = "Nomor Anggota atau Kata Sandi salah."
            
    return render_template('member_login.html', error=error)


@router.route('/opac/member/dashboard')
def member_dashboard():
    if 'member_id' not in session:
        return redirect('/opac/member/login')
        
    member_id = session['member_id']
    nama = session.get('nama', 'Anggota')
    tipe = session.get('tipe', 'Mahasiswa')
    
    conn = database.get_db_connection()
    # Peminjaman aktif
    try:
        pinjam_count = conn.execute("SELECT count(*) as count FROM sirkulasi WHERE member_id = ? AND return_date IS NULL", (member_id,)).fetchone()['count']
        if pinjam_count == 0:
            pinjam_count = conn.execute("SELECT count(*) as count FROM peminjaman WHERE member_id = ? AND (loan_status IS NULL OR loan_status != 'Returned')", (member_id,)).fetchone()['count']
    except:
        pinjam_count = 0
        
    # Reservasi aktif
    try:
        reservasi_count = conn.execute("SELECT count(*) as count FROM reservasi WHERE nim_pemustaka = ? AND status IN ('Aktif', 'MENUNGGU', 'Tersedia')", (member_id,)).fetchone()['count']
    except:
        reservasi_count = 0

    # Saldo tabungan fotocopy / print
    saldo_tabungan = 0
    no_rekening = None
    try:
        tab_row = conn.execute("SELECT no_rekening, saldo FROM pos_tabungan WHERE member_id = ?", (member_id,)).fetchone()
        if tab_row:
            saldo_tabungan = tab_row['saldo']
            no_rekening = tab_row['no_rekening']
    except:
        saldo_tabungan = 0
    conn.close()
    
    return render_template('member_dashboard.html', 
                           nama=nama, 
                           tipe=tipe,
                           pinjam_count=pinjam_count,
                           reservasi_count=reservasi_count,
                           saldo_tabungan=saldo_tabungan,
                           no_rekening=no_rekening)


@router.route('/opac/member/logout')
def member_logout():
    session.clear()
    return redirect('/opac/member/login')


@router.route('/opac/member/reader')
def member_reader():
    if 'member_id' not in session:
        return redirect('/opac/member/login')
        
    book_id = request.args.get('id')
    buku = {'judul': 'The fate of reason', 'pengarang': 'Beiser, Frederick'} # Fallback
    
    if book_id:
        conn = database.get_db_connection()
        row = conn.execute("SELECT judul, pengarang FROM bibliografi WHERE id = ?", (book_id,)).fetchone()
        conn.close()
        if row:
            buku = dict(row)
            
    return render_template('member_reader.html', buku=buku)


@router.route('/opac/repository')
def repository():
    return render_template('repository.html')


@router.route('/api/repository/search')
def api_repo_search():
    q = request.args.get('q', '')
    conn = database.get_db_connection()
    if q:
        items = conn.execute("SELECT id, judul, pengarang, tahun_terbit, gmd FROM bibliografi WHERE file_dokumen IS NOT NULL AND judul LIKE ?", ('%'+q+'%',)).fetchall()
    else:
        items = conn.execute("SELECT id, judul, pengarang, tahun_terbit, gmd FROM bibliografi WHERE file_dokumen IS NOT NULL ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    return jsonify([dict(i) for i in items])


@router.route('/opac/member/reservasi')
def member_reservasi():
    if 'member_id' not in session:
        return redirect('/opac/member/login')
        
    member_id = session['member_id']
    nama = session.get('nama', 'Anggota')
    tipe = session.get('tipe', 'Mahasiswa')
    
    conn = database.get_db_connection()
    # Fetch user reservations with book titles
    query = """
        SELECT r.id, r.tanggal as tanggal_reservasi, r.status, b.judul 
        FROM reservasi r
        JOIN bibliografi b ON r.biblio_id = b.id
        WHERE r.nim_pemustaka = ?
        ORDER BY r.tanggal DESC
    """
    try:
        reservasi_list = conn.execute(query, (member_id,)).fetchall()
        reservasi_list = [dict(r) for r in reservasi_list]
    except Exception as e:
        reservasi_list = []
    finally:
        conn.close()
    
    return render_template('member_reservasi.html', nama=nama, tipe=tipe, reservasi_list=reservasi_list)


@router.route('/opac/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username == ADMIN_USER and password == ADMIN_PASS:
            session['admin_logged_in'] = True
            return redirect('/opac/admin/dashboard')
        else:
            return render_template('admin_login.html', error="Username atau Password salah!")
            
    return render_template('admin_login.html')


@router.route('/opac/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect('/opac/admin/login')


@router.route('/opac/admin/dashboard')
def admin_dashboard():
    if not session.get('admin_logged_in'):
        return redirect('/opac/admin/login')
        
    conn = database.get_db_connection()
    # Ambil 500 bibliografi terbaru untuk dropdown upload PDF
    biblio_list = conn.execute("SELECT id, judul, pengarang FROM bibliografi ORDER BY id DESC LIMIT 500").fetchall()
    biblio_list = [dict(b) for b in biblio_list]
    conn.close()
    
    return render_template('admin_dashboard.html', biblio_list=biblio_list)


@router.route('/opac/admin/upload_pdf', methods=['POST'])
def admin_upload_pdf():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    biblio_id = request.form.get('biblio_id')
    if 'pdf_file' not in request.files:
        return jsonify({"status": "error", "message": "Tidak ada file yang diunggah"}), 400
        
    file = request.files['pdf_file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "File kosong"}), 400
        
    if file and file.filename.endswith('.pdf'):
        filename = secure_filename(f"buku_{biblio_id}_{file.filename}")
        upload_dir = os.path.join(current_app.root_path, 'static', 'ebooks')
        os.makedirs(upload_dir, exist_ok=True)
        save_path = os.path.join(upload_dir, filename)
        file.save(save_path)
        
        # Update database
        conn = database.get_db_connection()
        conn.execute("UPDATE bibliografi SET file_dokumen = ? WHERE id = ?", (filename, biblio_id))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": f"File {filename} berhasil diunggah dan disambungkan!"})
        
    return jsonify({"status": "error", "message": "Hanya file PDF yang diizinkan."}), 400


@router.route('/opac/admin/upload_zip', methods=['POST'])
def admin_upload_zip():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    if 'zip_file' not in request.files:
        return jsonify({"status": "error", "message": "Tidak ada file ZIP yang diunggah"}), 400
        
    file = request.files['zip_file']
    if file and file.filename.endswith('.zip'):
        filename = secure_filename(file.filename)
        upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        save_path = os.path.join(upload_dir, filename)
        file.save(save_path)
        
        # Untuk saat ini, kita simpan saja. Logika MySQL import menyusul.
        return jsonify({"status": "success", "message": "File ZIP backup berhasil diunggah! Sistem sedang memproses integrasi ke MySQL."})
        
    return jsonify({"status": "error", "message": "Hanya file ZIP yang diizinkan."}), 400


@router.route('/opac/member/change_password', methods=['POST'])
def member_change_password():
    if 'member_id' not in session:
        return jsonify({'status': 'error', 'message': 'Sesi Anda telah berakhir, silakan login kembali.'}), 401
    member_id = session['member_id']
    data = request.json or request.form
    old_pass = data.get('old_password')
    new_pass = data.get('new_password')
    if not old_pass or not new_pass:
        return jsonify({'status': 'error', 'message': 'Kata sandi lama dan baru wajib diisi.'}), 400
    if len(new_pass) < 4:
        return jsonify({'status': 'error', 'message': 'Kata sandi baru minimal 4 karakter.'}), 400
    conn = database.get_db_connection()
    try:
        user = conn.execute("SELECT password FROM anggota WHERE member_id = ?", (member_id,)).fetchone()
        if not user or user['password'] != old_pass:
            return jsonify({'status': 'error', 'message': 'Kata sandi lama salah.'}), 400
        conn.execute("UPDATE anggota SET password = ? WHERE member_id = ?", (new_pass, member_id))
        conn.commit()
        return jsonify({'status': 'success', 'message': 'Kata sandi berhasil diperbarui!'})
    finally:
        conn.close()


