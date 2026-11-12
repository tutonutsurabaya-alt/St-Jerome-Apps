"""Domain module: sirkulasi."""

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

router = ModuleRouter('sirkulasi')

@router.route('/')
def index():
    if 'logged_in' in session:
        try:
            conn = database.get_db_connection()
            c = conn.cursor()
            
            # 1. Total bibliografi & bersampul
            c.execute("SELECT COUNT(*) FROM bibliografi")
            total_buku = c.fetchone()[0] or 14155
            
            c.execute("SELECT COUNT(*) FROM bibliografi WHERE image IS NOT NULL AND image != '' AND image != 'tidak_ada' AND image != 'NOT_FOUND' AND image != 'RATE_LIMIT'")
            total_bersampul = c.fetchone()[0] or 3097
            bersampul_pct = round((total_bersampul / total_buku * 100), 1) if total_buku else 0
            
            # 2. Total eksemplar
            c.execute("SELECT COUNT(*) FROM eksemplar")
            total_eksemplar = c.fetchone()[0] or total_buku
            
            # 3. Kunjungan hari ini (sjla_visitor_logs)
            c.execute("SELECT COUNT(*) FROM sjla_visitor_logs WHERE date(waktu_kunjungan) = date('now', 'localtime')")
            kunjungan_today = c.fetchone()[0] or 0
            
            # 4. Buku dibaca di tempat hari ini
            c.execute("SELECT COUNT(*) FROM buku_dibaca WHERE date(tanggal) = date('now', 'localtime')")
            dibaca_today = c.fetchone()[0] or 0
            
            # 5. Peminjaman aktif & jatuh tempo
            c.execute("SELECT COUNT(*) FROM sirkulasi WHERE return_date IS NULL")
            pinjam_aktif = c.fetchone()[0] or 0
            
            c.execute("SELECT COUNT(*) FROM sirkulasi WHERE return_date IS NULL AND date(due_date) < date('now', 'localtime')")
            jatuh_tempo = c.fetchone()[0] or 0
            
            # 6. Koleksi populer (top read books)
            c.execute('''
                SELECT b.id, b.judul, b.pengarang, b.image, b.klasifikasi, COUNT(bd.id) as read_count 
                FROM buku_dibaca bd 
                JOIN eksemplar e ON bd.no_induk = e.no_induk 
                JOIN bibliografi b ON e.biblio_id = b.id 
                GROUP BY b.id 
                ORDER BY read_count DESC 
                LIMIT 5
            ''')
            populer = [dict(row) for row in c.fetchall()]
            if len(populer) < 5:
                c.execute('''
                    SELECT id, judul, pengarang, image, klasifikasi, 0 as read_count
                    FROM bibliografi
                    WHERE image IS NOT NULL AND image != '' AND image != 'tidak_ada' AND image != 'NOT_FOUND'
                    ORDER BY id DESC LIMIT 5
                ''')
                populer = [dict(row) for row in c.fetchall()]
                
            # 7. Recent reads
            c.execute('''
                SELECT 'baca' as tipe, b.judul, bd.tanggal as waktu, 'Meja Baca' as keterangan
                FROM buku_dibaca bd
                JOIN eksemplar e ON bd.no_induk = e.no_induk
                JOIN bibliografi b ON e.biblio_id = b.id
                ORDER BY bd.tanggal DESC LIMIT 4
            ''')
            recent_reads = [dict(row) for row in c.fetchall()]
            
            # 8. Recent loans
            c.execute('''
                SELECT 'sirkulasi' as tipe, b.judul, s.loan_date as waktu, s.member_id as keterangan
                FROM sirkulasi s
                JOIN eksemplar e ON s.no_induk = e.no_induk
                JOIN bibliografi b ON e.biblio_id = b.id
                ORDER BY s.loan_date DESC LIMIT 3
            ''')
            recent_loans = [dict(row) for row in c.fetchall()]
            
            conn.close()
        except Exception as e:
            total_buku = 14155
            total_bersampul = 3097
            bersampul_pct = 21.9
            total_eksemplar = 14155
            kunjungan_today = 0
            dibaca_today = 0
            pinjam_aktif = 0
            jatuh_tempo = 0
            populer = []
            recent_reads = []
            recent_loans = []

        return render_template('index.html',
            total_buku=total_buku,
            total_bersampul=total_bersampul,
            bersampul_pct=bersampul_pct,
            total_eksemplar=total_eksemplar,
            kunjungan_today=kunjungan_today,
            dibaca_today=dibaca_today,
            pinjam_aktif=pinjam_aktif,
            jatuh_tempo=jatuh_tempo,
            populer=populer,
            recent_reads=recent_reads,
            recent_loans=recent_loans
        )
    return redirect(url_for('opac_page'))


@router.route('/export_baca')
@login_required
def export_baca():
    conn = database.get_db_connection()
    # Export by current month if needed, but for now we export all grouped by klasifikasi
    data = conn.execute('''
        SELECT b.klasifikasi, b.no_induk, b.judul, b.pengarang, COUNT(bd.id) as total_baca 
        FROM buku_dibaca bd 
        JOIN buku b ON bd.no_induk = b.no_induk 
        GROUP BY b.no_induk 
        ORDER BY b.klasifikasi ASC, total_baca DESC
    ''').fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(['Klasifikasi DDC', 'No Induk', 'Judul Buku', 'Pengarang', 'Total Dibaca'])
    
    for row in data:
        writer.writerow([row['klasifikasi'], row['no_induk'], row['judul'], row['pengarang'], row['total_baca']])
        
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        mimetype="text/csv",
        as_attachment=True,
        download_name="Laporan_Baca_Buku.csv"
    )


@router.route('/scan_baca/scanner')
@login_required
def scan_baca_scanner():
    return render_template('scan_baca_scanner.html')


