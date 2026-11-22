"""Where each teacher's files live.

LocalStore  - a folder per user on the server (for running on one laptop, or testing).
DriveStore  - an "RPH Generator" folder in the teacher's own Google Drive (used when signed in with Google).
              Nothing is lost when a free server restarts, and the teacher can see their files in Drive.
"""
import json
import os
import re
import time

import requests

MIME = {
    '.json': 'application/json',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.pdf': 'application/pdf',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
}


def _mime(name):
    return MIME.get(os.path.splitext(name)[1].lower(), 'application/octet-stream')


def _safe(name):
    return re.sub(r'[^A-Za-z0-9._ -]', '_', name)[:120]


class Store:
    def read(self, name):  # -> bytes | None
        raise NotImplementedError

    def write(self, name, data):
        raise NotImplementedError

    def link(self, name):  # a URL to open the file, if there is one
        return None

    def read_json(self, name, default=None):
        b = self.read(name)
        return json.loads(b) if b else default

    def write_json(self, name, obj):
        self.write(name, json.dumps(obj, ensure_ascii=False, indent=1, default=str).encode())


class LocalStore(Store):
    def __init__(self, root, user):
        self.dir = os.path.join(root, _safe(user))
        os.makedirs(self.dir, exist_ok=True)

    def read(self, name):
        p = os.path.join(self.dir, _safe(name))
        if os.path.exists(p):
            with open(p, 'rb') as f:
                return f.read()
        return None

    def write(self, name, data):
        with open(os.path.join(self.dir, _safe(name)), 'wb') as f:
            f.write(data)


class DriveStore(Store):
    API = 'https://www.googleapis.com/drive/v3/files'
    UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
    FOLDER = 'RPH Generator'
    _ids = {}      # (user, name) -> file id
    _cache = {}    # (user, name) -> (time, bytes)

    def __init__(self, token, user):
        self.h = {'Authorization': f'Bearer {token}'}
        self.user = user
        self.folder = self._folder()

    def _q(self, q):
        r = requests.get(self.API, headers=self.h, params={'q': q, 'fields': 'files(id,name,webViewLink)', 'spaces': 'drive'}, timeout=30)
        r.raise_for_status()
        return r.json().get('files', [])

    def _folder(self):
        key = (self.user, '/')
        if key in self._ids:
            return self._ids[key]
        f = self._q(f"name = '{self.FOLDER}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
        if f:
            fid = f[0]['id']
        else:
            r = requests.post(self.API, headers=self.h, json={'name': self.FOLDER, 'mimeType': 'application/vnd.google-apps.folder'}, timeout=30)
            r.raise_for_status()
            fid = r.json()['id']
        self._ids[key] = fid
        return fid

    def _id(self, name):
        key = (self.user, name)
        if key not in self._ids:
            esc = name.replace("'", "\\'")
            f = self._q(f"name = '{esc}' and '{self.folder}' in parents and trashed = false")
            if not f:
                return None
            self._ids[key] = f[0]['id']
        return self._ids[key]

    def read(self, name):
        key = (self.user, name)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        fid = self._id(name)
        if not fid:
            return None
        r = requests.get(f'{self.API}/{fid}', headers=self.h, params={'alt': 'media'}, timeout=60)
        if r.status_code == 404:
            self._ids.pop(key, None)
            return None
        r.raise_for_status()
        self._cache[key] = (time.time(), r.content)
        return r.content

    def write(self, name, data):
        key = (self.user, name)
        fid = self._id(name)
        meta = {'name': name}
        if fid:
            url, method = f'{self.UPLOAD}/{fid}?uploadType=multipart', requests.patch
        else:
            url, method = f'{self.UPLOAD}?uploadType=multipart', requests.post
            meta['parents'] = [self.folder]
        files = {'metadata': ('metadata', json.dumps(meta), 'application/json'), 'file': (name, data, _mime(name))}
        r = method(url, headers=self.h, files=files, timeout=120)
        r.raise_for_status()
        self._ids[key] = r.json()['id']
        self._cache[key] = (time.time(), data)

    def link(self, name):
        fid = self._id(name)
        return f'https://drive.google.com/file/d/{fid}/view' if fid else None

    def folder_link(self):
        return f'https://drive.google.com/drive/folders/{self.folder}'
