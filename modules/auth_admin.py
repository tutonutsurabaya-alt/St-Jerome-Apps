"""Domain module: auth_admin."""

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

router = ModuleRouter('auth_admin')

@router.route('/health', methods=['GET'])
def health_check():
    conn = None
    try:
        conn = database.get_db_connection()
        row = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()
        tables = row[0] if row else 0
        return jsonify({'status': 'ok', 'db': 'ok', 'tables': tables}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'db': str(e)}), 503
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@router.route('/portofolio')
def portofolio():
    return render_template('portofolio.html')


@router.route('/login', methods=['GET', 'POST'])
def login():
    conn = database.get_db_connection()
    if get_feature_config().get('APP_MODE') == 'offline_portable':
        conn.close()
        turnstile_site_key = ''
        turnstile_secret_key = ''
    else:
        sitekey_row = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'CF_TURNSTILE_SITE_KEY'").fetchone()
        secret_row = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'CF_TURNSTILE_SECRET_KEY'").fetchone()
        conn.close()

        turnstile_site_key = (sitekey_row['nilai'] if sitekey_row and sitekey_row['nilai'] else os.environ.get('CF_TURNSTILE_SITE_KEY', '')).strip()
        turnstile_secret_key = (secret_row['nilai'] if secret_row and secret_row['nilai'] else os.environ.get('CF_TURNSTILE_SECRET_KEY', '')).strip()

    if request.method == 'POST':
        # Cloudflare Turnstile Bot Verification
        if turnstile_secret_key and turnstile_site_key:
            turnstile_token = request.form.get('cf-turnstile-response', '').strip()
            if not turnstile_token:
                return render_template('login.html', error='Harap selesaikan verifikasi Cloudflare (bukan bot).', turnstile_site_key=turnstile_site_key)
            try:
                import urllib.request
                import urllib.parse
                cf_url = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
                client_ip = request.headers.get('CF-Connecting-IP') or request.remote_addr
                cf_data = urllib.parse.urlencode({
                    'secret': turnstile_secret_key,
                    'response': turnstile_token,
                    'remoteip': client_ip
                }).encode('utf-8')
                cf_req = urllib.request.Request(cf_url, data=cf_data, method='POST')
                with urllib.request.urlopen(cf_req, timeout=5) as cf_res:
                    cf_result = json.loads(cf_res.read().decode('utf-8'))
                    if not cf_result.get('success'):
                        return render_template('login.html', error='Verifikasi Cloudflare tidak valid. Silakan coba kembali.', turnstile_site_key=turnstile_site_key)
            except Exception as e:
                print("Cloudflare Turnstile verification warning:", e)

        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')

        authenticated = False
        user_info = None
        try:
            conn = database.get_db_connection()
            user = conn.execute("SELECT * FROM users WHERE LOWER(username) = ?", (username,)).fetchone()
            if user and user['password'] == password:
                if user['is_active'] == 0:
                    conn.close()
                    return render_template('login.html', error='Akun Anda telah dinonaktifkan oleh Master Admin.', turnstile_site_key=turnstile_site_key)
                authenticated = True
                user_info = dict(user)
            conn.close()
        except Exception:
            pass

        # Fallback check
        if not authenticated:
            if (username == 'yosef' and password == 'bcajember') or (username == 'imavi' and password == 'imavi'):
                authenticated = True
                user_info = {
                    'username': username,
                    'nama_lengkap': 'Yosef Paskah' if username == 'yosef' else 'Pustakawan IMAVI',
                    'role': 'Master Admin' if username == 'yosef' else 'Pustakawan',
                    'hak_akses': 'all' if username == 'yosef' else 'sirkulasi,pos,koleksi,baca,anggota,laporan'
                }

        if authenticated and user_info:
            session.permanent = True
            session['logged_in'] = True
            session['username'] = user_info['username']
            session['nama_lengkap'] = user_info.get('nama_lengkap', user_info['username'].capitalize())
            session['role'] = user_info.get('role', 'Administrator')
            hak = user_info.get('hak_akses', 'all' if user_info['username'] == 'yosef' else '')
            session['hak_akses'] = hak

            user_perms = [p.strip().lower() for p in (hak or '').split(',') if p.strip()]
            if user_perms == ['pos']:
                return redirect('/pos')
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='Username atau Password salah', turnstile_site_key=turnstile_site_key)
    return render_template('login.html', turnstile_site_key=turnstile_site_key)


