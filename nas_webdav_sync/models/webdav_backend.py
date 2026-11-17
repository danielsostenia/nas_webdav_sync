# -*- coding: utf-8 -*-
import logging
import mimetypes
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import plaintext2html

from .webdav_client import WebDAVClient, WebDAVError, safe_name

_logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# first key of the advisory lock taken while a server is being synced ("NAS")
SYNC_LOCK_KEY = 0x4E4153
# records written up to this long before the last sync are checked for renames
RENAME_MARGIN = timedelta(hours=1)
# alert after this many failed connections in a row (one hour with the 15 min cron)
ALERT_AFTER_FAILURES = 4


class WebdavBackend(models.Model):
    _name = "webdav.backend"
    _description = "NAS / WebDAV Server"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "sequence, id"

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, tracking=True)
    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
        help="Only sync documents of this company (and documents shared by all "
             "companies). Leave empty to sync the documents of every company.",
    )

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

    alert_user_id = fields.Many2one(
        "res.users",
        "Alert",
        default=lambda self: self.env.user,
        domain=[("share", "=", False)],
        help="Gets a To-Do when the server is unreachable for an hour or files keep failing. "
             "Followers of the server are notified as well.",
    )
    connection_failures = fields.Integer(readonly=True, copy=False)

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
                backend._sync(commit=True)
            except Exception:  # never let one server block the others
                self.env.cr.rollback()
                _logger.exception("NAS sync failed for backend %s", backend.name)

    def _alert(self, summary, details):
        """Tell the followers (chatter) and give the alert user a To-Do (only one open)."""
        self.ensure_one()
        body = plaintext2html(details)
        self.message_post(body=body, subject=summary, subtype_xmlid="mail.mt_comment")
        user = self.alert_user_id
        if not user or not user.active:
            return
        todo = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        if self.activity_ids.filtered(lambda a: a.user_id == user and a.activity_type_id == todo):
            return
        self.activity_schedule(
            "mail.mail_activity_data_todo", user_id=user.id, summary=summary, note=body,
        )

    def _try_lock(self):
        """Transaction-level advisory lock: only one sync runs per server.

        Released automatically on commit/rollback (never leaks on errors);
        the cron takes it again after each intermediate commit.
        """
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)", [SYNC_LOCK_KEY, self.id]
        )
        return self.env.cr.fetchone()[0]

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
        domain += rule._get_file_domain()
        record_domain = list(rule._get_record_domain())
        Model = self.env[rule.model_name]
        company_field = Model._fields.get("company_id")
        if (self.company_id and company_field and company_field.store
                and company_field.comodel_name == "res.company"):
            record_domain.append(("company_id", "in", [self.company_id.id, False]))
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
            parts.append(self._record_folder(record))
        filename = safe_name(attachment.name or "file-%s" % attachment.id)
        path = "/".join(p for p in parts if p) + "/" + filename
        if path in used_paths:
            stem, dot, ext = filename.rpartition(".")
            if not dot:
                stem, ext = filename, ""
            filename = "%s (%s)%s" % (stem, attachment.id, "." + ext if ext else "")
            path = "/".join(p for p in parts if p) + "/" + filename
        return path

    def _record_folder(self, record):
        return safe_name("%s (%s)" % (record.display_name, record.id))

    def _move_renamed_folders(self, client, rule):
        """Rename '<record> (<id>)' folders on the NAS after the record was renamed.

        Only records modified since their files were synced are checked.
        Returns the number of moved folders.
        """
        if not rule.record_subfolder:
            return 0
        items = self.env["webdav.sync.item"].sudo().search([
            ("backend_id", "=", self.id),
            ("rule_id", "=", rule.id),
            ("remote_path", "!=", False),
            ("attachment_id.res_model", "=", rule.model_name),
        ])
        by_record = {}
        for item in items:
            by_record.setdefault(item.attachment_id.res_id, self.env["webdav.sync.item"])
            by_record[item.attachment_id.res_id] |= item
        records = self.env[rule.model_name].sudo().browse(list(by_record)).exists()
        moved = 0
        for record in records:
            record_items = by_record[record.id]
            last_sync = min((i.sync_date for i in record_items if i.sync_date), default=None)
            # margin: a rename committed by a transaction that started before
            # the last sync has an older write_date
            if last_sync and record.write_date and record.write_date < last_sync - RENAME_MARGIN:
                continue
            new_name = self._record_folder(record)
            by_dir = {}
            for item in record_items:
                folder = item.remote_path.rpartition("/")[0]
                if folder.rpartition("/")[2] != new_name:
                    by_dir.setdefault(folder, []).append(item)
            for old_dir, dir_items in by_dir.items():
                new_dir = old_dir.rpartition("/")[0] + "/" + new_name
                moved += self._move_folder(client, old_dir, new_dir, dir_items)
        return moved

    def _move_folder(self, client, old_dir, new_dir, items):
        now = fields.Datetime.now()
        try:
            client.move(old_dir, new_dir, is_dir=True)
        except WebDAVError as e:
            # The new folder already exists (or the old one is gone): move the
            # files one by one, re-upload the ones missing on the NAS.
            _logger.info("NAS sync %s: folder move failed (%s), moving files", self.name, e)
            for item in items:
                new_path = new_dir + "/" + item.remote_path.rpartition("/")[2]
                try:
                    if client.exists(item.remote_path):
                        client.move(item.remote_path, new_path)
                        item.write({"remote_path": new_path, "sync_date": now})
                    else:
                        item.write({"remote_path": new_path, "state": "pending", "attempts": 0})
                except WebDAVError as err:
                    _logger.warning("NAS sync %s: could not move %s: %s",
                                    self.name, item.remote_path, err)
            return 1
        for item in items:
            item.write({
                "remote_path": new_dir + item.remote_path[len(old_dir):],
                "sync_date": now,
            })
        return 1

    def _sync(self, commit=False):
        """Upload pending files. With `commit` (cron), progress is committed
        after every file, so a timeout or crash never loses finished uploads.

        Returns (done, errors), or None when the server is unreachable or
        another sync of the same server is already running.
        """
        self.ensure_one()
        if not self._try_lock():
            if not commit:
                raise UserError(_("A sync of %s is already running. Try again in a moment.", self.name))
            _logger.info("NAS sync %s: already running, skipped", self.name)
            return None
        Item = self.env["webdav.sync.item"].sudo()
        try:
            client = self._get_client()
            client.check(self._root())
        except WebDAVError as e:
            failures = self.connection_failures + 1
            self.write({
                "last_sync": fields.Datetime.now(),
                "last_sync_message": str(e)[:250],
                "connection_failures": failures,
            })
            _logger.warning("NAS sync %s: %s", self.name, e)
            if failures == ALERT_AFTER_FAILURES:
                self._alert(
                    _("NAS server %s is unreachable", self.name),
                    _("The last %(count)s syncs failed:\n%(error)s", count=failures, error=e),
                )
            return None
        if self.connection_failures:
            self.connection_failures = 0

        out_of_time = False
        given_up = Item.browse()
        remaining = self.batch_size or 200
        done = errors = 0
        for rule in self.rule_ids.filtered("active"):
            if remaining <= 0 or out_of_time:
                break
            self._move_renamed_folders(client, rule)
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
                if commit and (done or errors):
                    # Commit the previous file. Stop when the cron is out of
                    # time or a manual sync took the lock in the meantime.
                    time_left = self.env["ir.cron"]._commit_progress(processed=1)
                    if time_left <= 0 or not self._try_lock():
                        out_of_time = True
                        break
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
                    if item.attempts >= MAX_ATTEMPTS:
                        given_up |= item
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
        if given_up:
            self._alert(
                _("%(count)s file(s) could not be uploaded to %(name)s",
                  count=len(given_up), name=self.name),
                _("Gave up after %(max)s attempts (see the Sync Log, then use Retry errors):\n%(files)s",
                  max=MAX_ATTEMPTS,
                  files="\n".join("- %s: %s" % (i.attachment_name or i.attachment_id.id,
                                                 (i.error_message or "")[:150])
                                  for i in given_up[:20])),
            )
        message = _("%(done)s file(s) uploaded, %(errors)s error(s)", done=done, errors=errors)
        self.write({"last_sync": fields.Datetime.now(), "last_sync_message": message})
        _logger.info("NAS sync %s: %s", self.name, message)
        return done, errors
