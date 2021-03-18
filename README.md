# RIA-Tools

This is supposed to become a collection of scripts to setup and maintain a
datalad RIA store. For now this is not intended to be an installable package,
since generic deployment and acceptable dependencies aren't obvious. Instead
it's an entry point showing what you can (and may be have to) do and how.


## OUTDATED - base requirements for config procedure


- Server side:

    7z needs to be in the path.


- Client side:

    the storage's base path needs to be configured via
    annex.ria-remote.inm7-storage.base-path

    for SSH Connection a host needs to be specified via
annex.ria-remote.inm7-storage.ssh-host Any SSH config will be respected,
but needs to reference that very host, of course. Don't forget, that you may
need to configure your username for this host.

    Although those configs could be done at the dataset level, they should be
done at user or system level.  Note, that this means, that your local config
overrides whatever config may be stored in the dataset. This allows to access
that storage via SSH from your computer, while another clone of the same
dataset might access it via the local filesystem on brainbfast.  If no host is
configured, the base_path is assumed to be a local path. To explicitly
configure no host (for overriding) assign the value 0 to the respective config
variable.