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

import android.annotation.SuppressLint
import android.bluetooth.AudioInputControl
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothVolumeControl
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.postSnippetEvent

@SuppressLint("MissingPermission")
class BluetoothVolumeControlSnippet : Snippet {

  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val bluetoothManager = context.getSystemService(BluetoothManager::class.java)
  private val bluetoothAdapter = bluetoothManager.adapter

  private val vcpProxy =
    Utils.getProfileProxy(context, BluetoothProfile.VOLUME_CONTROL) as BluetoothVolumeControl
  private val callbackExecutor = context.mainExecutor

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
  }

  private fun getBluetoothDevice(address: String): BluetoothDevice =
    bluetoothAdapter.getRemoteDevice(address)

  private fun getAic(address: String, instanceId: Int): AudioInputControl =
    vcpProxy.getAudioInputControlServices(getBluetoothDevice(address))[instanceId]

  private inner class VolumeControlCallback(val callbackId: String) :
    BluetoothVolumeControl.Callback {

    override fun onVolumeOffsetChanged(device: BluetoothDevice, instanceId: Int, offset: Int) {
      postSnippetEvent(callbackId, SnippetConstants.VOLUME_OFFSET_CHANGED) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putInt(SnippetConstants.FIELD_INSTANCE_ID, instanceId)
        putInt(SnippetConstants.FIELD_OFFSET, offset)
      }
    }

    override fun onVolumeOffsetAudioLocationChanged(
      device: BluetoothDevice,
      instanceId: Int,
      audioLocation: Int,
    ) {
      postSnippetEvent(callbackId, SnippetConstants.VOLUME_OFFSET_AUDIO_LOCATION_CHANGED) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putInt(SnippetConstants.FIELD_INSTANCE_ID, instanceId)
        putInt(SnippetConstants.FIELD_AUDIO_LOCATION, audioLocation)
      }
    }

    override fun onVolumeOffsetAudioDescriptionChanged(
      device: BluetoothDevice,
      instanceId: Int,
      audioDescription: String,
    ) {
      postSnippetEvent(callbackId, SnippetConstants.VOLUME_OFFSET_AUDIO_DESCRIPTION_CHANGED) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putInt(SnippetConstants.FIELD_INSTANCE_ID, instanceId)
        putString(SnippetConstants.FIELD_AUDIO_DESCRIPTION, audioDescription)
      }
    }

    override fun onDeviceVolumeChanged(device: BluetoothDevice, volume: Int) {
      postSnippetEvent(callbackId, SnippetConstants.DEVICE_VOLUME_CHANGED) {
        putString(SnippetConstants.FIELD_DEVICE, device.address)
        putInt(SnippetConstants.FIELD_VOLUME, volume)
      }
    }
  }

  private inner class AicsCallback(val callbackId: String) : AudioInputControl.AudioInputCallback {
    override fun onDescriptionChanged(description: String) {
      postSnippetEvent(callbackId, SnippetConstants.AICS_DESCRIPTION_CHANGED) {
        putString(SnippetConstants.AICS_DESCRIPTION, description)
      }
    }

    override fun onAudioInputStatusChanged(status: Int) {
      postSnippetEvent(callbackId, SnippetConstants.AICS_STATUS_CHANGED) {
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }

    override fun onGainSettingChanged(gainSetting: Int) {
      postSnippetEvent(callbackId, SnippetConstants.AICS_GAIN_SETTING_CHANGED) {
        putInt(SnippetConstants.AICS_GAIN_SETTING, gainSetting)
      }
    }

    override fun onSetGainSettingFailed() {
      postSnippetEvent(callbackId, SnippetConstants.AICS_SET_GAIN_SETTING_FAILED) {}
    }

    override fun onMuteChanged(mute: Int) {
      postSnippetEvent(callbackId, SnippetConstants.AICS_MUTE_CHANGED) {
        putInt(SnippetConstants.AICS_MUTE, mute)
      }
    }

    override fun onSetMuteFailed() {
      postSnippetEvent(callbackId, SnippetConstants.AICS_SET_MUTE_FAILED) {}
    }

    override fun onGainModeChanged(gainMode: Int) {
      postSnippetEvent(callbackId, SnippetConstants.AICS_GAIN_MODE_CHANGED) {
        putInt(SnippetConstants.AICS_GAIN_MODE, gainMode)
      }
    }

    override fun onSetGainModeFailed() {
      postSnippetEvent(callbackId, SnippetConstants.AICS_SET_GAIN_MODE_FAILED) {}
    }
  }

  private val activeCallbacks = mutableMapOf<String, Pair<AudioInputControl, AicsCallback>>()
  private val vcpCallbacks = mutableMapOf<String, BroadcastReceiver>()
  private val vcpProfileCallbacks = mutableMapOf<String, BluetoothVolumeControl.Callback>()

  @AsyncRpc(description = "Sets up a callback for an AICS instance.")
  fun registerAicsCallback(callbackId: String, address: String, instanceId: Int) {
    val aic = getAic(address, instanceId)
    val callback = AicsCallback(callbackId)
    aic.registerCallback(callbackExecutor, callback)
    activeCallbacks[callbackId] = Pair(aic, callback)
  }

  @Rpc(description = "Tears down an AICS callback.")
  fun unregisterAicsCallback(callbackId: String) {
    activeCallbacks.remove(callbackId)?.let { (aic, callback) -> aic.unregisterCallback(callback) }
  }

  @Rpc(description = "Unregisters a callback for Connection state changes and VCP events.")
  fun unregisterVolumeControlCallback(callbackId: String) {
    vcpCallbacks.remove(callbackId)?.let { context.unregisterReceiver(it) }
    vcpProfileCallbacks.remove(callbackId)?.let {
      try {
        vcpProxy.unregisterCallback(it)
      } catch (e: IllegalArgumentException) {
        Log.e(TAG, "Failed to unregister volume control callback for $callbackId", e)
      }
    }
  }

  @AsyncRpc(description = "Registers a callback for VCP events including connection state changes.")
  fun registerVolumeControlCallback(callbackId: String) {
    val profileCallback = VolumeControlCallback(callbackId)
    Log.i(TAG, "Registering profile callback for $callbackId")
    vcpProxy.registerCallback(callbackExecutor, profileCallback)
    vcpProfileCallbacks[callbackId] = profileCallback

    val receiver =
      object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
          val device =
            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
          when (intent.action) {
            BluetoothVolumeControl.ACTION_CONNECTION_STATE_CHANGED -> {
              postSnippetEvent(callbackId, SnippetConstants.PROFILE_CONNECTION_STATE_CHANGE) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(
                  SnippetConstants.FIELD_STATE,
                  intent.getIntExtra(BluetoothProfile.EXTRA_STATE, BluetoothDevice.ERROR),
                )
              }
            }
          }
        }
      }
    val filter =
      IntentFilter().apply { addAction(BluetoothVolumeControl.ACTION_CONNECTION_STATE_CHANGED) }
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
      context.registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
    } else {
      context.registerReceiver(receiver, filter)
    }
    vcpCallbacks[callbackId] = receiver
  }

  override fun shutdown() {
    bluetoothAdapter.closeProfileProxy(BluetoothProfile.VOLUME_CONTROL, vcpProxy)
  }

  @Rpc(description = "Get Connected devices")
  fun vcpGetConnectedDevices(): List<String> {
    return vcpProxy.connectedDevices.map { it.address }.toList()
  }

  @Rpc(description = "Get Connection State")
  fun vcpGetConnectionState(address: String): Int =
    vcpProxy.getConnectionState(getBluetoothDevice(address))

  // VOCS RPCs

  @Rpc(description = "Set the Volume Offset.")
  fun setVolumeOffset(address: String, instanceId: Int, offset: Int) {
    vcpProxy.setVolumeOffset(getBluetoothDevice(address), instanceId, offset)
  }

  @Rpc(description = "Check if the volume offset is available")
  fun isVolumeOffsetAvailable(address: String): Boolean =
    vcpProxy.isVolumeOffsetAvailable(getBluetoothDevice(address))

  @Rpc(description = "Get the number of VOCS instances")
  fun getNumberofVocsInstances(address: String): Int =
    vcpProxy.getNumberOfVolumeOffsetInstances(getBluetoothDevice(address))

  @Rpc(description = "Connection Policy")
  fun vcpSetConnectionPolicy(address: String, policy: Int): Boolean =
    vcpProxy.setConnectionPolicy(getBluetoothDevice(address), policy)

  @Rpc(description = "Get Connection Policy")
  fun vcpGetConnectionPolicy(address: String): Int =
    vcpProxy.getConnectionPolicy(getBluetoothDevice(address))

  @Rpc(description = "Set device volume")
  fun vcpSetDeviceVolume(address: String, volume: Int, isGroupOperation: Boolean) {
    vcpProxy.setDeviceVolume(getBluetoothDevice(address), volume, isGroupOperation)
  }

  // AICS RPCs
  @Rpc(description = "Gets the Audio Input Type.")
  fun aicsGetAudioInputType(address: String, instanceId: Int): Int =
    getAic(address, instanceId).audioInputType

  @Rpc(description = "Gets the Gain Setting Units.")
  fun aicsGetGainSettingUnit(address: String, instanceId: Int): Int =
    getAic(address, instanceId).gainSettingUnit

  @Rpc(description = "Gets the minimum Gain Setting.")
  fun aicsGetGainSettingMin(address: String, instanceId: Int): Int =
    getAic(address, instanceId).gainSettingMin

  @Rpc(description = "Gets the maximum Gain Setting.")
  fun aicsGetGainSettingMax(address: String, instanceId: Int): Int =
    getAic(address, instanceId).gainSettingMax

  @Rpc(description = "Gets the description.")
  fun aicsGetDescription(address: String, instanceId: Int): String =
    getAic(address, instanceId).description

  @Rpc(description = "Checks if description is writable.")
  fun aicsIsDescriptionWritable(address: String, instanceId: Int): Boolean =
    getAic(address, instanceId).isDescriptionWritable

  @Rpc(description = "Sets the description.")
  fun aicsSetDescription(address: String, instanceId: Int, description: String): Boolean {
    val aic = getAic(address, instanceId)
    return aic.setDescription(description)
  }

  @Rpc(description = "Gets the Audio Input Status.")
  fun aicsGetAudioInputStatus(address: String, instanceId: Int): Int =
    getAic(address, instanceId).audioInputStatus

  @Rpc(description = "Gets the gain setting.")
  fun aicsGetGainSetting(address: String, instanceId: Int): Int =
    getAic(address, instanceId).gainSetting

  @Rpc(description = "Sets the gain setting.")
  fun aicsSetGainSetting(address: String, instanceId: Int, gainSetting: Int): Boolean =
    getAic(address, instanceId).setGainSetting(gainSetting)

  @Rpc(description = "Gets the gain mode.")
  fun aicsGetGainMode(address: String, instanceId: Int): Int = getAic(address, instanceId).gainMode

  @Rpc(description = "Sets the gain mode.")
  fun aicsSetGainMode(address: String, instanceId: Int, gainMode: Int): Boolean {
    Log.i(TAG, "aicsSetGainMode called with gainMode: $gainMode for device $address")
    return getAic(address, instanceId).setGainMode(gainMode)
  }

  @Rpc(description = "Gets the mute state.")
  fun aicsGetMute(address: String, instanceId: Int): Int = getAic(address, instanceId).mute

  @Rpc(description = "Sets the mute state.")
  fun aicsSetMute(address: String, instanceId: Int, mute: Int): Boolean =
    getAic(address, instanceId).setMute(mute)

  companion object {
    private const val TAG = "AudioInputControlSnippet"
  }
}
