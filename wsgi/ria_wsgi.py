#!/usr/bin/env python

import sys
import hashlib
import subprocess
from pathlib import Path
from io import BytesIO


# Requested URI is supposed to point to an annex key (dirhashmixed),
# i.e. something like
# /store/814/cb17e-95b2-11ea-8bc2-d14d8c08eceb/annex/objects/km/xp/MD5E-s5--a2e5e988620bbe17ef14dfd37e4d86ca/MD5E-s5--a2e5e988620bbe17ef14dfd37e4d86ca

# In simplest case this is exactly what we need to look at

# store class (+cache?): Based on CONTEXT_DOCUMENT_ROOT
# AnnexObject can then be based on PATH_INFO


# TODO: This is imported once during runtime of webserver. We could use module
#       level for caching!
#       - Note: Datasets/stores may have different layouts/permissions
#               -> Cache needs file path keys
#               -> How to deal with permissions?


BLOCKSIZE = 8192
# ATM this needs to be adjusted to server config and may need to be
# "SCRIPT_NAME" for example:
PATH_IN_ENV = "PATH_INFO"

class KeyNotFoundError(Exception):
    pass


class AnnexObject(object):
    """

    """

    def __init__(self, request_path, path_prefix):
        """

        :param request_path:
        :param path_prefix:
        """
        path_prefix = Path(path_prefix)

        uri_parts = request_path.split('/')
        self.key = uri_parts[-1]
        # Note, that uri_parts[1:-6] derives from:
        # - URI has to start with '/', therefore split() results starts with
        #   empty string
        # - 6 substracted levels: annex/objects/tree_1/tree_2/key_dir/key_file
        ds_dir = path_prefix.joinpath(*uri_parts[1:-6])

        self.archive_path = ds_dir / 'archives' / 'archive.7z'

        # We need to figure where to actually look for a key file. Currently a
        # dataset may use dirhashlower or dirhashmixed to build its
        # annex/objects tree.
        # See https://git-annex.branchable.com/internals/hashing/
        ds_layout_version = \
            (ds_dir / 'ria-layout-version').read_text().strip().split('|')[0]
        if ds_layout_version == '1':
            # dataset representation uses dirhashlower
            md5 = hashlib.md5(self.key.encode()).hexdigest()
            self.object_path = Path(md5[:3]) / md5[3:] / self.key / self.key
            self.file_path = ds_dir / 'annex' / 'objects' / self.object_path

        elif ds_layout_version == '2':
            # dataset representation uses dirhashmixed; this is what we expect
            # the original URI to be already. Note, that URI is absolute while
            # we need to look at it as a relative path.
            self.file_path = path_prefix.joinpath(*uri_parts[1:])
            self.object_path = Path(uri_parts[-4]).joinpath(*uri_parts[-3:])
        else:
            # TODO: Proper status
            raise ValueError("layout: %s" % ds_layout_version)

        self._exists = None
        self._in_archive = None

    def in_archive(self):

        def check_archive():
            if not self.archive_path.exists():
                # no archive, no file
                return False
            # TODO: ultimately those paths come from user input (the request).
            #       What exactly do we need to make sure at this point wrt
            #       security?
            loc = str(self.object_path)
            arc = str(self.archive_path)

            try:
                res = subprocess.run(['7z', 'l', arc, loc],
                                     stdout=subprocess.PIPE,
                                     check=True)
            except subprocess.CalledProcessError:
                # - if we can't run that, we don't have access
                # - includes missing 7z executable
                return False

            return loc in res.stdout.decode()

        # store result; note that within this script we respond to a single
        # request
        if self._in_archive is None:
            self._in_archive = check_archive()
        return self._in_archive

    def in_object_tree(self):

        # store result; note that within this script we respond to a single
        # request
        if self._exists is None:
            self._exists = self.file_path.exists()
        return self._exists

    def is_present(self):

        # TODO: What do missing read permissions lead to with those checks?
        #       => we may not want to reveal whether a key is here if requesting
        #       user has no permission to read that key and/or dataset.

        return self.in_object_tree() or self.in_archive()

    def get(self):

        if self.in_object_tree():
            return self.file_path.open('rb')
        elif self.in_archive():
            res = subprocess.run(['7z', 'x', '-so',
                                  str(self.archive_path),
                                  str(self.object_path)],
                                 stdout=subprocess.PIPE)
            return BytesIO(res.stdout)
        else:
            raise KeyNotFoundError

    def size(self):

        # see: https://git-annex.branchable.com/internals/key_format/
        key_parts = self.key.split('--')
        key_fields = key_parts[0].split('-')
        parsed = {field[0]: int(field[1:]) if field[1:].isdigit() else None
                  for field in key_fields[1:]
                  if field[0] in "sSC"}

        # don't lookup the dict for the same things several times;
        # Is there a faster (and more compact) way of doing this? Note, that
        # locals() can't be updated.
        s = parsed.get('s')
        S = parsed.get('S')
        C = parsed.get('C')

        if S is None and C is None:
            return s  # also okay if s is None as well -> no size to report
        elif s is None:
            # s is None, while S and/or C are not.
            raise ValueError("invalid key: {}".format(self.key))
        elif S and C:
            if C <= int(s / S):
                return S
            else:
                return s % S
        else:
            # S or C are given with the respective other one missing
            raise ValueError("invalid key: {}".format(self.key))


