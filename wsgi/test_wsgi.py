#! /usr/bin/env python

import multiprocessing
import os
import requests
import tempfile
import hashlib
from pathlib import Path
from wsgiref.simple_server import make_server
from wsgiref.validate import validator
from unittest.mock import patch
from datalad.api import Dataset
from datalad.utils import rmtree

from ria_wsgi import application


# wrap our wsgi app in wsgiref's validator app to potentially raise
# AssertionErrors due to WSGI spec violation
served_app = validator(application)


# Note: Server context manager basically taken from
#       datalad.tests.utils' serve_path_via_http and modified for our needs here

def _multiproc_serve(hostname, port, path, app, queue):
    os.chdir(path)
    httpd = make_server(hostname, port, app)
    httpd.base_environ['CONTEXT_DOCUMENT_ROOT'] = path
    queue.put(httpd.server_port)
    httpd.serve_forever()


class WSGITestServer(object):

    def __init__(self, app, base_path):
        self.app = app
        self.base_path = base_path
        self.url = None
        self._env_patch = None
        self._mproc = None

    def __enter__(self):
        self.start()
        return self.url

    def __exit__(self, *args):
        self.stop()

    def start(self):
        hostname = '127.0.0.1'
        port = 8080
        queue = multiprocessing.Queue()
        self._mproc = multiprocessing.Process(
            target=_multiproc_serve,
            args=(hostname, port, self.base_path, self.app, queue))
        self._mproc.start()
        port = queue.get(timeout=300)
        self.url = 'http://{}:{}/'.format(hostname, port)

        # Such tests don't require real network so if http_proxy settings were
        # provided, we remove them from the env for the duration of this run
        env = os.environ.copy()
        env.pop('http_proxy', None)
        self._env_patch = patch.dict('os.environ', env, clear=True)
        self._env_patch.start()

    def stop(self):
        """Stop serving `path`.
        """
        self._env_patch.stop()
        self._mproc.terminate()


def test_wsgi_no_store():

    with WSGITestServer(served_app, '/') as store_url:
        # there's no valid RIA store yet
        assert requests.get(store_url +
                            'some/non/existent/content').status_code == 500
        assert requests.head(store_url +
                             'some/non/existent/content').status_code == 500
        assert requests.get(store_url).status_code == 500

        # invalid regardless of whether or not there's a store
        # no POST
        assert requests.post(store_url).status_code == 400
        # no QUERY
        assert requests.get(store_url + "?id=3").status_code == 400


def test_wsgi_minimal_store():

    # set up a minimal store;
    # doesn't require an actual repo;
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / 'ria-layout-version').write_text('1')
        (td / '123' / 'fakeid').mkdir(parents=True, exist_ok=True)
        (td / '123' / 'fakeid' / 'ria-layout-version').write_text('2')

        with WSGITestServer(served_app, str(td)) as store_url:
            # setup should suffice to get a 404 instead of a 500:
            assert requests.get(
                store_url + 'some/non/existent').status_code == 404
            assert requests.head(
                store_url + 'some/non/existent').status_code == 404
            # POST remains invalid
            assert requests.post(store_url).status_code == 400
            # query parameters, too
            assert requests.get(store_url + "?id=3").status_code == 400


def test_wsgi_actual_store():

    with tempfile.TemporaryDirectory() as dsdir, \
            tempfile.TemporaryDirectory() as storedir:

        # create an actual dataset for a real annex/objects tree
        dsdir = Path(dsdir)
        ds = Dataset(dsdir).create()
        (dsdir / 'file_a').write_text('bla')
        (dsdir / 'file_b').write_text('blub')
        (dsdir / 'file_c').write_text('palim')
        ds.save()

        annex_obj_dir = dsdir / '.git' / 'annex' / 'objects'
        annex_objs = [Path('W2/j1/MD5E-s3--128ecf542a35ac5270a87dc740918404/MD5E-s3--128ecf542a35ac5270a87dc740918404'),
                      Path('wm/4j/MD5E-s4--455523d86a8a1ab7c7d33208fe0219e7/MD5E-s4--455523d86a8a1ab7c7d33208fe0219e7'),
                      Path('z3/Mw/MD5E-s5--f69f1d0cea22ba66e52fc8aeefb925f7/MD5E-s5--f69f1d0cea22ba66e52fc8aeefb925f7')]

        for obj in [annex_obj_dir / o for o in annex_objs]:
            assert obj.exists()

        # setup a store
        storedir = Path(storedir)
        (storedir / 'ria-layout-version').write_text('1')
        ds_in_store = (storedir / ds.id[:3] / ds.id[3:])
        (ds_in_store / 'annex').mkdir(parents=True, exist_ok=True)
        (ds_in_store / 'ria-layout-version').write_text('2')

        # move all keys into the store
        (dsdir / '.git' / 'annex' / 'objects').rename(
            ds_in_store / 'annex' / 'objects')

        with WSGITestServer(served_app, str(storedir)) as store_url:
            base_url = store_url + ds.id[:3] + '/' + ds.id[3:] \
                       + '/annex' + '/objects/'

            for key_path in annex_objs:
                key_url = base_url + str(key_path)
                # store knows those keys are available
                assert requests.head(key_url).status_code == 200
                # we can actually get them
                response = requests.get(key_url)
                assert response.status_code == 200
                md5 = hashlib.md5(response.content).hexdigest()
                assert md5 == str(key_path).split('--')[-1]

                # remove read permission
                (ds_in_store / 'annex' / 'objects' / key_path).chmod(0o000)

                # we have the key, but we don't give it to you:
                assert requests.head(key_url).status_code == 200
                assert requests.get(key_url).status_code == 403

                # restore permission
                (ds_in_store / 'annex' / 'objects' / key_path).chmod(0o444)

            # now put things in an archive
            (ds_in_store / 'archives').mkdir(parents=True, exist_ok=True)

            import subprocess
            subprocess.run(['7z', 'u',
                            str(ds_in_store / 'archives' / 'archive.7z'),
                            '.', '-mx0'],
                           cwd=str(ds_in_store / 'annex' / 'objects'))

            rmtree(str(ds_in_store / 'annex' / 'objects'))

            # files gone, still accessible via the archive:
            for key_path in annex_objs:
                key_url = base_url + str(key_path)
                # store knows those keys are available
                assert requests.head(key_url).status_code == 200
                # we can actually get them
                response = requests.get(key_url)
                assert response.status_code == 200
                md5 = hashlib.md5(response.content).hexdigest()
                assert md5 == str(key_path).split('--')[-1]
