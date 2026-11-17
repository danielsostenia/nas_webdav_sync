# -*- coding: utf-8 -*-
"""Minimal WebDAV client (no Odoo imports, so it can be tested standalone).

Works with Synology (WebDAV Server package), QNAP, TrueNAS, Nextcloud,
ownCloud and any RFC 4918 compliant server.
"""
import re
from urllib.parse import quote

import requests

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


class WebDAVError(Exception):
    pass


def safe_name(name, max_len=120):
    """Make a string safe to use as a single file/folder name on any NAS."""
    name = INVALID_CHARS.sub("_", (name or "").strip())
    name = name.strip(" .") or "_"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


class WebDAVClient:
    def __init__(self, url, username=None, password=None, verify=True, timeout=30):
        if not url:
            raise WebDAVError("WebDAV URL is required")
        self.base_url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if username:
            self.session.auth = (username, password or "")
        self.session.verify = verify
        self._known_dirs = set()

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _segments(path):
        return [s for s in (path or "").strip("/").split("/") if s]

    def _url(self, path, is_dir=False):
        segs = self._segments(path)
        url = self.base_url + "/" + "/".join(quote(s, safe="") for s in segs)
        if is_dir and segs:
            url += "/"
        return url

    def _request(self, method, path, is_dir=False, **kwargs):
        try:
            return self.session.request(
                method, self._url(path, is_dir), timeout=self.timeout, **kwargs
            )
        except requests.RequestException as e:
            raise WebDAVError("%s %s failed: %s" % (method, path or "/", e)) from e

    @staticmethod
    def _raise(resp, what):
        if resp.status_code == 401:
            raise WebDAVError("%s: authentication failed (401). Check username/password." % what)
        if resp.status_code == 403:
            raise WebDAVError("%s: access denied (403). Check folder permissions." % what)
        raise WebDAVError("%s: HTTP %s %s" % (what, resp.status_code, resp.text[:200]))

    # ---------------------------------------------------------------- actions
    def check(self, path=""):
        """Verify the server is reachable and credentials work."""
        resp = self._request(
            "PROPFIND", path, is_dir=True, headers={"Depth": "0"},
        )
        if resp.status_code in (200, 207):
            return True
        if resp.status_code == 404:
            # Root folder missing: try to create it
            self.ensure_dir(path)
            return True
        self._raise(resp, "Connection test")

    def ensure_dir(self, path):
        """Create every folder of `path` that does not exist yet (like mkdir -p)."""
        current = []
        for seg in self._segments(path):
            current.append(seg)
            sub = "/".join(current)
            if sub in self._known_dirs:
                continue
            resp = self._request("MKCOL", sub, is_dir=True)
            # 201 created, 405 already exists (most servers), 301 redirect to dir
            if resp.status_code not in (200, 201, 204, 301, 405):
                self._raise(resp, "Create folder '%s'" % sub)
            self._known_dirs.add(sub)

    def upload(self, path, data, content_type=None):
        folder = "/".join(self._segments(path)[:-1])
        if folder:
            self.ensure_dir(folder)
        headers = {"Content-Type": content_type or "application/octet-stream"}
        resp = self._request("PUT", path, data=data, headers=headers)
        if resp.status_code not in (200, 201, 204):
            self._raise(resp, "Upload '%s'" % path)
        return True

    def delete(self, path):
        resp = self._request("DELETE", path)
        if resp.status_code not in (200, 204, 404):
            self._raise(resp, "Delete '%s'" % path)
        return True

    def exists(self, path):
        resp = self._request("PROPFIND", path, headers={"Depth": "0"})
        return resp.status_code in (200, 207)