def application(environ, start_response):

    response_headers = list()
    response_body = None

    # fail early on invalid requests:
    # TODO: This probably needs to be enhanced. For now just have some rejection
    #       implemented from the start.
    if environ['QUERY_STRING'] or \
            environ['REQUEST_METHOD'] not in ['HEAD', 'GET']:
        # no query is currently implemented
        status = "400 Bad Request"
        response_body = "<h1>{}</h1>".format(status).encode('utf-8')
        response_headers.extend([('Content-Type', 'text/html; charset=utf-8'),
                                 ('Content-Length', str(len(response_body)))
                                 ])
        start_response(status, response_headers)
        return [response_body]

    # TODO: consider using the following rathern than querying env vars. Setup
    #       might be more complex.
    # from wsgiref.util import request_uri, application_uri

    # Note, that empty values might not be present in the environment at all
    # TODO: What are reasonable defaults in that case? / How to deal with it
    #       if we don't go for any defaults? ATM 'None' will lead to an
    #       exception in AnnexObject.__init__ and therefore to an 500 response,
    #       which may or may not be wat we want.
    key_object = AnnexObject(environ.get(PATH_IN_ENV),
                             environ.get("CONTEXT_DOCUMENT_ROOT", '/'))

    if environ.get("REQUEST_METHOD") == "GET":
        try:
            f = key_object.get()
            if 'wsgi.file_wrapper' in environ:  # optional acc. to WSGI spec
                response_body = environ['wsgi.file_wrapper'](f)
            else:
                response_body = iter(lambda: f.read(BLOCKSIZE), b'')

            status = "200 OK"
            response_headers.extend([
               ('Content-Type', 'application/octet-stream'),
               ('Content-Disposition',
                'attachment; filename=\"{}\"'.format(key_object.key))
            ])
            try:
                response_headers.append(('Content-Length',
                                         str(key_object.size())))
            except ValueError:
                # invalid key
                # TODO: for now just no size info
                #       but:
                #       we might want to consider checking this before hand and
                #       reject to serve sth that is based on an invalid key
                pass

            # TODO: ETag header field using the key itself?

        except KeyNotFoundError:
            status = "404 Not Found"
            response_headers.append(('Content-Type', 'text/html; charset=utf-8'))
            response_body = ["<h1>{}</h1>".format(status).encode('utf-8')]
        except PermissionError:
            status = "403 Forbidden"
            response_headers.append(('Content-Type', 'text/html; charset=utf-8'))
            # note, that the error itself intentionally isn't reported in a
            # public response, since it may contain paths etc. revealing
            # internal structure.
            # TODO: Put into store-side log instead
            response_body = ["<h1>{}</h1>".format(status).encode('utf-8')]
        except Exception as e:
            # something else failed
            # Note: report error class only to not reveal anything internal
            # TODO: Figure out proper exception detection and error reporting
            exctype, value, tb = sys.exc_info()
            status = "500 Internal Server Error"
            response_headers.append(('Content-Type', 'text/html; charset=utf-8'))
            response_body = ["<h1>{}</h1><p>{}</p>{}"
                             "".format(status, repr(exctype).strip('<>')
                                       ).encode('utf-8')]

    elif environ.get("REQUEST_METHOD") == "HEAD":
        # Check key availability and respond accordingly
        # TODO: - No read permission -> Status? 404 to not give it away?
        #       - "200 OK" or "302 Found"?

        if key_object.is_present():
            status = "200 OK"
            response_headers.append(('Content-Type', 'application/octet-stream'))
            try:
                response_headers.append(('Content-Length',
                                         str(key_object.size())))
            except ValueError:
                # invalid key
                # TODO: for now just no size info
                #       but:
                #       we might want to consider checking this before hand and
                #       reject to serve sth that is based on an invalid key
                pass

        else:
            status = "404 Not Found"
            response_body = ["<h1>{}</h1>".format(status).encode('utf-8')]
            response_headers.extend(
                [('Content-Type', 'text/html; charset=utf-8'),
                 ('Content-Length', str(len(response_body)))
                 ])

    start_response(status, response_headers)
    return response_body if response_body else []