@router.route('/scan')
@login_required
def scan():
    conn = database.get_db_connection()
    # Fetch recently read books today
    recent = conn.execute('''
        SELECT b.judul, e.no_induk, bd.tanggal as waktu
        FROM buku_dibaca bd
        JOIN eksemplar e ON bd.no_induk = e.no_induk
        JOIN bibliografi b ON e.biblio_id = b.id
        WHERE date(bd.tanggal) = date('now', 'localtime')
        ORDER BY bd.tanggal DESC LIMIT 20
    ''').fetchall()
    conn.close()
    return render_template('scan_baca.html', recent=recent)


@router.route('/api/scan', methods=['POST'])
def api_scan():
    data = request.json
    no_induk = data.get('no_induk')
    mode = data.get('mode', 'baca')
    target_rak = data.get('target_rak')
    
    if not no_induk:
        return jsonify({'status': 'error', 'message': 'Barcode kosong'})
        
    conn = database.get_db_connection()
    
    # Gunakan eksemplar JOIN bibliografi
    buku = conn.execute('''
        SELECT e.no_induk, b.judul, b.klasifikasi as subjek 
        FROM eksemplar e 
        JOIN bibliografi b ON e.biblio_id = b.id 
        WHERE e.no_induk = ?
    ''', (no_induk,)).fetchone()
    
    if not buku:
        conn.close()
        return jsonify({'status': 'error', 'message': f'Buku dengan Barcode {no_induk} tidak ditemukan.'})
        
    if mode == 'audit_rak':
        if not target_rak:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Target rak tidak dipilih'})
            
        ddc = str(buku['subjek'] or '')
        status_audit = 'ANOMALI'
        
        # Validasi berdasarkan digit pertama DDC
        if ddc.strip():
            digit_pertama = ddc.strip()[0]
            target_digit = target_rak[0]
            if digit_pertama == target_digit:
                status_audit = 'BENAR'
            else:
                status_audit = 'SALAH RAK'
                
        conn.execute('INSERT INTO audit_rak (no_induk, rak_target, status_audit) VALUES (?, ?, ?)', 
                     (no_induk, target_rak, status_audit))
        conn.commit()
        conn.close()
        # Sesuaikan dengan ekspektasi frontend (success, danger, warning)
        response_status = 'success'
        message = f"Buku {buku['judul']} benar berada di rak ini."
        
        if status_audit == 'SALAH RAK':
            response_status = 'danger'
            message = f"Buku {buku['judul']} (DDC {ddc}) SEHARUSNYA di rak lain!"
        elif status_audit == 'ANOMALI':
            response_status = 'warning'
            message = f"DDC untuk buku {buku['judul']} tidak jelas/kosong."
            
        return jsonify({
            'status': response_status, 
            'judul': buku['judul'], 
            'subjek': ddc,
            'status_audit': status_audit,
            'message': message
        })
    else:
        # Mode Baca
        conn.execute('INSERT OR REPLACE INTO buku_dibaca (no_induk) VALUES (?)', (no_induk,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'judul': buku['judul'], 'subjek': buku['subjek']})


@router.route('/kiosk')
def kiosk():
    # Halaman Kiosk bersifat publik (tanpa login_required) untuk ditaruh di lobi
    return render_template('kiosk.html')


@router.route('/api/kunjungan/autocomplete', methods=['GET'])
def kunjungan_autocomplete():
    q = request.args.get('q', '').strip()
    if not q or len(q) < 3:
        return jsonify([])
        
    conn = database.get_db_connection()
    try:
        results = conn.execute("""
            SELECT identitas, asal_instansi, peran_jabatan, fakultas 
            FROM sjla_visitor_logs 
            WHERE tipe_pengunjung = 'Non-Member' AND identitas LIKE ?
            GROUP BY identitas
            LIMIT 10
        """, ('%' + q + '%',)).fetchall()
        
        suggestions = [dict(row) for row in results]
        return jsonify(suggestions)
    finally:
        conn.close()


@router.route('/api/kunjungan', methods=['POST'])
def submit_kunjungan():
    data = request.json
    tipe_pengunjung = data.get('tipe_pengunjung', 'Non-Member')
    identitas = data.get('identitas', '').strip()
    asal_instansi = data.get('asal_instansi', '').strip()
    peran = data.get('peran_jabatan', '').strip()
    
    if not identitas:
        return jsonify({"status": "error", "message": "Identitas tidak boleh kosong!"}), 400
        
    conn = database.get_db_connection()
    try:
        # Safeguard if column doesn't exist for some reason
        try:
            conn.execute("ALTER TABLE sjla_visitor_logs ADD COLUMN peran_jabatan TEXT")
        except:
            pass
            
        member_name = None
        if tipe_pengunjung == 'Member':
            # Verifikasi ID di tabel anggota
            member = conn.execute("SELECT nama, institusi FROM anggota WHERE member_id = ?", (identitas,)).fetchone()
            if member:
                member_name = member['nama']
                asal_instansi = member['institusi'] or 'Internal'
            else:
                return jsonify({"status": "error", "message": f"Member dengan ID {identitas} tidak ditemukan."}), 404
        
        # Simpan log
        fakultas = data.get('fakultas', '').strip()
        try:
            conn.execute("ALTER TABLE sjla_visitor_logs ADD COLUMN fakultas TEXT")
        except:
            pass
            
        conn.execute("""
            INSERT INTO sjla_visitor_logs (tipe_pengunjung, identitas, asal_instansi, peran_jabatan, fakultas)
            VALUES (?, ?, ?, ?, ?)
        """, (tipe_pengunjung, identitas, asal_instansi, peran, fakultas))
        conn.commit()
        
        display_name = member_name if member_name else identitas
        return jsonify({
            "status": "success", 
            "message": f"Selamat datang, {display_name}!",
            "display_name": display_name
        })
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