@router.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@router.route('/profil', methods=['GET', 'POST'])
@login_required
def profil():
    curr_username = session.get('username', 'yosef')
    conn = database.get_db_connection()
    
    # Ensure users table exists
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            nama_lengkap TEXT NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'Administrator',
            email TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()

    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'update_profile':
            nama_lengkap = request.form.get('nama_lengkap', '').strip()
            email = request.form.get('email', '').strip()
            if nama_lengkap:
                conn.execute("UPDATE users SET nama_lengkap = ?, email = ? WHERE LOWER(username) = ?", 
                             (nama_lengkap, email, curr_username.lower()))
                conn.commit()
                session['nama_lengkap'] = nama_lengkap
                flash("Informasi profil berhasil diperbarui!", "success")
            return redirect(url_for('profil', tab='biodata'))
            
        elif action == 'change_password':
            password_lama = request.form.get('password_lama', '')
            password_baru = request.form.get('password_baru', '').strip()
            konfirmasi_password = request.form.get('konfirmasi_password', '').strip()
            
            user = conn.execute("SELECT * FROM users WHERE LOWER(username) = ?", (curr_username.lower(),)).fetchone()
            current_pw = user['password'] if user else ('bcajember' if curr_username == 'yosef' else 'imavi')
            
            if password_lama != current_pw:
                flash("Kata sandi lama yang Anda masukkan salah!", "error")
                conn.close()
                return redirect(url_for('profil', tab='password'))
                
            if len(password_baru) < 4:
                flash("Kata sandi baru minimal 4 karakter!", "error")
                conn.close()
                return redirect(url_for('profil', tab='password'))
                
            if password_baru != konfirmasi_password:
                flash("Konfirmasi kata sandi baru tidak cocok!", "error")
                conn.close()
                return redirect(url_for('profil', tab='password'))
                
            # Update password in DB
            conn.execute("UPDATE users SET password = ? WHERE LOWER(username) = ?", 
                         (password_baru, curr_username.lower()))
            conn.commit()
            conn.close()
            flash("Kata sandi berhasil diperbarui! Gunakan kata sandi baru ini pada sesi login berikutnya.", "success")
            return redirect(url_for('profil', tab='password'))

    # GET request
    user_row = conn.execute("SELECT * FROM users WHERE LOWER(username) = ?", (curr_username.lower(),)).fetchone()
    if not user_row:
        user_data = {
            'username': curr_username,
            'nama_lengkap': session.get('nama_lengkap', 'Yosef Paskah'),
            'role': session.get('role', 'Administrator'),
            'email': 'yosef@imavi.ac.id'
        }
    else:
        user_data = dict(user_row)
    conn.close()
    
    return render_template('profil.html', user=user_data)


@router.route('/pengguna')
@login_required
def kelola_pengguna():
    curr_username = session.get('username', '').lower()
    curr_role = session.get('role', '')
    curr_akses = session.get('hak_akses', '')

    # Only Master Admin or users with 'users'/'all' access
    if curr_username != 'yosef' and curr_role != 'Master Admin' and 'users' not in curr_akses and curr_akses != 'all':
        flash("Hanya Master Admin yang memiliki hak akses untuk mengelola pengguna!", "error")
        return redirect(url_for('index'))

    conn = database.get_db_connection()
    # Ensure columns exist
    c = conn.cursor()
    c.execute('PRAGMA table_info(users)')
    cols = [r[1] for r in c.fetchall()]
    if 'hak_akses' not in cols:
        conn.execute('ALTER TABLE users ADD COLUMN hak_akses TEXT DEFAULT "all"')
    if 'is_active' not in cols:
        conn.execute('ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1')
    conn.commit()

    rows = conn.execute("SELECT id, username, nama_lengkap, role, email, hak_akses, is_active, created_at FROM users ORDER BY id ASC").fetchall()
    users_list = [dict(r) for r in rows]
    conn.close()

    return render_template('pengguna.html', users=users_list)


