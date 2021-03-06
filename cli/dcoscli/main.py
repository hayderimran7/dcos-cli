"""
Command line utility for the Mesosphere Datacenter Operating
System (DCOS)

'dcos help' lists all available subcommands. See 'dcos <command> --help'
to read about a specific subcommand.

Usage:
    dcos [options] [<command>] [<args>...]

Options:
    --help                      Show this screen
    --version                   Show version
    --log-level=<log-level>     If set then print supplementary messages to
                                stderr at or above this level. The severity
                                levels in the order of severity are: debug,
                                info, warning, error, and critical. E.g.
                                Setting the option to warning will print
                                warning, error and critical messages to stderr.
                                Note: that this does not affect the output sent
                                to stdout by the command.
    --debug                     If set then enable further debug messages which
                                are sent to stdout.

Environment Variables:
    DCOS_LOG_LEVEL              If set then it specifies that message should be
                                printed to stderr at or above this level. See
                                the --log-level option for details.

    DCOS_CONFIG                 This environment variable points to the
                                location of the DCOS configuration file.
                                [default: ~/.dcos/dcos.toml]

    DCOS_DEBUG                  If set then enable further debug messages which
                                are sent to stdout.

    DCOS_SSL_VERIFY             If set, specifies whether to verify SSL certs
                                for HTTPS, or the path to the certificate(s).
                                Can also be configured by setting
                                `core.ssl_config` in the config.
"""

import os
import signal
import sys
from functools import wraps
from subprocess import PIPE, Popen

import dcoscli
import docopt
from dcos import auth, constants, emitting, errors, http, subcommand, util
from dcos.errors import DCOSException
from dcoscli import analytics

emitter = emitting.FlatEmitter()


def main():
    try:
        return _main()
    except DCOSException as e:
        emitter.publish(e)
        return 1


def _main():
    signal.signal(signal.SIGINT, signal_handler)

    args = docopt.docopt(
        __doc__,
        version='dcos version {}'.format(dcoscli.version),
        options_first=True)

    log_level = args['--log-level']
    if log_level and not _config_log_level_environ(log_level):
        return 1

    if args['--debug']:
        os.environ[constants.DCOS_DEBUG_ENV] = 'true'

    util.configure_process_from_environ()

    if args['<command>'] != 'config' and \
       not auth.check_if_user_authenticated():
        auth.force_auth()

    config = util.get_config()
    set_ssl_info_env_vars(config)

    command = args['<command>']
    http.silence_requests_warnings()

    if not command:
        command = "help"

    executable = subcommand.command_executables(command)

    subproc = Popen([executable,  command] + args['<args>'],
                    stderr=PIPE)
    if dcoscli.version != 'SNAPSHOT':
        return analytics.wait_and_track(subproc)
    else:
        return analytics.wait_and_capture(subproc)[0]


def _config_log_level_environ(log_level):
    """
    :param log_level: Log level to set
    :type log_level: str
    :returns: True if the log level was configured correctly; False otherwise.
    :rtype: bool
    """

    log_level = log_level.lower()

    if log_level in constants.VALID_LOG_LEVEL_VALUES:
        os.environ[constants.DCOS_LOG_LEVEL_ENV] = log_level
        return True

    msg = 'Log level set to an unknown value {!r}. Valid values are {!r}'
    emitter.publish(msg.format(log_level, constants.VALID_LOG_LEVEL_VALUES))

    return False


def signal_handler(signal, frame):
    emitter.publish(
        errors.DefaultError("User interrupted command with Ctrl-C"))
    sys.exit(0)


def decorate_docopt_usage(func):
    """Handle DocoptExit exception

    :param func: function
    :type func: function
    :return: wrapped function
    :rtype: function
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
        except docopt.DocoptExit as e:
            emitter.publish("Command not recognized\n")
            emitter.publish(e)
            return 1
        return result
    return wrapper


def set_ssl_info_env_vars(config):
    """Set SSL info from config to environment variable if enviornment
       variable doesn't exist

    :param config: config
    :type config: Toml
    :rtype: None
    """

    if 'core.ssl_verify' in config and (
            not os.environ.get(constants.DCOS_SSL_VERIFY_ENV)):

        os.environ[constants.DCOS_SSL_VERIFY_ENV] = str(
            config['core.ssl_verify'])