@router.route('/sirkulasi')
@login_required
def sirkulasi():
    conn = database.get_db_connection()
    try:
        total_active = conn.execute("SELECT COUNT(*) FROM sirkulasi WHERE return_date IS NULL").fetchone()[0] or 0
        total_returned = conn.execute("SELECT COUNT(*) FROM sirkulasi WHERE return_date IS NOT NULL").fetchone()[0] or 0
        total_overdue = conn.execute("SELECT COUNT(*) FROM sirkulasi WHERE return_date IS NULL AND due_date < date('now', 'localtime')").fetchone()[0] or 0
        unpaid_fines = conn.execute("SELECT COALESCE(SUM(fine_amount), 0) FROM sirkulasi WHERE fine_status = 'UNPAID'").fetchone()[0] or 0
        
        stats = {
            'total_active': total_active,
            'total_returned': total_returned,
            'total_overdue': total_overdue,
            'unpaid_fines': unpaid_fines
        }
        
        rc_rows = conn.execute("""
            SELECT 
                s.id, s.member_id, s.no_induk, s.loan_date, s.due_date, s.return_date, s.fine_amount, s.fine_status,
                COALESCE(a.nama, s.member_id) as nama_anggota,
                COALESCE(b.judul, 'Buku #' || s.no_induk) as judul,
                CASE WHEN s.return_date IS NOT NULL THEN 'KEMBALI' ELSE 'PINJAM' END as tipe_transaksi
            FROM sirkulasi s
            LEFT JOIN anggota a ON s.member_id = a.member_id
            LEFT JOIN eksemplar e ON s.no_induk = e.no_induk
            LEFT JOIN bibliografi b ON e.biblio_id = b.id
            ORDER BY s.id DESC
            LIMIT 7
        """).fetchall()
        recent_circulations = [dict(r) for r in rc_rows]
    except Exception as e:
        stats = {'total_active': 0, 'total_returned': 0, 'total_overdue': 0, 'unpaid_fines': 0}
        recent_circulations = []
    finally:
        conn.close()

    return render_template(
        'sirkulasi.html', 
        page_title="Sirkulasi & Kasir",
        stats=stats,
        recent_circulations=recent_circulations
    )


@router.route('/api/sirkulasi/member/<path:member_id>', methods=['GET'])
@api_login_required
def api_get_member(member_id):
    conn = database.get_db_connection()
    member = conn.execute("SELECT * FROM anggota WHERE member_id = ?", (member_id,)).fetchone()
    if not member:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Anggota tidak ditemukan'})
        
    m = dict(member)
    
    # Hitung pinjaman aktif (buku yang belum dikembalikan)
    active_loans = conn.execute("SELECT COUNT(*) FROM sirkulasi WHERE member_id = ? AND return_date IS NULL", (member_id,)).fetchone()[0]
    
    # Hitung batas maksimal
    tipe = m.get('tipe_anggota', 'Reguler')
    if tipe is None:
        tipe = 'Reguler'
    max_loans = 4 if 'Skripsi' in tipe else 2
    
    # Cek suspend
    import datetime
    if m.get('status') == 'DIBLOKIR' and m.get('suspended_until'):
        suspend_date = datetime.datetime.strptime(m.get('suspended_until'), '%Y-%m-%d').date()
        today = datetime.datetime.now().date()
        if today >= suspend_date:
            conn.execute("UPDATE anggota SET status = ?, suspended_until = ? WHERE member_id = ?", ('AKTIF', None, member_id))
            conn.commit()
            m['status'] = 'AKTIF'
            m['suspended_until'] = None
    
    # Fetch active loans detail
    loans_cursor = conn.execute("""
        SELECT s.*, e.no_induk, b.judul 
        FROM sirkulasi s
        JOIN eksemplar e ON s.no_induk = e.no_induk
        JOIN bibliografi b ON e.biblio_id = b.id
        WHERE s.member_id = ? AND s.return_date IS NULL
    """, (member_id,))
    loans = [dict(row) for row in loans_cursor.fetchall()]

    # Fetch history loans (already returned)
    history_cursor = conn.execute("""
        SELECT s.*, e.no_induk, b.judul 
        FROM sirkulasi s
        JOIN eksemplar e ON s.no_induk = e.no_induk
        JOIN bibliografi b ON e.biblio_id = b.id
        WHERE s.member_id = ? AND s.return_date IS NOT NULL
        ORDER BY s.return_date DESC
    """, (member_id,))
    history_loans = [dict(row) for row in history_cursor.fetchall()]

    conn.close()
    return jsonify({
        'status': 'success',
        'data': {
            'id': m['member_id'],
            'nama': m['nama'],
            'tipe': tipe,
            'status': m['status'],
            'email': m.get('email', '-'),
            'masa_berlaku': m.get('masa_berlaku', '2027-05-07'),
            'suspended_until': m.get('suspended_until', '-'),
            'active_loans': active_loans,
            'max_loans': max_loans,
            'loans': loans,
            'history_loans': history_loans
        }
    })


@router.route('/api/sirkulasi/pay_fine', methods=['POST'])
@api_login_required
def api_pay_fine():
    data = request.json
    loan_id = data.get('loan_id')
    
    conn = database.get_db_connection()
    loan = conn.execute("SELECT * FROM sirkulasi WHERE id = ?", (loan_id,)).fetchone()
    
    if not loan:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Data peminjaman tidak ditemukan.'})
        
    conn.execute("UPDATE sirkulasi SET fine_status = 'LUNAS' WHERE id = ?", (loan_id,))
    
    # Check if user has any OTHER unpaid fines
    unpaid = conn.execute("SELECT COUNT(*) FROM sirkulasi WHERE member_id = ? AND fine_amount > 0 AND fine_status = 'BELUM_LUNAS'", (loan['member_id'],)).fetchone()[0]
    
    if unpaid == 0:
        # Unblock user
        conn.execute("UPDATE anggota SET status = 'AKTIF', suspended_until = NULL WHERE member_id = ?", (loan['member_id'],))
        
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'message': 'Denda lunas.'})


