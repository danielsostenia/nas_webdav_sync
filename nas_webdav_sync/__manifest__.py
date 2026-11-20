# -*- coding: utf-8 -*-
{
    "name": "NAS & Nextcloud Sync (WebDAV) | Synology, QNAP, TrueNAS",
    "summary": "Automatically copy Odoo attachments to Synology, QNAP, TrueNAS, "
               "Nextcloud or any WebDAV server, in clean per-record folders.",
    "description": """
Sync Odoo attachments to your NAS or Nextcloud over WebDAV.
One module, no heavy dependencies, set up in five minutes.
""",
    "version": "19.0.1.2.1",
    "category": "Productivity/Documents",
    "author": "Banica Daniel",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "security/webdav_security.xml",
        "data/ir_cron.xml",
        "views/webdav_backend_views.xml",
        "views/webdav_sync_rule_views.xml",
        "views/webdav_sync_item_views.xml",
        "views/menu.xml",
    ],
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": True,
}
