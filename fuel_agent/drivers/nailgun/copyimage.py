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
from fuel_agent.drivers.base import BaseImageDriver


LOG = logging.getLogger(__name__)


class NailgunCopyImageDriver(BaseImageDriver):

    def __init__(self, data):
        self.data = copy.deepcopy(data)

    @property
    def image_scheme(self):
        if not hasattr(self, '_image_scheme'):
            self._image_scheme = self._parse_image_scheme()
        return self._image_scheme

    @property
    def image_meta(self):
        if not hasattr(self, '_image_meta'):
            self._image_meta = self._parse_image_meta()
        return self._image_meta

    def _parse_image_meta(self):
        LOG.debug('--- Preparing image metadata ---')
        data = self.data
        # FIXME(agordeev): this piece of code for fetching additional image
        # meta data should be factored out of this particular nailgun driver
        # into more common and absract data getter which should be able to deal
        # with various data sources (local file, http(s), etc.) and different
        # data formats ('blob', json, yaml, etc.).
        # So, the manager will combine and manipulate all those multiple data
        # getter instances.
        # Also, the initial data source should be set to sort out chicken/egg
        # problem. Command line option may be useful for such a case.
        # BUG: https://bugs.launchpad.net/fuel/+bug/1430418
        root_uri = data['ks_meta']['image_data']['/']['uri']
        filename = os.path.basename(urlparse(root_uri).path).split('.')[0] + \
            '.yaml'
        metadata_url = urljoin(root_uri, filename)
        try:
            image_meta = yaml.load(
                utils.init_http_request(metadata_url).text)
        except Exception as e:
            LOG.exception(e)
            LOG.debug('Failed to fetch/decode image meta data')
            image_meta = {}
        return image_meta

    def _parse_image_scheme(self):
        LOG.debug('--- Preparing image scheme ---')
        data = self.data
        image_scheme = objects.ImageScheme()
        # We assume for every file system user may provide a separate
        # file system image. For example if partitioning scheme has
        # /, /boot, /var/lib file systems then we will try to get images
        # for all those mount points. Images data are to be defined
        # at provision.json -> ['ks_meta']['image_data']
        LOG.debug('Looping over all images in provision data')
        for mount_point, image_data in six.iteritems(
                data['ks_meta']['image_data']):
            LOG.debug('Adding image for fs %s: uri=%s format=%s container=%s' %
                      (mount_point, image_data['uri'],
                       image_data['format'], image_data['container']))
            iname = os.path.basename(urlparse(image_data['uri']).path)
            imeta = next(itertools.chain(
                (img for img in self.image_meta.get('images', [])
                 if img['container_name'] == iname), [{}]))
            image_scheme.add_image(
                uri=image_data['uri'],
                target_device=self.partition_scheme.fs_by_mount(
                    mount_point).device,
                format=image_data['format'],
                container=image_data['container'],
                size=imeta.get('raw_size'),
                md5=imeta.get('raw_md5'),
            )
        return image_scheme