@router.route('/api/sirkulasi/borrow', methods=['POST'])
@api_login_required
def api_borrow():
    data = request.json
    member_id = data.get('member_id')
    book_id = data.get('book_id')
    
    conn = database.get_db_connection()
    eksemplar = conn.execute("SELECT e.*, b.judul FROM eksemplar e JOIN bibliografi b ON e.biblio_id = b.id WHERE e.no_induk = ?", (book_id,)).fetchone()
    
    if not eksemplar:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Buku dengan Barcode tersebut tidak ditemukan'})
        
    eksemplar = dict(eksemplar)
    if eksemplar.get('is_reference_only'):
        conn.close()
        return jsonify({'status': 'error', 'message': 'Buku ini adalah buku Referensi dan TIDAK BOLEH dipinjam.'})
    if eksemplar.get('status_buku') == 'DIPINJAM':
        conn.close()
        return jsonify({'status': 'error', 'message': 'Buku saat ini sedang berstatus dipinjam orang lain.'})
        
    import datetime
    loan_date = datetime.datetime.now()
    due_date = loan_date + datetime.timedelta(days=14)
    
    conn.execute("INSERT INTO sirkulasi (member_id, no_induk, loan_date, due_date) VALUES (?, ?, ?, ?)", (member_id, book_id, loan_date.strftime('%Y-%m-%d %H:%M:%S'), due_date.strftime('%Y-%m-%d %H:%M:%S')))
    conn.execute("UPDATE eksemplar SET status_buku = 'DIPINJAM', status_ketersediaan = 'Dipinjam' WHERE no_induk = ?", (book_id,))
    
    conn.commit()
    conn.close()
    
    wa_msg = f"?? *STRUK PEMINJAMAN ST. JEROME*\n\n"
    wa_msg += f"Halo, Anda baru saja meminjam buku:\n"
    wa_msg += f"?? *{eksemplar.get('judul')}*\n"
    wa_msg += f"Barcode: {book_id}\n"
    wa_msg += f"Tanggal Kembali: *{due_date.strftime('%d-%m-%Y')}*\n\n"
    wa_msg += "Harap kembalikan tepat waktu untuk menghindari denda. Terima kasih!"
    send_wa_notification(member_id, wa_msg)
    
    return jsonify({'status': 'success', 'data': {'buku_judul': eksemplar.get('judul')}})


@router.route('/api/sirkulasi/renew', methods=['POST'])
@api_login_required
def api_renew():
    data = request.json
    book_id = data.get('book_id')
    
    conn = database.get_db_connection()
    loan = conn.execute("""
        SELECT s.*, s.member_id as e_member_id, a.tipe_anggota 
        FROM sirkulasi s 
        JOIN eksemplar e ON s.no_induk = e.no_induk 
        LEFT JOIN anggota a ON s.member_id = a.member_id 
        WHERE s.no_induk = ? AND s.return_date IS NULL
    """, (book_id,)).fetchone()
    
    if not loan:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Tidak ada peminjaman aktif untuk buku ini.'})
        
    loan = dict(loan)
    tipe = loan.get('tipe_anggota', 'Reguler')
    if tipe is None:
        tipe = 'Reguler'
        
    # Check max renewals (Reguler = 2, Skripsi = unlimited)
    member = conn.execute("SELECT is_tesis FROM anggota WHERE member_id = ?", (loan['member_id'],)).fetchone()
    is_tesis = member['is_tesis'] if member else 0
    
    if not is_tesis and loan['renewal_count'] >= 1:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Batas maksimal perpanjangan (1 kali) telah tercapai untuk anggota non-tesis/skripsi.'})
        
    import datetime
    # Perpanjang 14 hari dari HARI INI
    new_due_date = datetime.datetime.now() + datetime.timedelta(days=14)
    new_count = loan['renewal_count'] + 1
    
    conn.execute("UPDATE sirkulasi SET due_date = ?, renewal_count = ? WHERE id = ?", (new_due_date.strftime('%Y-%m-%d %H:%M:%S'), new_count, loan['id']))
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'message': 'Berhasil diperpanjang 14 hari.', 'new_due_date': new_due_date.strftime('%Y-%m-%d')})


