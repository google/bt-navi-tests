#  Copyright 2026 Google LLC
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
import itertools
from typing import Any, TypeAlias

from bumble import core
from bumble import l2cap
from bumble import rfcomm
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import obex
from navi.bumble_ext import pbap
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_PBAP_PCE_SDP_RECORD_HANDLE = 1
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_OBEX_PACKET_LENGTH = 8192
_MAX_LIST_COUNT = 500
_PBAP_PHONE_BOOK_TYPE = b"x-bt/phonebook\0"
_PROPERTY_PBAP_SERVER_ENABLED = "bluetooth.profile.pbap.server.enabled"
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

_Module: TypeAlias = bl4a_api.Module

# Mapping from Android phone type to vCard phone type.
_VCARD_PHONE_TYPES = {
    android_constants.PhoneType.HOME: "HOME",
    android_constants.PhoneType.MOBILE: "CELL",
    android_constants.PhoneType.WORK: "WORK",
}

# Mapping from Android phone type to vCard Email type.
_VCARD_EMAIL_TYPES = {
    android_constants.EmailType.HOME: "HOME",
    android_constants.EmailType.WORK: "WORK",
}

# Mapping from vCard call type to Android call type.
_VCARD_CALL_TYPES = {
    "ich": android_constants.CallType.INCOMING,
    "och": android_constants.CallType.OUTGOING,
    "mch": android_constants.CallType.MISSED,
}

_CALL_LOGS = [
    {
        "number": "+81-3-1234-5678",
        "name": "John Smith",
        "date": 1727273204595,
        "duration": 10000,
        "call_type": 1,
    },
    {
        "number": "+81-3-1234-5678",
        "name": "鈴木 花子",
        "date": 1727273204595,
        "duration": 10000,
        "call_type": 2,
    },
    {
        "number": "+49-30-12345678",
        "name": "محمد علي",
        "date": 1727273204595,
        "duration": 0,
        "call_type": 3,
    },
]
_CONTACTS = [
    {
        "number": "+1-555-123-4567",
        "name": "John Doe",
        "phone_type": 2,
        "email": "john.doe@example.com",
        "email_type": 2,
        "company": "Acme Corporation",
        "job_title": "Software Engineer",
    },
    {
        "number": "+81-3-1234-5678",
        "name": "鈴木 花子",
        "phone_type": 1,
        "email": "hana.suzuki@example.co.jp",
        "email_type": 1,
        "company": "株式会社 例",
        "job_title": "営業部長",
    },
    {
        "number": "+49-30-12345678",
        "name": "Johannes Schmidt",
        "phone_type": 3,
        "email": "johannes.schmidt@example.de",
        "email_type": 2,
        "company": "Beispiel GmbH",
        "job_title": "Softwareentwickler",
    },
    {
        "number": "+33-1-23-45-67-89",
        "name": "Pierre Dupont",
        "phone_type": 2,
        "email": "pierre.dupont@example.fr",
        "email_type": 1,
        "company": "Société Exemple",
        "job_title": "Ingénieur Logiciel",
    },
    {
        "number": "+86-10-1234-5678",
        "name": "张伟",
        "phone_type": 3,
        "email": "zhang.wei@example.cn",
        "email_type": 2,
        "company": "示例公司",
        "job_title": "软件工程师",
    },
    {
        "number": "+34-91-123-45-67",
        "name": "María García",
        "phone_type": 1,
        "email": "maria.garcia@example.es",
        "email_type": 1,
        "company": "Ejemplo S.A.",
        "job_title": "Ingeniera de Software",
    },
    {
        "number": "+55-11-1234-5678",
        "name": "Otávio Oliveira Magalhães",
        "phone_type": 2,
        "email": "otavio.magalhaes@example.com.br",
        "email_type": 2,
        "company": "Exemplo Ltda.",
        "job_title": "Engenheiro de Software",
    },
    {
        "number": "+7-495-123-45-67",
        "name": "Иван Иванов",
        "phone_type": 3,
        "email": "ivan.ivanov@example.ru",
        "email_type": 2,
        "company": 'ООО "Пример"',
        "job_title": "Программист",
    },
    {
        "number": "+91-22-1234-5678",
        "name": "Rahul Kumar",
        "phone_type": 2,
        "email": "rahul.kumar@example.in",
        "email_type": 1,
        "company": "Example Pvt. Ltd.",
        "job_title": "Software Engineer",
    },
    {
        "number": "+39-06-1234-5678",
        "name": "Marco Rossi",
        "phone_type": 1,
        "email": "marco.rossi@example.it",
        "email_type": 1,
        "company": "Esempio S.p.A.",
        "job_title": "Ingegnere del Software",
    },
    {
        "number": "+82-2-1234-5678",
        "name": "김철수",
        "phone_type": 3,
        "email": "cheolsu.kim@example.co.kr",
        "email_type": 2,
        "company": "예제 주식회사",
        "job_title": "소프트웨어 엔지니어",
    },
    {
        "number": "+971-4-123-4567",
        "name": "محمد علي",
        "phone_type": 2,
        "email": "mohammed.ali@example.ae",
        "email_type": 1,
        "company": "شركة مثال",
        "job_title": "مهندس برمجيات",
    },
    {
        "number": "+20-2-1234-5678",
        "name": "محمد أحمد",
        "phone_type": 1,
        "email": "mohamed.ahmed@example.eg",
        "email_type": 1,
        "company": "مثال شركة",
        "job_title": "مهندس برمجيات",
    },
    {
        "number": "+54-11-1234-5678",
        "name": "Juan Pérez",
        "phone_type": 3,
        "email": "juan.perez@example.com.ar",
        "email_type": 2,
        "company": "Ejemplo S.A.",
        "job_title": "Ingeniero de Software",
    },
    {
        "number": "+44-20-1234-5678",
        "name": "David Smith",
        "phone_type": 2,
        "email": "david.smith@example.co.uk",
        "email_type": 1,
        "company": "Example Ltd.",
        "job_title": "Software Engineer",
    },
    {
        "number": "+61-2-1234-5678",
        "name": "John Smith",
        "phone_type": 1,
        "email": "john.smith@example.com.au",
        "email_type": 1,
        "company": "Example Pty Ltd.",
        "job_title": "Software Engineer",
    },
    {
        "number": "+351-21-123-45-67",
        "name": "João Silva",
        "phone_type": 3,
        "email": "joao.silva@example.pt",
        "email_type": 2,
        "company": "Exemplo Lda.",
        "job_title": "Engenheiro de Software",
    },
    {
        "number": "+47-22-12-34-56",
        "name": "Ola Nordmann",
        "phone_type": 2,
        "email": "ola.nordmann@example.no",
        "email_type": 1,
        "company": "Eksempel AS",
        "job_title": "Programvareutvikler",
    },
]


