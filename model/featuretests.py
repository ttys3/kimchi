#
# Project Kimchi
#
# Copyright IBM Corp, 2015-2016
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
import platform
import subprocess

import cherrypy
import libvirt
import lxml.etree as ET
from lxml.builder import E
from wok.utils import run_command
from wok.utils import servermethod
from wok.utils import wok_log


FEATURETEST_VM_NAME = 'FEATURETEST_VM'
FEATURETEST_POOL_NAME = 'FEATURETEST_POOL'

ISO_STREAM_XML = """
<domain type='%(domain)s'>
  <name>%(name)s</name>
  <memory unit='MiB'>256</memory>
  <os>
    <type arch='%(arch)s'>hvm</type>
    <boot dev='cdrom'/>
  </os>
  <devices>
    <disk type='network' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source protocol='%(protocol)s' name='/url/path/to/iso/file'>
        <host name='host.name' port='1234'/>
      </source>
      <target dev='hdc' bus='ide'/>
      <readonly/>
      <alias name='ide0-1-0'/>
      <address type='drive' controller='0' bus='1' target='0' unit='0'/>
    </disk>
  </devices>
</domain>"""

SIMPLE_VM_XML = """
<domain type='%(domain)s'>
  <name>%(name)s</name>
  <memory unit='MiB'>256</memory>
  <os>
    <type arch='%(arch)s'>hvm</type>
    <boot dev='hd'/>
  </os>
</domain>"""

MAXMEM_VM_XML = """
<domain type='%(domain)s'>
  <name>%(name)s</name>
  <maxMemory slots='1' unit='MiB'>512</maxMemory>
  <memory unit='MiB'>256</memory>
  <os>
    <type arch='%(arch)s'>hvm</type>
    <boot dev='hd'/>
  </os>
  <cpu>
    <numa>
      <cell id='0' cpus='0' memory='256' unit='MiB'/>
    </numa>
  </cpu>
  <features>
    <acpi/>
  </features>
</domain>"""

DEV_MEM_XML = """
<memory model='dimm'>
  <target>
    <size unit='MiB'>256</size>
    <node>0</node>
  </target>
</memory>"""

SCSI_FC_XML = """
<pool type='scsi'>
  <name>%(name)s</name>
  <source>
    <adapter type='fc_host' wwnn='1234567890abcdef' wwpn='abcdef1234567890'/>
  </source>
  <target>
    <path>/dev/disk/by-path</path>
  </target>
</pool>
"""


class FeatureTests(object):
    @staticmethod
    def disable_libvirt_error_logging():
        def libvirt_errorhandler(userdata, error):
            # A libvirt error handler to ignore annoying messages in stderr
            pass

        # Filter functions are enable only in production env
        if cherrypy.config.get('environment') != 'production':
            return
        # Register the error handler to hide libvirt error in stderr
        libvirt.registerErrorHandler(f=libvirt_errorhandler, ctx=None)

    @staticmethod
    def enable_libvirt_error_logging():
        # Filter functions are enable only in production env
        if cherrypy.config.get('environment') != 'production':
            return
        # Unregister the error handler
        libvirt.registerErrorHandler(f=None, ctx=None)

    @staticmethod
    def libvirt_supports_iso_stream(conn, protocol):
        conn_type = conn.getType().lower()
        domain_type = 'test' if conn_type == 'test' else 'kvm'
        arch = 'i686' if conn_type == 'test' else platform.machine()
        arch = 'ppc64' if arch == 'ppc64le' else arch
        xml = ISO_STREAM_XML % {
            'name': FEATURETEST_VM_NAME,
            'domain': domain_type,
            'protocol': protocol,
            'arch': arch,
        }
        try:
            FeatureTests.disable_libvirt_error_logging()
            dom = conn.defineXML(xml)
            dom.undefine()
            return True
        except libvirt.libvirtError as e:
            wok_log.error(str(e))
            return False
        finally:
            FeatureTests.enable_libvirt_error_logging()

    @staticmethod
    def libvirt_support_nfs_probe(conn):
        def _get_xml():
            obj = E.source(E.host(name='127.0.0.1'), E.format(type='nfs'))
            xml = ET.tostring(obj, encoding='unicode')
            return xml

        try:
            FeatureTests.disable_libvirt_error_logging()
            conn.findStoragePoolSources('netfs', _get_xml(), 0)
        except libvirt.libvirtError as e:
            wok_log.error(str(e))
            if e.get_error_code() == 38:
                # if libvirt cannot find showmount,
                # it returns 38--general system call failure
                return False
        finally:
            FeatureTests.enable_libvirt_error_logging()

        return True

    @staticmethod
    @servermethod
    def qemu_supports_iso_stream():
        host = cherrypy.server.socket_host
        port = cherrypy.server.socket_port
        cmd = (
            "qemu-io -r http://%s:%d/plugins/kimchi/images/icon-fedora.png \
              -c 'read -v 0 512'"
            % (host, port)
        )
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
        )
        stdout, stderr = proc.communicate()
        return len(stderr) == 0

    @staticmethod
    def libvirt_support_fc_host(conn):
        pool = None
        try:
            FeatureTests.disable_libvirt_error_logging()
            pool_xml = SCSI_FC_XML % {'name': FEATURETEST_POOL_NAME}
            pool = conn.storagePoolDefineXML(pool_xml, 0)
        except libvirt.libvirtError as e:
            if e.get_error_code() == 27:
                # Libvirt requires adapter name, not needed when supports to FC
                return False
        finally:
            FeatureTests.enable_libvirt_error_logging()
            pool is None or pool.undefine()
        return True

    @staticmethod
    def kernel_support_vfio():
        out, err, rc = run_command(['modprobe', 'vfio-pci'])
        if rc != 0:
            wok_log.warning('Unable to load Kernal module vfio-pci.')
            return False
        return True

    @staticmethod
    def is_nm_running():
        """Tries to determine whether NetworkManager is running."""

        out, err, rc = run_command(['nmcli', 'dev', 'status'])
        if rc != 0:
            return False

        return True

    @staticmethod
    def has_mem_hotplug_support(conn):
        """
        A memory device can be hot-plugged or hot-unplugged since libvirt
        version 1.2.14.
        """
        # Libvirt < 1.2.14 does not support memory devices, so try to attach a
        # device. Then check if QEMU (>= 2.1) supports memory hotplug, starting
        # the guest These steps avoid errors with Libvirt 'test' driver for KVM
        conn_type = conn.getType().lower()
        domain_type = 'test' if conn_type == 'test' else 'kvm'
        arch = 'i686' if conn_type == 'test' else platform.machine()
        arch = 'ppc64' if arch == 'ppc64le' else arch

        dom = None
        try:
            FeatureTests.disable_libvirt_error_logging()
            dom = conn.defineXML(
                MAXMEM_VM_XML
                % {'name': FEATURETEST_VM_NAME, 'domain': domain_type, 'arch': arch}
            )
            dom.attachDeviceFlags(DEV_MEM_XML, libvirt.VIR_DOMAIN_MEM_CONFIG)
            dom.create()
        except libvirt.libvirtError:
            return False
        finally:
            if dom and dom.isActive() == 1:
                dom.destroy()
            dom is None or dom.undefine()
            FeatureTests.enable_libvirt_error_logging()
        return True
