"""Domain module: anggota_laporan."""

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

router = ModuleRouter('anggota_laporan')

@router.route('/dashboard')
@login_required
def dashboard():
    conn = database.get_db_connection()

    # AUTO-CANCEL RESERVASI: Batalkan reservasi yang lebih dari 1x24 jam (Hanya jika status = 'Aktif' atau 'MENUNGGU')
    conn.execute("UPDATE reservasi SET status = 'Batal (Kedaluwarsa)' WHERE status IN ('Aktif', 'MENUNGGU') AND julianday('now', 'localtime') - julianday(tanggal) > 1")
    conn.commit()

    conn.close()
    lokasi = request.args.get('lokasi')
    if not lokasi:
        return redirect(url_for('dashboard', lokasi=get_branding()['default_lokasi']))
        
    conn = database.get_db_connection()
    
    # 1. Input Hari Ini (today's date in YYYY-MM-DD)
    from datetime import date
    today_str = date.today().strftime('%Y-%m-%d')
    
    input_hari_ini = conn.execute('SELECT COUNT(*) FROM buku WHERE tgl_terima = ? AND lokasi = ?', (today_str, lokasi)).fetchone()[0]
    
    # 2. Dibaca di Meja Baca (today)
    dibaca_hari_ini = conn.execute('SELECT COUNT(*) FROM buku_dibaca bd JOIN buku b ON bd.no_induk = b.no_induk WHERE bd.tanggal = ? AND b.lokasi = ?', (today_str, lokasi)).fetchone()[0]
    
    # 3. Antrean Cetak Label
    import os, json
    antrean_count = 0
    antrean_list = []
    if os.path.exists(ANTREAN_FILE):
        with open(ANTREAN_FILE, 'r') as f:
            try:
                antrean_list = json.load(f)
                antrean_count = len(antrean_list)
            except:
                pass
    
    # Get all no_induk in antrean for quick lookup
    antrean_no_induk = [item.get('no_induk') for item in antrean_list]
    
    # 4. Anomali Metadata (missing DDC or missing Author/Title)
    anomali = conn.execute("""
        SELECT COUNT(*) FROM buku 
        WHERE lokasi = ? AND 
        (klasifikasi IS NULL OR klasifikasi = '' OR pengarang IS NULL OR pengarang = '' OR judul IS NULL OR judul = '')
    """, (lokasi,)).fetchone()[0]
    
    # 5. Tabel Rekapan Input Hari Ini
    buku_input_hari_ini = conn.execute("""
        SELECT no_induk, judul, klasifikasi 
        FROM buku 
        WHERE tgl_terima = ? AND lokasi = ?
        ORDER BY id DESC LIMIT 20
    """, (today_str, lokasi)).fetchall()
    
    rekapan_list = []
    for b in buku_input_hari_ini:
        status = 'ANTRE' if b['no_induk'] in antrean_no_induk else 'SIAP'
        rekapan_list.append({
            'no_induk': b['no_induk'],
            'judul': b['judul'],
            'ddc': b['klasifikasi'] if b['klasifikasi'] else '-',
            'status': status
        })
        
    # 6. Buku Paling Sering Dibaca (In-house usage)
    buku_sering_dibaca = conn.execute("""
        SELECT b.judul, b.pengarang, b.klasifikasi, COUNT(bd.id) as freq
        FROM buku_dibaca bd
        JOIN buku b ON bd.no_induk = b.no_induk
        WHERE b.lokasi = ?
        GROUP BY b.no_induk
        ORDER BY freq DESC
        LIMIT 10
    """, (lokasi,)).fetchall()
    
    return render_template('intelijen_evaluasi.html', 
                          lokasi=lokasi, 
                          input_hari_ini=input_hari_ini,
                          dibaca_hari_ini=dibaca_hari_ini,
                          antrean_count=antrean_count,
                          anomali=anomali,
                          rekapan_list=rekapan_list,
                          buku_sering_dibaca=buku_sering_dibaca)


@router.route('/anggota')
@login_required
def daftar_anggota():
    conn = database.get_db_connection()
    search = request.args.get('search', '')
    
    if search:
        query = 'SELECT * FROM anggota WHERE nama LIKE ? OR member_id LIKE ? ORDER BY id DESC'
        anggota_list = conn.execute(query, (f'%{search}%', f'%{search}%')).fetchall()
    else:
        anggota_list = conn.execute('SELECT * FROM anggota ORDER BY id DESC').fetchall()
        
    conn.close()
    return render_template('anggota.html', anggota_list=anggota_list, search=search)


@router.route('/edit_anggota', methods=['POST'])
@login_required
def edit_anggota():
    member_id = request.form.get('member_id')
    nama = request.form.get('nama')
    tipe_anggota = request.form.get('tipe_anggota')
    tanggal_input = request.form.get('tanggal_input')
    masa_berlaku = request.form.get('masa_berlaku')
    telepon = request.form.get('telepon')
    is_tesis = request.form.get('is_tesis', '0')
    
    if member_id and nama and tipe_anggota and masa_berlaku:
        conn = database.get_db_connection()
        conn.execute('''
            UPDATE anggota 
            SET nama = ?, tipe_anggota = ?, tanggal_input = ?, masa_berlaku = ?, telepon = ?, is_tesis = ?
            WHERE member_id = ?
        ''', (nama, tipe_anggota, tanggal_input, masa_berlaku, telepon, int(is_tesis), member_id))
        conn.commit()
        conn.close()
        
    return redirect(url_for('daftar_anggota'))


