# Copyright 2015 Cisco Systems, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import sys
import time
import datetime

from ucsmsdk.utils.ccoimage import get_ucs_cco_image_list
from ucsmsdk.utils.ccoimage import get_ucs_cco_image

from ucsmsdk.mometa.top.TopSystem import TopSystem
from ucsmsdk.mometa.firmware.FirmwareCatalogue import FirmwareCatalogue
from ucsmsdk.mometa.firmware.FirmwareDownloader import FirmwareDownloader
from ucsmsdk.mometa.firmware.FirmwareDownloader import FirmwareDownloaderConsts
from ucsmsdk.mometa.firmware.FirmwareAck import FirmwareAckConsts


log = logging.getLogger('ucs')

def firmware_available(username, password, mdf_id_list=None, proxy=None):
    images = get_ucs_cco_image_list(username=username, password=password,
                                  mdf_id_list=mdf_id_list, proxy=proxy)

    image_names = [image.image_name for image in images]
    return sorted(image_names)

# #######################################################################################
# Return the list of firmware bundles that have been downloaded on the Fabric Interconnect
def get_firmware_bundles(handle, bundle_type=None):
	filter_str = None
	if bundle_type != None:
		filter_str = '(type, %s, type="eq")' % bundle_type
	bundles = handle.query_classid(
                class_id="FirmwareDistributable", filter_str=filter_str)
	return bundles

# ##########################################################
# Return the image firmware versions given the bundle version
def get_infra_firmware_version(handle, bundle_version,
				image_types = ['system', 'switch-kernel', 'switch-software']):
	bundles = get_firmware_bundles(handle, bundle_type = 'infrastructure-bundle')
	firmware_map = {}
	for image_type in image_types:
		firmware_map[image_type] = { 'image_name' : None, 'version' : None }

	for bundle in bundles:
		#log.debug("Bundle type: %s, version: %s. Bundle version: %s", bundle.type, bundle.version, bundle_version)
		if bundle.type == 'infrastructure-bundle' and bundle.version.startswith(bundle_version):
			dist_images = handle.query_children(in_mo=bundle, class_id="FirmwareDistImage")
			for dist_image in dist_images:
				for image_type in image_types:
					if dist_image.type == image_type:
						firmware_map[image_type]['image_name'] = dist_image.name
						log.debug('Bundle version %s, infra firmware image name: %s', bundle_version, dist_image.name)
			break

	filter_str = None
	for image_type in image_types:
		if firmware_map[image_type]['image_name'] == None:
			raise Exception("Infra image type '%s' version '%s' is not present", image_type, bundle_version)
		else:
			str = '(name, %s, type="eq")' % (firmware_map[image_type]['image_name']) 
			if filter_str == None:
				filter_str = str
			else:
				filter_str = filter_str + " or " + str

	firmware_images = handle.query_classid(
                class_id="FirmwareImage", filter_str=filter_str)
	for firmware_image in firmware_images:
		for image_type in image_types:
			if firmware_image.name == firmware_map[image_type]['image_name']:
				log.debug("Found bundle/image version mapping. Image type: %s, img version: %s, bundle: %s",
						 image_type, firmware_image.version, bundle_version)
				firmware_map[image_type]['version'] = firmware_image.version
	return firmware_map

# #######################################################################################
# Returns true if the specified UCS bundle (A, B, C...) is present on the FIs
def has_firmware_bundle(handle, version):
    bundles = get_firmware_bundles(handle)
    for bundle in bundles:
        #log.debug("Bundle version %s is available on UCS, want %s", bundle.version, version)
        if bundle.version == version:
            return True
    return False

def firmware_download(image_name, username, password, download_dir,
                      mdf_id_list=None, proxy=None):

    images = get_ucs_cco_image_list(username=username, password=password,
                                    mdf_id_list=mdf_id_list, proxy=proxy)

    image_dict = {}
    for image in images:
        image_dict[image.image_name] = image

    if image_name not in image_dict:
        raise ValueError("Image not available")

    # download image
    image = image_dict[image_name]
    get_ucs_cco_image(image, file_dir=download_dir, proxy=proxy)


