import hashlib
import json
import os
import sys
import tempfile
import zipfile

import dcoscli
import docopt
import pkg_resources
from dcos import (cmds, emitting, errors, http, marathon, options, package,
                  subcommand, util)
from dcos.errors import DCOSException
from dcoscli import tables
from dcoscli.main import decorate_docopt_usage

logger = util.get_logger(__name__)
emitter = emitting.FlatEmitter()


def main():
    try:
        return _main()
    except DCOSException as e:
        emitter.publish(e)
        return 1


def _doc():
    return pkg_resources.resource_string(
        'dcoscli',
        'data/help/package.txt').decode('utf-8')


@decorate_docopt_usage
def _main():
    util.configure_process_from_environ()

    args = docopt.docopt(
        _doc(),
        version='dcos-package version {}'.format(dcoscli.version))
    http.silence_requests_warnings()

    return cmds.execute(_cmds(), args)


def _cmds():
    """
    :returns: All of the supported commands
    :rtype: dcos.cmds.Command
    """

    return [
        cmds.Command(
            hierarchy=['package', 'sources'],
            arg_keys=[],
            function=_list_sources),

        cmds.Command(
            hierarchy=['package', 'update'],
            arg_keys=['--validate'],
            function=_update),

        cmds.Command(
            hierarchy=['package', 'describe'],
            arg_keys=['<package-name>', '--app', '--cli', '--options',
                      '--render', '--package-versions', '--package-version',
                      '--config'],
            function=_describe),

        cmds.Command(
            hierarchy=['package', 'install'],
            arg_keys=['<package-name>', '--package-version', '--options',
                      '--app-id', '--cli', '--app', '--yes'],
            function=_install),

        cmds.Command(
            hierarchy=['package', 'list'],
            arg_keys=['--json', '--endpoints', '--app-id', '<package-name>'],
            function=_list),

        cmds.Command(
            hierarchy=['package', 'search'],
            arg_keys=['--json', '<query>'],
            function=_search),

        cmds.Command(
            hierarchy=['package', 'uninstall'],
            arg_keys=['<package-name>', '--all', '--app-id', '--cli', '--app'],
            function=_uninstall),

        cmds.Command(
            hierarchy=['package', 'bundle'],
            arg_keys=['<package-directory>', '--output-directory'],
            function=_bundle),

        cmds.Command(
            hierarchy=['package'],
            arg_keys=['--config-schema', '--info'],
            function=_package),
    ]


def _package(config_schema, info):
    """
    :param config_schema: Whether to output the config schema
    :type config_schema: boolean
    :param info: Whether to output a description of this subcommand
    :type info: boolean
    :returns: Process status
    :rtype: int
    """

    if config_schema:
        schema = json.loads(
            pkg_resources.resource_string(
                'dcoscli',
                'data/config-schema/package.json').decode('utf-8'))
        emitter.publish(schema)
    elif info:
        _info()
    else:
        emitter.publish(options.make_generic_usage_message(_doc()))
        return 1

    return 0


def _info():
    """Print package cli information.

    :returns: Process status
    :rtype: int
    """

    emitter.publish(_doc().split('\n')[0])
    return 0


def _list_sources():
    """List configured package sources.

    :returns: Process status
    :rtype: int
    """

    config = util.get_config()

    sources = package.list_sources(config)

    for source in sources:
        emitter.publish("{} {}".format(source.hash(), source.url))

    return 0


def _update(validate):
    """Update local package definitions from sources.

    :param validate: Whether to validate package content when updating sources.
    :type validate: bool
    :returns: Process status
    :rtype: int
    """

    config = util.get_config()

    package.update_sources(config, validate)

    return 0


