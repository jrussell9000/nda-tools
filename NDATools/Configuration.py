from __future__ import absolute_import, with_statement

import configparser
import getpass
import json
import logging
import logging.config
import os
import platform
import time

import keyring
import requests
import yaml
from pkg_resources import resource_filename
from requests import HTTPError

import NDATools
from NDATools import NDA_TOOLS_LOGGING_YML_FILE, Utils
from NDATools.Utils import exit_error, HttpErrorHandlingStrategy

logger = logging.getLogger(__name__)


class LoggingConfiguration:

    def __init__(self):
        pass

    @staticmethod
    def load_config(default_log_directory, verbose=False, log_dir=None):

        with open(NDA_TOOLS_LOGGING_YML_FILE, 'r') as stream:
            config = yaml.load(stream, Loader=yaml.FullLoader)
        if log_dir and os.path.exists(log_dir):
            log_file = os.path.join(log_dir, "debug_log_{}.txt").format(time.strftime("%Y%m%dT%H%M%S"))
        else:
            log_file = os.path.join(default_log_directory, "debug_log_{}.txt").format(time.strftime("%Y%m%dT%H%M%S"))
        config['handlers']['file']['filename'] = log_file
        if verbose:
            config['loggers']['NDATools']['level'] = 'DEBUG'
            config['handlers']['console']['formatter'] = 'detailed'
        logging.config.dictConfig(config)


class ClientConfiguration:
    SERVICE_NAME = 'nda-tools'

    def __init__(self, args):
        self.config = configparser.ConfigParser()
        logger.info('Using configuration file from {}'.format(NDATools.NDA_TOOLS_SETTINGS_CFG_FILE))
        self.config.read(NDATools.NDA_TOOLS_SETTINGS_CFG_FILE)
        self._check_and_fix_missing_options()
        self.validation_api = self.config.get("Endpoints", "validation")
        self.submission_package_api = self.config.get("Endpoints", "submission_package")
        self.submission_api = self.config.get("Endpoints", "submission")
        self.validationtool_api = self.config.get("Endpoints", "validationtool")
        self.package_creation_api = self.config.get("Endpoints", "package_creation")
        self.package_api = self.config.get("Endpoints", "package")
        self.datadictionary_api = self.config.get("Endpoints", "datadictionary")
        self.collection_api = self.config.get("Endpoints", "collection")
        self.user_api = self.config.get("Endpoints", "user")
        self.aws_access_key = self.config.get("User", "access_key")
        self.aws_secret_key = self.config.get("User", "secret_key")
        self.aws_session_token = self.config.get('User', 'session_token')
        self.username = self.config.get("User", "username").lower()

        # options that appear in both vtcmd and downloadcmd
        self.workerThreads = args.workerThreads

        if args.username:
            self.username = args.username
        elif self.username:
            logger.warning("-u/--username argument not provided. Using default value of '%s' which was saved in %s",
                           self.username, NDATools.NDA_TOOLS_SETTINGS_CFG_FILE)
        self.password = None

        is_vtcmd = 'collectionID' in args
        if is_vtcmd:
            self.aws_access_key = args.accessKey
            self.aws_secret_key = args.secretKey
            self.hideProgress = args.hideProgress
            self.force = True if args.force else False
            self.collection_id = args.collectionID
            self.directory_list = args.listDir
            self.manifest_path = args.manifestPath
            self.source_bucket = args.s3Bucket
            self.source_prefix = args.s3Prefix
            self.validation_timeout = args.validation_timeout
            self.title = args.title
            self.description = args.description
            self.scope = args.scope
            self.JSON = args.JSON
            self.skip_local_file_check = args.skipLocalAssocFileCheck
            self.replace_submission = args.replace_submission
        logger.info('proceeding as nda user: {}'.format(self.username))

    def _check_and_fix_missing_options(self):
        default_config = configparser.ConfigParser()
        default_file_path = resource_filename(__name__, 'clientscripts/config/settings.cfg')
        default_config.read(default_file_path)
        change_detected = False
        for section in default_config.sections():
            if section not in self.config.sections():
                logger.debug(f'adding {section} to settings.cfg')
                self.config.add_section(section)
                change_detected = True
            for option in default_config[section]:
                if option not in self.config[section]:
                    logger.debug('[{}][{}] is missing'.format(section, option))
                    self.config.set(section, option, default_config[section][option])
                    change_detected = True
        if change_detected:
            logger.debug(f'updating settings.cfg')
            with open(NDATools.NDA_TOOLS_SETTINGS_CFG_FILE, 'w') as configfile:
                self.config.write(configfile)
        else:
            logger.debug(f'settings.cfg is up to date')

    def read_user_credentials(self, auth_req=True):

        if auth_req:
            while True:
                try:
                    while not self.username:
                        self.username = str(input('Enter your NIMH Data Archives username:')).lower()
                        with open(NDATools.NDA_TOOLS_SETTINGS_CFG_FILE, 'w') as configfile:
                            self.config.set('User', 'username', self.username)
                            self.config.write(configfile)
                    self.password = keyring.get_password(self.SERVICE_NAME, self.username)
                    while not self.password:
                        self.password = getpass.getpass('Enter your NIMH Data Archives password:')
                    while not self.is_valid_nda_credentials():
                        logger.error("The password that was entered for user '%s' is invalid ...", self.username)
                        logger.error('If your username was previously entered incorrectly, you may update it in your '
                                     'settings.cfg located at \n{}'.format(NDATools.NDA_TOOLS_SETTINGS_CFG_FILE))
                        self.password = getpass.getpass('Enter your NIMH Data Archives password:')
                        while self.password == '':
                            self.password = getpass.getpass('Enter your NIMH Data Archives password:')
                    else:
                        keyring.set_password(self.SERVICE_NAME, self.username, self.password)
                        break
                except RuntimeError as e:
                    if platform.system() == 'Linux':
                        print('If there is no backend set up for keyring, you may try\n'
                              'pip install secretstorage --upgrade keyrings.alt')
                    raise e

        # Only ask for access-key/secret-key when needed (which is only when a user is creating a submission
        # and the files are stored in a s3 bucket)
        if hasattr(self, 'source_bucket') and self.source_bucket:
            self.read_aws_credentials()

    def read_aws_credentials(self):
        if not self.aws_access_key:
            self.aws_access_key = getpass.getpass(
                "Enter your aws_access_key (must have read access to the %s bucket): " % self.source_bucket)

        if not self.aws_secret_key:
            self.aws_secret_key = getpass.getpass('Enter your aws_secret_key:')

    def is_valid_nda_credentials(self):
        try:
            # will raise HTTP error 401 if invalid creds
            Utils.get_request(self.user_api, headers={'content-type': 'application/json'},
                              auth=requests.auth.HTTPBasicAuth(self.username, self.password),
                              error_handler=HttpErrorHandlingStrategy.reraise_status)
            return True
        except HTTPError as e:
            if e.response.status_code == 401:
                if 'locked' in e.response.text:
                    # user account is locked
                    tmp = json.loads(e.response.text)
                    logger.error('\nError: %s', tmp['message'])
                    logger.error('\nPlease contact NDAHelp@mail.nih.gov for help in resolving this error')
                    exit_error()
                else:
                    return False
            else:
                logger.error('\nSystemError while checking credentials for user %s', self.username)
                logger.error('\nPlease contact NDAHelp@mail.nih.gov for help in resolving this error')
                exit_error()