def firmware_add_local(handle, image_dir, image_name, timeout=10*60):

    from ucsmsdk.ucseventhandler import UcsEventHandle

    file_path = os.path.join(image_dir, image_name)

    if not os.path.exists(file_path):
        raise IOError("File does not exist")

    top_system = TopSystem()
    firmware_catalogue = FirmwareCatalogue(parent_mo_or_dn=top_system)
    firmware_downloader = FirmwareDownloader(
                                        parent_mo_or_dn=firmware_catalogue,
                                        file_name=image_name)
    firmware_downloader.server = FirmwareDownloaderConsts.PROTOCOL_LOCAL
    firmware_downloader.protocol = FirmwareDownloaderConsts.PROTOCOL_LOCAL
    firmware_downloader.admin_state = \
        FirmwareDownloaderConsts.ADMIN_STATE_RESTART

    uri_suffix = "operations/file-%s/image.txt" % image_name
    handle.file_upload(url_suffix=uri_suffix,
                       file_dir=image_dir,
                       file_name=image_name)

    handle.add_mo(firmware_downloader, modify_present=True)
    handle.set_dump_xml()
    handle.commit()

    start = datetime.datetime.now()
    while not firmware_downloader.transfer_state == FirmwareDownloaderConsts.TRANSFER_STATE_DOWNLOADED:
        firmware_downloader = handle.query_dn(firmware_downloader.dn)
        if firmware_downloader.transfer_state == FirmwareDownloaderConsts.TRANSFER_STATE_FAILED:
            raise Exception("Download of '%s' failed. Error: %s" %
				(image_name, firmware_downloader.fsm_rmt_inv_err_descr))
        if (datetime.datetime.now() - start).total_seconds() > timeout:
            raise Exception("Download of '%s' timed out" % image_name)

    return firmware_downloader


def firmware_add_remote(handle, file_name, remote_path, protocol, server,
                            user="", pwd=""):

    file_path = os.path.join(remote_path, file_name)

    if not os.path.exists(file_path):
        raise IOError("Image does not exist")

    if protocol is not FirmwareDownloaderConsts.PROTOCOL_TFTP:
        if not user:
            raise ValueError("Provide user")
        if not pwd:
            raise ValueError("Provide pwd")

    top_system = TopSystem()
    firmware_catalogue = FirmwareCatalogue(parent_mo_or_dn=top_system)
    firmware_downloader = FirmwareDownloader(
                                        parent_mo_or_dn=firmware_catalogue,
                                        file_name=file_name)
    firmware_downloader.remote_path = remote_path
    firmware_downloader.protocol = protocol
    firmware_downloader.server = server
    firmware_downloader.user = user
    firmware_downloader.pwd = pwd
    firmware_downloader.admin_state = \
        FirmwareDownloaderConsts.ADMIN_STATE_RESTART

    handle.add_mo(firmware_downloader)
    handle.set_dump_xml()
    handle.commit()

def firmware_remove(handle, image_name):

    top_system = TopSystem()
    firmware_catalogue = FirmwareCatalogue(parent_mo_or_dn=top_system)
    firmware_downloader = FirmwareDownloader(
                                        parent_mo_or_dn=firmware_catalogue,
                                        file_name=image_name)

    dn = firmware_downloader.dn
    mo = handle.query_dn(dn)
    if mo is None:
        raise ValueError("Image not available on UCSM.")

    handle.remove_mo(mo)
    handle.set_dump_xml()
    handle.commit()

def validate_connection(handle, timeout=15*60):
	connected = False
	start = datetime.datetime.now()
	while not connected:
		try:
			# If the session is already established, this will validate the session
			connected = handle.login()
		except Exception as e:
			# UCSM may been in the middle of activation, hence connection would fail
			log.debug("Login to UCSM failed: %s", str(e))

		if not connected:
			try:
				log.debug("Login to UCS Manager, elapsed time %ds", (datetime.datetime.now() - start).total_seconds())
				handle.login(force=True)
				log.debug ("Login successful")
				connected = True
			except:
				log.debug("Login failed. Sleeping for 60 seconds")
				time.sleep(60)
			if (datetime.datetime.now() - start).total_seconds() > timeout:
				raise Exception("Unable to login to UCS Manager")
	return connected

