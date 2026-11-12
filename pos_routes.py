import os
import json
import datetime
from flask import Blueprint, request, jsonify, render_template, session, redirect, current_app
from werkzeug.utils import secure_filename
import database

pos_bp = Blueprint('pos', __name__)


@pos_bp.before_request
def _pos_feature_guard():
    from core.features import is_enabled
    if not is_enabled('ENABLE_POS_FOTOCOPY'):
        if request.path.startswith('/api/') or request.is_json:
            return jsonify({'status': 'disabled', 'message': 'Layanan Kasir POS dinonaktifkan pada mode aplikasi saat ini.'}), 404
        from flask import flash
        flash('Layanan Kasir POS dinonaktifkan pada mode aplikasi saat ini.', 'error')
        return redirect('/')

def pos_login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function

def pos_api_login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return jsonify({'status': 'error', 'message': 'Sesi telah habis, silakan login kembali.'}), 401
        return f(*args, **kwargs)
    return decorated_function

def send_wa_pos(member_id_or_phone, message):
    try:
        from core.features import is_enabled
        if not is_enabled('ENABLE_WA_BOT'):
            return
        import threading, urllib.request
        phone = str(member_id_or_phone).strip()
        if not phone.startswith(('08', '62', '+62')):
            conn = database.get_db_connection()
            mem = conn.execute("SELECT telepon FROM anggota WHERE member_id = ?", (phone,)).fetchone()
            conn.close()
            if mem and mem['telepon']:
                phone = mem['telepon']
            else:
                return
        def _send():
            try:
                data = json.dumps({'number': phone, 'message': message}).encode('utf-8')
                req = urllib.request.Request('http://127.0.0.1:3005/api/send_message', data=data, headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=5)
            except:
                pass
        threading.Thread(target=_send).start()
    except:
        pass

# =========================================================================
# WEB PAGES
# =========================================================================

@pos_bp.route('/pos')
@pos_login_required
def pos_kasir():
    return render_template('pos/kasir.html')

@pos_bp.route('/pos/tabungan')
@pos_login_required
def pos_tabungan():
    return render_template('pos/tabungan.html')

@pos_bp.route('/pos/riwayat')
@pos_login_required
def pos_riwayat():
    return render_template('pos/riwayat.html')

@pos_bp.route('/pos/layanan')
@pos_login_required
def pos_layanan():
    return render_template('pos/layanan.html')

@pos_bp.route('/pos/kirim_print')
def pos_kirim_print():
    # Accessible to members/students without staff login
    member_data = None
    if 'member_id' in session:
        conn = database.get_db_connection()
        acc = conn.execute("SELECT * FROM pos_tabungan WHERE member_id = ?", (session['member_id'],)).fetchone()
        conn.close()
        if acc:
            member_data = dict(acc)
    return render_template('pos/kirim_print.html', member_data=member_data)

@pos_bp.route('/pos/cetak_struk/<path:no_transaksi>')
@pos_login_required
def pos_cetak_struk(no_transaksi):
    conn = database.get_db_connection()
    trx = conn.execute("SELECT * FROM pos_transaksi WHERE no_transaksi = ?", (no_transaksi,)).fetchone()
    if not trx:
        conn.close()
        return "Transaksi tidak ditemukan.", 404
        
    raw_items = conn.execute("SELECT * FROM pos_transaksi_detail WHERE transaksi_id = ?", (trx['id'],)).fetchall()
    
    # Detail tabungan jika ada
    tabungan = None
    if trx['no_rekening']:
        tabungan = conn.execute("SELECT * FROM pos_tabungan WHERE no_rekening = ?", (trx['no_rekening'],)).fetchone()
    conn.close()
    
    formatted_items = []
    for row in raw_items:
        it = dict(row)
        full_name = it.get('nama_layanan', '')
        if '(' in full_name and ')' in full_name:
            main_title = full_name[:full_name.find('(')].strip()
            sub_info = full_name[full_name.find('(')+1 : full_name.rfind(')')].strip()
        else:
            main_title = full_name
            sub_info = ''
            
        # Format nama file panjang agar rapi dan kompak di nota thermal
        if sub_info:
            parts = sub_info.split(' - ')
            if len(parts) >= 2:
                fname = parts[0].strip()
                specs = ' - '.join(parts[1:]).strip()
                if len(fname) > 22:
                    fname = fname[:18] + '...' + (fname[-4:] if '.' in fname[-5:] else '')
                sub_info = f"{fname} ({specs})"
            elif len(sub_info) > 32:
                sub_info = sub_info[:28] + '...'
                
        it['main_title'] = main_title
        it['sub_info'] = sub_info
        formatted_items.append(it)
        
        trx_dict = dict(trx)
    raw_tgl = trx_dict.get('tanggal', '')
    if raw_tgl:
        try:
            parts = raw_tgl.strip().split(' ')
            d_parts = parts[0].split('-')
            jam_part = parts[1][:5] if len(parts) > 1 else ''
            trx_dict['formatted_tgl'] = f"{d_parts[2]}-{d_parts[1]}-{d_parts[0]} {jam_part} WIB"
        except Exception:
            trx_dict['formatted_tgl'] = f"{raw_tgl} WIB"
    else:
        trx_dict['formatted_tgl'] = '-'
        
    return render_template('pos/struk.html', trx=trx_dict, items=formatted_items, tabungan=dict(tabungan) if tabungan else None)

# =========================================================================
# API ENDPOINTS
# =========================================================================

@pos_bp.route('/api/pos/init_data', methods=['GET'])
@pos_api_login_required
def api_pos_init_data():
    conn = database.get_db_connection()
    layanan_rows = conn.execute("SELECT * FROM pos_layanan WHERE is_active = 1 ORDER BY urutan ASC, id ASC").fetchall()
    
    # Generate next no_transaksi
    today_str = datetime.datetime.now().strftime('%Y%m%d')
    today_prefix = f"KSR-{today_str}-"
    last_trx = conn.execute("SELECT no_transaksi FROM pos_transaksi WHERE no_transaksi LIKE ? ORDER BY id DESC LIMIT 1", (f"{today_prefix}%",)).fetchone()
    
    next_num = 1
    if last_trx:
        try:
            next_num = int(last_trx['no_transaksi'].split('-')[-1]) + 1
        except:
            next_num = 1
    next_no_transaksi = f"{today_prefix}{next_num:04d}"
    
    # Active print queue count
    queue_count = conn.execute("SELECT COUNT(*) FROM pos_antrean_print WHERE status = 'MENUNGGU'").fetchone()[0]
    
    conn.close()
    return jsonify({
        'status': 'success',
        'layanan': [dict(r) for r in layanan_rows],
        'next_no_transaksi': next_no_transaksi,
        'queue_count': queue_count,
        'kasir': session.get('username', 'Kasir St. Jerome')
    })