@router.route('/api/pengguna/tambah', methods=['POST'])
@login_required
def api_pengguna_tambah():
    curr_username = session.get('username', '').lower()
    if curr_username != 'yosef' and session.get('role') != 'Master Admin':
        flash("Hanya Master Admin yang dapat menambah pengguna baru!", "error")
        return redirect(url_for('index'))

    username = request.form.get('username', '').strip().lower()
    nama_lengkap = request.form.get('nama_lengkap', '').strip()
    password = request.form.get('password', '').strip()
    role = request.form.get('role', 'Pustakawan').strip()
    email = request.form.get('email', '').strip()
    modules = request.form.getlist('modules')

    if not username or not nama_lengkap or not password:
        flash("Semua kolom bertanda bintang wajib diisi!", "error")
        return redirect(url_for('kelola_pengguna'))

    if len(password) < 4:
        flash("Password minimal 4 karakter!", "error")
        return redirect(url_for('kelola_pengguna'))

    if role == 'Master Admin':
        hak_akses = 'all'
    else:
        hak_akses = ','.join(modules) if modules else 'sirkulasi'

    conn = database.get_db_connection()
    existing = conn.execute("SELECT id FROM users WHERE LOWER(username) = ?", (username,)).fetchone()
    if existing:
        flash(f"Username '@{username}' sudah terdaftar! Gunakan username lain.", "error")
        conn.close()
        return redirect(url_for('kelola_pengguna'))

    conn.execute('''
        INSERT INTO users (username, nama_lengkap, password, role, email, hak_akses, is_active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
    ''', (username, nama_lengkap, password, role, email, hak_akses))
    conn.commit()
    conn.close()

    flash(f"Petugas baru '{nama_lengkap}' (@{username}) berhasil ditambahkan!", "success")
    return redirect(url_for('kelola_pengguna'))


@router.route('/api/pengguna/edit/<int:user_id>', methods=['POST'])
@login_required
def api_pengguna_edit(user_id):
    curr_username = session.get('username', '').lower()
    if curr_username != 'yosef' and session.get('role') != 'Master Admin':
        flash("Akses ditolak!", "error")
        return redirect(url_for('index'))

    conn = database.get_db_connection()
    target_user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target_user:
        conn.close()
        flash("Pengguna tidak ditemukan!", "error")
        return redirect(url_for('kelola_pengguna'))

    nama_lengkap = request.form.get('nama_lengkap', '').strip()
    email = request.form.get('email', '').strip()

    # Protected rule for yosef
    if target_user['username'].lower() == 'yosef':
        role = 'Master Admin'
        hak_akses = 'all'
        is_active = 1
    else:
        role = request.form.get('role', target_user['role']).strip()
        is_active = int(request.form.get('is_active', 1))
        modules = request.form.getlist('modules')
        if role == 'Master Admin':
            hak_akses = 'all'
        else:
            hak_akses = ','.join(modules) if modules else 'sirkulasi'

    conn.execute('''
        UPDATE users 
        SET nama_lengkap = ?, email = ?, role = ?, hak_akses = ?, is_active = ?
        WHERE id = ?
    ''', (nama_lengkap, email, role, hak_akses, is_active, user_id))
    conn.commit()
    conn.close()

    flash("Data petugas berhasil diperbarui!", "success")
    return redirect(url_for('kelola_pengguna'))


