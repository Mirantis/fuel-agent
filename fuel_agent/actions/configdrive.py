# Copyright 2015 Mirantis, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os

from oslo_config import cfg

from fuel_agent.utils import utils
from fuel_agent.actions.base import BaseAction

LOG = logging.getLogger(__name__)

CONF = cfg.CONF

# NOTE:file should be strictly named as 'user-data'
#      the same is for meta-data as well
USER_DATA_PATH = os.path.join(CONF.tmp_path, 'user-data')
METADATA_PATH = os.path.join(CONF.tmp_path, 'meta-data')
CLOUD_CONFIG_PATH = os.path.join(CONF.tmp_path, 'cloud_config.txt')
BOOTHOOK_PATH = os.path.join(CONF.tmp_path, 'boothook.txt')

opts = [
    cfg.StrOpt(
        'cd_user_data_path',
        default=USER_DATA_PATH,
        help='Path to directory with cloud-init user-data',
    ),
    cfg.StrOpt(
        'cd_meta_data_path',
        default=USER_DATA_PATH,
        help='Path to directory with cloud-init meta-data',
    )
]

CONF.register_opts(opts)


class GenerateConfigDriveConfigurationFilesAction(BaseAction):

    def __init__(self, data):
        self.datadriver = utils.get_driver(CONF.data_driver)(data)

    def run(self):
        self.do_generate_cloud_config()

    def do_generate_cloud_config(self, make_iso=False):
        LOG.debug(
            '--- Generating configdrive configs (do_generate_cloud_config) ---'
        )

        tmpl_dir = CONF.nc_template_path
        utils.render_and_save(
            tmpl_dir,
            self.datadriver.configdrive_scheme.template_names('cloud_config'),
            self.datadriver.configdrive_scheme.template_data(),
            CLOUD_CONFIG_PATH
        )
        utils.render_and_save(
            tmpl_dir,
            self.datadriver.configdrive_scheme.template_names('boothook'),
            self.datadriver.configdrive_scheme.template_data(),
            BOOTHOOK_PATH
        )
        utils.render_and_save(
            tmpl_dir,
            self.datadriver.configdrive_scheme.template_names('meta_data'),
            self.datadriver.configdrive_scheme.template_data(),
            METADATA_PATH
        )

        utils.write_mime_multipart(
            USER_DATA_PATH,
            (
                (BOOTHOOK_PATH, 'cloud-boothook'),
                (CLOUD_CONFIG_PATH, 'cloud-config'),
            ))

        if make_iso:
            utils.generate_iso_image(
                CONF.config_drive_path,
                (USER_DATA_PATH, METADATA_PATH)
            )


class CreateConfigDriveAction(BaseAction):

    def __init__(self, data):
        self.datadriver = utils.get_driver(CONF.data_driver)(data)

    def make_iso(self, user_data_path=USER_DATA_PATH,
                 metadata_path=METADATA_PATH):
        utils.generate_iso_image(
            CONF.config_drive_path,
            (user_data_path, metadata_path)
        )