@router.route('/tambah_anggota', methods=['POST'])
@login_required
def tambah_anggota():
    member_id = request.form.get('member_id')
    nama = request.form.get('nama')
    tipe_anggota = request.form.get('tipe_anggota')
    tanggal_input = request.form.get('tanggal_input')
    masa_berlaku = request.form.get('masa_berlaku')
    telepon = request.form.get('telepon')
    is_tesis = request.form.get('is_tesis', '0')
    
    if member_id and nama:
        conn = database.get_db_connection()
        try:
            conn.execute("""
                INSERT INTO anggota (member_id, nama, tipe_anggota, tanggal_input, masa_berlaku, telepon, is_tesis)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (member_id, nama, tipe_anggota, tanggal_input, masa_berlaku, telepon, int(is_tesis)))
            conn.commit()
        except Exception as e:
            print(e)
            pass
        conn.close()
    return redirect(url_for('daftar_anggota'))


@router.route('/import_anggota', methods=['POST'])
@login_required
def import_anggota():
    if 'file' not in request.files:
        return redirect(url_for('daftar_anggota'))
        
    file = request.files['file']
    if file.filename == '':
        return redirect(url_for('daftar_anggota'))
        
    if file:
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        reader = csv.reader(stream, delimiter=',', quotechar='"')
        
        conn = database.get_db_connection()
        added = 0
        updated = 0
        
        for row in reader:
            if len(row) < 10:
                continue
            
            member_id = row[0]
            nama = row[1]
            tipe = row[3]
            email = row[4]
            alamat = row[5]
            institusi = row[7]
            telepon = row[11] if len(row) > 11 else ''
            masa_berlaku = row[15] if len(row) > 15 else ''
            
            existing = conn.execute('SELECT member_id FROM anggota WHERE member_id = ?', (member_id,)).fetchone()
            
            if existing:
                conn.execute('''
                    UPDATE anggota 
                    SET nama=?, tipe_anggota=?, institusi=?, email=?, telepon=?, alamat=?, masa_berlaku=?
                    WHERE member_id=?
                ''', (nama, tipe, institusi, email, telepon, alamat, masa_berlaku, member_id))
                updated += 1
            else:
                conn.execute('''
                    INSERT INTO anggota (member_id, nama, tipe_anggota, institusi, email, telepon, alamat, masa_berlaku)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (member_id, nama, tipe, institusi, email, telepon, alamat, masa_berlaku))
                added += 1
                
        conn.commit()
        conn.close()
        
    return redirect(url_for('daftar_anggota'))


@router.route('/cetak_kartu', methods=['POST'])
@login_required
def cetak_kartu():
    member_ids = request.form.getlist('member_ids')
    if not member_ids:
        return redirect(url_for('daftar_anggota'))
        
    conn = database.get_db_connection()
    # Build query
    placeholders = ','.join('?' for _ in member_ids)
    query = f"SELECT * FROM anggota WHERE member_id IN ({placeholders})"
    anggota_data = conn.execute(query, member_ids).fetchall()
    conn.close()
    
    # Sort data to match the exact order of selected member_ids from the frontend
    anggota_dict = {row['member_id']: row for row in anggota_data}
    anggota_data = [anggota_dict[m_id] for m_id in member_ids if m_id in anggota_dict]
    
    import template_kartu
    
    # Convert Row to dict
    anggota_dicts = []
    for a in anggota_data:
        anggota_dicts.append({
            'member_id': a['member_id'],
            'nama': a['nama'],
            'tipe_anggota': a['tipe_anggota'],
            'masa_berlaku': a['masa_berlaku']
        })
        
    template_kartu.generate_cards_pdf(anggota_dicts, 'kartu_output.pdf')
    return send_file('kartu_output.pdf', as_attachment=True)


@router.route('/analisis_lanjutan')
@login_required
def analisis_lanjutan():
    lokasi = request.args.get('lokasi')
    if not lokasi:
        return redirect(url_for('analisis_lanjutan', lokasi=get_branding()['default_lokasi']))
        
    conn = database.get_db_connection()
    
    # Mencegah error jika tabel belum ada
    conn.execute('CREATE TABLE IF NOT EXISTS peminjaman (id INTEGER PRIMARY KEY AUTOINCREMENT, no_induk TEXT, judul TEXT, tgl_pinjam TEXT, tgl_kembali TEXT, status TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS buku_dibaca (id INTEGER PRIMARY KEY AUTOINCREMENT, no_induk TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    conn.commit()
    

    # Bypass redirect


    # 1. Turnover Rate (existing)
    turnover_query = """
        SELECT 
            CASE 
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '0' THEN '000 - Komputer & Informasi'
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '1' THEN '100 - Filsafat & Psikologi'
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '2' THEN '200 - Agama & Teologi'
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '3' THEN '300 - Ilmu Sosial'
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '4' THEN '400 - Bahasa'
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '5' THEN '500 - Sains & Matematika'
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '6' THEN '600 - Teknologi'
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '7' THEN '700 - Kesenian & Rekreasi'
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '8' THEN '800 - Sastra'
                WHEN SUBSTR(b.klasifikasi, 1, 1) = '9' THEN '900 - Sejarah & Geografi'
                ELSE 'Lainnya'
            END as ddc_group,
            COUNT(DISTINCT b.no_induk) as total_eksemplar,
            COUNT(DISTINCT p.id) as total_pinjam
        FROM buku b
        LEFT JOIN peminjaman p ON b.no_induk = p.no_induk
        WHERE b.lokasi = ?
        GROUP BY ddc_group
    """
    turnover_data = conn.execute(turnover_query, (lokasi,)).fetchall()

    # 2. Smart Procurement: High Usage, Single Copy
    smart_procurement = conn.execute("""
        WITH Copies AS (
            SELECT judul, pengarang, klasifikasi, COUNT(no_induk) as total_eksemplar
            FROM buku
            WHERE lokasi = ?
            GROUP BY judul, pengarang
            HAVING total_eksemplar = 1
        ),
        InHouseReads AS (
            SELECT b.judul, COUNT(bd.id) as read_count
            FROM buku_dibaca bd
            JOIN buku b ON bd.no_induk = b.no_induk
            WHERE b.lokasi = ?
            GROUP BY b.judul
        ),
        ExternalLoans AS (
            SELECT p.judul as judul, COUNT(p.id) as loan_count
            FROM peminjaman p
            GROUP BY p.judul
        )
        SELECT 
            c.judul, 
            c.pengarang,
            c.klasifikasi,
            c.total_eksemplar,
            COALESCE(i.read_count, 0) as read_count,
            COALESCE(e.loan_count, 0) as loan_count,
            (COALESCE(i.read_count, 0) + COALESCE(e.loan_count, 0)) as total_usage
        FROM Copies c
        LEFT JOIN InHouseReads i ON c.judul = i.judul
        LEFT JOIN ExternalLoans e ON c.judul = e.judul
        WHERE total_usage > 2
        ORDER BY total_usage DESC
        LIMIT 10
    """, (lokasi, lokasi)).fetchall()

    # 3. Gap Subjek Prioritas
    gap_subjek = conn.execute("""
        SELECT 
            CASE 
                WHEN SUBSTR(klasifikasi, 1, 1) = '0' THEN '000 - Komputer & Informasi'
                WHEN SUBSTR(klasifikasi, 1, 1) = '1' THEN '100 - Filsafat & Psikologi'
                WHEN SUBSTR(klasifikasi, 1, 1) = '2' THEN '200 - Agama & Teologi'
                WHEN SUBSTR(klasifikasi, 1, 1) = '3' THEN '300 - Ilmu Sosial'
                WHEN SUBSTR(klasifikasi, 1, 1) = '4' THEN '400 - Bahasa'
                WHEN SUBSTR(klasifikasi, 1, 1) = '5' THEN '500 - Sains & Matematika'
                WHEN SUBSTR(klasifikasi, 1, 1) = '6' THEN '600 - Teknologi'
                WHEN SUBSTR(klasifikasi, 1, 1) = '7' THEN '700 - Kesenian & Rekreasi'
                WHEN SUBSTR(klasifikasi, 1, 1) = '8' THEN '800 - Sastra'
                WHEN SUBSTR(klasifikasi, 1, 1) = '9' THEN '900 - Sejarah & Geografi'
                ELSE 'Lainnya'
            END as ddc_group,
            MAX(tgl_terima) as update_terakhir,
            COUNT(no_induk) as jumlah_koleksi
        FROM buku
        WHERE lokasi = ? AND tgl_terima IS NOT NULL AND tgl_terima != ''
        GROUP BY ddc_group
        ORDER BY update_terakhir ASC
        LIMIT 5
    """, (lokasi,)).fetchall()

    # 4. Akreditasi: Judul & Eksemplar
    akreditasi_judul_eks = conn.execute('SELECT COUNT(DISTINCT judul) as j, COUNT(no_induk) as e FROM buku WHERE lokasi = ?', (lokasi,)).fetchone()
    
    # 5. Akreditasi: Inti vs Umum
    akreditasi_inti_umum = conn.execute("""
        SELECT 
            SUM(CASE WHEN SUBSTR(klasifikasi, 1, 1) IN ('1', '2') THEN 1 ELSE 0 END) as inti,
            SUM(CASE WHEN SUBSTR(klasifikasi, 1, 1) NOT IN ('1', '2') THEN 1 ELSE 0 END) as umum,
            COUNT(*) as total
        FROM buku WHERE lokasi = ?
    """, (lokasi,)).fetchone()

    # 6. Akreditasi: Pertambahan / Tahun
    akreditasi_pertambahan = conn.execute("""
        SELECT SUBSTR(tgl_terima, 1, 4) as tahun, COUNT(no_induk) as jumlah 
        FROM buku WHERE lokasi = ? AND tgl_terima IS NOT NULL AND tgl_terima != ''
        GROUP BY tahun ORDER BY tahun DESC LIMIT 3
    """, (lokasi,)).fetchall()

    
    # 1.5. Dead Stock (Koleksi Dorman)
    dead_stock = conn.execute("""
        SELECT b.judul, b.pengarang, b.klasifikasi
        FROM buku b
        LEFT JOIN buku_dibaca bd ON b.no_induk = bd.no_induk
        LEFT JOIN peminjaman p ON b.no_induk = p.no_induk
        WHERE b.lokasi = ? AND bd.id IS NULL AND p.id IS NULL
        LIMIT 10
    """, (lokasi,)).fetchall()

    # 1.6. In-House vs External
    total_in_house = conn.execute("SELECT COUNT(*) FROM buku_dibaca bd JOIN buku b ON bd.no_induk = b.no_induk WHERE b.lokasi = ?", (lokasi,)).fetchone()[0]
    total_external = conn.execute("SELECT COUNT(*) FROM peminjaman p JOIN buku b ON p.no_induk = b.no_induk WHERE b.lokasi = ?", (lokasi,)).fetchone()[0]

    return render_template('analisis_lanjutan.html', 
                          dead_stock=dead_stock,
                          total_in_house=total_in_house,
                          total_external=total_external,
                          
                          lokasi=lokasi, 
                          turnover_data=turnover_data,
                          smart_procurement=smart_procurement,
                          gap_subjek=gap_subjek,
                          akr_je=akreditasi_judul_eks,
                          akr_iu=akreditasi_inti_umum,
                          akr_tambah=akreditasi_pertambahan)


@router.route('/export_akreditasi')
@login_required
def export_akreditasi():
    lokasi = request.args.get('lokasi') or get_branding()['default_lokasi']
    conn = database.get_db_connection()
    
    # Gathering data
    akreditasi_judul_eks = conn.execute('SELECT COUNT(DISTINCT judul) as j, COUNT(no_induk) as e FROM buku WHERE lokasi = ?', (lokasi,)).fetchone()
    akreditasi_inti_umum = conn.execute("""
        SELECT 
            SUM(CASE WHEN SUBSTR(klasifikasi, 1, 1) IN ('1', '2') THEN 1 ELSE 0 END) as inti,
            SUM(CASE WHEN SUBSTR(klasifikasi, 1, 1) NOT IN ('1', '2') THEN 1 ELSE 0 END) as umum,
            COUNT(*) as total
        FROM buku WHERE lokasi = ?
    """, (lokasi,)).fetchone()
    
    # Create an excel file using pandas
    import pandas as pd
    from io import BytesIO
    data = [
        {'Indikator': 'Jumlah Judul', 'Nilai': akreditasi_judul_eks['j']},
        {'Indikator': 'Jumlah Eksemplar', 'Nilai': akreditasi_judul_eks['e']},
        {'Indikator': 'Koleksi Inti (Teologi/Filsafat)', 'Nilai': akreditasi_inti_umum['inti']},
        {'Indikator': 'Koleksi Umum', 'Nilai': akreditasi_inti_umum['umum']},
        {'Indikator': 'Total Koleksi', 'Nilai': akreditasi_inti_umum['total']}
    ]
    
    df = pd.DataFrame(data)
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Borang Akreditasi')
    
    output.seek(0)
    
    return send_file(
        output, 
        as_attachment=True, 
        download_name=f'Borang_Akreditasi_{lokasi}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@router.route('/anomali')
@login_required
def anomali():
    lokasi = request.args.get('lokasi') or get_branding()['default_lokasi']
    conn = database.get_db_connection()
    
    # 1. Metadata Tidak Lengkap
    metadata_cacat = conn.execute("""
        SELECT no_induk, judul, pengarang, klasifikasi 
        FROM buku 
        WHERE lokasi = ? AND 
        (klasifikasi IS NULL OR klasifikasi = '' OR pengarang IS NULL OR pengarang = '' OR judul IS NULL OR judul = '')
        LIMIT 50
    """, (lokasi,)).fetchall()
    
    # 2. Inkonsistensi Klasifikasi (Judul sama persis, tapi beda klasifikasi)
    page = int(request.args.get('page', 1))
    per_page = 5
    offset = (page - 1) * per_page
    
    # Get total anomalies for pagination
    total_anomalies_query = conn.execute("""
        SELECT COUNT(DISTINCT judul) FROM buku 
        WHERE lokasi = ? AND klasifikasi != ''
        GROUP BY judul
        HAVING COUNT(DISTINCT klasifikasi) > 1
    """, (lokasi,)).fetchall()
    total_pages = (len(total_anomalies_query) // per_page) + (1 if len(total_anomalies_query) % per_page > 0 else 0)
    
    titles_with_inconsistency = conn.execute("""
        SELECT judul FROM buku 
        WHERE lokasi = ? AND klasifikasi != ''
        GROUP BY judul
        HAVING COUNT(DISTINCT klasifikasi) > 1
        LIMIT ? OFFSET ?
    """, (lokasi, per_page, offset)).fetchall()
    
    inkonsistensi_dict = {}
    for t in titles_with_inconsistency:
        judul = t['judul']
        exemplars = conn.execute("""
            SELECT no_induk, judul, klasifikasi, pengarang, penerbit, tahun_terbit,
                   TRIM(klasifikasi || ' ' || COALESCE(cutter, '') || ' ' || COALESCE(huruf_judul, '')) as no_panggil
            FROM buku
            WHERE judul = ? AND lokasi = ?
            ORDER BY no_induk
        """, (judul, lokasi)).fetchall()
        inkonsistensi_dict[judul] = [dict(e) for e in exemplars]

    return render_template('anomali.html', lokasi=lokasi, metadata_cacat=metadata_cacat, inkonsistensi_dict=inkonsistensi_dict, page=page, total_pages=total_pages)


@router.route('/analitik_kunjungan')
@login_required
def analitik_kunjungan():
    range_preset = request.args.get('range', '')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    import datetime
    today = datetime.date.today()
    today_str = today.strftime('%Y-%m-%d')

    BULAN_INDO = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                  "Juli", "Agustus", "September", "Oktober", "November", "Desember"]

    today_display = f"{today.day} {BULAN_INDO[today.month]} {today.year}"
    current_month_name = BULAN_INDO[today.month]

    first_this = today.replace(day=1)
    last_prev = first_this - datetime.timedelta(days=1)
    prev_month_name = BULAN_INDO[last_prev.month]

    if range_preset == 'today':
        start_date = today_str
        end_date = today_str
    elif range_preset == '7days':
        start_date = (today - datetime.timedelta(days=6)).strftime('%Y-%m-%d')
        end_date = today_str
    elif range_preset == 'this_month':
        start_date = today.strftime('%Y-%m-01')
        end_date = today_str
    elif range_preset == 'last_month':
        start_date = last_prev.strftime('%Y-%m-01')
        end_date = last_prev.strftime('%Y-%m-%d')
    elif range_preset == 'all':
        start_date = '2025-10-01'
        end_date = today_str
    elif not start_date and not end_date:
        range_preset = 'this_month'
        start_date = today.strftime('%Y-%m-01')
        end_date = today_str

    conn = database.get_db_connection()
    try:
        # Global KPI
        kpi_total = conn.execute("SELECT COUNT(id) FROM sjla_visitor_logs").fetchone()[0]
        kpi_today = conn.execute("SELECT COUNT(id) FROM sjla_visitor_logs WHERE date(waktu_kunjungan) = ?", (today_str,)).fetchone()[0]
        seven_days_ago = (today - datetime.timedelta(days=6)).strftime('%Y-%m-%d')
        kpi_week = conn.execute("SELECT COUNT(id) FROM sjla_visitor_logs WHERE date(waktu_kunjungan) >= ? AND date(waktu_kunjungan) <= ?", (seven_days_ago, today_str)).fetchone()[0]
        kpi_month = conn.execute("SELECT COUNT(id) FROM sjla_visitor_logs WHERE strftime('%Y-%m', waktu_kunjungan) = ?", (today.strftime('%Y-%m'),)).fetchone()[0]

        # Filtered Metrics
        filter_row = conn.execute("""
            SELECT 
                COUNT(id) as total,
                SUM(CASE WHEN tipe_pengunjung = 'Member' THEN 1 ELSE 0 END) as member_count,
                SUM(CASE WHEN tipe_pengunjung = 'Non-Member' THEN 1 ELSE 0 END) as non_member_count
            FROM sjla_visitor_logs 
            WHERE date(waktu_kunjungan) >= ? AND date(waktu_kunjungan) <= ?
        """, (start_date, end_date)).fetchone()

        kunjungan_hari_ini = filter_row['total'] if filter_row and filter_row['total'] else 0
        member_hari_ini = filter_row['member_count'] if filter_row and filter_row['member_count'] else 0
        non_member_hari_ini = filter_row['non_member_count'] if filter_row and filter_row['non_member_count'] else 0

        # Sebaran Fakultas (Subquery agar tidak duplikat)
        fakultas_rows = conn.execute("""
            SELECT kategori_fakultas as fakultas, COUNT(id) as jumlah
            FROM (
                SELECT id,
                    CASE 
                        WHEN lower(fakultas) LIKE '%farmasi%' THEN 'Farmasi'
                        WHEN lower(fakultas) LIKE '%kedokteran%' THEN 'Kedokteran'
                        WHEN lower(fakultas) LIKE '%keperawatan%' THEN 'Keperawatan'
                        WHEN lower(fakultas) LIKE '%psikologi%' THEN 'Psikologi'
                        WHEN lower(peran_jabatan) LIKE '%frater%' OR lower(asal_instansi) LIKE '%seminari%' THEN 'Filsafat & Teologi'
                        ELSE 'Umum / Lainnya'
                    END as kategori_fakultas
                FROM sjla_visitor_logs
                WHERE date(waktu_kunjungan) >= ? AND date(waktu_kunjungan) <= ?
            )
            GROUP BY kategori_fakultas
            ORDER BY jumlah DESC
        """, (start_date, end_date)).fetchall()
        fakultas_hari_ini = [dict(row) for row in fakultas_rows]

        # Demografi Instansi (Subquery agar UKWMS, Seminari, IMAVI tidak terpecah/duplikat)
        demografi_rows = conn.execute("""
            SELECT kategori_instansi as asal_instansi, COUNT(id) as jumlah 
            FROM (
                SELECT id,
                    CASE 
                        WHEN lower(asal_instansi) LIKE '%ukwms%' OR lower(asal_instansi) LIKE '%widya mandala%' THEN 'UKWMS'
                        WHEN lower(asal_instansi) LIKE '%seminari%' THEN 'Seminari Tinggi Providentia Dei'
                        WHEN lower(asal_instansi) LIKE '%imavi%' THEN 'IMAVI'
                        ELSE 'Umum / Mitra Luar'
                    END as kategori_instansi
                FROM sjla_visitor_logs 
                WHERE date(waktu_kunjungan) >= ? AND date(waktu_kunjungan) <= ?
            )
            GROUP BY kategori_instansi
            ORDER BY jumlah DESC
        """, (start_date, end_date)).fetchall()
        demografi = [dict(row) for row in demografi_rows]

        # TOP 10 ALL-TIME (SEJAK AWAL PERPUSTAKAAN BUKA - PERMINTAAN USER)
        top_alltime_raw = conn.execute("""
            SELECT 
                identitas, 
                tipe_pengunjung, 
                asal_instansi, 
                peran_jabatan, 
                fakultas, 
                COUNT(id) as total_kunjungan,
                MIN(date(waktu_kunjungan)) as sejak,
                MAX(date(waktu_kunjungan)) as sampai
            FROM sjla_visitor_logs
            GROUP BY lower(trim(identitas))
            ORDER BY total_kunjungan DESC
            LIMIT 10
        """).fetchall()
        default_inst = get_branding()['default_lokasi']
        top_visitors_alltime = []
        for r in top_alltime_raw:
            d = dict(r)
            d['identitas_display'] = d['identitas'].strip().title()
            d['asal_instansi_display'] = d['asal_instansi'] if d['asal_instansi'] else default_inst
            top_visitors_alltime.append(d)

        # TOP 10 Sesuai Rentang Filter
        top_filtered_raw = conn.execute("""
            SELECT 
                identitas, 
                tipe_pengunjung, 
                asal_instansi, 
                peran_jabatan, 
                fakultas, 
                COUNT(id) as total_kunjungan
            FROM sjla_visitor_logs 
            WHERE date(waktu_kunjungan) >= ? AND date(waktu_kunjungan) <= ?
            GROUP BY lower(trim(identitas))
            ORDER BY total_kunjungan DESC
            LIMIT 10
        """, (start_date, end_date)).fetchall()
        top_visitors_filtered = []
        for r in top_filtered_raw:
            d = dict(r)
            d['identitas_display'] = d['identitas'].strip().title()
            d['asal_instansi_display'] = d['asal_instansi'] if d['asal_instansi'] else default_inst
            top_visitors_filtered.append(d)

        # Log Kunjungan (Limit 150)
        log_rows = conn.execute("""
            SELECT waktu_kunjungan, identitas, tipe_pengunjung, asal_instansi, peran_jabatan, fakultas
            FROM sjla_visitor_logs
            WHERE date(waktu_kunjungan) >= ? AND date(waktu_kunjungan) <= ?
            ORDER BY waktu_kunjungan DESC
            LIMIT 150
        """, (start_date, end_date)).fetchall()
        log_hari_ini = [dict(row) for row in log_rows]

        # 1. Multi-Period Trend Datasets (7 Hari, 30 Hari, Bulan Ini, Sepanjang Waktu)
        t7_rows = conn.execute("""
            SELECT date(waktu_kunjungan) as tgl, COUNT(id) as total
            FROM sjla_visitor_logs
            WHERE date(waktu_kunjungan) >= date(?, '-6 days') AND date(waktu_kunjungan) <= ?
            GROUP BY tgl ORDER BY tgl ASC
        """, (today_str, today_str)).fetchall()
        trend_7d = [dict(r) for r in t7_rows]

        t30_rows = conn.execute("""
            SELECT date(waktu_kunjungan) as tgl, COUNT(id) as total
            FROM sjla_visitor_logs
            WHERE date(waktu_kunjungan) >= date(?, '-29 days') AND date(waktu_kunjungan) <= ?
            GROUP BY tgl ORDER BY tgl ASC
        """, (today_str, today_str)).fetchall()
        trend_30d = [dict(r) for r in t30_rows]

        tmonth_rows = conn.execute("""
            SELECT date(waktu_kunjungan) as tgl, COUNT(id) as total
            FROM sjla_visitor_logs
            WHERE strftime('%Y-%m', waktu_kunjungan) = strftime('%Y-%m', ?)
            GROUP BY tgl ORDER BY tgl ASC
        """, (today_str,)).fetchall()
        trend_month = [dict(r) for r in tmonth_rows]

        tall_rows = conn.execute("""
            SELECT date(waktu_kunjungan) as tgl, COUNT(id) as total
            FROM sjla_visitor_logs
            GROUP BY tgl ORDER BY tgl ASC
        """).fetchall()
        trend_all = [dict(r) for r in tall_rows]

        trend_datasets = {
            '7d': trend_7d,
            '30d': trend_30d,
            'month': trend_month,
            'all': trend_all
        }
        chart_trend = trend_month

        first_tgl_row = conn.execute("SELECT MIN(date(waktu_kunjungan)) FROM sjla_visitor_logs").fetchone()
        first_record_date = first_tgl_row[0] if first_tgl_row and first_tgl_row[0] else '2025-10-06'
        try:
            fd = datetime.datetime.strptime(first_record_date, '%Y-%m-%d').date()
            first_record_display = f"{fd.day} {BULAN_INDO[fd.month]} {fd.year}"
        except Exception:
            first_record_display = first_record_date

        # 2. Hourly Peak Hours (07:00 - 16:00)
        hourly_rows = conn.execute("""
            SELECT strftime('%H', waktu_kunjungan) as jam, COUNT(id) as total
            FROM sjla_visitor_logs
            WHERE strftime('%H', waktu_kunjungan) BETWEEN '07' AND '16'
            GROUP BY jam
            ORDER BY jam ASC
        """).fetchall()
        chart_hourly = [dict(r) for r in hourly_rows]

        # 3. Day of Week Pattern (Senin - Jumat)
        dow_rows = conn.execute("""
            SELECT strftime('%w', waktu_kunjungan) as dow, COUNT(id) as total
            FROM sjla_visitor_logs
            WHERE strftime('%w', waktu_kunjungan) IN ('1','2','3','4','5')
            GROUP BY dow
            ORDER BY dow ASC
        """).fetchall()
        chart_dow = [dict(r) for r in dow_rows]

    finally:
        conn.close()

    return render_template('analitik_kunjungan.html', 
                          kpi_total=kpi_total,
                          kpi_today=kpi_today,
                          kpi_week=kpi_week,
                          kpi_month=kpi_month,
                          today_display=today_display,
                          current_month_name=current_month_name,
                          prev_month_name=prev_month_name,
                          first_record_display=first_record_display,
                          trend_datasets=trend_datasets,
                          chart_trend=chart_trend,
                          chart_hourly=chart_hourly,
                          chart_dow=chart_dow,
                          kunjungan_hari_ini=kunjungan_hari_ini, 
                          member_hari_ini=member_hari_ini,
                          non_member_hari_ini=non_member_hari_ini,
                          fakultas_hari_ini=fakultas_hari_ini,
                          demografi=demografi, 
                          top_visitors_alltime=top_visitors_alltime,
                          top_visitors_filtered=top_visitors_filtered,
                          log_hari_ini=log_hari_ini,
                          start_date=start_date,
                          end_date=end_date,
                          range_preset=range_preset)


@router.route('/export_kunjungan', methods=['POST'])
@login_required
def export_kunjungan():
    start_date = request.form.get('start_date')
    end_date = request.form.get('end_date')
    
    if not start_date or not end_date:
        flash('Pilih rentang tanggal terlebih dahulu.', 'danger')
        return redirect(url_for('analitik_kunjungan'))
        
    conn = database.get_db_connection()
    query = """
        SELECT 
            waktu_kunjungan as 'Waktu Kunjungan',
            identitas as 'Nama/ID Pengunjung',
            tipe_pengunjung as 'Status Member',
            asal_instansi as 'Instansi',
            peran_jabatan as 'Peran/Jabatan',
            fakultas as 'Fakultas'
        FROM sjla_visitor_logs
        WHERE date(waktu_kunjungan) >= ? AND date(waktu_kunjungan) <= ?
        ORDER BY waktu_kunjungan DESC
    """
    df = pd.read_sql_query(query, conn, params=(start_date, end_date))
    conn.close()
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Laporan Kunjungan')
    
    output.seek(0)
    
    return send_file(
        output, 
        as_attachment=True, 
        download_name=f'Laporan_Kunjungan_{start_date}_sd_{end_date}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@router.route('/usulan_buku')
@login_required
def usulan_buku():
    import re
    from datetime import datetime

    def normalize_key(s):
        if not s:
            return ""
        cleaned = re.sub(r'[^a-zA-Z0-9]', ' ', str(s).lower())
        return ' '.join(cleaned.split())

    def format_wib(dt_str):
        if not dt_str:
            return "-"
        try:
            dt = datetime.strptime(str(dt_str)[:19], "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%d-%m-%Y %H:%M WIB")
        except:
            return str(dt_str)

    conn = database.get_db_connection()
    try:
        usulan_rows = conn.execute("SELECT * FROM usulan_buku ORDER BY tanggal DESC").fetchall()
        pg_rows = conn.execute("SELECT * FROM pencarian_gagal ORDER BY update_terakhir DESC").fetchall()
    except Exception as e:
        usulan_rows = []
        pg_rows = []
    finally:
        conn.close()

    catalog = {}

    for u in usulan_rows:
        raw_title = (u['judul_topik'] or '').strip()
        if not raw_title or len(raw_title) < 2:
            continue
        key = normalize_key(raw_title)
        sumber_val = u['sumber'] or ('WhatsApp' if 'WhatsApp' in (u['catatan'] or '') else 'Web OPAC')
        
        catatan_raw = u['catatan'] or ''
        pengusul = 'Pemustaka'
        catatan_clean = catatan_raw
        if 'Diusulkan via WhatsApp oleh' in catatan_raw:
            idx = catatan_raw.find('Diusulkan via WhatsApp oleh')
            catatan_clean = catatan_raw[:idx].rstrip(' -(')
            after = catatan_raw[idx + len('Diusulkan via WhatsApp oleh'):].strip()
            pengusul = after.rstrip(')').strip()
        elif sumber_val == 'Web OPAC':
            pengusul = 'Pemustaka Web OPAC'
            
        if key not in catalog:
            catalog[key] = {
                'id': u['id'],
                'judul': raw_title,
                'pengarang': u['pengarang'] or '-',
                'catatan': catatan_clean,
                'pengusul': pengusul,
                'has_wa': (sumber_val == 'WhatsApp'),
                'has_opac': (sumber_val == 'Web OPAC'),
                'wa_count': 1 if sumber_val == 'WhatsApp' else 0,
                'opac_count': 1 if sumber_val == 'Web OPAC' else 0,
                'total_frekuensi': 1,
                'terakhir_dicari': u['tanggal'],
                'status': u['status'] or 'Menunggu',
                'is_usulan': True
            }
        else:
            item = catalog[key]
            if sumber_val == 'WhatsApp':
                item['has_wa'] = True
                item['wa_count'] += 1
            else:
                item['has_opac'] = True
                item['opac_count'] += 1
            item['total_frekuensi'] += 1
            if u['pengarang'] and (not item['pengarang'] or item['pengarang'] == '-'):
                item['pengarang'] = u['pengarang']
            if catatan_clean and not item['catatan']:
                item['catatan'] = catatan_clean
            if pengusul != 'Pemustaka' and item['pengusul'] in ['Pemustaka', 'Pemustaka Web OPAC']:
                item['pengusul'] = pengusul
            if str(u['tanggal']) > str(item['terakhir_dicari']):
                item['terakhir_dicari'] = u['tanggal']

    for p in pg_rows:
        raw_kw = (p['keyword'] or '').strip()
        if not raw_kw or len(raw_kw) < 2:
            continue
        key = normalize_key(raw_kw)
        freq = int(p['jumlah_pencarian'] or 1)
        sumber_val = p['sumber'] or 'Web OPAC'
        
        if key not in catalog:
            catalog[key] = {
                'id': f"pg_{p['id']}",
                'judul': raw_kw.title(),
                'pengarang': '-',
                'catatan': f"Pencarian katalog gagal sebanyak {freq}x",
                'pengusul': 'Pencarian Web OPAC',
                'has_wa': (sumber_val == 'WhatsApp'),
                'has_opac': True,
                'wa_count': freq if sumber_val == 'WhatsApp' else 0,
                'opac_count': freq if sumber_val != 'WhatsApp' else 0,
                'total_frekuensi': freq,
                'terakhir_dicari': p['update_terakhir'],
                'status': 'Menunggu',
                'is_usulan': False
            }
        else:
            item = catalog[key]
            item['has_opac'] = True
            item['opac_count'] += freq
            item['total_frekuensi'] += freq
            if str(p['update_terakhir']) > str(item['terakhir_dicari']):
                item['terakhir_dicari'] = p['update_terakhir']

    items_list = []
    total_keduanya = 0
    total_wa = 0
    total_opac = 0
    total_permintaan = 0

    for key, item in catalog.items():
        if item['has_wa'] and item['has_opac']:
            item['sumber'] = 'Keduanya'
            total_keduanya += 1
        elif item['has_wa']:
            item['sumber'] = 'WhatsApp'
            total_wa += 1
        else:
            item['sumber'] = 'Web OPAC'
            total_opac += 1
            
        total_permintaan += item['total_frekuensi']
        item['terakhir_dicari_formatted'] = format_wib(item['terakhir_dicari'])
        items_list.append(item)

    items_list.sort(key=lambda x: (x['total_frekuensi'], str(x['terakhir_dicari'])), reverse=True)

    # Top 7 items for chart
    top_chart_items = items_list[:7]
    chart_top_labels = [it['judul'][:25] + ('...' if len(it['judul']) > 25 else '') for it in top_chart_items]
    chart_top_values = [it['total_frekuensi'] for it in top_chart_items]
    chart_top_colors = [
        '#F59E0B' if it['sumber'] == 'Keduanya' else ('#10B981' if it['sumber'] == 'WhatsApp' else '#0284C7')
        for it in top_chart_items
    ]

    total_unique = len(items_list)
    stats = {
        'total_judul': total_unique,
        'total_permintaan': total_permintaan,
        'keduanya': total_keduanya,
        'wa': total_wa,
        'opac': total_opac,
        'keduanya_pct': round((total_keduanya / total_unique * 100), 1) if total_unique > 0 else 0,
        'wa_pct': round((total_wa / total_unique * 100), 1) if total_unique > 0 else 0,
        'opac_pct': round((total_opac / total_unique * 100), 1) if total_unique > 0 else 0,
    }

    chart_data = {
        'source_labels': ['Keduanya (WA + OPAC)', 'WhatsApp Bot', 'Web OPAC'],
        'source_counts': [total_keduanya, total_wa, total_opac],
        'top_labels': chart_top_labels,
        'top_values': chart_top_values,
        'top_colors': chart_top_colors
    }

    return render_template(
        'usulan_buku.html',
        items=items_list,
        stats=stats,
        chart_data=chart_data
    )


@router.route('/api/usulan_buku/update_status', methods=['POST'])
@login_required
def api_update_usulan_status():
    data = request.json or {}
    usulan_id = data.get('id')
    new_status = data.get('status')
    
    if not usulan_id or not new_status:
        return jsonify({'status': 'error', 'message': 'Data tidak valid'}), 400
        
    conn = database.get_db_connection()
    try:
        if str(usulan_id).startswith('pg_'):
            pg_id = str(usulan_id).replace('pg_', '')
            pg_row = conn.execute("SELECT * FROM pencarian_gagal WHERE id = ?", (pg_id,)).fetchone()
            if pg_row:
                conn.execute(
                    "INSERT INTO usulan_buku (judul_topik, pengarang, catatan, status, sumber) VALUES (?, '-', ?, ?, 'Web OPAC')",
                    (pg_row['keyword'].title(), f"Dari pencarian katalog Web OPAC ({pg_row['jumlah_pencarian']}x)", new_status)
                )
                conn.commit()
        else:
            conn.execute("UPDATE usulan_buku SET status = ? WHERE id = ?", (new_status, usulan_id))
            conn.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        conn.close()


@router.route('/laporan_pertumbuhan')
@login_required
def laporan_pertumbuhan():
    conn = database.get_db_connection()
    rows = conn.execute("SELECT no_induk FROM buku WHERE status_buku != 'HILANG' AND status_buku != 'DIHAPUS'").fetchall()
    conn.close()
    
    from collections import defaultdict
    yearly_counts = defaultdict(int)
    
    for row in rows:
        no_induk = row['no_induk']
        if not no_induk: continue
        
        if '/' in no_induk:
            parts = no_induk.split('/')
            year_part = parts[-1].strip()
            
            # Sanitasi hanya angka
            import re
            year_part = re.sub(r'[^0-9]', '', year_part)
            
            if len(year_part) == 2:
                if int(year_part) > 50:
                    year = "19" + year_part
                else:
                    year = "20" + year_part
                yearly_counts[year] += 1
            elif len(year_part) == 4:
                yearly_counts[year_part] += 1
            else:
                yearly_counts["Tidak Diketahui"] += 1
        else:
            yearly_counts["Tidak Diketahui"] += 1
            
    # Sort valid years
    sorted_years = sorted([y for y in yearly_counts.keys() if y != "Tidak Diketahui"])
    
    labels = sorted_years
    data = [yearly_counts[y] for y in sorted_years]
    
    # Cumulative data
    cumulative = []
    current_total = 0
    for d in data:
        current_total += d
        cumulative.append(current_total)
        
    unknown_count = yearly_counts.get("Tidak Diketahui", 0)
    
    return render_template('laporan_pertumbuhan.html', 
                           labels=labels, 
                           data_yearly=data, 
                           data_cumulative=cumulative,
                           unknown_count=unknown_count)


@router.route('/laporan_ddc')
@login_required
def laporan_ddc():
    conn = database.get_db_connection()
    
    # 1. Ambil data buku
    rows = conn.execute("SELECT judul, klasifikasi FROM buku WHERE status_buku != 'HILANG' AND status_buku != 'DIHAPUS'").fetchall()
    conn.close()
    
    # Kategori DDC
    categories = {
        '0': {'name': '000-099 Karya Umum', 'judul': set(), 'eksemplar': 0},
        '1': {'name': '100-199 Filsafat & Psikologi', 'judul': set(), 'eksemplar': 0},
        '2': {'name': '200-299 Agama / Teologi', 'judul': set(), 'eksemplar': 0},
        '3': {'name': '300-399 Ilmu Sosial', 'judul': set(), 'eksemplar': 0},
        '4': {'name': '400-499 Bahasa', 'judul': set(), 'eksemplar': 0},
        '5': {'name': '500-599 Ilmu Murni', 'judul': set(), 'eksemplar': 0},
        '6': {'name': '600-699 Ilmu Terapan / Teknologi', 'judul': set(), 'eksemplar': 0},
        '7': {'name': '700-799 Seni & Olahraga', 'judul': set(), 'eksemplar': 0},
        '8': {'name': '800-899 Kesusastraan', 'judul': set(), 'eksemplar': 0},
        '9': {'name': '900-999 Sejarah & Geografi', 'judul': set(), 'eksemplar': 0},
        'other': {'name': 'Tanpa DDC / Format Tidak Dikenali', 'judul': set(), 'eksemplar': 0}
    }
    
    for row in rows:
        judul = row['judul'].strip().lower()
        klasifikasi = str(row['klasifikasi'] or '').strip()
        
        # Cari digit pertama (0-9)
        import re
        match = re.search(r'\d', klasifikasi)
        if match:
            first_digit = match.group(0)
            cat = categories.get(first_digit)
            if cat:
                cat['eksemplar'] += 1
                cat['judul'].add(judul)
            else:
                categories['other']['eksemplar'] += 1
                categories['other']['judul'].add(judul)
        else:
            categories['other']['eksemplar'] += 1
            categories['other']['judul'].add(judul)
            
    # Konversi set ke int count
    results = []
    for k, v in categories.items():
        results.append({
            'kategori': v['name'],
            'total_judul': len(v['judul']),
            'total_eksemplar': v['eksemplar']
        })
        
    # Total
    total_all_judul = sum([r['total_judul'] for r in results])
    total_all_eksemplar = sum([r['total_eksemplar'] for r in results])
    
    return render_template('laporan_ddc.html', results=results, total_judul=total_all_judul, total_eksemplar=total_all_eksemplar)


@router.route('/laporan_koleksi')
@login_required
def laporan_koleksi():
    start_id = request.args.get('start_id', '').strip()
    end_id = request.args.get('end_id', '').strip()
    
    has_result = True
    total_filsafat = 0
    total_teologi = 0
    total_umum = 0
    total_semua = 0
    
    judul_filsafat = set()
    judul_teologi = set()
    judul_umum = set()
    judul_semua = set()
    
    conn = database.get_db_connection()
    # Filter sesuai lokasi default aktif
    default_lokasi = get_branding()['default_lokasi']
    query = "SELECT no_induk, klasifikasi, judul FROM buku WHERE lokasi = ?"
    buku_list = conn.execute(query, (default_lokasi,)).fetchall()
    conn.close()
    
    def parse_no_induk(nid):
        try:
            parts = nid.split('/')
            if len(parts) == 2:
                return (int(parts[1]), int(parts[0]))
        except:
            pass
        return (-1, -1)
        
    s_val = parse_no_induk(start_id) if start_id else None
    e_val = parse_no_induk(end_id) if end_id else None
    
    for b in buku_list:
        if s_val and e_val:
            b_val = parse_no_induk(b['no_induk'])
            if b_val[0] == -1: continue # Skip if format doesn't match
            if not (s_val <= b_val <= e_val):
                continue
                
        total_semua += 1
        judul_buku = str(b['judul'] or '').strip().lower()
        judul_semua.add(judul_buku)
        
        klas = str(b['klasifikasi'] or '').strip()
        try:
            k_num = float(''.join([c for c in klas if c.isdigit() or c == '.']))
            if 100 <= k_num < 200:
                total_filsafat += 1
                judul_filsafat.add(judul_buku)
            elif 200 <= k_num < 300:
                total_teologi += 1
                judul_teologi.add(judul_buku)
            else:
                total_umum += 1
                judul_umum.add(judul_buku)
        except:
            total_umum += 1
            judul_umum.add(judul_buku)

    return render_template('laporan_koleksi.html', 
                          start_id=start_id, end_id=end_id, has_result=has_result,
                          total_filsafat=total_filsafat, total_teologi=total_teologi, 
                          total_umum=total_umum, total_semua=total_semua,
                          judul_filsafat=len(judul_filsafat), judul_teologi=len(judul_teologi),
                          judul_umum=len(judul_umum), judul_semua=len(judul_semua))


@router.route('/laporan_buku_dibaca')
@login_required
def laporan_buku_dibaca():
    return render_template('laporan_buku_dibaca.html')


@router.route('/api/buku_dibaca')
@api_login_required
def api_buku_dibaca():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    conn = database.get_db_connection()
    query = '''
        SELECT bd.id, bd.waktu_baca, bd.tanggal, bd.no_induk, b.judul, b.pengarang, b.klasifikasi
        FROM buku_dibaca bd
        LEFT JOIN eksemplar e ON bd.no_induk = e.no_induk
        LEFT JOIN bibliografi b ON e.biblio_id = b.id
        WHERE 1=1
    '''
    params = []
    if start_date:
        query += " AND bd.tanggal >= ?"
        params.append(start_date)
    if end_date:
        query += " AND bd.tanggal <= ?"
        params.append(end_date)
        
    query += " ORDER BY bd.waktu_baca DESC"
    
    cursor = conn.execute(query, params)
    data = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(data)


@router.route('/api/buku_dibaca/export')
@api_login_required
def export_buku_dibaca():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    conn = database.get_db_connection()
    query = '''
        SELECT bd.waktu_baca, bd.tanggal, bd.no_induk, b.judul, b.pengarang, b.klasifikasi
        FROM buku_dibaca bd
        LEFT JOIN eksemplar e ON bd.no_induk = e.no_induk
        LEFT JOIN bibliografi b ON e.biblio_id = b.id
        WHERE 1=1
    '''
    params = []
    if start_date:
        query += " AND bd.tanggal >= ?"
        params.append(start_date)
    if end_date:
        query += " AND bd.tanggal <= ?"
        params.append(end_date)
        
    query += " ORDER BY bd.waktu_baca DESC"
    
    cursor = conn.execute(query, params)
    data = cursor.fetchall()
    conn.close()
    
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Waktu Baca', 'Tanggal', 'No Barcode', 'Judul Buku', 'Pengarang', 'Klasifikasi'])
    for row in data:
        cw.writerow([row['waktu_baca'], row['tanggal'], row['no_induk'], row['judul'], row['pengarang'], row['klasifikasi']])
        
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=buku_dibaca.csv"
    output.headers["Content-type"] = "text/csv"
    return output


@router.route('/analisis_kecenderungan_dampak')
@login_required
def analisis_kecenderungan_dampak():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    
    conn = database.get_db_connection()
    
    filter_sql = ""
    filter_params = []
    if start_date:
        filter_sql += " AND bd.tanggal >= ?"
        filter_params.append(start_date)
    if end_date:
        filter_sql += " AND bd.tanggal <= ?"
        filter_params.append(end_date)
        
    total_baca_query = f"SELECT COUNT(*) as c FROM buku_dibaca bd WHERE 1=1 {filter_sql}"
    total_baca = conn.execute(total_baca_query, filter_params).fetchone()['c']
    
    total_judul_query = f"""
        SELECT COUNT(DISTINCT e.biblio_id) as c 
        FROM buku_dibaca bd 
        JOIN eksemplar e ON bd.no_induk = e.no_induk 
        WHERE 1=1 {filter_sql}
    """
    total_judul_unik = conn.execute(total_judul_query, filter_params).fetchone()['c']
    
    ddc_query = f"""
        SELECT 
            CASE 
                WHEN b.klasifikasi LIKE '0%' THEN '000 - Karya Umum / Komputer'
                WHEN b.klasifikasi LIKE '1%' THEN '100 - Filsafat & Psikologi'
                WHEN b.klasifikasi LIKE '2%' THEN '200 - Agama & Teologi'
                WHEN b.klasifikasi LIKE '3%' THEN '300 - Ilmu Sosial & Politik'
                WHEN b.klasifikasi LIKE '4%' THEN '400 - Bahasa & Linguistik'
                WHEN b.klasifikasi LIKE '5%' THEN '500 - Sains Murni'
                WHEN b.klasifikasi LIKE '6%' THEN '600 - Teknologi & Terapan'
                WHEN b.klasifikasi LIKE '7%' THEN '700 - Kesenian & Rekreasi'
                WHEN b.klasifikasi LIKE '8%' THEN '800 - Kesusastraan'
                WHEN b.klasifikasi LIKE '9%' THEN '900 - Sejarah & Geografi'
                WHEN b.klasifikasi = 'R' OR b.klasifikasi LIKE 'R%' THEN 'R - Referensi'
                ELSE 'Lainnya'
            END as kategori,
            COUNT(*) as total
        FROM buku_dibaca bd
        LEFT JOIN eksemplar e ON bd.no_induk = e.no_induk
        LEFT JOIN bibliografi b ON e.biblio_id = b.id
        WHERE 1=1 {filter_sql}
        GROUP BY kategori
        ORDER BY total DESC
    """
    ddc_rows = conn.execute(ddc_query, filter_params).fetchall()
    ddc_dist = [dict(r) for r in ddc_rows]
    for item in ddc_dist:
        item['persentase'] = round((item['total'] / total_baca * 100), 1) if total_baca > 0 else 0
        
    top_ddc = ddc_dist[0]['kategori'] if ddc_dist else 'Belum ada data'
    top_ddc_persen = ddc_dist[0]['persentase'] if ddc_dist else 0
    
    subjek_spesifik_query = f"""
        SELECT 
            b.klasifikasi,
            COUNT(*) as total
        FROM buku_dibaca bd
        LEFT JOIN eksemplar e ON bd.no_induk = e.no_induk
        LEFT JOIN bibliografi b ON e.biblio_id = b.id
        WHERE 1=1 {filter_sql} AND b.klasifikasi IS NOT NULL AND b.klasifikasi != ''
        GROUP BY b.klasifikasi
        ORDER BY total DESC
        LIMIT 8
    """
    subjek_spesifik = [dict(r) for r in conn.execute(subjek_spesifik_query, filter_params).fetchall()]
    
    # Konversi Waktu ke WIB (UTC+7 jam)
    hourly_query = f"""
        SELECT strftime('%H', datetime(waktu_baca, '+7 hours')) as jam, COUNT(*) as total
        FROM buku_dibaca bd
        WHERE 1=1 {filter_sql}
        GROUP BY jam
        ORDER BY jam
    """
    hourly_dist = [dict(r) for r in conn.execute(hourly_query, filter_params).fetchall()]
    
    peak_hour = 'Belum Ada'
    if hourly_dist:
        max_h = max(hourly_dist, key=lambda x: x['total'])
        jam_int = int(max_h['jam'])
        peak_hour = f"{jam_int:02d}:00 - {(jam_int + 1)%24:02d}:00 WIB ({max_h['total']} baca)"
        
    top_books_query = f"""
        SELECT 
            b.id, b.judul, b.pengarang, b.klasifikasi,
            COUNT(bd.id) as total_dibaca,
            (SELECT COUNT(*) FROM eksemplar e2 WHERE e2.biblio_id = b.id) as total_eksemplar
        FROM buku_dibaca bd
        JOIN eksemplar e ON bd.no_induk = e.no_induk
        JOIN bibliografi b ON e.biblio_id = b.id
        WHERE 1=1 {filter_sql}
        GROUP BY b.id
        ORDER BY total_dibaca DESC, b.judul ASC
        LIMIT 15
    """
    top_books = [dict(r) for r in conn.execute(top_books_query, filter_params).fetchall()]
    
    # Riwayat baca dalam format WIB (+7 hours)
    recent_reads_query = f"""
        SELECT datetime(bd.waktu_baca, '+7 hours') as waktu_baca, bd.no_induk, b.judul, b.pengarang, b.klasifikasi
        FROM buku_dibaca bd
        LEFT JOIN eksemplar e ON bd.no_induk = e.no_induk
        LEFT JOIN bibliografi b ON e.biblio_id = b.id
        WHERE 1=1 {filter_sql}
        ORDER BY bd.waktu_baca DESC
        LIMIT 10
    """
    recent_reads = [dict(r) for r in conn.execute(recent_reads_query, filter_params).fetchall()]
    
    conn.close()
    
    return render_template(
        'analisis_kecenderungan_dampak.html',
        start_date=start_date,
        end_date=end_date,
        total_baca=total_baca,
        total_judul_unik=total_judul_unik,
        ddc_dist=ddc_dist,
        top_ddc=top_ddc,
        top_ddc_persen=top_ddc_persen,
        subjek_spesifik=subjek_spesifik,
        hourly_dist=hourly_dist,
        peak_hour=peak_hour,
        top_books=top_books,
        recent_reads=recent_reads
    )


@router.route('/analisis_peminjaman_dampak')
@login_required
def analisis_peminjaman_dampak():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    
    conn = database.get_db_connection()
    
    filter_sql = ""
    filter_params = []
    if start_date:
        filter_sql += " AND p.loan_date >= ?"
        filter_params.append(start_date)
    if end_date:
        filter_sql += " AND p.loan_date <= ?"
        filter_params.append(end_date)
        
    # 1. Total Pinjam
    total_pinjam_query = f"SELECT COUNT(*) as c FROM peminjaman p WHERE 1=1 {filter_sql}"
    total_pinjam = conn.execute(total_pinjam_query, filter_params).fetchone()['c']
    
    # 2. Total Peminjam Unik
    total_peminjam_query = f"SELECT COUNT(DISTINCT p.member_id) as c FROM peminjaman p WHERE 1=1 {filter_sql}"
    total_peminjam_unik = conn.execute(total_peminjam_query, filter_params).fetchone()['c']
    
    # 3. Total Judul Unik
    total_judul_query = f"SELECT COUNT(DISTINCT p.judul) as c FROM peminjaman p WHERE 1=1 {filter_sql}"
    total_judul_unik = conn.execute(total_judul_query, filter_params).fetchone()['c']
    
    # 4. Distribusi DDC
    ddc_query = f"""
        SELECT 
            CASE 
                WHEN b.klasifikasi LIKE '0%' THEN '000 - Karya Umum / Komputer'
                WHEN b.klasifikasi LIKE '1%' THEN '100 - Filsafat & Psikologi'
                WHEN b.klasifikasi LIKE '2%' THEN '200 - Agama & Teologi'
                WHEN b.klasifikasi LIKE '3%' THEN '300 - Ilmu Sosial & Politik'
                WHEN b.klasifikasi LIKE '4%' THEN '400 - Bahasa & Linguistik'
                WHEN b.klasifikasi LIKE '5%' THEN '500 - Sains Murni'
                WHEN b.klasifikasi LIKE '6%' THEN '600 - Teknologi & Terapan'
                WHEN b.klasifikasi LIKE '7%' THEN '700 - Kesenian & Rekreasi'
                WHEN b.klasifikasi LIKE '8%' THEN '800 - Kesusastraan'
                WHEN b.klasifikasi LIKE '9%' THEN '900 - Sejarah & Geografi'
                WHEN b.klasifikasi = 'R' OR b.klasifikasi LIKE 'R%' THEN 'R - Referensi'
                ELSE 'Lainnya / Umum'
            END as kategori,
            COUNT(*) as total
        FROM peminjaman p
        LEFT JOIN eksemplar e ON p.no_induk = e.no_induk
        LEFT JOIN bibliografi b ON e.biblio_id = b.id
        WHERE 1=1 {filter_sql}
        GROUP BY kategori
        ORDER BY total DESC
    """
    ddc_rows = conn.execute(ddc_query, filter_params).fetchall()
    ddc_dist = [dict(r) for r in ddc_rows]
    for item in ddc_dist:
        item['persentase'] = round((item['total'] / total_pinjam * 100), 1) if total_pinjam > 0 else 0
        
    top_ddc = ddc_dist[0]['kategori'] if ddc_dist else 'Belum ada data'
    top_ddc_persen = ddc_dist[0]['persentase'] if ddc_dist else 0
    
    # 5. Tren Bulanan Sirkulasi
    trend_query = f"""
        SELECT SUBSTR(p.loan_date, 1, 7) as periode, COUNT(*) as total
        FROM peminjaman p
        WHERE p.loan_date IS NOT NULL AND p.loan_date != '' {filter_sql}
        GROUP BY periode
        ORDER BY periode ASC
    """
    trend_rows = conn.execute(trend_query, filter_params).fetchall()
    trend_data = [dict(r) for r in trend_rows]
    
    peak_month = 'Belum Ada'
    if trend_data:
        max_m = max(trend_data, key=lambda x: x['total'])
        peak_month = f"{max_m['periode']} ({max_m['total']} buku)"
        
    # 6. Top 15 Buku Paling Sering Dipinjam
    top_books_query = f"""
        SELECT 
            p.judul,
            COALESCE(MAX(b.pengarang), '-') as pengarang,
            COALESCE(MAX(b.klasifikasi), '-') as klasifikasi,
            COUNT(*) as total_dipinjam
        FROM peminjaman p
        LEFT JOIN eksemplar e ON p.no_induk = e.no_induk
        LEFT JOIN bibliografi b ON e.biblio_id = b.id
        WHERE 1=1 {filter_sql}
        GROUP BY p.judul
        ORDER BY total_dipinjam DESC, p.judul ASC
        LIMIT 15
    """
    top_books = [dict(r) for r in conn.execute(top_books_query, filter_params).fetchall()]
    
    # 7. Top 15 Pemustaka Teraktif
    top_borrowers_query = f"""
        SELECT 
            p.member_id,
            p.member_name,
            COUNT(*) as total_pinjam,
            COUNT(DISTINCT p.judul) as judul_dibaca
        FROM peminjaman p
        WHERE 1=1 {filter_sql}
        GROUP BY p.member_id, p.member_name
        ORDER BY total_pinjam DESC
        LIMIT 15
    """
    top_borrowers = [dict(r) for r in conn.execute(top_borrowers_query, filter_params).fetchall()]
    
    # 8. 30 Transaksi Sirkulasi Terkini
    recent_loans_query = f"""
        SELECT 
            p.member_id,
            p.member_name,
            p.no_induk,
            p.judul,
            p.loan_date,
            p.due_date,
            p.loan_status
        FROM peminjaman p
        WHERE 1=1 {filter_sql}
        ORDER BY p.loan_date DESC, p.id DESC
        LIMIT 30
    """
    recent_loans = [dict(r) for r in conn.execute(recent_loans_query, filter_params).fetchall()]
    
    conn.close()
    
    return render_template(
        'analisis_peminjaman_dampak.html',
        start_date=start_date,
        end_date=end_date,
        total_pinjam=total_pinjam,
        total_peminjam_unik=total_peminjam_unik,
        total_judul_unik=total_judul_unik,
        top_ddc=top_ddc,
        top_ddc_persen=top_ddc_persen,
        peak_month=peak_month,
        ddc_dist=ddc_dist,
        trend_data=trend_data,
        top_books=top_books,
        top_borrowers=top_borrowers,
        recent_loans=recent_loans
    )


@router.route('/analisis_strategis_terpadu')
@login_required
def analisis_strategis_terpadu():
    conn = database.get_db_connection()
    c = conn.cursor()
    
    # 1. Koleksi Stats
    total_biblio = c.execute("SELECT COUNT(*) FROM bibliografi").fetchone()[0]
    
    # DDC Koleksi
    ddc_koleksi_rows = c.execute("""
        SELECT 
            CASE 
                WHEN klasifikasi LIKE '1%' THEN '100 Filsafat'
                WHEN klasifikasi LIKE '2%' THEN '200 Teologi'
                WHEN klasifikasi LIKE '3%' THEN '300 Sosial'
                WHEN klasifikasi LIKE 'R%' OR klasifikasi = 'R' THEN 'Referensi'
                WHEN klasifikasi LIKE '9%' THEN '900 Sejarah'
                WHEN klasifikasi LIKE '8%' THEN '800 Sastra'
                ELSE 'Lainnya'
            END as ddc,
            COUNT(*) as total
        FROM bibliografi
        GROUP BY ddc
    """).fetchall()
    dict_koleksi = {r['ddc']: r['total'] for r in ddc_koleksi_rows}
    
    # 2. Pengunjung Stats
    total_kunjungan = c.execute("SELECT COUNT(*) FROM sjla_visitor_logs").fetchone()[0]
    
    # Peran Pengunjung
    peran_rows = c.execute("""
        SELECT 
            CASE 
                WHEN LOWER(peran_jabatan) LIKE '%frater%' THEN 'Frater Seminari STPD'
                WHEN LOWER(peran_jabatan) LIKE '%dosen%' THEN 'Dosen / Pengajar'
                WHEN LOWER(peran_jabatan) LIKE '%filsafat%' OR LOWER(peran_jabatan) LIKE '%mahasiswa%' THEN 'Mahasiswa UKWMS'
                WHEN LOWER(peran_jabatan) LIKE '%umum%' THEN 'Pemustaka Umum'
                ELSE 'Lainnya / Tamu'
            END as peran_kategori,
            COUNT(*) as total
        FROM sjla_visitor_logs
        GROUP BY peran_kategori
        ORDER BY total DESC
    """).fetchall()
    
    colors_map = {
        'Mahasiswa UKWMS': '#2563EB',
        'Frater Seminari STPD': '#7C3AED',
        'Pemustaka Umum': '#059669',
        'Dosen / Pengajar': '#D97706',
        'Lainnya / Tamu': '#64748B'
    }
    peran_stats = []
    for r in peran_rows:
        pct = round((r['total'] / total_kunjungan * 100), 1) if total_kunjungan > 0 else 0
        peran_stats.append({
            'peran': r['peran_kategori'],
            'total': r['total'],
            'persen': pct,
            'color': colors_map.get(r['peran_kategori'], '#94A3B8')
        })
        
    # 3. Peminjaman Stats
    total_pinjam = c.execute("SELECT COUNT(*) FROM peminjaman").fetchone()[0]
    total_judul_pinjam = c.execute("SELECT COUNT(DISTINCT judul) FROM peminjaman").fetchone()[0]
    
    # DDC Pinjam
    ddc_pinjam_rows = c.execute("""
        SELECT 
            CASE 
                WHEN b.klasifikasi LIKE '1%' THEN '100 Filsafat'
                WHEN b.klasifikasi LIKE '2%' THEN '200 Teologi'
                WHEN b.klasifikasi LIKE '3%' THEN '300 Sosial'
                WHEN b.klasifikasi LIKE 'R%' OR b.klasifikasi = 'R' THEN 'Referensi'
                WHEN b.klasifikasi LIKE '9%' THEN '900 Sejarah'
                WHEN b.klasifikasi LIKE '8%' THEN '800 Sastra'
                ELSE 'Lainnya'
            END as ddc,
            COUNT(*) as total
        FROM peminjaman p
        LEFT JOIN eksemplar e ON p.no_induk = e.no_induk
        LEFT JOIN bibliografi b ON e.biblio_id = b.id
        GROUP BY ddc
    """).fetchall()
    dict_pinjam = {r['ddc']: r['total'] for r in ddc_pinjam_rows}
    
    conn.close()
    
    # Matriks Komparasi
    categories_ordered = [
        ('100 Filsafat', '100 - Filsafat & Psikologi', 'Urgensi Utama: Pengadaan copy 2 & 3 untuk buku primer agar antrean teratasi.'),
        ('200 Teologi', '200 - Agama & Teologi', 'Pondasi utama institusi; kurasi preservasi naskah langka & penetapan koleksi tandon.'),
        ('300 Sosial', '300 - Ilmu Sosial & Politik', 'Seimbang; relevan untuk penopang mata kuliah Ajaran Sosial Gereja (ASG).'),
        ('Referensi', 'R - Koleksi Referensi', 'Kamus & leksikon Alkitab; wajib berstatus Tandon (hanya baca di tempat).'),
        ('900 Sejarah', '900 - Sejarah & Geografi', 'Cukup memadai untuk sejarah gereja universal dan sejarah misi nusantara.'),
        ('800 Sastra', '800 - Kesusastraan', 'Pengayaan estetika rohani, puisi, dan novel spiritual.'),
        ('Lainnya', 'Lainnya / Belum Terklasifikasi', 'Kebutuhan mendasar: Data cleaning & input DDC segera agar terindeks di OPAC.')
    ]
    
    matriks_rows = []
    chart_labels = []
    chart_koleksi_pct = []
    chart_pinjam_pct = []
    
    for key, display_name, rec in categories_ordered:
        k_cnt = dict_koleksi.get(key, 0)
        p_cnt = dict_pinjam.get(key, 0)
        k_pct = round((k_cnt / total_biblio * 100), 1) if total_biblio > 0 else 0
        p_pct = round((p_cnt / total_pinjam * 100), 1) if total_pinjam > 0 else 0
        ratio = round((p_pct / k_pct), 1) if k_pct > 0 else 0
        
        chart_labels.append(key)
        chart_koleksi_pct.append(k_pct)
        chart_pinjam_pct.append(p_pct)
        
        matriks_rows.append({
            'ddc': display_name,
            'koleksi_count': k_cnt,
            'koleksi_pct': k_pct,
            'pinjam_count': p_cnt,
            'pinjam_pct': p_pct,
            'utilitas_ratio': ratio,
            'rekomendasi': rec
        })
        
    return render_template(
        'analisis_strategis_terpadu.html',
        total_biblio=total_biblio,
        total_kunjungan=total_kunjungan,
        total_pinjam=total_pinjam,
        total_judul_pinjam=total_judul_pinjam,
        chart_labels=chart_labels,
        chart_koleksi_pct=chart_koleksi_pct,
        chart_pinjam_pct=chart_pinjam_pct,
        peran_stats=peran_stats,
        matriks_rows=matriks_rows
    )


@router.route('/jadwal', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE'])
@router.route('/jadwal/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@feature_required('ENABLE_SYNC_JADWAL_PROXY', 'Portal Jadwal online dinonaktifkan pada mode offline.', api_status=503)
def proxy_jadwal_vianney(path):
    target_url = f'http://127.0.0.1:5007/jadwal/{path}' if path else 'http://127.0.0.1:5007/jadwal'
    if request.query_string:
        target_url += f'?{request.query_string.decode("utf-8")}'
    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers={key: value for (key, value) in request.headers if key != 'Host'},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=30
        )
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for (name, value) in resp.raw.headers.items() if name.lower() not in excluded_headers]
        return make_response(resp.content, resp.status_code, headers)
    except Exception as e:
        return f"Gagal menghubungkan ke service Jadwal Doa: {str(e)}", 502


@router.route('/api/sync/office_all', methods=['POST'])
@feature_required('ENABLE_SYNC_JADWAL_PROXY', 'Sinkronisasi online dinonaktifkan pada mode offline.', api_status=503)
def api_sync_office_all():
    token = request.headers.get('X-Sync-Token') or request.args.get('token')
    if token != 'sjla_yosef_super_secret_sync_2026':
        return jsonify({'status': 'error', 'message': 'Token otentikasi tidak valid.'}), 403

    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({'status': 'error', 'message': 'Payload JSON tidak valid atau kosong.'}), 400

    conn = database.get_db_connection()
    c = conn.cursor()

    # --- 1. SINKRONISASI PENGUNJUNG (sjla_visitor_logs) ---
    visitors = payload.get('visitors', [])
    c.execute("SELECT identitas, waktu_kunjungan FROM sjla_visitor_logs")
    existing_sigs = {f"{r['identitas']}|{r['waktu_kunjungan']}" for r in c.fetchall()}

    visitors_added = 0
    for v in visitors:
        identitas = str(v.get('identitas', '')).strip()
        waktu = str(v.get('waktu_kunjungan', '')).strip()
        sig = f"{identitas}|{waktu}"
        if sig not in existing_sigs and identitas and waktu:
            existing_sigs.add(sig)
            c.execute("""
                INSERT INTO sjla_visitor_logs 
                (waktu_kunjungan, tipe_pengunjung, identitas, asal_instansi, tujuan_kunjungan, peran_jabatan, fakultas)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                waktu,
                str(v.get('tipe_pengunjung', 'Non-Member')),
                identitas,
                str(v.get('instansi', '-')),
                'Kunjungan Perpustakaan',
                str(v.get('peran_jabatan', '-')),
                str(v.get('nama_fakultas', '-'))
            ))
            visitors_added += 1

    # --- 2. SINKRONISASI PEMINJAMAN & SIRKULASI SLIMS (TERMASUK PERPANJANGAN) ---
    loans = payload.get('loans', [])
    active_loans = payload.get('active_loans', [])
    loans_synced = 0
    active_loans_count = 0

    if loans or active_loans:
        # 1. Update tabel peminjaman
        c.execute("""
            CREATE TABLE IF NOT EXISTS peminjaman (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id TEXT,
                member_name TEXT,
                no_induk TEXT,
                judul TEXT,
                loan_date TEXT,
                due_date TEXT,
                loan_status TEXT
            )
        """)
        c.execute("DELETE FROM peminjaman")

        # Map active loans by item_code for instant lookup
        active_map = {str(a.get('item_code', '')).strip(): a for a in active_loans}

        for l in loans:
            m_id = str(l.get('member_id', '')).strip()
            m_name = str(l.get('member_name', '')).strip()
            no_induk = str(l.get('item_code', '')).strip()
            judul = str(l.get('title', '')).strip()
            l_date = str(l.get('loan_date', '')).strip()
            d_date = str(l.get('due_date', '')).strip()
            status = str(l.get('loan_status', '')).strip()

            # Sinkronkan perpanjangan tanggal jatuh tempo dari active_loans
            if active_map:
                if no_induk in active_map and active_map[no_induk].get('member_id') == m_id:
                    act = active_map[no_induk]
                    d_date = str(act.get('due_date', d_date)).strip()
                    l_date = str(act.get('loan_date', l_date)).strip()
                    status = '0'
                else:
                    # Jika buku sudah tidak ada di active_loans SLiMS, pastikan statusnya dikembalikan (1)
                    if status in ('0', '0.0', 'On Loan'):
                        status = '1'

            c.execute("""
                INSERT INTO peminjaman (member_id, member_name, no_induk, judul, loan_date, due_date, loan_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (m_id, m_name, no_induk, judul, l_date, d_date, status))
            loans_synced += 1

        # 2. Update tabel sirkulasi (Gunakan data pinjaman aktif & perpanjangan riil SLiMS)
        c.execute("""
            SELECT member_id, no_induk, fine_status, fine_amount, renewal_count 
            FROM sirkulasi 
            WHERE return_date IS NULL OR return_date = ''
        """)
        existing_sirkulasi = {(r[0], r[1]): {'fine_status': r[2], 'fine_amount': r[3], 'renewal_count': r[4]} for r in c.fetchall()}

        c.execute("DELETE FROM sirkulasi")

        # Sumber pinjaman aktif: utamakan active_loans dari member_loan_list SLiMS
        source_active = active_loans if active_loans else [l for l in loans if str(l.get('loan_status', '')) in ('0', '0.0', 'On Loan')]

        for act in source_active:
            m_id = str(act.get('member_id', '')).strip()
            no_induk = str(act.get('item_code', '')).strip()
            l_date = str(act.get('loan_date', '')).strip()
            d_date = str(act.get('due_date', '')).strip()
            phone = str(act.get('phone', '')).strip()

            prev_meta = existing_sirkulasi.get((m_id, no_induk), {})
            prev_renew = prev_meta.get('renewal_count', 0)
            fine_status = prev_meta.get('fine_status', 'BELUM_LUNAS')
            fine_amount = prev_meta.get('fine_amount', 0)

            # Hitung perpanjangan: selisih due_date dan loan_date (> 16 hari)
            calculated_renew = 0
            try:
                dt_loan = datetime.datetime.strptime(l_date, '%Y-%m-%d').date()
                dt_due = datetime.datetime.strptime(d_date, '%Y-%m-%d').date()
                days = (dt_due - dt_loan).days
                if days > 16:
                    calculated_renew = max(1, (days - 14) // 14 + 1)
            except Exception:
                pass

            renewal_count = max(prev_renew, calculated_renew)

            # Jika riwayat peminjaman pernah mencatat buku ini dipinjam anggota yang sama sebelumnya
            if renewal_count == 0:
                c.execute("""
                    SELECT COUNT(*) FROM peminjaman 
                    WHERE member_id = ? AND no_induk = ? AND loan_date < ?
                """, (m_id, no_induk, l_date))
                prev_count = c.fetchone()[0]
                if prev_count > 0:
                    renewal_count = max(1, prev_count)

            c.execute("""
                INSERT INTO sirkulasi (member_id, no_induk, loan_date, due_date, renewal_count, fine_amount, fine_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (m_id, no_induk, l_date, d_date, renewal_count, fine_amount, fine_status))
            active_loans_count += 1

            # Update nomor telepon peminjam jika tersedia dari SLiMS
            if phone and len(phone) >= 10:
                c.execute("""
                    UPDATE anggota SET telepon = ? 
                    WHERE member_id = ? AND (telepon IS NULL OR telepon = '' OR LENGTH(telepon) < 8)
                """, (phone, m_id))

    conn.commit()
    conn.close()

    return jsonify({
        'status': 'success',
        'message': 'Sinkronisasi SLiMS dan Pengunjung Kantor berhasil!',
        'visitors_added': visitors_added,
        'total_loans_synced': loans_synced,
        'active_loans_synced': active_loans_count,
        'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })


@router.route('/pengadaan_kurikulum')
@login_required
def pengadaan_kurikulum():
    return render_template('pengadaan_kurikulum.html')


@router.route('/api/pengadaan_kurikulum/data')
@login_required
def api_pengadaan_kurikulum_data():
    try:
        conn = database.get_db_connection()
        c = conn.cursor()
        
        # Query courses with aggregate counts
        c.execute("""
            SELECT m.id, m.kode_mk, m.nama_mk, m.prodi, m.semester, m.sks,
                   COUNT(p.id) as total_books,
                   SUM(CASE WHEN p.status_rak = 'TERSEDIA' THEN 1 ELSE 0 END) as available_count,
                   SUM(CASE WHEN p.status_rak = 'DEFISIT' THEN 1 ELSE 0 END) as deficit_count
            FROM kurikulum_matakuliah m
            LEFT JOIN kurikulum_pustaka p ON m.id = p.matakuliah_id
            GROUP BY m.id
            ORDER BY m.semester ASC, m.kode_mk ASC
        """)
        courses_rows = c.fetchall()

        # Query all books
        c.execute("""
            SELECT id, matakuliah_id, kode_mk, judul_buku, penulis, sitasi_lengkap, status_rak, biblio_id, call_number, jumlah_tersedia
            FROM kurikulum_pustaka
            ORDER BY status_rak DESC, judul_buku ASC
        """)
        all_books = c.fetchall()
        books_by_mk = {}
        for b in all_books:
            mk_id = b['matakuliah_id']
            if mk_id not in books_by_mk:
                books_by_mk[mk_id] = []
            books_by_mk[mk_id].append(dict(b))

        courses = []
        for r in courses_rows:
            item = dict(r)
            item['books'] = books_by_mk.get(r['id'], [])
            courses.append(item)

        total_mk = len(courses_rows)
        total_pustaka = len(all_books)
        total_tersedia = sum(1 for b in all_books if b['status_rak'] == 'TERSEDIA')
        total_defisit = total_pustaka - total_tersedia
        cov_pct = round((total_tersedia / total_pustaka * 100), 1) if total_pustaka > 0 else 0

        conn.close()
        return jsonify({
            'status': 'success',
            'kpi': {
                'total_mk': total_mk,
                'total_pustaka': total_pustaka,
                'tersedia': total_tersedia,
                'defisit': total_defisit,
                'coverage_pct': cov_pct
            },
            'courses': courses
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@router.route('/pengadaan_kurikulum/cetak_proposal')
@login_required
def pengadaan_kurikulum_cetak_proposal():
    prodi = request.args.get('prodi', 'ALL')
    semester = request.args.get('semester', 'ALL')

    conn = database.get_db_connection()
    c = conn.cursor()

    query = "SELECT * FROM kurikulum_matakuliah WHERE 1=1"
    params = []
    if prodi != 'ALL':
        query += " AND prodi = ?"
        params.append(prodi)
    if semester != 'ALL':
        query += " AND semester = ?"
        params.append(semester)
    query += " ORDER BY semester ASC, kode_mk ASC"

    c.execute(query, params)
    courses = [dict(r) for r in c.fetchall()]

    c_ids = [c_item['id'] for c_item in courses]
    deficit_books = []
    total_pustaka = 0

    if c_ids:
        placeholders = ','.join(['?'] * len(c_ids))
        # Count total pustaka
        c.execute(f"SELECT COUNT(*) as c FROM kurikulum_pustaka WHERE matakuliah_id IN ({placeholders})", c_ids)
        total_pustaka = c.fetchone()['c']

        # Get deficit books
        c.execute(f"""
            SELECT p.*, m.nama_mk 
            FROM kurikulum_pustaka p
            JOIN kurikulum_matakuliah m ON p.matakuliah_id = m.id
            WHERE p.status_rak = 'DEFISIT' AND p.matakuliah_id IN ({placeholders})
            ORDER BY m.semester ASC, m.kode_mk ASC, p.judul_buku ASC
        """, c_ids)
        deficit_books = [dict(r) for r in c.fetchall()]

    conn.close()

    prodi_label = prodi if prodi != 'ALL' else 'Seluruh Program Studi'
    sem_label = f"Semester {semester}" if semester != 'ALL' else 'Seluruh Semester (1-12)'

    bulan_indo = ['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']
    now = datetime.datetime.now()
    tanggal_hari_ini = f"{now.day} {bulan_indo[now.month]} {now.year}"

    return render_template('cetak_proposal_pengadaan.html',
                           courses=courses,
                           deficit_books=deficit_books,
                           total_pustaka=total_pustaka,
                           prodi_label=prodi_label,
                           sem_label=sem_label,
                           tanggal_hari_ini=tanggal_hari_ini)


