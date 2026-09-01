/*
 * Copyright 2026 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.google.wireless.android.pixel.bluetooth.snippet

import android.bluetooth.BluetoothCodecConfig
import android.bluetooth.BluetoothCodecType
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothHidDeviceAppSdpSettings
import android.bluetooth.BluetoothQualityReport
import android.bluetooth.BluetoothSocket
import android.bluetooth.BluetoothSocketSettings
import android.bluetooth.OobData
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.bluetooth.le.AdvertisingSetParameters
import android.bluetooth.le.PeriodicAdvertisingParameters
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.net.Uri
import android.os.Build
import android.os.ParcelUuid
import android.telecom.Call
import android.telecom.CallEndpoint
import android.util.Base64
import androidx.core.util.forEach
import androidx.media3.common.AudioAttributes
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.session.legacy.MediaBrowserCompat
import com.google.android.mobly.snippet.SnippetObjectConverter
import com.google.android.mobly.snippet.uiautomator.Converter as UiAutomatorObjectConverter
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.getOrNull
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.toList
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.toMap
import java.lang.reflect.Type
import java.util.UUID
import org.json.JSONArray
import org.json.JSONObject

/** Converter converts between Android objects and JSON objects. */
@Suppress("DEPRECATION")
class JsonObjectConverter : SnippetObjectConverter {
  val uiAutomatorObjectConverter = UiAutomatorObjectConverter()

  private fun BluetoothGattDescriptor.toJson() =
    JSONObject().also {
      it.put(SnippetConstants.FIELD_UUID, this.uuid.toString())
      it.put(SnippetConstants.GATT_FIELD_PERMISSIONS, this.permissions)
    }

  private fun BluetoothGattCharacteristic.toJson() =
    JSONObject().also {
      it.put(SnippetConstants.FIELD_HANDLE, this.instanceId)
      it.put(SnippetConstants.FIELD_UUID, this.uuid.toString())
      it.put(SnippetConstants.GATT_FIELD_PERMISSIONS, this.permissions)
      it.put(SnippetConstants.GATT_FIELD_PROPERTIES, this.properties)
      it.put(
        SnippetConstants.GATT_FIELD_DESCRIPTORS,
        JSONArray(this.descriptors.map { descriptor -> descriptor.toJson() }),
      )
    }

  private fun BluetoothGattService.toJson(): JSONObject =
    JSONObject().also {
      it.put(SnippetConstants.FIELD_HANDLE, this.instanceId)
      it.put(SnippetConstants.FIELD_UUID, this.uuid.toString())
      it.put(SnippetConstants.GATT_FIELD_TYPE, this.type)
      it.put(
        SnippetConstants.GATT_FIELD_CHARACTERISTICS,
        JSONArray(this.characteristics.map { characteristic -> characteristic.toJson() }),
      )
      it.put(
        SnippetConstants.GATT_FIELD_INCLUDED_SERVICES,
        JSONArray(this.includedServices.map { service -> service.toJson() }),
      )
    }

  private fun Call.toJson() =
    JSONObject().also {
      it.put(SnippetConstants.FIELD_STATE, this.details.state)
      it.put(SnippetConstants.FIELD_HANDLE, this.details.handle)
      it.put(SnippetConstants.FIELD_NAME, this.details.callerDisplayName)
    }