def _describe(package_name,
              app,
              cli,
              options_path,
              render,
              package_versions,
              package_version,
              config):
    """Describe the specified package.

    :param package_name: The package to describe
    :type package_name: str
    :param app: If True, marathon.json will be printed
    :type app: boolean
    :param cli: If True, command.json should be printed
    :type cli: boolean
    :param options_path: Path to json file with options to override
                         config.json defaults.
    :type options_path: str
    :param render: If True, marathon.json and/or command.json templates
                   will be rendered
    :type render: boolean
    :param package_versions: If True, a list of all package versions will
                             be printed
    :type package_versions: boolean
    :param package_version: package version
    :type package_version: str | None
    :param config: If True, config.json will be printed
    :type config: boolean
    :returns: Process status
    :rtype: int
    """

    # If the user supplied template options, they definitely want to
    # render the template
    if options_path:
        render = True

    if package_versions and \
       (app or cli or options_path or render or package_version or config):
        raise DCOSException(
            'If --package-versions is provided, no other option can be '
            'provided')

    pkg = package.resolve_package(package_name)
    if pkg is None:
        raise DCOSException("Package [{}] not found".format(package_name))

    pkg_revision = pkg.latest_package_revision(package_version)

    if pkg_revision is None:
        raise DCOSException("Version {} of package [{}] is not available".
                            format(package_version, package_name))

    pkg_json = pkg.package_json(pkg_revision)

    if package_version is None:
        revision_map = pkg.package_revisions_map()
        pkg_versions = list(revision_map.values())
        del pkg_json['version']
        pkg_json['versions'] = pkg_versions

    if package_versions:
        emitter.publish('\n'.join(pkg_json['versions']))
    elif cli or app or config:
        user_options = _user_options(options_path)
        options = pkg.options(pkg_revision, user_options)

        if cli:
            if render:
                cli_output = pkg.command_json(pkg_revision, options)
            else:
                cli_output = pkg.command_template(pkg_revision)
                if cli_output and cli_output[-1] == '\n':
                    cli_output = cli_output[:-1]
            emitter.publish(cli_output)
        if app:
            if render:
                app_output = pkg.marathon_json(pkg_revision, options)
            else:
                app_output = pkg.marathon_template(pkg_revision)
                if app_output and app_output[-1] == '\n':
                    app_output = app_output[:-1]
            emitter.publish(app_output)
        if config:
            config_output = pkg.config_json(pkg_revision)
            emitter.publish(config_output)
    else:
        pkg_json = pkg.package_json(pkg_revision)
        emitter.publish(pkg_json)

    return 0


def _user_options(path):
    """ Read the options at the given file path.

    :param path: file path
    :type path: str
    :returns: options
    :rtype: dict
    """
    if path is None:
        return {}
    else:
        with util.open_file(path) as options_file:
            return util.load_json(options_file)


def _confirm(prompt, yes):
    """
    :param prompt: message to display to the terminal
    :type prompt: str
    :param yes: whether to assume that the user responded with yes
    :type yes: bool
    :returns: True if the user responded with yes; False otherwise
    :rtype: bool
    """

    if yes:
        return True
    else:
        while True:
            sys.stdout.write('{} [yes/no] '.format(prompt))
            sys.stdout.flush()
            response = sys.stdin.readline().strip().lower()
            if response == 'yes' or response == 'y':
                return True
            elif response == 'no' or response == 'n':
                return False
            else:
                emitter.publish(
                    "'{}' is not a valid response.".format(response))


def _install(package_name, package_version, options_path, app_id, cli, app,
             yes):
    """Install the specified package.

    :param package_name: the package to install
    :type package_name: str
    :param package_version: package version to install
    :type package_version: str
    :param options_path: path to file containing option values
    :type options_path: str
    :param app_id: app ID for installation of this package
    :type app_id: str
    :param cli: indicates if the cli should be installed
    :type cli: bool
    :param app: indicate if the application should be installed
    :type app: bool
    :param yes: automatically assume yes to all prompts
    :type yes: bool
    :returns: process status
    :rtype: int
    """

    if cli is False and app is False:
        # Install both if neither flag is specified
        cli = app = True

    config = util.get_config()

    pkg = package.resolve_package(package_name, config)
    if pkg is None:
        msg = "Package [{}] not found\n".format(package_name) + \
              "You may need to run 'dcos package update' to update your " + \
              "repositories"
        raise DCOSException(msg)

    pkg_revision = pkg.latest_package_revision(package_version)
    if pkg_revision is None:
        if package_version is not None:
            msg = "Version {} of package [{}] is not available".format(
                package_version, package_name)
        else:
            msg = "Package [{}] not available".format(package_name)
        raise DCOSException(msg)

    user_options = _user_options(options_path)

    pkg_json = pkg.package_json(pkg_revision)
    pre_install_notes = pkg_json.get('preInstallNotes')
    if pre_install_notes:
        emitter.publish(pre_install_notes)
        if not _confirm('Continue installing?', yes):
            emitter.publish('Exiting installation.')
            return 0

    options = pkg.options(pkg_revision, user_options)

    revision_map = pkg.package_revisions_map()
    package_version = revision_map.get(pkg_revision)

    if app and pkg.has_marathon_definition(pkg_revision):
        # Install in Marathon
        msg = 'Installing Marathon app for package [{}] version [{}]'.format(
            pkg.name(), package_version)
        if app_id is not None:
            msg += ' with app id [{}]'.format(app_id)

        emitter.publish(msg)

        init_client = marathon.create_client(config)

        package.install_app(
            pkg,
            pkg_revision,
            init_client,
            options,
            app_id)

    if cli and pkg.has_command_definition(pkg_revision):
        # Install subcommand
        msg = 'Installing CLI subcommand for package [{}] version [{}]'.format(
            pkg.name(), package_version)
        emitter.publish(msg)

        subcommand.install(pkg, pkg_revision, options)

        subcommand_paths = subcommand.get_package_commands(package_name)
        new_commands = [os.path.basename(p).replace('-', ' ', 1)
                        for p in subcommand_paths]

        if new_commands:
            commands = ', '.join(new_commands)
            plural = "s" if len(new_commands) > 1 else ""
            emitter.publish("New command{} available: {}".format(plural,
                                                                 commands))

    post_install_notes = pkg_json.get('postInstallNotes')
    if post_install_notes:
        emitter.publish(post_install_notes)

    return 0


