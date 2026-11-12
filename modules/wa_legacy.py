"""Domain module: wa_legacy."""

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

router = ModuleRouter('wa_legacy')

@router.route('/api/trigger_wa_reminder', methods=['POST'])
@login_required
@feature_required('ENABLE_WA_BOT', 'Modul WhatsApp dinonaktifkan sementara (mode Telegram-only).', api_status=200)
def trigger_wa_reminder():
    import threading
    def _run():
        send_daily_wa_reminders()
    threading.Thread(target=_run).start()
    return jsonify({'status': 'success', 'message': 'Proses pengiriman pengingat WA sedang berjalan di latar belakang.'})


@router.route('/api/internal/cron_reminder', methods=['POST'])
def internal_cron_reminder():
    import threading
    is_local = (
        request.remote_addr in ('127.0.0.1', '::1', '::ffff:127.0.0.1')
        and not request.headers.get('CF-Connecting-IP')
        and not request.headers.get('Cf-Ray')
        and not request.headers.get('X-Forwarded-For')
    )
    if not is_local and 'logged_in' not in session:
        return jsonify({'status': 'error', 'message': 'Forbidden: internal endpoint only.'}), 403
    if not is_enabled('ENABLE_WA_BOT'):
        return jsonify({'status': 'disabled', 'message': 'Modul WhatsApp dinonaktifkan sementara (mode Telegram-only).'})
    def _run():
        send_daily_wa_reminders()
    threading.Thread(target=_run).start()
    return jsonify({'status': 'success'})