  private fun ScanResult.toJson(): JSONObject =
    JSONObject().apply {
      put(SnippetConstants.FIELD_DEVICE, device?.address)
      put(SnippetConstants.ADV_PARAMETER_PRIMARY_PHY, primaryPhy)
      put(SnippetConstants.ADV_PARAMETER_SECONDARY_PHY, secondaryPhy)
      put(SnippetConstants.ADV_REPORT_SID, advertisingSid)
      put(SnippetConstants.ADV_PARAMETER_TX_POWER_LEVEL, txPower)
      put(SnippetConstants.ADV_REPORT_RSSI, rssi)
      put(SnippetConstants.ADV_REPORT_PA_INTERVAL, periodicAdvertisingInterval)
      put(SnippetConstants.ADV_REPORT_TIMESTAMP, timestampNanos)

      scanRecord?.let {
        put(SnippetConstants.ADV_DATA_FLAGS, it.advertiseFlags)
        put(SnippetConstants.FIELD_NAME, it.deviceName)
        it.serviceUuids
          ?.map { uuid -> uuid.toString() }
          ?.let { uuids -> put(SnippetConstants.ADV_DATA_SERVICE_UUID, JSONArray(uuids)) }
        it.serviceData
          ?.map { (uuid, data) -> uuid.toString() to Base64.encodeToString(data, Base64.NO_WRAP) }
          ?.toMap()
          ?.let { data -> put(SnippetConstants.ADV_DATA_SERVICE_DATA, JSONObject(data)) }
        put(
          SnippetConstants.ADV_DATA_SERVICE_SOLICITATION_UUIDS,
          JSONArray(it.serviceSolicitationUuids.map { uuid -> uuid.toString() }),
        )
        it.manufacturerSpecificData?.let { manufacturerSpecificData ->
          val jsonManufacturerSpecificData = JSONObject()
          // SparseArray cannot be mapped, so we must take all of them out first.
          manufacturerSpecificData.forEach { key, value ->
            jsonManufacturerSpecificData.put(
              key.toString(),
              Base64.encodeToString(value, Base64.NO_WRAP),
            )
          }
          put(SnippetConstants.ADV_DATA_MANUFACTURER_DATA, jsonManufacturerSpecificData)
        }
      }
    }

