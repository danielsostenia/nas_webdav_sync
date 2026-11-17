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
        # Read everything now (the records are gone after the commit), but
        # delete on the NAS only once the deletion is committed in Odoo: a
        # rolled back transaction must never lose the remote copy.
        jobs = []
        for backend in items.backend_id:
            try:
                client = backend._get_client()
            except WebDAVError as e:
                _logger.warning("NAS sync: could not delete remote copy: %s", e)
                continue
            files = [
                (i.attachment_id.id, i.remote_path)
                for i in items.filtered(lambda i: i.backend_id == backend)
                if i.remote_path
            ]
            jobs.append((backend.name, client, files))
        if jobs:
            registry = self.env.registry
            self.env.cr.postcommit.add(lambda: _delete_remote_copies(registry, jobs))
        return super().unlink()


def _delete_remote_copies(registry, jobs):
    att_ids = [att_id for _name, _client, files in jobs for att_id, _path in files]
    # An unlink undone by a savepoint still reaches the post-commit hooks:
    # only delete copies of attachments that are really gone.
    with registry.cursor() as cr:
        cr.execute("SELECT id FROM ir_attachment WHERE id = ANY(%s)", [att_ids])
        still_there = {row[0] for row in cr.fetchall()}
    for name, client, files in jobs:
        for att_id, path in files:
            if att_id in still_there:
                continue
            try:
                client.delete(path)
            except WebDAVError as e:
                # Best effort: deleting a file in Odoo never depends on the NAS
                _logger.warning("NAS sync %s: could not delete remote copy: %s", name, e)