@router.route('/api/wa_webhook', methods=['POST'])
@feature_required('ENABLE_WA_BOT', 'Modul WhatsApp dinonaktifkan sementara (mode Telegram-only).', api_status=200)
def api_wa_webhook():
    import re
    data = request.json
    if not data:
        return jsonify({'status': 'error'})
        
    sender = data.get('from', '')
    message = data.get('body', '') or ''
    name = data.get('sender_name', 'Pengguna')
    
    msg_strip = message.strip()
    msg_lower = msg_strip.lower()
    
    # 0. IDENTIFIKASI NOMOR WA & ANGGOTA
    nomor_wa = sender.split('@')[0]
    if nomor_wa.startswith('62'):
        nomor_wa_lokal = '0' + nomor_wa[2:]
    else:
        nomor_wa_lokal = nomor_wa
        
    conn = database.get_db_connection()
    anggota = conn.execute("SELECT member_id, nama FROM anggota WHERE telepon = ? OR telepon = ?", (nomor_wa, nomor_wa_lokal)).fetchone()
    if not anggota:
        words_check = msg_strip.replace(',', ' ').replace('.', ' ').split()
        if words_check:
            placeholders = ','.join(['?'] * len(words_check))
            anggota = conn.execute(f"SELECT member_id, nama FROM anggota WHERE member_id IN ({placeholders})", words_check).fetchone()
            
    if anggota:
        name = anggota['nama']

    # 0.5 INTERSEPSI OPAC RESERVASI / KONFIRMASI KETERSEDIAAN BUKU (DARI TOMBOL OPAC)
    is_opac_hold = (
        'katalog opac' in msg_lower or
        'menanyakan ketersediaan' in msg_lower or
        'konfirmasi ketersediaan' in msg_lower or
        ('no. barcode' in msg_lower and 'lokasi rak' in msg_lower) or
        ('tahan buku' in msg_lower) or
        msg_lower.startswith('reservasi')
    )
    
    if is_opac_hold:
        barcode_m = re.search(r'(?:barcode|no\.?\s*induk)\s*:\*?\s*([^\n\r]+)', msg_strip, re.IGNORECASE)
        barcode_val = barcode_m.group(1).strip().replace('*', '').strip() if barcode_m else ''
        if not barcode_val or barcode_val in ['-', 'none', 'null']:
            tahan_cmd_m = re.search(r'(?:tahan\s+buku|reservasi)\s+([0-9a-zA-Z\/\-_]+)', msg_strip, re.IGNORECASE)
            if tahan_cmd_m:
                barcode_val = tahan_cmd_m.group(1).strip()
            else:
                barcode_val = ''
            
        judul_m = re.search(r'judul\s*:\*?\s*([^\n\r]+)', msg_strip, re.IGNORECASE)
        judul_val = judul_m.group(1).strip().replace('*', '').strip() if judul_m else ''
        
        pengarang_m = re.search(r'(?:pengarang|penulis)\s*:\*?\s*([^\n\r]+)', msg_strip, re.IGNORECASE)
        pengarang_val = pengarang_m.group(1).strip().replace('*', '').strip() if pengarang_m else ''

        is_direct_hold = 'tahan buku' in msg_lower or msg_lower.startswith('reservasi')

        book_info = None
        if barcode_val:
            book_info = conn.execute("""
                SELECT b.id as biblio_id, b.judul, b.pengarang, b.klasifikasi, e.no_induk, e.lokasi, e.status_buku,
                       (SELECT COUNT(*) FROM sirkulasi s WHERE s.no_induk = e.no_induk AND s.return_date IS NULL) as is_dipinjam,
                       (SELECT s.due_date FROM sirkulasi s WHERE s.no_induk = e.no_induk AND s.return_date IS NULL LIMIT 1) as batas_kembali
                FROM eksemplar e
                JOIN bibliografi b ON e.biblio_id = b.id
                WHERE e.no_induk = ? OR e.no_induk LIKE ?
                LIMIT 1
            """, (barcode_val, f"%{barcode_val}%")).fetchone()

        if not book_info and judul_val:
            book_info = conn.execute("""
                SELECT b.id as biblio_id, b.judul, b.pengarang, b.klasifikasi, e.no_induk, e.lokasi, e.status_buku,
                       (SELECT COUNT(*) FROM sirkulasi s WHERE s.no_induk = e.no_induk AND s.return_date IS NULL) as is_dipinjam,
                       (SELECT s.due_date FROM sirkulasi s WHERE s.no_induk = e.no_induk AND s.return_date IS NULL LIMIT 1) as batas_kembali
                FROM bibliografi b
                LEFT JOIN eksemplar e ON b.id = e.biblio_id
                WHERE b.judul LIKE ?
                LIMIT 1
            """, (f"%{judul_val}%",)).fetchone()

        if is_direct_hold:
            target_id = book_info['biblio_id'] if book_info else None
            target_title = book_info['judul'] if book_info else (judul_val or 'Buku Katalog')
            target_code = (book_info['no_induk'] if book_info else None) or barcode_val or '-'
            if target_id:
                try:
                    conn.execute(
                        "INSERT INTO reservasi (biblio_id, nim_pemustaka, tanggal, status) VALUES (?, ?, CURRENT_TIMESTAMP, 'MENUNGGU_PENGAMBILAN')",
                        (target_id, nomor_wa_lokal)
                    )
                    conn.commit()
                except Exception as e_res:
                    print("Error DB reservasi:", e_res)
            
            conn.close()
            reply_hold = (
                f"Halo Kak {name}!\n\n"
                f"Permintaan penahanan (HOLD) buku telah kami terima:\n"
                f"- Judul: *{target_title}*\n"
                f"- No. Barcode: {target_code}\n"
                f"- Pemesan: *{name}* ({nomor_wa_lokal})\n\n"
                f"Status: BERHASIL DITAHAN di Meja Layanan Sirkulasi Perpustakaan St. Jerome selama 1x24 Jam Kerja.\n\n"
                f"Silakan datang ke perpustakaan dan tunjukkan pesan WhatsApp ini atau kartu identitas/NIM Anda saat mengambil buku. Terima kasih!"
            )
            return jsonify({'reply': reply_hold})

        if book_info:
            b_judul = book_info['judul']
            b_pengarang = book_info['pengarang'] or '-'
            b_barcode = book_info['no_induk'] or barcode_val or '-'
            b_klas = book_info['klasifikasi'] or '-'
            b_lokasi = book_info['lokasi'] or 'IMAVI'
            is_dipinjam = book_info['is_dipinjam'] > 0

            if is_dipinjam:
                tgl_kembali = book_info['batas_kembali'] or 'segera'
                reply_inq = (
                    f"Halo Kak {name}!\n\n"
                    f"Kami telah memeriksa sistem katalog untuk buku:\n"
                    f"- Judul: *{b_judul}*\n"
                    f"- Pengarang: {b_pengarang}\n"
                    f"- Barcode: {b_barcode}\n\n"
                    f"Status: Saat ini buku SEDANG DIPINJAM oleh anggota lain dengan estimasi pengembalian tanggal {tgl_kembali}.\n\n"
                    f"Jika ingin diprioritaskan saat buku dikembalikan, Kakak dapat membalas chat ini dengan ketik: *RESERVASI {b_barcode}*."
                )
            else:
                reply_inq = (
                    f"Halo Kak {name}!\n\n"
                    f"Buku yang Kakak tanyakan dari katalog OPAC:\n"
                    f"- Judul: *{b_judul}*\n"
                    f"- Pengarang: {b_pengarang}\n"
                    f"- No. Panggil: {b_klas}\n"
                    f"- Barcode: {b_barcode}\n\n"
                    f"Status: *TERSEDIA* di rak koleksi ({b_lokasi}) dan siap dibaca di tempat atau dipinjam.\n\n"
                    f"Apakah Kakak ingin buku ini disiapkan dan DITAHAN (HOLD) di meja sirkulasi selama 1x24 jam kerja sebelum Kakak tiba?\n\n"
                    f"Jika YA, silakan balas chat ini dengan: *TAHAN BUKU {b_barcode}*."
                )
            conn.close()
            return jsonify({'reply': reply_inq})
        else:
            conn.close()
            reply_inq = (
                f"Halo Kak {name}!\n\n"
                f"Terima kasih telah menghubungi Perpustakaan St. Jerome. Kami telah menerima permohonan pengecekan buku:\n"
                f"- Judul: {judul_val or '-'}\n"
                f"- Pengarang: {pengarang_val or '-'}\n"
                f"- Barcode: {barcode_val or '-'}\n\n"
                f"Petugas sirkulasi kami akan segera mengecek langsung ke rak fisik dan mengabari Kakak kembali dalam beberapa saat. Terima kasih!"
            )
            return jsonify({'reply': reply_inq})

    # 1. INTERSEPSI FORMULIR USULAN BUKU (DIRECT DB INSERT)
    is_usulan_form = (
        '#usulan' in msg_lower or
        (re.search(r'judul(?:\s+buku)?\s*:', msg_strip, re.IGNORECASE) and 
         re.search(r'(?:pengarang|penulis|nama\s+penulis(?:nya)?|nama\s+pengarang(?:nya)?)\s*:', msg_strip, re.IGNORECASE))
    ) and not is_opac_hold
    
    if is_usulan_form:
        judul_m = re.search(r'judul(?:\s+buku)?\s*:\s*(.+)', msg_strip, re.IGNORECASE)
        pengarang_m = re.search(r'(?:pengarang|penulis|nama\s+penulis(?:nya)?|nama\s+pengarang(?:nya)?)\s*:\s*(.+)', msg_strip, re.IGNORECASE)
        catatan_m = re.search(r'catatan\s*:\s*(.+)', msg_strip, re.IGNORECASE)
        
        judul = judul_m.group(1).strip() if judul_m else ''
        pengarang = pengarang_m.group(1).strip() if pengarang_m else ''
        catatan_val = catatan_m.group(1).strip() if catatan_m else ''
        
        # Bersihkan placeholder format jika user tidak menghapus kurung contoh
        for placeholder in ['(tulis judul buku)', '(tulis judul)', '(judul buku)', 'tulis judul buku']:
            if judul.lower() == placeholder:
                judul = ''
        for placeholder in ['(tulis nama penulis/pengarang)', '(tulis nama pengarang)', '(pengarang)', 'tulis nama pengarang']:
            if pengarang.lower() == placeholder:
                pengarang = ''
        if catatan_val.lower() in ['(alasan usulan / edisi / kebutuhan matakuliah)', '(kebutuhan / matakuliah / edisi)', '(catatan)']:
            catatan_val = ''
            
        if judul and len(judul) >= 2:
            catatan_full = f"{catatan_val} (Diusulkan via WhatsApp oleh {name} - {nomor_wa_lokal})".strip()
            if not catatan_val:
                catatan_full = f"Diusulkan via WhatsApp oleh {name} ({nomor_wa_lokal})"
                
            try:
                conn.execute(
                    "INSERT INTO usulan_buku (judul_topik, pengarang, catatan, status, sumber) VALUES (?, ?, ?, 'Menunggu', 'WhatsApp')",
                    (judul, pengarang, catatan_full)
                )
                conn.commit()
                conn.close()
                
                reply_sukses = (
                    f"Halo Kak {name}! Terima kasih banyak, usulan buku Anda telah BERHASIL TERCATAT ke dalam database pengadaan Perpustakaan St. Jerome:\n\n"
                    f"- Judul: {judul}\n"
                    f"- Pengarang: {pengarang or '-'}\n"
                    f"- Catatan: {catatan_val or '-'}\n"
                    f"- Status: Menunggu Verifikasi\n\n"
                    f"Tim pustakawan kami akan meninjau usulan ini untuk proses pengadaan koleksi berikutnya. Anda juga dapat mengecek katalog kami di: https://yosefhomeserver.my.id/opac"
                )
                return jsonify({'reply': reply_sukses})
            except Exception as e_db:
                conn.close()
                print("Error DB usulan_buku:", e_db)
                return jsonify({'reply': f"Mohon maaf Kak {name}, terjadi kendala saat menyimpan usulan buku: {str(e_db)}. Silakan hubungi pustakawan kami."})
        else:
            conn.close()
            reply_panduan = (
                f"Halo Kak {name}, judul buku belum terisi pada formulir usulan. Silakan kirim ulang dengan format berikut ya:\n\n"
                f"#USULAN BUKU\n"
                f"Judul: (tulis judul buku)\n"
                f"Pengarang: (tulis nama pengarang)\n"
                f"Catatan: (alasan usulan / kebutuhan kuliah)"
            )
            return jsonify({'reply': reply_panduan})

    # 2. INTERSEPSI PERTANYAAN CARA USUL BUKU (KIRIM FORMAT FORMULIR)
    tanya_usul_keywords = ['mau usul buku', 'cara usul buku', 'bisa usul buku', 'usulkan buku', 'saran buku', 'sarankan buku', 'rekomendasi buku baru', 'mau usul', 'usul judul', 'pengadaan buku']
    if any(k in msg_lower for k in tanya_usul_keywords):
        conn.close()
        reply_form = (
            f"Halo Kak {name}! Untuk mengusulkan buku atau referensi baru agar dapat diadakan oleh Perpustakaan St. Jerome, silakan salin format formulir berikut, lengkapi datanya, lalu kirim kembali ke obrolan ini:\n\n"
            f"#USULAN BUKU\n"
            f"Judul: \n"
            f"Pengarang: \n"
            f"Catatan: \n\n"
            f"Setelah format di atas dikirim, sistem kami akan langsung mencatat usulan Anda ke database pengadaan perpustakaan.\n"
            f"Koleksi buku kami juga dapat dicek di: https://yosefhomeserver.my.id/opac"
        )
        return jsonify({'reply': reply_form})

    # 3. INTERSEPSI LAYANAN PRINT / FOTOCOPY (ARAHKAN KE LINK PORTAL MANDIRI)
    print_keywords = ['mau print', 'bisa print', 'ngeprint', 'nge-print', 'cara print', 'cetak dokumen', 'cetak file', 'cetak pdf', 'mau cetak', 'bisa fotocopy', 'bisa fotokopi', 'layanan print', 'link print', 'upload print', 'print mandiri', 'fotocopy', 'fotokopi', 'print tugas', 'print skripsi']
    is_seeking_print_book = 'buku' in msg_lower and any(b in msg_lower for b in ['tentang', 'mengenai', 'judul'])
    if any(k in msg_lower for k in print_keywords) and not is_seeking_print_book:
        conn.close()
        reply_print = (
            f"Halo Kak {name}! Untuk layanan cetak dokumen (print mandiri) dan fotocopy di Perpustakaan St. Jerome, silakan unggah file Anda langsung melalui portal cetak mandiri kami:\n\n"
            f"https://yosefhomeserver.my.id/pos/kirim_print\n\n"
            f"Melalui link tersebut Anda dapat:\n"
            f"1. Mengunggah file PDF/dokumen secara langsung\n"
            f"2. Memilih preferensi cetak (Warna atau Hitam-Putih)\n"
            f"3. Melihat estimasi total biaya secara otomatis\n"
            f"4. Mengambil hasil cetakan langsung di meja sirkulasi perpustakaan\n\n"
            f"Catatan: Mohon jangan mengirim file dokumen ke ruang obrolan WhatsApp ini ya. Terima kasih!"
        )
        return jsonify({'reply': reply_print})

    # 4. PEMERIKSAAN KONFIGURASI GEMINI AI
    config = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'GEMINI_API_KEY'").fetchone()
    api_key = config['nilai'] if config else None
    
    info_cfg = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'INFO_PERPUSTAKAAN'").fetchone()
    info_perpus = info_cfg['nilai'] if info_cfg and info_cfg['nilai'] else "Buka: Senin - Jumat (08:00 - 16:00).\nAturan pinjam: Reguler 2 buku, Tesis 4 buku."
    
    if not api_key:
        conn.close()
        return jsonify({'reply': 'Mohon maaf, sistem AI perpustakaan saat ini sedang dinonaktifkan (API Key belum diisi oleh Admin).'})

    # DATA PINJAMAN ANGGOTA
    peminjaman_context = "USER_TIDAK_TERDAFTAR"
    if anggota:
        pinjaman = conn.execute("""
            SELECT b.judul, s.loan_date as tanggal_pinjam, s.due_date as batas_kembali, s.fine_amount as denda 
            FROM sirkulasi s
            JOIN eksemplar e ON s.no_induk = e.no_induk
            JOIN bibliografi b ON e.biblio_id = b.id
            WHERE s.member_id = ? AND s.return_date IS NULL
        """, (anggota['member_id'],)).fetchall()
        
        if pinjaman:
            peminjaman_context = f"Anggota bernama {name} saat ini sedang meminjam buku berikut:\n"
            for p in pinjaman:
                peminjaman_context += f"- Judul: {p['judul']}, Pinjam: {p['tanggal_pinjam']}, Batas Kembali: {p['batas_kembali']}, Denda Berjalan: Rp{p['denda'] or 0}\n"
        else:
            peminjaman_context = f"Anggota bernama {name} saat ini TIDAK memiliki pinjaman buku yang aktif."

    # AI RAG: PENCARIAN KATALOG
    stop_words = ['ada', 'buku', 'gak', 'nggak', 'tidak', 'yang', 'tentang', 'judul', 'pengarang', 'tolong', 'cari', 'carikan', 'apakah']
    words = [w for w in msg_lower.replace('?', '').replace('!', '').replace(',', '').split() if w not in stop_words and len(w) > 2]
    
    buku_context = ""
    if words:
        conditions = " OR ".join(["b.judul LIKE ? OR b.pengarang LIKE ?" for _ in words])
        params = []
        for w in words:
            params.extend([f"%{w}%", f"%{w}%"])
            
        buku = conn.execute(f"SELECT b.judul, b.pengarang, e.lokasi, e.status_ketersediaan as status_buku FROM bibliografi b JOIN eksemplar e ON b.id = e.biblio_id WHERE e.lokasi = 'IMAVI' AND ({conditions}) LIMIT 15", params).fetchall()
        if buku:
            buku_context = "\n".join([f"- {b['judul']} (Oleh: {b['pengarang']}) - Lokasi: {b['lokasi']} [{b['status_buku']}]" for b in buku])
        else:
            buku_context = "Tidak ditemukan buku yang cocok dengan pencarian di database saat ini."
            kata_kunci = " ".join(words)
            if len(kata_kunci) > 3:
                try:
                    conn.execute(
                        "INSERT INTO pencarian_gagal (keyword, jumlah_pencarian, update_terakhir) VALUES (?, 1, CURRENT_TIMESTAMP)",
                        (kata_kunci,)
                    )
                    conn.commit()
                except Exception:
                    pass
    else:
        buku = conn.execute("SELECT b.judul, b.pengarang, e.lokasi, e.status_ketersediaan as status_buku FROM bibliografi b JOIN eksemplar e ON b.id = e.biblio_id WHERE e.lokasi = 'IMAVI' LIMIT 5").fetchall()
        buku_context = "\n".join([f"- {b['judul']} (Oleh: {b['pengarang']}) - Lokasi: {b['lokasi']} [{b['status_buku']}]" for b in buku])
        
    conn.close()

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        
        model = genai.GenerativeModel('gemini-3.8-flash')
        
        system_prompt = f"""Anda adalah Asisten AI Perpustakaan St. Jerome yang ramah, santun, dan cerdas. Berbicaralah secara natural layaknya pustakawan profesional. Jangan menggunakan menu angka-angka kaku (seperti "Tekan 1 untuk X").

Konteks Pencarian Katalog (jika user mencari buku): {buku_context}
Data Peminjaman User (jika user menanyakan status pinjamannya): {peminjaman_context}
Info Perpustakaan: {info_perpus}

ATURAN PENTING:
1. Jika user sekadar menyapa, balas dengan ramah dan tawarkan bantuan (misal: mencari buku, mengecek pinjaman, portal print mandiri, atau mengusulkan buku baru).
2. Jika user mencari buku:
   - Jika ada di Konteks Pencarian, sebutkan judul, pengarang, dan ketersediaannya.
   - Jika TIDAK ADA di katalog atau user ingin usul/saran buku baru, beri tahu mereka bahwa mereka bisa mengusulkannya dengan menyalin dan mengisi formulir berikut ke obrolan ini:
     #USULAN BUKU
     Judul: 
     Pengarang: 
     Catatan: 
     Jelaskan bahwa jika mereka mengirim format tersebut, usulan mereka akan otomatis langsung masuk ke database pengadaan perpustakaan kami. Sertakan link katalog OPAC: https://yosefhomeserver.my.id/opac
3. Jika user mengecek status peminjaman namun sistem menampilkan "USER_TIDAK_TERDAFTAR", mintalah mereka untuk mengetikkan Nomor ID/Nomor Anggota mereka.
4. Jika user bertanya tentang layanan Fotocopy atau Print / Cetak dokumen:
   - Selalu arahkan user untuk mengunggah dokumen mereka secara mandiri ke link portal cetak:
     https://yosefhomeserver.my.id/pos/kirim_print
   - Jelaskan bahwa di link tersebut mereka dapat mengunggah file (PDF/Word), memilih opsi cetak (Hitam-Putih / Warna), melihat estimasi biaya, dan mengambil hasil cetakan di meja sirkulasi Perpustakaan St. Jerome.
   - DILARANG KERAS meminta atau mengizinkan user mengirimkan file dokumen langsung ke obrolan WhatsApp ini.
5. Jawablah secara ringkas (maksimal 2-3 paragraf pendek) agar nyaman dibaca di layar HP.
6. PENTING: DILARANG KERAS menggunakan format Markdown tanda bintang ganda (**) untuk menebalkan teks. Jangan gunakan simbol bintang sama sekali dalam memformat tulisan.
"""

        response = model.generate_content([system_prompt, message])
        return jsonify({'reply': response.text})
    except Exception as e:
        print("Error Gemini:", e)
        return jsonify({'reply': f"Maaf, error sistem AI: {str(e)}"})


