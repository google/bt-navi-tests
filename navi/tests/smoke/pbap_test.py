#  Copyright 2025 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Tests for Phone Book Access Profile (PBAP) Server implementation on Android."""

import enum
import json
from typing import Any, TypeAlias, cast

from bumble import core
from bumble import rfcomm
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.utils import resources
from navi.bumble_ext import obex
from navi.bumble_ext import pbap
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import retry

_PBAP_PCE_SDP_RECORD_HANDLE = 1
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_OBEX_PACKET_LENGTH = 8192
_MAX_LIST_COUNT = 500
_PBAP_PHONE_BOOK_TYPE = b'x-bt/phonebook\0'
_SAMPLE_CONTACTS_FILE_PATH = 'navi/tests/smoke/data/contacts.json'
_SAMPLE_CALL_LOGS_FILE_PATH = 'navi/tests/smoke/data/call_logs.json'
_PROPERTY_PBAP_SERVER_ENABLED = 'bluetooth.profile.pbap.server.enabled'
_CONNECT_REQUEST = obex.ConnectRequest(
    obex_version_number=obex.Version.V_1_0,
    flags=0,
    maximum_obex_packet_length=_MAX_OBEX_PACKET_LENGTH,
    final=True,
    headers=obex.Headers(
        target=pbap.TARGET_UUID.bytes,
        app_parameters=pbap.ApplicationParameters(
            pbap_supported_features=pbap.ApplicationParameterValue.SupportedFeatures(
                0x03FF
            )
        ).to_bytes(),
    ),
)

_CallbackHandler: TypeAlias = bl4a_api.CallbackHandler
_Module: TypeAlias = bl4a_api.Module

# Mapping from Android phone type to vCard phone type.
_VCARD_PHONE_TYPES = {
    android_constants.PhoneType.HOME: 'HOME',
    android_constants.PhoneType.MOBILE: 'CELL',
    android_constants.PhoneType.WORK: 'WORK',
}

# Mapping from Android phone type to vCard Email type.
_VCARD_EMAIL_TYPES = {
    android_constants.EmailType.HOME: 'HOME',
    android_constants.EmailType.WORK: 'WORK',
}

# Mapping from vCard call type to Android call type.
_VCARD_CALL_TYPES = {
    'ich': android_constants.CallType.INCOMING,
    'och': android_constants.CallType.OUTGOING,
    'mch': android_constants.CallType.MISSED,
}


def _parse_vcard_list(data: bytes) -> list[dict[str, str]]:
  vcard_list = []
  vcard = dict[str, str]()
  for line in data.split(b'\r\n'):
    if not line:
      continue
    if line.startswith(b'BEGIN:VCARD'):
      vcard = {}
    elif line.startswith(b'END:VCARD'):
      vcard_list.append(vcard)
    else:
      key, value = line.split(b':', maxsplit=1)
      vcard[key.decode('utf-8')] = value.decode('utf-8')
  return vcard_list


class _DisconnectVariant(enum.IntEnum):
  ACL = 1
  BEARER = 2


