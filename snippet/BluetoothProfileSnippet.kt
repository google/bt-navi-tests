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

import android.bluetooth.BluetoothA2dpSink
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothHearingAid
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothMap
import android.bluetooth.BluetoothPbap
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothSap
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc

/** Snippet for profiles with connection state change events and without APIs. */
class BluetoothProfileSnippet : Snippet {
  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val broadcastReceivers = mutableMapOf<String, BroadcastReceiver>()
  private val bluetoothAdapter = context.getSystemService(BluetoothManager::class.java).adapter
  private val profileProxys = mutableMapOf<Int, BluetoothProfile>()

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
  }

  /** Register a Profile callback with [callbackId]. */
  @AsyncRpc(description = "Register Profile Callback.")
  fun registerProfileCallback(callbackId: String, profile: Int) {
    val stateChangeAction =
      STATE_CHANGE_ACTIONS[profile]
        ?: throw IllegalArgumentException("Profile $profile is not supported here")
    val activeDeviceChangedAction = ACTIVE_DEVICE_CHANGED_ACTIONS[profile]
    val intentFilter =
      IntentFilter().apply {
        addAction(stateChangeAction)
        if (activeDeviceChangedAction != null) {
          addAction(activeDeviceChangedAction)
        }
      }
    broadcastReceivers[callbackId] =
      object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
          val device =
            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
          val state = intent.getIntExtra(BluetoothProfile.EXTRA_STATE, BluetoothDevice.ERROR)
          when (intent.action) {
            stateChangeAction ->
              Utils.postSnippetEvent(callbackId, SnippetConstants.PROFILE_CONNECTION_STATE_CHANGE) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(SnippetConstants.FIELD_STATE, state)
              }
            activeDeviceChangedAction -> {
              Utils.postSnippetEvent(callbackId, SnippetConstants.ACTIVE_DEVICE_CHANGED) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
              }
            }
          }
        }
      }
    context.registerReceiver(broadcastReceivers[callbackId], intentFilter)
  }

  /** Unregisters a Profile callback with ID [callbackId]. */
  @Rpc(description = "Unregister Profile callbacks.")
  fun unregisterProfileCallback(callbackId: String) {
    broadcastReceivers.remove(callbackId)?.let { context.unregisterReceiver(it) }
  }

  /** Gets active devices of [profile]. */
  @Rpc(description = "Get active devices of a profile.")
  fun getActiveDevices(profile: Int): List<String> =
    bluetoothAdapter.getActiveDevices(profile).filter { it != null }.map { it.address }

  /** Sets active device of [profile] to device in [address]. */
  @Rpc(description = "Set active device of profiles.")
  fun setActiveDevice(address: String?, profiles: Int): Boolean {
    if (address != null) {
      return bluetoothAdapter.setActiveDevice(bluetoothAdapter.getRemoteDevice(address), profiles)
    } else {
      return bluetoothAdapter.removeActiveDevice(profiles)
    }
  }

  /** Gets connection state of [profile] for device in [address]. */
  @Rpc(description = "Get connection state of a profile.")
  fun getProfileConnectionState(profile: Int, address: String): Int =
    getProfileProxy(profile).getConnectionState(bluetoothAdapter.getRemoteDevice(address))

  /** Gets connected devices of [profile]. */
  @Rpc(description = "Get connected devices of a profile.")
  fun getProfileConnectedDevices(profile: Int): List<String> =
    getProfileProxy(profile).connectedDevices.map { it.address }

  /** Gets devices matching connection states of [profile] to device in [states]. */
  @Rpc(description = "Get devices matching connection states of a profile.")
  fun getProfileDevicesMatchingConnectionStates(profile: Int, states: List<Int>): List<String> =
    getProfileProxy(profile).getDevicesMatchingConnectionStates(states.toIntArray()).map {
      it.address
    }

  /** Waits for connection state of [profile] to device in [address] to be [connected]. */
  @Rpc(description = "Wait for connection state of a profile.")
  fun waitForProfileReady(profile: Int) {
    val unused = Utils.getProfileProxy(context, profile)
  }

  private fun getProfileProxy(profile: Int): BluetoothProfile =
    profileProxys.getOrPut(profile) { Utils.getProfileProxy(context, profile) }

  companion object {
    val STATE_CHANGE_ACTIONS =
      mapOf(
        BluetoothProfile.HEARING_AID to BluetoothHearingAid.ACTION_CONNECTION_STATE_CHANGED,
        BluetoothProfile.PBAP to BluetoothPbap.ACTION_CONNECTION_STATE_CHANGED,
        BluetoothProfile.MAP to BluetoothMap.ACTION_CONNECTION_STATE_CHANGED,
        BluetoothProfile.SAP to BluetoothSap.ACTION_CONNECTION_STATE_CHANGED,
        BluetoothProfile.A2DP_SINK to BluetoothA2dpSink.ACTION_CONNECTION_STATE_CHANGED,
      )
    val ACTIVE_DEVICE_CHANGED_ACTIONS =
      mapOf(BluetoothProfile.HEARING_AID to BluetoothHearingAid.ACTION_ACTIVE_DEVICE_CHANGED)
  }
}