@router.route('/api/pengguna/reset_password/<int:user_id>', methods=['POST'])
@login_required
def api_pengguna_reset_password(user_id):
    curr_username = session.get('username', '').lower()
    if curr_username != 'yosef' and session.get('role') != 'Master Admin':
        flash("Akses ditolak!", "error")
        return redirect(url_for('index'))

    password_baru = request.form.get('password_baru', '').strip()
    if len(password_baru) < 4:
        flash("Password baru minimal 4 karakter!", "error")
        return redirect(url_for('kelola_pengguna'))

    conn = database.get_db_connection()
    target = conn.execute("SELECT username, nama_lengkap FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        conn.close()
        flash("Pengguna tidak ditemukan!", "error")
        return redirect(url_for('kelola_pengguna'))

    conn.execute("UPDATE users SET password = ? WHERE id = ?", (password_baru, user_id))
    conn.commit()
    conn.close()

    flash(f"Password untuk '{target['nama_lengkap']}' (@{target['username']}) berhasil direset!", "success")
    return redirect(url_for('kelola_pengguna'))


@router.route('/api/pengguna/hapus/<int:user_id>', methods=['POST'])
@login_required
def api_pengguna_hapus(user_id):
    curr_username = session.get('username', '').lower()
    if curr_username != 'yosef' and session.get('role') != 'Master Admin':
        flash("Akses ditolak!", "error")
        return redirect(url_for('index'))

    conn = database.get_db_connection()
    target = conn.execute("SELECT username, nama_lengkap FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        conn.close()
        flash("Pengguna tidak ditemukan!", "error")
        return redirect(url_for('kelola_pengguna'))

    if target['username'].lower() == 'yosef':
        conn.close()
        flash("Akun Master Admin utama tidak dapat dihapus!", "error")
        return redirect(url_for('kelola_pengguna'))

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    flash(f"Akun '{target['nama_lengkap']}' (@{target['username']}) telah dihapus dari sistem.", "success")
    return redirect(url_for('kelola_pengguna'))


@router.route('/backup')
@login_required
def backup_db():
    import datetime
    import shutil
    import tempfile
    from ops.sqlite_utils import create_verified_snapshot

    if not os.path.exists(database.DB_NAME):
        return "Database belum ada.", 404

    tmpdir = tempfile.mkdtemp(prefix='stjerome_dl_bak_')
    snap_path = os.path.join(tmpdir, 'katalog.db')
    dl_name = f"katalog_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

    @after_this_request
    def _cleanup_snapshot(response):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return response

    try:
        create_verified_snapshot(database.DB_NAME, snap_path)
        return send_file(snap_path, as_attachment=True, download_name=dl_name)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


@router.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    from core.features import FEATURE_FLAGS
    conn = database.get_db_connection()
    try:
        if request.method == 'POST':
            form_section = (request.form.get('form_section') or '').strip()

            if form_section == 'features' or request.form.get('save_feature_flags') == '1':
                app_mode = (request.form.get('app_mode') or 'online').strip().lower()
                if app_mode not in ('online', 'offline_portable', 'demo'):
                    app_mode = 'online'
                lib_name = (request.form.get('library_name') or 'Perpustakaan IMAVI').strip() or 'Perpustakaan IMAVI'
                def_lokasi = 'STPD' if 'STPD' in lib_name.upper() else 'IMAVI'

                conn.execute("INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES ('APP_MODE', ?)", (app_mode,))
                conn.execute("INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES ('LIBRARY_NAME', ?)", (lib_name,))
                conn.execute("INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES ('DEFAULT_LOKASI', ?)", (def_lokasi,))

                for flag in FEATURE_FLAGS:
                    if flag == 'ENABLE_WA_BOT':
                        val = '0'  # Bot WhatsApp dinonaktifkan di Fase 2 (Telegram-only).
                    else:
                        val = '1' if request.form.get(f'flag_{flag}') == 'on' else '0'
                    conn.execute("INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES (?, ?)", (flag, val))

                conn.commit()
                flash("Mode aplikasi & feature flags berhasil diperbarui!", "success")
                return redirect(url_for('settings'))

            token = request.form.get('telegram_token', '').strip()
            chat_id = request.form.get('telegram_chat_id', '').strip()
            gemini_key = request.form.get('gemini_api_key', '').strip()
            info_perpus = request.form.get('info_perpustakaan')

            conn.execute("INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES ('TELEGRAM_BOT_TOKEN', ?)", (token,))
            conn.execute("INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES ('TELEGRAM_CHAT_ID', ?)", (chat_id,))
            conn.execute("INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES ('GEMINI_API_KEY', ?)", (gemini_key,))
            if info_perpus is not None:
                conn.execute("INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES ('INFO_PERPUSTAKAAN', ?)", (info_perpus.strip(),))
            conn.commit()
            flash("Pengaturan berhasil disimpan!", "success")
            return redirect(url_for('settings'))

        config = {}
        rows = conn.execute("SELECT kunci, nilai FROM pengaturan_sistem").fetchall()
        for row in rows:
            config[row['kunci']] = row['nilai']
    finally:
        conn.close()

    return render_template('settings.html', config=config)


@router.route('/api/test_backup', methods=['POST'])
@login_required
def test_backup():
    import telegram_backup
    import traceback
    try:
        success, message = telegram_backup.run_backup(manual=True)
        return jsonify({
            'status': 'success' if success else 'error',
            'message': message
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'trace': traceback.format_exc()
        })


@router.route('/api/debug/db_path', methods=['GET'])
@admin_api_required
def api_debug_db():
    import os
    import database
    return jsonify({
        'cwd': os.getcwd(),
        'db_path_relative': database.DB_NAME,
        'db_path_absolute': os.path.abspath(database.DB_NAME),
        'db_exists': os.path.exists(os.path.abspath(database.DB_NAME))
    })


@router.route('/api/debug/tables', methods=['GET'])
@admin_api_required
def api_debug_tables():
    import database
    conn = database.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    
    eksemplar_count = 0
    sirkulasi_count = 0
    if 'eksemplar' in tables:
        eksemplar_count = conn.execute("SELECT COUNT(*) FROM eksemplar").fetchone()[0]
    if 'sirkulasi' in tables:
        sirkulasi_count = conn.execute("SELECT COUNT(*) FROM sirkulasi").fetchone()[0]
        
    conn.close()
    return jsonify({
        'tables': tables,
        'eksemplar_count': eksemplar_count,
        'sirkulasi_count': sirkulasi_count
    })


@router.route('/api/debug/force_migrate', methods=['GET', 'POST'])
@admin_api_required
def api_force_migrate():
    import database
    import traceback
    if (session.get('username') or '').lower() != 'yosef' and session.get('role') != 'Master Admin':
        return jsonify({'status': 'error', 'message': 'Hanya Master Admin.'}), 403
    if request.args.get('confirm') != 'YES_REBUILD_BIBLIO':
        return jsonify({'status': 'error', 'message': 'Konfirmasi wajib: ?confirm=YES_REBUILD_BIBLIO'}), 400
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor()
        
        # 1. Create bibliografi
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS bibliografi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            judul TEXT NOT NULL,
            gmd TEXT DEFAULT 'Text',
            edisi TEXT,
            isbn TEXT,
            penerbit TEXT,
            tahun_terbit TEXT,
            deskripsi_fisik TEXT,
            judul_seri TEXT,
            klasifikasi TEXT,
            cutter TEXT,
            huruf_judul TEXT,
            bahasa TEXT DEFAULT 'Indonesia',
            tempat_terbit TEXT,
            pengarang TEXT,
            subjek TEXT,
            image TEXT
        )
        """)

        # 2. Create eksemplar
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS eksemplar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            biblio_id INTEGER NOT NULL,
            no_induk TEXT UNIQUE NOT NULL,
            status_buku TEXT DEFAULT 'BELI',
            lokasi TEXT DEFAULT 'IMAVI',
            tgl_terima TEXT,
            copy_ke INTEGER DEFAULT 1,
            catatan TEXT,
            status_ketersediaan TEXT DEFAULT 'Tersedia',
            FOREIGN KEY (biblio_id) REFERENCES bibliografi(id)
        )
        """)
        
        # 3. Migrate data if empty
        msg = "Tables created."
        # Selalu reset dan migrasi ulang untuk memperbaiki duplikat
        cursor.execute("DELETE FROM eksemplar")
        cursor.execute("DELETE FROM bibliografi")
        
        cursor.execute("SELECT * FROM buku")
        buku_rows = cursor.fetchall()
        cursor.execute("PRAGMA table_info(buku)")
        columns = [col[1] for col in cursor.fetchall()]
        
        biblio_map = {} # (judul, pengarang, penerbit) -> biblio_id
        
        migrated_count = 0
        for row in buku_rows:
            row_dict = dict(zip(columns, row))
            key = (row_dict.get('judul', ''), row_dict.get('pengarang', ''), row_dict.get('penerbit', ''))
            
            if key not in biblio_map:
                cursor.execute("""
                    INSERT INTO bibliografi (
                        judul, gmd, edisi, isbn, penerbit, tahun_terbit, deskripsi_fisik, 
                        judul_seri, klasifikasi, cutter, huruf_judul, bahasa, tempat_terbit, 
                        pengarang, subjek
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    row_dict.get('judul'), row_dict.get('gmd'), row_dict.get('edisi'), 
                    row_dict.get('isbn'), row_dict.get('penerbit'), row_dict.get('tahun_terbit'), 
                    row_dict.get('deskripsi_fisik'), row_dict.get('judul_seri'), 
                    row_dict.get('klasifikasi'), row_dict.get('cutter'), row_dict.get('huruf_judul'), 
                    row_dict.get('bahasa'), row_dict.get('tempat_terbit'), row_dict.get('pengarang'), 
                    row_dict.get('subjek')
                ))
                biblio_map[key] = cursor.lastrowid
            
            biblio_id = biblio_map[key]
            
            cursor.execute("""
                INSERT INTO eksemplar (
                    biblio_id, no_induk, status_buku, lokasi, tgl_terima, copy_ke, catatan
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                biblio_id, row_dict.get('no_induk'), row_dict.get('status_buku'), 
                row_dict.get('lokasi'), row_dict.get('tgl_terima'), row_dict.get('copy_ke'), 
                row_dict.get('catatan')
            ))
            migrated_count += 1
        msg += f" Migrated {migrated_count} copies into {len(biblio_map)} titles."
        
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': msg})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e), 'trace': traceback.format_exc()})