@router.route('/api/kiosk_pinjam', methods=['POST'])
def api_kiosk_pinjam():
    data = request.json
    member_id = data.get('member_id')
    no_induk = data.get('no_induk')
    
    if not member_id or not no_induk:
        return jsonify({'status': 'error', 'message': 'Data tidak lengkap.'})
        
    conn = database.get_db_connection()
    member = conn.execute("SELECT * FROM anggota WHERE member_id = ?", (member_id,)).fetchone()
    if not member:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Anggota tidak ditemukan.'})
        
    if member['status'] != 'AKTIF':
        conn.close()
        return jsonify({'status': 'error', 'message': 'Kartu anggota sedang tidak aktif.'})
        
    # Check max loans
    is_tesis = member['is_tesis']
    max_loans = 4 if is_tesis else 2
    
    current_loans = conn.execute("SELECT COUNT(*) FROM sirkulasi WHERE member_id = ? AND return_date IS NULL", (member_id,)).fetchone()[0]
    if current_loans >= max_loans:
        conn.close()
        return jsonify({'status': 'error', 'message': f'Batas pinjaman maksimal tercapai ({max_loans} buku).'})
        
    # Check fines
    fines = conn.execute("SELECT SUM(fine_amount) FROM sirkulasi WHERE member_id = ? AND fine_status = 'BELUM_LUNAS'", (member_id,)).fetchone()[0] or 0
    if fines > 0:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Anda memiliki denda yang belum lunas. Selesaikan denda di petugas terlebih dahulu.'})
        
    # Check if book exists and available
    book = conn.execute("SELECT e.*, b.judul FROM eksemplar e JOIN bibliografi b ON e.biblio_id = b.id WHERE e.no_induk = ?", (no_induk,)).fetchone()
    if not book:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Buku tidak ditemukan di katalog.'})
        
    is_borrowed = conn.execute("SELECT id FROM sirkulasi WHERE no_induk = ? AND return_date IS NULL", (no_induk,)).fetchone()
    if is_borrowed:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Buku ini sedang dipinjam oleh orang lain.'})
        
    # Process loan
    import datetime
    loan_date = datetime.datetime.now()
    due_date = loan_date + datetime.timedelta(days=14)
    
    conn.execute("INSERT INTO sirkulasi (member_id, no_induk, loan_date, due_date) VALUES (?, ?, ?, ?)",
                 (member_id, no_induk, loan_date.strftime('%Y-%m-%d %H:%M:%S'), due_date.strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()
    
    # Kirim Struk Digital WA
    wa_msg = f"?? *STRUK PEMINJAMAN ST. JEROME*\n\n"
    wa_msg += f"Halo {member['nama']}, Anda baru saja meminjam buku:\n"
    wa_msg += f"?? *{book['judul']}*\n"
    wa_msg += f"Barcode: {no_induk}\n"
    wa_msg += f"Tanggal Kembali: *{due_date.strftime('%d-%m-%Y')}*\n\n"
    wa_msg += "Harap kembalikan tepat waktu untuk menghindari denda. Terima kasih!"
    
    send_wa_notification(member_id, wa_msg)
    
    return jsonify({
        'status': 'success',
        'judul': book['judul'],
        'due_date': due_date.strftime('%Y-%m-%d')
    })


@router.route('/api/sirkulasi/return', methods=['POST'])
@api_login_required
def api_return():
    data = request.json
    book_id = data.get('book_id')
    
    conn = database.get_db_connection()
    try:
        loan = conn.execute("SELECT s.*, s.member_id as e_member_id, a.nama as anggota_nama, b.judul, e.biblio_id FROM sirkulasi s JOIN eksemplar e ON s.no_induk = e.no_induk JOIN bibliografi b ON e.biblio_id = b.id LEFT JOIN anggota a ON s.member_id = a.member_id WHERE s.no_induk = ? AND s.return_date IS NULL", (book_id,)).fetchone()
    
        if not loan:
            return jsonify({'status': 'error', 'message': 'Tidak ada catatan peminjaman aktif untuk buku ini.'})
        
        loan = dict(loan)
        import datetime
    
        return_datetime = datetime.datetime.now()
        due_str = str(loan['due_date']).strip()
        try:
            if ' ' in due_str:
                due_datetime = datetime.datetime.strptime(due_str, '%Y-%m-%d %H:%M:%S')
            else:
                due_datetime = datetime.datetime.strptime(due_str, '%Y-%m-%d')
        except Exception:
            due_datetime = datetime.datetime.strptime(due_str[:10], '%Y-%m-%d')
    
        denda = 0
        terlambat_hari = 0
    
        if return_datetime.date() > due_datetime.date():
            terlambat_hari = calculate_working_days(due_datetime.date(), return_datetime.date(), conn)
            denda = terlambat_hari * 500
        
        conn.execute("UPDATE sirkulasi SET return_date = ?, fine_amount = ?, fine_status = ? WHERE id = ?", (return_datetime.strftime('%Y-%m-%d %H:%M:%S'), denda, 'BELUM_LUNAS' if denda > 0 else 'LUNAS', loan['id']))
        conn.execute("UPDATE eksemplar SET status_buku = 'TERSEDIA', status_ketersediaan = 'Tersedia' WHERE no_induk = ?", (book_id,))
    
        # CEK ANTRIAN RESERVASI
        reservasi = conn.execute("SELECT r.id, r.nim_pemustaka as member_id, a.telepon, a.nama FROM reservasi r JOIN anggota a ON r.nim_pemustaka = a.member_id WHERE r.biblio_id = ? AND r.status = 'Aktif' ORDER BY r.tanggal ASC LIMIT 1", (loan['biblio_id'],)).fetchone()
        if reservasi:
            # Ubah status reservasi menjadi TERSEDIA
            conn.execute("UPDATE reservasi SET status = 'Tersedia' WHERE id = ?", (reservasi['id'],))
            # Kirim notifikasi WA
            if reservasi['telepon']:
                res_msg = f" *NOTIFIKASI ANTRIAN BUKU*\n\n"
                res_msg += f"Halo {reservasi['nama']}, buku yang Anda antre:\n"
                res_msg += f" *{loan['judul']}*\n\n"
                res_msg += f"Saat ini SUDAH TERSEDIA dan siap dipinjam! Silakan ambil di Perpustakaan St. Jerome.\nTerima Kasih"
                send_wa_notification(reservasi['member_id'], res_msg)
    
        if denda > 0:
            suspend_until = return_datetime.date() + datetime.timedelta(days=1)
            conn.execute("UPDATE anggota SET status = 'DIBLOKIR', suspended_until = ? WHERE member_id = ?", (suspend_until.strftime('%Y-%m-%d'), loan['member_id']))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
    conn.close()
    
    wa_msg = f" *STRUK PENGEMBALIAN ST. JEROME*\n\n"
    wa_msg += f"Halo, Anda telah mengembalikan buku:\n"
    wa_msg += f" *{loan['judul']}*\n"
    wa_msg += f"Barcode: {book_id}\n"
    if denda > 0:
        wa_msg += f"Keterlambatan: {terlambat_hari} hari\n"
        wa_msg += f"Denda: *Rp{denda}* (Status: BELUM LUNAS)\n"
    else:
        wa_msg += f"Denda: Rp0 (Tepat Waktu)\n"
    wa_msg += "\nTerima kasih telah mengembalikan buku!"
    send_wa_notification(loan['member_id'], wa_msg)
    
    return jsonify({
        'status': 'success',
        'data': {
            'buku_judul': loan.get('judul'),
            'anggota_nama': loan.get('anggota_nama'),
            'denda': denda,
            'terlambat_hari': terlambat_hari
        }
    })


@router.route('/sirkulasi/cetak_struk/<member_id>')
def cetak_struk(member_id):
    conn = database.get_db_connection()
    member = conn.execute("SELECT * FROM anggota WHERE member_id = ?", (member_id,)).fetchone()
    if not member:
        conn.close()
        return "Anggota tidak ditemukan", 404
        
    # Get active loans
    loans = conn.execute("SELECT s.*, b.judul FROM sirkulasi s JOIN eksemplar e ON s.no_induk = e.no_induk JOIN bibliografi b ON e.biblio_id = b.id WHERE s.member_id = ? AND s.return_date IS NULL ORDER BY s.loan_date DESC", (member_id,)).fetchall()
    conn.close()
    
    return render_template('cetak_struk.html', member=dict(member), loans=[dict(l) for l in loans])


@router.route('/cek_pinjaman/<member_id>')
def cek_pinjaman(member_id):
    conn = database.get_db_connection()
    member = conn.execute("SELECT * FROM anggota WHERE member_id = ?", (member_id,)).fetchone()
    if not member:
        conn.close()
        return "Anggota tidak ditemukan", 404
        
    loans = conn.execute("SELECT s.*, b.judul, b.image, b.pengarang FROM sirkulasi s JOIN eksemplar e ON s.no_induk = e.no_induk JOIN bibliografi b ON e.biblio_id = b.id WHERE s.member_id = ? AND s.return_date IS NULL ORDER BY s.due_date ASC", (member_id,)).fetchall()
    
    # Check fines
    fines = conn.execute("SELECT SUM(fine_amount) FROM sirkulasi WHERE member_id = ? AND fine_status = 'BELUM_LUNAS'", (member_id,)).fetchone()[0] or 0
    
    conn.close()
    return render_template('cek_pinjaman.html', member=dict(member), loans=[dict(l) for l in loans], total_denda=fines)


@router.route('/self_checkout/<member_id>')
def self_checkout(member_id):
    conn = database.get_db_connection()
    member = conn.execute("SELECT * FROM anggota WHERE member_id = ?", (member_id,)).fetchone()
    if not member:
        conn.close()
        return "Anggota tidak ditemukan", 404
        
    loans = conn.execute("SELECT s.*, b.judul, b.image, b.pengarang FROM sirkulasi s JOIN eksemplar e ON s.no_induk = e.no_induk JOIN bibliografi b ON e.biblio_id = b.id WHERE s.member_id = ? AND s.return_date IS NULL ORDER BY s.due_date ASC", (member_id,)).fetchall()
    
    # Check fines
    fines = conn.execute("SELECT SUM(fine_amount) FROM sirkulasi WHERE member_id = ? AND fine_status = 'BELUM_LUNAS'", (member_id,)).fetchone()[0] or 0
    
    conn.close()
    return render_template('self_checkout.html', member=dict(member), loans=[dict(l) for l in loans], total_denda=fines)


@router.route('/kiosk_sirkulasi')
def kiosk_sirkulasi():
    return render_template('kiosk_sirkulasi.html')


@router.route('/sirkulasi/cetak_struk_kembali/<member_id>')
@login_required
def cetak_struk_kembali(member_id):
    conn = database.get_db_connection()
    member = conn.execute("SELECT * FROM anggota WHERE member_id = ?", (member_id,)).fetchone()
    if not member:
        conn.close()
        return "Anggota tidak ditemukan", 404
        
    # Ambil buku yang dikembalikan HARI INI oleh member ini
    returns = conn.execute("SELECT s.*, b.judul FROM sirkulasi s JOIN eksemplar e ON s.no_induk = e.no_induk JOIN bibliografi b ON e.biblio_id = b.id WHERE s.member_id = ? AND s.return_date IS NOT NULL AND date(s.return_date) = date('now', 'localtime') ORDER BY s.return_date DESC", (member_id,)).fetchall()
    conn.close()
    
    return render_template('cetak_struk_kembali.html', member=dict(member), returns=[dict(r) for r in returns])


@router.route('/api/visitors/recent', methods=['GET'])
def api_visitors_recent():
    limit = request.args.get('limit', default=100, type=int)
    search = request.args.get('q', default='', type=str).strip()
    conn = database.get_db_connection()
    try:
        import datetime
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        today_count = conn.execute("SELECT COUNT(*) FROM sjla_visitor_logs WHERE date(waktu_kunjungan) = ?", (today_str,)).fetchone()[0]
        total_count = conn.execute("SELECT COUNT(*) FROM sjla_visitor_logs").fetchone()[0]

        if search:
            query = """
                SELECT id, waktu_kunjungan, identitas, tipe_pengunjung, asal_instansi, peran_jabatan, fakultas
                FROM sjla_visitor_logs
                WHERE identitas LIKE ? OR asal_instansi LIKE ? OR fakultas LIKE ? OR peran_jabatan LIKE ?
                ORDER BY waktu_kunjungan DESC, id DESC
                LIMIT ?
            """
            pattern = f"%{search}%"
            rows = conn.execute(query, (pattern, pattern, pattern, pattern, limit)).fetchall()
        else:
            query = """
                SELECT id, waktu_kunjungan, identitas, tipe_pengunjung, asal_instansi, peran_jabatan, fakultas
                FROM sjla_visitor_logs
                ORDER BY waktu_kunjungan DESC, id DESC
                LIMIT ?
            """
            rows = conn.execute(query, (limit,)).fetchall()

        return jsonify({
            'status': 'success',
            'total': total_count,
            'today': today_count,
            'data': [dict(r) for r in rows]
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        conn.close()


@router.route('/api/visitors/import_csv', methods=['POST'])
@login_required
def api_visitors_import_csv():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'Tidak ada file yang diunggah.'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'Pilih file CSV terlebih dahulu.'}), 400

    try:
        import csv
        import re
        import io

        BULAN_MAP = {
            'januari': 1, 'februari': 2, 'maret': 3, 'april': 4,
            'mei': 5, 'juni': 6, 'juli': 7, 'agustus': 8,
            'september': 9, 'oktober': 10, 'november': 11, 'desember': 12
        }

        def parse_waktu_str(w):
            cleaned = re.sub(r'\s+', ' ', w.strip())
            m = re.search(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s+(\d{1,2})[.:](\d{1,2})', cleaned)
            if m:
                day, mon_str, year, hr, minute = m.groups()
                mon = BULAN_MAP.get(mon_str.lower(), 1)
                return f"{year}-{int(mon):02d}-{int(day):02d} {int(hr):02d}:{int(minute):02d}:00"
            return None

        # Read content
        content = file.read().decode('utf-8', errors='ignore')
        reader = csv.reader(io.StringIO(content), delimiter=';')
        
        # Check if delimiter is comma instead of semicolon
        rows = list(reader)
        if len(rows) > 0 and len(rows[0]) <= 2:
            reader = csv.reader(io.StringIO(content), delimiter=',')
            rows = list(reader)

        if len(rows) <= 1:
            return jsonify({'status': 'error', 'message': 'File CSV kosong atau format tidak sesuai.'}), 400

        conn = database.get_db_connection()
        c = conn.cursor()

        # Existing signatures (identitas + waktu_kunjungan)
        c.execute("SELECT identitas, waktu_kunjungan FROM sjla_visitor_logs")
        existing_signatures = {f"{r['identitas']}|{r['waktu_kunjungan']}" for r in c.fetchall()}

        new_count = 0
        to_insert = []

        # Skip header
        for row in rows[1:]:
            if not row or len(row) < 5:
                continue
            nama = row[1].strip()
            tipe = row[2].strip() if len(row) > 2 else 'Umum'
            instansi = row[3].strip() if len(row) > 3 else ''
            peran = row[4].strip() if len(row) > 4 else ''
            fakultas = row[5].strip() if len(row) > 5 else ''
            waktu_raw = row[6].strip() if len(row) > 6 else ''
            waktu = parse_waktu_str(waktu_raw) or waktu_raw

            sig = f"{nama}|{waktu}"
            if sig not in existing_signatures:
                to_insert.append((waktu, tipe, nama, instansi, 'Kunjungan Perpustakaan', peran, fakultas))
                existing_signatures.add(sig)
                new_count += 1

        if to_insert:
            c.executemany("""
                INSERT INTO sjla_visitor_logs (waktu_kunjungan, tipe_pengunjung, identitas, asal_instansi, tujuan_kunjungan, peran_jabatan, fakultas)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, to_insert)
            conn.commit()

        c.execute("SELECT COUNT(id) FROM sjla_visitor_logs")
        total_now = c.fetchone()[0]
        conn.close()

        return jsonify({
            'status': 'success',
            'message': f'Berhasil! {new_count} data baru ditambahkan.',
            'new_count': new_count,
            'total_count': total_now
        })

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Gagal memproses file: {str(e)}'}), 500


@router.route('/sirkulasi/bebas_pustaka/<path:member_id>')
@login_required
def bebas_pustaka_view(member_id):
    member_id = member_id.strip()
    conn = database.get_db_connection()
    c = conn.cursor()

    # 1. Cari data anggota
    c.execute("SELECT * FROM anggota WHERE member_id = ?", (member_id,))
    member_row = c.fetchone()

    if not member_row:
        # Cek di tabel peminjaman apakah ada nama
        c.execute("SELECT member_name FROM peminjaman WHERE member_id = ? LIMIT 1", (member_id,))
        p_row = c.fetchone()
        member = {
            'member_id': member_id,
            'nama': p_row['member_name'] if p_row else member_id,
            'tipe_anggota': 'Mahasiswa',
            'institusi': get_branding()['default_lokasi'],
            'prodi': 'Filsafat Keilahian / Teologi'
        }
    else:
        member = dict(member_row)
        if not member.get('prodi'):
            member['prodi'] = 'Filsafat Keilahian / Teologi'

    # 2. Cek apakah ada tanggungan pinjaman aktif (buku belum kembali)
    c.execute("""
        SELECT s.no_induk, s.loan_date, s.due_date, s.fine_amount, b.judul 
        FROM sirkulasi s
        LEFT JOIN eksemplar e ON s.no_induk = e.no_induk
        LEFT JOIN bibliografi b ON e.biblio_id = b.id
        WHERE s.member_id = ? AND (s.return_date IS NULL OR s.return_date = '')
    """, (member_id,))
    active_loans = c.fetchall()

    c.execute("""
        SELECT no_induk, judul, loan_date, due_date 
        FROM peminjaman 
        WHERE member_id = ? AND loan_status IN ('0', 'On Loan')
    """, (member_id,))
    peminjaman_active = c.fetchall()

    # 3. Cek denda belum lunas
    c.execute("""
        SELECT SUM(fine_amount) as total_denda 
        FROM sirkulasi 
        WHERE member_id = ? AND fine_status = 'BELUM_LUNAS'
    """, (member_id,))
    fine_row = c.fetchone()
    total_denda = fine_row['total_denda'] if (fine_row and fine_row['total_denda']) else 0

    has_unreturned = (len(active_loans) > 0) or (len(peminjaman_active) > 0)
    has_unpaid_fines = total_denda > 0

    if has_unreturned or has_unpaid_fines:
        unreturned_list = []
        for l in active_loans:
            unreturned_list.append({
                'no_induk': l['no_induk'],
                'judul': l['judul'] or l['no_induk'],
                'due_date': l['due_date']
            })
        for l in peminjaman_active:
            if not any(x['no_induk'] == l['no_induk'] for x in unreturned_list):
                unreturned_list.append({
                    'no_induk': l['no_induk'],
                    'judul': l['judul'],
                    'due_date': l['due_date']
                })

        conn.close()
        return render_template('blokir_bebas_pustaka.html',
                               member=member,
                               unreturned_books=unreturned_list,
                               total_denda=total_denda), 400

    now = datetime.datetime.now()
    bulan_romawi = ['', 'I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X', 'XI', 'XII']
    bulan_indo = ['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']
    
    default_lokasi = get_branding()['default_lokasi']
    seq_num = (abs(hash(member_id)) % 900) + 100
    nomor_surat = f"{seq_num}/BP-SJLA/{default_lokasi}/{bulan_romawi[now.month]}/{now.year}"
    tanggal_hari_ini = f"{now.day} {bulan_indo[now.month]} {now.year}"

    keperluan = request.args.get('keperluan', 'Penyelesaian Studi / Ujian Komprehensif S.T.B. & Wisuda Sarjana Teologi')
    semester = request.args.get('semester', 'Semester Genap 2026/2027')

    qr_data = f"VALID:SJLA-{default_lokasi}|{nomor_surat}|{member_id}|{member.get('nama')}|BEBAS_TANGGUNGAN|{now.strftime('%Y%m%d')}"

    conn.close()
    return render_template('cetak_bebas_pustaka.html',
                           member=member,
                           nomor_surat=nomor_surat,
                           tanggal_hari_ini=tanggal_hari_ini,
                           keperluan=keperluan,
                           semester=semester,
                           qr_data=qr_data)


@router.route('/api/sirkulasi/cek_bebas_pustaka/<path:member_id>')
@login_required
def api_cek_bebas_pustaka(member_id):
    member_id = member_id.strip()
    conn = database.get_db_connection()
    c = conn.cursor()
    
    c.execute("""
        SELECT COUNT(*) as c FROM sirkulasi 
        WHERE member_id = ? AND (return_date IS NULL OR return_date = '')
    """, (member_id,))
    sirk_active = c.fetchone()['c']

    c.execute("""
        SELECT COUNT(*) as c FROM peminjaman 
        WHERE member_id = ? AND loan_status IN ('0', 'On Loan')
    """, (member_id,))
    pem_active = c.fetchone()['c']

    c.execute("""
        SELECT SUM(fine_amount) as total_denda FROM sirkulasi 
        WHERE member_id = ? AND fine_status = 'BELUM_LUNAS'
    """, (member_id,))
    f_row = c.fetchone()
    denda = f_row['total_denda'] if (f_row and f_row['total_denda']) else 0

    conn.close()

    eligible = (sirk_active == 0) and (pem_active == 0) and (denda == 0)
    return jsonify({
        'status': 'success',
        'member_id': member_id,
        'eligible': eligible,
        'unreturned_books': max(sirk_active, pem_active),
        'unpaid_fines': denda
    })


@router.route('/sirkulasi/keterlambatan')
@login_required
def sirkulasi_keterlambatan_view():
    return render_template('keterlambatan.html')


@router.route('/api/sirkulasi/terlambat')
@login_required
def api_sirkulasi_terlambat():
    try:
        conn = database.get_db_connection()
        c = conn.cursor()
        
        c.execute("""
            SELECT s.id, s.member_id, 
                   COALESCE(a.nama, p.member_name, s.member_id) as nama,
                   COALESCE(a.telepon, '') as telepon,
                   COALESCE(a.tipe_anggota, 'Mahasiswa') as tipe_anggota,
                   s.no_induk,
                   COALESCE(b.judul, p.judul, s.no_induk) as judul,
                   COALESCE(b.pengarang, '') as pengarang,
                   COALESCE(b.image, '') as image,
                   COALESCE(b.klasifikasi, '') as klasifikasi,
                   COALESCE(e.lokasi, 'Koleksi Utama') as lokasi,
                   s.loan_date,
                   s.due_date,
                   s.fine_amount,
                   s.fine_status,
                   COALESCE(s.renewal_count, 0) as renewal_count,
                   CAST((julianday('now', 'localtime') - julianday(s.due_date)) AS INTEGER) as days_overdue
            FROM sirkulasi s
            LEFT JOIN anggota a ON s.member_id = a.member_id
            LEFT JOIN eksemplar e ON s.no_induk = e.no_induk
            LEFT JOIN bibliografi b ON e.biblio_id = b.id
            LEFT JOIN peminjaman p ON (s.member_id = p.member_id AND s.no_induk = p.no_induk AND p.loan_status IN ('0', 'On Loan'))
            WHERE s.return_date IS NULL OR s.return_date = ''
            ORDER BY s.due_date ASC
        """)
        rows = [dict(r) for r in c.fetchall()]

        total_active = len(rows)
        total_overdue = sum(1 for r in rows if r['days_overdue'] > 0)
        due_today = sum(1 for r in rows if r['days_overdue'] == 0)
        due_soon = sum(1 for r in rows if r['days_overdue'] < 0)
        total_fines = sum((r['days_overdue'] * 500) for r in rows if r['days_overdue'] > 0)

        conn.close()
        resp = jsonify({
            'status': 'success',
            'kpi': {
                'total_active': total_active,
                'total_overdue': total_overdue,
                'due_today': due_today,
                'due_soon': due_soon,
                'total_fines': total_fines
            },
            'loans': rows
        })
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