def _get_running_firmware_version(handle, version, subject="system"):
	running_firmware_list = []
	mgmt_controllers = handle.query_classid(class_id="MgmtController",
								filter_str='(subject, ' + subject + ', type="eq")')

	if len(mgmt_controllers) == 0:
		raise Exception("No Mgmt Controller Object with subject %s", subject)

	for mgmt_controller in mgmt_controllers:
		list = handle.query_children(in_mo=mgmt_controller,
											 class_id="FirmwareRunning")
		if len(list) == 0:
			raise Exception("No FirmwareRunning Object with subject %s", subject)
		for running_firmware in list:
			running_firmware_list.append(running_firmware)

	return running_firmware_list

# ################################################################
# Returns True if firmware is already running at the specified version
# If not running at the desired version, optionally wait until activation has completed and UCS came back online.
def wait_for_firmware_activation(handle, bundle_version,
						subject,
						image_types,
						wait_for_upgrade_completion,
						acknowledge_reboot,
						timeout,
						observer=None):

	is_running_desired_version = False	
	start = datetime.datetime.now()
	while not is_running_desired_version:
		validate_connection(handle, timeout)

		try:
			is_running_desired_version = True
			running_firmware_list = _get_running_firmware_version(handle, bundle_version, subject)

			firmware_map = get_infra_firmware_version(handle, bundle_version)

			for image_type in image_types:
				found_image_type_match = False
				for running_firmware in running_firmware_list:
					if running_firmware.type == image_type:
						found_image_type_match = True
						expected_version = firmware_map[image_type]['version']
						log.debug("UCS %s is running version %s, expected: %s, bundle: %s",
							running_firmware.dn, running_firmware.version, expected_version, bundle_version)
						if running_firmware.version != expected_version:
							is_running_desired_version = False
				if not found_image_type_match:
					raise Exception("No FirmwareRunning object of type %s", image_type)

			if not is_running_desired_version:
				if not wait_for_upgrade_completion:
					log.debug("UCS %s is not running at desired version", subject)
					break
				else:
					log.debug("UCS %s is not running at desired version. Waiting for activation completion", subject)
					#if observer: observer.fw_observer_cb("UCS %s is not running at desired version. Waiting for activation completion", subject)
					time.sleep(60)

					# Check if there is a pending switch reboot
					firmware_ack = handle.query_dn('sys/fw-system/ack')
					log.debug("Firmware ack: oper_state: %s, scheduler:%s",
						firmware_ack.oper_state, firmware_ack.scheduler)
					if firmware_ack.oper_state == 'waiting-for-user' and acknowledge_reboot:
						log.debug("Acknowledging switch reboot")
						if observer: observer.fw_observer_cb('Acknowledging UCS primary Fabric Interconnect reboot')
						firmware_ack.adminState = FirmwareAckConsts.ADMIN_STATE_TRIGGER_IMMEDIATE
						handle.set_mo(firmware_ack)
						handle.commit()
		except Exception as e:
			# Login session may become invalid during upgrade because UCSM will restart,
			# or FIs will reboot.
			log.exception("Script lost connectivity to UCSM during upgrade. This is expected")
			time.sleep(30)

		if (datetime.datetime.now() - start).total_seconds() > timeout:
			log.warning("UCS %s activation timeout. Elapsed time: %ds",
						subject, (datetime.datetime.now() - start).total_seconds())
			break

	return is_running_desired_version

# ################################################################
# Returns True if UCSM is already running at the specified version
# If not running at the desired version, optionally wait until activation has completed and UCS came back online.
def wait_for_ucsm_activation(handle, version,
						wait_for_upgrade_completion=True,
						timeout=20*60):
	log.debug("Wait for UCSM firmware activation")
	return wait_for_firmware_activation(handle, version, subject="system",
								image_types=['system'],
								wait_for_upgrade_completion=wait_for_upgrade_completion,
								acknowledge_reboot=False,
								timeout=timeout)