@pos_bp.route('/api/pos/cari_nasabah', methods=['GET'])
@pos_api_login_required
def api_pos_cari_nasabah():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'status': 'success', 'data': []})
        
    conn = database.get_db_connection()
    # Search by no_rekening exact, member_id exact (barcode scan), or name wildcard
    rows = conn.execute("""
        SELECT t.*, a.telepon, a.email, a.tipe_anggota, a.institusi
        FROM pos_tabungan t
        LEFT JOIN anggota a ON t.member_id = a.member_id
        WHERE t.no_rekening = ? OR t.member_id = ? OR t.nama_nasabah LIKE ? OR a.nama LIKE ?
        ORDER BY (t.no_rekening = ? OR t.member_id = ?) DESC, t.nama_nasabah ASC
        LIMIT 10
    """, (q, q, f"%{q}%", f"%{q}%", q, q)).fetchall()
    conn.close()
    
    results = [dict(r) for r in rows]
    return jsonify({'status': 'success', 'data': results})

@pos_bp.route('/api/pos/transaksi', methods=['POST'])
@pos_api_login_required
def api_pos_transaksi():
    data = request.json or {}
    items = data.get('items', [])
    if not items:
        return jsonify({'status': 'error', 'message': 'Keranjang transaksi masih kosong!'}), 400
        
    total_tagihan = int(data.get('total_tagihan', 0))
    metode_bayar = data.get('metode_bayar', 'TUNAI').upper()
    no_rekening = data.get('no_rekening')
    nama_pelanggan = data.get('nama_pelanggan', 'UMUM').strip() or 'UMUM'
    bayar_tunai = int(data.get('bayar_tunai', 0))
    catatan = data.get('catatan', '').strip()
    kasir = session.get('username', 'Kasir St. Jerome')
    
    conn = database.get_db_connection()
    try:
        # Generate unique transaction code
        today_str = datetime.datetime.now().strftime('%Y%m%d')
        today_prefix = f"KSR-{today_str}-"
        last_trx = conn.execute("SELECT no_transaksi FROM pos_transaksi WHERE no_transaksi LIKE ? ORDER BY id DESC LIMIT 1", (f"{today_prefix}%",)).fetchone()
        next_num = 1
        if last_trx:
            try: next_num = int(last_trx['no_transaksi'].split('-')[-1]) + 1
            except: next_num = 1
        no_transaksi = f"{today_prefix}{next_num:04d}"
        
        potong_saldo = 0
        kembalian = 0
        saldo_sebelum = 0
        saldo_sesudah = 0
        nasabah = None
        
        # Handle Pembayaran
        if metode_bayar in ['TABUNGAN', 'SPLIT']:
            if not no_rekening:
                return jsonify({'status': 'error', 'message': 'Pilih nasabah tabungan terlebih dahulu!'}), 400
                
            nasabah = conn.execute("SELECT * FROM pos_tabungan WHERE no_rekening = ?", (no_rekening,)).fetchone()
            if not nasabah:
                return jsonify({'status': 'error', 'message': f'Rekening {no_rekening} tidak ditemukan!'}), 404
                
            saldo_sebelum = nasabah['saldo']
            
            if metode_bayar == 'TABUNGAN':
                if saldo_sebelum < total_tagihan:
                    return jsonify({
                        'status': 'error', 
                        'message': f"Saldo tabungan {nasabah['nama_nasabah']} tidak mencukupi (Rp {saldo_sebelum:,} < Rp {total_tagihan:,})! Gunakan fitur Split Payment untuk bayar sisa tunai."
                    }), 400
                potong_saldo = total_tagihan
                saldo_sesudah = saldo_sebelum - potong_saldo
                bayar_tunai = 0
                kembalian = 0
                
            elif metode_bayar == 'SPLIT':
                potong_saldo = min(saldo_sebelum, total_tagihan)
                sisa_tagihan = total_tagihan - potong_saldo
                if bayar_tunai < sisa_tagihan:
                    return jsonify({
                        'status': 'error',
                        'message': f"Sisa tagihan tunai adalah Rp {sisa_tagihan:,}, tetapi uang tunai yang diterima kurang (Rp {bayar_tunai:,})!"
                    }), 400
                saldo_sesudah = saldo_sebelum - potong_saldo
                kembalian = bayar_tunai - sisa_tagihan
        else:
            # TUNAI MURNI
            if bayar_tunai < total_tagihan:
                return jsonify({
                    'status': 'error',
                    'message': f"Uang tunai kurang! Tagihan: Rp {total_tagihan:,}, Diterima: Rp {bayar_tunai:,}."
                }), 400
            potong_saldo = 0
            kembalian = bayar_tunai - total_tagihan
            
        # Hitung total lembar kertas perpustakaan
        lembar_perpus = 0
        for it in items:
            if it.get('is_kertas_perpus'):
                lembar_perpus += int(it.get('qty', 1))
                
        now_dt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 1. Simpan Transaksi Header
        cursor = conn.execute("""
            INSERT INTO pos_transaksi (
                no_transaksi, tanggal, no_rekening, nama_pelanggan, metode_bayar,
                total_tagihan, potong_saldo, bayar_tunai, kembalian,
                lembar_kertas_perpus, kasir, catatan
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            no_transaksi, now_dt, no_rekening, nama_pelanggan, metode_bayar,
            total_tagihan, potong_saldo, bayar_tunai, kembalian,
            lembar_perpus, kasir, catatan
        ))
        transaksi_id = cursor.lastrowid
        
        # 2. Simpan Detail Transaksi
        for it in items:
            conn.execute("""
                INSERT INTO pos_transaksi_detail (
                    transaksi_id, kode_layanan, nama_layanan, harga_satuan, qty, subtotal
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                transaksi_id, it.get('kode', 'ITEM'), it.get('nama_layanan', ''),
                int(it.get('harga', 0)), int(it.get('qty', 1)), int(it.get('subtotal', 0))
            ))
            
        # 3. Potong Tabungan & Catat Mutasi jika ada potongan saldo
        if potong_saldo > 0 and no_rekening:
            conn.execute("""
                UPDATE pos_tabungan SET saldo = ?, total_pakai = total_pakai + ? WHERE no_rekening = ?
            """, (saldo_sesudah, potong_saldo, no_rekening))
            
            ket = f"Transaksi {no_transaksi} ({metode_bayar})"
            conn.execute("""
                INSERT INTO pos_mutasi_saldo (
                    no_rekening, tanggal, jenis, nominal, saldo_sebelum, saldo_sesudah,
                    no_transaksi, keterangan, kasir
                ) VALUES (?, ?, 'POTONG_KASIR', ?, ?, ?, ?, ?, ?)
            """, (
                no_rekening, now_dt, potong_saldo, saldo_sebelum, saldo_sesudah,
                no_transaksi, ket, kasir
            ))
            
        conn.commit()
        
        # 4. WhatsApp Receipt (jika nasabah punya no telp di anggota)
        telepon = None
        if nasabah and nasabah['member_id']:
            mem = conn.execute("SELECT telepon FROM anggota WHERE member_id = ?", (nasabah['member_id'],)).fetchone()
            if mem and mem['telepon']:
                telepon = mem['telepon']
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
    
    # Format WhatsApp Receipt jika nomor ada
    if telepon:
        try:
            wa_text = f"🧾 *NOTA KASIR PERPUSTAKAAN ST. JEROME*\n"
            wa_text += f"No: {no_transaksi}\n"
            wa_text += f"Waktu: {now_dt}\n"
            wa_text += f"Pelanggan: {nama_pelanggan} (Rek: {no_rekening})\n"
            wa_text += f"--------------------------------\n"
            for it in items:
                wa_text += f"• {it.get('nama_layanan')} ({it.get('qty')}x) = Rp {it.get('subtotal'):,}\n"
            wa_text += f"--------------------------------\n"
            wa_text += f"TOTAL: *Rp {total_tagihan:,}*\n"
            wa_text += f"Metode: {metode_bayar}\n"
            if potong_saldo > 0:
                wa_text += f"Potong Tabungan: Rp {potong_saldo:,}\n"
                wa_text += f"💳 *Sisa Saldo Anda: Rp {saldo_sesudah:,}*\n"
            if bayar_tunai > 0:
                wa_text += f"Tunai: Rp {bayar_tunai:,} | Kembali: Rp {kembalian:,}\n"
            wa_text += f"\nTerima kasih atas kunjungan Anda!"
            send_wa_pos(telepon, wa_text)
        except Exception as e:
            print("WA Receipt failed:", e)

    return jsonify({
        'status': 'success',
        'message': f'Transaksi {no_transaksi} berhasil diproses!',
        'no_transaksi': no_transaksi,
        'metode_bayar': metode_bayar,
        'total_tagihan': total_tagihan,
        'potong_saldo': potong_saldo,
        'bayar_tunai': bayar_tunai,
        'kembalian': kembalian,
        'saldo_sesudah': saldo_sesudah if potong_saldo > 0 else None
    })

