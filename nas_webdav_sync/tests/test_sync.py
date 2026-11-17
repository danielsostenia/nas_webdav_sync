import base64

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged

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
        self.assertNotIn(path, self.store.files)
        self.assertFalse(self.env["webdav.sync.item"].search([("remote_path", "=", path)]))

    def test_07_deleted_record(self):
        p = self.env["res.partner"].create({"name": "Gone"})
        att = self._att(p, "g.txt")
        # simulate orphan attachment (record removed in SQL)
        self.env.cr.execute("UPDATE ir_attachment SET res_id = 999999999 WHERE id = %s", [att.id])
        att.invalidate_recordset()
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