@router.route('/wa_broadcast')
@login_required
@feature_required('ENABLE_WA_BOT', 'Modul WhatsApp dinonaktifkan sementara (mode Telegram-only).', api_status=200)
def wa_broadcast():
    conn = database.get_db_connection()
    total = conn.execute("SELECT COUNT(*) FROM anggota WHERE status = 'AKTIF'").fetchone()[0]
    conn.close()
    return render_template('wa_broadcast.html', total_anggota=total)


@router.route('/api/wa_status')
@login_required
@feature_required('ENABLE_WA_BOT', 'Modul WhatsApp dinonaktifkan sementara (mode Telegram-only).', api_status=200)
def api_wa_status():
    try:
        import requests
        resp = requests.get('http://127.0.0.1:3005/api/status', timeout=3)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({'status': 'offline', 'message': str(e), 'qr': None})


@router.route('/api/wa_groups')
@api_login_required
@feature_required('ENABLE_WA_BOT', 'Modul WhatsApp dinonaktifkan sementara (mode Telegram-only).', api_status=200)
def api_wa_groups():
    try:
        import requests
        resp = requests.get('http://127.0.0.1:3005/api/groups', timeout=5)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@router.route('/api/wa_broadcast', methods=['POST'])
@api_login_required
@feature_required('ENABLE_WA_BOT', 'Modul WhatsApp dinonaktifkan sementara (mode Telegram-only).', api_status=200)
def api_wa_broadcast():
    data = request.json
    target = data.get('target')
    msg_template = data.get('message')
    
    if not msg_template:
        return jsonify({'status': 'error', 'message': 'Pesan kosong'})
        
    conn = database.get_db_connection()
    
    query = "SELECT nama, telepon FROM anggota WHERE status = 'AKTIF' AND telepon IS NOT NULL AND telepon != ''"
    if target == 'tesis':
        query += " AND is_tesis = 1"
    elif target == 'reguler':
        query += " AND is_tesis = 0"
        
    members = conn.execute(query).fetchall()
    conn.close()
    
    if not members:
        return jsonify({'status': 'error', 'message': 'Tidak ada anggota yang memenuhi kriteria atau memiliki nomor WA.'})
        
    success_count = 0
    import threading
    
    def send_broadcast_background(members_list, template):
        for m in members_list:
            final_msg = template.replace('[NAMA]', m['nama'])
            try:
                data = json.dumps({'number': m['telepon'], 'message': final_msg}).encode('utf-8')
                req = urllib.request.Request('http://127.0.0.1:3005/api/send_message', data=data, headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=5)
            except:
                pass
                
    # Run in background to avoid blocking the UI
    threading.Thread(target=send_broadcast_background, args=(members, msg_template)).start()
    
    return jsonify({'status': 'success', 'message': f'Broadcast sedang dikirim ke {len(members)} anggota di latar belakang.'})


@router.route('/api/sirkulasi/kirim_wa_komunitas', methods=['POST'])
@login_required
@feature_required('ENABLE_WA_BOT', 'Modul WhatsApp dinonaktifkan sementara (mode Telegram-only).', api_status=200)
def api_kirim_wa_komunitas():
    data = request.get_json() or {}
    target_number = data.get('number', '6281130899997').strip()
    custom_msg = data.get('message', '').strip()
    
    if not custom_msg:
        return jsonify({'status': 'error', 'message': 'Pesan tidak boleh kosong.'}), 400

    try:
        payload = json.dumps({'number': target_number, 'message': custom_msg}).encode('utf-8')
        req = urllib.request.Request('http://127.0.0.1:3005/api/send_message', data=payload, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp_data = json.loads(resp.read().decode('utf-8'))
            return jsonify({'status': 'success', 'message': 'Pesan pengingat berhasil dikirim ke nomor komunitas!', 'gateway_response': resp_data})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Gagal mengirim melalui bot WhatsApp: {str(e)}'}), 500


