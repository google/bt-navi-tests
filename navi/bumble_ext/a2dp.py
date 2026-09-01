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

"""A2DP vendor-specific codec helpers.

There isn't an opened specification for most vendor-specific codecs, so this
module majorly refers to the implementation of AOSP:
* packages/modules/Bluetooth/system/stack/a2dp/
* packages/modules/Bluetooth/system/stack/include/
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import dataclasses
import enum
import struct
from typing import ClassVar, Self, TypeVar

from bumble import a2dp
from bumble import avdtp
from bumble import codecs
from bumble import core
from bumble import device as device_lib
from bumble import sdp

from navi.bumble_ext import avdtp as avdtp_ext
from navi.bumble_ext import ogg
from navi.utils import android_constants
from navi.utils import constants


class AptxChannelMode(enum.IntFlag):
  MONO = 0x01
  STEREO = 0x02


class AptxSamplingRate(enum.IntFlag):
  RATE_44100 = 0x20
  RATE_48000 = 0x10


class LdacSamplingRate(enum.IntFlag):
  RATE_44100 = 0x20
  RATE_48000 = 0x10
  RATE_88200 = 0x08
  RATE_96000 = 0x04
  RATE_176400 = 0x02
  RATE_192000 = 0x01


class LdacChannelMode(enum.IntFlag):
  MONO = 0x04
  DUAL = 0x02
  STEREO = 0x01


class LhdcSamplingRate(enum.IntFlag):
  RATE_44100 = 0x20
  RATE_48000 = 0x10
  RATE_96000 = 0x04
  RATE_192000 = 0x01


class LhdcBitsPerSample(enum.IntFlag):
  BITS_16 = 0x04
  BITS_24 = 0x02
  BITS_32 = 0x01


class LhdcChannelMode(enum.IntFlag):
  MONO = 0x04
  DUAL = 0x02
  STEREO = 0x01


@dataclasses.dataclass(frozen=True)
class LhdcCodecInformation:
  """LHDC v5 codec information."""

  sample_rate: LhdcSamplingRate
  bits_per_sample: LhdcBitsPerSample
  channel_mode: LhdcChannelMode
  version: int = 1
  frame_len_type: int = 0x10  # 5ms
  max_target_bitrate: int = 0x00  # 1000k
  min_target_bitrate: int = 0x00  # 64k
  has_feature_ll: bool = True

  VENDOR_ID: ClassVar[int] = 0x053A
  CODEC_ID: ClassVar[int] = 0x4C35

  def __bytes__(self) -> bytes:
    p7 = (
        (self.min_target_bitrate << 6)
        | (self.max_target_bitrate << 4)
        | self.bits_per_sample
    )
    p8 = self.frame_len_type | (self.version & 0x0F)
    p9 = 0
    if self.has_feature_ll:
      p9 |= 0x40
    p10 = 0
    return struct.pack(
        '<IHBBBBB',
        self.VENDOR_ID,
        self.CODEC_ID,
        self.sample_rate,
        p7,
        p8,
        p9,
        p10,
    )

  @classmethod
  def from_vendor_info(
      cls, info: a2dp.VendorSpecificMediaCodecInformation
  ) -> Self:
    """Decodes the LHDC codec information from the vendor-specific info.

    Args:
      info: The vendor-specific info.

    Returns:
      The LHDC codec information.

    Raises:
      ValueError: If the vendor ID or codec ID is invalid.
    """
    if info.vendor_id != cls.VENDOR_ID:
      raise ValueError(f'Invalid vendor ID: {info.vendor_id}')
    if info.codec_id != cls.CODEC_ID:
      raise ValueError(f'Invalid codec ID: {info.codec_id}')

    sample_rate = info.value[0]
    p7 = info.value[1]
    p8 = info.value[2]
    p9 = info.value[3]

    bits_per_sample = p7 & 0x07
    max_target_bitrate = (p7 >> 4) & 0x03
    min_target_bitrate = (p7 >> 6) & 0x03

    version = p8 & 0x0F
    frame_len_type = p8 & 0x30

    has_feature_ll = bool(p9 & 0x40)

    return cls(
        sample_rate=LhdcSamplingRate(sample_rate),
        bits_per_sample=LhdcBitsPerSample(bits_per_sample),
        channel_mode=LhdcChannelMode.STEREO,
        version=version,
        frame_len_type=frame_len_type,
        max_target_bitrate=max_target_bitrate,
        min_target_bitrate=min_target_bitrate,
        has_feature_ll=has_feature_ll,
    )


@dataclasses.dataclass(frozen=True)
class AptxCodecInformation:
  """APT-X codec information."""

  sample_rate: AptxSamplingRate
  channel_mode: AptxChannelMode

  VENDOR_ID: ClassVar[int] = 0x4F
  CODEC_ID: ClassVar[int] = 0x01

  def __bytes__(self) -> bytes:
    return struct.pack(
        '<IHB',
        self.VENDOR_ID,
        self.CODEC_ID,
        self.sample_rate | self.channel_mode,
    )

  @classmethod
  def from_vendor_info(
      cls, info: a2dp.VendorSpecificMediaCodecInformation
  ) -> Self:
    if info.vendor_id != cls.VENDOR_ID:
      raise ValueError(f'Invalid vendor ID: {info.vendor_id}')
    if info.codec_id != cls.CODEC_ID:
      raise ValueError(f'Invalid codec ID: {info.codec_id}')
    sample_rate = info.value[0] & 0xF0
    channel_mode = info.value[0] & 0x0F
    return cls(AptxSamplingRate(sample_rate), AptxChannelMode(channel_mode))


@dataclasses.dataclass(frozen=True)
class AptxHdCodecInformation:
  """APT-X HD codec information."""

  sample_rate: AptxSamplingRate
  channel_mode: AptxChannelMode

  VENDOR_ID: ClassVar[int] = 0xD7
  CODEC_ID: ClassVar[int] = 0x24

  def __bytes__(self) -> bytes:
    return struct.pack(
        '<IHB4s',
        self.VENDOR_ID,
        self.CODEC_ID,
        self.sample_rate | self.channel_mode,
        bytes(4),  # RFU
    )

  @classmethod
  def from_vendor_info(
      cls, info: a2dp.VendorSpecificMediaCodecInformation
  ) -> Self:
    if info.vendor_id != cls.VENDOR_ID:
      raise ValueError(f'Invalid vendor ID: {info.vendor_id}')
    if info.codec_id != cls.CODEC_ID:
      raise ValueError(f'Invalid codec ID: {info.codec_id}')
    sample_rate = info.value[0] & 0xF0
    channel_mode = info.value[0] & 0x0F
    return cls(AptxSamplingRate(sample_rate), AptxChannelMode(channel_mode))


@dataclasses.dataclass(frozen=True)
class LdacCodecInformation:
  """LDAC codec information."""

  sample_rate: LdacSamplingRate
  channel_mode: LdacChannelMode

  VENDOR_ID: ClassVar[int] = 0x012D
  CODEC_ID: ClassVar[int] = 0xAA

  def __bytes__(self) -> bytes:
    return struct.pack(
        '<IHBB',
        self.VENDOR_ID,
        self.CODEC_ID,
        self.sample_rate,
        self.channel_mode,
    )

  @classmethod
  def from_vendor_info(
      cls, info: a2dp.VendorSpecificMediaCodecInformation
  ) -> Self:
    if info.vendor_id != cls.VENDOR_ID:
      raise ValueError(f'Invalid vendor ID: {info.vendor_id}')
    if info.codec_id != cls.CODEC_ID:
      raise ValueError(f'Invalid codec ID: {info.codec_id}')
    return cls(LdacSamplingRate(info.value[0]), LdacChannelMode(info.value[1]))


@enum.unique
class A2dpCodec(constants.ShortReprEnum):
  """A2DP codecs.

  Codecs are following the order of
  packages/modules/Bluetooth/android/app/res/values/config.xml
  """

  OPUS = enum.auto()
  LDAC = enum.auto()
  LHDC = enum.auto()
  APTX_HD = enum.auto()
  APTX = enum.auto()
  AAC = enum.auto()
  SBC = enum.auto()

  def get_default_capabilities(self) -> avdtp.MediaCodecCapabilities:
    match self:
      case A2dpCodec.AAC:
        return avdtp.MediaCodecCapabilities(
            media_type=avdtp.MediaType.AUDIO,
            media_codec_type=a2dp.CodecType.MPEG_2_4_AAC,
            media_codec_information=a2dp.AacMediaCodecInformation(
                object_type=(
                    a2dp.AacMediaCodecInformation.ObjectType.MPEG_2_AAC_LC
                ),
                sampling_frequency=(
                    a2dp.AacMediaCodecInformation.SamplingFrequency.SF_44100
                    | a2dp.AacMediaCodecInformation.SamplingFrequency.SF_48000
                ),
                channels=(
                    a2dp.AacMediaCodecInformation.Channels.MONO
                    | a2dp.AacMediaCodecInformation.Channels.STEREO
                ),
                vbr=1,
                bitrate=256000,
            ),
        )
      case A2dpCodec.SBC:
        return avdtp.MediaCodecCapabilities(
            media_type=avdtp.MediaType.AUDIO,
            media_codec_type=a2dp.CodecType.SBC,
            media_codec_information=a2dp.SbcMediaCodecInformation(
                sampling_frequency=(
                    a2dp.SbcMediaCodecInformation.SamplingFrequency.SF_16000
                    | a2dp.SbcMediaCodecInformation.SamplingFrequency.SF_32000
                    | a2dp.SbcMediaCodecInformation.SamplingFrequency.SF_44100
                    | a2dp.SbcMediaCodecInformation.SamplingFrequency.SF_48000
                ),
                channel_mode=(
                    a2dp.SbcMediaCodecInformation.ChannelMode.MONO
                    | a2dp.SbcMediaCodecInformation.ChannelMode.JOINT_STEREO
                    | a2dp.SbcMediaCodecInformation.ChannelMode.DUAL_CHANNEL
                    | a2dp.SbcMediaCodecInformation.ChannelMode.STEREO
                ),
                block_length=(
                    a2dp.SbcMediaCodecInformation.BlockLength.BL_4
                    | a2dp.SbcMediaCodecInformation.BlockLength.BL_8
                    | a2dp.SbcMediaCodecInformation.BlockLength.BL_12
                    | a2dp.SbcMediaCodecInformation.BlockLength.BL_16
                ),
                subbands=(
                    a2dp.SbcMediaCodecInformation.Subbands.S_4
                    | a2dp.SbcMediaCodecInformation.Subbands.S_8
                ),
                allocation_method=(
                    a2dp.SbcMediaCodecInformation.AllocationMethod.SNR
                    | a2dp.SbcMediaCodecInformation.AllocationMethod.LOUDNESS
                ),
                minimum_bitpool_value=2,
                maximum_bitpool_value=53,
            ),
        )
      case A2dpCodec.APTX:
        return avdtp.MediaCodecCapabilities(
            media_type=avdtp.MediaType.AUDIO,
            media_codec_type=a2dp.CodecType.NON_A2DP,
            media_codec_information=AptxCodecInformation(
                sample_rate=AptxSamplingRate.RATE_48000,
                channel_mode=AptxChannelMode.STEREO,
            ),
        )
      case A2dpCodec.APTX_HD:
        return avdtp.MediaCodecCapabilities(
            media_type=avdtp.MediaType.AUDIO,
            media_codec_type=a2dp.CodecType.NON_A2DP,
            media_codec_information=AptxHdCodecInformation(
                sample_rate=AptxSamplingRate.RATE_48000,
                channel_mode=AptxChannelMode.STEREO,
            ),
        )
      case A2dpCodec.LDAC:
        return avdtp.MediaCodecCapabilities(
            media_type=avdtp.MediaType.AUDIO,
            media_codec_type=a2dp.CodecType.NON_A2DP,
            media_codec_information=LdacCodecInformation(
                sample_rate=(
                    LdacSamplingRate.RATE_44100
                    | LdacSamplingRate.RATE_48000
                    | LdacSamplingRate.RATE_88200
                    | LdacSamplingRate.RATE_96000
                ),
                channel_mode=LdacChannelMode.STEREO,
            ),
        )
      case A2dpCodec.LHDC:
        return avdtp.MediaCodecCapabilities(
            media_type=avdtp.MediaType.AUDIO,
            media_codec_type=a2dp.CodecType.NON_A2DP,
            media_codec_information=LhdcCodecInformation(
                sample_rate=(
                    LhdcSamplingRate.RATE_44100
                    | LhdcSamplingRate.RATE_48000
                    | LhdcSamplingRate.RATE_96000
                ),
                bits_per_sample=(
                    LhdcBitsPerSample.BITS_16 | LhdcBitsPerSample.BITS_24
                ),
                channel_mode=LhdcChannelMode.STEREO,
            ),
        )
      case A2dpCodec.OPUS:
        return avdtp.MediaCodecCapabilities(
            media_type=avdtp.MediaType.AUDIO,
            media_codec_type=a2dp.CodecType.NON_A2DP,
            media_codec_information=a2dp.OpusMediaCodecInformation(
                sampling_frequency=a2dp.OpusMediaCodecInformation.SamplingFrequency.SF_48000,
                channel_mode=a2dp.OpusMediaCodecInformation.ChannelMode.STEREO,
                frame_size=a2dp.OpusMediaCodecInformation.FrameSize.FS_20MS,
            ),
        )

  def get_media_packet_pump(self, peer_mtu: int) -> avdtp.MediaPacketPump:
    """Returns an empty packet pump for the given codec."""

    # Empty packet source.
    # TODO: Implement valid packet source.
    async def read(size: int) -> bytes:
      del size
      return b''

    source: a2dp.SbcPacketSource | a2dp.AacPacketSource | a2dp.OpusPacketSource
    match self:
      case A2dpCodec.SBC:
        source = a2dp.SbcPacketSource(read, peer_mtu)
      case A2dpCodec.AAC:
        source = a2dp.AacPacketSource(read, peer_mtu)
      case A2dpCodec.OPUS:
        source = a2dp.OpusPacketSource(read, peer_mtu)
      case _:
        raise ValueError(f'Unsupported codec: {self}')
    return avdtp.MediaPacketPump(source.packets)

  @property
  def format(self) -> str:
    """Container format of the codec.

    Older ffmpeg doesn't support "opus" format and so we use "ogg" instead.
    """
    if self == A2dpCodec.OPUS:
      return 'ogg'
    return self.name.lower()

  @property
  def codec_id(self) -> int:
    return {
        A2dpCodec.SBC: 0,
        A2dpCodec.AAC: 0,
        A2dpCodec.APTX: AptxCodecInformation.CODEC_ID,
        A2dpCodec.APTX_HD: AptxHdCodecInformation.CODEC_ID,
        A2dpCodec.LDAC: LdacCodecInformation.CODEC_ID,
        A2dpCodec.LHDC: LhdcCodecInformation.CODEC_ID,
        A2dpCodec.OPUS: a2dp.OpusMediaCodecInformation.CODEC_ID,
    }[self]

  @property
  def vendor_id(self) -> int:
    return {
        A2dpCodec.SBC: 0,
        A2dpCodec.AAC: 0,
        A2dpCodec.APTX: AptxCodecInformation.VENDOR_ID,
        A2dpCodec.APTX_HD: AptxHdCodecInformation.VENDOR_ID,
        A2dpCodec.LDAC: LdacCodecInformation.VENDOR_ID,
        A2dpCodec.LHDC: LhdcCodecInformation.VENDOR_ID,
        A2dpCodec.OPUS: a2dp.OpusMediaCodecInformation.VENDOR_ID,
    }[self]

  @property
  def codec_type(self) -> int:
    return {
        A2dpCodec.SBC: a2dp.A2DP_SBC_CODEC_TYPE,
        A2dpCodec.AAC: a2dp.A2DP_MPEG_2_4_AAC_CODEC_TYPE,
        A2dpCodec.APTX: a2dp.CodecType.NON_A2DP,
        A2dpCodec.APTX_HD: a2dp.CodecType.NON_A2DP,
        A2dpCodec.LDAC: a2dp.CodecType.NON_A2DP,
        A2dpCodec.LHDC: a2dp.CodecType.NON_A2DP,
        A2dpCodec.OPUS: a2dp.CodecType.NON_A2DP,
    }[self]

  @property
  def android_codec_id(self) -> android_constants.BluetoothCodecId:
    return {
        A2dpCodec.SBC: android_constants.BluetoothCodecId.SBC,
        A2dpCodec.AAC: android_constants.BluetoothCodecId.AAC,
        A2dpCodec.APTX: android_constants.BluetoothCodecId.APTX,
        A2dpCodec.APTX_HD: android_constants.BluetoothCodecId.APTX_HD,
        A2dpCodec.LDAC: android_constants.BluetoothCodecId.LDAC,
        A2dpCodec.LHDC: android_constants.BluetoothCodecId.LHDC_V5,
        A2dpCodec.OPUS: android_constants.BluetoothCodecId.OPUS,
    }[self]


def select_configuration(
    codec: A2dpCodec,
    remote_capabilities: avdtp.MediaCodecCapabilities,
) -> list[avdtp.ServiceCapabilities]:
  """Selects the mutually supported codec configuration."""
  local_capabilities = codec.get_default_capabilities()
  local_info = local_capabilities.media_codec_information
  remote_info = remote_capabilities.media_codec_information

  _F = TypeVar('_F', bound=enum.IntFlag)

  def select_highest_flag(flags: int, priority_list: Sequence[_F]) -> _F:
    for flag in priority_list:
      if flags & flag:
        return flag
    raise ValueError(f'No common capabilities found in {flags}')

  match local_info:
    case a2dp.AacMediaCodecInformation():
      if not isinstance(remote_info, a2dp.AacMediaCodecInformation):
        raise TypeError('Incompatible remote capabilities for AAC')
      return [
          avdtp.ServiceCapabilities(
              service_category=avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY
          ),
          avdtp.MediaCodecCapabilities(
              media_type=avdtp.MediaType.AUDIO,
              media_codec_type=a2dp.CodecType.MPEG_2_4_AAC,
              media_codec_information=a2dp.AacMediaCodecInformation(
                  object_type=select_highest_flag(
                      local_info.object_type & remote_info.object_type,
                      [
                          a2dp.AacMediaCodecInformation.ObjectType.MPEG_2_AAC_LC,
                          a2dp.AacMediaCodecInformation.ObjectType.MPEG_4_AAC_LC,
                          a2dp.AacMediaCodecInformation.ObjectType.MPEG_4_AAC_LTP,
                          a2dp.AacMediaCodecInformation.ObjectType.MPEG_4_AAC_SCALABLE,
                      ],
                  ),
                  sampling_frequency=select_highest_flag(
                      local_info.sampling_frequency
                      & remote_info.sampling_frequency,
                      [
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_44100,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_48000,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_88200,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_96000,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_32000,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_24000,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_22050,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_16000,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_12000,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_11025,
                          a2dp.AacMediaCodecInformation.SamplingFrequency.SF_8000,
                      ],
                  ),
                  channels=select_highest_flag(
                      local_info.channels & remote_info.channels,
                      [
                          a2dp.AacMediaCodecInformation.Channels.STEREO,
                          a2dp.AacMediaCodecInformation.Channels.MONO,
                      ],
                  ),
                  vbr=local_info.vbr & remote_info.vbr,
                  bitrate=min(local_info.bitrate, remote_info.bitrate),
              ),
          ),
      ]
    case a2dp.SbcMediaCodecInformation():
      if not isinstance(remote_info, a2dp.SbcMediaCodecInformation):
        raise TypeError('Incompatible remote capabilities for SBC')
      return [
          avdtp.ServiceCapabilities(
              service_category=avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY
          ),
          avdtp.MediaCodecCapabilities(
              media_type=avdtp.MediaType.AUDIO,
              media_codec_type=a2dp.CodecType.SBC,
              media_codec_information=a2dp.SbcMediaCodecInformation(
                  sampling_frequency=select_highest_flag(
                      local_info.sampling_frequency
                      & remote_info.sampling_frequency,
                      [
                          a2dp.SbcMediaCodecInformation.SamplingFrequency.SF_44100,
                          a2dp.SbcMediaCodecInformation.SamplingFrequency.SF_48000,
                          a2dp.SbcMediaCodecInformation.SamplingFrequency.SF_32000,
                          a2dp.SbcMediaCodecInformation.SamplingFrequency.SF_16000,
                      ],
                  ),
                  channel_mode=select_highest_flag(
                      local_info.channel_mode & remote_info.channel_mode,
                      [
                          a2dp.SbcMediaCodecInformation.ChannelMode.JOINT_STEREO,
                          a2dp.SbcMediaCodecInformation.ChannelMode.STEREO,
                          a2dp.SbcMediaCodecInformation.ChannelMode.DUAL_CHANNEL,
                          a2dp.SbcMediaCodecInformation.ChannelMode.MONO,
                      ],
                  ),
                  block_length=select_highest_flag(
                      local_info.block_length & remote_info.block_length,
                      [
                          a2dp.SbcMediaCodecInformation.BlockLength.BL_16,
                          a2dp.SbcMediaCodecInformation.BlockLength.BL_12,
                          a2dp.SbcMediaCodecInformation.BlockLength.BL_8,
                          a2dp.SbcMediaCodecInformation.BlockLength.BL_4,
                      ],
                  ),
                  subbands=select_highest_flag(
                      local_info.subbands & remote_info.subbands,
                      [
                          a2dp.SbcMediaCodecInformation.Subbands.S_8,
                          a2dp.SbcMediaCodecInformation.Subbands.S_4,
                      ],
                  ),
                  allocation_method=select_highest_flag(
                      local_info.allocation_method
                      & remote_info.allocation_method,
                      [
                          a2dp.SbcMediaCodecInformation.AllocationMethod.LOUDNESS,
                          a2dp.SbcMediaCodecInformation.AllocationMethod.SNR,
                      ],
                  ),
                  minimum_bitpool_value=max(
                      local_info.minimum_bitpool_value,
                      remote_info.minimum_bitpool_value,
                  ),
                  maximum_bitpool_value=min(
                      local_info.maximum_bitpool_value,
                      remote_info.maximum_bitpool_value,
                  ),
              ),
          ),
      ]
    case AptxCodecInformation():
      if isinstance(remote_info, a2dp.VendorSpecificMediaCodecInformation):
        remote_info = AptxCodecInformation.from_vendor_info(remote_info)
      elif not isinstance(remote_info, AptxCodecInformation):
        raise TypeError('Incompatible remote capabilities for APTX')
      return [
          avdtp.ServiceCapabilities(
              service_category=avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY
          ),
          avdtp.MediaCodecCapabilities(
              media_type=avdtp.MediaType.AUDIO,
              media_codec_type=a2dp.CodecType.NON_A2DP,
              media_codec_information=AptxCodecInformation(
                  sample_rate=AptxSamplingRate(
                      select_highest_flag(
                          local_info.sample_rate & remote_info.sample_rate,
                          [
                              AptxSamplingRate.RATE_44100,
                              AptxSamplingRate.RATE_48000,
                          ],
                      )
                  ),
                  channel_mode=AptxChannelMode(
                      select_highest_flag(
                          local_info.channel_mode & remote_info.channel_mode,
                          [AptxChannelMode.STEREO, AptxChannelMode.MONO],
                      )
                  ),
              ),
          ),
      ]
    case AptxHdCodecInformation():
      if isinstance(remote_info, a2dp.VendorSpecificMediaCodecInformation):
        remote_info = AptxHdCodecInformation.from_vendor_info(remote_info)
      elif not isinstance(remote_info, AptxHdCodecInformation):
        raise TypeError('Incompatible remote capabilities for APTX-HD')
      return [
          avdtp.ServiceCapabilities(
              service_category=avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY
          ),
          avdtp.MediaCodecCapabilities(
              media_type=avdtp.MediaType.AUDIO,
              media_codec_type=a2dp.CodecType.NON_A2DP,
              media_codec_information=AptxHdCodecInformation(
                  sample_rate=AptxSamplingRate(
                      select_highest_flag(
                          local_info.sample_rate & remote_info.sample_rate,
                          [
                              AptxSamplingRate.RATE_44100,
                              AptxSamplingRate.RATE_48000,
                          ],
                      )
                  ),
                  channel_mode=AptxChannelMode(
                      select_highest_flag(
                          local_info.channel_mode & remote_info.channel_mode,
                          [AptxChannelMode.STEREO, AptxChannelMode.MONO],
                      )
                  ),
              ),
          ),
      ]
    case LdacCodecInformation():
      if isinstance(remote_info, a2dp.VendorSpecificMediaCodecInformation):
        remote_info = LdacCodecInformation.from_vendor_info(remote_info)
      elif not isinstance(remote_info, LdacCodecInformation):
        raise TypeError('Incompatible remote capabilities for LDAC')
      return [
          avdtp.ServiceCapabilities(
              service_category=avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY
          ),
          avdtp.MediaCodecCapabilities(
              media_type=avdtp.MediaType.AUDIO,
              media_codec_type=a2dp.CodecType.NON_A2DP,
              media_codec_information=LdacCodecInformation(
                  sample_rate=LdacSamplingRate(
                      select_highest_flag(
                          local_info.sample_rate & remote_info.sample_rate,
                          [
                              LdacSamplingRate.RATE_96000,
                              LdacSamplingRate.RATE_88200,
                              LdacSamplingRate.RATE_48000,
                              LdacSamplingRate.RATE_44100,
                              LdacSamplingRate.RATE_192000,
                              LdacSamplingRate.RATE_176400,
                          ],
                      )
                  ),
                  channel_mode=LdacChannelMode(
                      select_highest_flag(
                          local_info.channel_mode & remote_info.channel_mode,
                          [
                              LdacChannelMode.STEREO,
                              LdacChannelMode.DUAL,
                              LdacChannelMode.MONO,
                          ],
                      )
                  ),
              ),
          ),
      ]
    case LhdcCodecInformation():
      if isinstance(remote_info, a2dp.VendorSpecificMediaCodecInformation):
        remote_info = LhdcCodecInformation.from_vendor_info(remote_info)
      elif not isinstance(remote_info, LhdcCodecInformation):
        raise TypeError('Incompatible remote capabilities for LHDC')
      return [
          avdtp.ServiceCapabilities(
              service_category=avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY
          ),
          avdtp.MediaCodecCapabilities(
              media_type=avdtp.MediaType.AUDIO,
              media_codec_type=a2dp.CodecType.NON_A2DP,
              media_codec_information=LhdcCodecInformation(
                  sample_rate=LhdcSamplingRate(
                      select_highest_flag(
                          local_info.sample_rate & remote_info.sample_rate,
                          [
                              LhdcSamplingRate.RATE_44100,
                              LhdcSamplingRate.RATE_48000,
                              LhdcSamplingRate.RATE_96000,
                              LhdcSamplingRate.RATE_192000,
                          ],
                      )
                  ),
                  bits_per_sample=LhdcBitsPerSample(
                      select_highest_flag(
                          local_info.bits_per_sample
                          & remote_info.bits_per_sample,
                          [
                              LhdcBitsPerSample.BITS_16,
                              LhdcBitsPerSample.BITS_24,
                              LhdcBitsPerSample.BITS_32,
                          ],
                      )
                  ),
                  channel_mode=LhdcChannelMode.STEREO,
                  version=local_info.version,
                  frame_len_type=local_info.frame_len_type,
                  max_target_bitrate=local_info.max_target_bitrate,
                  min_target_bitrate=local_info.min_target_bitrate,
                  has_feature_ll=local_info.has_feature_ll
                  & remote_info.has_feature_ll,
              ),
          ),
      ]
    case a2dp.OpusMediaCodecInformation():
      if not isinstance(remote_info, a2dp.OpusMediaCodecInformation):
        raise TypeError('Incompatible remote capabilities for OPUS')
      return [
          avdtp.ServiceCapabilities(
              service_category=avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY
          ),
          avdtp.MediaCodecCapabilities(
              media_type=avdtp.MediaType.AUDIO,
              media_codec_type=a2dp.CodecType.NON_A2DP,
              media_codec_information=a2dp.OpusMediaCodecInformation(
                  sampling_frequency=select_highest_flag(
                      local_info.sampling_frequency
                      & remote_info.sampling_frequency,
                      [
                          a2dp.OpusMediaCodecInformation.SamplingFrequency.SF_48000,
                      ],
                  ),
                  channel_mode=select_highest_flag(
                      local_info.channel_mode & remote_info.channel_mode,
                      [
                          a2dp.OpusMediaCodecInformation.ChannelMode.STEREO,
                          a2dp.OpusMediaCodecInformation.ChannelMode.MONO,
                      ],
                  ),
                  frame_size=select_highest_flag(
                      local_info.frame_size & remote_info.frame_size,
                      [
                          a2dp.OpusMediaCodecInformation.FrameSize.FS_20MS,
                          a2dp.OpusMediaCodecInformation.FrameSize.FS_10MS,
                      ],
                  ),
              ),
          ),
      ]
    case _:
      raise ValueError(f'Unsupported codec info: {local_info!r}')


class LocalSinkWrapper:
  """Wrapper for LocalSink to provide start/suspend events."""

  def __init__(self, impl: avdtp.LocalSink):
    self.impl = impl
    self.condition = asyncio.Condition()
    for command in (
        impl.EVENT_CONFIGURATION,
        impl.EVENT_OPEN,
        impl.EVENT_START,
        impl.EVENT_SUSPEND,
        impl.EVENT_CLOSE,
        impl.EVENT_ABORT,
    ):
      self.impl.on(command, self._on_command)

  async def _on_command(self) -> None:
    async with self.condition:
      self.condition.notify_all()

  @property
  def stream_state(self) -> int | None:
    return self.impl.stream.state if self.impl.stream else None


def register_sink_buffer(
    sink: avdtp.LocalSink, codec: A2dpCodec
) -> bytearray | None:
  """Registers the sink buffer to receive the packets.

  Args:
    sink: The sink to register the buffer to.
    codec: The codec of the sink.

  Returns:
    The sink buffer, or None if the codec is not supported.
  """
  buffer = bytearray()
  match codec:
    case A2dpCodec.SBC | A2dpCodec.LDAC:

      @sink.on(avdtp.LocalSink.EVENT_RTP_PACKET)
      def on_rtp_packet_sbc_ldac(packet: avdtp.MediaPacket) -> None:
        buffer.extend(packet.payload[1:])

    case A2dpCodec.LHDC:

      @sink.on(avdtp.LocalSink.EVENT_RTP_PACKET)
      def on_rtp_packet_lhdc(packet: avdtp.MediaPacket) -> None:
        buffer.extend(packet.payload)

    case A2dpCodec.AAC:

      @sink.on(avdtp.LocalSink.EVENT_RTP_PACKET)
      def on_rtp_packet_aac(packet: avdtp.MediaPacket) -> None:
        buffer.extend(
            codecs.AacAudioRtpPacket.from_bytes(packet.payload).to_adts()
        )

    case A2dpCodec.APTX:

      def on_avdtp_packet(packet: bytes) -> None:
        buffer.extend(packet)

      sink.on_avdtp_packet = on_avdtp_packet  # type: ignore[method-assign]
      if sink.stream and sink.stream.rtp_channel:
        sink.stream.rtp_channel.sink = sink.on_avdtp_packet

    case A2dpCodec.APTX_HD:

      @sink.on(avdtp.LocalSink.EVENT_RTP_PACKET)
      def on_rtp_packet_aptx_hd(packet: avdtp.MediaPacket) -> None:
        buffer.extend(packet.payload)

    case A2dpCodec.OPUS:

      # https://datatracker.ietf.org/doc/html/rfc7845#section-3
      # First page must be the ID header.
      buffer.extend(
          ogg.Page(
              # Change this when we support other codec configurations.
              payload=ogg.OpusIdHeader(sample_rate=48000, channel_count=2),
              header_type=ogg.Page.HeaderType.IS_FIRST_PAGE,
              page_sequence_number=0,
          ).to_bytes()
      )
      # Second page must be the comment header. It can be empty.
      buffer.extend(
          ogg.Page(
              payload=ogg.OpusCommentHeader(),
              page_sequence_number=1,
          ).to_bytes()
      )
      page_sequence_number = 2

      @sink.on(avdtp.LocalSink.EVENT_RTP_PACKET)
      def on_rtp_packet_opus(packet: avdtp.MediaPacket) -> None:
        nonlocal page_sequence_number
        buffer.extend(
            ogg.Page(
                payload=packet.payload[1:],
                page_sequence_number=page_sequence_number,
            ).to_bytes()
        )
        page_sequence_number += 1

    case _:
      # Unexpected codec or no decoder.
      return None
  return buffer


def _endpoint_supports_codec(
    endpoint: avdtp.LocalStreamEndPoint,
    codec_type: int,
    vendor_id: int = 0,
    codec_id: int = 0,
) -> bool:
  """Checks if the endpoint supports the codec."""
  for capability in endpoint.capabilities:
    if not (
        isinstance(capability, avdtp.MediaCodecCapabilities)
        and capability.media_type == avdtp.MediaType.AUDIO
        and capability.media_codec_type == codec_type
    ):
      continue
    codec_info = capability.media_codec_information
    if not isinstance(codec_info, a2dp.VendorSpecificMediaCodecInformation) or (
        codec_info.vendor_id == vendor_id and codec_info.codec_id == codec_id
    ):
      return True
  return False


_ENDPOINT = TypeVar('_ENDPOINT', bound=avdtp.LocalStreamEndPoint)


def find_local_endpoints_by_codec(
    protocol: avdtp.Protocol,
    codec_type: int,
    endpoint_type: type[_ENDPOINT],
    vendor_id: int = 0,
    codec_id: int = 0,
) -> list[_ENDPOINT]:
  """Finds the local source by codec type and vendor/codec ID."""
  return [
      endpoint
      for endpoint in protocol.local_endpoints
      if isinstance(endpoint, endpoint_type)
      and _endpoint_supports_codec(endpoint, codec_type, vendor_id, codec_id)
  ]


def setup_sink_server(
    device: device_lib.Device,
    supported_capabilities: Sequence[avdtp.MediaCodecCapabilities],
    a2dp_sink_handle: int,
    *,
    protocol_factory: Callable[..., avdtp.Protocol] | None = None,
) -> avdtp_ext.Listener:
  """Sets up the sink server on the device.

  Args:
    device: The device to set up the sink server on.
    supported_capabilities: The capabilities of the sink server.
    a2dp_sink_handle: The handle of the A2DP sink service record.
    protocol_factory: Factory function or class for creating the AVDTP protocol
      instance.

  Returns:
    The AVDTP listener.
  """
  listener = avdtp_ext.Listener.for_device(
      device, protocol_factory=protocol_factory
  )

  @listener.on(listener.EVENT_CONNECTION)
  def _(server: avdtp_ext.Protocol) -> None:
    for capability in supported_capabilities:
      server.add_sink(capability)

  device.sdp_service_records.update({
      a2dp_sink_handle: (
          SinkSdpRecord(
              service_record_handle=a2dp_sink_handle
          ).to_service_attributes()
      ),
  })
  return listener


@dataclasses.dataclass
class SourceSdpRecord:
  """A2DP source SDP record."""

  class Features(enum.IntFlag):
    """A2DP source SDP record features."""

    PLAYER = 0x01
    MICROPHONE = 0x02
    TUNER = 0x04
    MIXER = 0x08

  service_record_handle: int
  avdtp_version: tuple[int, int] = (1, 3)
  a2dp_version: tuple[int, int] = (1, 3)
  supported_features: Features | None = None

  def to_service_attributes(self) -> list[sdp.ServiceAttribute]:
    """Converts the SDP record to a list of SDP service attributes.

    The record exposes the features supported in the input configuration,
    and the allocated RFCOMM channel.

    Returns:
      A list of SDP service attributes.
    """
    attributes = [
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_32(self.service_record_handle),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_BROWSE_GROUP_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence(
                [sdp.DataElement.uuid(sdp.SDP_PUBLIC_BROWSE_ROOT)]
            ),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence(
                [sdp.DataElement.uuid(core.BT_AUDIO_SOURCE_SERVICE)]
            ),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID),
                    sdp.DataElement.unsigned_integer_16(avdtp.AVDTP_PSM),
                ]),
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_AVDTP_PROTOCOL_ID),
                    sdp.DataElement.unsigned_integer_16(
                        self.avdtp_version[0] << 8 | self.avdtp_version[1]
                    ),
                ]),
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(
                        core.BT_ADVANCED_AUDIO_DISTRIBUTION_SERVICE
                    ),
                    sdp.DataElement.unsigned_integer_16(
                        self.a2dp_version[0] << 8 | self.a2dp_version[1]
                    ),
                ])
            ]),
        ),
    ]
    if self.supported_features is not None:
      attributes.append(
          sdp.ServiceAttribute(
              sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID,
              sdp.DataElement.unsigned_integer_16(self.supported_features),
          )
      )
    return attributes

  @classmethod
  async def find(
      cls,
      connection: device_lib.Connection,
  ) -> list[SourceSdpRecord]:
    """Searches for A2DP source SDP records from remote device.

    Args:
        connection: ACL connection to make SDP search.

    Returns:
        A list of A2DP source SDP records.
    """
    records: list[SourceSdpRecord] = []
    async with sdp.Client(connection) as sdp_client:
      search_result = await sdp_client.search_attributes(
          uuids=[core.BT_AUDIO_SOURCE_SERVICE],
          attribute_ids=[
              sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
              sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID,
              sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
          ],
      )
      for attribute_lists in search_result:
        avdtp_version: tuple[int, int] | None = None
        a2dp_version: tuple[int, int] | None = None
        service_record_handle: int | None = None
        features: SourceSdpRecord.Features | None = None
        for attribute in attribute_lists:
          match attribute.id:
            case sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID:
              service_record_handle = attribute.value.value
            case sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID:
              profile_descriptor_list = attribute.value.value
              a2dp_version = (
                  profile_descriptor_list[0].value[1].value >> 8,
                  profile_descriptor_list[0].value[1].value & 0xFF,
              )
            case sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID:
              protocol_descriptor_list = attribute.value.value
              avdtp_version = (
                  protocol_descriptor_list[1].value[1].value >> 8,
                  protocol_descriptor_list[1].value[1].value & 0xFF,
              )
            case sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID:
              features = SourceSdpRecord.Features(attribute.value.value)

        if (
            avdtp_version is None
            or a2dp_version is None
            or service_record_handle is None
        ):
          continue
        records.append(
            cls(
                service_record_handle=service_record_handle,
                avdtp_version=avdtp_version,
                a2dp_version=a2dp_version,
                supported_features=features,
            )
        )
    return records


@dataclasses.dataclass
class SinkSdpRecord:
  """A2DP sink SDP record."""

  class Features(enum.IntFlag):
    """A2DP sink SDP record features."""

    HEADPHONE = 0x01
    SPEAKER = 0x02
    RECORDER = 0x04
    AMPLIFIER = 0x08

  service_record_handle: int
  avdtp_version: tuple[int, int] = (1, 3)
  a2dp_version: tuple[int, int] = (1, 3)
  supported_features: Features | None = None

  def to_service_attributes(self) -> list[sdp.ServiceAttribute]:
    """Converts the SDP record to a list of SDP service attributes.

    The record exposes the features supported in the input configuration,
    and the allocated RFCOMM channel.

    Returns:
      A list of SDP service attributes.
    """
    attributes = [
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_32(self.service_record_handle),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence(
                [sdp.DataElement.uuid(core.BT_AUDIO_SINK_SERVICE)]
            ),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID),
                    sdp.DataElement.unsigned_integer_16(avdtp.AVDTP_PSM),
                ]),
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_AVDTP_PROTOCOL_ID),
                    sdp.DataElement.unsigned_integer_16(
                        self.avdtp_version[0] << 8 | self.avdtp_version[1]
                    ),
                ]),
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(
                        core.BT_ADVANCED_AUDIO_DISTRIBUTION_SERVICE
                    ),
                    sdp.DataElement.unsigned_integer_16(
                        self.a2dp_version[0] << 8 | self.a2dp_version[1]
                    ),
                ])
            ]),
        ),
    ]
    if self.supported_features is not None:
      attributes.append(
          sdp.ServiceAttribute(
              sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID,
              sdp.DataElement.unsigned_integer_16(self.supported_features),
          )
      )
    return attributes

  @classmethod
  async def find(
      cls,
      connection: device_lib.Connection,
  ) -> list[SinkSdpRecord]:
    """Searches for A2DP sink SDP records from remote device.

    Args:
        connection: ACL connection to make SDP search.

    Returns:
        A list of A2DP source SDP records.
    """
    records: list[SinkSdpRecord] = []
    async with sdp.Client(connection) as sdp_client:
      search_result = await sdp_client.search_attributes(
          uuids=[core.BT_AUDIO_SINK_SERVICE],
          attribute_ids=[
              sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
              sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID,
              sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
          ],
      )
      for attribute_lists in search_result:
        avdtp_version: tuple[int, int] | None = None
        a2dp_version: tuple[int, int] | None = None
        service_record_handle: int | None = None
        features: SinkSdpRecord.Features | None = None
        for attribute in attribute_lists:
          match attribute.id:
            case sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID:
              service_record_handle = attribute.value.value
            case sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID:
              profile_descriptor_list = attribute.value.value
              a2dp_version = (
                  profile_descriptor_list[0].value[1].value >> 8,
                  profile_descriptor_list[0].value[1].value & 0xFF,
              )
            case sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID:
              protocol_descriptor_list = attribute.value.value
              avdtp_version = (
                  protocol_descriptor_list[1].value[1].value >> 8,
                  protocol_descriptor_list[1].value[1].value & 0xFF,
              )
            case sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID:
              features = SinkSdpRecord.Features(attribute.value.value)

        if (
            avdtp_version is None
            or a2dp_version is None
            or service_record_handle is None
        ):
          continue
        records.append(
            cls(
                service_record_handle=service_record_handle,
                avdtp_version=avdtp_version,
                a2dp_version=a2dp_version,
                supported_features=features,
            )
        )
    return records
