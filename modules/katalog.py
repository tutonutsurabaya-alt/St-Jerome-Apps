"""Domain module: katalog."""

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

router = ModuleRouter('katalog')

@router.route('/input_buku')
@login_required
def input_buku():
    resp = make_response(render_template('input_buku.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@router.route('/input_majalah')
@login_required
def input_majalah():
    resp = make_response(render_template('input_majalah.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@router.route('/api/antrean', methods=['GET'])
@api_login_required
def get_antrean():
    if os.path.exists(ANTREAN_FILE):
        with open(ANTREAN_FILE, 'r') as f:
            try:
                return jsonify(json.load(f))
            except:
                return jsonify([])
    return jsonify([])


@router.route('/cetak_khusus')
@login_required
def cetak_khusus():
    search = request.args.get('search', '')
    mode = request.args.get('mode', '')
    page = int(request.args.get('page', 1))
    per_page = 15
    offset = (page - 1) * per_page
    
    conn = database.get_db_connection()
    if search:
        query = 'SELECT * FROM buku WHERE judul LIKE ? OR pengarang LIKE ? OR no_induk LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?'
        params = (f'%{search}%', f'%{search}%', f'%{search}%', per_page, offset)
        count_query = 'SELECT COUNT(*) FROM buku WHERE judul LIKE ? OR pengarang LIKE ? OR no_induk LIKE ?'
        count_params = (f'%{search}%', f'%{search}%', f'%{search}%')
    else:
        query = 'SELECT * FROM buku ORDER BY id DESC LIMIT ? OFFSET ?'
        params = (per_page, offset)
        count_query = 'SELECT COUNT(*) FROM buku'
        count_params = ()
        
    koleksi = conn.execute(query, params).fetchall()
    total = conn.execute(count_query, count_params).fetchone()[0]
    conn.close()
    
    total_pages = (total // per_page) + (1 if total % per_page > 0 else 0)
    
    return render_template('cetak_khusus.html', koleksi_list=[dict(row) for row in koleksi], search=search, page=page, total_pages=total_pages, mode=mode)


@router.route('/api/cetak_khusus', methods=['POST'])
@api_login_required
def api_cetak_khusus():
    import json, os, tempfile
    from template_stiker import generate_stiker_pdf
    
    data = request.json
    ids = data.get('ids', [])
    options = data.get('options', {})
    
    if not ids:
        return jsonify({'status': 'error', 'message': 'Tidak ada buku yang dipilih'}), 400
        
    conn = database.get_db_connection()
    placeholders = ','.join('?' for _ in ids)
    koleksi_data = conn.execute(f'SELECT * FROM buku WHERE id IN ({placeholders})', ids).fetchall()
    conn.close()
    # Sort to match clicked order
    k_dict = {str(row['id']): row for row in koleksi_data}
    koleksi = [k_dict[str(i)] for i in ids if str(i) in k_dict]
    
    buku_list = [dict(row) for row in koleksi]
    
    # Write to a temp JSON file
    temp_fd, temp_path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(temp_fd, 'w') as f:
        json.dump(buku_list, f)
        
    output_pdf = 'stiker_khusus_output.pdf'
    
    try:
        generate_stiker_pdf(temp_path, output_pdf, options)
        os.remove(temp_path)
        return send_file(output_pdf, as_attachment=True, download_name='stiker_khusus.pdf')
    except Exception as e:
        print(e)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@router.route('/api/buku/<path:no_induk>', methods=['GET'])
@api_login_required
def get_buku(no_induk):
    conn = database.get_db_connection()
    buku = conn.execute("SELECT * FROM buku WHERE no_induk = ?", (no_induk,)).fetchone()
    conn.close()
    
    if buku:
        return jsonify({'status': 'success', 'data': dict(buku)})
    return jsonify({'status': 'error', 'message': 'Not found'}), 404


@router.route('/api/antrean', methods=['POST'])
@api_login_required
def add_antrean():
    data = request.json
    
    # Save to SQLite DB
    conn = database.get_db_connection()
    try:
        conn.execute('''
            INSERT OR REPLACE INTO buku (
                no_induk, tgl_terima, status_buku, judul, pengarang, subjek, gmd, edisi, 
                isbn, penerbit, tahun_terbit, tempat_terbit, deskripsi_fisik, 
                judul_seri, bahasa, klasifikasi, cutter, huruf_judul, copy_ke, catatan, lokasi
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('no_induk'), data.get('tgl_terima'), data.get('status_buku'),
            data.get('judul'), data.get('pengarang'), data.get('subjek'),
            data.get('gmd'), data.get('edisi'), data.get('isbn'),
            data.get('penerbit'), data.get('tahun_terbit'), data.get('tempat_terbit'),
            data.get('deskripsi_fisik'), data.get('judul_seri'), data.get('bahasa'),
            data.get('klasifikasi'), data.get('cutter'), data.get('huruf_judul'),
            data.get('copy_ke'), data.get('catatan'), (data.get('lokasi') or get_branding()['default_lokasi'])
        ))
        conn.commit()
    except Exception as e:
        print("Error DB:", e)
    finally:
        conn.close()

    # Save to JSON Queue for printing
    queue = []
    if os.path.exists(ANTREAN_FILE):
        with open(ANTREAN_FILE, 'r') as f:
            try:
                queue = json.load(f)
            except:
                pass
            
    queue.append(data)
    with open(ANTREAN_FILE, 'w') as f:
        json.dump(queue, f)
        
    return jsonify({'status': 'success'})


@router.route('/api/antrean/hapus', methods=['POST'])
@api_login_required
def hapus_antrean():
    no_induk = request.json.get('no_induk')
    index = request.json.get('index')
    
    if os.path.exists(ANTREAN_FILE):
        import json
        with open(ANTREAN_FILE, 'r') as f:
            try:
                queue = json.load(f)
            except:
                queue = []
                
        if no_induk:
            queue = [b for b in queue if str(b.get('no_induk', '')) != str(no_induk)]
        elif index is not None and 0 <= index < len(queue):
            queue.pop(index)
            
        with open(ANTREAN_FILE, 'w') as f:
            json.dump(queue, f)
            
    return jsonify({'status': 'success'})


@router.route('/api/antrean/hapus_semua', methods=['POST'])
@api_login_required
def hapus_semua_antrean():
    if os.path.exists(ANTREAN_FILE):
        os.remove(ANTREAN_FILE)
    return jsonify({'status': 'success'})


@router.route('/api/antrean/existing', methods=['POST'])
@api_login_required
def add_antrean_existing():
    data = request.json
    no_induk = data.get('no_induk')
    
    conn = database.get_db_connection()
    buku_db = conn.execute("SELECT * FROM buku WHERE no_induk = ?", (no_induk,)).fetchone()
    conn.close()
    
    if not buku_db:
        return jsonify({'status': 'error', 'message': 'Buku tidak ditemukan di database.'}), 404
        
    buku_dict = dict(buku_db)
    
    # Save to JSON Queue for printing
    import os, json
    ANTREAN_FILE = 'antrian_stiker.json'
    queue = []
    if os.path.exists(ANTREAN_FILE):
        with open(ANTREAN_FILE, 'r') as f:
            try:
                queue = json.load(f)
            except:
                pass
            
    queue.append(buku_dict)
    with open(ANTREAN_FILE, 'w') as f:
        json.dump(queue, f)
        
    return jsonify({'status': 'success', 'data': buku_dict})


@router.route('/cetak_stiker_tas')
@login_required
def cetak_stiker_tas():
    import io
    import os
    from PIL import Image
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    logo_path = os.path.join(current_app.root_path, 'static', 'img', 'logo_stiker_tas.png')
    if not os.path.exists(logo_path):
        return "Logo image not found.", 404
        
    logo = Image.open(logo_path).convert('RGBA')
    logo_cropped = logo.crop((70, 190, 960, 475))
    logo_io = io.BytesIO()
    logo_cropped.save(logo_io, format="PNG")
    logo_io.seek(0)
    img_reader = ImageReader(logo_io)

    pdf_bytes = io.BytesIO()
    c = canvas.Canvas(pdf_bytes, pagesize=(11.0*cm, 3.5*cm))

    for num in range(1, 26):
        c.setStrokeColorRGB(90/255.0, 51/255.0, 21/255.0)
        c.setLineWidth(2)
        c.roundRect(0.1*cm, 0.1*cm, 10.8*cm, 3.3*cm, 0.2*cm, stroke=1, fill=0)

        # Draw smaller width so it doesn't touch the line
        c.drawImage(img_reader, 0.15*cm, 0.15*cm, width=7.7*cm, height=3.2*cm, preserveAspectRatio=True, anchor='c')

        # Line closes top and bottom bounds exactly
        c.setLineWidth(1)
        c.line(8.1*cm, 0.1*cm, 8.1*cm, 3.4*cm)

        c.setFont("Times-Bold", 65)
        c.setFillColorRGB(0, 51/255.0, 102/255.0)
        c.drawCentredString(9.5*cm, 0.95*cm, str(num))
        
        c.showPage()
    
    c.save()
    pdf_bytes.seek(0)
    return send_file(pdf_bytes, download_name='stiker_tas.pdf', mimetype='application/pdf')


@router.route('/cetak_sirkulasi')

@login_required
def cetak_sirkulasi():
    search = request.args.get('search', '')
    page = int(request.args.get('page', 1))
    per_page = 15
    offset = (page - 1) * per_page
    
    conn = database.get_db_connection()
    if search:
        query = 'SELECT * FROM buku WHERE judul LIKE ? OR pengarang LIKE ? OR no_induk LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?'
        params = (f'%{search}%', f'%{search}%', f'%{search}%', per_page, offset)
        count_query = 'SELECT COUNT(*) FROM buku WHERE judul LIKE ? OR pengarang LIKE ? OR no_induk LIKE ?'
        count_params = (f'%{search}%', f'%{search}%', f'%{search}%')
    else:
        query = 'SELECT * FROM buku ORDER BY id DESC LIMIT ? OFFSET ?'
        params = (per_page, offset)
        count_query = 'SELECT COUNT(*) FROM buku'
        count_params = ()
        
    buku_list = conn.execute(query, params).fetchall()
    total_books = conn.execute(count_query, count_params).fetchone()[0]
    conn.close()
    
    total_pages = (total_books + per_page - 1) // per_page
    return render_template('cetak_sirkulasi.html', koleksi_list=[dict(row) for row in buku_list], page=page, total_pages=total_pages, search=search)


@router.route('/api/cetak_sirkulasi', methods=['POST'])
@api_login_required
def api_cetak_sirkulasi():
    import json, os, tempfile
    from template_stiker import generate_stiker_pdf
    
    data = request.json
    ids = data.get('ids', [])
    options = data.get('options', {})
    
    conn = database.get_db_connection()
    placeholders = ','.join('?' for _ in ids)
    buku_list = []
    if ids:
        buku_list_raw = conn.execute(f'SELECT e.no_induk, b.judul, b.pengarang, b.klasifikasi, b.cutter, b.huruf_judul FROM eksemplar e JOIN bibliografi b ON e.biblio_id = b.id WHERE e.no_induk IN ({placeholders})', ids).fetchall()
        buku_list = [dict(row) for row in buku_list_raw]
    conn.close()
    
    if not buku_list:
        return "Tidak ada buku yang dipilih", 400
        
    fd, temp_path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(buku_list, f)
        
    output_pdf = 'kartu_buku_output.pdf'
    if os.path.exists(output_pdf): os.remove(output_pdf)
    
    try:
        generate_stiker_pdf(temp_path, output_pdf, options)
        os.remove(temp_path)
        return send_file(output_pdf, as_attachment=True, download_name='kartu_kantong.pdf')
    except Exception as e:
        print("Error generating PDF:", e)
        if os.path.exists(temp_path): os.remove(temp_path)
        return "Gagal menghasilkan PDF.", 500


@router.route('/cetak_pdf')
@login_required
def cetak_pdf():
    from template_stiker import generate_stiker_pdf
    import os
    output_pdf = 'stiker_output.pdf'
    
    if not os.path.exists(ANTREAN_FILE):
        return "Antrean kosong. Tambahkan buku ke antrean terlebih dahulu."
        
    if os.path.exists(output_pdf):
        os.remove(output_pdf)
        
    try:
        generate_stiker_pdf(ANTREAN_FILE, output_pdf)
    except Exception as e:
        print("Error generating PDF:", e)
        return "Gagal menghasilkan PDF. Terjadi kesalahan internal.", 500
    
    if not os.path.exists(output_pdf):
        return "Gagal menghasilkan PDF. Pastikan antrean tidak kosong dan format data benar."
        
    return send_file(output_pdf, as_attachment=True, download_name='stiker.pdf')


@router.route('/export_biblio')
@login_required
def export_biblio():
    import slims_bridge
    csv_str = slims_bridge.generate_biblio_csv()
    return send_file(
        io.BytesIO(csv_str.encode('utf-8-sig')),
        mimetype="text/csv",
        as_attachment=True,
        download_name="biblio_export.csv"
    )


@router.route('/export_item')
@login_required
def export_item():
    import slims_bridge
    csv_str = slims_bridge.generate_item_csv()
    return send_file(
        io.BytesIO(csv_str.encode('utf-8-sig')),
        mimetype="text/csv",
        as_attachment=True,
        download_name="item_export.csv"
    )


@router.route('/koleksi')
@login_required
def daftar_koleksi():
    lokasi = request.args.get('lokasi')
    if not lokasi:
        # Jika belum milih lokasi, arahkan ke lokasi default perpustakaan aktif
        return redirect(url_for('daftar_koleksi', lokasi=get_branding()['default_lokasi']))
        
    conn = database.get_db_connection()
    page = int(request.args.get('page', 1))
    search = request.args.get('search', '')
    limit = 50
    offset = (page - 1) * limit
    
    if search:
        query = 'SELECT * FROM buku WHERE lokasi = ? AND (judul LIKE ? OR no_induk LIKE ? OR pengarang LIKE ?) ORDER BY id DESC LIMIT ? OFFSET ?'
        buku_list = conn.execute(query, (lokasi, f'%{search}%', f'%{search}%', f'%{search}%', limit, offset)).fetchall()
        total = conn.execute('SELECT COUNT(*) FROM buku WHERE lokasi = ? AND (judul LIKE ? OR no_induk LIKE ? OR pengarang LIKE ?)', (lokasi, f'%{search}%', f'%{search}%', f'%{search}%')).fetchone()[0]
    else:
        buku_list = conn.execute('SELECT * FROM buku WHERE lokasi = ? ORDER BY id DESC LIMIT ? OFFSET ?', (lokasi, limit, offset)).fetchall()
        total = conn.execute('SELECT COUNT(*) FROM buku WHERE lokasi = ?', (lokasi,)).fetchone()[0]
        
    conn.close()
    
    total_pages = (total + limit - 1) // limit
    
    return render_template('koleksi.html', 
                           buku_list=buku_list, 
                           page=page, 
                           total_pages=total_pages,
                           search=search,
                           lokasi=lokasi)


@router.route('/koleksi/delete/<int:id>', methods=['POST'])
@login_required
def delete_koleksi(id):
    conn = database.get_db_connection()
    conn.execute('DELETE FROM buku WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('daftar_koleksi'))


@router.route('/koleksi/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_koleksi(id):
    conn = database.get_db_connection()
    buku = conn.execute('SELECT * FROM buku WHERE id = ?', (id,)).fetchone()
    
    if request.method == 'POST':
        conn.execute('''
            UPDATE buku SET 
                no_induk=?, judul=?, pengarang=?, subjek=?, gmd=?, edisi=?, 
                isbn=?, penerbit=?, tahun_terbit=?, tempat_terbit=?, deskripsi_fisik=?, 
                judul_seri=?, bahasa=?, klasifikasi=?, cutter=?, huruf_judul=?, lokasi=?, is_reference_only=?
            WHERE id=?
        ''', (
            request.form.get('no_induk'), request.form.get('judul'), request.form.get('pengarang'),
            request.form.get('subjek'), request.form.get('gmd'), request.form.get('edisi'),
            request.form.get('isbn'), request.form.get('penerbit'), request.form.get('tahun_terbit'),
            request.form.get('tempat_terbit'), request.form.get('deskripsi_fisik'), request.form.get('judul_seri'),
            request.form.get('bahasa'), request.form.get('klasifikasi'), request.form.get('cutter'),
            request.form.get('huruf_judul'), request.form.get('lokasi'), 1 if request.form.get('is_reference_only') else 0, id
        ))
        conn.commit()
        conn.close()
        return redirect(url_for('daftar_koleksi'))
        
    conn.close()
    return render_template('edit_koleksi.html', buku=buku)


@router.route('/import_slims', methods=['GET'])
@login_required
def import_slims():
    return render_template('import_slims.html')


@router.route('/import_slims_loan', methods=['POST'])
@login_required
def import_slims_loan():
    if 'file' not in request.files:
        return "No file part", 400
    file = request.files['file']
    if file.filename == '':
        return "No selected file", 400
        
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join('/tmp', filename) if os.name != 'nt' else filename
        
        try:
            file.save(filepath)
            df = pd.read_excel(filepath)
            conn = database.get_db_connection()
            
            conn.execute("""
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
            conn.execute('DELETE FROM peminjaman')
            
            for index, row in df.iterrows():
                no_induk = str(row.get('Item Code', ''))
                if no_induk == 'nan' or pd.isna(row.get('Item Code')): no_induk = ''
                
                loan_date = str(row.get('Loan Date', ''))
                if loan_date == 'nan' or pd.isna(row.get('Loan Date')): loan_date = ''
                
                conn.execute("""
                    INSERT INTO peminjaman (member_id, member_name, no_induk, judul, loan_date, due_date, loan_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(row.get('Member ID', '')),
                    str(row.get('Member Name', '')),
                    no_induk,
                    str(row.get('Title', '')),
                    loan_date,
                    str(row.get('Due Date', '')),
                    str(row.get('Loan Status', ''))
                ))
            conn.commit()
            conn.close()
            os.remove(filepath)
            default_lokasi = get_branding()['default_lokasi']
            return f"<script>alert('Sukses sinkronisasi Riwayat Peminjaman!'); window.location.href='/analisis_lanjutan?lokasi={default_lokasi}';</script>"
        except Exception as e:
            return f"Error memproses file: {str(e)}", 500


@router.route('/import_slims_katalog', methods=['POST'])
@login_required
def import_slims_katalog():
    biblio_file = request.files.get('biblio_file')
    item_file = request.files.get('item_file')
    
    if not biblio_file or not item_file or biblio_file.filename == '' or item_file.filename == '':
        return "Harap unggah kedua file CSV (Biblio dan Item)!", 400
        
    try:
        import os
        biblio_path = os.path.join('/tmp', 'temp_biblio.csv') if os.name != 'nt' else 'temp_biblio.csv'
        item_path = os.path.join('/tmp', 'temp_item.csv') if os.name != 'nt' else 'temp_item.csv'
        
        biblio_file.save(biblio_path)
        item_file.save(item_path)
        
        import csv
        import re
        
        biblio_map = {}
        with open(biblio_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=',', quotechar='"')
            headers = next(reader, None)
            for row in reader:
                if len(row) < 18: continue
                title = row[0]
                gmd = row[1]
                edisi = row[2]
                isbn = row[3]
                penerbit = row[4]
                tahun_terbit = row[5]
                deskripsi = row[6]
                klasifikasi = row[11]
                pengarang = row[15].strip('<>').replace('><', ', ')
                subjek = row[16].strip('<>').replace('><', ', ')
                
                item_code_raw = row[17]
                codes = re.findall(r'<(.*?)>', item_code_raw)
                if not codes and item_code_raw.strip():
                    codes = [item_code_raw.strip()]
                
                for code in codes:
                    biblio_map[code] = {
                        'title': title, 'gmd': gmd, 'edisi': edisi, 'isbn': isbn,
                        'penerbit': penerbit, 'tahun_terbit': tahun_terbit,
                        'deskripsi': deskripsi, 'klasifikasi': klasifikasi,
                        'pengarang': pengarang, 'subjek': subjek
                    }
                    
        conn = database.get_db_connection()
        conn.execute('DELETE FROM buku')
        
        count = 0
        with open(item_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=',', quotechar='"')
            headers = next(reader, None)
            for row in reader:
                if len(row) < 10: continue
                no_induk = row[0]
                call_number = row[1]
                tgl_terima = row[4]
                lokasi_raw = row[7].upper()
                status_buku = row[9]
                
                lokasi = 'IMAVI' if 'IMAVI' in lokasi_raw else ('STPD' if 'STPD' in lokasi_raw else 'UMUM')
                
                bib = biblio_map.get(no_induk, {})
                judul = bib.get('title', row[18] if len(row) > 18 else '')
                pengarang = bib.get('pengarang', '')
                subjek = bib.get('subjek', '')
                gmd = bib.get('gmd', '')
                edisi = bib.get('edisi', '')
                isbn = bib.get('isbn', '')
                penerbit = bib.get('penerbit', '')
                tahun_terbit = bib.get('tahun_terbit', '')
                deskripsi = bib.get('deskripsi', '')
                klasifikasi = bib.get('klasifikasi', '')
                
                final_class, cutter, huruf = parse_slims_call_number(call_number)
                if not final_class: final_class = klasifikasi
                is_ref = 1 if (call_number and call_number.strip().startswith('R')) else 0
                
                conn.execute("""
                    INSERT OR REPLACE INTO buku (no_induk, judul, pengarang, lokasi, klasifikasi, cutter, huruf_judul, subjek,
                                    gmd, edisi, isbn, penerbit, tahun_terbit, tempat_terbit, 
                                    deskripsi_fisik, status_buku, tgl_terima, is_reference_only)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (no_induk, judul, pengarang, lokasi, final_class, cutter, huruf, subjek,
                      gmd, edisi, isbn, penerbit, tahun_terbit, '', 
                      deskripsi, status_buku, tgl_terima, is_ref))
                count += 1
                
        conn.commit()
        conn.close()
        
        try: os.remove(biblio_path)
        except: pass
        try: os.remove(item_path)
        except: pass
        
        default_lokasi = get_branding()['default_lokasi']
        return f"<script>alert('Sukses sinkronisasi {count} Katalog Buku SLiMS!'); window.location.href='/analisis_lanjutan?lokasi={default_lokasi}';</script>"
        
    except Exception as e:
        return f"Error memproses file: {str(e)}", 500


@router.route('/audit_rak/scanner')
@login_required
def audit_rak_scanner():
    conn = database.get_db_connection()
    active_session = conn.execute("SELECT * FROM stock_opname_session WHERE status = 'AKTIF' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not active_session:
        flash("Tidak ada sesi aktif, silakan mulai sesi terlebih dahulu.", "warning")
        return redirect(url_for('audit_rak'))
    return render_template('audit_rak_scanner.html', active_session=active_session)


@router.route('/audit_rak', methods=['GET', 'POST'])
@login_required
def audit_rak():
    conn = database.get_db_connection()
    try:
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'start':
                nama_sesi = request.form.get('nama_sesi')
                conn.execute("INSERT INTO stock_opname_session (nama_sesi) VALUES (?)", (nama_sesi,))
                conn.commit()
            elif action == 'stop':
                session_id = request.form.get('session_id')
                conn.execute("UPDATE stock_opname_session SET end_date = CURRENT_TIMESTAMP, status = 'SELESAI' WHERE id = ?", (session_id,))
                conn.commit()
                return redirect(url_for('laporan_opname', session_id=session_id))

        # Check active session
        active_session = conn.execute("SELECT * FROM stock_opname_session WHERE status = 'AKTIF' ORDER BY id DESC LIMIT 1").fetchone()

        recent_scans = []
        stats = {'total_koleksi': 0, 'ditemukan': 0, 'belum_tercatat': 0}

        if active_session:
            stats['total_koleksi'] = conn.execute("SELECT COUNT(*) FROM eksemplar").fetchone()[0]
            stats['ditemukan'] = conn.execute(
                "SELECT COUNT(DISTINCT no_induk) FROM stock_opname_scan WHERE session_id = ? AND COALESCE(status, '') != 'BELUM TERCATAT'",
                (active_session['id'],),
            ).fetchone()[0]
            stats['belum_tercatat'] = conn.execute(
                "SELECT COUNT(DISTINCT no_induk) FROM stock_opname_scan WHERE session_id = ? AND status = 'BELUM TERCATAT'",
                (active_session['id'],),
            ).fetchone()[0]

            recent_scans = conn.execute('''
                SELECT s.scan_date, s.no_induk, s.status, b.judul
                FROM stock_opname_scan s
                LEFT JOIN eksemplar e ON s.no_induk = e.no_induk
                LEFT JOIN bibliografi b ON e.biblio_id = b.id
                WHERE s.session_id = ?
                ORDER BY s.id DESC LIMIT 20
            ''', (active_session['id'],)).fetchall()

        past_sessions = conn.execute("SELECT * FROM stock_opname_session WHERE status = 'SELESAI' ORDER BY id DESC").fetchall()

        return render_template('audit_rak.html', active_session=active_session, stats=stats, recent_scans=recent_scans, past_sessions=past_sessions)
    finally:
        conn.close()


@router.route('/api/scan_opname', methods=['POST'])
@login_required
def api_scan_opname():
    data = request.json or {}
    no_induk = (data.get('no_induk') or '').strip()
    session_id = data.get('session_id')
    target_rak = (data.get('target_rak') or '').strip().upper()

    if not no_induk or not session_id:
        return jsonify({'status': 'error', 'message': 'Data tidak lengkap'})

    conn = database.get_db_connection()
    try:
        buku = conn.execute('''
            SELECT e.no_induk, b.judul, b.klasifikasi
            FROM eksemplar e
            LEFT JOIN bibliografi b ON e.biblio_id = b.id
            WHERE e.no_induk = ?
        ''', (no_induk,)).fetchone()

        if not buku:
            already_unreg = conn.execute(
                "SELECT id FROM stock_opname_scan WHERE session_id = ? AND no_induk = ?",
                (session_id, no_induk),
            ).fetchone()
            input_url = f"/input_buku?no_induk={urllib.parse.quote(no_induk, safe='')}"
            if already_unreg:
                return jsonify({
                    'status': 'warning',
                    'message': f'Barcode {no_induk} sudah tercatat di daftar Belum Tercatat pada sesi ini.',
                    'no_induk': no_induk,
                    'judul': 'BELUM TERCATAT DI KATALOG',
                    'status_rak': 'BELUM TERCATAT',
                    'ddc_asli': '-',
                    'unregistered': True,
                    'input_url': input_url,
                })
            conn.execute(
                "INSERT INTO stock_opname_scan (session_id, no_induk, status) VALUES (?, ?, 'BELUM TERCATAT')",
                (session_id, no_induk),
            )
            conn.commit()
            return jsonify({
                'status': 'unregistered',
                'message': f'Barcode {no_induk} belum tercatat di katalog. Tersimpan ke daftar Belum Tercatat.',
                'no_induk': no_induk,
                'judul': 'BELUM TERCATAT DI KATALOG',
                'status_rak': 'BELUM TERCATAT',
                'ddc_asli': '-',
                'unregistered': True,
                'input_url': input_url,
            })

        ddc = str(buku['klasifikasi'] or '')
        status_rak = 'ANOMALI'

        if not target_rak or target_rak == 'ALL':
            status_rak = 'BENAR'
        elif target_rak == 'F':
            status_rak = 'BENAR' if ddc.upper().startswith('F') else 'SALAH RAK'
        elif target_rak == 'R':
            status_rak = 'BENAR' if ddc.upper().startswith('R') else 'SALAH RAK'
        elif ddc.strip() and target_rak[0].isdigit():
            status_rak = 'BENAR' if ddc.strip()[0] == target_rak[0] else 'SALAH RAK'

        already = conn.execute(
            "SELECT id, status FROM stock_opname_scan WHERE session_id = ? AND no_induk = ?",
            (session_id, no_induk),
        ).fetchone()
        if already:
            if already['status'] == 'BELUM TERCATAT':
                conn.execute(
                    "UPDATE stock_opname_scan SET status = ?, scan_date = CURRENT_TIMESTAMP WHERE id = ?",
                    (status_rak, already['id']),
                )
                conn.commit()
                return jsonify({
                    'status': 'success',
                    'message': f'Berhasil (Terdaftar): {buku["judul"]}',
                    'no_induk': no_induk,
                    'judul': buku['judul'],
                    'status_rak': status_rak,
                    'ddc_asli': ddc,
                })
            return jsonify({
                'status': 'warning',
                'message': f'Buku {buku["judul"]} sudah dipindai sebelumnya.',
                'no_induk': no_induk,
                'judul': buku['judul'],
                'status_rak': status_rak,
                'ddc_asli': ddc,
            })

        conn.execute(
            "INSERT INTO stock_opname_scan (session_id, no_induk, status) VALUES (?, ?, ?)",
            (session_id, no_induk, status_rak),
        )
        conn.commit()
        return jsonify({
            'status': 'success',
            'message': f'Berhasil: {buku["judul"]}',
            'no_induk': no_induk,
            'judul': buku['judul'],
            'status_rak': status_rak,
            'ddc_asli': ddc,
        })
    finally:
        conn.close()


@router.route('/laporan_opname/<int:session_id>')
@login_required
def laporan_opname(session_id):
    conn = database.get_db_connection()
    try:
        sesi = conn.execute("SELECT * FROM stock_opname_session WHERE id = ?", (session_id,)).fetchone()
        if not sesi:
            return "Sesi tidak ditemukan", 404

        total_koleksi = conn.execute("SELECT COUNT(*) FROM eksemplar").fetchone()[0]
        ditemukan = conn.execute(
            "SELECT COUNT(DISTINCT no_induk) FROM stock_opname_scan WHERE session_id = ? AND COALESCE(status, '') != 'BELUM TERCATAT'",
            (session_id,),
        ).fetchone()[0]
        hilang = max(0, total_koleksi - ditemukan)

        buku_hilang = conn.execute('''
            SELECT e.no_induk, b.judul, e.lokasi
            FROM eksemplar e
            LEFT JOIN bibliografi b ON e.biblio_id = b.id
            WHERE e.no_induk NOT IN (
                SELECT no_induk FROM stock_opname_scan WHERE session_id = ? AND COALESCE(status, '') != 'BELUM TERCATAT'
            )
            ORDER BY e.no_induk
            LIMIT 500
        ''', (session_id,)).fetchall()

        belum_tercatat = conn.execute('''
            SELECT no_induk, scan_date
            FROM stock_opname_scan
            WHERE session_id = ? AND status = 'BELUM TERCATAT'
            ORDER BY id DESC
        ''', (session_id,)).fetchall()

        return render_template(
            'laporan_opname.html',
            sesi=sesi,
            total_koleksi=total_koleksi,
            ditemukan=ditemukan,
            hilang=hilang,
            buku_hilang=buku_hilang,
            belum_tercatat=belum_tercatat,
            belum_tercatat_count=len(belum_tercatat),
        )
    finally:
        conn.close()


@router.route('/laporan_opname/<int:session_id>/export_csv')
@login_required
def export_laporan_opname_csv(session_id):
    conn = database.get_db_connection()
    try:
        sesi = conn.execute("SELECT * FROM stock_opname_session WHERE id = ?", (session_id,)).fetchone()
        if not sesi:
            return "Sesi tidak ditemukan", 404

        missing_rows = conn.execute('''
            SELECT e.no_induk, b.judul, b.pengarang, b.klasifikasi, e.lokasi
            FROM eksemplar e
            LEFT JOIN bibliografi b ON e.biblio_id = b.id
            WHERE e.no_induk NOT IN (
                SELECT no_induk FROM stock_opname_scan WHERE session_id = ? AND COALESCE(status, '') != 'BELUM TERCATAT'
            )
            ORDER BY e.no_induk
        ''', (session_id,)).fetchall()

        unreg_rows = conn.execute('''
            SELECT no_induk, scan_date
            FROM stock_opname_scan
            WHERE session_id = ? AND status = 'BELUM TERCATAT'
            ORDER BY id DESC
        ''', (session_id,)).fetchall()
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Status Temuan', 'No Induk / Barcode', 'Judul', 'Pengarang', 'Klasifikasi', 'Lokasi', 'Waktu Scan'])
    for r in unreg_rows:
        writer.writerow(['BELUM TERCATAT', r['no_induk'], '', '', '', '', r['scan_date'] or ''])
    for r in missing_rows:
        writer.writerow(['BELUM TERPINDAI / HILANG', r['no_induk'], r['judul'] or '', r['pengarang'] or '', r['klasifikasi'] or '', r['lokasi'] or '', ''])

    csv_bytes = output.getvalue().encode('utf-8-sig')
    return send_file(
        io.BytesIO(csv_bytes),
        mimetype='text/csv; charset=utf-8',
        as_attachment=True,
        download_name=f"laporan_opname_{session_id}.csv",
    )


@router.route('/api/buku/missing_covers', methods=['GET'])
def api_missing_covers():
    conn = database.get_db_connection()
    # Get up to 100 books that lack both ISBN and image
    cursor = conn.execute("SELECT id, judul, pengarang FROM bibliografi WHERE (isbn IS NULL OR isbn = '') AND (image IS NULL OR image = '') LIMIT 50")
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(results)


@router.route('/api/buku/save_cover', methods=['POST'])
def api_save_cover():
    data = request.json
    biblio_id = data.get('id')
    url = data.get('url')
    if biblio_id and url:
        conn = database.get_db_connection()
        conn.execute("UPDATE bibliografi SET image = ? WHERE id = ?", (url, biblio_id))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    return jsonify({'status': 'error'})


@router.route('/kelola_sampul')
@login_required
def kelola_sampul():
    return render_template('kelola_sampul.html')


@router.route('/api/sampul_kosong', methods=['GET'])
@api_login_required
def api_sampul_kosong():
    search = request.args.get('search', '')
    page = int(request.args.get('page', 1))
    per_page = 15
    offset = (page - 1) * per_page
    
    conn = database.get_db_connection()
    query = '''
        SELECT id, judul, pengarang, penerbit, tahun_terbit, isbn
        FROM bibliografi
        WHERE (image IS NULL OR image = '' OR image = 'NOT_FOUND' OR image = 'RATE_LIMIT')
    '''
    params = []
    
    if search:
        query += ' AND (judul LIKE ? OR pengarang LIKE ? OR isbn LIKE ?)'
        params.extend(['%'+search+'%', '%'+search+'%', '%'+search+'%'])
        
    count_query = query.replace('SELECT id, judul, pengarang, penerbit, tahun_terbit, isbn', 'SELECT COUNT(*) as total')
    cursor = conn.execute(count_query, params)
    total = cursor.fetchone()['total']
    
    query += ' ORDER BY id DESC LIMIT ? OFFSET ?'
    params.extend([per_page, offset])
    
    cursor = conn.execute(query, params)
    books = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return jsonify({
        'status': 'success',
        'data': books,
        'total': total,
        'page': page,
        'total_pages': (total + per_page - 1) // per_page
    })


@router.route('/api/sampul/update', methods=['POST'])
@api_login_required
def api_sampul_update():
    data = request.json
    biblio_id = data.get('id')
    image_url = data.get('image_url')
    
    if not biblio_id or not image_url:
        return jsonify({'status': 'error', 'message': 'Data tidak lengkap'}), 400
    
    # Hanya simpan kalau benar-benar ada gambar (URL valid)
    # Kalau NOT_FOUND, biarkan kosong supaya bisa dicoba lagi nanti
    if image_url and image_url.startswith('http'):
        conn = database.get_db_connection()
        conn.execute('UPDATE bibliografi SET image = ? WHERE id = ?', (image_url, biblio_id))
        conn.commit()
        conn.close()
    
    return jsonify({'status': 'success'})


@router.route('/api/sampul/stats', methods=['GET'])
@api_login_required
def api_sampul_stats():
    conn = database.get_db_connection()
    total = conn.execute('SELECT COUNT(*) as c FROM bibliografi').fetchone()['c']
    punya_sampul = conn.execute(
        "SELECT COUNT(*) as c FROM bibliografi WHERE image IS NOT NULL AND image != '' AND image != 'NOT_FOUND' AND image != 'RATE_LIMIT'"
    ).fetchone()['c']
    conn.close()
    belum_sampul = total - punya_sampul
    persen = round((punya_sampul / total * 100), 1) if total > 0 else 0
    return jsonify({
        'status': 'success',
        'total': total,
        'punya_sampul': punya_sampul,
        'belum_sampul': belum_sampul,
        'persen': persen
    })


@router.route('/api/robot/start', methods=['POST'])
@api_login_required
@feature_required('ENABLE_AUTOCOVER_ONLINE', 'Auto-cover online dinonaktifkan pada mode offline.', api_status=503)
def api_robot_start():
    state = get_robot_state()
    if state.get("is_running"):
        return jsonify({'status': 'error', 'message': 'Robot sedang berjalan!'})
        
    limit = request.json.get('limit', 50)
    thread = threading.Thread(target=robot_worker, args=(limit,))
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'success'})


@router.route('/api/robot/stop', methods=['POST'])
@api_login_required
def api_robot_stop():
    state = get_robot_state()
    state["is_running"] = False
    state["error"] = "Dihentikan manual oleh admin."
    save_robot_state(state)
    return jsonify({'status': 'success'})


@router.route('/api/robot/status', methods=['GET'])
@api_login_required
def api_robot_status():
    return jsonify(get_robot_state())


@router.route('/api/majalah/next_number', methods=['GET'])
@api_login_required
def api_majalah_next_number():
    tipe = request.args.get('tipe', 'hidup').lower()
    tahun = request.args.get('tahun', '')
    if not tahun:
        import datetime
        tahun = datetime.datetime.now().strftime('%Y')
    
    prefix = 'MJLH'
    if tipe == 'basis':
        prefix = 'MJLB'
    elif tipe == 'custom':
        prefix = request.args.get('prefix', 'MJLX').strip().upper()
    
    year_suffix = str(tahun)[-2:] if len(str(tahun)) >= 2 else '26'
    next_serial, barcode = get_next_majalah_barcode(prefix, str(tahun))
    
    return jsonify({
        'status': 'success',
        'prefix': prefix,
        'tahun_2digit': year_suffix,
        'serial_int': next_serial,
        'serial_str': f"{next_serial:03d}",
        'barcode': barcode
    })


@router.route('/api/majalah/riwayat', methods=['GET'])
@api_login_required
def api_majalah_riwayat():
    conn = database.get_db_connection()
    rows = conn.execute("""
        SELECT e.no_induk, b.id as biblio_id, b.judul, b.edisi, b.penerbit, b.tahun_terbit, 
               b.klasifikasi, b.image, e.tgl_terima, e.status_buku, e.lokasi
        FROM eksemplar e
        JOIN bibliografi b ON e.biblio_id = b.id
        WHERE e.no_induk LIKE 'MJL%' OR b.gmd IN ('Serial', 'Majalah')
        ORDER BY e.id DESC LIMIT 15
    """).fetchall()
    conn.close()
    results = [dict(r) for r in rows]
    for r in results:
        if r.get('image'):
            if r['image'].startswith('http'):
                r['cover_url'] = r['image']
            else:
                r['cover_url'] = f"/static/uploads/{r['image']}"
        else:
            r['cover_url'] = None
    return jsonify({'status': 'success', 'data': results})


@router.route('/api/majalah/simpan', methods=['POST'])
@api_login_required
def api_majalah_simpan():
    from werkzeug.utils import secure_filename
    import os, datetime
    
    if request.is_json:
        data = request.json
    else:
        data = request.form

    tipe = data.get('tipe_majalah', 'hidup').lower()
    tahun_terbit = str(data.get('tahun_terbit', datetime.datetime.now().year)).strip()
    year_suffix = tahun_terbit[-2:] if len(tahun_terbit) >= 2 else '26'
    
    # Prefix
    if tipe == 'hidup':
        prefix = 'MJLH'
    elif tipe == 'basis':
        prefix = 'MJLB'
    else:
        prefix = data.get('prefix', 'MJLX').strip().upper()
        
    # Check barcode
    barcode = data.get('no_induk', '').strip()
    if not barcode:
        serial_input = data.get('serial_str', '').strip()
        if serial_input and serial_input.isdigit():
            serial_num = int(serial_input)
            barcode = f"{prefix} {serial_num:03d}/{year_suffix}"
        else:
            _, barcode = get_next_majalah_barcode(prefix, tahun_terbit)
            
    conn = database.get_db_connection()
    # Validate uniqueness
    existing = conn.execute("SELECT no_induk FROM eksemplar WHERE no_induk = ?", (barcode,)).fetchone()
    if existing:
        conn.close()
        return jsonify({
            'status': 'error', 
            'message': f"Nomor Induk '{barcode}' sudah ada di database! Silakan periksa kembali nomor seri terbitan."
        }), 400

    # Image upload
    image_filename = None
    if 'image' in request.files:
        file = request.files['image']
        if file and file.filename != '' and allowed_image(file.filename):
            filename = secure_filename(file.filename)
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(upload_dir, exist_ok=True)
            file.save(os.path.join(upload_dir, filename))
            image_filename = filename

    # Prepare metadata
    judul = data.get('judul', '').strip()
    edisi = data.get('edisi', '').strip()
    pengarang = data.get('pengarang', '').strip() or f"Redaksi {prefix}"
    penerbit = data.get('penerbit', '').strip()
    tempat_terbit = data.get('tempat_terbit', '').strip()
    klasifikasi = data.get('klasifikasi', '050').strip()
    cutter = data.get('cutter', '').strip() or prefix[:3]
    huruf_judul = data.get('huruf_judul', 'm').strip()
    subjek = data.get('subjek', 'Terbitan Berseri / Majalah').strip()
    deskripsi_fisik = data.get('deskripsi_fisik', '').strip()
    bahasa = data.get('bahasa', 'Indonesia')
    gmd = 'Serial'
    status_buku = data.get('status_buku', 'BELI')
    lokasi = (data.get('lokasi') or '').strip() or get_branding()['default_lokasi']
    tgl_terima = data.get('tgl_terima') or datetime.datetime.now().strftime('%Y-%m-%d')
    isbn_issn = data.get('isbn') or data.get('issn', '')
    
    # 1. Insert Bibliografi
    cursor = conn.execute("""
        INSERT INTO bibliografi (
            judul, pengarang, penerbit, isbn, klasifikasi, tempat_terbit,
            tahun_terbit, edisi, bahasa, gmd, deskripsi_fisik, judul_seri,
            cutter, huruf_judul, subjek, image
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        judul, pengarang, penerbit, isbn_issn, klasifikasi, tempat_terbit,
        tahun_terbit, edisi, bahasa, gmd, deskripsi_fisik, data.get('judul_seri', ''),
        cutter, huruf_judul, subjek, image_filename
    ))
    biblio_id = cursor.lastrowid
    
    # 2. Insert Eksemplar
    conn.execute("""
        INSERT INTO eksemplar (biblio_id, no_induk, status_buku, lokasi, tgl_terima, copy_ke)
        VALUES (?, ?, ?, ?, ?, 1)
    """, (
        biblio_id, barcode, status_buku, lokasi, tgl_terima
    ))
    
    # 3. Insert Buku (for compatibility across all modules)
    conn.execute("""
        INSERT OR REPLACE INTO buku (
            no_induk, judul, pengarang, penerbit, isbn, klasifikasi, tempat_terbit,
            tahun_terbit, edisi, bahasa, gmd, deskripsi_fisik, judul_seri,
            cutter, huruf_judul, subjek, copy_ke, status_buku, lokasi, tgl_terima, gambar_sampul
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        barcode, judul, pengarang, penerbit, isbn_issn, klasifikasi, tempat_terbit,
        tahun_terbit, edisi, bahasa, gmd, deskripsi_fisik, data.get('judul_seri', ''),
        cutter, huruf_judul, subjek, 1, status_buku, lokasi, tgl_terima, image_filename
    ))
    
    conn.commit()
    conn.close()
    
    # Fetch next barcode for convenience
    next_s, next_code = get_next_majalah_barcode(prefix, tahun_terbit)
    
    return jsonify({
        'status': 'success',
        'message': f"Berhasil mencatat majalah dengan Nomor Induk: {barcode}",
        'barcode': barcode,
        'biblio_id': biblio_id,
        'next_barcode': next_code,
        'next_serial': f"{next_s:03d}"
    })


@router.route('/api/ddc/search', methods=['GET'])
@api_login_required
def api_ddc_search():
    keyword = request.args.get('q', '').lower()
    import json
    try:
        with open('ddc_kamus.json', 'r') as f:
            ddc_dict = json.load(f)
    except:
        ddc_dict = {}
        
    results = []
    
    # Clean keywords (ignore short words)
    ignore_words = ['pengantar', 'buku', 'panduan', 'dasar', 'teori', 'ilmu', 'dan', 'yang']
    keywords = [w for w in keyword.split() if w not in ignore_words and len(w) > 2]
    
    # If no keywords left, just search the original keyword
    if not keywords:
        keywords = [keyword]
        
    for code, desc in ddc_dict.items():
        # exact match
        if keyword in desc.lower():
            results.append({'kode': code, 'deskripsi': desc})
            continue
            
        # keyword match
        for kw in keywords:
            if kw in desc.lower():
                results.append({'kode': code, 'deskripsi': desc})
                break
                
    return jsonify(results)


@router.route('/api/bibliografi/search', methods=['GET'])
@api_login_required
def api_biblio_search():
    keyword = request.args.get('q', '')
    conn = database.get_db_connection()
    # Search by title or author
    cursor = conn.execute("SELECT * FROM bibliografi WHERE judul LIKE ? OR pengarang LIKE ? LIMIT 10", (f'%{keyword}%', f'%{keyword}%'))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(results)


@router.route('/api/ai/models', methods=['GET'])
@api_login_required
@feature_required('ENABLE_AI_ASSISTANT', 'Fitur Asisten AI dinonaktifkan pada mode ini.', api_status=503)
def list_ai_models():
    conn = database.get_db_connection()
    api_key_row = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'GEMINI_API_KEY'").fetchone()
    conn.close()
    
    if not api_key_row or not api_key_row['nilai']:
        return "No API key", 400
        
    import google.generativeai as genai
    genai.configure(api_key=api_key_row['nilai'])
    models = []
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            models.append(m.name)
    return jsonify(models)


@router.route('/api/ai/scan_cover', methods=['POST'])
@api_login_required
@feature_required('ENABLE_AI_ASSISTANT', 'Fitur Asisten AI dinonaktifkan pada mode ini.', api_status=503)
def api_ai_scan_cover():
    if 'image' not in request.files:
        return jsonify({'status': 'error', 'message': 'Tidak ada gambar yang diunggah.'})
        
    file = request.files['image']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'File gambar kosong.'})
        
    conn = database.get_db_connection()
    api_key_row = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'GEMINI_API_KEY'").fetchone()
    conn.close()
    
    if not api_key_row or not api_key_row['nilai']:
        return jsonify({'status': 'error', 'message': 'API Key Gemini belum diatur di menu Pengaturan.'})
        
    try:
        import google.generativeai as genai
        import PIL.Image
        import io
        
        # Read image to memory
        img_bytes = file.read()
        img = PIL.Image.open(io.BytesIO(img_bytes))
        
        genai.configure(api_key=api_key_row['nilai'])
        try:
            model = genai.GenerativeModel('gemini-3.8-flash')
        except:
            model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = '''Anda adalah pakar pustakawan. 
Lihat gambar sampul buku ini dan ekstrak metadatanya.
Kembalikan HANYA format JSON murni.
Gunakan persis key berikut (jika tidak ada di sampul, isi dengan string kosong ""):
{
  "judul": "...",
  "pengarang": "...",
  "penerbit": "..."
}'''
        response = model.generate_content([prompt, img])
        
        import re, json
        json_str = response.text
        json_str = re.sub(r'```json\n?', '', json_str)
        json_str = re.sub(r'```\n?', '', json_str)
        
        data = json.loads(json_str.strip())
        return jsonify({'status': 'success', 'data': data})
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@router.route('/api/ai/metadata', methods=['POST'])
@api_login_required
@feature_required('ENABLE_AI_ASSISTANT', 'Fitur Asisten AI dinonaktifkan pada mode ini.', api_status=503)
def api_ai_metadata():
    import urllib.request, urllib.parse, json, re
    data = request.json or {}
    query = data.get('query', '').strip()
    
    if not query:
        return jsonify({'status': 'error', 'message': 'Judul atau ISBN harus diisi.'})
        
    cleaned_isbn = re.sub(r'[^0-9X]', '', query.upper())
    is_isbn = len(cleaned_isbn) in (10, 13)
    
    metadata = {}
    
    # 1. Coba OpenLibrary jika ada ISBN
    if is_isbn:
        try:
            ol_url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{cleaned_isbn}&format=json&jscmd=data"
            req = urllib.request.Request(ol_url, headers={'User-Agent': 'StJeromeLib/1.0'})
            with urllib.request.urlopen(req, timeout=4) as resp:
                ol_data = json.loads(resp.read().decode('utf-8'))
                book_key = f"ISBN:{cleaned_isbn}"
                if book_key in ol_data:
                    b_info = ol_data[book_key]
                    metadata['judul'] = b_info.get('title', '')
                    if b_info.get('authors'):
                        metadata['pengarang'] = ", ".join([a.get('name', '') for a in b_info['authors']])
                    if b_info.get('publishers'):
                        metadata['penerbit'] = b_info['publishers'][0].get('name', '')
                    if b_info.get('publish_date'):
                        metadata['tahun_terbit'] = re.sub(r'\D', '', b_info['publish_date'])[:4]
                    if b_info.get('number_of_pages'):
                        metadata['deskripsi_fisik'] = f"{b_info['number_of_pages']} hlm."
                    metadata['isbn'] = cleaned_isbn
                    if b_info.get('cover') and b_info['cover'].get('large'):
                        metadata['cover_url'] = b_info['cover']['large']
                    elif b_info.get('cover') and b_info['cover'].get('medium'):
                        metadata['cover_url'] = b_info['cover']['medium']
        except Exception:
            pass

    # 2. Coba Google Books API jika ada data yang masih kosong
    if not metadata.get('judul') or not metadata.get('pengarang'):
        try:
            q_enc = urllib.parse.quote(query)
            gb_url = f"https://www.googleapis.com/books/v1/volumes?q={q_enc}&maxResults=1"
            req = urllib.request.Request(gb_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=4) as resp:
                gb_data = json.loads(resp.read().decode('utf-8'))
                if gb_data.get('totalItems', 0) > 0 and gb_data.get('items'):
                    v = gb_data['items'][0].get('volumeInfo', {})
                    if not metadata.get('judul'): metadata['judul'] = v.get('title', '')
                    if not metadata.get('pengarang') and v.get('authors'):
                        metadata['pengarang'] = ", ".join(v.get('authors', []))
                    if not metadata.get('penerbit'): metadata['penerbit'] = v.get('publisher', '')
                    if not metadata.get('tahun_terbit') and v.get('publishedDate'):
                        metadata['tahun_terbit'] = v.get('publishedDate')[:4]
                    if not metadata.get('deskripsi_fisik') and v.get('pageCount'):
                        metadata['deskripsi_fisik'] = f"{v.get('pageCount')} hlm."
                    if not metadata.get('subjek') and v.get('categories'):
                        metadata['subjek'] = ", ".join(v.get('categories', []))
                    if not metadata.get('cover_url') and v.get('imageLinks'):
                        metadata['cover_url'] = v['imageLinks'].get('thumbnail') or v['imageLinks'].get('smallThumbnail')
                    if not metadata.get('isbn') and v.get('industryIdentifiers'):
                        for idf in v['industryIdentifiers']:
                            if idf.get('type') in ('ISBN_13', 'ISBN_10'):
                                metadata['isbn'] = idf.get('identifier')
                                break
        except Exception:
            pass

    # 3. Gunakan Gemini AI untuk melengkapi DDC dan detail teologi / Indonesia
    conn = database.get_db_connection()
    api_key_row = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'GEMINI_API_KEY'").fetchone()
    conn.close()

    if api_key_row and api_key_row['nilai'] and (not metadata.get('klasifikasi') or not metadata.get('judul')):
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key_row['nilai'])
            try:
                model = genai.GenerativeModel('gemini-3.8-flash')
            except:
                model = genai.GenerativeModel('gemini-1.5-flash')
            
            prompt = f'''Anda adalah asisten pakar katalogisasi perpustakaan Indonesia (DDC).
Lengkapi metadata katalog buku untuk: "{query}".
Judul diketahui: "{metadata.get('judul', '')}"
Pengarang diketahui: "{metadata.get('pengarang', '')}"

Kembalikan HANYA format JSON murni:
{{
  "judul": "...",
  "pengarang": "...",
  "penerbit": "...",
  "tempat_terbit": "...",
  "tahun_terbit": "...",
  "isbn": "...",
  "klasifikasi": "...",
  "subjek": "..."
}}
Note: "klasifikasi" adalah kode angka DDC (contoh: "262.91" untuk dokumen kepausan/gereja, "230" untuk teologi, "100" untuk filsafat).
'''
            response = model.generate_content(prompt)
            text = response.text.strip()
            if text.startswith('```json'): text = text[7:]
            if text.startswith('```'): text = text[3:]
            if text.endswith('```'): text = text[:-3]
            ai_data = json.loads(text.strip())
            
            for k, v in ai_data.items():
                if v and not metadata.get(k):
                    metadata[k] = v
        except Exception:
            pass

    if metadata.get('judul'):
        return jsonify({'status': 'success', 'data': metadata})
    else:
        return jsonify({'status': 'error', 'message': 'Buku tidak ditemukan di database online maupun AI. Silakan lengkapi manual.'})


@router.route('/api/buku/next_barcode', methods=['GET'])
@api_login_required
def api_next_barcode():
    import datetime
    current_year = datetime.datetime.now().strftime('%y')
    conn = database.get_db_connection()
    rows = conn.execute("SELECT no_induk FROM eksemplar WHERE no_induk GLOB '[0-9]*/" + current_year + "'").fetchall()
    max_num = 0
    for r in rows:
        try:
            num = int(r[0].split('/')[0])
            if num > max_num:
                max_num = num
        except:
            pass
    conn.close()
    return jsonify({
        'next_num': max_num + 1,
        'year': current_year,
        'next_barcode': f"{max_num+1:04d}/{current_year}"
    })


@router.route('/api/buku/input_batch', methods=['POST'])
@api_login_required
def api_input_batch():
    from werkzeug.utils import secure_filename

    if request.is_json:
        data = request.json or {}
    else:
        data = request.form

    biblio_id = data.get('biblio_id')
    jumlah_eksemplar = int(data.get('jumlah', 1))
    custom_barcode = (data.get('custom_barcode') or data.get('no_induk') or '').strip()
    lokasi_buku = (data.get('lokasi') or '').strip() or get_branding()['default_lokasi']

    conn = database.get_db_connection()
    try:
        if custom_barcode:
            if jumlah_eksemplar != 1:
                return jsonify({
                    'status': 'error',
                    'message': 'Barcode fisik khusus hanya mendukung 1 eksemplar per input.',
                }), 400
            dup = conn.execute(
                "SELECT 1 FROM eksemplar WHERE no_induk = ?",
                (custom_barcode,),
            ).fetchone()
            if dup:
                return jsonify({
                    'status': 'error',
                    'message': f'Barcode {custom_barcode} sudah terdaftar di katalog.',
                }), 400

        # Handle image upload or image url
        image_filename = None
        if 'image' in request.files:
            file = request.files['image']
            if file and file.filename != '' and allowed_image(file.filename):
                filename = secure_filename(file.filename)
                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
                os.makedirs(upload_dir, exist_ok=True)
                file.save(os.path.join(upload_dir, filename))
                image_filename = filename
        if not image_filename and data.get('image_url'):
            image_filename = data.get('image_url').strip()

        # 1. Handle Bibliografi
        if not biblio_id:
            cursor = conn.execute("""
                INSERT INTO bibliografi (
                    judul, pengarang, penerbit, isbn, klasifikasi, tempat_terbit,
                    tahun_terbit, edisi, bahasa, gmd, deskripsi_fisik, judul_seri,
                    cutter, huruf_judul, subjek, image
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('judul'), data.get('pengarang'), data.get('penerbit'), data.get('isbn'),
                data.get('klasifikasi'), data.get('tempat_terbit'), data.get('tahun_terbit'),
                data.get('edisi'), data.get('bahasa', 'Indonesia'),
                data.get('gmd', 'Text'), data.get('deskripsi_fisik'), data.get('judul_seri'),
                data.get('cutter'), data.get('huruf_judul'), data.get('subjek'), image_filename
            ))
            biblio_id = cursor.lastrowid
        else:
            update_query = """
                UPDATE bibliografi SET
                    judul=?, pengarang=?, penerbit=?, isbn=?, klasifikasi=?, tempat_terbit=?,
                    tahun_terbit=?, edisi=?, bahasa=?, gmd=?, deskripsi_fisik=?, judul_seri=?,
                    cutter=?, huruf_judul=?, subjek=?
                WHERE id=?
            """
            conn.execute(update_query, (
                data.get('judul'), data.get('pengarang'), data.get('penerbit'), data.get('isbn'),
                data.get('klasifikasi'), data.get('tempat_terbit'), data.get('tahun_terbit'),
                data.get('edisi'), data.get('bahasa', 'Indonesia'),
                data.get('gmd', 'Text'), data.get('deskripsi_fisik'), data.get('judul_seri'),
                data.get('cutter'), data.get('huruf_judul'), data.get('subjek'), biblio_id
            ))
            if image_filename:
                conn.execute("UPDATE bibliografi SET image = ? WHERE id = ?", (image_filename, biblio_id))

            if data.get('status_buku'):
                conn.execute("UPDATE eksemplar SET status_buku = ? WHERE biblio_id = ?", (data.get('status_buku'), biblio_id))

        generated_barcodes = []
        if jumlah_eksemplar > 0:
            if custom_barcode:
                barcodes_to_insert = [custom_barcode]
            else:
                current_year = datetime.datetime.now().strftime('%y')
                rows = conn.execute("SELECT no_induk FROM eksemplar WHERE no_induk GLOB '[0-9]*/" + current_year + "'").fetchall()
                max_num = 0
                for r in rows:
                    try:
                        num = int(r[0].split('/')[0])
                        if num > max_num:
                            max_num = num
                    except Exception:
                        pass
                start_num = max_num + 1 if max_num > 0 else 1
                barcodes_to_insert = [f"{(start_num + i):04d}/{current_year}" for i in range(jumlah_eksemplar)]

            tgl_now = datetime.datetime.now().strftime('%Y-%m-%d')
            for i, new_barcode in enumerate(barcodes_to_insert):
                conn.execute("""
                    INSERT INTO eksemplar (biblio_id, no_induk, status_buku, lokasi, tgl_terima, copy_ke)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    biblio_id, new_barcode, data.get('status_buku', 'BELI'), lokasi_buku,
                    tgl_now, (i + 1)
                ))
                conn.execute("""
                    INSERT OR REPLACE INTO buku (
                        no_induk, judul, pengarang, penerbit, isbn, klasifikasi, tempat_terbit,
                        tahun_terbit, edisi, bahasa, gmd, deskripsi_fisik, judul_seri,
                        cutter, huruf_judul, subjek, copy_ke, status_buku, lokasi, tgl_terima, gambar_sampul
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    new_barcode, data.get('judul'), data.get('pengarang'), data.get('penerbit'),
                    data.get('isbn'), data.get('klasifikasi'), data.get('tempat_terbit'),
                    data.get('tahun_terbit'), data.get('edisi'), data.get('bahasa', 'Indonesia'),
                    data.get('gmd', 'Text'), data.get('deskripsi_fisik'), data.get('judul_seri'),
                    data.get('cutter'), data.get('huruf_judul'), data.get('subjek'),
                    (i + 1), data.get('status_buku', 'BELI'), lokasi_buku,
                    tgl_now, image_filename
                ))
                conn.execute("""
                    UPDATE stock_opname_scan
                    SET status = 'DITEMUKAN'
                    WHERE no_induk = ? AND status = 'BELUM TERCATAT'
                      AND session_id IN (SELECT id FROM stock_opname_session WHERE status = 'AKTIF')
                """, (new_barcode,))
                generated_barcodes.append(new_barcode)

        conn.commit()
        return jsonify({
            'status': 'success',
            'message': f'Berhasil menyimpan bibliografi dan {jumlah_eksemplar} eksemplar.',
            'barcodes': generated_barcodes
        })
    finally:
        conn.close()


@router.route('/api/koleksi/list', methods=['GET'])
@api_login_required
def api_koleksi_list():
    import traceback
    try:
        search = request.args.get('search', '').strip()
        page = int(request.args.get('page', 1))
        cover_status = request.args.get('cover_status', 'all')
        per_page = int(request.args.get('per_page', 24))
        offset = (page - 1) * per_page
        
        conn = database.get_db_connection()
        
        where_clauses = ["(b.judul LIKE ? OR b.pengarang LIKE ? OR b.klasifikasi LIKE ? OR b.isbn LIKE ?)"]
        params = [f'%{search}%', f'%{search}%', f'%{search}%', f'%{search}%']
        
        if cover_status == 'has_cover':
            where_clauses.append("(b.image IS NOT NULL AND b.image != '' AND b.image NOT IN ('NOT_FOUND', 'tidak_ada', 'none', 'null', 'RATE_LIMIT'))")
        elif cover_status == 'no_cover':
            where_clauses.append("(b.image IS NULL OR b.image = '' OR b.image IN ('NOT_FOUND', 'tidak_ada', 'none', 'null', 'RATE_LIMIT'))")
        elif cover_status == 'has_isbn':
            where_clauses.append("(b.isbn IS NOT NULL AND b.isbn != '')")

        where_sql = " AND ".join(where_clauses)
            
        query = f"""
            SELECT b.*, 
                   COUNT(e.no_induk) as jumlah_eksemplar,
                   SUM(CASE WHEN e.status_buku = 'TERSEDIA' OR e.status_buku IS NULL OR e.status_buku = '' THEN 1 ELSE 0 END) as jumlah_tersedia
            FROM bibliografi b
            LEFT JOIN eksemplar e ON b.id = e.biblio_id
            WHERE {where_sql}
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT ? OFFSET ?
        """
        cursor = conn.execute(query, tuple(params + [per_page, offset]))
        results = [dict(row) for row in cursor.fetchall()]
        
        import re
        for row in results:
            img = (row.get('image') or '').strip()
            if img and img not in ('NOT_FOUND', 'tidak_ada', 'none', 'null', 'RATE_LIMIT'):
                if img.startswith('http'):
                    row['cover_url'] = img
                else:
                    row['cover_url'] = f"/static/uploads/{img}"
                row['fallback_cover_url'] = None
                row['has_real_cover'] = True
            elif row.get('isbn') and is_enabled('ENABLE_AUTOCOVER_ONLINE'):
                raw_isbn = str(row['isbn']).split(',')[0].split(' ')[0].upper()
                cleaned = re.sub(r'[^0-9X]', '', raw_isbn)
                if cleaned:
                    row['cover_url'] = f"https://covers.openlibrary.org/b/isbn/{cleaned}-M.jpg?default=false"
                    row['fallback_cover_url'] = f"https://books.google.com/books/content?vid=ISBN{cleaned}&printsec=frontcover&img=1&zoom=1"
                    row['has_real_cover'] = True
                else:
                    row['cover_url'] = None
                    row['fallback_cover_url'] = None
                    row['has_real_cover'] = False
            else:
                row['cover_url'] = None
                row['fallback_cover_url'] = None
                row['has_real_cover'] = False

            # DDC metadata helper
            klas = str(row.get('klasifikasi') or '').strip().upper()
            row['ddc_code'] = klas if klas else 'UMUM'
        
        total_query = f"SELECT COUNT(*) FROM bibliografi b WHERE {where_sql}"
        total = conn.execute(total_query, tuple(params)).fetchone()[0]
        conn.close()
        return jsonify({
            'data': results,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total // per_page) + (1 if total % per_page > 0 else 0)
        })
    except Exception as e:
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@router.route('/api/koleksi/quick_cover', methods=['POST'])
@api_login_required
def api_koleksi_quick_cover():
    biblio_id = request.form.get('id') or (request.json and request.json.get('id'))
    if not biblio_id:
        return jsonify({'status': 'error', 'message': 'ID Bibliografi tidak ditemukan'}), 400
        
    image_val = None
    # 1. Handle file upload
    if 'image_file' in request.files:
        file = request.files['image_file']
        if file and file.filename != '':
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
                import time
                filename = f"cover_{biblio_id}_{int(time.time())}{ext}"
                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
                os.makedirs(upload_dir, exist_ok=True)
                file.save(os.path.join(upload_dir, filename))
                image_val = filename
    
    # 2. Handle URL paste
    if not image_val:
        url = request.form.get('url') or (request.json and request.json.get('url'))
        if url and url.strip().startswith('http'):
            image_val = url.strip()

    if not image_val:
        return jsonify({'status': 'error', 'message': 'File gambar atau URL tidak valid'}), 400

    conn = database.get_db_connection()
    conn.execute("UPDATE bibliografi SET image = ? WHERE id = ?", (image_val, biblio_id))
    conn.commit()
    conn.close()

    cover_url = image_val if image_val.startswith('http') else f"/static/uploads/{image_val}"
    return jsonify({
        'status': 'success',
        'message': 'Sampul buku berhasil diperbarui!',
        'cover_url': cover_url
    })


@router.route('/api/koleksi/eksemplar/<int:biblio_id>', methods=['GET'])
@api_login_required
def api_koleksi_eksemplar(biblio_id):
    conn = database.get_db_connection()
    cursor = conn.execute("SELECT no_induk, status_ketersediaan, status_buku, lokasi, tgl_terima FROM eksemplar WHERE biblio_id = ? ORDER BY no_induk", (biblio_id,))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(results)


@router.route('/api/bibliografi/<int:id>', methods=['DELETE'])
@api_login_required
def api_delete_biblio(id):
    import database
    conn = database.get_db_connection()
    # Delete eksemplar first
    conn.execute("DELETE FROM eksemplar WHERE biblio_id = ?", (id,))
    # Delete bibliografi
    conn.execute("DELETE FROM bibliografi WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@router.route('/api/eksemplar/<path:no_induk>', methods=['DELETE'])
@api_login_required
def api_delete_eksemplar(no_induk):
    import database
    conn = database.get_db_connection()
    conn.execute("DELETE FROM eksemplar WHERE no_induk = ?", (no_induk,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@router.route('/api/bibliografi/get/<int:id>', methods=['GET'])
@api_login_required
def api_get_biblio(id):
    import database
    conn = database.get_db_connection()
    row = conn.execute("SELECT * FROM bibliografi WHERE id = ?", (id,)).fetchone()
    conn.close()
    if row:
        return jsonify(dict(row))
    return jsonify({}), 404


