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
import math

from fuel_agent import errors
from fuel_agent import objects
from fuel_agent.drivers import ks_spaces_validator
from fuel_agent.drivers.base import BasePartitionigDriver
from fuel_agent.openstack.common import log as logging
from fuel_agent.utils import hardware as hu


LOG = logging.getLogger(__name__)


class NailgunPartitioningDriver(BasePartitionigDriver):

    def __init__(self, data):
        self.data = copy.deepcopy(data)
        self._boot_partition_done = False
        self._boot_done = False

    @property
    def partition_scheme(self):
        if not hasattr(self, '_partition_scheme'):
            self._partition_scheme = self._parse_partition_scheme()
        return self._partition_scheme

    @staticmethod
    def match_device(hu_disk, ks_disk):
        """Check if hu_disk and ks_disk are the same device

        Tries to figure out if hu_disk got from hu.list_block_devices
        and ks_spaces_disk given correspond to the same disk device. This
        is the simplified version of hu.match_device

        :param hu_disk: A dict representing disk device how
        it is given by list_block_devices method.
        :param ks_disk: A dict representing disk device according to
         ks_spaces format.

        :returns: True if hu_disk matches ks_spaces_disk else False.
        """
        uspec = hu_disk['uspec']

        # True if at least one by-id link matches ks_disk
        if ('DEVLINKS' in uspec and len(ks_disk.get('extra', [])) > 0
                and any(x.startswith('/dev/disk/by-id') for x in
                        set(uspec['DEVLINKS']) &
                        set(['/dev/%s' % l for l in ks_disk['extra']]))):
            return True

        # True if one of DEVLINKS matches ks_disk id
        if (len(ks_disk.get('extra', [])) == 0
                and 'DEVLINKS' in uspec and 'id' in ks_disk
                and '/dev/%s' % ks_disk['id'] in uspec['DEVLINKS']):
            return True

        return False

    @property
    def small_ks_disks(self):
        """Get those disks which are smaller than 2T"""
        return [d for d in self.ks_disks if d['size'] <= 2097152]

    @property
    def partition_data(self):
        return self.data['ks_meta']['pm_data']['ks_spaces']

    def _get_partition_count(self, name):
        count = 0
        for disk in self.ks_disks:
            count += len([v for v in disk["volumes"]
                          if v.get('name') == name and v['size'] > 0])
        return count

    def _num_ceph_osds(self):
        return self._get_partition_count('ceph')

    def _num_ceph_journals(self):
        return self._get_partition_count('cephjournal')

    @property
    def hu_disks(self):
        """Actual disks which are available on this node

        It is a list of dicts which are formatted other way than
        ks_spaces disks. To match both of those formats use
        _match_device method.
        """
        if not getattr(self, '_hu_disks', None):
            self._hu_disks = hu.list_block_devices(disks=True)
        return self._hu_disks

    @property
    def ks_vgs(self):
        return filter(lambda x: x['type'] == 'vg', self.partition_data)

    def _getlabel(self, label):
        if not label:
            return ''
        # XFS will refuse to format a partition if the
        # disk label is > 12 characters.
        return ' -L {0} '.format(label[:12])

    def _disk_dev(self, ks_disk):
        # first we try to find a device that matches ks_disk
        # comparing by-id and by-path links
        matched = [hu_disk['device'] for hu_disk in self.hu_disks
                   if self.match_device(hu_disk, ks_disk)]
        # if we can not find a device by its by-id and by-path links
        # we try to find a device by its name
        fallback = [hu_disk['device'] for hu_disk in self.hu_disks
                    if '/dev/%s' % ks_disk['name'] == hu_disk['device']]
        found = matched or fallback
        if not found or len(found) > 1:
            raise errors.DiskNotFoundError(
                'Disk not found: %s' % ks_disk['name'])
        return found[0]

    @property
    def ks_disks(self):
        return filter(
            lambda x: x['type'] == 'disk' and x['size'] > 0,
            self.partition_data)

    def _parse_partition_scheme(self):
        LOG.debug('--- Preparing partition scheme ---')
        data = self.partition_data
        ks_spaces_validator.validate(data)
        partition_scheme = objects.PartitionScheme()

        ceph_osds = self._num_ceph_osds()
        journals_left = ceph_osds
        ceph_journals = self._num_ceph_journals()

        LOG.debug('Looping over all disks in provision data')
        for disk in self.ks_disks:
            # skipping disk if there are no volumes with size >0
            # to be allocated on it which are not boot partitions
            if all((
                v["size"] <= 0
                for v in disk["volumes"]
                if v["type"] != "boot" and v.get("mount") != "/boot"
            )):
                continue
            LOG.debug('Processing disk %s' % disk['name'])
            LOG.debug('Adding gpt table on disk %s' % disk['name'])
            parted = partition_scheme.add_parted(
                name=self._disk_dev(disk), label='gpt')
            # we install bootloader on every disk
            LOG.debug('Adding bootloader stage0 on disk %s' % disk['name'])
            parted.install_bootloader = True
            # legacy boot partition
            LOG.debug('Adding bios_grub partition on disk %s: size=24' %
                      disk['name'])
            parted.add_partition(size=24, flags=['bios_grub'])
            # uefi partition (for future use)
            LOG.debug('Adding UEFI partition on disk %s: size=200' %
                      disk['name'])
            parted.add_partition(size=200)

            LOG.debug('Looping over all volumes on disk %s' % disk['name'])
            for volume in disk['volumes']:
                LOG.debug('Processing volume: '
                          'name=%s type=%s size=%s mount=%s vg=%s' %
                          (volume.get('name'), volume.get('type'),
                           volume.get('size'), volume.get('mount'),
                           volume.get('vg')))
                if volume['size'] <= 0:
                    LOG.debug('Volume size is zero. Skipping.')
                    continue

                if volume.get('name') == 'cephjournal':
                    LOG.debug('Volume seems to be a CEPH journal volume. '
                              'Special procedure is supposed to be applied.')
                    # We need to allocate a journal partition for each ceph OSD
                    # Determine the number of journal partitions we need on
                    # each device
                    ratio = int(math.ceil(float(ceph_osds) / ceph_journals))

                    # No more than 10GB will be allocated to a single journal
                    # partition
                    size = volume["size"] / ratio
                    if size > 10240:
                        size = 10240

                    # This will attempt to evenly spread partitions across
                    # multiple devices e.g. 5 osds with 2 journal devices will
                    # create 3 partitions on the first device and 2 on the
                    # second
                    if ratio < journals_left:
                        end = ratio
                    else:
                        end = journals_left

                    for i in range(0, end):
                        journals_left -= 1
                        if volume['type'] == 'partition':
                            LOG.debug('Adding CEPH journal partition on '
                                      'disk %s: size=%s' %
                                      (disk['name'], size))
                            prt = parted.add_partition(size=size)
                            LOG.debug('Partition name: %s' % prt.name)
                            if 'partition_guid' in volume:
                                LOG.debug('Setting partition GUID: %s' %
                                          volume['partition_guid'])
                                prt.set_guid(volume['partition_guid'])
                    continue

                if volume['type'] in ('partition', 'pv', 'raid'):
                    if volume.get('mount') != '/boot':
                        LOG.debug('Adding partition on disk %s: size=%s' %
                                  (disk['name'], volume['size']))
                        prt = parted.add_partition(
                            size=volume['size'],
                            keep_data=volume.get('keep_data', False))
                        LOG.debug('Partition name: %s' % prt.name)

                    elif volume.get('mount') == '/boot' \
                            and not self._boot_partition_done \
                            and 'nvme' not in disk['name'] \
                            and (disk in self.small_ks_disks or
                                 not self.small_ks_disks):
                        # FIXME(agordeev): NVMe drives should be skipped as
                        # accessing such drives during the boot typically
                        # requires using UEFI which is still not supported
                        # by fuel-agent (it always installs BIOS variant of
                        # grub)
                        # * grub bug (http://savannah.gnu.org/bugs/?41883)
                        # NOTE(kozhukalov): On some hardware GRUB is not able
                        # to see disks larger than 2T due to firmware bugs,
                        # so we'd better avoid placing /boot on such
                        # huge disks if it is possible.
                        LOG.debug('Adding /boot partition on disk %s: '
                                  'size=%s', disk['name'], volume['size'])
                        prt = parted.add_partition(
                            size=volume['size'],
                            keep_data=volume.get('keep_data', False))
                        LOG.debug('Partition name: %s', prt.name)
                        self._boot_partition_done = True
                    else:
                        LOG.debug('No need to create partition on disk %s. '
                                  'Skipping.', disk['name'])
                        continue

                if volume['type'] == 'partition':
                    if 'partition_guid' in volume:
                        LOG.debug('Setting partition GUID: %s' %
                                  volume['partition_guid'])
                        prt.set_guid(volume['partition_guid'])

                    if 'mount' in volume and volume['mount'] != 'none':
                        LOG.debug('Adding file system on partition: '
                                  'mount=%s type=%s' %
                                  (volume['mount'],
                                   volume.get('file_system', 'xfs')))
                        partition_scheme.add_fs(
                            device=prt.name, mount=volume['mount'],
                            fs_type=volume.get('file_system', 'xfs'),
                            fs_label=self._getlabel(volume.get('disk_label')))
                        if volume['mount'] == '/boot' and not self._boot_done:
                            self._boot_done = True

                if volume['type'] == 'pv':
                    LOG.debug('Creating pv on partition: pv=%s vg=%s' %
                              (prt.name, volume['vg']))
                    lvm_meta_size = volume.get('lvm_meta_size', 64)
                    # The reason for that is to make sure that
                    # there will be enough space for creating logical volumes.
                    # Default lvm extension size is 4M. Nailgun volume
                    # manager does not care of it and if physical volume size
                    # is 4M * N + 3M and lvm metadata size is 4M * L then only
                    # 4M * (N-L) + 3M of space will be available for
                    # creating logical extensions. So only 4M * (N-L) of space
                    # will be available for logical volumes, while nailgun
                    # volume manager might reguire 4M * (N-L) + 3M
                    # logical volume. Besides, parted aligns partitions
                    # according to its own algorithm and actual partition might
                    # be a bit smaller than integer number of mebibytes.
                    if lvm_meta_size < 10:
                        raise errors.WrongPartitionSchemeError(
                            'Error while creating physical volume: '
                            'lvm metadata size is too small')
                    metadatasize = int(math.floor((lvm_meta_size - 8) / 2))
                    metadatacopies = 2
                    partition_scheme.vg_attach_by_name(
                        pvname=prt.name, vgname=volume['vg'],
                        metadatasize=metadatasize,
                        metadatacopies=metadatacopies)

                if volume['type'] == 'raid':
                    if 'mount' in volume and \
                            volume['mount'] not in ('none', '/boot'):
                        LOG.debug('Attaching partition to RAID '
                                  'by its mount point %s' % volume['mount'])
                        metadata = 'default'
                        if self.have_grub1_by_default:
                            metadata = '0.90'
                        LOG.debug('Going to use MD metadata version {0}. '
                                  'The version was guessed at the data has '
                                  'been given about the operating system.'
                                  .format(metadata))
                        partition_scheme.md_attach_by_mount(
                            device=prt.name, mount=volume['mount'],
                            fs_type=volume.get('file_system', 'xfs'),
                            fs_label=self._getlabel(volume.get('disk_label')),
                            metadata=metadata)

                    if 'mount' in volume and volume['mount'] == '/boot' and \
                            not self._boot_done:
                        LOG.debug('Adding file system on partition: '
                                  'mount=%s type=%s' %
                                  (volume['mount'],
                                   volume.get('file_system', 'ext2')))
                        partition_scheme.add_fs(
                            device=prt.name, mount=volume['mount'],
                            fs_type=volume.get('file_system', 'ext2'),
                            fs_label=self._getlabel(volume.get('disk_label')))
                        self._boot_done = True

            # this partition will be used to put there configdrive image
            if partition_scheme.configdrive_device() is None:
                LOG.debug('Adding configdrive partition on disk %s: size=20' %
                          disk['name'])
                parted.add_partition(size=20, configdrive=True)

        # checking if /boot is created
        if not self._boot_partition_done or not self._boot_done:
            raise errors.WrongPartitionSchemeError(
                '/boot partition has not been created for some reasons')

        LOG.debug('Looping over all volume groups in provision data')
        for vg in self.ks_vgs:
            LOG.debug('Processing vg %s' % vg['id'])
            LOG.debug('Looping over all logical volumes in vg %s' % vg['id'])
            for volume in vg['volumes']:
                LOG.debug('Processing lv %s' % volume['name'])
                if volume['size'] <= 0:
                    LOG.debug('LogicalVolume size is zero. Skipping.')
                    continue

                if volume['type'] == 'lv':
                    LOG.debug('Adding lv to vg %s: name=%s, size=%s' %
                              (vg['id'], volume['name'], volume['size']))
                    lv = partition_scheme.add_lv(name=volume['name'],
                                                 vgname=vg['id'],
                                                 size=volume['size'])

                    if 'mount' in volume and volume['mount'] != 'none':
                        LOG.debug('Adding file system on lv: '
                                  'mount=%s type=%s' %
                                  (volume['mount'],
                                   volume.get('file_system', 'xfs')))
                        partition_scheme.add_fs(
                            device=lv.device_name, mount=volume['mount'],
                            fs_type=volume.get('file_system', 'xfs'),
                            fs_label=self._getlabel(volume.get('disk_label')))

        partition_scheme.elevate_keep_data()
        return partition_scheme
