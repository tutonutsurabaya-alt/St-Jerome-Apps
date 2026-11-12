"""Isolated production safety smoke & regression tests for St. Jerome Apps.

IMPORTANT: Always runs strictly against a temporary SQLite DB inside pytest's tmp_path.
Never touches live /root/st_jerome_web/katalog.db or sends real Telegram messages.
"""

import os
import sqlite3
import sys
import tempfile

import pytest


def _bootstrap_test_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            nama_lengkap TEXT NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'Administrator',
            email TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            hak_akses TEXT DEFAULT 'all',
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS anggota (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id TEXT UNIQUE,
            nama TEXT,
            tipe_anggota TEXT,
            institusi TEXT,
            email TEXT,
            telepon TEXT,
            alamat TEXT,
            masa_berlaku TEXT,
            status TEXT DEFAULT 'AKTIF',
            suspended_until TEXT,
            tanggal_input TEXT,
            is_tesis INTEGER DEFAULT 0,
            password TEXT
        );
        CREATE TABLE IF NOT EXISTS buku (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            no_induk TEXT UNIQUE NOT NULL,
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
            copy_ke INTEGER DEFAULT 1,
            catatan TEXT,
            pengarang TEXT,
            subjek TEXT,
            status_buku TEXT DEFAULT 'BELI',
            lokasi TEXT DEFAULT 'IMAVI',
            tgl_terima TEXT,
            gambar_sampul TEXT,
            is_reference_only INTEGER DEFAULT 0
        );
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
            image TEXT,
            file_dokumen TEXT
        );
        CREATE TABLE IF NOT EXISTS eksemplar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            biblio_id INTEGER NOT NULL,
            no_induk TEXT UNIQUE NOT NULL,
            status_buku TEXT DEFAULT 'BELI',
            lokasi TEXT DEFAULT 'IMAVI',
            tgl_terima TEXT,
            copy_ke INTEGER DEFAULT 1,
            catatan TEXT,
            status_ketersediaan TEXT DEFAULT 'Tersedia'
        );
        CREATE TABLE IF NOT EXISTS sirkulasi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id TEXT,
            no_induk TEXT,
            loan_date TEXT,
            due_date TEXT,
            return_date TEXT,
            renewal_count INTEGER DEFAULT 0,
            fine_amount INTEGER DEFAULT 0,
            fine_status TEXT DEFAULT 'BELUM_LUNAS'
        );
        CREATE TABLE IF NOT EXISTS peminjaman (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id TEXT,
            member_name TEXT,
            no_induk TEXT,
            judul TEXT,
            loan_date TEXT,
            due_date TEXT,
            loan_status TEXT
        );
        CREATE TABLE IF NOT EXISTS pengaturan_sistem (
            kunci TEXT PRIMARY KEY,
            nilai TEXT
        );
        CREATE TABLE IF NOT EXISTS usulan_buku (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            judul_topik TEXT NOT NULL,
            pengarang TEXT,
            catatan TEXT,
            tanggal DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'Menunggu',
            sumber TEXT DEFAULT 'Web OPAC'
        );
        CREATE TABLE IF NOT EXISTS sjla_visitor_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            waktu_kunjungan DATETIME DEFAULT CURRENT_TIMESTAMP,
            tipe_pengunjung TEXT,
            identitas TEXT,
            asal_instansi TEXT,
            tujuan_kunjungan TEXT,
            peran_jabatan TEXT,
            fakultas TEXT
        );
        CREATE TABLE IF NOT EXISTS pos_layanan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kode TEXT UNIQUE NOT NULL,
            nama_layanan TEXT NOT NULL,
            kategori TEXT NOT NULL,
            satuan TEXT DEFAULT 'Lembar',
            harga INTEGER NOT NULL,
            is_kertas_perpus INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            urutan INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS pos_transaksi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            no_transaksi TEXT UNIQUE NOT NULL,
            tanggal TEXT NOT NULL,
            no_rekening TEXT,
            nama_pelanggan TEXT DEFAULT 'UMUM',
            metode_bayar TEXT NOT NULL,
            total_tagihan INTEGER NOT NULL,
            potong_saldo INTEGER DEFAULT 0,
            bayar_tunai INTEGER DEFAULT 0,
            kembalian INTEGER DEFAULT 0,
            lembar_kertas_perpus INTEGER DEFAULT 0,
            kasir TEXT DEFAULT 'Kasir',
            catatan TEXT
        );
        CREATE TABLE IF NOT EXISTS pos_transaksi_detail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaksi_id INTEGER NOT NULL,
            kode_layanan TEXT NOT NULL,
            nama_layanan TEXT NOT NULL,
            harga_satuan INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            subtotal INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pos_buku_kas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            periode TEXT NOT NULL,
            tanggal DATE NOT NULL,
            keterangan TEXT NOT NULL,
            jenis TEXT NOT NULL,
            kategori TEXT DEFAULT 'FOTOCOPY',
            nominal REAL NOT NULL,
            urutan INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT DEFAULT 'test_admin'
        );
        """
    )
    conn.execute(
        "INSERT INTO users (username, nama_lengkap, password, role, hak_akses, is_active) "
        "VALUES ('test_admin', 'Test Admin', 'unused', 'Master Admin', 'all', 1)"
    )
    conn.execute(
        "INSERT INTO buku (no_induk, judul, pengarang, penerbit, tahun_terbit, klasifikasi, lokasi) "
        "VALUES ('TEST001', 'Teologi Sistematika Pengantar', 'Tester', 'StJerome', '2026', '230', 'IMAVI')"
    )
    conn.execute(
        "INSERT INTO bibliografi (id, judul, pengarang, penerbit, tahun_terbit, klasifikasi) "
        "VALUES (1, 'Teologi Sistematika Pengantar', 'Tester', 'StJerome', '2026', '230')"
    )
    conn.execute(
        "INSERT INTO eksemplar (biblio_id, no_induk, lokasi, status_ketersediaan) "
        "VALUES (1, 'TEST001', 'IMAVI', 'Tersedia')"
    )
    # Seed two POS cash entries across months for range-filter regression test
    conn.execute(
        "INSERT INTO pos_buku_kas (periode, tanggal, keterangan, jenis, kategori, nominal, urutan) "
        "VALUES ('2026-08', '2026-08-25', 'Kas Fotocopy Agustus', 'DEBET', 'FOTOCOPY', 15000, 1)"
    )
    conn.execute(
        "INSERT INTO pos_buku_kas (periode, tanggal, keterangan, jenis, kategori, nominal, urutan) "
        "VALUES ('2026-09', '2026-09-05', 'Kas Fotocopy September', 'DEBET', 'FOTOCOPY', 99000, 1)"
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope='module')
def isolated_env(tmp_path_factory):
    root_tmp = tmp_path_factory.mktemp('stjerome_smoke')
    db_file = root_tmp / 'isolated_katalog.db'
    backup_dir = root_tmp / 'backups'
    alert_dir = root_tmp / 'alerts'

    _bootstrap_test_schema(str(db_file))

    os.environ['DATABASE_PATH'] = str(db_file)
    os.environ['STJEROME_BACKUP_DIR'] = str(backup_dir)
    os.environ['STJEROME_ALERT_COOLDOWN_DIR'] = str(alert_dir)
    os.environ['STJEROME_DISABLE_ALERTS'] = '1'

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    import database
    assert os.path.abspath(database.DB_NAME) == os.path.abspath(str(db_file))

    import app as st_app
    st_app.app.config['TESTING'] = True

    yield {
        'app_module': st_app,
        'app': st_app.app,
        'db_file': db_file,
        'backup_dir': backup_dir,
        'root_tmp': root_tmp,
    }


def _login_admin(client):
    with client.session_transaction() as sess:
        sess['logged_in'] = True
        sess['username'] = 'test_admin'
        sess['nama_lengkap'] = 'Test Admin'
        sess['role'] = 'Master Admin'
        sess['hak_akses'] = 'all'


def test_health_and_public_routes(isolated_env):
    client = isolated_env['app'].test_client()

    r_health = client.get('/health')
    assert r_health.status_code == 200
    payload = r_health.get_json()
    assert payload['status'] == 'ok'
    assert payload['db'] == 'ok'
    assert payload['tables'] >= 10

    assert client.get('/').status_code in (200, 302)
    assert client.get('/login').status_code == 200
    assert client.get('/opac').status_code == 200

    r_search = client.get('/api/opac/search?q=Teologi')
    assert r_search.status_code == 200
    assert 'Teologi Sistematika Pengantar' in r_search.get_data(as_text=True)

    assert client.get('/api/opac/koleksi_unggulan').status_code == 200


def test_unauthenticated_debug_and_dangerous_routes_blocked(isolated_env):
    client = isolated_env['app'].test_client()

    for endpoint in (
        '/api/debug/db_path',
        '/api/debug/tables',
        '/api/debug/force_migrate',
        '/api/debug/koleksi',
        '/api/debug/files',
        '/debug_schema',
        '/api/wa_groups',
        '/api/ai/models',
    ):
        resp = client.get(endpoint)
        assert resp.status_code in (401, 403), f"{endpoint} unexpectedly returned {resp.status_code}"

    # Verify eksemplar wasn't wiped
    conn = sqlite3.connect(str(isolated_env['db_file']))
    cnt = conn.execute("SELECT COUNT(*) FROM eksemplar").fetchone()[0]
    conn.close()
    assert cnt == 1

    # Verify Cloudflare-forwarded requests cannot trigger internal cron endpoint
    r_cron = client.post(
        '/api/internal/cron_reminder',
        headers={'CF-Connecting-IP': '1.2.3.4'},
    )
    assert r_cron.status_code == 403

    # Logged-in admin calling force_migrate without explicit confirmation must return 400
    _login_admin(client)
    r_force = client.get('/api/debug/force_migrate', base_url='https://localhost')
    assert r_force.status_code == 400


def test_wal_safe_backup_download_and_local_rotation(isolated_env):
    client = isolated_env['app'].test_client()
    _login_admin(client)

    resp = client.get('/backup', base_url='https://localhost')
    assert resp.status_code == 200
    data = resp.data
    assert data.startswith(b'SQLite format 3\x00')

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tf:
        tf.write(data)
        tmp_dl = tf.name
    try:
        from ops.sqlite_utils import inspect_snapshot_db
        counts = inspect_snapshot_db(tmp_dl)
        assert counts['buku'] == 1
        assert counts['eksemplar'] == 1
    finally:
        os.remove(tmp_dl)

    from ops.db_backup import get_latest_backup_info, run_backup
    res = run_backup(
        mode='hourly',
        verify_restore=True,
        backup_root=str(isolated_env['backup_dir']),
        db_path=str(isolated_env['db_file']),
    )
    assert res['status'] == 'ok'
    assert os.path.exists(res['path'])
    assert os.path.exists(res['path'] + '.sha256')
    info = get_latest_backup_info(backup_root=str(isolated_env['backup_dir']))
    assert info is not None
    assert info['path'] == res['path']


def test_pos_cash_report_date_range_regression(isolated_env):
    client = isolated_env['app'].test_client()
    _login_admin(client)

    resp = client.get(
        '/pos/laporan_keuangan/cetak?tgl_mulai=2026-08-20&tgl_selesai=2026-08-31',
        base_url='https://localhost',
    )
    assert resp.status_code == 200
    html_text = resp.get_data(as_text=True)
    assert 'Kas Fotocopy Agustus' in html_text
    assert 'Kas Fotocopy September' not in html_text


def test_health_monitor_state_transitions(isolated_env, monkeypatch):
    from ops import health_monitor

    state_file = isolated_env['root_tmp'] / 'monitor_state.json'
    monkeypatch.setattr(health_monitor, 'check_web_health', lambda: (False, 'Simulated outage'))
    monkeypatch.setattr(health_monitor, 'check_wa_bot', lambda: (True, 'ready'))
    monkeypatch.setattr(health_monitor, 'check_disk', lambda: (True, '50% used'))
    monkeypatch.setattr(health_monitor, 'check_backup_freshness', lambda: (True, '0.1h old'))

    sent = []
    monkeypatch.setattr(
        health_monitor,
        'send_alert',
        lambda title, msg, **kw: sent.append((kw.get('level'), title)),
    )

    health_monitor.run_checks(notify=True, state_path=str(state_file))
    assert len(sent) == 1
    assert 'web_app' in sent[0][1]

    # Repeated failure within cooldown does not duplicate alert
    health_monitor.run_checks(notify=True, state_path=str(state_file))
    assert len(sent) == 1

    # Recovery sends info notification once
    monkeypatch.setattr(health_monitor, 'check_web_health', lambda: (True, 'HTTP 200'))
    checks = health_monitor.run_checks(notify=True, state_path=str(state_file))
    assert 'wa_bot' not in checks
    assert len(sent) == 2
    assert 'Recovered: web_app' in sent[1][1]


def test_wa_bot_disabled_and_no_port_3005_calls(isolated_env):
    from core.features import is_enabled
    assert is_enabled('ENABLE_WA_BOT') is False

    client = isolated_env['app'].test_client()
    _login_admin(client)

    for api_ep in ('/api/wa_status', '/api/wa_groups'):
        r = client.get(api_ep, base_url='https://localhost')
        assert r.status_code == 200
        assert r.get_json()['status'] == 'disabled'

    for post_ep in ('/api/wa_broadcast', '/api/sirkulasi/kirim_wa_komunitas', '/api/trigger_wa_reminder'):
        r = client.post(post_ep, json={'number': '628111', 'message': 'hi'}, base_url='https://localhost')
        assert r.status_code == 200
        assert r.get_json()['status'] == 'disabled'

    r_page = client.get('/wa_broadcast', base_url='https://localhost')
    assert r_page.status_code == 302

    r_dash = client.get('/settings', base_url='https://localhost')
    assert r_dash.status_code == 200
    html = r_dash.get_data(as_text=True)
    assert 'href="/wa_broadcast"' not in html
    assert 'Perpustakaan IMAVI' in html
    assert 'href="/pos"' in html


def test_feature_flags_and_branding_modes(isolated_env, monkeypatch):
    from core.features import get_feature_config, is_enabled

    client = isolated_env['app'].test_client()
    _login_admin(client)

    # Partial legacy settings POST must preserve existing flags
    r_leg = client.post(
        '/settings',
        data={'telegram_token': 'dummy-tok', 'telegram_chat_id': '12345', 'gemini_api_key': ''},
        base_url='https://localhost',
    )
    assert r_leg.status_code == 302
    assert is_enabled('ENABLE_POS_FOTOCOPY') is True

    # Offline portable switch updates branding & gates POS/AI/AutoCover/Sync
    r_feat = client.post(
        '/settings',
        data={
            'form_section': 'features',
            'save_feature_flags': '1',
            'library_name': 'Perpustakaan STPD',
            'app_mode': 'offline_portable',
        },
        base_url='https://localhost',
    )
    assert r_feat.status_code == 302

    r_settings = client.get('/settings', base_url='https://localhost')
    assert r_settings.status_code == 200
    assert 'Perpustakaan STPD' in r_settings.get_data(as_text=True)

    with isolated_env['app'].app_context():
        cfg = get_feature_config()
        assert cfg['APP_MODE'] == 'offline_portable'
        assert cfg['LIBRARY_NAME'] == 'Perpustakaan STPD'
        assert cfg['DEFAULT_LOKASI'] == 'STPD'
        assert cfg['ENABLE_POS_FOTOCOPY'] is False
        assert cfg['ENABLE_AI_ASSISTANT'] is False
        assert cfg['ENABLE_AUTOCOVER_ONLINE'] is False
        assert cfg['ENABLE_SYNC_JADWAL_PROXY'] is False
        assert cfg['ENABLE_WA_BOT'] is False

    assert client.get('/pos', base_url='https://localhost').status_code == 302
    assert client.get('/api/ai/models', base_url='https://localhost').status_code == 503
    assert client.post('/api/robot/start', json={'limit': 1}, base_url='https://localhost').status_code == 503

    # Env override takes precedence over DB config
    monkeypatch.setenv('STJEROME_LIBRARY_NAME', 'Perpustakaan Override')
    with isolated_env['app'].test_request_context('/'):
        assert get_feature_config()['LIBRARY_NAME'] == 'Perpustakaan Override'

    # Cleanup test overrides from DB so module-scoped fixture remains clean
    conn = sqlite3.connect(str(isolated_env['db_file']))
    conn.execute("DELETE FROM pengaturan_sistem WHERE kunci IN ('APP_MODE', 'LIBRARY_NAME', 'DEFAULT_LOKASI', 'ENABLE_POS_FOTOCOPY', 'ENABLE_AI_ASSISTANT', 'ENABLE_AUTOCOVER_ONLINE', 'ENABLE_SYNC_JADWAL_PROXY', 'ENABLE_TELEGRAM_OPS', 'ENABLE_WA_BOT')")
    conn.commit()
    conn.close()


def test_modular_template_endpoints_parity(isolated_env):
    import re
    from pathlib import Path

    project_root = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    tpl_root = project_root / 'templates'
    view_funcs = isolated_env['app'].view_functions
    pattern = re.compile(r"url_for\(\s*['\"]([a-zA-Z0-9_\.]+)['\"]")

    missing = []
    for html_path in tpl_root.rglob('*.html'):
        if '.bak' in html_path.name:
            continue
        text = html_path.read_text(encoding='utf-8', errors='ignore')
        for ep in pattern.findall(text):
            if ep == 'static':
                continue
            if ep not in view_funcs:
                missing.append((str(html_path.relative_to(project_root)), ep))

    assert not missing, f"Missing endpoints referenced in templates: {missing}"


def test_offline_portable_mode_cookies_cdn_and_turnstile_bypass(isolated_env, monkeypatch, tmp_path):
    import database

    offline_db = tmp_path / 'offline_portal.db'
    _bootstrap_test_schema(str(offline_db))

    conn = sqlite3.connect(str(offline_db))
    conn.executemany(
        "INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES (?, ?)",
        [
            ('CF_TURNSTILE_SITE_KEY', '1x00000000000000000000AA'),
            ('CF_TURNSTILE_SECRET_KEY', '1x0000000000000000000000000000000AA'),
            ('APP_MODE', 'offline_portable'),
            ('LIBRARY_NAME', 'Perpustakaan STPD'),
            ('DEFAULT_LOKASI', 'STPD'),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, 'DB_NAME', str(offline_db))
    monkeypatch.setenv('DATABASE_PATH', str(offline_db))
    monkeypatch.setenv('STJEROME_APP_MODE', 'offline_portable')
    monkeypatch.setenv('STJEROME_LIBRARY_NAME', 'Perpustakaan STPD')
    monkeypatch.setenv('STJEROME_DEFAULT_LOKASI', 'STPD')

    offline_app = isolated_env['app_module'].create_app()
    offline_app.config['TESTING'] = True
    assert offline_app.config['SESSION_COOKIE_SECURE'] is False

    client = offline_app.test_client()

    # Login page should strip Turnstile & CDNs in offline_portable mode
    login_page = client.get('/login')
    assert login_page.status_code == 200
    login_html = login_page.get_data(as_text=True)
    assert 'challenges.cloudflare.com' not in login_html
    assert 'https://cdn.tailwindcss.com' not in login_html
    assert '/static/vendor/tailwindcss.js' in login_html
    assert 'fonts.googleapis.com' not in login_html

    # Login succeeds without Turnstile response and session cookie persists over HTTP
    resp = client.post(
        '/login',
        data={'username': 'yosef', 'password': 'bcajember'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    home = client.get('/')
    assert home.status_code == 200
    home_html = home.get_data(as_text=True)
    assert 'Perpustakaan STPD' in home_html
    assert '/static/vendor/fontawesome/css/all.min.css' in home_html
    assert '/static/vendor/alpine.min.js' in home_html
    assert 'https://cdnjs.cloudflare.com' not in home_html
    assert 'https://cdn.jsdelivr.net' not in home_html

    opac_page = client.get('/opac')
    assert opac_page.status_code == 200
    opac_html = opac_page.get_data(as_text=True)
    assert 'Perpustakaan STPD' in opac_html
    assert 'googleapis.com/books/v1/volumes' not in opac_html


def test_stpd_dynamic_branding_opac_and_custom_barcode_stock_opname(isolated_env, monkeypatch, tmp_path):
    import database

    stpd_db = tmp_path / 'stpd_flow.db'
    _bootstrap_test_schema(str(stpd_db))

    conn = sqlite3.connect(str(stpd_db))
    conn.executemany(
        "INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES (?, ?)",
        [
            ('APP_MODE', 'offline_portable'),
            ('LIBRARY_NAME', 'Perpustakaan STPD'),
            ('DEFAULT_LOKASI', 'STPD'),
            ('ENABLE_AUTOCOVER_ONLINE', '0'),
        ],
    )
    conn.execute(
        "INSERT INTO bibliografi (id, judul, pengarang, isbn, klasifikasi, image) VALUES (901, 'Teologi Sistematika STPD', 'Romo Pengajar', '9781234567890', '230', NULL)"
    )
    conn.execute(
        "INSERT INTO eksemplar (biblio_id, no_induk, status_buku, lokasi, status_ketersediaan) VALUES (901, '0003/21', 'BELI', 'STPD', 'Tersedia')"
    )
    conn.execute(
        "INSERT INTO buku (no_induk, judul, pengarang, klasifikasi, lokasi, status_buku) VALUES ('0003/21', 'Teologi Sistematika STPD', 'Romo Pengajar', '230', 'STPD', 'BELI')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, 'DB_NAME', str(stpd_db))
    monkeypatch.setenv('DATABASE_PATH', str(stpd_db))
    monkeypatch.setenv('STJEROME_APP_MODE', 'offline_portable')
    monkeypatch.setenv('STJEROME_LIBRARY_NAME', 'Perpustakaan STPD')
    monkeypatch.setenv('STJEROME_DEFAULT_LOKASI', 'STPD')

    stpd_app = isolated_env['app_module'].create_app()
    stpd_app.config['TESTING'] = True
    client = stpd_app.test_client()
    _login_admin(client)

    # OPAC respects STPD default lokasi & omits external Google Books URLs offline
    info = client.get('/api/opac/library_info').get_json()
    assert info['library_name'] == 'Perpustakaan STPD'

    search_items = client.get('/api/opac/search?q=Teologi%20Sistematika%20STPD').get_json()
    assert len(search_items) == 1
    assert search_items[0]['judul'] == 'Teologi Sistematika STPD'
    assert search_items[0]['cover_url'] is None

    # Default location redirects use STPD, not hardcoded IMAVI
    koleksi_redir = client.get('/koleksi', follow_redirects=False)
    assert koleksi_redir.status_code == 302
    assert 'lokasi=STPD' in koleksi_redir.headers['Location']

    # Stock Opname flow: Start -> Scan ALL known -> Scan unknown (BELUM TERCATAT) -> CSV Export
    start_resp = client.post('/audit_rak', data={'action': 'start', 'nama_sesi': 'Opname STPD Test'})
    assert start_resp.status_code == 200

    scanner_resp = client.get('/audit_rak/scanner')
    assert scanner_resp.status_code == 200
    scanner_html = scanner_resp.get_data(as_text=True)
    assert 'value="ALL"' in scanner_html
    assert 'html5-qr.min.js"></script>' in scanner_html

    conn = sqlite3.connect(str(stpd_db))
    sess_id = conn.execute("SELECT id FROM stock_opname_session ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.close()

    known_scan = client.post(
        '/api/scan_opname',
        json={'no_induk': '0003/21', 'session_id': sess_id, 'target_rak': 'ALL'},
    ).get_json()
    assert known_scan['status'] == 'success'
    assert known_scan['status_rak'] == 'BENAR'

    unknown_scan = client.post(
        '/api/scan_opname',
        json={'no_induk': '9999/18', 'session_id': sess_id, 'target_rak': 'ALL'},
    ).get_json()
    assert unknown_scan['status'] == 'unregistered'
    assert unknown_scan['unregistered'] is True
    assert 'no_induk=9999%2F18' in unknown_scan['input_url']

    report_resp = client.get(f'/laporan_opname/{sess_id}')
    assert report_resp.status_code == 200
    assert '9999/18' in report_resp.get_data(as_text=True)

    csv_resp = client.get(f'/laporan_opname/{sess_id}/export_csv')
    assert csv_resp.status_code == 200
    csv_text = csv_resp.get_data(as_text=True)
    assert 'BELUM TERCATAT,9999/18' in csv_text

    # Registering custom physical barcode preserves exact barcode, sets lokasi=STPD, and resolves BELUM TERCATAT
    batch_resp = client.post(
        '/api/buku/input_batch',
        json={
            'judul': 'Buku Fisik Lama STPD',
            'pengarang': 'Penulis STPD',
            'klasifikasi': '240',
            'jumlah': 1,
            'custom_barcode': '9999/18',
        },
    )
    assert batch_resp.status_code == 200
    assert batch_resp.get_json()['barcodes'] == ['9999/18']

    dup_resp = client.post(
        '/api/buku/input_batch',
        json={'judul': 'Duplikat', 'jumlah': 1, 'custom_barcode': '9999/18'},
    )
    assert dup_resp.status_code == 400

    conn = sqlite3.connect(str(stpd_db))
    eks_loc = conn.execute("SELECT lokasi FROM eksemplar WHERE no_induk = '9999/18'").fetchone()
    scan_status = conn.execute(
        "SELECT status FROM stock_opname_scan WHERE session_id = ? AND no_induk = '9999/18'",
        (sess_id,),
    ).fetchone()
    conn.close()
    assert eks_loc is not None and eks_loc[0] == 'STPD'
    assert scan_status is not None and scan_status[0] == 'DITEMUKAN'


