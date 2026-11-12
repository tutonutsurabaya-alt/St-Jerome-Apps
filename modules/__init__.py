"""Modular domain route registration for St. Jerome Library Assistant."""

from modules.auth_admin import router as auth_admin_router
from modules.opac import router as opac_router
from modules.katalog import router as katalog_router
from modules.sirkulasi import router as sirkulasi_router
from modules.anggota_laporan import router as anggota_laporan_router
from modules.wa_legacy import router as wa_legacy_router

ROUTERS = (
    auth_admin_router,
    opac_router,
    katalog_router,
    sirkulasi_router,
    anggota_laporan_router,
    wa_legacy_router,
)


def register_all_modules(app):
    """Register all domain routers cleanly on ``app``."""
    for router in ROUTERS:
        router.register(app)