def _list(json_, endpoints, app_id, package_name):
    """List installed apps

    :param json_: output json if True
    :type json_: bool
    :param endpoints: Whether to include a list of
        endpoints as port-host pairs
    :type endpoints: boolean
    :param app_id: App ID of app to show
    :type app_id: str
    :param package_name: The package to show
    :type package_name: str
    :returns: process return code
    :rtype: int
    """

    config = util.get_config()
    init_client = marathon.create_client(config)
    installed = package.installed_packages(init_client, endpoints)

    # only emit those packages that match the provided package_name and app_id
    results = []
    for pkg in installed:
        pkg_info = pkg.dict()
        if (_matches_package_name(package_name, pkg_info) and
                _matches_app_id(app_id, pkg_info)):
            if app_id:
                # if the user is asking a specific id then only show that id
                pkg_info['apps'] = [
                    app for app in pkg_info['apps']
                    if app == app_id
                ]

            results.append(pkg_info)

    if results or json_:
        emitting.publish_table(emitter, results, tables.package_table, json_)
    else:
        msg = ("There are currently no installed packages. "
               "Please use `dcos package install` to install a package.")
        raise DCOSException(msg)
    return 0


def _matches_package_name(name, pkg_info):
    """
    :param name: the name of the package
    :type name: str
    :param pkg_info: the package description
    :type pkg_info: dict
    :returns: True if the name is not defined or the package matches that name;
              False otherwise
    :rtype: bool
    """

    return name is None or pkg_info['name'] == name


def _matches_app_id(app_id, pkg_info):
    """
    :param app_id: the application id
    :type app_id: str
    :param pkg_info: the package description
    :type pkg_info: dict
    :returns: True if the app id is not defined or the package matches that app
              id; False otherwize
    :rtype: bool
    """

    return app_id is None or app_id in pkg_info.get('apps')


def _search(json_, query):
    """Search for matching packages.

    :param json_: output json if True
    :type json_: bool
    :param query: The search term
    :type query: str
    :returns: Process status
    :rtype: int
    """
    if not query:
        query = ''

    config = util.get_config()
    results = [index_entry.as_dict()
               for index_entry in package.search(query, config)]

    if any(result['packages'] for result in results) or json_:
        emitting.publish_table(emitter,
                               results,
                               tables.package_search_table,
                               json_)
    else:
        raise DCOSException('No packages found.')
    return 0


def _uninstall(package_name, remove_all, app_id, cli, app):
    """Uninstall the specified package.

    :param package_name: The package to uninstall
    :type package_name: str
    :param remove_all: Whether to remove all instances of the named package
    :type remove_all: boolean
    :param app_id: App ID of the package instance to uninstall
    :type app_id: str
    :returns: Process status
    :rtype: int
    """

    err = package.uninstall(package_name, remove_all, app_id, cli, app)
    if err is not None:
        emitter.publish(err)
        return 1

    return 0


def _bundle(package_directory, output_directory):
    """
    :param package_directory: directory containing the package
    :type package_directory: str
    :param output_directory: directory where to save the package zip file
    :type output_directory: str
    :returns: process status
    :rtype: int
    """

    if output_directory is None:
        output_directory = os.getcwd()
    logger.debug('Using [%s] as the ouput directory', output_directory)

    # Find package.json file and parse it
    if not os.path.exists(os.path.join(package_directory, 'package.json')):
        raise DCOSException(
            ('The file package.json is required in the package directory '
             '[{}]').format(package_directory))

    package_json = _validate_json_file(
        os.path.join(package_directory, 'package.json'))

    with tempfile.NamedTemporaryFile() as temp_file:
        with zipfile.ZipFile(
                temp_file.name,
                mode='w',
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True) as zip_file:
            # list through package directory and add files zip archive
            for filename in sorted(os.listdir(package_directory)):
                fullpath = os.path.join(package_directory, filename)
                if filename == 'marathon.json.mustache':
                    zip_file.write(fullpath, arcname=filename)
                elif filename in ['config.json', 'command.json',
                                  'package.json']:
                    # schema check the config and command json file
                    _validate_json_file(fullpath)
                    zip_file.write(fullpath, arcname=filename)
                elif filename == 'assets' and os.path.isdir(fullpath):
                    _bundle_assets(fullpath, zip_file)
                elif filename == 'images' and os.path.isdir(fullpath):
                    _bundle_images(fullpath, zip_file)
                else:
                    # anything else is an error
                    raise DCOSException(
                        ('Error bundling package. Extra file in package '
                         'directory [{}]').format(fullpath))

        # Compute the name of the package file
        zip_file_name = os.path.join(
            output_directory,
            '{}-{}-{}.zip'.format(
                package_json['name'],
                package_json['version'],
                _hashfile(temp_file.name)))

        if os.path.exists(zip_file_name):
            raise DCOSException(
                'Output file [{}] already exists'.format(
                    zip_file_name))

        # rename with digest
        util.sh_copy(temp_file.name, zip_file_name)

    # Print the full path to the file
    emitter.publish(
        errors.DefaultError(
            'Created DCOS Universe package [{}].'.format(zip_file_name)))

    return 0