# ################################################################
# Returns True if the FIs are already running at the specified version
def wait_for_fi_activation(handle, version,
						wait_for_upgrade_completion=True,
                        timeout=60*60,
						observer=None):
	log.debug("Wait for FI firmware activation")
	return wait_for_firmware_activation(handle, version, subject="switch",
								image_types=['switch-software', 'switch-kernel'],
								wait_for_upgrade_completion=wait_for_upgrade_completion,
								acknowledge_reboot=True,
								timeout=timeout,
								observer=observer)

# ################################################################
def firmware_activate_infra(handle, version="2.2(2c)", require_user_confirmation=True, observer=None):

	infra_bundle_version=version + "A"
	bundle_available = has_firmware_bundle(handle, version=infra_bundle_version)
	if not bundle_available:
		raise Exception("Bundle %s is not available on Fabric Interconnect", infra_bundle_version) 

	if observer: observer.fw_observer_cb('Querying UCS Manager version')
	ucsm_has_desired_version = wait_for_ucsm_activation(handle, version, wait_for_upgrade_completion=False)
	if observer: observer.fw_observer_cb('Querying UCS switch firmware version')
	fis_have_desired_version = wait_for_fi_activation(handle, version, wait_for_upgrade_completion=False)

	need_activation = not ucsm_has_desired_version or not fis_have_desired_version
	if not need_activation:
		log.debug("No infra firmware activation required")
		return
	if require_user_confirmation:
		set_flag = False
		set_str = raw_input("Are you sure want to proceed? This will reboot the "
						"Fabric Interconnects. Enter 'yes' to proceed.")
		if set_str.strip().lower() == "yes":
			set_flag = True

		if not set_flag:
			log.debug("Abort activate firmware version.")
			return

	firmware_infra_pack = handle.query_classid(class_id="FirmwareInfraPack")[0]
	connected = True
	if (firmware_infra_pack.infra_bundle_version != infra_bundle_version):
		firmware_infra_pack.infra_bundle_version = infra_bundle_version

		handle.set_mo(firmware_infra_pack)
		handle.commit()
		if not ucsm_has_desired_version:
			handle.logout()

	if observer: observer.fw_observer_cb('Activating UCS Manager version %s', version)
	ucsm_has_desired_version = wait_for_ucsm_activation(handle, version, wait_for_upgrade_completion=True)

	if ucsm_has_desired_version:
		log.debug("UCS Manager successfully updated to version '%s'" % version)
	else:
		log.debug("UCS Manager not updated to version '%s'" % version)
		raise Exception("UCS Manager not updated to version %s", version)

	if observer: observer.fw_observer_cb('Activating UCS switch firmware version %s', version)
	fis_have_desired_version = wait_for_fi_activation(handle, version, wait_for_upgrade_completion=True, observer=observer)


def firmware_activate_blade(handle, version):

    blade_bundle = version + "B"
    rack_bundle = version + "C"

    blades = handle.query_classid("ComputeBlade")
    for blade in blades:
        mgmt_controllers = handle.query_children(in_mo=blade,
                                                 class_id="MgmtController")
        for mo in mgmt_controllers:
            if mo.subject == "blade":
                mgmt_controller = mo
                break

        firmware_running = handle.query_children(in_mo=mgmt_controller,
                                                 class_id="FirmwareRunning")
        for mo in firmware_running:
            if mo.deployment == "system" and mo.version == version:
                log.debug("Blade <%s> is already at version <%s>" % (blade.dn,
                                                                 version))
                return

        assigned_to_dn = blade.assigned_to_dn
        if not assigned_to_dn:
            host_firmware_pack_dn = "org-root/fw-host-pack-default"
        else:
            # sp_name = re.search(r'^ls-(?P<sp_name>\w+)$',
            #         os.path.basename(assigned_to_dn)).groupdict()['sp_name']
            sp = handle.query_dn(assigned_to_dn)
            host_firmware_pack_dn = sp.oper_host_fw_policy_name

        host_firmware_pack = handle.query_dn(host_firmware_pack_dn)
        host_firmware_pack.blade_bundle_version = blade_bundle

        set_flag = False
        set_str = raw_input("Are you sure want to proceed? This will reboot "
                            "the server.Enter 'yes' to proceed.")
        if set_str.strip().lower() == "yes":
            set_flag = True

        if not set_flag:
            log.debug("Abort update blade firmware.")
            return None

        # handle.set_mo(host_firmware_pack)
        # handle.commit()

        host_firmware_pack_dn = "host_firmware_pack_dn"

        instance_filter = '(type, "instance", type="eq")'
        assoc_filter = '(assoc_state, "associated", type="eq")'
        oper_host_fw_filter = '(oper_host_fw_policy_name, %s, type="eq")' % \
                              host_firmware_pack_dn
        filter_str = instance_filter + " and " + assoc_filter + " and " + \
                     oper_host_fw_filter

        sps = handle.query_classid(class_id="LsServer", filter_str=filter_str)
        for sp in sps:
            dn = sp.dn + '/ack'
            ls_maint_ack = handle.query_dn(dn)
            if ls_maint_ack:
                ls_maint_ack.admin_state = 'trigger-immediate'
                handle.set_mo(ls_maint_ack)
                handle.commit()