class PbapTest(navi_test_base.TwoDevicesTestBase):
  contacts: list[dict[str, Any]]
  call_logs: list[dict[str, Any]]

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if self.dut.device.is_emulator:
      self.dut.setprop(_PROPERTY_PBAP_SERVER_ENABLED, 'true')

    if self.dut.getprop(_PROPERTY_PBAP_SERVER_ENABLED) != 'true':
      raise signals.TestAbortClass('PBAP server is not enabled on DUT.')

    await self._setup_paired_devices()

  @retry.retry_on_exception()
  async def _setup_paired_devices(self) -> None:
    self.assertTrue(self.dut.bt.factoryReset())
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      await self.ref.reset()

    self.dut.bt.clearContacts()
    self.dut.bt.clearCallLogs()

    self.contacts = cast(
        list[dict[str, Any]],
        json.loads(resources.GetResource(_SAMPLE_CONTACTS_FILE_PATH, 'r')),
    )
    self.call_logs = cast(
        list[dict[str, Any]],
        json.loads(resources.GetResource(_SAMPLE_CALL_LOGS_FILE_PATH, 'r')),
    )
    self.dut.bt.addContacts(self.contacts)
    self.dut.bt.addCallLogs(self.call_logs)

    self.ref.device.sdp_service_records = {
        _PBAP_PCE_SDP_RECORD_HANDLE: (
            pbap.PceSdpInfo(
                service_record_handle=_PBAP_PCE_SDP_RECORD_HANDLE,
                version=pbap.Version.V_1_1,
            ).to_sdp_records()
        ),
    }
    with self.dut.bl4a.register_callback(_Module.ADAPTER) as dut_cb:
      await self.classic_connect_and_pair()
      self.dut.bt.setPhonebookAccessPermission(
          self.ref.address,
          android_constants.BluetoothAccessPermission.ALLOWED,
      )
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected, lambda e: e.address == self.ref.address
      )

  @override
  @retry.retry_on_exception()
  async def async_setup_test(self) -> None:
    self.assertTrue(self.dut.bt.disable())
    self.assertTrue(self.dut.bt.enable())

  async def _make_pbap_client_from_ref(self) -> obex.ClientSession:
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info('[REF] Connect to DUT.')
      ref_dut_acl = await self.ref.device.connect(
          self.dut.address, transport=core.BT_BR_EDR_TRANSPORT
      )

      self.logger.info('[REF] Authenticate and encrypt.')
      await ref_dut_acl.authenticate()
      await ref_dut_acl.encrypt()

      self.logger.info('[REF] Find SDP record.')
      sdp_info = await pbap.find_pse_sdp_record(ref_dut_acl)
      if not sdp_info:
        self.fail('Failed to find SDP record for pbap.')

      self.logger.info('[REF] Connect RFCOMM.')
      rfcomm_client = await rfcomm.Client(ref_dut_acl).start()
      self.logger.info('[REF] Open DLC to %d.', sdp_info.rfcomm_channel)
      ref_dlc = await rfcomm_client.open_dlc(sdp_info.rfcomm_channel)
      return obex.ClientSession(ref_dlc)

  @navi_test_base.parameterized(
      _DisconnectVariant.ACL,
      _DisconnectVariant.BEARER,
  )
  async def test_connect_disconnect(self, variant: _DisconnectVariant) -> None:
    """Tests connecting and disconnecting PBAP.

    Test Steps:
      1. Connect PBAP from REF to DUT.
      2. Disconnect bearer or ACL from REF.

    Args:
      variant: The disconnect variant.
    """
    with self.dut.bl4a.register_callback(_Module.PBAP) as dut_cb:
      client = await self._make_pbap_client_from_ref()
      self.logger.info('[REF] Send connect request.')
      connect_response = await client.send_request(_CONNECT_REQUEST)
      self.assertEqual(
          connect_response.response_code, obex.ResponseCode.SUCCESS
      )
      self.logger.info('[REF] Wait for profile connected.')
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.state == android_constants.ConnectionState.CONNECTED,
      )

      match variant:
        case _DisconnectVariant.ACL:
          self.logger.info('[REF] Disconnect ACL.')
          coroutine = (
              client.bearer.multiplexer.l2cap_channel.connection.disconnect()
          )
        case _DisconnectVariant.BEARER:
          self.logger.info('[REF] Disconnect bearer.')
          coroutine = client.bearer.disconnect()

      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        await coroutine

      self.logger.info('[REF] Wait for profile disconnected.')
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.state == android_constants.ConnectionState.DISCONNECTED,
      )

  async def test_download_contact(self) -> None:
    """Tests downloading contact phonebook.

    Test Steps:
      1. Connect PBAP from REF to DUT.
      2. Get contact phonebook size.
      3. Get contact phonebook.
    """
    client = await self._make_pbap_client_from_ref()

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info('[REF] Send OBEX connect request.')
      connect_response = await client.send_request(_CONNECT_REQUEST)

    self.assertEqual(connect_response.response_code, obex.ResponseCode.SUCCESS)
    connection_id = connect_response.headers.connection_id
    if connection_id is None:
      self.fail('Missing Connection ID.')

    self.logger.info('[REF] Get contact phonebook size.')
    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            connection_id=connection_id,
            name='telecom/pb.vcf',
            type=_PBAP_PHONE_BOOK_TYPE,
            app_parameters=pbap.ApplicationParameters(
                format=pbap.ApplicationParameterValue.Format.V_3_0,
                max_list_count=0,
                list_start_offset=0,
            ).to_bytes(),
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      response = await client.send_request(request)
    self.assertEqual(response.response_code, obex.ResponseCode.SUCCESS)
    self.assertIsNotNone(response.headers.app_parameters)
    response_app_params = pbap.ApplicationParameters.from_bytes(
        response.headers.app_parameters
    )
    # The first contact must be the owner, which is not included in the contact
    # list.
    self.assertEqual(response_app_params.phonebook_size, len(self.contacts) + 1)

    self.logger.info('[REF] Get contact phonebook.')
    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            connection_id=connection_id,
            name='telecom/pb.vcf',
            type=_PBAP_PHONE_BOOK_TYPE,
            app_parameters=pbap.ApplicationParameters(
                format=pbap.ApplicationParameterValue.Format.V_3_0,
                max_list_count=_MAX_LIST_COUNT,
                list_start_offset=0,
            ).to_bytes(),
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      response = await client.send_request(request)
    self.assertEqual(response.response_code, obex.ResponseCode.SUCCESS)

    # Check the vCard list.
    # Note: The order of the vCards is not guaranteed. If the behavior is
    # changed in the future, we need to update the test to ignore the order.
    vcards = _parse_vcard_list(
        response.headers.body or response.headers.end_of_body or b''
    )
    self.logger.debug('<<< %s', vcards)
    self.assertLen(vcards, len(self.contacts) + 1)
    for i, vcard in enumerate(vcards[1:]):
      phone_type = _VCARD_PHONE_TYPES.get(self.contacts[i]['phone_type'])
      email_type = _VCARD_EMAIL_TYPES.get(self.contacts[i]['email_type'])
      self.assertEqual(vcard['FN'], self.contacts[i]['name'])
      self.assertEqual(
          vcard[f'TEL;TYPE={phone_type}'],
          self.contacts[i]['number'].replace('-', ''),
      )
      self.assertEqual(
          vcard[f'EMAIL;TYPE={email_type}'],
          self.contacts[i]['email'],
      )
      org = vcard.get('ORG', None) or vcard.get('ORG;CHARSET=UTF-8', None)
      self.assertEqual(org, self.contacts[i]['company'])

  @navi_test_base.parameterized(
      ('ich',),
      ('och',),
      ('mch',),
  )
  async def test_download_call_logs(self, phonebook_name: str) -> None:
    """Tests downloading call logs."""
    client = await self._make_pbap_client_from_ref()

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info('[REF] Send OBEX connect request.')
      connect_response = await client.send_request(_CONNECT_REQUEST)

    self.assertEqual(connect_response.response_code, obex.ResponseCode.SUCCESS)
    connection_id = connect_response.headers.connection_id
    if connection_id is None:
      self.fail('Missing Connection ID.')

    self.logger.info('[REF] Get phonebook.')
    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            connection_id=connection_id,
            name=f'telecom/{phonebook_name}.vcf',
            type=_PBAP_PHONE_BOOK_TYPE,
            app_parameters=pbap.ApplicationParameters(
                format=pbap.ApplicationParameterValue.Format.V_3_0,
                max_list_count=_MAX_LIST_COUNT,
                list_start_offset=0,
            ).to_bytes(),
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      response = await client.send_request(request)
    self.assertEqual(response.response_code, obex.ResponseCode.SUCCESS)

    call_type = _VCARD_CALL_TYPES.get(phonebook_name)
    call_logs = [
        call_log
        for call_log in self.call_logs
        if call_log['call_type'] == call_type
    ]
    vcards = _parse_vcard_list(
        response.headers.body or response.headers.end_of_body or b''
    )
    self.logger.debug('<<< %s', vcards)
    for i, vcard in enumerate(vcards):
      full_name = vcard.get('FN', None) or vcard.get('FN;CHARSET=UTF-8', None)
      self.assertEqual(full_name, call_logs[i]['name'])
      self.assertEqual(vcard['TEL;TYPE=0'], call_logs[i]['number'])


if __name__ == '__main__':
  test_runner.main()
