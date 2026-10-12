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

import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothLeAudio
import android.bluetooth.BluetoothLeAudioCodecConfig
import android.bluetooth.BluetoothLeAudioCodecStatus
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.postSnippetEvent

class BluetoothLeAudioSnippet : Snippet {
  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val bluetoothAdapter = context.getSystemService(BluetoothManager::class.java).adapter
  private val leaProxy =
    Utils.getProfileProxy(context, BluetoothProfile.LE_AUDIO) as BluetoothLeAudio
  private val callbacks = mutableMapOf<String, BroadcastReceiver>()

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
  }

  /** Registers LE Audio callbacks for cookie [callbackId]. */
  @AsyncRpc(description = "Register LE Audio callbacks")
  fun registerLeAudioCallback(callbackId: String) {
    val callback =
      object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
          val device =
            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
          val state = intent.getIntExtra(BluetoothProfile.EXTRA_STATE, BluetoothDevice.ERROR)
          when (intent.action) {
            BluetoothLeAudio.ACTION_LE_AUDIO_CONNECTION_STATE_CHANGED -> {
              postSnippetEvent(callbackId, SnippetConstants.PROFILE_CONNECTION_STATE_CHANGE) {
                putInt(SnippetConstants.FIELD_STATE, state)
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
              }
            }
            BluetoothLeAudio.ACTION_LE_AUDIO_ACTIVE_DEVICE_CHANGED -> {
              postSnippetEvent(callbackId, SnippetConstants.ACTIVE_DEVICE_CHANGED) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
              }
            }
          }
        }
      }

    val intentFilter =
      IntentFilter().apply {
        addAction(BluetoothLeAudio.ACTION_LE_AUDIO_CONNECTION_STATE_CHANGED)
        addAction(BluetoothLeAudio.ACTION_LE_AUDIO_ACTIVE_DEVICE_CHANGED)
      }
    context.registerReceiver(callback, intentFilter)
    callbacks[callbackId] = callback
  }

  /** Unregisters LE Audio callbacks for cookie [callbackId]. */
  @Rpc(description = "Unregister LE Audio callbacks")
  fun unregisterLeAudioCallback(callbackId: String) {
    callbacks.remove(callbackId)?.let { context.unregisterReceiver(it) }
      ?: throw IllegalArgumentException("Callback $callbackId doesn't exist")
  }

  /** Sets LE Audio connection policy of device [address] to [policy]. */
  @Rpc(description = "Set LE Audio connection policy.")
  fun setLeAudioConnectionPolicy(address: String, policy: Int): Boolean =
    leaProxy.setConnectionPolicy(bluetoothAdapter.getRemoteDevice(address), policy)

  /** Gets connected LE Audio devices list. */
  @Rpc(description = "Get connected LE Audio devices list")
  fun leaGetConnectedDevices(): List<String> {
    return leaProxy.connectedDevices.map { it.address }.toList()
  }

  /** Gets LE Audio group ID for device [address]. */
  @Rpc(description = "Get LE Audio group ID")
  fun getLeAudioGroupId(address: String): Int =
    leaProxy.getGroupId(bluetoothAdapter.getRemoteDevice(address))

  /** Gets LE Audio codec status for [groupId]. */
  @Rpc(description = "Get LE Audio codec status")
  fun getLeAudioCodecStatus(groupId: Int): BluetoothLeAudioCodecStatus? =
    leaProxy.getCodecStatus(groupId)

  /** Sets LE Audio codec config preference for [groupId]. */
  @Rpc(description = "Set LE Audio codec config preference")
  fun setLeAudioCodecConfigPreference(
    groupId: Int,
    inputCodecConfig: BluetoothLeAudioCodecConfig,
    outputCodecConfig: BluetoothLeAudioCodecConfig,
  ) {
    leaProxy.setCodecConfigPreference(groupId, inputCodecConfig, outputCodecConfig)
  }

  companion object {
    const val TAG = "BluetoothLeAudioSnippet"
  }
}