# =========================================================================
# TABUNGAN APIS
# =========================================================================

@pos_bp.route('/api/pos/tabungan/list', methods=['GET'])
@pos_api_login_required
def api_pos_tabungan_list():
    q = request.args.get('search', '').strip()
    conn = database.get_db_connection()
    if q:
        rows = conn.execute("""
            SELECT t.*, a.telepon, a.email, a.tipe_anggota
            FROM pos_tabungan t
            LEFT JOIN anggota a ON t.member_id = a.member_id
            WHERE t.no_rekening LIKE ? OR t.nama_nasabah LIKE ? OR t.member_id LIKE ? OR a.nama LIKE ?
            ORDER BY t.no_rekening ASC
        """, (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    else:
        rows = conn.execute("""
            SELECT t.*, a.telepon, a.email, a.tipe_anggota
            FROM pos_tabungan t
            LEFT JOIN anggota a ON t.member_id = a.member_id
            ORDER BY t.no_rekening ASC
        """).fetchall()
        
    stats = conn.execute("""
        SELECT COUNT(*) as total_rekening, SUM(saldo) as total_saldo,
               SUM(total_setor) as total_setor, SUM(total_pakai) as total_pakai
        FROM pos_tabungan
    """).fetchone()
    
    conn.close()
    return jsonify({
        'status': 'success',
        'data': [dict(r) for r in rows],
        'stats': dict(stats)
    })

@pos_bp.route('/api/pos/tabungan/setor', methods=['POST'])
@pos_api_login_required
def api_pos_tabungan_setor():
    data = request.json or {}
    no_rekening = data.get('no_rekening')
    nominal = int(data.get('nominal', 0))
    keterangan = data.get('keterangan', 'Setor Tunai Tabungan').strip() or 'Setor Tunai Tabungan'
    kasir = session.get('username', 'Kasir St. Jerome')
    
    if nominal <= 0:
        return jsonify({'status': 'error', 'message': 'Nominal setoran harus lebih dari 0!'}), 400
        
    conn = database.get_db_connection()
    telepon = None
    try:
        acc = conn.execute("SELECT * FROM pos_tabungan WHERE no_rekening = ?", (no_rekening,)).fetchone()
        if not acc:
            return jsonify({'status': 'error', 'message': 'Rekening tidak ditemukan!'}), 404
            
        saldo_sebelum = acc['saldo']
        saldo_sesudah = saldo_sebelum + nominal
        now_dt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        conn.execute("""
            UPDATE pos_tabungan SET saldo = ?, total_setor = total_setor + ? WHERE no_rekening = ?
        """, (saldo_sesudah, nominal, no_rekening))
        
        conn.execute("""
            INSERT INTO pos_mutasi_saldo (
                no_rekening, tanggal, jenis, nominal, saldo_sebelum, saldo_sesudah,
                no_transaksi, keterangan, kasir
            ) VALUES (?, ?, 'SETOR', ?, ?, ?, 'TOPUP_TUNAI', ?, ?)
        """, (no_rekening, now_dt, nominal, saldo_sebelum, saldo_sesudah, keterangan, kasir))
        
        conn.commit()
        
        # Cari nomor WA untuk notifikasi jika ada
        if acc and acc['member_id']:
            mem = conn.execute("SELECT telepon FROM anggota WHERE member_id = ?", (acc['member_id'],)).fetchone()
            if mem and mem['telepon']:
                telepon = mem['telepon']
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
        
    if telepon:
        try:
            msg = f"💰 *SETORAN TABUNGAN PERPUSTAKAAN ST. JEROME*\n"
            msg += f"Rekening: {no_rekening} ({acc['nama_nasabah']})\n"
            msg += f"Waktu: {now_dt}\n"
            msg += f"Setoran: +Rp {nominal:,}\n"
            msg += f"Saldo Baru: *Rp {saldo_sesudah:,}*\n"
            msg += f"Kasir: {kasir}\n\nTerima kasih!"
            send_wa_pos(telepon, msg)
        except Exception:
            pass
            
    return jsonify({
        'status': 'success',
        'message': f"Berhasil setor Rp {nominal:,} ke rekening {no_rekening} ({acc['nama_nasabah']})!",
        'saldo_baru': saldo_sesudah
    })

@pos_bp.route('/api/pos/tabungan/tarik', methods=['POST'])
@pos_api_login_required
def api_pos_tabungan_tarik():
    data = request.json or {}
    no_rekening = data.get('no_rekening')
    nominal = int(data.get('nominal', 0))
    keterangan = data.get('keterangan', 'Penarikan Saldo Tunai').strip() or 'Penarikan Saldo Tunai'
    kasir = session.get('username', 'Kasir St. Jerome')
    
    if nominal <= 0:
        return jsonify({'status': 'error', 'message': 'Nominal penarikan harus lebih dari 0!'}), 400
        
    conn = database.get_db_connection()
    acc = conn.execute("SELECT * FROM pos_tabungan WHERE no_rekening = ?", (no_rekening,)).fetchone()
    if not acc:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Rekening tidak ditemukan!'}), 404
        
    saldo_sebelum = acc['saldo']
    if saldo_sebelum < nominal:
        conn.close()
        return jsonify({'status': 'error', 'message': f'Saldo tidak cukup! Saldo saat ini: Rp {saldo_sebelum:,}.'}), 400
        
    saldo_sesudah = saldo_sebelum - nominal
    now_dt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    conn.execute("""
        UPDATE pos_tabungan SET saldo = ? WHERE no_rekening = ?
    """, (saldo_sesudah, no_rekening))
    
    conn.execute("""
        INSERT INTO pos_mutasi_saldo (
            no_rekening, tanggal, jenis, nominal, saldo_sebelum, saldo_sesudah,
            no_transaksi, keterangan, kasir
        ) VALUES (?, ?, 'TARIK', ?, ?, ?, 'TARIK_TUNAI', ?, ?)
    """, (no_rekening, now_dt, nominal, saldo_sebelum, saldo_sesudah, keterangan, kasir))
    
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'message': f"Berhasil menarik Rp {nominal:,} dari rekening {no_rekening}!",
        'saldo_baru': saldo_sesudah
    })

