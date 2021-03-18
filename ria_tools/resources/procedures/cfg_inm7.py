"""Apply the default configuration to use the INM7 data infrastructure

NOTE: While this procedure should still work, they way it's implemented is not
up-to-date and should change to make use of new datalad-create-sibling-ria and
"ria+scheme://..." URLs instead of configurations for host and basepath.


Specifically this will:

- initialize special remote access to the INM7 data store (inm7-storage)
- create a git remote 'inm7' for the RIA store and set a publication dependency
  on inm7-storage


XXXXXXXXXXXXX  OUTDATED: XXXXXXXXXXXXXXXX

It used to:

- create a project for this dataset under the namespace of the INM7 on
  jugit.fz-juelich.de
- configure this GitLab project as a remote, and set up a publication
  dependency on inm7-storage


For this, there used to be additional code to set up the GitLab project.
However, given the current state of RIA stores, it should actually establish a
two-step publication dependency and therefore the git remote to RIA needs a
different name (gitlab-remote depends on git-remote-ria depends on
special-remote-ria):

# if it has a dataset ID, it also should have a commit
# do this should be safe to run
author, err = ds.repo._git_custom_command(
    '',
    ['git', 'log', '-n', '1', "--pretty=format:%aN <%aE>"]
)

print('Create matching GitLab project (needs JuGit API access)')
ds.create_sibling_gitlab(
    site='inm7',
    project="inm7/hammerpants/{}".format(ds.id),
    layout='flat',
    name='inm7',
    existing='error',
    publish_depends='inm7-storage',
    description="Creator: {}".format(author),
    access='ssh',
)

"""

import sys
import subprocess
from datalad.distribution.dataset import require_dataset
from datalad.support.sshconnector import SSHManager
from ria_remote.remote import RIARemote
from pathlib import Path
from six import text_type

import logging

lgr = logging.getLogger('datalad.procedure.inm7')


GIT_REMOTE_NAME = "inm7"
SPECIAL_REMOTE_NAME = "inm7-storage"


def get_cfg(dataset):
    """Read local/dataset configuration for inm7 remote
    """
    config = dict()
    # TODO: consider annexconfig the same way the special remote does (in-dataset special remote config)
    base_path = dataset.config.get("annex.ria-remote.inm7-storage.base-path", None)
    if not base_path:
        lgr.error("Missing required 'annex.ria-remote.inm7-storage.base-path' configuration")
        # Note: We don't have full control of how an error is communicated to the user from within this procedure.
        # Exiting non-zero leads to CommandError (by datalad-run-procedure). Exiting zero doesn't seem optimal either.
        sys.exit(1)

    config['base_path'] = Path(base_path)
    config['ssh_host'] = dataset.config.get("annex.ria-remote.inm7-storage.ssh-host")
    if config['ssh_host'] == '0':
        config['ssh_host'] = None

    # determine layout locations
    config['repo_path'], config['archive_path'], config['objects_path'] = \
        RIARemote.get_layout_locations(config['base_path'], dataset.id)

    return config


def configure_special_remote(dataset):
    print('Configure INM7 data store access (needs VPN for external access)')
    cmd = ['git', 'annex',
           'initremote', 'inm7-storage',
           'type=external',
           'externaltype=ria',
           'encryption=none',
           'autoenable=true'
           ]
    result = subprocess.run(cmd, cwd=str(dataset.path), stderr=subprocess.PIPE)
    if result.returncode != 0:
        if result.stderr == b'git-annex: There is already a special remote named "inm7-storage".' \
                                                   b' (Use enableremote to enable an existing special remote.)\n':
            # run enableremote instead
            cmd[2] = 'enableremote'
            subprocess.run(cmd, cwd=str(dataset.path))
        else:
            raise RuntimeError("initremote failed.\nstdout: %s\nstderr: %s" % (result.stdout, result.stderr))


def setup_storage_tree(dataset, ssh_host, repo_path):
    """
    1. trigger creation of the dataset's directory at the remote end
    2. make it a bare repository
    """

    # Note: All it actually takes is to trigger the special remote's `prepare` method once.
    # ATM trying to achieve that by invoking a minimal fsck.
    # TODO: - It's probably faster to actually talk to the special remote (i.e. pretending to be annex and use the
    #       protocol to send PREPARE)
    #       - Alternatively we can create the remote directory and ria version file directly, but this means code
    #       duplication that then needs to be kept in sync with ria-remote implementation.
    #       - this leads to the third option: Have that creation routine importable and callable from ria-remote package
    #       without the need to actually instantiate a RIARemote object
    print("Initializing INM7 storage for this dataset")
    cmd = ['git', 'annex', 'fsck', '--from=inm7-storage', '--fast', '--exclude=*/*']
    subprocess.run(cmd, cwd=text_type(dataset.path))

    # TODO: we should prob. check whether it's there already. How?
    # Note: like the special remote itself, we assume local FS if no SSH host is specified
    if ssh_host:
        sshmanager = SSHManager()
        ssh = sshmanager.get_connection(ssh_host, use_remote_annex_bundle=False)
        ssh.open()
        ssh('cd {} && git init --bare'.format(repo_path))
    else:
        cmd = ['git', 'init', '--bare']
        subprocess.run(cmd, cwd=text_type(repo_path), check=True)


def configure_git_remote(dataset, ssh_host, repo_path):
    """add a git remote to the bare repository"""

    # Note: needs annex-ignore! Otherwise we might push into default annex/object tree instead of
    # directory type tree with dirhash lower. This in turn would be an issue, if we want to pack the entire thing into
    # an archive. Special remote will then not be able to access content in the "wrong" place within the archive

    dataset.config.set("remote.inm7.annex-ignore", value="true", where="local")
    dataset.siblings(
        'configure',
        name='inm7',
        url='ssh://{}{}'.format(ssh_host, text_type(repo_path))
            if ssh_host
            else text_type(repo_path),
        recursive=False,
        publish_depends='inm7-storage',
        result_renderer=None)


def publish_index(dataset):
    # Publish to the git remote (without data)
    # This should prevent weird disconnected history situations
    # and give the remote end an idea who's dataset that is
    print("Updating sibling inm7")
    dataset.publish(to="inm7", transfer_data='none')


ds = require_dataset(
    sys.argv[1],
    check_installed=True,
    purpose='INM7 dataset configuration')

if ds.repo.get_hexsha() is None or ds.id is None:
    raise RuntimeError(
        "Repository at {} is not a DataLad dataset, "
        "run 'datalad create' first.".format(ds.path))


cfg = get_cfg(ds)
configure_special_remote(ds)

# Does this depend on existing remotes?
setup_storage_tree(ds, cfg['ssh_host'], cfg['repo_path'])

configure_git_remote(ds, cfg['ssh_host'], cfg['repo_path'])
publish_index(ds)
