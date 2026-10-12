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

"""Tests for Message Access Profile (MAP) Server implementation on Android."""

import asyncio
import dataclasses
import datetime
import enum
import itertools
from typing import TypeAlias
import xml.etree.ElementTree as ET

from bumble import core
from bumble import l2cap
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import message_access
from navi.bumble_ext import obex
from navi.bumble_ext import rfcomm
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_AppParams: TypeAlias = message_access.ApplicationParameters
_AppParamValue: TypeAlias = message_access.ApplicationParameterValue
_CallbackHandler: TypeAlias = bl4a_api.CallbackHandler
_Module: TypeAlias = bl4a_api.Module


@dataclasses.dataclass
class _SmsMessage:
  """A SMS message.

  Attributes:
    thread_id: The thread ID of the message. Can be any positive integer.
    date: The POSIX timestamp in milliseconds of the message.
    address: The address (phone number) of the message.
    body: The body of the message.
  """

  thread_id: int
  date: int
  address: str
  body: str


_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_OBEX_PACKET_LENGTH = 8192
_PROPERTY_MAP_SERVER_ENABLED = "bluetooth.profile.map.server.enabled"
_MESSAGE_STATUS = b"x-bt/messageStatus\0"
_MMSSMS_DB_PATH = (
    "/data/data/com.android.providers.telephony/databases/mmssms.db"
)
_CONNECT_REQUEST = obex.ConnectRequest(
    obex_version_number=obex.Version.V_1_0,
    flags=0,
    maximum_obex_packet_length=_MAX_OBEX_PACKET_LENGTH,
    final=True,
    headers=obex.Headers(
        target=message_access.MAS_TARGET_UUID.bytes,
        app_parameters=message_access.ApplicationParameters(
            map_supported_features=_AppParamValue.SupportedFeatures(0xFF),
        ).to_bytes(),
    ),
)
_SAMPLE_MESSAGE = _SmsMessage(
    thread_id=1,
    date=int(datetime.datetime(year=2024, month=10, day=1).timestamp() * 1000),
    address="0987654321",
    body="Hi, 你好",
)


class _DisconnectVariant(enum.IntEnum):
  ACL = 1
  BEARER = 2


class _BearerType(enum.IntEnum):
  RFCOMM = 1
  L2CAP = 2