def _parse_vcard_list(data: bytes) -> list[dict[str, str]]:
  vcard_list = []
  vcard = dict[str, str]()
  for line in data.split(b"\r\n"):
    if not line:
      continue
    if line.startswith(b"BEGIN:VCARD"):
      vcard = {}
    elif line.startswith(b"END:VCARD"):
      vcard_list.append(vcard)
    else:
      key, value = line.split(b":", maxsplit=1)
      vcard[key.decode("utf-8")] = value.decode("utf-8")
  return vcard_list


class _DisconnectVariant(enum.IntEnum):
  ACL = 1
  BEARER = 2


class _BearerType(enum.IntEnum):
  RFCOMM = 1
  L2CAP = 2


class PbapTest(navi_test_base.TwoDevicesTestBase):
  contacts: list[dict[str, Any]]
  call_logs: list[dict[str, Any]]

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if self.dut.device.is_emulator:
      self.setprop_for_class_context(_PROPERTY_PBAP_SERVER_ENABLED, "true")

    if self.dut.getprop(_PROPERTY_PBAP_SERVER_ENABLED) != "true":
      raise signals.TestAbortClass("PBAP server is not enabled on DUT.")
    self.contacts = _CONTACTS
    self.call_logs = _CALL_LOGS

  async def _setup_paired_devices(self) -> None:
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
          bl4a_api.AclDisconnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          ),
      )

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    self.dut.bt.clearContacts()
    self.dut.bt.clearCallLogs()
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
    await self._setup_paired_devices()

  async def _make_pbap_client_from_ref(
      self, bearer_type: _BearerType
  ) -> obex.ClientSession:
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info("[REF] Connect to DUT.")
      ref_dut_acl = await self.ref.device.connect(
          self.dut.address, transport=core.BT_BR_EDR_TRANSPORT
      )

      self.logger.info("[REF] Authenticate and encrypt.")
      await ref_dut_acl.authenticate()
      await ref_dut_acl.encrypt()

      self.logger.info("[REF] Find SDP record.")
      sdp_info = await pbap.find_pse_sdp_record(ref_dut_acl)
      if not sdp_info:
        self.fail("Failed to find SDP record for pbap.")

      bearer: obex.Bearer
      if bearer_type == _BearerType.L2CAP:
        if not sdp_info.goep_l2cap_psm:
          self.fail("Failed to find GOEP L2CAP PSM in SDP record.")
        self.logger.info("[REF] Open L2CAP to %d.", sdp_info.goep_l2cap_psm)
        bearer = await ref_dut_acl.create_l2cap_channel(
            l2cap.ClassicChannelSpec(
                psm=sdp_info.goep_l2cap_psm,
                mode=l2cap.TransmissionMode.ENHANCED_RETRANSMISSION,
                fcs_enabled=True,
            )
        )
      else:
        self.logger.info("[REF] Connect RFCOMM.")
        rfcomm_client = await rfcomm.Client(ref_dut_acl).start()
        self.logger.info("[REF] Open DLC to %d.", sdp_info.rfcomm_channel)
        bearer = await rfcomm_client.open_dlc(sdp_info.rfcomm_channel)
      return obex.ClientSession(bearer)

  @navi_test_base.parameterized(
      *itertools.product(
          (_DisconnectVariant.ACL, _DisconnectVariant.BEARER),
          (_BearerType.RFCOMM, _BearerType.L2CAP),
      )
  )
  async def test_connect_disconnect(
      self, variant: _DisconnectVariant, bearer_type: _BearerType
  ) -> None:
    """Tests connecting and disconnecting PBAP.

    Test steps:
      1. Connect PBAP from REF to DUT.
      2. Disconnect bearer or ACL from REF.

    Args:
      variant: The disconnect variant.
      bearer_type: The bearer type.
    """
    with self.dut.bl4a.register_callback(_Module.PBAP) as dut_cb:
      client = await self._make_pbap_client_from_ref(bearer_type)
      self.logger.info("[REF] Send connect request.")
      connect_response = await client.send_request(_CONNECT_REQUEST)
      self.assertEqual(
          connect_response.response_code, obex.ResponseCode.SUCCESS
      )
      self.logger.info("[REF] Wait for profile connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

      match variant:
        case _DisconnectVariant.ACL:
          self.logger.info("[REF] Disconnect ACL.")
          coroutine = list(self.ref.device.connections.values())[0].disconnect()
        case _DisconnectVariant.BEARER:
          self.logger.info("[REF] Disconnect bearer.")
          coroutine = client.bearer.disconnect()

      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        await coroutine

      self.logger.info("[REF] Wait for profile disconnected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )

  @navi_test_base.parameterized(
      (_BearerType.RFCOMM,),
      (_BearerType.L2CAP,),
  )
  async def test_download_contact(self, bearer_type: _BearerType) -> None:
    """Tests downloading contact phonebook.

    Test steps:
      1. Connect PBAP from REF to DUT.
      2. Get contact phonebook size.
      3. Get contact phonebook.

    Args:
      bearer_type: The bearer type.
    """
    client = await self._make_pbap_client_from_ref(bearer_type)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info("[REF] Send OBEX connect request.")
      connect_response = await client.send_request(_CONNECT_REQUEST)

    self.assertEqual(connect_response.response_code, obex.ResponseCode.SUCCESS)
    connection_id = connect_response.headers.connection_id
    if connection_id is None:
      self.fail("Missing Connection ID.")

    self.logger.info("[REF] Get contact phonebook size.")
    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            connection_id=connection_id,
            name="telecom/pb.vcf",
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
    if not response.headers.app_parameters:
      self.fail("Missing app parameters.")
    response_app_params = pbap.ApplicationParameters.from_bytes(
        response.headers.app_parameters
    )
    # The first contact must be the owner, which is not included in the contact
    # list.
    self.assertEqual(response_app_params.phonebook_size, len(self.contacts) + 1)

    self.logger.info("[REF] Get contact phonebook.")
    body = b""
    list_start_offset = 0
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        request = obex.Request(
            opcode=obex.Opcode.GET,
            final=True,
            headers=obex.Headers(
                connection_id=connection_id,
                name="telecom/pb.vcf",
                type=_PBAP_PHONE_BOOK_TYPE,
                app_parameters=pbap.ApplicationParameters(
                    format=pbap.ApplicationParameterValue.Format.V_3_0,
                    max_list_count=_MAX_LIST_COUNT,
                    list_start_offset=list_start_offset,
                ).to_bytes(),
            ),
        )
        response = await client.send_request(request)
        body += response.headers.body or response.headers.end_of_body or b""
        if response.response_code != obex.ResponseCode.CONTINUE:
          break
        if (
            (app_param_bytes := response.headers.app_parameters)
            and (
                app_params := pbap.ApplicationParameters.from_bytes(
                    app_param_bytes
                )
            )
            and (
                (new_list_start_offset := app_params.list_start_offset)
                is not None
            )
        ):
          list_start_offset = new_list_start_offset

    # Check the vCard list.
    # Note: The order of the vCards is not guaranteed. If the behavior is
    # changed in the future, we need to update the test to ignore the order.
    vcards = _parse_vcard_list(body)
    self.logger.debug("<<< %s", vcards)
    self.assertLen(vcards, len(self.contacts) + 1)
    for i, vcard in enumerate(vcards[1:]):
      phone_type = _VCARD_PHONE_TYPES.get(self.contacts[i]["phone_type"])
      email_type = _VCARD_EMAIL_TYPES.get(self.contacts[i]["email_type"])
      self.assertEqual(vcard["FN"], self.contacts[i]["name"])
      self.assertEqual(
          vcard[f"TEL;TYPE={phone_type}"],
          self.contacts[i]["number"].replace("-", ""),
      )
      self.assertEqual(
          vcard[f"EMAIL;TYPE={email_type}"],
          self.contacts[i]["email"],
      )
      org = vcard.get("ORG", None) or vcard.get("ORG;CHARSET=UTF-8", None)
      self.assertEqual(org, self.contacts[i]["company"])

  @navi_test_base.parameterized(
      ("ich",),
      ("och",),
      ("mch",),
  )
  async def test_download_call_logs(self, phonebook_name: str) -> None:
    """Tests downloading call logs."""
    client = await self._make_pbap_client_from_ref(_BearerType.RFCOMM)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info("[REF] Send OBEX connect request.")
      connect_response = await client.send_request(_CONNECT_REQUEST)

    self.assertEqual(connect_response.response_code, obex.ResponseCode.SUCCESS)
    connection_id = connect_response.headers.connection_id
    if connection_id is None:
      self.fail("Missing Connection ID.")

    self.logger.info("[REF] Get phonebook.")
    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            connection_id=connection_id,
            name=f"telecom/{phonebook_name}.vcf",
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
        if call_log["call_type"] == call_type
    ]
    vcards = _parse_vcard_list(
        response.headers.body or response.headers.end_of_body or b""
    )
    self.logger.debug("<<< %s", vcards)
    for i, vcard in enumerate(vcards):
      full_name = vcard.get("FN", None) or vcard.get("FN;CHARSET=UTF-8", None)
      self.assertEqual(full_name, call_logs[i]["name"])
      self.assertEqual(vcard["TEL;TYPE=0"], call_logs[i]["number"])

  async def test_pull_phonebook_with_pagination(self) -> None:
    """Tests downloading phonebook with pagination (offset & limit)."""
    batch_size = 5
    client = await self._make_pbap_client_from_ref(_BearerType.RFCOMM)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      connect_response = await client.send_request(_CONNECT_REQUEST)
    self.assertEqual(connect_response.response_code, obex.ResponseCode.SUCCESS)
    connection_id = connect_response.headers.connection_id
    if connection_id is None:
      self.fail("Missing Connection ID.")

    # Get total size
    size_request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            connection_id=connection_id,
            name="telecom/pb.vcf",
            type=_PBAP_PHONE_BOOK_TYPE,
            app_parameters=pbap.ApplicationParameters(
                format=pbap.ApplicationParameterValue.Format.V_3_0,
                max_list_count=0,
                list_start_offset=0,
            ).to_bytes(),
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      size_response = await client.send_request(size_request)
    self.assertEqual(size_response.response_code, obex.ResponseCode.SUCCESS)
    app_params = pbap.ApplicationParameters.from_bytes(
        size_response.headers.app_parameters or b""
    )
    total_size = (
        app_params.phonebook_size
        if app_params.phonebook_size is not None
        else (len(self.contacts) + 1)
    )

    all_downloaded_body = b""
    for offset in range(0, total_size, batch_size):
      page_request = obex.Request(
          opcode=obex.Opcode.GET,
          final=True,
          headers=obex.Headers(
              connection_id=connection_id,
              name="telecom/pb.vcf",
              type=_PBAP_PHONE_BOOK_TYPE,
              app_parameters=pbap.ApplicationParameters(
                  format=pbap.ApplicationParameterValue.Format.V_3_0,
                  max_list_count=batch_size,
                  list_start_offset=offset,
              ).to_bytes(),
          ),
      )
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        page_response = await client.send_request(page_request)
      self.assertEqual(page_response.response_code, obex.ResponseCode.SUCCESS)
      all_downloaded_body += (
          page_response.headers.body
          or page_response.headers.end_of_body
          or b""
      )

    vcards = _parse_vcard_list(all_downloaded_body)
    self.assertLen(vcards, total_size)

  async def test_pull_empty_phonebook(self) -> None:
    """Tests pulling phonebook when contact database is empty."""
    self.dut.bt.clearContacts()
    client = await self._make_pbap_client_from_ref(_BearerType.RFCOMM)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      connect_response = await client.send_request(_CONNECT_REQUEST)
    self.assertEqual(connect_response.response_code, obex.ResponseCode.SUCCESS)
    connection_id = connect_response.headers.connection_id
    if connection_id is None:
      self.fail("Missing Connection ID.")

    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            connection_id=connection_id,
            name="telecom/pb.vcf",
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
    body = response.headers.body or response.headers.end_of_body or b""
    vcards = _parse_vcard_list(body)
    # When empty, at most owner card (1) or 0 cards returned
    self.assertLessEqual(len(vcards), 1)


if __name__ == "__main__":
  test_runner.main()
