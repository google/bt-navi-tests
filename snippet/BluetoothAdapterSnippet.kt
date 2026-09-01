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
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothQualityReport
import android.bluetooth.BondStatus
import android.bluetooth.OobData
import android.bluetooth.TakStatus
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.bluetooth.le.AdvertisingSet
import android.bluetooth.le.AdvertisingSetCallback
import android.bluetooth.le.AdvertisingSetParameters
import android.bluetooth.le.PeriodicAdvertisingParameters
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.Handler
import android.os.ParcelUuid
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.android.mobly.snippet.rpc.RpcDefault
import com.google.android.mobly.snippet.rpc.RpcOptional
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.postSnippetEvent
import java.util.UUID
import kotlin.time.Duration.Companion.seconds
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.timeout
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull

// Permissions are requested with UiAutomation.
@SuppressLint("MissingPermission")
class BluetoothAdapterSnippet : Snippet {

  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val bluetoothAdapter = context.getSystemService(BluetoothManager::class.java).adapter
  private val advertisers = mutableMapOf<String, AdvertiseCallback>()
  private val advertisingSets = mutableMapOf<String, AdvertisingSetCallback>()
  private val scanners = mutableMapOf<String, ScanCallback>()
  private val adapterState = MutableStateFlow(bluetoothAdapter.state)
  private val broadcastReceivers = mutableMapOf<String, BroadcastReceiver>()
  private val bqrCallbacks =
    mutableMapOf<String, BluetoothAdapter.BluetoothQualityReportReadyCallback>()

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
    context.registerReceiver(
      object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
          when (intent.action) {
            BluetoothAdapter.ACTION_BLE_STATE_CHANGED -> {
              val state = intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR)
              Log.d(TAG, "BLE state changed to ${BluetoothAdapter.nameForState(state)}")
              adapterState.value = state
            }
          }
        }
      },
      IntentFilter(BluetoothAdapter.ACTION_BLE_STATE_CHANGED),
      Context.RECEIVER_EXPORTED,
    )
  }

  private fun getDevice(address: String): BluetoothDevice {
    return bluetoothAdapter.getRemoteDevice(address)
  }

  /** Resets Bluetooth, waits for auto-restart, and returns whether everything succeeds. */
  @Rpc(description = "Completely reset Bluetooth")
  fun factoryReset(): Boolean = runBlocking {
    val turningOff = async {
      adapterState
        .timeout(BLUETOOTH_ON_OFF_TIMEOUT)
        .catch { exception ->
          if (exception is TimeoutCancellationException) {
            throw RuntimeException(
              "Bluetooth isn't turned off after ${BLUETOOTH_ON_OFF_TIMEOUT}, " +
                "final state=${BluetoothAdapter.nameForState(adapterState.value)}"
            )
          }
        }
        .first { it != BluetoothAdapter.STATE_ON }
    }
    val result = bluetoothAdapter.clearBluetooth()
    if (result) {
      turningOff.await()
      adapterState
        .timeout(BLUETOOTH_ON_OFF_TIMEOUT)
        .catch { exception ->
          if (exception is TimeoutCancellationException) {
            throw RuntimeException(
              "Bluetooth isn't turned on after ${BLUETOOTH_ON_OFF_TIMEOUT}, " +
                "final state=${BluetoothAdapter.nameForState(adapterState.value)}"
            )
          }
        }
        .first { it == BluetoothAdapter.STATE_ON }
    } else {
      turningOff.cancel()
    }
    result
  }

  /**
   * Gets the current state of the local Bluetooth adapter defined in
   * [android.bluetooth.BluetoothAdapter.STATE_*].
   */
  @Rpc(description = "Get Bluetooth Adapter state")
  fun getAdapterState(): Int = bluetoothAdapter.getState()

  /** Enables Bluetooth and returns the operation result. */
  @Suppress("DEPRECATION")
  @Rpc(description = "Enable Bluetooth")
  fun enable(): Boolean = bluetoothAdapter.enable()

  /** Disables Bluetooth and returns the operation result. */
  @Rpc(description = "Disable Bluetooth") fun disable(): Boolean = bluetoothAdapter.disable(true)

  @Rpc(description = "Wait for Bluetooth adapter state")
  fun waitForAdapterState(state: Int) = runBlocking {
    adapterState
      .timeout(BLUETOOTH_ON_OFF_TIMEOUT)
      .catch { exception ->
        if (exception is TimeoutCancellationException) {
          throw RuntimeException(
            "Bluetooth isn't at state=${BluetoothAdapter.nameForState(state)} after " +
              "${BLUETOOTH_ON_OFF_TIMEOUT}, " +
              "final state=${BluetoothAdapter.nameForState(adapterState.value)}"
          )
        }
      }
      .first { it == state }
  }

  /** Creates a [BroadcastReceiver] which redirects intents to the event queue of [callbackId]. */
  @AsyncRpc(description = "Register adapter callback")
  fun registerAdapterCallback(callbackId: String) {
    val intentFilter =
      IntentFilter().apply {
        addAction(BluetoothDevice.ACTION_PAIRING_REQUEST)
        addAction(BluetoothDevice.ACTION_BOND_STATE_CHANGED)
        addAction(BluetoothDevice.ACTION_ACL_CONNECTED)
        addAction(BluetoothDevice.ACTION_ACL_DISCONNECTED)
        addAction(BluetoothDevice.ACTION_FOUND)
        addAction(BluetoothDevice.ACTION_BATTERY_LEVEL_CHANGED)
        addAction(BluetoothDevice.ACTION_UUID)
        addAction(BluetoothDevice.ACTION_ENCRYPTION_CHANGE)
        addAction(BluetoothDevice.ACTION_KEY_MISSING)
        addAction(BluetoothDevice.ACTION_NAME_CHANGED)
        addAction(BluetoothAdapter.ACTION_STATE_CHANGED)
        addAction(BluetoothAdapter.ACTION_DISCOVERY_STARTED)
        addAction(BluetoothAdapter.ACTION_DISCOVERY_FINISHED)
      }
    broadcastReceivers[callbackId] =
      object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
          Log.i(TAG, "Receive Intent ${intent.action}")
          val device =
            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
          val variant =
            intent.getIntExtra(BluetoothDevice.EXTRA_PAIRING_VARIANT, BluetoothDevice.ERROR)
          val pin = intent.getIntExtra(BluetoothDevice.EXTRA_PAIRING_KEY, BluetoothDevice.ERROR)
          val state = intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.ERROR)
          val transport = intent.getIntExtra(BluetoothDevice.EXTRA_TRANSPORT, BluetoothDevice.ERROR)
          val adapterState =
            intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR)
          when (intent.action) {
            BluetoothDevice.ACTION_PAIRING_REQUEST ->
              postSnippetEvent(callbackId, SnippetConstants.PAIRING_REQUEST) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(SnippetConstants.FIELD_VARIANT, variant)
                putInt(SnippetConstants.FIELD_PIN, pin)
              }
            BluetoothDevice.ACTION_BOND_STATE_CHANGED ->
              postSnippetEvent(callbackId, SnippetConstants.BOND_STATE_CHANGE) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(SnippetConstants.FIELD_STATE, state)
              }
            BluetoothDevice.ACTION_ACL_CONNECTED ->
              postSnippetEvent(callbackId, SnippetConstants.ACL_CONNECTED) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(SnippetConstants.FIELD_TRANSPORT, transport)
              }
            BluetoothDevice.ACTION_ACL_DISCONNECTED ->
              postSnippetEvent(callbackId, SnippetConstants.ACL_DISCONNECTED) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(SnippetConstants.FIELD_TRANSPORT, transport)
              }
            BluetoothDevice.ACTION_FOUND ->
              postSnippetEvent(callbackId, SnippetConstants.DEVICE_FOUND) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putString(SnippetConstants.FIELD_NAME, device?.name)
                putShort(
                  SnippetConstants.FIELD_RSSI,
                  intent.getShortExtra(BluetoothDevice.EXTRA_RSSI, Short.MIN_VALUE),
                )
              }
            BluetoothDevice.ACTION_BATTERY_LEVEL_CHANGED ->
              postSnippetEvent(callbackId, SnippetConstants.BATTERY_LEVEL_CHANGED) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(
                  SnippetConstants.FIELD_VALUE,
                  intent.getIntExtra(BluetoothDevice.EXTRA_BATTERY_LEVEL, BluetoothDevice.ERROR),
                )
              }
            BluetoothDevice.ACTION_UUID ->
              postSnippetEvent(callbackId, SnippetConstants.UUID_CHANGED) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                intent
                  .getParcelableArrayExtra(BluetoothDevice.EXTRA_UUID, ParcelUuid::class.java)
                  ?.let { uuids ->
                    putStringArray(
                      SnippetConstants.FIELD_UUID,
                      uuids.map { it.uuid.toString() }.toTypedArray(),
                    )
                  }
              }
            BluetoothAdapter.ACTION_STATE_CHANGED ->
              postSnippetEvent(callbackId, SnippetConstants.ADAPTER_STATE_CHANGED) {
                putInt(SnippetConstants.FIELD_STATE, adapterState)
              }
            BluetoothDevice.ACTION_NAME_CHANGED ->
              postSnippetEvent(callbackId, SnippetConstants.NAME_CHANGED) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putString(
                  SnippetConstants.FIELD_NAME,
                  intent.getStringExtra(BluetoothDevice.EXTRA_NAME),
                )
              }
            BluetoothDevice.ACTION_ENCRYPTION_CHANGE ->
              postSnippetEvent(callbackId, SnippetConstants.ENCRYPTION_CHANGE) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(SnippetConstants.FIELD_TRANSPORT, transport)
              }
            BluetoothDevice.ACTION_KEY_MISSING ->
              postSnippetEvent(callbackId, SnippetConstants.KEY_MISSING) {
                putString(SnippetConstants.FIELD_DEVICE, device?.address)
                putInt(SnippetConstants.FIELD_STATE, state)
              }
            BluetoothAdapter.ACTION_DISCOVERY_STARTED ->
              postSnippetEvent(callbackId, SnippetConstants.DISCOVERY_STARTED) {}
            BluetoothAdapter.ACTION_DISCOVERY_FINISHED ->
              postSnippetEvent(callbackId, SnippetConstants.DISCOVERY_FINISHED) {}
          }
        }
      }
    context.registerReceiver(
      broadcastReceivers[callbackId],
      intentFilter,
      Context.RECEIVER_EXPORTED,
    )
  }

  /** Removes a [BroadcastReceiver] of [callbackId]. */
  @Rpc(description = "Unregister an adapter callback")
  fun unregisterAdapterCallback(callbackId: String) {
    broadcastReceivers.remove(callbackId)?.let {
      try {
        context.unregisterReceiver(it)
      } catch (e: IllegalArgumentException) {
        Log.w(TAG, "Receiver for $callbackId not registered.")
      }
    }
  }

  /** Returns addresses of bonded devices. */
  @Rpc(description = "Get all bonded devices")
  fun getBondedDevices(): List<String> {
    return bluetoothAdapter.bondedDevices.map { it.address }.toList()
  }

  /** Get bond state of device [address]. */
  @Rpc(description = "Get bond state of device")
  fun getBondState(address: String): Int = bluetoothAdapter.getRemoteDevice(address).bondState

  /** Get identity address of device [address]. */
  @Rpc(description = "Get identity address of remote device")
  fun getIdentityAddress(address: String): String? =
    bluetoothAdapter.getRemoteDevice(address).identityAddress

  /** Returns local Bluetooth Public Address. */
  @Rpc(description = "Get local Bluetooth public address")
  fun getAddress(): String {
    return bluetoothAdapter.address
  }

  /** Returns local Bluetooth name. */
  @Rpc(description = "Get local Bluetooth name")
  fun getName(): String {
    return bluetoothAdapter.name
  }

  /** Sets local Bluetooth name and returns true if successful. */
  @Rpc(description = "Set local Bluetooth name")
  fun setName(name: String): Boolean {
    return bluetoothAdapter.setName(name)
  }

  /** Sets device of [address]'s alias name to [aliasName] and return the status. */
  @Rpc(description = "Set alias name of remote device")
  fun setAlias(address: String, aliasName: String): Int =
    bluetoothAdapter.getRemoteDevice(address).setAlias(aliasName)

  /** Gets device of [address]'s alias name. */
  @Rpc(description = "Get alias name of remote device")
  fun getAlias(address: String): String? = bluetoothAdapter.getRemoteDevice(address).alias

  /** Starts a TAK session for a given device. */
  @AsyncRpc(description = "Start TAK session.")
  @SuppressLint("NewApi")
  fun startTakSession(callbackId: String, address: String, keyList: IntArray, uuidStr: String) {
    val device = bluetoothAdapter.getRemoteDevice(address)
    val key = keyList.map { it.toByte() }.toByteArray()
    val uuid = ParcelUuid(UUID.fromString(uuidStr))

    device.startTakSession(
      key,
      uuid,
      context.mainExecutor,
      object : BluetoothDevice.TakCallback {
        override fun onTakSessionStatusChanged(device: BluetoothDevice, status: TakStatus) {
          postSnippetEvent(callbackId, SnippetConstants.TAK_STATE_CHANGED) {
            putString(SnippetConstants.FIELD_DEVICE, device.address)
            putString(SnippetConstants.FIELD_UUID, status.uuid.toString())
            putInt(SnippetConstants.FIELD_STATE, status.state)
            putInt(SnippetConstants.FIELD_STATUS, status.statusCode)
          }
        }
      },
    )
  }

  /**
   * Creates bond to a remote device with [address] and [addressType] over [transport], and returns
   * true if successful.
   */
  @Rpc(description = "Create bond to a device")
  fun createBond(
    address: String,
    @RpcDefault(BluetoothDevice.TRANSPORT_AUTO.toString(), converter = Utils.IntConverter::class)
    transport: Int = BluetoothDevice.TRANSPORT_AUTO,
    @RpcOptional addressType: Int? = null,
  ): Boolean {
    return when (transport) {
      BluetoothDevice.TRANSPORT_LE ->
        bluetoothAdapter.getRemoteLeDevice(
          address,
          addressType ?: BluetoothDevice.ADDRESS_TYPE_RANDOM,
        )
      BluetoothDevice.TRANSPORT_BREDR -> bluetoothAdapter.getRemoteDevice(address)
      BluetoothDevice.TRANSPORT_AUTO ->
        if (addressType == null) {
          bluetoothAdapter.getRemoteDevice(address)
        } else {
          bluetoothAdapter.getRemoteLeDevice(address, addressType)
        }
      else -> throw IllegalArgumentException("Invalid transport type $transport")
    }.createBond(transport)
  }

  /** Creates bond to a remote device using out of band data, and returns true if successful. */
  @Rpc(description = "Create bond to a device using out of band data")
  fun createBondOutOfBand(
    address: String,
    @RpcDefault(BluetoothDevice.TRANSPORT_AUTO.toString(), converter = Utils.IntConverter::class)
    transport: Int = BluetoothDevice.TRANSPORT_AUTO,
    @RpcOptional addressType: Int? = null,
    @RpcOptional remoteP192data: OobData? = null,
    @RpcOptional remoteP256data: OobData? = null,
  ): Boolean {
    return when (transport) {
      BluetoothDevice.TRANSPORT_LE ->
        bluetoothAdapter.getRemoteLeDevice(
          address,
          addressType ?: BluetoothDevice.ADDRESS_TYPE_RANDOM,
        )
      BluetoothDevice.TRANSPORT_BREDR -> bluetoothAdapter.getRemoteDevice(address)
      BluetoothDevice.TRANSPORT_AUTO ->
        if (addressType == null) {
          bluetoothAdapter.getRemoteDevice(address)
        } else {
          bluetoothAdapter.getRemoteLeDevice(address, addressType)
        }
      else -> throw IllegalArgumentException("Invalid transport type $transport")
    }.createBondOutOfBand(transport, remoteP192data, remoteP256data)
  }

  /** Creates bond to a remote device using out of band data, and returns true if successful. */
  @Rpc(description = "Create bond to a device using out of band data")
  fun generateLocalOobData(transport: Int): OobData {
    val deferred = CompletableDeferred<OobData>()
    val callback =
      object : BluetoothAdapter.OobDataCallback {
        override fun onOobData(transport: Int, data: OobData) {
          Log.i(TAG, "onOobData transport: $transport, data: $data")
          deferred.complete(data)
        }

        override fun onError(errorCode: Int) {
          Log.i(TAG, "onError errorCode: $errorCode")
          deferred.completeExceptionally(
            RuntimeException("Failed to generate local OobData, errorCode=$errorCode")
          )
        }
      }
    bluetoothAdapter.generateLocalOobData(transport, context.mainExecutor, callback)
    return runBlocking {
      withTimeoutOrNull(OOB_DATA_GENERATION_TIMEOUT) { deferred.await() }
        ?: throw RuntimeException(
          "Failed to generate local OobData after ${OOB_DATA_GENERATION_TIMEOUT}"
        )
    }
  }

  @Rpc(description = "Remove bond to a device")
  fun removeBond(address: String): Boolean = bluetoothAdapter.getRemoteDevice(address).removeBond()

  /** Cancel a bond process to the remote device in [address]. */
  @Rpc(description = "Cancel bond to a device")
  fun cancelBond(address: String): Boolean =
    bluetoothAdapter.getRemoteDevice(address).cancelBondProcess()

  /**
   * Connects to a remote device of [address] and returns the status defined in
   * [android.bluetooth.BluetoothStatusCodes].
   */
  @Rpc(description = "Connect to a remote device")
  fun connect(address: String): Int = bluetoothAdapter.getRemoteDevice(address).connect()

  /**
   * Disconnects a remote device of [address] and returns the status defined in
   * [android.bluetooth.BluetoothStatusCodes].
   */
  @Rpc(description = "Disconnect from a remote device")
  fun disconnect(address: String): Int {
    val device = bluetoothAdapter.getRemoteDevice(address)
    return device.disconnect()
  }

  /**
   * Disconnects a remote device of [address] and returns the status defined in
   * [android.bluetooth.BluetoothStatusCodes].
   */
  @Rpc(description = "Disconnect from a remote device")
  fun disconnectAcl(address: String, transport: Int): Int {
    val device = bluetoothAdapter.getRemoteDevice(address)
    return device.javaClass.getMethod("disconnectAcl", Int::class.java).invoke(device, transport)
      as Int
  }

  /** Returns whether a device of [address] is connected on [transport]. (Android U+) */
  @Rpc(description = "Get if a device is connected on given transport (Android U+)")
  fun getDeviceConnected(address: String, transport: Int): Boolean {
    val device = bluetoothAdapter.getRemoteDevice(address)
    return if (Build.VERSION.SDK_INT >= 34) {
      device.getConnectionHandle(transport) != BluetoothDevice.ERROR
    } else {
      // Cannot precisely check connection state by transport before U
      device.isConnected
    }
  }

  /**
   * Sets Bluetooth Pairing confirmation [confirm] to a remote device with [address] and returns
   * true if successful.
   */
  @Rpc(description = "Set Bluetooth Pairing confirmation")
  fun setPairingConfirmation(address: String, confirm: Boolean): Boolean =
    bluetoothAdapter.getRemoteDevice(address).setPairingConfirmation(confirm)

  /**
   * Sets Legacy Pairing PIN Code [pin] to a remote device with [address] and returns true if
   * successful.
   */
  @Rpc(description = "Set Bluetooth Pairing PIN code")
  fun setPin(address: String, pin: String): Boolean =
    bluetoothAdapter.getRemoteDevice(address).setPin(pin)

  /**
   * Triggers service discovery on the given device to fetch UUIDs. Listen for the results using
   * [registerAdapterCallback] and handling the [SnippetConstants.UUID_CHANGED] event.
   */
  @Rpc(description = "Triggers service discovery on the given device to fetch UUIDs.")
  fun fetchUuidsWithSdp(address: String): Boolean {
    @Suppress("DEPRECATION")
    return getDevice(address).fetchUuidsWithSdp()
  }

  /**
   * Gets the a device's Bluetooth class.
   *
   * @throws RuntimeException if the Bluetooth class cannot be retrieved.
   */
  @Rpc(description = "Gets the Bluetooth class of the device.")
  fun getBluetoothClass(address: String): Int {
    return getDevice(address).bluetoothClass?.deviceClass
      ?: throw RuntimeException("Failed to get BluetoothClass for $address, API returned null.")
  }

  /**
   * Gets the cached UUIDs from the device.
   *
   * @throws RuntimeException if the UUIDs cannot be retrieved.
   */
  @Rpc(description = "Gets the device's cached UUIDs.")
  fun getDeviceUuids(address: String): List<String> {
    val device = getDevice(address)
    return device.uuids?.map { it.toString() }
      ?: throw RuntimeException(
        "Failed to get UUIDs for $address, API returned null (is SDP done?)."
      )
  }

  /**
   * Sets Bluetooth scan mode to [scanMode] and returns the status defined in
   * [android.bluetooth.BluetoothStatusCodes].
   */
  @Rpc(description = "Set Bluetooth scan mode")
  fun setScanMode(scanMode: Int): Int = bluetoothAdapter.setScanMode(scanMode)

  /** Gets Bluetooth scan mode. */
  @Rpc(description = "Get Bluetooth scan mode") fun getScanMode(): Int = bluetoothAdapter.scanMode

  /**
   * Starts a BLE advertiser using JSON parameters [advertiseSettings], [advertiseData],
   * [scanResponse], and returns the advertising cookie if advertising successfully or null if
   * failed.
   */
  @Rpc(description = "Start BLE Advertising")
  fun startAdvertising(
    advertiseSettings: AdvertiseSettings,
    @RpcOptional advertiseData: AdvertiseData?,
    @RpcOptional scanResponse: AdvertiseData?,
  ): String = runBlocking {
    val advertiser = bluetoothAdapter.bluetoothLeAdvertiser
    val deferred = CompletableDeferred<Unit>()
    val advertiseCallback =
      object : AdvertiseCallback() {
        override fun onStartSuccess(settingsInEffect: AdvertiseSettings?) {
          deferred.complete(Unit)
        }

        override fun onStartFailure(errorCode: Int) {
          deferred.completeExceptionally(
            RuntimeException("Advertising failed with error code $errorCode")
          )
        }
      }

    advertiser?.startAdvertising(advertiseSettings, advertiseData, scanResponse, advertiseCallback)

    withTimeoutOrNull(ADVERTISING_START_TIMEOUT) { deferred.await() }
      ?: throw RuntimeException("Advertising didn't start after ${ADVERTISING_START_TIMEOUT}")
    val cookie = UUID.randomUUID().toString()
    advertisers[cookie] = advertiseCallback
    cookie
  }

  /**
   * Starts a BLE advertising set using parameters [advertiseSettings], [advertiseData],
   * [scanResponse], [periodicAdvertisingParameters], [periodicAdvertisingParameters], and returns
   * the advertising cookie if advertising successfully or null if failed.
   */
  @Rpc(description = "Start a BLE Advertising Set")
  fun startAdvertisingSet(
    advertiseSetParameters: AdvertisingSetParameters,
    @RpcOptional advertiseData: AdvertiseData?,
    @RpcOptional scanResponse: AdvertiseData?,
    @RpcOptional periodicAdvertisingParameters: PeriodicAdvertisingParameters?,
    @RpcOptional periodicAdvertisingData: AdvertiseData?,
    @RpcDefault("0", converter = Utils.IntConverter::class) duration: Int = 0,
    @RpcDefault("0", converter = Utils.IntConverter::class) maxExtendedAdvertisingEvents: Int = 0,
    @RpcOptional gattServer: String?,
  ): String = runBlocking {
    val deferred = CompletableDeferred<Unit>()
    val advertisingSetCallback =
      object : AdvertisingSetCallback() {
        override fun onAdvertisingSetStarted(
          advertisingSet: AdvertisingSet,
          txPower: Int,
          status: Int,
        ) {
          if (status == AdvertisingSetCallback.ADVERTISE_SUCCESS) {
            deferred.complete(Unit)
          } else {
            deferred.completeExceptionally(
              RuntimeException("Unable to start advertising, status=$status")
            )
          }
        }
      }

    bluetoothAdapter.bluetoothLeAdvertiser?.startAdvertisingSet(
      advertiseSetParameters,
      advertiseData,
      scanResponse,
      periodicAdvertisingParameters,
      periodicAdvertisingData,
      duration,
      maxExtendedAdvertisingEvents,
      gattServer?.let { BluetoothGattServerSnippet.servers[it] },
      advertisingSetCallback,
      Handler(context.mainLooper),
    )

    withTimeoutOrNull(ADVERTISING_START_TIMEOUT) { deferred.await() }
      ?: throw RuntimeException("Advertising didn't start after $ADVERTISING_START_TIMEOUT")
    val cookie = UUID.randomUUID().toString()
    advertisingSets[cookie] = advertisingSetCallback
    cookie
  }

  /** Stops BLE advertiser with [cookie]. */
  @Rpc(description = "Stop BLE Advertising")
  fun stopAdvertising(cookie: String): Unit? =
    advertisers.remove(cookie)?.let { bluetoothAdapter.bluetoothLeAdvertiser?.stopAdvertising(it) }

  /** Stops a BLE advertising set with [cookie]. */
  @Rpc(description = "Stop BLE Advertising Set")
  fun stopAdvertisingSet(cookie: String): Unit? =
    advertisingSets.remove(cookie)?.let {
      bluetoothAdapter.bluetoothLeAdvertiser?.stopAdvertisingSet(it)
    }

  /**
   * Starts a BLE scanner with [scanFilter] and [scanSettings]. The scan results will be pushed to
   * the event queue with [callbackId].
   */
  @AsyncRpc(description = "Start BLE scanning")
  fun startScanning(
    callbackId: String,
    @RpcOptional scanFilter: ScanFilter?,
    @RpcOptional scanSettings: ScanSettings?,
  ) {
    val scanner = bluetoothAdapter.bluetoothLeScanner
    val callback =
      object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
          postSnippetEvent(callbackId, SnippetConstants.SCAN_RESULT) {
            putParcelable(SnippetConstants.SCAN_RESULT, result)
          }
        }

        override fun onBatchScanResults(results: List<ScanResult>) {
          postSnippetEvent(callbackId, SnippetConstants.BATCH_SCAN_RESULTS) {
            putParcelableArray(SnippetConstants.BATCH_SCAN_RESULTS, results.toTypedArray())
          }
        }

        override fun onScanFailed(errorCode: Int) {
          super.onScanFailed(errorCode)
          Log.e(TAG, "onScanFailed $errorCode")
        }
      }

    // Mobly snippet lib cannot pass non-primitive list properly, so we only take 0 or 1 filter
    // here (and it's the most commonly used case).
    val scanFilters = listOfNotNull(scanFilter)
    scanner?.startScan(
      /* filters = */ scanFilters,
      /* settings = */ scanSettings ?: ScanSettings.Builder().build(),
      /* callback = */ callback,
    )
    scanners[callbackId] = callback
  }

  /** Stop BLE scanner with [callbackId]. */
  @Rpc(description = "Stop BLE scanning")
  fun stopScanning(callbackId: String) {
    scanners[callbackId]?.let {
      bluetoothAdapter.bluetoothLeScanner?.stopScan(it)
      scanners.remove(callbackId)
    } ?: throw IllegalArgumentException("Scanner with cookie $callbackId doesn't exist")
  }

  /** Starts Classic inquiry and returns true if inquiry is successfully started. */
  @Rpc(description = "Start Classic inquiry")
  fun startInquiry(): Boolean = bluetoothAdapter.startDiscovery()

  /** Stops Classic inquiry and returns true if inquiry is successfully stopped. */
  @Rpc(description = "Stop Classic inquiry")
  fun stopInquiry(): Boolean = bluetoothAdapter.cancelDiscovery()

  /** Returns true if the local Bluetooth adapter is currently in the device discovery process. */
  @Rpc(description = "Check if Classic inquiry/discovery is active")
  fun isDiscovering(): Boolean = bluetoothAdapter.isDiscovering

  /**
   * Sets Phonebook Access Permission to a remote device of [address] and returns true if
   * successful.
   */
  @Rpc(description = "Set Phonebook Access Permission")
  fun setPhonebookAccessPermission(address: String, permission: Int): Boolean =
    bluetoothAdapter.getRemoteDevice(address).setPhonebookAccessPermission(permission)

  /**
   * Sets Message Access Permission to a remote device of [address] and returns true if successful.
   */
  @Rpc(description = "Set Message Access Permission")
  fun setMessageAccessPermission(address: String, permission: Int): Boolean =
    bluetoothAdapter.getRemoteDevice(address).setMessageAccessPermission(permission)

  /** Sets SIM Access Permission to a remote device of [address] and returns true if successful. */
  @Rpc(description = "Set SIM Access Permission")
  fun setSimAccessPermission(address: String, permission: Int): Boolean =
    bluetoothAdapter.getRemoteDevice(address).setSimAccessPermission(permission)

  /** Registers Bluetooth Quality Report Callback. */
  @AsyncRpc(description = "Register Bluetooth Quality Report Callback")
  fun registerBluetoothQualityReportCallback(callbackId: String) {
    val callback =
      object : BluetoothAdapter.BluetoothQualityReportReadyCallback {
        override fun onBluetoothQualityReportReady(
          device: BluetoothDevice,
          report: BluetoothQualityReport,
          status: Int,
        ) {
          postSnippetEvent(callbackId, SnippetConstants.BLUETOOTH_QUALITY_REPORT) {
            putString(SnippetConstants.FIELD_DEVICE, device.address)
            putInt(SnippetConstants.FIELD_STATUS, status)
            putParcelable(SnippetConstants.FIELD_REPORT, report)
          }
        }
      }
    bluetoothAdapter.registerBluetoothQualityReportReadyCallback(context.mainExecutor, callback)
    bqrCallbacks[callbackId] = callback
  }

  @Rpc(description = "Unregister Bluetooth Quality Report Callback")
  fun unregisterBluetoothQualityReportCallback(callbackId: String) {
    bqrCallbacks.remove(callbackId)?.let {
      bluetoothAdapter.unregisterBluetoothQualityReportReadyCallback(it)
    }
  }

  @Rpc(description = "Get max connected audio devices")
  fun maxConnectedAudioDevices(): Int {
    return bluetoothAdapter.getMaxConnectedAudioDevices()
  }

  /** Returns whether LE Periodic Advertising is supported. */
  @Rpc(description = "Is LE Periodic Advertising Supported")
  fun isLePeriodicAdvertisingSupported(): Boolean =
    bluetoothAdapter.isLePeriodicAdvertisingSupported

  /** Returns the supported profiles. */
  @Rpc(description = "Get supported profiles")
  fun getSupportedProfiles(): List<Int> = bluetoothAdapter.supportedProfiles

  /** Checks if a device is bonded on a specific transport. Return the bond status. */
  @Rpc(description = "Check if a device is bonded on a specific transport")
  fun getBondStatus(address: String, transport: Int): BondStatus? {
    return bluetoothAdapter.getRemoteDevice(address).getBondStatus(transport)
  }

  private companion object {
    const val TAG = "BluetoothAdapterSnippet"
    private val BLUETOOTH_ON_OFF_TIMEOUT = 12.seconds
    private val ADVERTISING_START_TIMEOUT = 3.seconds
    private val OOB_DATA_GENERATION_TIMEOUT = 10.seconds
  }
}