@router.route('/api/debug/koleksi', methods=['GET'])
@admin_api_required
def api_debug_koleksi():
    import traceback
    import database
    try:
        conn = database.get_db_connection()
        query = """
            SELECT b.*, COUNT(e.no_induk) as jumlah_eksemplar
            FROM bibliografi b
            LEFT JOIN eksemplar e ON b.id = e.biblio_id
            WHERE b.judul LIKE ?
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT 10
        """
        cursor = conn.execute(query, ('%%',))
        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({'status': 'success', 'data': results})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e), 'trace': traceback.format_exc()})


@router.route('/api/debug/files', methods=['GET'])
@admin_api_required
def api_debug_files():
    import os
    import database
    
    files_found = []
    
    paths_to_check = [
        '/root/st_jerome_web/katalog.db',
        '/root/katalog.db',
        os.path.join(os.getcwd(), 'katalog.db'),
        os.path.abspath('katalog.db'),
        database.DB_NAME,
        os.path.abspath(database.DB_NAME)
    ]
    
    for p in paths_to_check:
        try:
            if os.path.exists(p):
                size = os.path.getsize(p)
                mtime = os.path.getmtime(p)
                # check if it has bibliografi
                from ops.sqlite_utils import connect_readonly
                conn = connect_readonly(p, timeout=5.0)
                has_bib = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bibliografi'").fetchone() is not None
                conn.close()
                files_found.append({'path': p, 'size': size, 'mtime': mtime, 'has_bibliografi': has_bib})
            else:
                files_found.append({'path': p, 'exists': False})
        except Exception as e:
            files_found.append({'path': p, 'error': str(e)})
            
    return jsonify(files_found)


@router.route('/api/version', methods=['GET'])
def api_version():
    return jsonify({'version': '2b2fa3d-fix-401', 'status': 'ok'})


@router.route('/debug_schema')
@admin_api_required
def debug_schema():
    conn = database.get_db_connection()
    try:
        r_schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='reservasi'").fetchone()
        a_schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='anggota'").fetchone()
        
        # Test query to see where it breaks
        error_msg = 'NO_ERROR'
        try:
            conn.execute("SELECT r.id, r.member_id, a.telepon, a.nama FROM reservasi r JOIN anggota a ON r.member_id = a.member_id LIMIT 1")
        except Exception as e:
            error_msg = str(e)
            
        return jsonify({
            'reservasi': r_schema['sql'] if r_schema else 'TABLE NOT FOUND',
            'anggota': a_schema['sql'] if a_schema else 'TABLE NOT FOUND',
            'test_error': error_msg
        })
    finally:
        conn.close()