class MapTest(navi_test_base.TwoDevicesTestBase):

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if self.dut.device.is_emulator:
      self.setprop_for_class_context(_PROPERTY_MAP_SERVER_ENABLED, "true")

    if self.dut.getprop(_PROPERTY_MAP_SERVER_ENABLED) != "true":
      raise signals.TestAbortClass("MAP server is not enabled on DUT.")

  async def _setup_paired_devices(self) -> None:
    # Setup pairing and terminate connection.
    with self.dut.bl4a.register_callback(_Module.ADAPTER) as dut_cb:
      await self.classic_connect_and_pair()
      self.dut.bt.setMessageAccessPermission(
          self.ref.address, android_constants.BluetoothAccessPermission.ALLOWED
      )
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          ),
      )

  @override
  async def async_setup_test(self) -> None:
    self._clear_sms_messages()
    await super().async_setup_test()
    await self._setup_paired_devices()

  async def _make_mas_client_from_ref(
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
      sdp_info = await message_access.MasSdpInfo.find(ref_dut_acl)
      if not sdp_info:
        self.fail("Failed to find SDP record for MAP MAS.")

      bearer: obex.Bearer
      if bearer_type == _BearerType.L2CAP:
        if not sdp_info.goep_l2cap_psm:
          self.fail("Failed to find GOEP L2CAP PSM in SDP record.")
        self.logger.info(
            "[REF] Open L2CAP channel to %d.", sdp_info.goep_l2cap_psm
        )
        bearer = await ref_dut_acl.create_l2cap_channel(
            l2cap.ClassicChannelSpec(
                psm=sdp_info.goep_l2cap_psm,
                mode=l2cap.TransmissionMode.ENHANCED_RETRANSMISSION,
                fcs_enabled=True,
            )
        )
      else:
        ref_rfcomm_manager = rfcomm.Manager.find_or_create(self.ref.device)
        self.logger.info("[REF] Connect RFCOMM.")
        rfcomm_client = await ref_rfcomm_manager.connect(ref_dut_acl)
        self.logger.info("[REF] Open DLC to %d.", sdp_info.rfcomm_channel)
        bearer = await rfcomm_client.open_dlc(sdp_info.rfcomm_channel)
      return obex.ClientSession(bearer)

  async def _make_connected_mas_client_from_ref(
      self, bearer_type: _BearerType
  ) -> tuple[obex.ClientSession, int]:
    with self.dut.bl4a.register_callback(_Module.MAP) as dut_cb:
      client = await self._make_mas_client_from_ref(bearer_type)
      self.logger.info("[REF] Send connect request.")
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        response = await client.send_request(_CONNECT_REQUEST)
      self.assertEqual(response.response_code, obex.ResponseCode.SUCCESS)
      if not (connection_id := response.headers.connection_id):
        self.fail("Failed to get connection ID from connect response.")
      self.logger.info("[DUT] Wait for profile connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )
    return client, connection_id

  def _clear_sms_messages(self) -> None:
    self.dut.shell(["sqlite3", _MMSSMS_DB_PATH, "'delete from sms'"])

  def _add_sms_message(
      self, sms_message: _SmsMessage, message_type: int
  ) -> None:
    self.dut.shell([
        "sqlite3",
        _MMSSMS_DB_PATH,
        (
            '"insert into sms (thread_id, date, type, address, body) values '
            "(%d, %d, %d, '%s', '%s')\""
            % (
                sms_message.thread_id,
                sms_message.date,
                message_type,
                sms_message.address,
                sms_message.body,
            )
        ),
    ])

  @navi_test_base.parameterized(
      *itertools.product(
          (_DisconnectVariant.ACL, _DisconnectVariant.BEARER),
          (_BearerType.RFCOMM, _BearerType.L2CAP),
      )
  )
  async def test_connect_disconnect(
      self, variant: _DisconnectVariant, bearer_type: _BearerType
  ) -> None:
    """Tests connecting and disconnecting message_access.

    Test steps:
      1. Connect MAP from REF to DUT.
      2. Disconnect bearer or ACL from REF.

    Args:
      variant: The disconnect variant.
      bearer_type: The bearer type.
    """
    with self.dut.bl4a.register_callback(_Module.MAP) as dut_cb:
      client, _ = await self._make_connected_mas_client_from_ref(bearer_type)

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
      *itertools.product(
          (
              android_constants.SmsMessageType.INBOX,
              android_constants.SmsMessageType.SENT,
          ),
          (_BearerType.RFCOMM, _BearerType.L2CAP),
      )
  )
  async def test_get_sms(
      self,
      message_type: android_constants.SmsMessageType,
      bearer_type: _BearerType,
  ) -> None:
    """Tests getting SMS message from DUT.

    Test steps:
      1. Add SMS message to DUT.
      2. Connect MAP from REF to DUT.
      3. Get SMS message list.
      4. Get SMS message with handle returned in step 3.

    Args:
      message_type: The SMS message type.
      bearer_type: The bearer type.
    """
    folder_path = {
        android_constants.SmsMessageType.INBOX: "inbox",
        android_constants.SmsMessageType.SENT: "sent",
    }[message_type]
    self._add_sms_message(_SAMPLE_MESSAGE, message_type)

    client, connection_id = await self._make_connected_mas_client_from_ref(
        bearer_type
    )
    for folder in ("telecom", "msg"):
      self.logger.info("[REF] Set folder path to %s.", folder)
      setpath_request = obex.SetpathRequest(
          opcode=obex.Opcode.SETPATH,
          final=True,
          flags=obex.SetpathRequest.Flags.DO_NOT_CREATE_FOLDER_IF_NOT_EXIST,
          headers=obex.Headers(
              connection_id=connection_id,
              name=folder,
          ),
      )
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        response = await client.send_request(setpath_request)
        self.assertEqual(response.response_code, obex.ResponseCode.SUCCESS)

    self.logger.info("[REF] Get SMS Message list.")
    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            name=folder_path,
            type=message_access.ObexHeaderType.MESSAGE_LISTING.value,
            connection_id=connection_id,
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      response = await client.send_request(request)
    self.assertEqual(response.response_code, obex.ResponseCode.SUCCESS)
    body = response.headers.body or response.headers.end_of_body or b""
    if (msg := ET.fromstring(body).find("msg")) is None:
      self.fail("Failed to find message in message listing.")
    self.assertIsNotNone(handle := msg.attrib.get("handle"))

    self.logger.info("[REF] Get SMS Message with handle %s.", handle)
    request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            name=handle,
            type=message_access.ObexHeaderType.MESSAGE.value,
            connection_id=connection_id,
            app_parameters=message_access.ApplicationParameters(
                charset=_AppParamValue.Charset.UTF_8,
                attachment=0,
            ).to_bytes(),
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      response = await client.send_request(request)
    self.assertEqual(response.response_code, obex.ResponseCode.SUCCESS)
    body = response.headers.body or response.headers.end_of_body or b""
    lines = body.decode("utf-8").split("\r\n")
    self.assertIn(_SAMPLE_MESSAGE.body, lines)
    self.assertIn(f"TEL:{_SAMPLE_MESSAGE.address}", lines)

  @navi_test_base.parameterized(
      (_BearerType.RFCOMM,),
      (_BearerType.L2CAP,),
  )
  async def test_message_notification(self, bearer_type: _BearerType) -> None:
    """Tests sending message notification from DUT to REF.

    Test steps:
      1. Setup MNS server on REF.
      2. Register message notification from REF.
      3. Wait for DUT to connect to the MNS server.
      4. Add SMS message to DUT.
      5. Wait for REF to receive the message notification.

    Args:
      bearer_type: The bearer type.
    """
    mns_server = message_access.MnsServer(self.ref.device)
    goep_l2cap_psm: int | None
    if bearer_type == _BearerType.L2CAP:
      goep_l2cap_psm = mns_server.l2cap_server.psm
    else:
      goep_l2cap_psm = None
    self.ref.device.sdp_service_records = {
        1: (
            message_access.MnsSdpInfo(
                service_record_handle=1,
                rfcomm_channel=mns_server.rfcomm_channel,
                version=message_access.Version.V_1_4,
                supported_features=(
                    message_access.ApplicationParameterValue.SupportedFeatures(
                        0xFF
                    )
                ),
                goep_l2cap_psm=goep_l2cap_psm,
            ).to_sdp_records()
        ),
    }
    client, connection_id = await self._make_connected_mas_client_from_ref(
        bearer_type
    )

    request = obex.Request(
        opcode=obex.Opcode.PUT,
        final=True,
        headers=obex.Headers(
            type=message_access.ObexHeaderType.NOTIFICATION_REGISTRATION.value,
            connection_id=connection_id,
            app_parameters=message_access.ApplicationParameters(
                notification_status=1
            ).to_bytes(),
            end_of_body=b"\0",
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info("[REF] Register message notification.")
      response = await client.send_request(request)
    self.assertEqual(response.response_code, obex.ResponseCode.SUCCESS)

    # After sending the request, DUT should connect to the MNS server.
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for MNS OBEX connection.")
      session = await mns_server.sessions.get()
      self.logger.info("[REF] Wait for MNS connect request.")
      await session.connected.wait()

    # SMS Content Observer might not be ready right after MNS connection.
    # However, there isn't a good way to check if the SMS Content Observer is
    # ready. So we just wait for a short period.
    await asyncio.sleep(0.5)

    # Add an SMS message to DUT to trigger message notification.
    new_message = _SmsMessage(
        thread_id=1,
        # Since 24Q1, Android only sends notification for new messages in the
        # last 7 days.
        date=int(self.dut.shell("date +%s")) * 1000,
        address="0987654321",
        body="This is a new message",
    )
    self._add_sms_message(new_message, android_constants.SmsMessageType.INBOX)
    self.dut.bt.notifyMmsSmsChange()
    self.logger.info("[REF] Wait for message notification.")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      notification = await session.notifications.get()
    # Check the notification content.
    if (event := ET.fromstring(notification).find("event")) is None:
      self.fail("Failed to find event in message notification.")
    self.assertEqual(event.attrib["type"], "NewMessage")
    self.assertEqual(event.attrib["sender_name"], new_message.address)
    self.assertIn("handle", event.attrib)

  async def test_read_sms_message_from_carkit(self) -> None:
    """Tests receiving SMS notification on car-kit and updating read status.

    Test steps:
      1. Configure REF as a Car Kit device and set up MNS server SDP record.
      2. Establish MAS connection from REF to DUT.
      3. Register for MNS event notifications on DUT and wait for MNS session.
      4. Simulate an incoming SMS with symbols and emojis on DUT.
      5. Verify REF receives the NewMessage notification and extracts handle.
      6. Send OBEX PUT with type 'x-bt/messageStatus' to mark message as READ.
      7. Verify bMessage retrieved via OBEX GET reflects 'STATUS:READ'.
    """
    self.logger.info(
        "[REF] Configure Car Kit Class of Device and start MNS server."
    )
    self.ref.config.class_of_device = 0x200408  # Car Kit (Handsfree / Audio)
    mns_server = message_access.MnsServer(self.ref.device)
    self.ref.device.sdp_service_records = {
        1: (
            message_access.MnsSdpInfo(
                service_record_handle=1,
                rfcomm_channel=mns_server.rfcomm_channel,
                version=message_access.Version.V_1_4,
                supported_features=(
                    message_access.ApplicationParameterValue.SupportedFeatures(
                        0xFF
                    )
                ),
            ).to_sdp_records()
        ),
    }

    self.logger.info("[REF] Connect MAS client to DUT over RFCOMM.")
    client, connection_id = await self._make_connected_mas_client_from_ref(
        _BearerType.RFCOMM
    )

    self.logger.info("[REF] Register for MNS event notifications on DUT.")
    request = obex.Request(
        opcode=obex.Opcode.PUT,
        final=True,
        headers=obex.Headers(
            type=message_access.ObexHeaderType.NOTIFICATION_REGISTRATION.value,
            connection_id=connection_id,
            app_parameters=message_access.ApplicationParameters(
                notification_status=1
            ).to_bytes(),
            end_of_body=b"\0",
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      response = await client.send_request(request)
    self.assertEqual(response.response_code, obex.ResponseCode.SUCCESS)

    self.logger.info("[REF] Wait for MNS session connection from DUT.")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      session = await mns_server.sessions.get()
      await session.connected.wait()

    await asyncio.sleep(0.5)

    self.logger.info(
        "[DUT] Inject incoming SMS into telephony provider to simulate message"
        " arrival."
    )
    new_message = _SmsMessage(
        thread_id=1,
        date=int(self.dut.shell("date +%s")) * 1000,
        address="1234567890",
        body="A short message with symbols: !@#$%^&*()_+ and emojis: 😊🚀",
    )
    self._add_sms_message(new_message, android_constants.SmsMessageType.INBOX)
    self.logger.info("[DUT] Notify Bluetooth MAP service of new incoming SMS.")
    self.dut.bt.notifyMmsSmsChange()

    self.logger.info("[REF] Wait for NewMessage notification event via MNS.")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      notification = await session.notifications.get()
    if (event := ET.fromstring(notification).find("event")) is None:
      self.fail("Failed to find event in message notification.")
    self.assertEqual(event.attrib["type"], "NewMessage")
    handle = event.attrib.get("handle")
    self.assertIsNotNone(handle, "NewMessage event missing handle attribute.")

    self.logger.info(
        "[REF] Set message status to READ via SetMessageStatus PUT (handle:"
        " %s).",
        handle,
    )
    set_status_request = obex.Request(
        opcode=obex.Opcode.PUT,
        final=True,
        headers=obex.Headers(
            name=handle,
            type=_MESSAGE_STATUS,
            connection_id=connection_id,
            app_parameters=message_access.ApplicationParameters(
                status_indicator=0,
                status_value=1,
            ).to_bytes(),
            end_of_body=b"\0",
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      set_status_response = await client.send_request(set_status_request)
    self.assertEqual(
        set_status_response.response_code, obex.ResponseCode.SUCCESS
    )

    self.logger.info(
        "[REF] Fetch bMessage to verify updated READ status (handle: %s).",
        handle,
    )
    get_message_request = obex.Request(
        opcode=obex.Opcode.GET,
        final=True,
        headers=obex.Headers(
            name=handle,
            type=message_access.ObexHeaderType.MESSAGE.value,
            connection_id=connection_id,
            app_parameters=message_access.ApplicationParameters(
                charset=_AppParamValue.Charset.UTF_8,
                attachment=0,
            ).to_bytes(),
        ),
    )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      get_message_response = await client.send_request(get_message_request)
    self.assertEqual(
        get_message_response.response_code, obex.ResponseCode.SUCCESS
    )
    body = (
        get_message_response.headers.body
        or get_message_response.headers.end_of_body
        or b""
    )
    lines = body.decode("utf-8", errors="replace").splitlines()
    status_line = next(
        (line for line in lines if line.startswith("STATUS:")), None
    )
    self.assertIsNotNone(
        status_line, f"STATUS line not found in bMessage: {body!r}"
    )
    self.assertEqual(status_line.split(":", 1)[1], "READ")


if __name__ == "__main__":
  test_runner.main()
