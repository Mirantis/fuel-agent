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

import os
import shutil
import signal
import tempfile
import yaml

from oslo_config import cfg

from fuel_agent import errors
from fuel_agent.openstack.common import log as logging
from fuel_agent.utils import artifact as au
from fuel_agent.utils import build as bu
from fuel_agent.utils import fs as fu
from fuel_agent.utils import grub as gu
from fuel_agent.utils import lvm as lu
from fuel_agent.utils import md as mu
from fuel_agent.utils import partition as pu
from fuel_agent.utils import utils

from fuel_agent.actions.base import BaseActionDriver


CONF = cfg.CONF

LOG = logging.getLogger(__name__)


class PartitioningAction(BaseActionDriver):

    def __init__(self, data):
        self.datadriver = utils.get_driver(CONF.data_driver)(data)

    def run(self):
        self.do_partitioning()

    def do_get_fstab(self):

        def get_device_uuid(device):
            return utils.execute(
                'blkid', '-o', 'value', '-s', 'UUID', device,
                check_exit_code=[0])
            [0].strip()

        fstab = ""
        for fs in self.driver.partition_scheme.fss:
            uuid = get_device_uuid(fs.device)
            options = (
                "defaults",
            )
            if fs.mount == '/':
                options.append('errors=panic')

            print("UUID={uuid} {mount} {type} {options} 0 0".format(
                uuid=uuid,
                mount=fs.mount,
                type=fs.type,
                options=','.join(options)
            ))
        print(fstab)

    def do_clean_filesystems(self):
        # NOTE(agordeev): it turns out that only mkfs.xfs needs '-f' flag in
        # order to force recreation of filesystem.
        # This option will be added to mkfs.xfs call explicitly in fs utils.
        # TODO(asvechnikov): need to refactor processing keep_flag logic when
        # data model will become flat
        for fs in self.datadriver.partition_scheme.fss:
            found_images = [img for img in self.datadriver.image_scheme.images
                            if img.target_device == fs.device]

            if not fs.keep_data and not found_images:
                fu.make_fs(fs.type, fs.options, fs.label, fs.device)

    def do_partitioning(self):
        LOG.debug('--- Partitioning disks (do_partitioning) ---')

        if self.datadriver.partition_scheme.skip_partitioning:
            LOG.debug('Some of fs has keep_data flag, '
                      'partitioning is skiping')
            self.do_clean_filesystems()
            return

        # If disks are not wiped out at all, it is likely they contain lvm
        # and md metadata which will prevent re-creating a partition table
        # with 'device is busy' error.
        mu.mdclean_all()
        lu.lvremove_all()
        lu.vgremove_all()
        lu.pvremove_all()

        LOG.debug("Enabling udev's rules blacklisting")
        utils.blacklist_udev_rules(udev_rules_dir=CONF.udev_rules_dir,
                                   udev_rules_lib_dir=CONF.udev_rules_lib_dir,
                                   udev_rename_substr=CONF.udev_rename_substr,
                                   udev_empty_rule=CONF.udev_empty_rule)

        for parted in self.datadriver.partition_scheme.parteds:
            for prt in parted.partitions:
                # We wipe out the beginning of every new partition
                # right after creating it. It allows us to avoid possible
                # interactive dialog if some data (metadata or file system)
                # present on this new partition and it also allows udev not
                # hanging trying to parse this data.
                utils.execute('dd', 'if=/dev/zero', 'bs=1M',
                              'seek=%s' % max(prt.begin - 3, 0), 'count=5',
                              'of=%s' % prt.device, check_exit_code=[0])
                # Also wipe out the ending of every new partition.
                # Different versions of md stores metadata in different places.
                # Adding exit code 1 to be accepted as for handling situation
                # when 'no space left on device' occurs.
                utils.execute('dd', 'if=/dev/zero', 'bs=1M',
                              'seek=%s' % max(prt.end - 3, 0), 'count=5',
                              'of=%s' % prt.device, check_exit_code=[0, 1])

        for parted in self.datadriver.partition_scheme.parteds:
            pu.make_label(parted.name, parted.label)
            for prt in parted.partitions:
                pu.make_partition(prt.device, prt.begin, prt.end, prt.type)
                for flag in prt.flags:
                    pu.set_partition_flag(prt.device, prt.count, flag)
                if prt.guid:
                    pu.set_gpt_type(prt.device, prt.count, prt.guid)
                # If any partition to be created doesn't exist it's an error.
                # Probably it's again 'device or resource busy' issue.
                if not os.path.exists(prt.name):
                    raise errors.PartitionNotFoundError(
                        'Partition %s not found after creation' % prt.name)

        LOG.debug("Disabling udev's rules blacklisting")
        utils.unblacklist_udev_rules(
            udev_rules_dir=CONF.udev_rules_dir,
            udev_rename_substr=CONF.udev_rename_substr)

        # If one creates partitions with the same boundaries as last time,
        # there might be md and lvm metadata on those partitions. To prevent
        # failing of creating md and lvm devices we need to make sure
        # unused metadata are wiped out.
        mu.mdclean_all()
        lu.lvremove_all()
        lu.vgremove_all()
        lu.pvremove_all()

        # creating meta disks
        for md in self.datadriver.partition_scheme.mds:
            mu.mdcreate(md.name, md.level, md.devices, md.metadata)

        # creating physical volumes
        for pv in self.datadriver.partition_scheme.pvs:
            lu.pvcreate(pv.name, metadatasize=pv.metadatasize,
                        metadatacopies=pv.metadatacopies)

        # creating volume groups
        for vg in self.datadriver.partition_scheme.vgs:
            lu.vgcreate(vg.name, *vg.pvnames)

        # creating logical volumes
        for lv in self.datadriver.partition_scheme.lvs:
            lu.lvcreate(lv.vgname, lv.name, lv.size)

        # making file systems
        for fs in self.datadriver.partition_scheme.fss:
            found_images = [img for img in self.datadriver.image_scheme.images
                            if img.target_device == fs.device]
            if not found_images:
                fu.make_fs(fs.type, fs.options, fs.label, fs.device)

        self.do_get_fstab()
