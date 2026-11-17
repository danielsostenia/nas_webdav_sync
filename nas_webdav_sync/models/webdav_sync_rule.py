# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.safe_eval import datetime, dateutil, safe_eval, time


class WebdavSyncRule(models.Model):
    _name = "webdav.sync.rule"
    _description = "NAS Sync Rule"
    _order = "sequence, id"

    backend_id = fields.Many2one("webdav.backend", required=True, ondelete="cascade")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    model_id = fields.Many2one(
        "ir.model",
        string="Document type",
        required=True,
        ondelete="cascade",
        domain=[("transient", "=", False)],
    )
    model_name = fields.Char(related="model_id.model", store=True)
    folder_name = fields.Char(
        help="Folder on the NAS for this document type. Defaults to the model name."
    )
    record_subfolder = fields.Boolean(
        "One folder per record",
        default=True,
        help="Example: Odoo/Invoices/INV-2026-0001 (42)/scan.pdf",
    )
    domain = fields.Char(
        "Only records matching",
        default="[]",
        help="Optional filter on the records whose attachments are synced.",
    )
    sync_from = fields.Datetime(
        "Only files created after",
        help="Leave empty to also upload existing attachments.",
    )

    @api.onchange("model_id")
    def _onchange_model_id(self):
        if self.model_id and not self.folder_name:
            self.folder_name = self.model_id.name

    def _domain_eval_context(self):
        """Same names the domain editor and server actions offer."""
        user = self.env.user.with_context({})
        return {
            "uid": self.env.uid,
            "user": user,
            "company_id": self.env.company.id,
            "company_ids": self.env.companies.ids,
            "time": time,
            "datetime": datetime,
            "dateutil": dateutil,
            "relativedelta": dateutil.relativedelta.relativedelta,
            "context_today": lambda: fields.Date.context_today(self),
        }

    def _get_record_domain(self):
        self.ensure_one()
        return safe_eval(self.domain or "[]", self._domain_eval_context())

    @api.constrains("domain", "model_id")
    def _check_domain(self):
        for rule in self:
            try:
                domain = rule._get_record_domain()
                rule.env[rule.model_name].sudo()._search(domain)
            except Exception as e:
                raise ValidationError(
                    _("Invalid filter for %(model)s:\n%(error)s",
                      model=rule.model_id.name, error=e)
                ) from e
