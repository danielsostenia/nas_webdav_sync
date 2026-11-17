# -*- coding: utf-8 -*-
import logging

from odoo import fields, models

from .webdav_client import WebDAVError

_logger = logging.getLogger(__name__)

# Writing any of these means the file content changed
CONTENT_FIELDS = ("datas", "raw", "db_datas")


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    webdav_item_ids = fields.One2many(
        "webdav.sync.item", "attachment_id", groups="base.group_system"
    )

    def write(self, vals):
        res = super().write(vals)
        if any(f in vals for f in CONTENT_FIELDS):
            # Upload the new content again (it overwrites the existing remote copy)
            self.env["webdav.sync.item"].sudo().search([
                ("attachment_id", "in", self.ids),
                ("state", "!=", "pending"),
            ]).write({"state": "pending", "attempts": 0, "error_message": False})
        return res

    def unlink(self):
        items = self.env["webdav.sync.item"].sudo().search([
            ("attachment_id", "in", self.ids),
            ("state", "=", "done"),
            ("backend_id.delete_remote", "=", True),
        ])
        for backend in items.backend_id:
            try:
                client = backend._get_client()
                for item in items.filtered(lambda i: i.backend_id == backend):
                    client.delete(item.remote_path)
            except WebDAVError as e:
                # Best effort: never block deleting a file in Odoo
                _logger.warning("NAS sync: could not delete remote copy: %s", e)
        return super().unlink()