def get_firmware_file_names(version, extension="bin"):
	# create version string
	ver_split = version.split('(')
	version_bundle = ver_split[0] + "." + ver_split[1].strip(')')

	# create firmware file name for the respective version
	aseries_bundle = "ucs-k9-bundle-infra." + version_bundle + ".A." + extension
	bseries_bundle = "ucs-k9-bundle-b-series." + version_bundle + ".B." + extension
	cseries_bundle = "ucs-k9-bundle-c-series." + version_bundle + ".C." + extension

	return {"A" : (aseries_bundle, version + "A"),
			"B" : (bseries_bundle, version + "B"),
			"C" : (cseries_bundle, version + "C"),
           }

def firmware_auto_install(handle, version, image_dir, infra_only=False):

    from ucsmsdk.ucseventhandler import UcsEventHandle

    try:
        bundle_map = get_fimrware_file_names(bundle_version)

        bundles = []
        cco_image_list = []

        bundles.append(bundle_map['A'][0])

        if not infra_only:
            bundles.append(bundle_map['B'][0])
            bundles.append(bundle_map['C'][0])

        log.debug("Starting Firmware download process to local directory: %s" %
              image_dir)

        # adding files to cco image list if not available in local directory
        for bundle in bundles:
            file_path = os.path.join(image_dir, bundle)
            if os.path.exists(file_path):
                log.debug("Image already exist in image directory ")
            else:
                cco_image_list.append(bundle)

        # if image not available raising exception to download
        if cco_image_list:
            raise ValueError("Download images %s using firmware_download" %
                             cco_image_list)

        # check if image is already uploaded to ucs domain
        for image in bundles:
            log.debug("Checking if image file: '%s' is already uploaded to UCS "
                  "Domain" % image)

            deleted = False
            filter_str = '(name, %s, type="eq")' % image
            firmware_package = handle.query_classid(
                class_id="FirmwareDistributable", filter_str=filter_str)[0]
            if firmware_package:
                firmware_dist_image = handle.query_children(
                    in_mo=firmware_package, class_id="FirmwareDistImage")[0]
                if firmware_dist_image:
                    if firmware_dist_image.image_deleted != "":
                        deleted = True

            # image does not exist then upload
            if deleted or not firmware_package:
                log.debug("Uploading file to UCSM.")
                firmware = firmware_add_local(handle, image_dir, image)
                eh = UcsEventHandle(handle)
                eh.add(managed_object=firmware, prop="transfer_state",
                       success_value=['downloaded'], poll_sec=30,
                       timeout_sec=600)
                log.debug("Upload of image file '%s' is completed." % image)

            else:
                log.debug("Image file '%s' is already upload available on UCSM" %
                      image)

        # Activate UCSM
        firmware_activate_ucsm(handle, version=version)

        if not infra_only:
            # Activate Blade
            firmware_activate_blade(handle, version=version)
    except:
        log.debug("Error Occurred in Script.")
        handle.logout()
        raise


