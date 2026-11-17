import base64
from contextlib import closing
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.sql_db import db_connect
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models.webdav_backend import SYNC_LOCK_KEY
from .mock_dav import Store, serve


@tagged("post_install", "-at_install")
class TestNasSync(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.store = Store()
        cls.srv = serve(cls.store)
        cls.addClassCleanup(cls.srv.server_close)
        cls.addClassCleanup(cls.srv.shutdown)
        cls.url = "http://127.0.0.1:%s" % cls.srv.server_address[1]
        cls.partner = cls.env["res.partner"].create({"name": "ACME / SRL"})
        cls.partner2 = cls.env["res.partner"].create({"name": "Beta"})
        cls.backend = cls.env["webdav.backend"].create({
            "name": "Mock NAS",
            "server_type": "other",
            "url": cls.url,
            "username": "u",
            "password": "p",
            "root_folder": "Odoo/Test",
            "rule_ids": [(0, 0, {
                "model_id": cls.env.ref("base.model_res_partner").id,
                "folder_name": "Partners",
                # ignore attachments that already exist in the database
                "sync_from": cls.env.cr.now(),
            })],
        })

    def setUp(self):
        super().setUp()
        # post-commit hooks open registry cursors: let them see this transaction
        self.registry_enter_test_mode()
        self.store.files.clear()
        self.store.dirs.clear()
        self.store.dirs.add("")
        self.store.fail_put.clear()
        self.store.log.clear()

    def _att(self, record, name, data=b"data"):
        return self.env["ir.attachment"].create({
            "name": name,
            "res_model": record._name,
            "res_id": record.id,
            "datas": base64.b64encode(data),
        })

    def test_01_connection(self):
        res = self.backend.action_test_connection()
        self.assertEqual(res["params"]["type"], "success")
        self.assertIn("Odoo/Test", self.store.dirs)
        bad = self.backend.copy({"password": "x"})
        with self.assertRaises(UserError):
            bad.action_test_connection()

    def test_02_sync_layout_and_collisions(self):
        a1 = self._att(self.partner, "scan.pdf", b"one")
        a2 = self._att(self.partner, "scan.pdf", b"two")
        a3 = self._att(self.partner2, "noext", b"three")
        self._att(self.partner, "url").write({"type": "url", "url": "https://x"})
        done, errors = self.backend._sync()
        self.assertEqual((done, errors), (3, 0))
        folder = "Odoo/Test/Partners/ACME _ SRL (%s)" % self.partner.id
        self.assertEqual(self.store.files[folder + "/scan.pdf"][0], b"one")
        self.assertEqual(self.store.files[folder + "/scan (%s).pdf" % a2.id][0], b"two")
        self.assertIn("Odoo/Test/Partners/Beta (%s)/noext" % self.partner2.id, self.store.files)
        self.assertEqual(self.backend.synced_count, 3)
        # second run: nothing new
        self.assertEqual(self.backend._sync(), (0, 0))
        # new attachment with same name as an already synced one -> suffix
        a4 = self._att(self.partner, "scan.pdf", b"four")
        self.backend._sync()
        self.assertIn(folder + "/scan (%s).pdf" % a4.id, self.store.files)
        self.assertEqual(self.store.files[folder + "/scan.pdf"][0], b"one")
        self.assertTrue(a1.webdav_item_ids and a3.webdav_item_ids)

    def test_03_errors_and_retry(self):
        att = self._att(self.partner, "fail.txt")
        path = "Odoo/Test/Partners/ACME _ SRL (%s)/fail.txt" % self.partner.id
        self.store.fail_put.add(path)
        for i in range(1, 6):
            self.assertEqual(self.backend._sync(), (0, 1) if i <= 5 else (0, 0))
        item = att.webdav_item_ids
        self.assertEqual((item.state, item.attempts), ("error", 5))
        self.assertEqual(self.backend._sync(), (0, 0), "gives up after 5 attempts")
        self.store.fail_put.clear()
        self.backend.action_retry_errors()
        self.assertEqual(item.state, "done")
        self.assertIn(path, self.store.files)

    def test_04_batch_size_and_domain(self):
        self.backend.batch_size = 2
        for i in range(5):
            self._att(self.partner, "f%s.txt" % i)
        self.assertEqual(self.backend._sync()[0], 2)
        self.assertEqual(self.backend._sync()[0], 2)
        self.assertEqual(self.backend._sync()[0], 1)
        self.backend.batch_size = 200
        self.backend.rule_ids.domain = "[('name', '=', 'Beta')]"
        self._att(self.partner, "acme.txt")
        b = self._att(self.partner2, "beta.txt")
        self.assertEqual(self.backend._sync(), (1, 0))
        self.assertEqual(b.webdav_item_ids.state, "done")

    def test_05_server_down(self):
        self._att(self.partner, "x.txt")
        self.backend.url = "http://127.0.0.1:1"
        self.backend.timeout = 2
        self.assertIsNone(self.backend._sync())
        self.assertIn("failed", self.backend.last_sync_message)
        # only this server: other servers of the database are not part of the test
        self.env["webdav.backend"].search([("id", "!=", self.backend.id)]).active = False
        self.env["webdav.backend"]._cron_sync()  # must not raise

    def test_06_delete_remote(self):
        att = self._att(self.partner, "del.txt")
        self.backend._sync()
        path = att.webdav_item_ids.remote_path
        self.assertIn(path, self.store.files)
        att.unlink()
        self.assertIn(path, self.store.files, "delete_remote disabled by default")
        self.backend.delete_remote = True
        att = self._att(self.partner, "del2.txt")
        self.backend._sync()
        path = att.webdav_item_ids.remote_path
        user = new_test_user(self.env, "nas_emp", groups="base.group_user,base.group_partner_manager")
        att.with_user(user).unlink()
        self.assertIn(path, self.store.files, "remote copy kept until the commit")
        self.env.cr.postcommit.run()
        self.assertNotIn(path, self.store.files)
        self.assertFalse(self.env["webdav.sync.item"].search([("remote_path", "=", path)]))

    def test_07_deleted_record(self):
        p = self.env["res.partner"].create({"name": "Gone"})
        att = self._att(p, "g.txt")
        # simulate orphan attachment (record removed in SQL)
        self.env.cr.execute("UPDATE ir_attachment SET res_id = 999999999 WHERE id = %s", [att.id])
        att.invalidate_recordset()
        self.backend.company_id = False  # the company filter already skips it
        self.assertEqual(self.backend._sync(), (0, 1))
        self.assertEqual(att.webdav_item_ids.attempts, 5)

    def test_08_regular_user_reads_attachment(self):
        """A non-admin user must still be able to read attachments."""
        att = self._att(self.partner, "r.txt")
        self.backend._sync()
        user = new_test_user(self.env, "nas_emp2", groups="base.group_user")
        att_u = att.with_user(user)
        att_u.read(["name", "datas"])
        self.assertNotIn("webdav_item_ids", att_u.fields_get())
        with self.assertRaises(AccessError):
            att_u.read(["webdav_item_ids"])

    def test_09_modified_attachment_resynced(self):
        att = self._att(self.partner, "v.txt", b"v1")
        self.backend._sync()
        path = att.webdav_item_ids.remote_path
        att.datas = base64.b64encode(b"v2")
        self.assertEqual(att.webdav_item_ids.state, "pending")
        self.assertEqual(self.backend._sync(), (1, 0))
        item = att.webdav_item_ids
        self.assertEqual(len(item), 1)
        self.assertEqual(item.remote_path, path, "same remote file is overwritten")
        self.assertEqual(self.store.files[path][0], b"v2", "changed content is re-uploaded")
        self.assertEqual(item.checksum, att.checksum)
        self.assertEqual(len([f for f in self.store.files if "v" in f.rpartition("/")[2]]), 1)

    def test_10_rule_domain_with_uid(self):
        self.partner.user_id = self.env.user
        self.backend.rule_ids.domain = "[('user_id', '=', uid)]"
        a = self._att(self.partner, "u.txt")
        b = self._att(self.partner2, "u2.txt")
        self.assertEqual(self.backend._sync(), (1, 0))
        self.assertEqual(a.webdav_item_ids.state, "done")
        self.assertFalse(b.webdav_item_ids)
        self.backend.rule_ids.domain = "[('create_date', '>=', context_today() - relativedelta(days=1))]"
        self.assertEqual(self.backend._sync(), (1, 0))

    def test_10b_invalid_domain_rejected(self):
        with self.assertRaises(ValidationError):
            self.backend.rule_ids.domain = "[('no_such_field', '=', 1)]"
        with self.assertRaises(ValidationError):
            self.backend.rule_ids.domain = "[('name', '=', undefined_name)]"

    def test_09b_rename_only_not_resynced(self):
        att = self._att(self.partner, "n.txt")
        self.backend._sync()
        att.name = "renamed.txt"
        self.assertEqual(att.webdav_item_ids.state, "done")

    def test_11_actions(self):
        self.backend.action_view_items()
        res = self.backend.action_sync_now()
        self.assertEqual(res["tag"], "display_notification")
        self.backend.copy()

    def test_12_delete_remote_rolled_back(self):
        self.backend.delete_remote = True
        att = self._att(self.partner, "keep.txt")
        self.backend._sync()
        path = att.webdav_item_ids.remote_path
        # unlink undone by a savepoint: the post-commit hook still runs later
        with self.assertRaises(ZeroDivisionError), self.env.cr.savepoint():
            att.unlink()
            1 / 0
        self.env.invalidate_all()
        self.assertTrue(att.exists())
        self.env.cr.postcommit.run()
        self.assertIn(path, self.store.files, "attachment still exists: keep the copy")

    def test_13_unique_item(self):
        att = self._att(self.partner, "u.txt")
        self.backend._sync()
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self.env["webdav.sync.item"].create({
                "backend_id": self.backend.id, "attachment_id": att.id,
            })

    def test_14_one_sync_per_server(self):
        self._att(self.partner, "l.txt")
        # another connection holds the lock (= a sync already running)
        with closing(db_connect(self.env.cr.dbname).cursor()) as other:
            other.execute("SELECT pg_advisory_xact_lock(%s, %s)", [SYNC_LOCK_KEY, self.backend.id])
            with self.assertRaisesRegex(UserError, "already running"):
                self.backend.action_sync_now()
            self.assertIsNone(self.backend._sync(commit=True), "cron skips it")
            self.assertFalse(self.store.files)
            other.rollback()
        self.assertEqual(self.backend._sync(), (1, 0))

    def test_15_cron_commits_progress(self):
        for i in range(3):
            self._att(self.partner, "c%s.txt" % i)
        Cron = self.registry["ir.cron"]
        with patch.object(Cron, "_commit_progress", return_value=100.0) as progress:
            self.assertEqual(self.backend._sync(commit=True), (3, 0))
        self.assertEqual(progress.call_count, 2, "commit after each file but the last")
        for i in range(3):
            self._att(self.partner, "d%s.txt" % i)
        with patch.object(Cron, "_commit_progress", return_value=0.0):
            self.assertEqual(self.backend._sync(commit=True), (1, 0), "stops when out of time")
        self.assertEqual(self.backend._sync(), (2, 0), "the rest goes next run")

    def test_16_company_filter(self):
        company2 = self.env["res.company"].create({"name": "Other Co"})
        mine = self.env["res.partner"].create({"name": "Mine", "company_id": self.env.company.id})
        other = self.env["res.partner"].create({"name": "Other", "company_id": company2.id})
        a_shared = self._att(self.partner, "shared.txt")
        a_mine = self._att(mine, "mine.txt")
        a_other = self._att(other, "other.txt")
        self.backend.company_id = self.env.company
        self.assertEqual(self.backend._sync(), (2, 0))
        self.assertTrue(a_shared.webdav_item_ids)
        self.assertTrue(a_mine.webdav_item_ids)
        self.assertFalse(a_other.webdav_item_ids, "documents of another company are skipped")
        self.backend.company_id = False
        self.assertEqual(self.backend._sync(), (1, 0), "no company: every company is synced")

    def test_17_renamed_record_folder_moved(self):
        a1 = self._att(self.partner2, "r1.txt", b"one")
        a2 = self._att(self.partner2, "r2.txt", b"two")
        self.backend._sync()
        old = "Odoo/Test/Partners/Beta (%s)" % self.partner2.id
        self.assertIn(old + "/r1.txt", self.store.files)
        self.partner2.name = "Beta Group"
        self.store.log.clear()
        self.assertEqual(self.backend._sync(), (0, 0))
        new = "Odoo/Test/Partners/Beta Group (%s)" % self.partner2.id
        self.assertEqual(self.store.files[new + "/r1.txt"][0], b"one")
        self.assertEqual(self.store.files[new + "/r2.txt"][0], b"two")
        self.assertNotIn(old, self.store.dirs)
        self.assertNotIn("PUT", [m for m, _p in self.store.log], "moved, not re-uploaded")
        self.assertEqual(a1.webdav_item_ids.remote_path, new + "/r1.txt")
        self.assertEqual(a2.webdav_item_ids.remote_path, new + "/r2.txt")
        # a new file goes to the new folder
        a3 = self._att(self.partner2, "r3.txt")
        self.backend._sync()
        self.assertEqual(a3.webdav_item_ids.remote_path, new + "/r3.txt")

    def test_17b_rename_when_target_exists_or_files_gone(self):
        a1 = self._att(self.partner2, "t1.txt", b"one")
        a2 = self._att(self.partner2, "t2.txt", b"two")
        self.backend._sync()
        old = "Odoo/Test/Partners/Beta (%s)" % self.partner2.id
        new = "Odoo/Test/Partners/Beta SA (%s)" % self.partner2.id
        self.store.dirs.add(new)  # the target folder already exists on the NAS
        del self.store.files[old + "/t2.txt"]  # and someone deleted a file there
        self.partner2.name = "Beta SA"
        self.assertEqual(self.backend._sync(), (1, 0), "the missing file is uploaded again")
        self.assertEqual(self.store.files[new + "/t1.txt"][0], b"one")
        self.assertEqual(self.store.files[new + "/t2.txt"][0], b"two")
        self.assertEqual(a1.webdav_item_ids.remote_path, new + "/t1.txt")
        self.assertEqual(a2.webdav_item_ids.remote_path, new + "/t2.txt")

    def test_18_same_content_not_reuploaded(self):
        att = self._att(self.partner, "same.txt", b"same")
        self.backend._sync()
        att.datas = base64.b64encode(b"same")
        self.assertEqual(att.webdav_item_ids.state, "done", "identical content: nothing to do")
        att.datas = base64.b64encode(b"other")
        self.assertEqual(att.webdav_item_ids.state, "pending")

    def test_19_file_type_and_size_filter(self):
        rule = self.backend.rule_ids
        rule.write({"file_types": "PDF, .xml", "max_size_mb": 0.001})  # ~1 KB
        pdf = self._att(self.partner, "a.pdf")
        xml = self._att(self.partner, "b.XML")
        jpg = self._att(self.partner, "c.jpg")
        big = self._att(self.partner, "big.pdf", b"x" * 5000)
        self.assertEqual(self.backend._sync(), (2, 0))
        self.assertTrue(pdf.webdav_item_ids and xml.webdav_item_ids)
        self.assertFalse(jpg.webdav_item_ids, "other file types are skipped")
        self.assertFalse(big.webdav_item_ids, "files over the size limit are skipped")
        rule.write({"file_types": False, "max_size_mb": 0})
        self.assertEqual(self.backend._sync(), (2, 0))

    def test_20_alert_on_failed_files(self):
        todo = self.env.ref("mail.mail_activity_data_todo")
        self.backend.alert_user_id = self.env.ref("base.user_admin")
        att = self._att(self.partner, "f.txt")
        self.store.fail_put.add("Odoo/Test/Partners/ACME _ SRL (%s)/f.txt" % self.partner.id)
        for _i in range(4):
            self.backend._sync()
        self.assertFalse(self.backend.activity_ids, "no alert before the last attempt")
        self.backend._sync()
        self.assertEqual(att.webdav_item_ids.attempts, 5)
        activity = self.backend.activity_ids
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.activity_type_id, todo)
        self.assertEqual(activity.user_id, self.backend.alert_user_id)
        self.assertIn("could not be uploaded", activity.summary)
        self.assertIn("f.txt", self.backend.message_ids[0].body)
        # a second failure does not pile up To-Dos
        self.backend.item_ids.write({"attempts": 4})
        self.backend._sync()
        self.assertEqual(len(self.backend.activity_ids), 1)

    def test_21_alert_when_server_unreachable(self):
        self.backend.write({
            "url": "http://127.0.0.1:1", "timeout": 1,
            "alert_user_id": self.env.ref("base.user_admin").id,
        })
        for _i in range(3):
            self.backend._sync()
        self.assertEqual(self.backend.connection_failures, 3)
        self.assertFalse(self.backend.activity_ids)
        self.backend._sync()
        self.assertIn("unreachable", self.backend.activity_ids.summary)
        self.backend._sync()
        self.assertEqual(len(self.backend.activity_ids), 1, "alerted once")
        self.backend.url = self.url
        self.backend._sync()
        self.assertEqual(self.backend.connection_failures, 0, "reset when it works again")
