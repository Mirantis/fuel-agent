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

import copy

import itertools
import math
import os

import six
from six.moves.urllib.parse import urljoin
from six.moves.urllib.parse import urlparse
from six.moves.urllib.parse import urlsplit
import yaml

from fuel_agent.drivers import ks_spaces_validator
from fuel_agent import errors
from fuel_agent import objects
from fuel_agent.openstack.common import log as logging
from fuel_agent.utils import hardware as hu
from fuel_agent.utils import utils
from fuel_agent.drivers.base import BaseConfigDriveDriver


LOG = logging.getLogger(__name__)


class NailgunConfigDriveDriver(BaseConfigDriveDriver):

    def __init__(self, data):
        self.data = copy.deepcopy(data)

    @property
    def configdrive_scheme(self):
        if not hasattr(self, '_configdrive_scheme'):
            self._configdrive_scheme = self._parse_configdrive_scheme()
        return self._configdrive_scheme

    def _parse_configdrive_scheme(self):
        LOG.debug('--- Preparing configdrive scheme ---')
        data = self.data
        configdrive_scheme = objects.ConfigDriveScheme()

        LOG.debug('Adding common parameters')
        admin_interface = filter(
            lambda x: (x['mac_address'] ==
                       data['kernel_options']['netcfg/choose_interface']),
            [dict(name=name, **spec) for name, spec
             in data['interfaces'].iteritems()])[0]

        ssh_auth_keys = data['ks_meta']['authorized_keys']
        if data['ks_meta']['auth_key']:
            ssh_auth_keys.append(data['ks_meta']['auth_key'])

        configdrive_scheme.set_common(
            ssh_auth_keys=ssh_auth_keys,
            hostname=data['hostname'],
            fqdn=data['hostname'],
            name_servers=data['name_servers'],
            search_domain=data['name_servers_search'],
            master_ip=data['ks_meta']['master_ip'],
            master_url='http://%s:8000/api' % data['ks_meta']['master_ip'],
            udevrules=data['kernel_options']['udevrules'],
            admin_mac=data['kernel_options']['netcfg/choose_interface'],
            admin_ip=admin_interface['ip_address'],
            admin_mask=admin_interface['netmask'],
            admin_iface_name=admin_interface['name'],
            timezone=data['ks_meta'].get('timezone', 'America/Los_Angeles'),
            gw=data['ks_meta']['gw'],
            ks_repos=data['ks_meta']['repo_setup']['repos']
        )

        LOG.debug('Adding puppet parameters')
        configdrive_scheme.set_puppet(
            master=data['ks_meta']['puppet_master'],
            enable=data['ks_meta']['puppet_enable']
        )

        LOG.debug('Adding mcollective parameters')
        configdrive_scheme.set_mcollective(
            pskey=data['ks_meta']['mco_pskey'],
            vhost=data['ks_meta']['mco_vhost'],
            host=data['ks_meta']['mco_host'],
            user=data['ks_meta']['mco_user'],
            password=data['ks_meta']['mco_password'],
            connector=data['ks_meta']['mco_connector'],
            enable=data['ks_meta']['mco_enable']
        )

        LOG.debug('Setting configdrive profile %s' % data['profile'])
        configdrive_scheme.set_profile(profile=data['profile'])
        configdrive_scheme.set_cloud_init_templates(
            templates=data['ks_meta'].get('cloud_init_templates', {}))
        return configdrive_scheme