  private fun BluetoothQualityReport.toJson(): JSONObject =
    JSONObject().apply {
      put(SnippetConstants.FIELD_ID, qualityReportId)
      bqrCommon?.let {
        val commonObj = JSONObject()
        commonObj.put(SnippetConstants.PACKET_TYPE, it.packetType)
        commonObj.put(SnippetConstants.CONNECTION_HANDLE, it.connectionHandle)
        commonObj.put(SnippetConstants.CONNECTION_ROLE, it.connectionRole)
        commonObj.put(SnippetConstants.TX_POWER_LEVEL, it.txPowerLevel)
        commonObj.put(SnippetConstants.RSSI, it.rssi)
        commonObj.put(SnippetConstants.SNR, it.snr)
        commonObj.put(SnippetConstants.UNUSED_AFH_CHANNEL_COUNT, it.unusedAfhChannelCount)
        commonObj.put(
          SnippetConstants.AFH_SELECT_UNIDEAL_CHANNEL_COUNT,
          it.afhSelectUnidealChannelCount,
        )
        commonObj.put(SnippetConstants.LSTO, it.lsto)
        commonObj.put(SnippetConstants.PICONET_CLOCK, it.piconetClock)
        commonObj.put(SnippetConstants.RETRANSMISSION_COUNT, it.retransmissionCount)
        commonObj.put(SnippetConstants.NO_RX_COUNT, it.noRxCount)
        commonObj.put(SnippetConstants.NAK_COUNT, it.nakCount)
        commonObj.put(SnippetConstants.LAST_TX_ACK_TIMESTAMP, it.lastTxAckTimestamp)
        commonObj.put(SnippetConstants.FLOW_OFF_COUNT, it.flowOffCount)
        commonObj.put(SnippetConstants.LAST_FLOW_ON_TIMESTAMP, it.lastFlowOnTimestamp)
        commonObj.put(SnippetConstants.OVERFLOW_COUNT, it.overflowCount)
        commonObj.put(SnippetConstants.UNDERFLOW_COUNT, it.underflowCount)
        commonObj.put(SnippetConstants.CAL_FAILED_ITEM_COUNT, it.calFailedItemCount)
        // V6 fields
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
          commonObj.put(SnippetConstants.TX_TOTAL_PACKETS, it.txTotalPackets)
          commonObj.put(SnippetConstants.TX_UNACK_PACKETS, it.txUnackPackets)
          commonObj.put(SnippetConstants.TX_FLUSH_PACKETS, it.txFlushPackets)
          commonObj.put(SnippetConstants.TX_LAST_SUBEVENT_PACKETS, it.txLastSubeventPackets)
          commonObj.put(SnippetConstants.CRC_ERROR_PACKETS, it.crcErrorPackets)
          commonObj.put(SnippetConstants.RX_DUP_PACKETS, it.rxDupPackets)
          commonObj.put(SnippetConstants.RX_UN_RECV_PACKETS, it.rxUnRecvPackets)
          commonObj.put(SnippetConstants.COEX_INFO_MASK, it.coexInfoMask)
        }
        put(SnippetConstants.FIELD_COMMON, commonObj)
      }
    }

  private fun OobData.toJson(): JSONObject =
    JSONObject().apply {
      put(
        SnippetConstants.OOB_DATA_CONFIRMATION_HASH,
        JSONArray(confirmationHash.map { it.toInt() and 0xFF }),
      )
      put(
        SnippetConstants.OOB_DATA_RANDOMIZER_HASH,
        JSONArray(randomizerHash.map { it.toInt() and 0xFF }),
      )
      if (leTemporaryKey != null) {
        this.put(
          SnippetConstants.OOB_DATA_LE_TEMPORARY_KEY,
          JSONArray(leTemporaryKey?.map { byte -> byte.toInt() and 0xFF }),
        )
      }
      put(
        SnippetConstants.OOB_DATA_DEVICE_ADDRESS_WITH_TYPE,
        JSONArray(deviceAddressWithType.map { it.toInt() and 0xFF }),
      )
    }

  private fun CallEndpoint.toJson(): JSONObject =
    JSONObject().apply {
      put(SnippetConstants.FIELD_TYPE, this@toJson.endpointType)
      put(SnippetConstants.FIELD_ID, this@toJson.identifier.toString())
      put(SnippetConstants.FIELD_NAME, this@toJson.endpointName)
    }

  private fun JSONObject.toAdvertiseSettings() =
    AdvertiseSettings.Builder()
      .apply {
        setConnectable(optBoolean(SnippetConstants.ADV_PARAMETER_CONNECTABLE, true))
        setOwnAddressType(
          optInt(
            SnippetConstants.ADV_PARAMETER_OWN_ADDRESS_TYPE,
            AdvertisingSetParameters.ADDRESS_TYPE_RANDOM,
          )
        )
        setTimeout(optInt(SnippetConstants.ADV_PARAMETER_TIMEOUT, 0))
        setTxPowerLevel(
          optInt(
            SnippetConstants.ADV_PARAMETER_TX_POWER_LEVEL,
            AdvertiseSettings.ADVERTISE_TX_POWER_MEDIUM,
          )
        )
        setAdvertiseMode(
          optInt(
            SnippetConstants.ADV_PARAMETER_ADVERTISE_MODE,
            AdvertiseSettings.ADVERTISE_MODE_LOW_POWER,
          )
        )
        if (Build.VERSION.SDK_INT >= 34) {
          setDiscoverable(optBoolean(SnippetConstants.ADV_PARAMETER_DISCOVERABLE, true))
        }
      }
      .build()

  private fun JSONObject.toAdvertiseData() =
    AdvertiseData.Builder()
      .apply {
        setIncludeDeviceName(optBoolean(SnippetConstants.ADV_DATA_INCLUDE_DEVICE_NAME, true))
        setIncludeTxPowerLevel(optBoolean(SnippetConstants.ADV_DATA_INCLUDE_TX_POWER_LEVEL, true))
        optJSONArray(SnippetConstants.ADV_DATA_SERVICE_UUID)?.toList<String>()?.forEach {
          addServiceUuid(ParcelUuid.fromString(it))
        }
        optJSONArray(SnippetConstants.ADV_DATA_SERVICE_SOLICITATION_UUIDS)
          ?.toList<String>()
          ?.forEach { addServiceSolicitationUuid(ParcelUuid.fromString(it)) }
        optJSONObject(SnippetConstants.ADV_DATA_MANUFACTURER_DATA)
          ?.toMap<String, JSONArray>()
          ?.forEach { (id, data) ->
            addManufacturerData(id.toInt(), data.toList<Byte>().toByteArray())
          }
        optJSONObject(SnippetConstants.ADV_DATA_SERVICE_DATA)
          ?.toMap<String, JSONArray>()
          ?.forEach { (uuid, data) ->
            addServiceData(ParcelUuid.fromString(uuid), data.toList<Byte>().toByteArray())
          }
      }
      .build()

  private fun JSONObject.toAdvertisingSetParameters() =
    AdvertisingSetParameters.Builder()
      .apply {
        setConnectable(optBoolean(SnippetConstants.ADV_PARAMETER_CONNECTABLE, false))
        setAnonymous(optBoolean(SnippetConstants.ADV_PARAMETER_ANONYMOUS, false))
        setIncludeTxPower(optBoolean(SnippetConstants.ADV_DATA_INCLUDE_TX_POWER_LEVEL, false))
        setScannable(optBoolean(SnippetConstants.ADV_PARAMETER_SCANNABLE, false))
        setLegacyMode(optBoolean(SnippetConstants.ADV_PARAMETER_LEGACY, false))
        setInterval(
          optInt(SnippetConstants.ADV_PARAMETER_INTERVAL, AdvertisingSetParameters.INTERVAL_LOW)
        )
        setOwnAddressType(
          optInt(
            SnippetConstants.ADV_PARAMETER_OWN_ADDRESS_TYPE,
            AdvertisingSetParameters.ADDRESS_TYPE_DEFAULT,
          )
        )
        setPrimaryPhy(optInt(SnippetConstants.ADV_PARAMETER_PRIMARY_PHY, BluetoothDevice.PHY_LE_1M))
        setSecondaryPhy(
          optInt(SnippetConstants.ADV_PARAMETER_SECONDARY_PHY, BluetoothDevice.PHY_LE_1M)
        )
        setTxPowerLevel(
          optInt(
            SnippetConstants.ADV_PARAMETER_TX_POWER_LEVEL,
            AdvertisingSetParameters.TX_POWER_MEDIUM,
          )
        )
        if (Build.VERSION.SDK_INT >= 34) {
          setDiscoverable(optBoolean(SnippetConstants.ADV_PARAMETER_DISCOVERABLE, true))
        }
      }
      .build()

  private fun JSONObject.toPeriodicAdvertisingParameters(): PeriodicAdvertisingParameters =
    PeriodicAdvertisingParameters.Builder()
      .apply {
        getOrNull<Int>(SnippetConstants.ADV_PARAMETER_INTERVAL)?.let { setInterval(it) }
        getOrNull<Boolean>(SnippetConstants.ADV_DATA_INCLUDE_TX_POWER_LEVEL)?.let {
          setIncludeTxPower(it)
        }
      }
      .build()

  private fun JSONObject.toBluetoothGattDescriptor() =
    BluetoothGattDescriptor(
      UUID.fromString(optString(SnippetConstants.FIELD_UUID)),
      optInt(SnippetConstants.GATT_FIELD_PERMISSIONS),
    )

  private fun JSONObject.toBluetoothGattCharacteristic() =
    BluetoothGattCharacteristic(
        UUID.fromString(optString(SnippetConstants.FIELD_UUID)),
        optInt(SnippetConstants.GATT_FIELD_PROPERTIES),
        optInt(SnippetConstants.GATT_FIELD_PERMISSIONS),
      )
      .apply {
        optJSONArray(SnippetConstants.GATT_FIELD_DESCRIPTORS)?.toList<JSONObject>()?.forEach {
          addDescriptor(it.toBluetoothGattDescriptor())
        }
      }

  private fun JSONObject.toBluetoothGattService(): BluetoothGattService =
    BluetoothGattService(
        UUID.fromString(optString(SnippetConstants.FIELD_UUID)),
        optInt(SnippetConstants.GATT_FIELD_TYPE),
      )
      .apply {
        optJSONArray(SnippetConstants.GATT_FIELD_CHARACTERISTICS)?.toList<JSONObject>()?.forEach {
          addCharacteristic(it.toBluetoothGattCharacteristic())
        }
        optJSONArray(SnippetConstants.GATT_FIELD_INCLUDED_SERVICES)?.toList<JSONObject>()?.forEach {
          if (!addService(it.toBluetoothGattService())) {
            throw RuntimeException("Failed to add included service")
          }
        }
      }

  private fun JSONObject.toScanFilter() =
    ScanFilter.Builder()
      .apply {
        getOrNull<Int>(SnippetConstants.ADV_DATA_TYPE)?.let { setAdvertisingDataType(it) }
        getOrNull<String>(SnippetConstants.FIELD_NAME)?.let { setDeviceName(it) }
        getOrNull<String>(SnippetConstants.FIELD_DEVICE)?.let {
          val addressType =
            optInt(SnippetConstants.FIELD_ADDRESS_TYPE, BluetoothDevice.ADDRESS_TYPE_PUBLIC)
          val irk = getOrNull<JSONArray>(SnippetConstants.FIELD_IRK)?.toList<Byte>()?.toByteArray()
          if (irk != null) {
            setDeviceAddress(it, addressType, irk)
          } else {
            setDeviceAddress(it, addressType)
          }
        }
        getOrNull<String>(SnippetConstants.ADV_DATA_SERVICE_UUID)?.let {
          setServiceUuid(ParcelUuid.fromString(it))
        }
        getOrNull<String>(SnippetConstants.ADV_DATA_SERVICE_SOLICITATION_UUIDS)?.let {
          setServiceSolicitationUuid(ParcelUuid.fromString(it))
        }
        optJSONObject(SnippetConstants.ADV_DATA_MANUFACTURER_DATA)?.let {
          if (it.length() > 1) {
            throw IllegalArgumentException("ScanFilter cannot have more than 1 manufacturerData")
          }
          val key = it.keys().asSequence().first()
          setManufacturerData(key.toInt(), it.getJSONArray(key).toList<Byte>().toByteArray())
        }
        optJSONObject(SnippetConstants.ADV_DATA_SERVICE_DATA)?.let {
          if (it.length() > 1) {
            throw IllegalArgumentException("ScanFilter cannot have more than 1 serviceData")
          }
          val key = it.keys().asSequence().first()
          setServiceData(
            ParcelUuid.fromString(key),
            it.getJSONArray(key).toList<Byte>().toByteArray(),
          )
        }
      }
      .build()

  private fun JSONObject.toScanSettings() =
    ScanSettings.Builder()
      .apply {
        getOrNull<Int>(SnippetConstants.SCAN_PARAM_SCAN_MODE)?.let { setScanMode(it) }
        getOrNull<Int>(SnippetConstants.SCAN_PARAM_CALLBACK_TYPE)?.let { setCallbackType(it) }
        getOrNull<Int>(SnippetConstants.SCAN_PARAM_MATCH_MODE)?.let {
          val unused = setMatchMode(it)
        }
        getOrNull<Int>(SnippetConstants.SCAN_PARAM_SCAN_RESULT_TYPE)?.let { setScanResultType(it) }
        getOrNull<Int>(SnippetConstants.SCAN_PARAM_PHY)?.let { setPhy(it) }
        getOrNull<Boolean>(SnippetConstants.ADV_PARAMETER_LEGACY)?.let { setLegacy(it) }
        // org.json has a very strange behavior to get long value, so it's better to use
        // optString and do the conversion here.
        optString(SnippetConstants.SCAN_PARAM_REPORT_DELAY_MILLIS).let {
          if (it.isNotEmpty() && it != "null") {
            setReportDelay(it.toLong())
          }
        }
      }
      .build()

  private fun JSONObject.toAudioAttributes(): AudioAttributes =
    AudioAttributes.Builder()
      .apply {
        getOrNull<Int>(SnippetConstants.CONTENT_TYPE)?.let { setContentType(it) }
        getOrNull<Int>(SnippetConstants.FLAGS)?.let { setFlags(it) }
        getOrNull<Int>(SnippetConstants.USAGE)?.let { setUsage(it) }
        getOrNull<Int>(SnippetConstants.ALLOWED_CAPTURE_POLICY)?.let { setAllowedCapturePolicy(it) }
        getOrNull<Int>(SnippetConstants.SPATIALIZATION_BEHAVIOR)?.let {
          setSpatializationBehavior(it)
        }
      }
      .build()

  private fun JSONObject.toOobData(): OobData {
    val confirmationHash =
      getJSONArray(SnippetConstants.OOB_DATA_CONFIRMATION_HASH).toList<Byte>().toByteArray()
    val deviceAddressWithType =
      getJSONArray(SnippetConstants.OOB_DATA_DEVICE_ADDRESS_WITH_TYPE).toList<Byte>().toByteArray()
    val leDeviceRole = getOrNull<Int>(SnippetConstants.OOB_DATA_LE_DEVICE_ROLE)
    val classicLength = getOrNull<Int>(SnippetConstants.OOB_DATA_CLASSIC_LENGTH)
    val randomizerHash =
      getOrNull<JSONArray>(SnippetConstants.OOB_DATA_RANDOMIZER_HASH)?.toList<Byte>()?.toByteArray()
    if (leDeviceRole != null) {
      return OobData.LeBuilder(confirmationHash, deviceAddressWithType, leDeviceRole)
        .apply {
          optJSONArray(SnippetConstants.OOB_DATA_LE_TEMPORARY_KEY)
            ?.toList<Byte>()
            ?.toByteArray()
            ?.let { setLeTemporaryKey(it) }
          randomizerHash?.let { setRandomizerHash(it) }
        }
        .build()
    }
    if (classicLength != null) {
      return OobData.ClassicBuilder(
          confirmationHash,
          byteArrayOf((classicLength / 0xFF).toByte(), (classicLength % 0xFF).toByte()),
          deviceAddressWithType,
        )
        .apply { randomizerHash?.let { setRandomizerHash(it) } }
        .build()
    }
    throw IllegalArgumentException("OobData must have either leDeviceRole or classicLength")
  }

  private fun JSONObject.toSDPSettings(): BluetoothHidDeviceAppSdpSettings {
    val name = getOrNull<String>(SnippetConstants.HID_DEVICE_APP_NAME)
    val description = getOrNull<String>(SnippetConstants.HID_DEVICE_APP_DESCRIPTION)
    val provider = getOrNull<String>(SnippetConstants.HID_DEVICE_APP_PROVIDER)
    val subclass = getOrNull<Int>(SnippetConstants.HID_DEVICE_APP_SUBCLASS)?.toByte() ?: 0.toByte()
    val descriptorsArray = getJSONArray(SnippetConstants.HID_DEVICE_APP_DESCRIPTORS)
    val descriptors = descriptorsArray.toList<Byte>().toByteArray()

    return BluetoothHidDeviceAppSdpSettings(name, description, provider, subclass, descriptors)
  }

  private fun JSONObject.toMediaItem(): MediaItem =
    MediaItem.Builder()
      .apply {
        getOrNull<String>(SnippetConstants.URI)?.let { setUri(it) }
        getOrNull<String>(SnippetConstants.FIELD_ID)?.let { setMediaId(it) }
        setMediaMetadata(
          MediaMetadata.Builder()
            .apply {
              getOrNull<String>(SnippetConstants.TITLE)?.let { setTitle(it) }
              getOrNull<String>(SnippetConstants.ARTIST)?.let { setArtist(it) }
              getOrNull<String>(SnippetConstants.ALBUM)?.let { setAlbumTitle(it) }
              getOrNull<String>(SnippetConstants.FIELD_ARTWORK_URI)?.let {
                setArtworkUri(Uri.parse(it))
              }
              getOrNull<Boolean>(SnippetConstants.FIELD_BROWSABLE)?.let { setIsBrowsable(it) }
              getOrNull<Boolean>(SnippetConstants.FIELD_PLAYABLE)?.let { setIsPlayable(it) }
            }
            .build()
        )
      }
      .build()

  private fun JSONObject.toMediaNode(): Utils.MediaNode {
    val item = toMediaItem()
    val children =
      optJSONArray(SnippetConstants.FIELD_CHILDREN)?.toList<JSONObject>()?.map { it.toMediaNode() }
        ?: listOf()
    return Utils.MediaNode(item, children)
  }

  private fun JSONObject.toBluetoothSocketSettings(): BluetoothSocketSettings {
    val builder =
      BluetoothSocketSettings.Builder()
        .setSocketType(
          optInt(SnippetConstants.SOCKET_SETTINGS_SOCKET_TYPE, BluetoothSocket.TYPE_LE)
        )
        .setEncryptionRequired(
          optBoolean(SnippetConstants.SOCKET_SETTINGS_ENCRYPTION_REQUIRED, false)
        )
        .setAuthenticationRequired(
          optBoolean(SnippetConstants.SOCKET_SETTINGS_AUTHENTICATION_REQUIRED, false)
        )
    getOrNull<Int>(SnippetConstants.SOCKET_SETTINGS_L2CAP_PSM)?.let {
      if (it > 0) {
        val unused = builder.setL2capPsm(it)
      }
    }
    getOrNull<String>(SnippetConstants.SOCKET_SETTINGS_RFCOMM_UUID)?.let {
      val unused = builder.setRfcommUuid(UUID.fromString(it))
    }
    getOrNull<String>(SnippetConstants.SOCKET_SETTINGS_RFCOMM_SERVICE_NAME)?.let {
      val unused = builder.setRfcommServiceName(it)
    }
    return builder.build()
  }

  private fun JSONObject.toBluetoothCodecType(): BluetoothCodecType =
    BluetoothCodecType(
      getInt(SnippetConstants.CODEC_TYPE),
      getLong(SnippetConstants.FIELD_ID),
      getString(SnippetConstants.FIELD_NAME),
    )

  private fun JSONObject.toBluetoothCodecConfig(): BluetoothCodecConfig =
    BluetoothCodecConfig.Builder()
      .apply {
        getOrNull<Int>(SnippetConstants.CODEC_TYPE)?.let {
          val unused = setCodecType(it)
        }
        getOrNull<Int>(SnippetConstants.CHANNEL_MODE)?.let {
          val unused = setChannelMode(it)
        }
        getOrNull<Int>(SnippetConstants.BITS_PER_SAMPLE)?.let {
          val unused = setBitsPerSample(it)
        }
        getOrNull<Int>(SnippetConstants.PRIORITY)?.let {
          val unused = setCodecPriority(it)
        }
        optJSONObject(SnippetConstants.EXTENDED_CODEC_TYPE)?.let {
          val unused = setExtendedCodecType(it.toBluetoothCodecType())
        }
        getOrNull<Int>(SnippetConstants.SAMPLE_RATE)?.let {
          val unused = setSampleRate(it)
        }
        getOrNull<Long>(SnippetConstants.CODEC_SPECIFIC_1)?.let {
          val unused = setCodecSpecific1(it)
        }
        getOrNull<Long>(SnippetConstants.CODEC_SPECIFIC_2)?.let {
          val unused = setCodecSpecific2(it)
        }
        getOrNull<Long>(SnippetConstants.CODEC_SPECIFIC_3)?.let {
          val unused = setCodecSpecific3(it)
        }
        getOrNull<Long>(SnippetConstants.CODEC_SPECIFIC_4)?.let {
          val unused = setCodecSpecific4(it)
        }
      }
      .build()

  /**
   * Serializes JVM object [parameter] to a [JSONObject], or returns null if there is no viable
   * conversion.
   */
  override fun serialize(parameter: Any?): JSONObject? {
    uiAutomatorObjectConverter.serialize(parameter)?.let {
      return it
    }
    if (parameter is BluetoothGattService) {
      return parameter.toJson()
    }
    if (parameter is Call) {
      return parameter.toJson()
    }
    if (parameter is ScanResult) {
      return parameter.toJson()
    }
    if (parameter is BluetoothQualityReport) {
      return parameter.toJson()
    }
    if (parameter is OobData) {
      return parameter.toJson()
    }
    if (parameter is MediaItem) {
      return parameter.toJson()
    }
    if (parameter is MediaBrowserCompat.MediaItem) {
      return parameter.toJson()
    }
    if (parameter is BluetoothCodecConfig) {
      return parameter.toJson()
    }
    if (Build.VERSION.SDK_INT >= 35) {
      if (parameter is BluetoothCodecType) {
        return parameter.toJson()
      }
    }
    if (Build.VERSION.SDK_INT >= 37 && parameter is android.bluetooth.BondStatus) {
      return JSONObject().apply {
        put(SnippetConstants.PAIRING_ALGORITHM, parameter.pairingAlgorithm)
        put(SnippetConstants.PAIRING_VARIANT, parameter.pairingVariant)
      }
    }
    if (Build.VERSION.SDK_INT >= 34 && parameter is CallEndpoint) {
      return parameter.toJson()
    }
    return null
  }

  /** Deserializes [jsonObject] to [type], or returns null if there is no viable conversion. */
  override fun deserialize(jsonObject: JSONObject?, type: Type?): Any? {
    uiAutomatorObjectConverter.deserialize(jsonObject, type)?.let {
      return it
    }
    // TODO - b/313796355: Add parameter deserializers.
    if (type === AdvertiseSettings::class.java) {
      return jsonObject?.toAdvertiseSettings()
    }
    if (type === AdvertiseData::class.java) {
      return jsonObject?.toAdvertiseData()
    }
    if (type === AdvertisingSetParameters::class.java) {
      return jsonObject?.toAdvertisingSetParameters()
    }
    if (type === PeriodicAdvertisingParameters::class.java) {
      return jsonObject?.toPeriodicAdvertisingParameters()
    }
    if (type === BluetoothGattService::class.java) {
      return jsonObject?.toBluetoothGattService()
    }
    if (type === ScanFilter::class.java) {
      return jsonObject?.toScanFilter()
    }
    if (type === ScanSettings::class.java) {
      return jsonObject?.toScanSettings()
    }
    if (type === AudioAttributes::class.java) {
      return jsonObject?.toAudioAttributes()
    }
    if (type === OobData::class.java) {
      return jsonObject?.toOobData()
    }
    if (type === BluetoothHidDeviceAppSdpSettings::class.java) {
      return jsonObject?.toSDPSettings()
    }
    if (type === MediaItem::class.java) {
      return jsonObject?.toMediaItem()
    }
    if (type === Utils.MediaNode::class.java) {
      return jsonObject?.toMediaNode()
    }
    if (type === BluetoothCodecConfig::class.java) {
      return jsonObject?.toBluetoothCodecConfig()
    }
    if (Build.VERSION.SDK_INT >= 35 && type === BluetoothCodecType::class.java) {
      return jsonObject?.toBluetoothCodecType()
    }
    if (type === BluetoothSocketSettings::class.java) {
      return jsonObject?.toBluetoothSocketSettings()
    }
    return null
  }

  companion object {
    // Used by AudioSnippet to convert MediaItem in SnippetEvent.
    internal fun MediaItem.toJson(): Utils.ParcelableJsonObject =
      Utils.ParcelableJsonObject().apply {
        put(SnippetConstants.FIELD_ID, this@toJson.mediaId)
        put(SnippetConstants.TITLE, this@toJson.mediaMetadata.title)
        put(SnippetConstants.ARTIST, this@toJson.mediaMetadata.artist)
        put(SnippetConstants.URI, this@toJson.localConfiguration?.uri.toString())
        put(SnippetConstants.FIELD_BROWSABLE, this@toJson.mediaMetadata.isBrowsable)
        put(SnippetConstants.FIELD_PLAYABLE, this@toJson.mediaMetadata.isPlayable)
      }

    private fun MediaBrowserCompat.MediaItem.toJson(): JSONObject =
      JSONObject().apply {
        put(SnippetConstants.FIELD_ID, this@toJson.mediaId)
        put(SnippetConstants.TITLE, this@toJson.description.title)
        put(SnippetConstants.FIELD_BROWSABLE, this@toJson.isBrowsable)
        put(SnippetConstants.FIELD_PLAYABLE, this@toJson.isPlayable)
      }

    private fun BluetoothCodecType.toJson(): JSONObject =
      JSONObject().apply {
        put(SnippetConstants.FIELD_ID, this@toJson.codecId)
        put(SnippetConstants.FIELD_NAME, this@toJson.codecName)
      }

    private fun BluetoothCodecConfig.toJson(): JSONObject =
      JSONObject().apply {
        put(SnippetConstants.CODEC_TYPE, this@toJson.codecType)
        put(SnippetConstants.CHANNEL_MODE, this@toJson.channelMode)
        put(SnippetConstants.BITS_PER_SAMPLE, this@toJson.bitsPerSample)
        put(SnippetConstants.PRIORITY, this@toJson.codecPriority)
        if (Build.VERSION.SDK_INT >= 35) {
          put(SnippetConstants.EXTENDED_CODEC_TYPE, this@toJson.extendedCodecType?.toJson())
        }
        put(SnippetConstants.SAMPLE_RATE, this@toJson.sampleRate)
        put(SnippetConstants.CODEC_SPECIFIC_1, this@toJson.codecSpecific1)
        put(SnippetConstants.CODEC_SPECIFIC_2, this@toJson.codecSpecific2)
        put(SnippetConstants.CODEC_SPECIFIC_3, this@toJson.codecSpecific3)
        put(SnippetConstants.CODEC_SPECIFIC_4, this@toJson.codecSpecific4)
      }
  }
}
