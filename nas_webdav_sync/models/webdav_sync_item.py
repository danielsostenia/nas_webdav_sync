# -*- coding: utf-8 -*-
from odoo import fields, models


class WebdavSyncItem(models.Model):
    _name = "webdav.sync.item"
    _description = "NAS Synced File"
    _order = "sync_date desc, id desc"
    _rec_name = "remote_path"

    _backend_attachment_uniq = models.Constraint(
        "UNIQUE(backend_id, attachment_id)",
        "Each file is tracked only once per server.",
    )

    backend_id = fields.Many2one("webdav.backend", required=True, ondelete="cascade", index=True)
    rule_id = fields.Many2one("webdav.sync.rule", ondelete="set null")
    attachment_id = fields.Many2one("ir.attachment", ondelete="cascade", index=True)
    attachment_name = fields.Char(related="attachment_id.name")
    res_model = fields.Char(related="attachment_id.res_model")
    res_id = fields.Many2oneReference(related="attachment_id.res_id", model_field="res_model")
    remote_path = fields.Char(readonly=True)
    checksum = fields.Char(readonly=True)
    state = fields.Selection(
        [("pending", "Pending"), ("done", "Synced"), ("error", "Error")],
        default="pending",
        required=True,
        index=True,
    )
    attempts = fields.Integer(default=0)
    error_message = fields.Text(readonly=True)
    sync_date = fields.Datetime(readonly=True)

    def action_open_record(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self.res_model,
            "res_id": self.res_id,
            "view_mode": "form",
        }