@pos_bp.route('/api/pos/tabungan/tambah', methods=['POST'])
@pos_api_login_required
def api_pos_tabungan_tambah():
    data = request.json or {}
    no_rekening = data.get('no_rekening', '').strip()
    nama_nasabah = data.get('nama_nasabah', '').strip()
    member_id = data.get('member_id', '').strip() or None
    saldo_awal = int(data.get('saldo_awal', 0))
    catatan = data.get('catatan', '').strip()
    kasir = session.get('username', 'Kasir St. Jerome')
    
    if not no_rekening or not nama_nasabah:
        return jsonify({'status': 'error', 'message': 'Nomor Rekening dan Nama Nasabah wajib diisi!'}), 400
        
    conn = database.get_db_connection()
    exist = conn.execute("SELECT id FROM pos_tabungan WHERE no_rekening = ?", (no_rekening,)).fetchone()
    if exist:
        conn.close()
        return jsonify({'status': 'error', 'message': f'Nomor Rekening {no_rekening} sudah terdaftar!'}), 400
        
    now_dt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        conn.execute("""
            INSERT INTO pos_tabungan (
                no_rekening, member_id, nama_nasabah, saldo, total_setor, catatan, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (no_rekening, member_id, nama_nasabah, saldo_awal, saldo_awal, catatan, now_dt))
        
        if saldo_awal > 0:
            conn.execute("""
                INSERT INTO pos_mutasi_saldo (
                    no_rekening, tanggal, jenis, nominal, saldo_sebelum, saldo_sesudah,
                    no_transaksi, keterangan, kasir
                ) VALUES (?, ?, 'SETOR', ?, 0, ?, 'SALDO_AWAL', 'Saldo awal pembukaan rekening', ?)
            """, (no_rekening, now_dt, saldo_awal, saldo_awal, kasir))
            
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
    return jsonify({'status': 'success', 'message': f'Rekening {no_rekening} atas nama {nama_nasabah} berhasil dibuat!'})

@pos_bp.route('/api/pos/tabungan/mutasi/<path:no_rekening>', methods=['GET'])
@pos_api_login_required
def api_pos_tabungan_mutasi(no_rekening):
    conn = database.get_db_connection()
    acc = conn.execute("SELECT * FROM pos_tabungan WHERE no_rekening = ?", (no_rekening,)).fetchone()
    if not acc:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Rekening tidak ditemukan!'}), 404
        
    rows = conn.execute("""
        SELECT * FROM pos_mutasi_saldo WHERE no_rekening = ? ORDER BY id DESC LIMIT 50
    """, (no_rekening,)).fetchall()
    conn.close()
    return jsonify({
        'status': 'success',
        'nasabah': dict(acc),
        'mutasi': [dict(r) for r in rows]
    })

# =========================================================================
# RIWAYAT & REKAP TUTUP KAS
# =========================================================================

@pos_bp.route('/api/pos/riwayat/list', methods=['GET'])
@pos_api_login_required
def api_pos_riwayat_list():
    tanggal = request.args.get('tanggal', datetime.datetime.now().strftime('%Y-%m-%d'))
    conn = database.get_db_connection()
    
    rows = conn.execute("""
        SELECT t.*, 
               (SELECT GROUP_CONCAT(d.nama_layanan || ' (' || d.qty || 'x)', ', ') 
                FROM pos_transaksi_detail d WHERE d.transaksi_id = t.id) as item_summary
        FROM pos_transaksi t
        WHERE date(t.tanggal) = date(?)
        ORDER BY t.id DESC
    """, (tanggal,)).fetchall()
    
    conn.close()
    return jsonify({'status': 'success', 'data': [dict(r) for r in rows]})

@pos_bp.route('/api/pos/rekap_harian', methods=['GET'])
@pos_bp.route('/api/pos/riwayat/rekap_harian', methods=['GET'])
@pos_api_login_required
def api_pos_rekap_harian():
    tanggal = request.args.get('tanggal', datetime.datetime.now().strftime('%Y-%m-%d'))
    conn = database.get_db_connection()
    
    # 1. Penjualan Kasir
    trx_stats = conn.execute("""
        SELECT 
            COUNT(*) as total_transaksi,
            COALESCE(SUM(total_tagihan), 0) as omset_kotor,
            COALESCE(SUM(potong_saldo), 0) as non_tunai_tabungan,
            COALESCE(SUM(bayar_tunai - kembalian), 0) as tunai_penjualan_bersih,
            COALESCE(SUM(lembar_kertas_perpus), 0) as lembar_perpus_terpakai
        FROM pos_transaksi
        WHERE date(tanggal) = date(?)
    """, (tanggal,)).fetchone()
    
    # 2. Setoran Tunai Top-up Tabungan hari ini
    topup_stats = conn.execute("""
        SELECT COALESCE(SUM(nominal), 0) as total_setoran_tunai
        FROM pos_mutasi_saldo
        WHERE date(tanggal) = date(?) AND jenis = 'SETOR' AND no_transaksi != 'SALDO_AWAL'
    """, (tanggal,)).fetchone()
    
    # 3. Penarikan Tunai Tabungan hari ini
    tarik_stats = conn.execute("""
        SELECT COALESCE(SUM(nominal), 0) as total_tarik_tunai
        FROM pos_mutasi_saldo
        WHERE date(tanggal) = date(?) AND jenis = 'TARIK'
    """, (tanggal,)).fetchone()
    
    conn.close()
    
    tunai_jual = trx_stats['tunai_penjualan_bersih']
    setor_tunai = topup_stats['total_setoran_tunai']
    tarik_tunai = tarik_stats['total_tarik_tunai']
    total_fisik_laci = tunai_jual + setor_tunai - tarik_tunai
    
    return jsonify({
        'status': 'success',
        'tanggal': tanggal,
        'total_transaksi': trx_stats['total_transaksi'],
        'omset_total': trx_stats['omset_kotor'],
        'non_tunai_tabungan': trx_stats['non_tunai_tabungan'],
        'tunai_penjualan': tunai_jual,
        'setoran_tunai_topup': setor_tunai,
        'penarikan_tunai': tarik_tunai,
        'total_uang_fisik_laci': total_fisik_laci,
        'lembar_kertas_perpus': trx_stats['lembar_perpus_terpakai']
    })

# =========================================================================
# MASTER LAYANAN JASA
# =========================================================================

@pos_bp.route('/api/pos/layanan/list', methods=['GET'])
@pos_api_login_required
def api_pos_layanan_list():
    conn = database.get_db_connection()
    rows = conn.execute("SELECT * FROM pos_layanan ORDER BY urutan ASC, id ASC").fetchall()
    conn.close()
    return jsonify({'status': 'success', 'data': [dict(r) for r in rows]})

@pos_bp.route('/api/pos/layanan/simpan', methods=['POST'])
@pos_api_login_required
def api_pos_layanan_simpan():
    data = request.json or {}
    item_id = data.get('id')
    kode = data.get('kode', '').strip().upper()
    nama_layanan = data.get('nama_layanan', '').strip().upper()
    kategori = data.get('kategori', 'FOTOCOPY').strip().upper()
    satuan = data.get('satuan', 'Lembar').strip()
    harga = int(data.get('harga', 0))
    is_kertas_perpus = 1 if data.get('is_kertas_perpus') else 0
    urutan = int(data.get('urutan', 99))
    
    if not kode or not nama_layanan or harga < 0:
        return jsonify({'status': 'error', 'message': 'Kode, Nama Layanan, dan Harga wajib diisi!'}), 400
        
    conn = database.get_db_connection()
    if item_id:
        conn.execute("""
            UPDATE pos_layanan SET 
                kode = ?, nama_layanan = ?, kategori = ?, satuan = ?, 
                harga = ?, is_kertas_perpus = ?, urutan = ?
            WHERE id = ?
        """, (kode, nama_layanan, kategori, satuan, harga, is_kertas_perpus, urutan, item_id))
    else:
        conn.execute("""
            INSERT OR REPLACE INTO pos_layanan (
                kode, nama_layanan, kategori, satuan, harga, is_kertas_perpus, urutan
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (kode, nama_layanan, kategori, satuan, harga, is_kertas_perpus, urutan))
        
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': 'Layanan berhasil disimpan!'})

@pos_bp.route('/api/pos/layanan/toggle', methods=['POST'])
@pos_api_login_required
def api_pos_layanan_toggle():
    data = request.json or {}
    item_id = data.get('id')
    is_active = 1 if data.get('is_active') else 0
    conn = database.get_db_connection()
    conn.execute("UPDATE pos_layanan SET is_active = ? WHERE id = ?", (is_active, item_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

# =========================================================================
# PORTAL PRINT MANDIRI (SELF-SERVICE PRINT QUEUE)
# =========================================================================

@pos_bp.route('/api/pos/kirim_print', methods=['POST'])
def api_pos_kirim_print():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'Tidak ada berkas yang diunggah!'}), 400
        
    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({'status': 'error', 'message': 'Silakan pilih berkas dokumen!'}), 400
        
    nama_pemesan = request.form.get('nama_pemesan', '').strip()
    no_rekening = request.form.get('no_rekening', '').strip() or None
    member_id = request.form.get('member_id', '').strip() or None
    telepon = request.form.get('telepon', '').strip()
    jenis_print = request.form.get('jenis_print', 'KERTAS_PERPUS')
    catatan = request.form.get('catatan', '').strip()
    
    # Opsi Cetak: Mode Sisi (1 Sisi / 2 Sisi Bolak-Balik), Warna, Kertas, Rangkap
    mode_sisi = request.form.get('mode_sisi', '1_SISI').strip().upper()
    if mode_sisi not in ['1_SISI', '2_SISI']:
        mode_sisi = '1_SISI'
        
    warna = request.form.get('warna', 'BW').strip().upper()
    if warna not in ['BW', 'WARNA']:
        warna = 'BW'
        
    kertas = request.form.get('kertas', 'PERPUS').strip().upper()
    if kertas not in ['PERPUS', 'SENDIRI']:
        if jenis_print == 'KERTAS_SENDIRI':
            kertas = 'SENDIRI'
        else:
            kertas = 'PERPUS'
            
    try:
        rangkap = max(1, int(request.form.get('rangkap', 1)))
    except:
        rangkap = 1
        
    if not nama_pemesan:
        return jsonify({'status': 'error', 'message': 'Nama pemesan wajib diisi!'}), 400
        
    raw_fname = secure_filename(file.filename)
    if not raw_fname.lower().endswith('.pdf'):
        return jsonify({
            'status': 'error',
            'message': 'Format berkas ditolak! Dokumen wajib berupa format PDF (.pdf). Berkas Word (.doc/.docx) tidak diizinkan agar tata letak cetak rapi.'
        }), 400
        
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    saved_fname = f"{timestamp}_{raw_fname}"
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'print_queue')
    os.makedirs(upload_dir, exist_ok=True)
    full_path = os.path.join(upload_dir, saved_fname)
    file.save(full_path)
    
    # Hitung halaman otomatis jika PDF dengan multi-tier fallback
    import math
    jml_halaman = 1
    if raw_fname.lower().endswith('.pdf'):
        # 1. Coba poppler pdfinfo (tercepat & paling akurat)
        try:
            import subprocess
            res = subprocess.run(['pdfinfo', full_path], capture_output=True, text=True, timeout=5)
            for line in res.stdout.splitlines():
                if line.startswith('Pages:'):
                    jml_halaman = int(line.split(':')[1].strip())
                    break
        except Exception:
            jml_halaman = 0

        # 2. Coba pypdf
        if jml_halaman <= 0:
            try:
                import pypdf
                with open(full_path, 'rb') as f:
                    reader = pypdf.PdfReader(f)
                    jml_halaman = len(reader.pages)
            except Exception:
                jml_halaman = 0

        # 3. Coba PyPDF2
        if jml_halaman <= 0:
            try:
                import PyPDF2
                with open(full_path, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    jml_halaman = len(reader.pages)
            except Exception:
                jml_halaman = 0

        # 4. Fallback scan regex
        if jml_halaman <= 0:
            try:
                import re
                with open(full_path, 'rb') as f:
                    content = f.read()
                    matches = re.findall(rb'/Type\s*/Page[^s]', content)
                    if matches:
                        jml_halaman = len(matches)
            except Exception:
                pass

        if jml_halaman <= 0:
            jml_halaman = 1
            
    # Hitung estimasi lembar fisik (bolak-balik = ceil(halaman/2))
    if mode_sisi == '2_SISI':
        lembar_per_dok = math.ceil(jml_halaman / 2)
    else:
        lembar_per_dok = jml_halaman
    jumlah_lembar = lembar_per_dok * rangkap
    
    sisi_label = "2 Sisi (Bolak-Balik)" if mode_sisi == '2_SISI' else "1 Sisi"
    warna_label = "Warna" if warna == 'WARNA' else "Hitam-Putih"
    kertas_label = "Kertas Sendiri" if kertas == 'SENDIRI' else "Kertas Perpus"
    jenis_print_formatted = f"{warna_label} | {sisi_label} | {kertas_label}"
    if rangkap > 1:
        jenis_print_formatted += f" ({rangkap}x)"
            
    conn = database.get_db_connection()
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    count_today = conn.execute("SELECT COUNT(*) FROM pos_antrean_print WHERE date(tanggal) = date('now', 'localtime')").fetchone()[0]
    kode_antrean = f"PRN-{(count_today + 1):02d}"
    
    now_dt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("""
        INSERT INTO pos_antrean_print (
            kode_antrean, tanggal, no_rekening, nama_pemesan, member_id,
            telepon, nama_file, file_path, jumlah_halaman, jenis_print, catatan, status,
            mode_sisi, warna, kertas, rangkap, jumlah_lembar
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'MENUNGGU', ?, ?, ?, ?, ?)
    """, (
        kode_antrean, now_dt, no_rekening, nama_pemesan, member_id,
        telepon, raw_fname, saved_fname, jml_halaman, jenis_print_formatted, catatan,
        mode_sisi, warna, kertas, rangkap, jumlah_lembar
    ))
    conn.commit()
    conn.close()
    
    return jsonify({
        'status': 'success',
        'message': 'Berkas berhasil dikirim ke antrean kasir perpustakaan!',
        'kode_antrean': kode_antrean,
        'nama_pemesan': nama_pemesan,
        'nama_file': raw_fname,
        'jumlah_halaman': jml_halaman,
        'jumlah_lembar': jumlah_lembar,
        'mode_sisi': mode_sisi,
        'warna': warna,
        'kertas': kertas,
        'rangkap': rangkap,
        'jenis_print': jenis_print_formatted
    })

@pos_bp.route('/api/pos/antrean_print', methods=['GET'])
@pos_api_login_required
def api_pos_antrean_print():
    conn = database.get_db_connection()
    rows = conn.execute("""
        SELECT * FROM pos_antrean_print 
        WHERE status = 'MENUNGGU' 
        ORDER BY id DESC
    """).fetchall()
    conn.close()
    return jsonify({'status': 'success', 'data': [dict(r) for r in rows]})

@pos_bp.route('/api/pos/antrean_print/status', methods=['POST'])
@pos_api_login_required
def api_pos_antrean_print_status():
    data = request.json or {}
    item_id = data.get('id')
    status = data.get('status', 'SELESAI')
    conn = database.get_db_connection()
    conn.execute("UPDATE pos_antrean_print SET status = ? WHERE id = ?", (status, item_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


# ==========================================
# MODUL LAPORAN KEUANGAN KAS BULANAN
# ==========================================

NAMA_BULAN = {
    '01': 'Januari', '02': 'Februari', '03': 'Maret', '04': 'April',
    '05': 'Mei', '06': 'Juni', '07': 'Juli', '08': 'Agustus',
    '09': 'September', '10': 'Oktober', '11': 'November', '12': 'Desember'
}

def terbilang_rupiah(n):
    satuan = ["", "Satu", "Dua", "Tiga", "Empat", "Lima", "Enam", "Tujuh", "Delapan", "Sembilan", "Sepuluh", "Sebelas"]
    try:
        n = int(round(float(n)))
    except:
        return "Nol Rupiah"
    if n < 0:
        return "Minus " + terbilang_rupiah(-n)
    if n == 0:
        return "Nol Rupiah"
    
    def bilang(x):
        if x < 12:
            return satuan[x]
        elif x < 20:
            return bilang(x - 10) + " Belas"
        elif x < 100:
            return bilang(x // 10) + " Puluh " + satuan[x % 10]
        elif x < 200:
            return "Seratus " + bilang(x - 100)
        elif x < 1000:
            return bilang(x // 100) + " Ratus " + bilang(x % 100)
        elif x < 2000:
            return "Seribu " + bilang(x - 1000)
        elif x < 1000000:
            return bilang(x // 1000) + " Ribu " + bilang(x % 1000)
        elif x < 1000000000:
            return bilang(x // 1000000) + " Juta " + bilang(x % 1000000)
        elif x < 1000000000000:
            return bilang(x // 1000000000) + " Miliar " + bilang(x % 1000000000)
        else:
            return str(x)
            
    import re
    hasil = bilang(n).strip()
    hasil = re.sub(r'\s+', ' ', hasil)
    return hasil + " Rupiah"

def format_tanggal_indonesia(tgl_str):
    try:
        tgl_only = str(tgl_str).split(' ')[0].strip()
        parts = tgl_only.split('-')
        y, m, d = parts[0], parts[1], parts[2]
        m_name = NAMA_BULAN.get(m, m)
        return f"{int(d)} {m_name} {y}"
    except:
        return str(tgl_str)

def format_periode_indonesia(periode_str):
    try:
        parts = periode_str.split('-')
        y, m = parts[0], parts[1]
        m_name = NAMA_BULAN.get(m, m).upper()
        return f"{m_name} {y}"
    except:
        return periode_str

@pos_bp.route('/pos/laporan_keuangan')
@pos_login_required
def pos_laporan_keuangan():
    return render_template('pos/laporan_keuangan.html')

@pos_bp.route('/pos/laporan_keuangan/cetak')
@pos_login_required
def pos_laporan_keuangan_cetak():
    # Mendukung cetak berdasarkan bulanan (bulan=YYYY-MM) maupun custom rentang tanggal
    # Parameter fleksibel: tgl_mulai / start_date / dari & tgl_selesai / end_date / sampai
    tgl_mulai = (request.args.get('tgl_mulai') or request.args.get('start_date') or request.args.get('dari') or '').strip()
    tgl_selesai = (request.args.get('tgl_selesai') or request.args.get('end_date') or request.args.get('sampai') or '').strip()
    bulan = request.args.get('bulan', '').strip()

    is_custom_range = bool(tgl_mulai and tgl_selesai)
    conn = database.get_db_connection()

    if is_custom_range:
        if tgl_mulai > tgl_selesai:
            tgl_mulai, tgl_selesai = tgl_selesai, tgl_mulai

        entries = conn.execute("""
            SELECT id, periode, strftime('%d-%m-%Y', tanggal) as tanggal_formatted, tanggal,
                   keterangan, jenis, kategori, nominal, urutan
            FROM pos_buku_kas
            WHERE DATE(tanggal) >= ? AND DATE(tanggal) <= ?
            ORDER BY tanggal ASC, urutan ASC, id ASC
        """, (tgl_mulai, tgl_selesai)).fetchall()

        rekap_raw = conn.execute("""
            SELECT 
                COALESCE(l.nama_layanan, d.nama_layanan) as nama_layanan,
                COALESCE(l.kategori, 'FOTOCOPY & PRINT') as kategori,
                COALESCE(l.satuan, 'Lembar') as satuan,
                SUM(d.qty) as total_qty,
                SUM(d.subtotal) as total_nominal
            FROM pos_transaksi_detail d
            JOIN pos_transaksi t ON d.transaksi_id = t.id
            LEFT JOIN pos_layanan l ON d.kode_layanan = l.kode
            WHERE DATE(t.tanggal) >= ? AND DATE(t.tanggal) <= ?
            GROUP BY COALESCE(l.nama_layanan, d.nama_layanan), COALESCE(l.kategori, 'FOTOCOPY & PRINT'), COALESCE(l.satuan, 'Lembar')
            ORDER BY total_nominal DESC
        """, (tgl_mulai, tgl_selesai)).fetchall()

        summary_raw = conn.execute("""
            SELECT 
                COUNT(*) as total_transaksi,
                COALESCE(SUM(total_tagihan), 0) as total_omzet,
                COALESCE(SUM(lembar_kertas_perpus), 0) as total_lembar
            FROM pos_transaksi
            WHERE DATE(tanggal) >= ? AND DATE(tanggal) <= ?
        """, (tgl_mulai, tgl_selesai)).fetchone()

        bulan_label = f"{format_tanggal_indonesia(tgl_mulai)} s.d. {format_tanggal_indonesia(tgl_selesai)}"
        tanggal_laporan = format_tanggal_indonesia(tgl_selesai)
    else:
        if not bulan:
            bulan = datetime.datetime.now().strftime('%Y-%m')

        entries = conn.execute("""
            SELECT id, periode, strftime('%d-%m-%Y', tanggal) as tanggal_formatted, tanggal,
                   keterangan, jenis, kategori, nominal, urutan
            FROM pos_buku_kas
            WHERE periode = ?
            ORDER BY tanggal ASC, urutan ASC, id ASC
        """, (bulan,)).fetchall()

        rekap_raw = conn.execute("""
            SELECT 
                COALESCE(l.nama_layanan, d.nama_layanan) as nama_layanan,
                COALESCE(l.kategori, 'FOTOCOPY & PRINT') as kategori,
                COALESCE(l.satuan, 'Lembar') as satuan,
                SUM(d.qty) as total_qty,
                SUM(d.subtotal) as total_nominal
            FROM pos_transaksi_detail d
            JOIN pos_transaksi t ON d.transaksi_id = t.id
            LEFT JOIN pos_layanan l ON d.kode_layanan = l.kode
            WHERE t.tanggal LIKE ?
            GROUP BY COALESCE(l.nama_layanan, d.nama_layanan), COALESCE(l.kategori, 'FOTOCOPY & PRINT'), COALESCE(l.satuan, 'Lembar')
            ORDER BY total_nominal DESC
        """, (f"{bulan}%",)).fetchall()

        summary_raw = conn.execute("""
            SELECT 
                COUNT(*) as total_transaksi,
                COALESCE(SUM(total_tagihan), 0) as total_omzet,
                COALESCE(SUM(lembar_kertas_perpus), 0) as total_lembar
            FROM pos_transaksi
            WHERE tanggal LIKE ?
        """, (f"{bulan}%",)).fetchone()

        import calendar
        y, m = int(bulan.split('-')[0]), int(bulan.split('-')[1])
        last_day = calendar.monthrange(y, m)[1]
        tanggal_laporan = f"{last_day} {NAMA_BULAN.get(f'{m:02d}', '')} {y}"
        bulan_label = f"{format_periode_indonesia(bulan)} (1 s.d. {tanggal_laporan})"

    running_saldo = 0
    rows = []
    total_debet = 0
    total_kredit = 0
    for r in entries:
        d = dict(r)
        if d['jenis'] == 'DEBET':
            running_saldo += d['nominal']
            total_debet += d['nominal']
            d['debet'] = d['nominal']
            d['kredit'] = 0
        else:
            running_saldo -= d['nominal']
            total_kredit += d['nominal']
            d['debet'] = 0
            d['kredit'] = d['nominal']
        d['saldo'] = running_saldo
        rows.append(d)
        
    sisa_saldo = running_saldo

    pos_summary = {
        'total_transaksi': summary_raw['total_transaksi'] if summary_raw else 0,
        'total_omzet': summary_raw['total_omzet'] if summary_raw else 0,
        'total_lembar': summary_raw['total_lembar'] if summary_raw else 0
    }

    pos_rekap = []
    total_omzet = pos_summary['total_omzet']
    for r in rekap_raw:
        d = dict(r)
        d['pct'] = round((d['total_nominal'] / total_omzet * 100), 1) if total_omzet > 0 else 0
        pos_rekap.append(d)

    terbilang_saldo = terbilang_rupiah(sisa_saldo)
    conn.close()
    
    return render_template('pos/cetak_laporan_keuangan.html',
                           bulan=bulan,
                           bulan_label=bulan_label,
                           tanggal_laporan=tanggal_laporan,
                           rows=rows,
                           total_debet=total_debet,
                           total_kredit=total_kredit,
                           sisa_saldo=sisa_saldo,
                           pos_summary=pos_summary,
                           pos_rekap=pos_rekap,
                           terbilang_saldo=terbilang_saldo,
                           is_custom_range=is_custom_range,
                           tgl_mulai=tgl_mulai,
                           tgl_selesai=tgl_selesai)

@pos_bp.route('/api/pos/laporan_keuangan', methods=['GET'])
@pos_api_login_required
def api_pos_laporan_keuangan():
    bulan = request.args.get('bulan', '').strip()
    conn = database.get_db_connection()
    
    months_raw = conn.execute("""
        SELECT DISTINCT periode FROM pos_buku_kas
        UNION
        SELECT DISTINCT strftime('%Y-%m', tanggal) FROM pos_transaksi
        WHERE tanggal IS NOT NULL
        ORDER BY 1 DESC
    """).fetchall()
    
    available_months = []
    for m in months_raw:
        m_str = m[0]
        if m_str and len(m_str) == 7:
            available_months.append({
                'kode': m_str,
                'label': format_periode_indonesia(m_str)
            })
            
    if not bulan and available_months:
        bulan = available_months[0]['kode']
    elif not bulan:
        bulan = datetime.datetime.now().strftime('%Y-%m')

    entries = conn.execute("""
        SELECT id, periode, strftime('%d-%m-%Y', tanggal) as tanggal_formatted, tanggal,
               keterangan, jenis, kategori, nominal, urutan
        FROM pos_buku_kas
        WHERE periode = ?
        ORDER BY tanggal ASC, urutan ASC, id ASC
    """, (bulan,)).fetchall()
    
    running_saldo = 0
    rows = []
    total_debet = 0
    total_kredit = 0
    for r in entries:
        d = dict(r)
        if d['jenis'] == 'DEBET':
            running_saldo += d['nominal']
            total_debet += d['nominal']
            d['debet'] = d['nominal']
            d['kredit'] = 0
        else:
            running_saldo -= d['nominal']
            total_kredit += d['nominal']
            d['debet'] = 0
            d['kredit'] = d['nominal']
        d['saldo'] = running_saldo
        rows.append(d)
        
    sisa_saldo = running_saldo

    pos_stat = conn.execute("""
        SELECT COUNT(*) as count, COALESCE(SUM(total_tagihan), 0) as total, COALESCE(SUM(lembar_kertas_perpus), 0) as lembar
        FROM pos_transaksi
        WHERE strftime('%Y-%m', tanggal) = ?
    """, (bulan,)).fetchone()

    conn.close()
    
    return jsonify({
        'status': 'success',
        'periode': bulan,
        'periode_label': format_periode_indonesia(bulan),
        'rows': rows,
        'summary': {
            'total_debet': total_debet,
            'total_kredit': total_kredit,
            'sisa_saldo': sisa_saldo,
            'pos_total': pos_stat['total'] if pos_stat else 0,
            'pos_count': pos_stat['count'] if pos_stat else 0,
            'pos_lembar': pos_stat['lembar'] if pos_stat else 0
        },
        'available_months': available_months
    })

@pos_bp.route('/api/pos/laporan_keuangan/simpan', methods=['POST'])
@pos_api_login_required
def api_pos_laporan_keuangan_simpan():
    data = request.json or {}
    tanggal = data.get('tanggal', '').strip()
    keterangan = data.get('keterangan', '').strip()
    jenis = data.get('jenis', 'DEBET').upper()
    kategori = data.get('kategori', 'OPERASIONAL').upper()
    try:
        nominal = float(data.get('nominal', 0))
    except:
        nominal = 0

    if not tanggal or not keterangan or nominal <= 0:
        return jsonify({'status': 'error', 'message': 'Tanggal, keterangan, dan nominal wajib diisi valid.'}), 400

    periode = tanggal[:7]
    conn = database.get_db_connection()
    conn.execute("""
        INSERT INTO pos_buku_kas (periode, tanggal, keterangan, jenis, kategori, nominal, urutan, created_by)
        VALUES (?, ?, ?, ?, ?, ?, 10, ?)
    """, (periode, tanggal, keterangan, jenis, kategori, nominal, session.get('username', 'yosef')))
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'message': 'Transaksi kas berhasil dicatat.'})

@pos_bp.route('/api/pos/laporan_keuangan/hapus/<int:item_id>', methods=['POST'])
@pos_api_login_required
def api_pos_laporan_keuangan_hapus(item_id):
    conn = database.get_db_connection()
    conn.execute("DELETE FROM pos_buku_kas WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': 'Data kas berhasil dihapus.'})

@pos_bp.route('/api/pos/laporan_keuangan/sinkron_pos', methods=['POST'])
@pos_api_login_required
def api_pos_laporan_keuangan_sinkron_pos():
    data = request.json or {}
    bulan = data.get('bulan', '').strip()
    if not bulan:
        return jsonify({'status': 'error', 'message': 'Bulan wajib dipilih.'}), 400
        
    conn = database.get_db_connection()
    pos_stat = conn.execute("""
        SELECT COALESCE(SUM(total_tagihan), 0) as total
        FROM pos_transaksi
        WHERE strftime('%Y-%m', tanggal) = ?
    """, (bulan,)).fetchone()
    
    total_pos = pos_stat['total'] if pos_stat else 0
    
    import calendar
    y, m = int(bulan.split('-')[0]), int(bulan.split('-')[1])
    last_day = calendar.monthrange(y, m)[1]
    tgl_akhir = f"{bulan}-{last_day:02d}"

    pos_entries = conn.execute("SELECT id, nominal FROM pos_buku_kas WHERE periode = ? AND keterangan LIKE '%Pendapatan Print%'", (bulan,)).fetchall()
    if len(pos_entries) > 1:
        current_sum = sum(e['nominal'] for e in pos_entries)
        if current_sum == total_pos:
            conn.close()
            return jsonify({'status': 'success', 'message': f'Data kas bulan {bulan} sudah sinkron (Total: Rp {total_pos:,.0f}) dengan rincian sub-periode.'})

    existing = conn.execute("SELECT id FROM pos_buku_kas WHERE periode = ? AND keterangan LIKE '%Pendapatan Print%'", (bulan,)).fetchone()
    if existing:
        conn.execute("UPDATE pos_buku_kas SET nominal = ?, tanggal = ? WHERE id = ?", (total_pos, tgl_akhir, existing['id']))
    else:
        conn.execute("""
            INSERT INTO pos_buku_kas (periode, tanggal, keterangan, jenis, kategori, nominal, urutan)
            VALUES (?, ?, 'Pendapatan Print & fotocopy', 'DEBET', 'FOTOCOPY', ?, 1)
        """, (bulan, tgl_akhir, total_pos))
        
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': f'Berhasil menyinkronkan pendapatan POS bulan {bulan}: Rp {total_pos:,.0f}'})