def _validate_json_file(fullpath):
    """Validates the content of the file against its schema. Throws an
    exception if the file is not valid.

    :param fullpath: full path to the file.
    :type fullpath: str
    :return: json object if it is a special file
    :rtype: dict
    """

    filename = os.path.basename(fullpath)
    if filename in ['command.json', 'config.json', 'package.json']:
        schema_path = 'data/universe-schema/{}'.format(filename)
    else:
        raise DCOSException(
            ('Error bundling package. Unknown file in package '
             'directory [{}]').format(fullpath))

    special_schema = util.load_jsons(
        pkg_resources.resource_string('dcoscli', schema_path).decode('utf-8'))

    with util.open_file(fullpath) as special_file:
        special_json = util.load_json(special_file)

    errs = util.validate_json(special_json, special_schema)
    if errs:
        emitter.publish(
            errors.DefaultError(
                'Error validating JSON file [{}]'.format(fullpath)))
        raise DCOSException(util.list_to_err(errs))

    return special_json


def _hashfile(filename):
    """Calculates the sha256 of a file

    :param filename: path to the file to sum
    :type filename: str
    :returns: digest in hexadecimal
    :rtype: str
    """

    hasher = hashlib.sha256()
    with open(filename, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def _bundle_assets(assets_directory, zip_file):
    """Bundle the assets directory

    :param assets_directory: path to the assets directory
    :type assets_directory: str
    :param zip_file: zip file object
    :type zip_file: zipfile.ZipFile
    :rtype: None
    """

    for filename in sorted(os.listdir(assets_directory)):
        fullpath = os.path.join(assets_directory, filename)
        if filename == 'uris' and os.path.isdir(fullpath):
            _bundle_uris(fullpath, zip_file)
        else:
            # anything else is an error
            raise DCOSException(
                ('Error bundling package. Extra file in package '
                 'directory [{}]').format(fullpath))


def _bundle_uris(uris_directory, zip_file):
    """Bundle the uris directory

    :param uris_directory: path to the uris directory
    :type uris_directory: str
    :param zip_file: zip file object
    :type zip_file: zipfile.ZipFile
    :rtype: None
    """

    for filename in sorted(os.listdir(uris_directory)):
        fullpath = os.path.join(uris_directory, filename)

        zip_file.write(fullpath, arcname='assets/uris/{}'.format(filename))


def _bundle_images(images_directory, zip_file):
    """Bundle the images directory

    :param images_directory: path to the images directory
    :type images_directory: str
    :param zip_file: zip file object
    :type zip_file: zipfile.ZipFile
    :rtype: None
    """

    for filename in sorted(os.listdir(images_directory)):
        fullpath = os.path.join(images_directory, filename)
        if (filename == 'icon-small.png' or
                filename == 'icon-medium.png' or
                filename == 'icon-large.png'):

            util.validate_png(fullpath)

            zip_file.write(fullpath, arcname='images/{}'.format(filename))
        elif filename == 'screenshots' and os.path.isdir(fullpath):
            _bundle_screenshots(fullpath, zip_file)
        else:
            # anything else is an error
            raise DCOSException(
                ('Error bundling package. Extra file in package '
                 'directory [{}]').format(fullpath))


def _bundle_screenshots(screenshot_directory, zip_file):
    """Bundle the screenshots directory

    :param screenshot_directory: path to the screenshots directory
    :type screenshot_directory: str
    :param zip_file: zip file object
    :type zip_file: zipfile.ZipFile
    :rtype: None
    """

    for filename in sorted(os.listdir(screenshot_directory)):
        fullpath = os.path.join(screenshot_directory, filename)

        util.validate_png(fullpath)

        zip_file.write(
            fullpath,
            arcname='images/screenshots/{}'.format(filename))
