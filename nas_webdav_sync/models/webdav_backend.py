# -*- coding: utf-8 -*-
import logging
import mimetypes

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .webdav_client import WebDAVClient, WebDAVError, safe_name

_logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


class WebdavBackend(models.Model):
    _name = "webdav.backend"
    _description = "NAS / WebDAV Server"
    _inherit = ["mail.thread"]
    _order = "sequence, id"

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, tracking=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    server_type = fields.Selection(
        [
            ("synology", "Synology"),
            ("qnap", "QNAP"),
            ("truenas", "TrueNAS"),
            ("nextcloud", "Nextcloud / ownCloud"),
            ("other", "Other WebDAV server"),
        ],
        default="synology",
        required=True,
    )
    url = fields.Char(
        "WebDAV URL",
        required=True,
        tracking=True,
        help="Examples:\n"
             "Synology: https://nas.example.com:5006\n"
             "QNAP: https://nas.example.com/webdav\n"
             "Nextcloud: https://cloud.example.com/remote.php/dav/files/USERNAME",
    )
    username = fields.Char(tracking=True)
    password = fields.Char(groups="base.group_system")
    root_folder = fields.Char(
        default="Odoo",
        help="Folder on the server where Odoo files are stored. Created if missing.",
    )
    verify_ssl = fields.Boolean(
        "Verify SSL certificate",
        default=True,
        help="Disable only for NAS devices with a self-signed certificate on a trusted network.",
    )
    timeout = fields.Integer("Timeout (seconds)", default=60)
    batch_size = fields.Integer(
        default=200, help="Maximum number of files uploaded per scheduled run."
    )
    delete_remote = fields.Boolean(
        "Delete on NAS when deleted in Odoo",
        default=False,
        help="When an attachment is deleted in Odoo, delete its copy on the server too.",
    )

    rule_ids = fields.One2many("webdav.sync.rule", "backend_id", string="Sync rules", copy=True)
    item_ids = fields.One2many("webdav.sync.item", "backend_id", string="Synced files")
    synced_count = fields.Integer(compute="_compute_counts")
    error_count = fields.Integer(compute="_compute_counts")
    last_sync = fields.Datetime(readonly=True)
    last_sync_message = fields.Char(readonly=True)

    @api.depends("item_ids.state")
    def _compute_counts(self):
        data = {
            (backend.id, state): count
            for backend, state, count in self.env["webdav.sync.item"]._read_group(
                [("backend_id", "in", self.ids)], ["backend_id", "state"], ["__count"]
            )
        }
        for rec in self:
            rec.synced_count = data.get((rec.id, "done"), 0)
            rec.error_count = data.get((rec.id, "error"), 0)

    @api.onchange("server_type")
    def _onchange_server_type(self):
        placeholders = {
            "synology": "https://nas.example.com:5006",
            "qnap": "https://nas.example.com/webdav",
            "truenas": "https://truenas.example.com/share",
            "nextcloud": "https://cloud.example.com/remote.php/dav/files/USERNAME",
        }
        if not self.url and self.server_type in placeholders:
            self.url = placeholders[self.server_type]

    # ----------------------------------------------------------------- client
    def _get_client(self):
        self.ensure_one()
        return WebDAVClient(
            self.sudo().url,
            self.sudo().username,
            self.sudo().password,
            verify=self.verify_ssl,
            timeout=self.timeout or 60,
        )

    def _root(self):
        return "/".join(
            safe_name(p) for p in (self.root_folder or "").split("/") if p.strip()
        )

    # ---------------------------------------------------------------- buttons
    def action_test_connection(self):
        self.ensure_one()
        try:
            self._get_client().check(self._root())
        except WebDAVError as e:
            raise UserError(_("Connection failed:\n%s", e)) from e
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Connection successful"),
                "message": _("Odoo can write to %s", self.url),
                "sticky": False,
            },
        }

    def action_sync_now(self):
        for backend in self:
            backend._sync()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "title": _("Sync finished"),
                "message": "\n".join(b.last_sync_message or "" for b in self),
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def action_view_items(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "nas_webdav_sync.action_webdav_sync_item"
        )
        action["domain"] = [("backend_id", "=", self.id)]
        action["context"] = {"default_backend_id": self.id}
        return action

    def action_retry_errors(self):
        self.item_ids.filtered(lambda i: i.state == "error").write({"attempts": 0})
        return self.action_sync_now()

    # ------------------------------------------------------------------- sync
    @api.model
    def _cron_sync(self):
        for backend in self.search([]):
            try:
                backend._sync()
            except Exception:  # never let one server block the others
                _logger.exception("NAS sync failed for backend %s", backend.name)

    def _pending_attachments(self, rule, limit):
        """Attachments matching `rule` not yet synced to this backend."""
        Attachment = self.env["ir.attachment"].sudo()
        domain = [
            ("res_model", "=", rule.model_name),
            ("res_id", "!=", False),
            ("res_field", "=", False),
            ("type", "=", "binary"),
            (
                "webdav_item_ids",
                "not any",
                [
                    ("backend_id", "=", self.id),
                    "|",
                    ("state", "=", "done"),
                    ("attempts", ">=", MAX_ATTEMPTS),
                ],
            ),
        ]
        if rule.sync_from:
            domain.append(("create_date", ">=", rule.sync_from))
        record_domain = rule._get_record_domain()
        if record_domain:
            # Sub-query instead of a list of ids: stays fast on large models
            records = self.env[rule.model_name].sudo().with_context(
                active_test=False
            )._search(record_domain)
            domain.append(("res_id", "in", records))
        return Attachment.search(domain, limit=limit, order="id")

    def _remote_path(self, rule, attachment, record, used_paths):
        parts = [self._root(), safe_name(rule.folder_name or rule.model_id.name)]
        if rule.record_subfolder:
            parts.append(safe_name("%s (%s)" % (record.display_name, record.id)))
        filename = safe_name(attachment.name or "file-%s" % attachment.id)
        path = "/".join(p for p in parts if p) + "/" + filename
        if path in used_paths:
            stem, dot, ext = filename.rpartition(".")
            if not dot:
                stem, ext = filename, ""
            filename = "%s (%s)%s" % (stem, attachment.id, "." + ext if ext else "")
            path = "/".join(p for p in parts if p) + "/" + filename
        return path

    def _sync(self):
        self.ensure_one()
        Item = self.env["webdav.sync.item"].sudo()
        try:
            client = self._get_client()
            client.check(self._root())
        except WebDAVError as e:
            self.write({"last_sync": fields.Datetime.now(), "last_sync_message": str(e)[:250]})
            _logger.warning("NAS sync %s: %s", self.name, e)
            return

        remaining = self.batch_size or 200
        done = errors = 0
        for rule in self.rule_ids.filtered("active"):
            if remaining <= 0:
                break
            attachments = self._pending_attachments(rule, remaining)
            if not attachments:
                continue
            existing = {
                i.attachment_id.id: i
                for i in Item.search([
                    ("backend_id", "=", self.id),
                    ("attachment_id", "in", attachments.ids),
                ])
            }
            used_paths = set(Item.search([
                ("backend_id", "=", self.id), ("state", "=", "done"),
            ]).mapped("remote_path"))
            Model = self.env[rule.model_name].sudo()
            for att in attachments:
                remaining -= 1
                item = existing.get(att.id) or Item.create({
                    "backend_id": self.id,
                    "rule_id": rule.id,
                    "attachment_id": att.id,
                })
                record = Model.browse(att.res_id).exists()
                if not record:
                    item.write({
                        "state": "error",
                        "attempts": MAX_ATTEMPTS,
                        "error_message": _("Linked record no longer exists"),
                    })
                    errors += 1
                    continue
                # A changed file is re-uploaded over its existing remote copy
                path = item.remote_path or self._remote_path(rule, att, record, used_paths)
                try:
                    content_type = att.mimetype or mimetypes.guess_type(att.name or "")[0]
                    client.upload(path, att.raw or b"", content_type)
                except WebDAVError as e:
                    item.write({
                        "state": "error",
                        "attempts": item.attempts + 1,
                        "error_message": str(e)[:500],
                        "sync_date": fields.Datetime.now(),
                    })
                    errors += 1
                    continue
                used_paths.add(path)
                item.write({
                    "state": "done",
                    "remote_path": path,
                    "checksum": att.checksum,
                    "error_message": False,
                    "sync_date": fields.Datetime.now(),
                })
                done += 1
        message = _("%(done)s file(s) uploaded, %(errors)s error(s)", done=done, errors=errors)
        self.write({"last_sync": fields.Datetime.now(), "last_sync_message": message})
        _logger.info("NAS sync %s: %s", self.name, message)
        return done, errors
